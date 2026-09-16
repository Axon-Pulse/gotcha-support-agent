"""The contract between console/index.html and console/server.py.

The UI is untested JavaScript; these assertions are what stop a backend edit from
silently blanking a tab. They pin the endpoints the page calls and the fields it reads.
"""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inventory  # noqa: E402
import secrets_store  # noqa: E402

HTML = (ROOT / "console" / "index.html").read_text()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "KB", tmp_path / "kb")
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "seed.md").write_text("# Seed\n")
    monkeypatch.setattr(inventory, "SYSTEMS", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(secrets_store, "PATH", tmp_path / "secrets.local.env")
    srv._reload_inventory()
    yield TestClient(srv.app)
    srv._reload_inventory()


def test_every_endpoint_the_page_calls_exists(client):
    """Catches a renamed route before a tab renders empty in a browser."""
    import console.server as srv
    routes = {r.path for r in srv.app.routes}
    called = {m.group(1) for m in re.finditer(r"api\('(/[a-z_/-]+)", HTML)}
    missing = []
    for path in sorted(called):
        full = "/api" + path
        if full in routes:
            continue
        # Parameterised routes: /api/kb/x matches /api/kb/{name}
        if not any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", r), full + "x")
                   or re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", r), full)
                   for r in routes):
            missing.append(full)
    assert missing == [], f"page calls routes the server does not serve: {missing}"


def test_meta_has_what_the_header_and_tools_tab_read(client):
    m = client.get("/api/meta").json()
    assert {"mode", "model", "credentials", "tools", "order", "permissions"} <= set(m)
    assert {"name", "side_effect", "description", "default_description", "params"} <= set(m["tools"][0])
    assert "allowed_commands" in m["permissions"]


def test_config_has_the_supervisor_card_and_editable_agents(client):
    c = client.get("/api/config").json()
    assert {"role", "picks", "max_steps", "eligible_pool", "read_only"} <= set(c["supervisor"])
    assert {"agents", "order", "requires", "supervisor_picks", "post_agents",
            "allowed_commands", "tool_descriptions"} <= set(c["effective"])


def test_graph_has_what_the_flow_editor_needs(client):
    g = client.get("/api/graph").json()
    assert {"agents", "order", "supervisor_picks", "post_agents", "max_steps"} <= set(g)
    assert {"name", "tools", "requires", "in_order"} <= set(g["agents"][0])
    assert "mermaid" not in g, "the export was removed from the UI"


def test_kb_has_what_the_structured_editor_needs(client):
    lst = client.get("/api/kb").json()
    assert "all_topics" in lst, "the topic picker is populated from this"
    assert {"name", "bytes", "topics", "symptom_count", "structured"} <= set(lst["docs"][0])
    doc = client.get("/api/kb/seed").json()
    assert {"name", "topics", "symptoms", "body", "text", "all_topics"} <= set(doc)


def test_inventory_has_what_the_new_tab_needs(client):
    r = client.put("/api/inventory/system/tower1", json={
        "site": "s", "sensors": [{"name": "magos", "type": "magos_radar",
                                  "address": "192.168.40.60", "port": 8080,
                                  "ssh": {"user": "magos", "password": "pw"}}]}).json()
    assert {"path", "exists", "secrets_path", "systems", "defaults", "domains",
            "agent_view", "agent_prompt"} <= set(r)
    sy = r["systems"][0]
    assert {"name", "site", "description", "flat", "sensors"} <= set(sy)
    assert {"roles", "type_catalog"} <= set(r)
    assert "camera" in r["domains"]
    se = sy["components"][0]
    assert {"name", "type", "role", "domain", "address", "port", "scheme", "hardware",
            "software_version", "ssh"} <= set(se)
    assert sy["sensors"] == sy["components"], "legacy key mirrors the new one"
    assert {"user", "port", "key_file", "password_env", "password",
            "password_in_environment"} <= set(se["ssh"])


