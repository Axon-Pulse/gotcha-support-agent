"""Structured knowledge-base editing, and the guard that keeps writes inside kb/."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC = {"topics": ["radar", "network"], "symptoms": ["no tracks", "link flapping"],
       "body": "# Radar link flap\n\nThe WebSocket reconnects every few seconds.\n"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "KB", tmp_path)
    (tmp_path / "seed.md").write_text("# Seed\n")
    return TestClient(srv.app)


def test_create_edit_read_delete(client):
    assert client.post("/api/kb", json={"name": "new-entry", **DOC}).json()["ok"]
    got = client.get("/api/kb/new-entry").json()
    assert got["topics"] == ["radar", "network"]
    assert got["symptoms"] == ["no tracks", "link flapping"]
    assert "WebSocket reconnects" in got["body"]

    client.put("/api/kb/new-entry", json={"topics": ["acoustic"], "symptoms": ["silent"],
                                          "body": "# Edited\n"})
    got = client.get("/api/kb/new-entry").json()
    assert (got["topics"], got["symptoms"], got["body"].strip()) == \
           (["acoustic"], ["silent"], "# Edited")

    assert {d["name"] for d in client.get("/api/kb").json()["docs"]} == {"seed", "new-entry"}
    client.delete("/api/kb/new-entry")
    assert [d["name"] for d in client.get("/api/kb").json()["docs"]] == ["seed"]


def test_frontmatter_is_written_as_parseable_yaml(client, tmp_path):
    client.post("/api/kb", json={"name": "fm", **DOC})
    raw = (tmp_path / "fm.md").read_text()
    assert raw.startswith("---\n")
    import yaml
    head = yaml.safe_load(raw.split("---")[1])
    assert head == {"topics": ["radar", "network"],
                    "symptoms": ["no tracks", "link flapping"]}


def test_a_document_with_no_metadata_gets_no_empty_frontmatter(client, tmp_path):
    client.post("/api/kb", json={"name": "plain", "topics": [], "symptoms": [],
                                 "body": "# Plain\n"})
    assert (tmp_path / "plain.md").read_text().strip() == "# Plain"


def test_legacy_documents_without_frontmatter_still_open(client):
    """Every seeded runbook entry predates the structured editor. None may break."""
    got = client.get("/api/kb/seed").json()
    assert got["topics"] == [] and got["symptoms"] == []
    assert got["body"] == "# Seed\n", "the whole file is the body"
    assert client.get("/api/kb").json()["docs"][0]["structured"] is False


def test_saving_a_legacy_document_adds_frontmatter(client, tmp_path):
    got = client.get("/api/kb/seed").json()
    client.put("/api/kb/seed", json={"topics": ["radar"], "symptoms": ["x"],
                                     "body": got["body"]})
    assert (tmp_path / "seed.md").read_text().startswith("---\ntopics:\n- radar")


def test_malformed_frontmatter_falls_back_to_raw_body(client, tmp_path):
    (tmp_path / "broken.md").write_text("---\ntopics: [unclosed\n---\n\n# Body\n")
    got = client.get("/api/kb/broken").json()
    assert got["topics"] == [] and "# Body" in got["body"]


def test_topics_offered_are_seeded_from_the_routing_table(client):
    topics = client.get("/api/kb").json()["all_topics"]
    import graph
    assert set(graph.DOMAINS) <= set(topics), "the common ones are there on a fresh install"


def test_a_case_can_carry_several_topics(client):
    """Found from either direction: the hijack is both a network and a radar case."""
    client.post("/api/kb", json={"name": "multi", "topics": ["network", "radar"],
                                 "symptoms": ["x"], "body": "# M"})
    assert client.get("/api/kb/multi").json()["topics"] == ["network", "radar"]
    row = next(d for d in client.get("/api/kb").json()["docs"] if d["name"] == "multi")
    assert row["topics"] == ["network", "radar"]


def test_duplicate_topics_are_collapsed(client):
    client.post("/api/kb", json={"name": "dup", "topics": ["radar", "radar", " radar "],
                                 "symptoms": ["x"], "body": "# D"})
    assert client.get("/api/kb/dup").json()["topics"] == ["radar"]


@pytest.mark.parametrize("front,expected", [
    ("domain: radar", ["radar"]),
    ("domain:\n- radar\n- network", ["radar", "network"]),
    ("domains:\n- radar", ["radar"]),
    ("topics:\n- radar", ["radar"]),
])
def test_older_spellings_of_the_field_still_open(client, tmp_path, front, expected):
    """Cases written before the rename must keep loading."""
    (tmp_path / "old.md").write_text(f"---\n{front}\n---\n\n# Old\n")
    assert client.get("/api/kb/old").json()["topics"] == expected


def test_duplicate_create_is_refused(client):
    assert client.post("/api/kb", json={"name": "seed", **DOC}).status_code == 409


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..%2f..%2fetc%2fpasswd", "Bad_Name", "UPPER",
    "with space", "", "a" * 80, ".hidden", "sub/dir",
])
def test_writes_cannot_escape_the_kb_directory(client, tmp_path, name):
    r = client.put(f"/api/kb/{name}", json={"domain": "", "symptoms": [], "body": "pwned"})
    assert r.status_code in (400, 404, 405), f"{name!r} was accepted"
    assert not any(p.read_text() == "pwned" for p in tmp_path.rglob("*") if p.is_file())


def test_editing_the_system_model_changes_what_the_model_sees(tmp_path, monkeypatch):
    """The system model IS the prompt; an edit must actually reach it."""
    import llm
    monkeypatch.setattr(llm, "KB_DIR", tmp_path)
    (tmp_path / "system-model.md").write_text("ORIGINAL GUIDANCE")
    assert "ORIGINAL GUIDANCE" in llm._kb()
    (tmp_path / "system-model.md").write_text("REPLACED GUIDANCE")
    assert "REPLACED GUIDANCE" in llm._kb()
    assert "ORIGINAL GUIDANCE" not in llm._kb()


def test_a_case_beside_the_system_model_still_stays_out_of_the_prompt(tmp_path, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "KB_DIR", tmp_path)
    (tmp_path / "system-model.md").write_text("THE MODEL")
    (tmp_path / "a-case.md").write_text("A RECORDED CASE")
    assert llm._kb() == "THE MODEL"


def test_structured_fields_reach_the_search_index(client, tmp_path, monkeypatch):
    """Domain and symptoms are what a ticket is matched against. Cases are not in the
    prompt any more, so they have to be reachable through the index instead."""
    import tools.kb as kbtool
    client.post("/api/kb", json={"name": "fm", **DOC})
    monkeypatch.setattr(kbtool, "KB_DIR", tmp_path)
    hits = kbtool.search_runbook("link flapping radar")["hits"]
    assert [h["doc"] for h in hits] == ["fm"]


# ---------- the two-tier runbook ----------
# The KB is split by lifecycle: a code-derived system model that is always in the prompt,
# and a case history that grows and is retrieved. These read the real kb/, not a tmp_path,
# because both halves are shipped artefacts.

KB_ROOT = ROOT / "kb"
SYSTEM_MODEL = KB_ROOT / "system-model.md"
CASES = KB_ROOT / "cases"


def test_the_system_model_exists_and_is_bounded():
    assert SYSTEM_MODEL.exists(), "with no system model a novel fault has nothing to reason from"
    size = SYSTEM_MODEL.stat().st_size
    assert size < 40_000, f"it is in every prompt; {size} B is too much to carry"


def test_only_the_system_model_reaches_the_prompt():
    """Cases are retrieved, not carried. This is what lets the history grow."""
    import llm
    kb = llm._kb()
    assert "How this system works" in kb
    for case in CASES.glob("*.md"):
        title = case.read_text().split("# ", 1)[-1].splitlines()[0]
        assert title not in kb, f"case {case.stem!r} is in the prompt; it should be searched"


def test_cases_are_searchable_and_the_system_model_is_not(tmp_path, monkeypatch):
    """Searching something already in context would spend a turn for nothing.

    Uses its own case rather than a shipped one — which cases exist is the operator's
    business, and deleting one must not fail the suite.
    """
    import tools.kb as kbtool
    # Mirror the real layout: the system model sits BESIDE the cases directory, which is
    # what keeps it out of the index — it is already in the prompt.
    cases = tmp_path / "cases"
    cases.mkdir()
    (tmp_path / "system-model.md").write_text("# How this system works\n\n"
                                              "A tailscale subnet route can hijack the LAN.\n")
    (cases / "a-case.md").write_text("---\ntopics:\n- network\n---\n\n"
                                     "# Tailnet route hijack\n\n"
                                     "A tailscale subnet route was preferred.\n")
    monkeypatch.setattr(kbtool, "KB_DIR", cases)
    hits = kbtool.search_runbook("tailscale subnet route")["hits"]
    assert [h["doc"] for h in hits] == ["a-case"], "cases only"


def test_a_pattern_with_no_case_returns_nothing():
    """No hits is the signal to reason from the system model. It must not be faked by
    one everyday word in common."""
    from registry import call, load_tools
    load_tools()
    assert call("search_runbook",
                {"query": "something nobody has ever seen before"})["data"]["hits"] == []


def test_every_case_is_structured():
    """Structure, not completeness.

    A case created a minute ago and not yet written up is a normal state, so demanding
    symptoms here would turn ordinary work into a red suite. The console flags a case
    with no symptoms instead, which is where someone can act on it.
    """
    import console.server as srv
    assert list(CASES.glob("*.md")), "no cases recorded"
    for p in sorted(CASES.glob("*.md")):
        parsed = srv._parse_doc(p.read_text())
        assert parsed["topics"], f"{p.name} declares no topics"
        assert parsed["body"].strip(), f"{p.name} has no body"


def test_a_saved_case_matches_what_the_editor_reads():
    """graph.save writes cases; the console edits them. One shape, or edits drift."""
    import console.server as srv
    import graph
    md = graph._render({"id": "x", "title": "T", "topics": ["radar", "network"],
                        "symptoms": ["a", "b"], "root_cause": "rc",
                        "checks": ["c1"], "fix": "f"})
    parsed = srv._parse_doc(md)
    assert parsed["topics"] == ["radar", "network"] and parsed["symptoms"] == ["a", "b"]
    assert srv._render_doc(parsed["topics"], parsed["symptoms"], parsed["body"]) == md


def test_approved_cases_land_in_the_cases_directory():
    import graph
    assert graph.KB_DIR == CASES


# ---------- the system model must not drift from the code ----------

def test_the_system_model_names_every_registered_tool():
    """A tool the model cannot read about is a tool it will not use well."""
    from registry import REGISTRY, load_tools
    load_tools()
    text = SYSTEM_MODEL.read_text()
    missing = sorted(n for n in REGISTRY if n not in text)
    assert missing == [], f"undocumented tools: {missing}"


def test_the_system_model_lists_every_probe_verdict():
    """A verdict nobody documented is one the agent will mishandle."""
    import re
    src = (ROOT / "tools" / "net.py").read_text()
    verdicts = set(re.findall(r'verdict, liveness = "([a-z_]+)"', src))
    assert len(verdicts) == 4, f"parser drifted: {verdicts}"
    text = SYSTEM_MODEL.read_text()
    assert sorted(v for v in verdicts if v not in text) == []


GOTCHA30 = Path(__import__("os").environ.get("GOTCHA30_REPO",
                                             "/home/gal/Documents/code/gotcha30"))
needs_repo = pytest.mark.skipif(not GOTCHA30.exists(), reason="gotcha30 checkout absent")


@needs_repo
def test_the_documented_health_timeout_matches_the_gateway():
    import re
    src = (GOTCHA30 / "GUItcha30/gateway/src/adapters/glue/store.py").read_text()
    value = re.search(r"health_timeout_seconds\s*=\s*([\d.]+)", src).group(1)
    assert f"health_timeout_seconds = {value}" in SYSTEM_MODEL.read_text()


@needs_repo
def test_the_documented_process_states_match_the_proto():
    import re
    proto = (GOTCHA30 / "src/glue/messages/Launcher.proto").read_text()
    block = proto.split("enum ProcessState")[1].split("}")[0]
    states = re.findall(r"(PROC_[A-Z_]+)\s*=", block)
    text = SYSTEM_MODEL.read_text()
    assert sorted(s for s in states if s not in text) == [], "undocumented process states"


# ---------- adding a case, and adding a domain along with it ----------

def test_creating_a_case_makes_a_stub_for_the_editor_to_fill(client, tmp_path):
    """Create is step one of two: it writes the file, the editor fills it in."""
    assert client.post("/api/kb", json={"name": "new-fault", "topics": ["radar"],
                                        "symptoms": [], "body": ""}).json()["ok"]
    got = client.get("/api/kb/new-fault").json()
    assert got["topics"] == ["radar"]
    assert got["symptoms"] == [], "symptoms are filled in the editor, not at create time"
    for heading in ("## Root cause", "## Checks (read-only)", "## Fix"):
        assert heading in got["body"], f"the stub should prompt for {heading}"


def test_a_brand_new_topic_is_accepted_and_then_offered(client):
    """Using an unknown topic is how a topic is added; no separate step exists."""
    assert "hydraulics" not in client.get("/api/kb").json()["all_topics"]
    client.post("/api/kb", json={"name": "pump-stall", "topics": ["hydraulics"],
                                 "symptoms": ["the mast will not raise"], "body": "# Pump"})
    assert "hydraulics" in client.get("/api/kb").json()["all_topics"]
    assert client.get("/api/kb/pump-stall").json()["topics"] == ["hydraulics"]


def test_a_topic_does_not_become_a_routing_domain(client):
    """Topics match cases. Gating agents is graph.DOMAINS' job, not the runbook's."""
    import graph
    client.post("/api/kb", json={"name": "pump-stall", "topics": ["hydraulics"],
                                 "symptoms": ["x"], "body": "# Pump"})
    assert "hydraulics" not in graph.DOMAINS


# ---------- a topic outlives the tab that created it ----------

def test_a_new_topic_survives_without_a_case_using_it(client, tmp_path, monkeypatch):
    """The list is otherwise derived from what cases carry, so a topic on no case would
    disappear on the next page load. It is persisted instead."""
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    assert "tower" not in client.get("/api/kb").json()["all_topics"]
    r = client.post("/api/topics", json={"name": "tower"}).json()
    assert "tower" in r["all_topics"] and r["removable_topics"] == ["tower"]
    assert "tower" in client.get("/api/kb").json()["all_topics"], "survives a reload"


@pytest.mark.parametrize("bad", ["with space", "", "9lives", "x" * 40, "a/b", "-x"])
def test_a_malformed_topic_is_refused(client, tmp_path, monkeypatch, bad):
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    assert client.post("/api/topics", json={"name": bad}).status_code == 400


@pytest.mark.parametrize("typed,stored", [("Tower", "tower"), ("  RADAR ", "radar")])
def test_case_and_padding_are_normalised_rather_than_rejected(client, tmp_path,
                                                              monkeypatch, typed, stored):
    """Typing it capitalised is a slip, not an error worth a dialog."""
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    assert stored in client.post("/api/topics", json={"name": typed}).json()["all_topics"]


def test_an_unused_topic_can_be_removed(client, tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    client.post("/api/topics", json={"name": "tower"})
    r = client.delete("/api/topics/tower").json()
    assert "tower" not in r["all_topics"] and r["removable_topics"] == []


def test_a_topic_a_case_uses_cannot_be_removed(client, tmp_path, monkeypatch):
    """It would reappear on the next rebuild, so offering the button would be a lie."""
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    client.post("/api/kb", json={"name": "c1", "topics": ["tower"], "symptoms": ["x"],
                                 "body": "# C"})
    r = client.delete("/api/topics/tower")
    assert r.status_code == 400 and "in use by a case" in r.text
    assert "tower" in client.get("/api/kb").json()["all_topics"]


def test_a_topic_in_use_is_not_offered_as_removable(client, tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    client.post("/api/topics", json={"name": "tower"})
    client.post("/api/kb", json={"name": "c1", "topics": ["tower"], "symptoms": ["x"],
                                 "body": "# C"})
    assert client.get("/api/kb").json()["removable_topics"] == []


def test_seeded_topics_are_never_removable(client, tmp_path, monkeypatch):
    """radar comes from graph.DOMAINS; deleting it here would not stick."""
    import console.server as srv
    monkeypatch.setattr(srv, "TOPICS_FILE", tmp_path / "topics.json")
    client.post("/api/topics", json={"name": "radar"})
    assert "radar" not in client.get("/api/kb").json()["removable_topics"]


# ---------- the filename is derived, never typed ----------

SLUG_CASES = [
    ("Radar WebSocket Flap", "radar-websocket-flap"),
    ("  ASU backend won't start!!  ", "asu-backend-won-t-start"),
    ("tracks__in  the///wrong place", "tracks-in-the-wrong-place"),
    ("Café outage — déjà vu", "cafe-outage-deja-vu"),
    ("MAGOS-AR300", "magos-ar300"),
    ("2 sensors down", "2-sensors-down"),
    ("../../etc/passwd", "etc-passwd"),
    ("-leading and trailing-", "leading-and-trailing"),
    ("x" * 90, "x" * 64),
    ("!!!", ""),
    ("", ""),
]


@pytest.mark.parametrize("raw,slug", SLUG_CASES)
def test_free_text_becomes_a_kebab_case_name(raw, slug):
    import console.server as srv
    assert srv._slugify(raw) == slug


def test_creating_with_free_text_names_the_file_and_reports_it(client, tmp_path):
    r = client.post("/api/kb", json={"name": "Radar WebSocket keeps flapping!",
                                     "topics": ["radar"], "symptoms": [], "body": ""})
    assert r.status_code == 200
    assert r.json()["name"] == "radar-websocket-keeps-flapping", \
        "the caller needs the real name to open the editor"
    assert (tmp_path / "radar-websocket-keeps-flapping.md").exists()
    assert client.get("/api/kb/radar-websocket-keeps-flapping").status_code == 200


def test_a_name_with_nothing_to_slug_is_refused(client):
    r = client.post("/api/kb", json={"name": "!!! ???", "topics": [], "symptoms": [],
                                     "body": ""})
    assert r.status_code == 400 and "no letters or digits" in r.text


def test_a_traversal_attempt_is_confined_not_followed(client, tmp_path):
    client.post("/api/kb", json={"name": "../../etc/passwd", "topics": [], "symptoms": [],
                                 "body": "x"})
    assert (tmp_path / "etc-passwd.md").exists()
    assert not any(p.name == "passwd" for p in tmp_path.parent.rglob("*") if p.is_file())


def test_the_stub_heading_follows_the_slug_not_the_raw_text(client, tmp_path):
    client.post("/api/kb", json={"name": "Radar WebSocket Flap", "topics": [],
                                 "symptoms": [], "body": ""})
    assert "# Radar Websocket Flap" in (tmp_path / "radar-websocket-flap.md").read_text()


def test_editing_and_deleting_still_refuse_a_raw_name(client, tmp_path):
    """Only creation slugifies. Every other route must keep resolving names strictly, or
    a request for one file could quietly reach another."""
    assert client.put("/api/kb/Not A Slug",
                      json={"topics": [], "symptoms": [], "body": "x"}).status_code in (400, 404)
    assert client.delete("/api/kb/Not A Slug").status_code in (400, 404)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_browser_preview_agrees_with_the_server():
    """The page previews the filename before it exists. If the two rules drift, the
    preview quietly lies about where the case is about to land."""
    import console.server as srv
    raws = [r for r, _ in SLUG_CASES]
    out = subprocess.run(["node", str(ROOT / "tests" / "slug_compare.js"), json.dumps(raws)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [srv._slugify(r) for r in raws]


# ---------- the body is edited as sections, not one box ----------

def test_a_body_splits_into_its_sections():
    import console.server as srv
    sec = srv._split_body(
        "# A radar fault\n\nSeen on the bench.\n\n## Root cause\n\nThe link flapped.\n\n"
        "## Checks (read-only)\n\n    ip route get 1.2.3.4\n\n## Fix\n\nReseat it.\n")
    assert sec["title"] == "A radar fault"
    assert sec["intro"] == "Seen on the bench."
    assert sec["root_cause"] == "The link flapped."
    assert sec["checks"] == "    ip route get 1.2.3.4"
    assert sec["fix"] == "Reseat it."
    assert sec["extra"] == ""


def test_an_unknown_heading_is_preserved_not_dropped():
    """An editor that silently deletes what it cannot display is worse than one textarea."""
    import console.server as srv
    sec = srv._split_body("# T\n\n## Root cause\n\nrc\n\n## Evidence\n\nthe capture\n")
    assert sec["root_cause"] == "rc"
    assert "## Evidence" in sec["extra"] and "the capture" in sec["extra"]
    assert "Evidence" in srv._join_body(sec), "and it survives the round trip"


def test_headings_are_matched_case_insensitively():
    import console.server as srv
    assert srv._split_body("# T\n\n## ROOT CAUSE\n\nrc\n")["root_cause"] == "rc"


def test_a_duplicate_heading_is_kept_rather_than_overwriting():
    import console.server as srv
    sec = srv._split_body("# T\n\n## Fix\n\nfirst\n\n## Fix\n\nsecond\n")
    assert sec["fix"] == "first" and "second" in sec["extra"]


def test_splitting_and_rejoining_is_idempotent():
    """Whitespace settles on one canonical shape, then stops moving."""
    import console.server as srv
    for p in sorted(CASES.glob("*.md")):
        body = srv._parse_doc(p.read_text())["body"]
        once = srv._join_body(srv._split_body(body))
        assert once == srv._join_body(srv._split_body(once)), p.name


def test_saving_sections_writes_the_same_markdown_shape(client, tmp_path):
    client.post("/api/kb", json={"name": "sec-case", "topics": ["radar"],
                                 "symptoms": ["x"], "body": ""})
    client.put("/api/kb/sec-case", json={"topics": ["radar"], "symptoms": ["x"],
        "sections": {"title": "T", "intro": "ctx", "root_cause": "rc",
                     "checks": "    cmd", "fix": "f", "extra": ""}})
    raw = (tmp_path / "sec-case.md").read_text()
    assert "# T\n\nctx\n\n## Root cause\n\nrc\n\n## Checks (read-only)\n\n    cmd\n\n## Fix\n\nf" in raw
    back = client.get("/api/kb/sec-case").json()["sections"]
    assert (back["title"], back["root_cause"], back["checks"], back["fix"]) == \
           ("T", "rc", "    cmd", "f")


def test_an_agent_written_case_opens_in_the_same_form():
    """graph._render and the editor must agree, or a proposed case looks malformed."""
    import console.server as srv
    import graph
    md = graph._render({"id": "x", "title": "T", "topics": ["radar"], "symptoms": ["s"],
                        "root_cause": "rc", "checks": ["c1", "c2"], "fix": "f"})
    sec = srv._split_body(srv._parse_doc(md)["body"])
    assert sec["title"] == "T" and sec["root_cause"] == "rc" and sec["fix"] == "f"
    assert sec["checks"] == "    c1\n    c2"
    assert sec["extra"] == "", "nothing should land in the catch-all"


def test_a_raw_body_is_still_accepted(client, tmp_path):
    """Older callers send markdown; that must keep working."""
    client.post("/api/kb", json={"name": "raw", "topics": ["radar"], "symptoms": ["x"],
                                 "body": "# Raw\n\n## Root cause\n\nrc\n"})
    assert client.get("/api/kb/raw").json()["sections"]["root_cause"] == "rc"
