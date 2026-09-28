"""One central agent, many systems: each session is about one system, and every check
for it runs on that system's own machine over SSH."""
import json
import shlex

import pytest

import inventory
import registry
import transport

READ_CONFIG = transport.read_config

INV = """\
version: 1
systems:
  gotcha3:
    site: lebanon
    host: apu3
    config: ~/gotcha30/configs/site3.yaml
    components:
      apu3: {type: jetson, role: compute, address: 10.3.0.10,
             access: {ssh: {user: diag, key_file: ~/.ssh/diag}}}
      radar3: {type: radar, role: sensor, domain: radar, address: 10.3.0.51,
               access: {ssh: {user: admin, key_file: ~/.ssh/diag}}}
      cam3: {type: camera, role: sensor, domain: camera, address: 10.3.0.60}
  gotcha5:
    host: apu5
    components:
      apu5: {type: jetson, role: compute, address: 10.5.0.10,
             access: {ssh: {user: diag, key_file: ~/.ssh/diag}}}
"""


def write_inv(text: str = INV) -> None:
    inventory.SYSTEMS.write_text(text)
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()
    inventory._config_cache.clear()


@pytest.fixture(autouse=True)
def _inv(monkeypatch):
    write_inv()
    monkeypatch.setattr(transport, "_CM_DIR", inventory.SYSTEMS.parent / "cm")
    yield
    inventory._config_cache.clear()


@pytest.fixture
def live(monkeypatch):
    """Live mode with subprocess recorded, not run."""
    calls = []

    class R:
        returncode, stdout, stderr = 0, "OUT", ""

    def fake(argv, **kw):
        calls.append({"argv": argv, "env": kw.get("env") or {}})
        return R()
    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport.subprocess, "run", fake)
    # The scoped view reads the system's config first; a test about one command should
    # see that command, so the config is served without a subprocess.
    monkeypatch.setattr(transport, "read_config", lambda system: "nodes: {}\n")
    return calls


def remote_argv(call):
    """What the host will actually execute: the one string after `--`, parsed back."""
    argv = call["argv"]
    return shlex.split(argv[argv.index("--") + 1])


# ---------- the inventory ----------

def test_systems_carry_host_and_config():
    s = inventory.systems()
    assert s["gotcha3"]["host"] == "apu3" and s["gotcha3"]["config"].startswith("~/")
    assert s["gotcha5"]["config"] is None


@pytest.mark.parametrize("bad,msg", [
    (INV.replace("host: apu3", "host: apu5"), "not one of its components"),
    (INV.replace("~/gotcha30/configs/site3.yaml", "'x; rm -rf ~'"), "plain path"),
    (INV.replace("~/gotcha30/configs/site3.yaml", "../../etc/passwd"), "plain path"),
])
def test_a_bad_host_or_config_does_not_load(bad, msg):
    write_inv(bad)
    with pytest.raises(inventory.InventoryError, match=msg):
        inventory.load()


# ---------- which system ----------

@pytest.mark.parametrize("text,explicit,remembered,want", [
    ("gotcha3 has no tracks", None, None, "gotcha3"),
    ("no tracks", "gotcha5", None, "gotcha5"),
    ("no tracks", None, "gotcha3", "gotcha3"),
    ("is gotcha30 ok?", None, "gotcha5", "gotcha5"),          # whole words only
])
def test_the_system_is_named_chosen_or_remembered(text, explicit, remembered, want):
    assert inventory.pick_system(text, explicit, remembered) == (want, "")


def test_it_asks_rather_than_guesses():
    chosen, why = inventory.pick_system("no tracks")
    assert chosen is None and "Which system" in why and "gotcha3" in why
    chosen, why = inventory.pick_system("gotcha3 and gotcha5 are down")
    assert chosen is None and "one system at a time" in why
    assert inventory.pick_system("x", "nope")[0] is None


def test_one_machine_and_no_hosts_needs_no_choice():
    write_inv("version: 1\nsystems:\n  a: {components: {}}\n  b: {components: {}}\n")
    assert inventory.pick_system("no tracks") == (None, "")


# ---------- scope ----------

def test_while_a_system_is_active_only_it_is_addressable(monkeypatch):
    monkeypatch.setattr(transport, "MODE", "mock")        # config nodes from the fixture
    with inventory.using("gotcha5"):
        assert "apu5" in inventory.names() and "radar3" not in inventory.names()
        with pytest.raises(KeyError, match="in system 'gotcha5'"):
            inventory.require("radar3")
        assert "System under diagnosis: gotcha5" in inventory.as_prompt()
    assert "radar3" in inventory.names()                   # unscoped again afterwards


def test_the_tools_node_enum_follows_the_active_system(monkeypatch):
    registry.load_tools()
    monkeypatch.setattr(transport, "MODE", "mock")
    enum = lambda: next(s for s in registry.schemas(["probe_endpoint"]))[  # noqa: E731
        "input_schema"]["properties"]["node"]["enum"]
    with inventory.using("gotcha3"):
        assert "radar3" in enum() and "apu5" not in enum()
    with inventory.using("gotcha5"):
        assert "apu5" in enum() and "radar3" not in enum()