def test_the_exact_payload_the_page_sends_is_accepted(client):
    """Mirrors saveSys() in index.html, nulls included."""
    payload = {"site": "beit-yanai", "description": None, "sensors": [{
        "name": "cam", "type": "optic_ptz", "domain": "optic",
        "address": "192.168.40.71", "port": 80, "scheme": "http",
        "hardware": "Hikvision", "software_version": "5.7.3", "description": None,
        "ssh": {"user": "admin", "port": None, "key_file": None,
                "password_env": None, "password": "pw", "clear_password": False}}]}
    r = client.put("/api/inventory/system/cams", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["systems"][0]["sensors"][0]["ssh"]["password_env"] == "CAM_SSH_PW"


def test_the_page_no_longer_references_removed_surfaces():
    assert "mermaid" not in HTML.lower()
    assert "Addressable nodes" not in HTML
    assert "f-order" not in HTML, "the comma-separated flow editor is gone"
    assert "kbtext" not in HTML, "the free-text KB editor is gone"


def test_the_page_wires_the_new_surfaces():
    for hook in ("loadInv", "renderFlow", "laneDrop", "addSym", "togglePw", "supCard",
                 'data-t="inv"', "renderView", "viewSys", "addComp", "setRole",
                 "roleTag", "ins-before", "flowProblems", "togglePerm", "renderPerms",
                 "firstSentence", "toggleTopic", "addTopic", "renderTopics",
                 "closeKb", "toggleKb", "removeTopic", "renderRemovable"):
        assert hook in HTML, f"missing {hook}"


def test_inventory_is_the_last_tab():
    nav = HTML[HTML.index("<nav id=\"tabs\">"):HTML.index("</nav>")]
    order = re.findall(r'data-t="(\w+)"', nav)
    assert order[-1] == "inv", f"tab order is {order}"
    assert order[:2] == ["run", "approvals"]
    # The section list drives which panel is shown; it must agree with the nav.
    js = re.search(r"\['run',([^\]]+)\]\.forEach\(t=>\$\('#'\+t\)", HTML).group(0)
    assert js.index("'inv'") > js.index("'perms'")


def test_diagrams_never_scale_past_one_to_one():
    """A viewBox narrower than its container was stretched, so a small graph rendered
    zoomed in. Both diagrams cap their rendered width."""
    assert HTML.count("style=\"max-width:${W") == 2


def test_an_unsaveable_flow_is_refused_in_the_browser_too():
    """The server validates on save; the editor must say so before the click."""
    assert "function flowProblems()" in HTML
    assert "This flow cannot be saved" in HTML
    assert "${problems.length?'disabled':''}" in HTML


def test_the_editor_is_a_dense_grid_not_a_stack():
    """Short fields share a row: a 12-column grid with explicit spans."""
    assert "grid-template-columns:repeat(12" in HTML
    assert "grid-column:span" in HTML


def test_agents_and_tools_start_collapsed():
    """Only the role summary and the tool chips until you click Edit."""
    assert "var OPEN={agent:new Set(), post:new Set(), tool:new Set()};" in HTML
    assert "function entry(kind,name," in HTML
    assert "${open&&body?" in HTML, "the editor body renders only when expanded"


def test_the_system_model_panel_offers_no_save():
    """Reachable, never editable: it is generated, and a hand-edit is how it drifts."""
    kb = HTML[HTML.index("async function loadKb()"):HTML.index("async function showKb(")]
    assert "read-only" in kb
    assert "saveSystemModel" not in HTML and "system-model'," not in kb
    assert "no save button here on" in kb          # the sentence wraps in source


def test_a_case_editor_can_be_closed_without_leaving_the_page():
    assert "function closeKb()" in HTML
    assert 'onclick="closeKb()"' in HTML, "an explicit Close button"
    assert "function toggleKb(n){" in HTML, "clicking edit again closes it"


def test_topics_are_multi_select_not_a_text_field():
    assert "TOPICS_SEL" in HTML and "function toggleTopic(" in HTML
    assert 'list="domlist"' not in HTML, "the single-value datalist is gone"
    assert "async function addTopic(" in HTML, "and a new topic can be added inline"


def test_adding_a_topic_goes_to_the_server_not_just_the_tab():
    """A client-only list vanishes on refresh — which is exactly what it used to do."""
    assert "api('/topics'," in HTML and "method:'POST'" in HTML
    assert "TOPICS_ALL=r.all_topics" in HTML, "the list comes back from the server"


def test_the_system_model_is_last_and_folded_away():
    """Reference material, not the working surface: cases come first, this stays shut."""
    kb = HTML[HTML.index("async function loadKb()"):HTML.index("async function showKb(")]
    assert "<details class=\"card sysmodel\">" in kb, "it should fold, not fill the page"
    assert "<details class=\"card sysmodel\" open" not in kb, "it must start closed"
    assert kb.index("Recorded cases") < kb.index("sysmodel"), "cases come first"
    assert kb.index("Add a case by hand") < kb.index("sysmodel"), "it is last on the page"


def test_view_and_edit_are_distinct_modes():
    """Clicking a system must not drop straight into the editor."""
    assert "function viewSys(n){ VIEW=n; EDIT=null;" in HTML
    assert 'onclick="editSys(' in HTML, "an explicit Edit button toggles the dense mode"


# ---------- credentials: an API key is not the only way in ----------

@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
              "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
              "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_IDENTITY_TOKEN",
              "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_no_credentials_is_reported_as_not_ready(clean_env):
    import console.server as srv
    assert srv._credentials() == {"ready": False, "source": None, "warning": ""}


def test_an_api_key_is_recognised(clean_env, monkeypatch):
    import console.server as srv
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert srv._credentials()["source"] == "ANTHROPIC_API_KEY"


def test_an_oauth_profile_counts_as_credentials(clean_env):
    """The original bug: a working `ant auth login` profile read as 'no API key'."""
    import console.server as srv
    creds = clean_env / "credentials"
    creds.mkdir()
    (creds / "default.json").write_text("{}")
    got = srv._credentials()
    assert got["ready"] is True and "OAuth profile (default)" == got["source"]


def test_the_named_profile_is_the_one_checked(clean_env, monkeypatch):
    import console.server as srv
    creds = clean_env / "credentials"
    creds.mkdir()
    (creds / "default.json").write_text("{}")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "admin")
    assert srv._credentials()["ready"] is False, "ANTHROPIC_PROFILE names a missing profile"
    (creds / "admin.json").write_text("{}")
    assert srv._credentials()["source"] == "OAuth profile (admin)"


