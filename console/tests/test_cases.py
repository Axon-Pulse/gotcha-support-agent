"""Cases, their SKILL.md index rows and their signatures, edited through the API."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from console import kb
from console.settings import S
from console.tests.conftest import ROOT

REAL_CASES = sorted((ROOT / "support-agent-skill" / "kb" / "cases").glob("*.md"))


def _skill_md() -> str:
    return (S.skill_dir / "SKILL.md").read_text()


def _sigs() -> str:
    return (S.skill_dir / "scripts" / "signatures.txt").read_text()


# --------------------------------------------------------------------------
# A case file round-trips
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", REAL_CASES, ids=lambda p: p.stem)
def test_every_real_case_survives_split_and_join_or_opens_as_markdown(path):
    text = path.read_text()
    parts = kb.split(text)
    if parts["structured"]:
        assert kb.join(parts) == text
    assert parts["title"], "every case has an H1"


def test_nearly_every_real_case_opens_in_the_form():
    structured = [kb.split(p.read_text())["structured"] for p in REAL_CASES]
    assert sum(structured) >= len(structured) - 2


def test_a_comment_in_a_code_block_is_not_a_heading():
    text = ("---\ntopics:\n- radar\nsymptoms:\n- x\n---\n\n# Title\n\n## Checks\n\n```sh\n"
            "# show the link\n## not a section\nip link\n```\n\n## Fix\n\nReseat it.\n")
    parts = kb.split(text)
    assert [s["heading"] for s in parts["sections"]] == ["Checks", "Fix"]
    assert "## not a section" in parts["sections"][0]["body"]
    assert parts["structured"] and kb.join(parts) == text


def test_sections_keep_their_order_and_indentation():
    parts = {"topics": ["radar"], "symptoms": ["no detections"], "title": "T", "intro": "",
             "sections": [{"heading": "Fix", "body": "a"},
                          {"heading": "Checks (read-only)", "body": "    ip route get 1.2.3.4"}]}
    again = kb.split(kb.join(parts))
    assert again["sections"] == parts["sections"]
    assert again["structured"]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_the_list_shows_index_and_signature_coverage(client):
    r = client.get("/api/cases").json()
    by = {c["slug"]: c for c in r["cases"]}
    assert len(by) == len(REAL_CASES)
    assert by["radar-transmitter-off"]["indexed"] and by["radar-transmitter-off"]["signatures"]
    assert not by["two-launcher-sessions"]["signatures"]
    assert "radar" in r["topics"]


def test_a_case_opens_with_its_index_row_and_signatures(client):
    d = client.get("/api/cases/asu-backend-never-reachable").json()
    assert d["index"]["says"] and d["index"]["evidence"]
    # The line's ERE alternatives, split only at a top-level |, never inside (...)
    assert len(d["signatures"]) == 3
    assert any("(HTTP GET .* failed|Couldn't connect to server)" in s for s in d["signatures"])
    assert d["etag"] and d["raw"].startswith("---\n")


@pytest.mark.parametrize("name", ["../SKILL", "..%2fSKILL", "UPPER", "a/b", ".hidden", "x" * 70])
def test_a_case_name_cannot_leave_the_cases_directory(client, name):
    assert client.get(f"/api/cases/{name}").status_code in (400, 404)


def test_an_unknown_case_is_404(client):
    assert client.get("/api/cases/no-such-case").status_code == 404


# --------------------------------------------------------------------------
# Creating, saving, renaming, deleting
# --------------------------------------------------------------------------

def _new(client, name="Radar WebSocket keeps flapping", topics=("radar",)):
    r = client.post("/api/cases", json={"name": name, "topics": list(topics)})
    assert r.status_code == 200, r.text
    return r.json()


def _fill(client, slug, **over):
    d = client.get(f"/api/cases/{slug}").json()
    body = {"base": d["etag"], "topics": d["topics"], "symptoms": ["the radar keeps reconnecting"],
            "title": d["title"], "intro": "First seen on axon-gotcha-4.",
            "sections": [{"heading": s["heading"], "body": "Something."} for s in d["sections"]],
            "index": {"says": '"radar keeps dropping"', "evidence": "`[radar-websocket-keeps-flapping]` in the radar log"},
            "signatures": ["websocket closed unexpectedly", "reconnect attempt [0-9]+"]}
    body.update(over)
    return client.put(f"/api/cases/{slug}", json=body)


def test_a_new_case_gets_the_usual_sections_and_fails_selftest_until_indexed(client):
    r = _new(client)
    assert r["slug"] == "radar-websocket-keeps-flapping"
    d = client.get("/api/cases/radar-websocket-keeps-flapping").json()
    assert [s["heading"] for s in d["sections"]] == kb.SEEDED_SECTIONS
    assert d["title"] == "Radar WebSocket keeps flapping"
    assert d["topics"] == ["radar"]
    assert not r["selftest"]["ok"]
    assert any("radar-websocket-keeps-flapping" in f for f in r["selftest"]["failures"])


def test_filling_in_the_index_row_makes_selftest_pass(client):
    _new(client)
    r = _fill(client, "radar-websocket-keeps-flapping")
    assert r.status_code == 200, r.text
    assert r.json()["selftest"]["ok"], r.json()["selftest"]["failures"]
    assert "| `radar-websocket-keeps-flapping` | \"radar keeps dropping\" |" in _skill_md()
    assert ("radar-websocket-keeps-flapping|websocket closed unexpectedly|reconnect attempt [0-9]+"
            in _sigs().splitlines())


def test_an_index_row_edit_touches_only_that_line(client):
    before = _skill_md().split("\n")
    d = client.get("/api/cases/radar-transmitter-off").json()
    r = client.put("/api/cases/radar-transmitter-off", json={
        "base": d["etag"], "raw": d["raw"], "index": {"says": '"no detections"', "evidence": "`radar=stopped`"}})
    assert r.status_code == 200, r.text
    after = _skill_md().split("\n")
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert after[changed[0]] == '| `radar-transmitter-off` | "no detections" | `radar=stopped` |'


def test_saving_a_case_unchanged_writes_nothing(client):
    p = S.cases_dir / "radar-transmitter-off.md"
    before, mtime = p.read_text(), p.stat().st_mtime_ns
    d = client.get("/api/cases/radar-transmitter-off").json()
    r = client.put("/api/cases/radar-transmitter-off", json={
        "base": d["etag"], "topics": d["topics"], "symptoms": d["symptoms"], "title": d["title"],
        "intro": d["intro"], "sections": d["sections"], "signatures": d["signatures"],
        "index": d["index"]})
    assert r.status_code == 200, r.text
    assert p.read_text() == before and p.stat().st_mtime_ns == mtime
    assert r.json()["selftest"]["ok"]


def test_a_save_over_someone_elses_newer_save_is_refused(client):
    d = client.get("/api/cases/radar-transmitter-off").json()
    (S.cases_dir / "radar-transmitter-off.md").write_text(d["raw"] + "\nsomeone else's line\n")
    r = client.put("/api/cases/radar-transmitter-off", json={"base": d["etag"], "raw": d["raw"]})
    assert r.status_code == 400 and "someone else" in r.json()["detail"]


@pytest.mark.parametrize("raw, why", [
    ("# no frontmatter\n", "frontmatter"),
    ("---\ntopics: [radar\n---\n# x\n", "YAML"),
    ("---\ntopics: radar\nsymptoms: []\n---\n# x\n", "lists"),
])
def test_bad_markdown_is_refused_and_nothing_is_written(client, raw, why):
    p = S.cases_dir / "radar-transmitter-off.md"
    before = p.read_text()
    r = client.put("/api/cases/radar-transmitter-off", json={"raw": raw})
    assert r.status_code == 400 and why in r.json()["detail"]
    assert p.read_text() == before


@pytest.mark.parametrize("over, why", [
    ({"topics": ["Not A Topic"]}, "topic"),
    ({"title": ""}, "title"),
    ({"sections": [{"heading": "", "body": "x"}]}, "heading"),
    ({"index": {"says": "a | b", "evidence": "x"}}, "|"),
    ({"index": {"says": "a", "evidence": ""}}, "evidence"),
    ({"signatures": ["(unclosed"]}, "regex"),
    ({"signatures": ["x|"]}, "empty line"),
    ({"signatures": ["foo__GOTCHA_SIGS__"]}, "heredoc"),
    ({"signatures": ["tab\there"]}, "control"),
])
def test_invalid_parts_are_refused_before_anything_is_written(client, over, why):
    _new(client)
    files = {p: p.read_text() for p in [S.skill_dir / "SKILL.md", S.skill_dir / "scripts" / "signatures.txt",
                                        S.cases_dir / "radar-websocket-keeps-flapping.md"]}
    r = _fill(client, "radar-websocket-keeps-flapping", **over)
    assert r.status_code == 400, r.text
    assert why in r.json()["detail"]
    assert all(p.read_text() == t for p, t in files.items())


def test_a_pipe_inside_code_is_fine_in_an_index_cell(client):
    _new(client)
    r = _fill(client, "radar-websocket-keeps-flapping",
              index={"says": '"drops"', "evidence": "`grep -E 'a|b'` finds it"})
    assert r.status_code == 200, r.text
    assert kb.index_rows()["radar-websocket-keeps-flapping"]["evidence"] == "`grep -E 'a|b'` finds it"


def test_removing_every_signature_removes_the_line(client):
    d = client.get("/api/cases/radar-transmitter-off").json()
    r = client.put("/api/cases/radar-transmitter-off", json={"base": d["etag"], "raw": d["raw"], "signatures": []})
    assert r.status_code == 200
    assert not any(x.startswith("radar-transmitter-off|") for x in _sigs().splitlines())
    assert r.json()["selftest"]["ok"]


def test_rename_moves_the_file_index_row_and_signature(client):
    r = client.post("/api/cases/radar-transmitter-off/rename", json={"name": "Radar TX off"})
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "radar-tx-off"
    assert (S.cases_dir / "radar-tx-off.md").exists()
    assert not (S.cases_dir / "radar-transmitter-off.md").exists()
    assert "`radar-tx-off`" in _skill_md() and "| `radar-transmitter-off` |" not in _skill_md()
    assert any(x.startswith("radar-tx-off|radar=stopped") for x in _sigs().splitlines())
    assert r.json()["selftest"]["ok"], r.json()["selftest"]["failures"]


def test_a_case_with_no_index_row_or_signature_can_be_renamed(client):
    _new(client)
    r = client.post("/api/cases/radar-websocket-keeps-flapping/rename", json={"name": "radar flaps"})
    assert r.status_code == 200, r.text
    assert (S.cases_dir / "radar-flaps.md").exists()
    assert "radar-flaps" not in _skill_md() and "radar-flaps" not in _sigs()


def test_rename_onto_an_existing_case_is_refused(client):
    r = client.post("/api/cases/radar-transmitter-off/rename", json={"name": "no-video-in-ui"})
    assert r.status_code == 409


def test_delete_removes_the_case_its_row_and_its_signature(client):
    r = client.request("DELETE", "/api/cases/radar-transmitter-off", json={})
    assert r.status_code == 200, r.text
    assert not (S.cases_dir / "radar-transmitter-off.md").exists()
    assert "`radar-transmitter-off`" not in _skill_md()
    assert not any(x.startswith("radar-transmitter-off|") for x in _sigs().splitlines())
    assert r.json()["selftest"]["ok"], r.json()["selftest"]["failures"]


def test_a_case_mentioned_elsewhere_reports_where(client, env):
    other = S.cases_dir / "no-video-in-ui.md"
    other.write_text(other.read_text() + "\nSee also `radar-transmitter-off`.\n")
    d = client.get("/api/cases/radar-transmitter-off").json()
    assert "kb/cases/no-video-in-ui.md" in d["references"]
    assert "SKILL.md" not in d["references"], "its own index row is not a reference"


def test_an_existing_case_name_is_a_conflict(client):
    assert client.post("/api/cases", json={"name": "radar-transmitter-off"}).status_code == 409


def test_a_name_with_no_letters_is_refused(client):
    assert client.post("/api/cases", json={"name": "!!!"}).status_code == 400


# --------------------------------------------------------------------------
# Signatures and reference docs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ere, alts", [
    ("a|b", ["a", "b"]),
    (r"\[asu[0-9]*\].*(HTTP GET .* failed|refused)|x", [r"\[asu[0-9]*\].*(HTTP GET .* failed|refused)", "x"]),
    ("[|]x|y", ["[|]x", "y"]),
    (r"a\|b|c", [r"a\|b", "c"]),
    ("[]|]x|y", ["[]|]x", "y"]),
])
def test_alternatives_split_only_at_the_top_level(ere, alts):
    assert kb.split_alternatives(ere) == alts
    assert "|".join(kb.split_alternatives(ere)) == ere


def test_every_real_signature_line_passes_the_checks(env):
    kb.check_signatures_text(_sigs())


@pytest.mark.parametrize("line, why", [
    ("no-such-case|foo", "no case"),
    ("radar-transmitter-off", "expected"),
    ("radar-transmitter-off|(", "regex"),
    ("radar-transmitter-off|__GOTCHA_SIGS__", "heredoc"),
    ("Bad Slug|x", "slug"),
])
def test_the_signatures_file_is_checked_as_a_whole(client, line, why):
    d = client.get("/api/docs/signatures").json()
    r = client.put("/api/docs/signatures", json={"text": d["text"] + line + "\n", "base": d["etag"]})
    assert r.status_code == 400 and why in r.json()["detail"]


def test_a_reference_doc_saves_and_skill_md_does_not(client):
    d = client.get("/api/docs/glossary").json()
    r = client.put("/api/docs/glossary", json={"text": d["text"] + "\nNew term.\n", "base": d["etag"]})
    assert r.status_code == 200 and (S.skill_dir / "kb" / "glossary.md").read_text().endswith("New term.\n")
    s = client.get("/api/docs/skill").json()
    assert s["read_only"]
    assert client.put("/api/docs/skill", json={"text": "x"}).status_code == 400
    assert client.get("/api/docs/../SKILL").status_code == 404


def test_every_write_is_audited_with_who_made_it(client):
    _new(client)
    client.headers.update({"tailscale-user-login": "PM@example.com"})
    _fill(client, "radar-websocket-keeps-flapping")
    log = [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]
    assert [(e["action"], e["who"]) for e in log] == [("create-case", "local"),
                                                     ("save-case", "pm@example.com")]


# --------------------------------------------------------------------------
# The page's slugify agrees with the server's
# --------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_pages_slug_preview_matches_the_server():
    raws = ["Radar WebSocket keeps flapping", "  --ASU  down--  ", "Ünïcödé café", "a" * 80 + "-b",
            "שלום radar", "x--y", "!!!", "UPPER_case 42", "trailing-" + "z" * 60 + "-"]
    out = subprocess.run(["node", str(Path(__file__).parent / "page_harness.js"), "slugify",
                          json.dumps(raws)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [kb.slugify(r) for r in raws]