def test_the_systems_own_config_is_read_from_its_host(live, monkeypatch):
    monkeypatch.setattr(transport, "read_config",
                        lambda s: "nodes:\n  sensors:\n    magos: {type: magos_radar, config: {ip: 10.3.0.52}}\n")
    with inventory.using("gotcha3"):
        assert "magos" in inventory.names()
        assert inventory.require("magos")["endpoint"]["host"] == "10.3.0.52"
    with inventory.using("gotcha5"):
        assert "magos" not in inventory.names(), "another system's config leaked in"


def test_an_unreadable_config_is_said_not_hidden(live, monkeypatch):
    def boom(s):
        raise OSError("No such file")
    monkeypatch.setattr(transport, "read_config", boom)
    with inventory.using("gotcha3"):
        assert "could not read ~/gotcha30/configs/site3.yaml on apu3" in inventory.as_prompt()


# ---------- where commands run ----------

def test_with_no_system_chosen_it_refuses_to_diagnose_this_server(live):
    with pytest.raises(RuntimeError, match="refusing to diagnose this server"):
        transport.run("health")
    assert live == []


def test_every_local_check_runs_on_the_systems_host(live):
    with inventory.using("gotcha3"):
        transport.run("asu_container")
    call = live[0]
    assert "diag@10.3.0.10" in call["argv"] and "-i" in call["argv"]
    assert "ControlMaster=auto" in " ".join(call["argv"])
    # docker's --format holds tabs: the remote shell must get back exactly this argv.
    assert remote_argv(call)[2:] == list(transport.DEFAULT_ALLOWED["asu_container"])


def test_ping_and_route_leave_from_the_site(live):
    with inventory.using("gotcha5"):
        transport.run_argv(["ping", "-c", "1", "10.5.0.51"], timeout=5)
    assert "diag@10.5.0.10" in live[0]["argv"]
    assert remote_argv(live[0]) == ["timeout", "5", "ping", "-c", "1", "10.5.0.51"]


def test_a_unit_is_reached_through_the_host(live):
    with inventory.using("gotcha3"):
        transport.run_on("radar_status", "radar3")
    argv = live[0]["argv"]
    assert "admin@10.3.0.51" in argv
    proxy = next(a for a in argv if a.startswith("ProxyCommand="))
    assert "diag@10.3.0.10" in proxy and "-W %h:%p" in proxy


def test_an_http_probe_of_a_unit_runs_on_the_host(live):
    with inventory.using("gotcha3"):
        transport.run_on("camera_status", "cam3")
    assert "diag@10.3.0.10" in live[0]["argv"]
    assert "http://10.3.0.60/" in remote_argv(live[0])


def test_jumping_needs_a_key_on_the_host(live, monkeypatch):
    write_inv(INV.replace("apu3: {type: jetson, role: compute, address: 10.3.0.10,\n"
                          "             access: {ssh: {user: diag, key_file: ~/.ssh/diag}}}",
                          "apu3: {type: jetson, role: compute, address: 10.3.0.10,\n"
                          "             access: {ssh: {user: diag, password_env: APU3_PW}}}"))
    monkeypatch.setenv("APU3_PW", "pw")
    with inventory.using("gotcha3"), pytest.raises(RuntimeError, match="key-based SSH"):
        transport.run_on("radar_status", "radar3")


def test_a_password_never_reaches_the_command_line(live, monkeypatch):
    write_inv(INV.replace("key_file: ~/.ssh/diag}}}\n      radar3",
                          "password_env: APU3_PW}}}\n      radar3"))
    monkeypatch.setenv("APU3_PW", "hunter2")
    with inventory.using("gotcha3"):
        transport.run("health")
    assert "hunter2" not in " ".join(live[0]["argv"]) and live[0]["env"]["SSHPASS"] == "hunter2"
    assert live[0]["argv"][2:4] == ["sshpass", "-e"]      # after `timeout N`


def test_the_config_path_is_quoted_and_only_tilde_expands(live):
    READ_CONFIG("gotcha3")
    remote = live[0]["argv"][-1]
    assert remote.endswith('-- "$HOME"/gotcha30/configs/site3.yaml')


def test_mock_mode_never_leaves_the_machine(monkeypatch):
    monkeypatch.setattr(transport, "MODE", "mock")
    with inventory.using("gotcha3"):
        assert transport.run("health")          # the fixture, as before


# ---------- entry points ----------

def test_the_trace_page_uses_the_recorded_system(tmp_path):
    import trace_tag
    d = tmp_path / "t"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps({"status": "done", "system": "gotcha5",
                                           "question": "gotcha3 looked fine"}) + "\n")
    row = trace_tag.describe(d)[0]
    assert row["system"] == "gotcha5", "inferred from the text instead of what ran"