def test_workload_identity_federation_counts(clean_env, monkeypatch):
    import console.server as srv
    for v in ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
              "ANTHROPIC_SERVICE_ACCOUNT_ID"):
        monkeypatch.setenv(v, "x")
    assert srv._credentials()["ready"] is False, "incomplete WIF is not credentials"
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "/tmp/t")
    assert srv._credentials()["source"] == "workload identity federation"


def test_an_empty_api_key_is_called_out_rather_than_ignored(clean_env, monkeypatch):
    """An empty value still wins its precedence slot and authenticates with nothing."""
    import console.server as srv
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    got = srv._credentials()
    assert got["ready"] is False and "set but empty" in got["warning"]


def test_setting_both_a_key_and_a_token_is_warned_about(clean_env, monkeypatch):
    """The SDK sends both and the API rejects the request."""
    import console.server as srv
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    assert "rejects" in srv._credentials()["warning"]


def test_a_run_is_refused_only_when_nothing_resolves(client, clean_env, monkeypatch):
    r = client.post("/api/run", json={"question": "x"})
    assert r.status_code == 400 and "ant auth login" in r.text
    creds = clean_env / "credentials"
    creds.mkdir()
    (creds / "default.json").write_text("{}")
    assert client.get("/api/meta").json()["credentials"]["ready"] is True


def test_the_page_gates_on_credentials_not_on_a_key():
    assert "api_key_set" not in HTML, "the old key-only check is gone"
    assert "m.credentials" in HTML and "c.ready" in HTML