def test_slack_asks_which_system_and_then_remembers_it(monkeypatch, tmp_path):
    import slack_app as S
    posts = []
    monkeypatch.setattr(S, "post", lambda ch, text, thread=None: posts.append(text))
    monkeypatch.setattr(S, "TRACES", tmp_path / "traces")
    monkeypatch.setattr(S, "DB", str(tmp_path / "graph.db"))
    monkeypatch.setattr(S, "_client", None)
    ran = []

    class G:
        def compile(self, checkpointer=None):
            return self

        def invoke(self, payload, cfg):
            ran.append(payload["system"])
            return {"report": {"root_cause": "x"}, "findings": [], "transcript": []}
    monkeypatch.setattr(S, "graph", lambda: G())

    S._diagnose("s1", "no tracks", "C_OPS", "100.0", "U1", [], "100.0")
    assert ran == [] and any("Which system" in p for p in posts)
    S._diagnose("s2", "gotcha3 no tracks", "C_OPS", "200.0", "U1", [], "200.0")
    assert ran == ["gotcha3"]
    monkeypatch.setattr(S.convo, "route", lambda t, m: {"kind": "run", "why": "new"})
    S._diagnose("s3", "and now the camera?", "C_OPS", "200.0", "U1", [], "201.0")
    assert ran == ["gotcha3", "gotcha3"], "the thread forgot its system"


def test_the_console_asks_and_a_conversation_keeps_its_system(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import console.server as srv
    monkeypatch.setattr(srv, "_credentials", lambda: {"ready": True})
    monkeypatch.setattr(srv, "TRACES", tmp_path / "traces")
    c = TestClient(srv.app)
    r = c.post("/api/run", json={"question": "no tracks"})
    assert r.status_code == 400 and "Which system" in r.json()["detail"]

    got = []
    monkeypatch.setattr(srv, "_converse", lambda cid, m, recs, system=None: got.append(system))
    cid = c.post("/api/conversation").json()["conversation_id"]
    assert c.post(f"/api/conversation/{cid}/message",
                  json={"message": "no tracks", "system": "gotcha5"}).status_code == 200
    srv._cset(cid, status="idle", turns=[{"kind": "run", "system": "gotcha5"}])
    assert c.post(f"/api/conversation/{cid}/message",
                  json={"message": "and the radar?"}).status_code == 200
    assert got == ["gotcha5", "gotcha5"]
    assert "gotcha3" in c.get("/api/meta").json()["systems"]


def test_the_inventory_editor_keeps_host_and_config(monkeypatch):
    from fastapi.testclient import TestClient
    import console.server as srv
    c = TestClient(srv.app)
    sysrow = next(s for s in c.get("/api/inventory").json()["systems"] if s["name"] == "gotcha5")
    assert sysrow["host"] == "apu5"
    body = {"components": [{"name": "apu5", "type": "jetson", "role": "compute",
                            "address": "10.5.0.10", "ssh": {"user": "diag"}}]}
    assert c.put("/api/inventory/system/gotcha5", json=body).status_code == 200
    assert inventory.systems()["gotcha5"]["host"] == "apu5", "a save without it dropped it"
    r = c.put("/api/inventory/system/gotcha5",
              json={**body, "host": "nope", "config": "~/c.yaml"})
    assert r.status_code == 400 and "not one of its components" in r.json()["detail"]
    assert inventory.systems()["gotcha5"]["host"] == "apu5", "a refused save still wrote"


# ---------- the graph and the preflight ----------

def test_each_agent_step_runs_with_its_sessions_system_active(monkeypatch):
    import graph
    seen = {}

    def fake_run_agent(name, cfg, question, context=""):
        seen["active"] = inventory.active()
        return "text", [], {"input": 0, "output": 0, "cache_read": 0}
    monkeypatch.setattr(graph.llm, "run_agent", fake_run_agent)
    monkeypatch.setattr(graph.A, "agents", lambda: {"triage": {"prompt": "", "tools": []}})
    graph.make_agent_node("triage")({"question": "q", "system": "gotcha5", "findings": []})
    assert seen["active"] == "gotcha5" and inventory.active() is None


def test_preflight_reports_each_check_and_fails_on_a_missing_tool(monkeypatch, capsys):
    import preflight

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_sh(host, remote, timeout=20):
        return R(out="docker\n") if remote.startswith("for t in") else R()
    monkeypatch.setattr(preflight, "_sh", fake_sh)
    monkeypatch.setattr(preflight, "_known_host", lambda ip, port: False)
    monkeypatch.setattr(transport, "read_config", lambda s: "nodes: {}\n")
    import tools.health as H
    monkeypatch.setattr(H, "get_system_health", lambda: {"nodes": [{"node": "magos"}]})
    assert preflight.main("gotcha3") == 1
    out = capsys.readouterr().out
    assert "ok    ssh login" in out and "FAIL  tools on host — missing: docker" in out
    assert "warn  host key" in out and "not in known_hosts" in out
    assert "not in inventory or config: magos" in out
