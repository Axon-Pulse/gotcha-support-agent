"""Who gets in, what of the bot's config is shown, Systems, and Deploy."""
import importlib
import json
import sys
import time

import pytest

from console import ops
from console.settings import S
from console.tests.conftest import ROOT, stub, write_turns

SECRETS = {"SLACK_BOT_TOKEN": "xoxb-very-secret", "SLACK_APP_TOKEN": "xapp-very-secret",
           "ANTHROPIC_API_KEY": "sk-ant-very-secret"}


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------

def test_an_allowed_tailnet_user_gets_in(client, monkeypatch):
    monkeypatch.setattr(S, "allow_local", False)
    r = client.get("/api/meta", headers={"Tailscale-User-Login": "pm@example.com"})
    assert r.status_code == 200 and r.json()["who"] == "pm@example.com"


def test_another_tailnet_user_is_refused(client):
    r = client.get("/api/meta", headers={"Tailscale-User-Login": "someone@else.com"})
    assert r.status_code == 403 and "someone@else.com" in r.json()["detail"]


def test_a_local_request_needs_allow_local(client, monkeypatch):
    monkeypatch.setattr(S, "allow_local", False)
    assert client.get("/api/meta").status_code == 403
    assert client.get("/").status_code == 403
    monkeypatch.setattr(S, "allow_local", True)
    assert client.get("/api/meta").json()["who"] == "local"


def test_no_allowed_users_lets_no_tailnet_user_in(client, monkeypatch):
    monkeypatch.setattr(S, "allowed_users", set())
    assert client.get("/api/meta", headers={"Tailscale-User-Login": "pm@example.com"}).status_code == 403


@pytest.mark.parametrize("headers", [{"x-console": ""}, {"content-type": "text/plain"}])
def test_a_write_without_the_console_header_or_json_is_refused(client, headers):
    from fastapi.testclient import TestClient
    from console.server import app
    c = TestClient(app)
    h = {"x-console": "1", "content-type": "application/json", **headers}
    r = c.post("/api/cases", content=json.dumps({"name": "x"}), headers=h)
    assert r.status_code == 403
    assert not (S.cases_dir / "x.md").exists()


# --------------------------------------------------------------------------
# Bot config
# --------------------------------------------------------------------------

def test_tokens_and_keys_never_leave_the_console(client):
    (S.bridge_dir / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in SECRETS.items()) + "SLACK_ALLOWED_CHANNELS=C0ABC\nBRIDGE_EFFORT=high\n")
    for path in ["/api/bot", "/api/deploy", "/api/systems", "/api/meta", "/api/conversations", "/api/stats"]:
        body = client.get(path).text
        for v in SECRETS.values():
            assert v not in body, f"{path} leaks a secret"
    b = client.get("/api/bot").json()
    assert sorted(b["secrets_set"]) == sorted(SECRETS)
    vals = {s["name"]: s["value"] for s in b["settings"]}
    assert vals["SLACK_ALLOWED_CHANNELS"] == "C0ABC" and vals["BRIDGE_EFFORT"] == "high"
    assert b["permissions"]["permissions"]["defaultMode"] == "dontAsk"
    assert "guard" in b and b["container"]["found"] is False


def test_the_defaults_shown_are_the_bridges_defaults(monkeypatch):
    for k in ops.DEFAULTS:
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, str(ROOT / "slack-bridge"))
    try:
        bridge = importlib.import_module("bridge")
        importlib.reload(bridge)
    finally:
        sys.path.remove(str(ROOT / "slack-bridge"))
    assert ops.DEFAULTS["BRIDGE_MODEL"] == bridge.MODEL
    assert ops.DEFAULTS["BRIDGE_EFFORT"] == bridge.EFFORT
    assert ops.DEFAULTS["BRIDGE_TIMEOUT_S"] == str(bridge.TIMEOUT_S)
    assert ops.DEFAULTS["BRIDGE_MAX_CONCURRENT"] == str(bridge.MAX_CONCURRENT)
    assert ops.DEFAULTS["BRIDGE_ALLOWED_HOSTS"] == bridge.ALLOWED_HOSTS


# --------------------------------------------------------------------------
# Systems
# --------------------------------------------------------------------------

def test_systems_lists_the_sites_with_their_last_known_version(client, env):
    stub(S.skill_dir / "scripts" / "list_systems.sh", """
[ "$GOTCHA_SYSTEMS_REGEX" = "axon-gotcha-[0-9]+" ] || { echo "regex not passed: $GOTCHA_SYSTEMS_REGEX" >&2; exit 1; }
cat <<'EOF'
tailnet name                 hostname                 ip               state    via      last seen
axon-gotcha-4                axon-gotcha-4            100.0.0.4        online   tailscale
axon-gotcha-3                gotcha3-box              100.0.0.3        offline  ssh      2026-10-07T08:19
EOF
""")
    S.systems_tsv.write_text("axon-gotcha-4\t2026-10-08T12:53:22Z\tv1.4.0-67\tlatest\tconfigs/x.yaml\t/h\n"
                             "axon-gotcha-9\t2026-09-01T00:00:00Z\tv1.2.0\tlatest\t\t\n")
    s = client.get("/api/systems").json()
    assert s["error"] is None
    by = {r["name"]: r for r in s["systems"]}
    assert by["axon-gotcha-4"]["last"]["release"] == "v1.4.0-67" and by["axon-gotcha-4"]["via"] == "tailscale"
    assert by["axon-gotcha-3"]["state"] == "offline" and by["axon-gotcha-3"]["last"] is None
    assert [u["name"] for u in s["unlisted"]] == ["axon-gotcha-9"]


def test_a_failing_list_systems_is_reported(client, env):
    stub(S.skill_dir / "scripts" / "list_systems.sh", "echo 'tailscale is not installed' >&2; exit 3\n")
    s = client.get("/api/systems").json()
    assert s["systems"] == [] and "not installed" in s["error"]


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------

@pytest.fixture
def fake_make(env, monkeypatch):
    script = stub(env / "make", "echo building; echo done\n")
    monkeypatch.setattr(S, "deploy_cmd", [str(script)])
    ops.DEPLOY.__init__()
    yield script
    ops.DEPLOY.__init__()


def _wait():
    for _ in range(100):
        if not ops.DEPLOY.running:
            return
        time.sleep(0.05)


def test_deploy_runs_make_update_and_keeps_its_log(client, fake_make):
    r = client.post("/api/deploy", json={})
    assert r.status_code == 200, r.text
    _wait()
    d = client.get("/api/deploy").json()["deploy"]
    assert d["exit_code"] == 0 and "building" in d["log"] and d["by"] == "local"
    log = (S.state_dir / "audit.jsonl").read_text()
    assert '"deploy"' in log


def test_deploy_is_refused_while_selftest_fails(client, fake_make):
    (S.cases_dir / "orphan.md").write_text("---\ntopics: []\nsymptoms: []\n---\n\n# Orphan\n")
    r = client.post("/api/deploy", json={})
    assert r.status_code == 409 and "selftest" in r.json()["detail"]
    assert not ops.DEPLOY.started


def test_deploy_is_refused_while_a_question_is_being_answered(client, fake_make):
    write_turns([{"event": "start", "ts": time.time(), "key": "C1:1.1", "channel": "C1", "thread_ts": "1.1"}])
    r = client.post("/api/deploy", json={})
    assert r.status_code == 409 and "being answered" in r.json()["detail"]


def test_one_deploy_at_a_time(client, env, monkeypatch):
    script = stub(env / "make", "sleep 2\n")
    monkeypatch.setattr(S, "deploy_cmd", [str(script)])
    ops.DEPLOY.__init__()
    assert client.post("/api/deploy", json={}).status_code == 200
    r = client.post("/api/deploy", json={})
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    _wait()
    ops.DEPLOY.__init__()


def test_deploy_status_compares_the_image_label_with_the_files(client, monkeypatch):
    monkeypatch.setattr(ops, "container", lambda: {"found": True, "skill_hash": "abc",
                                                   "image_created": "2000-01-01T00:00:00.123456789Z"})
    monkeypatch.setattr(ops, "disk_hash", lambda: "abc")
    d = client.get("/api/deploy").json()
    assert d["state"] == "deployed"
    monkeypatch.setattr(ops, "disk_hash", lambda: "def")
    d = client.get("/api/deploy").json()
    assert d["state"] == "changed"
    assert any(f.startswith("skill/SKILL.md") or f.endswith("SKILL.md") for f in d["changed"]), \
        "every file is newer than an image built in 2000"
    monkeypatch.setattr(ops, "container", lambda: {"found": False, "error": "no such container"})
    assert client.get("/api/deploy").json()["state"] == "no-container"


def test_the_hash_script_ignores_the_directory_it_is_run_from():
    import subprocess
    script = ROOT / "slack-bridge" / "skill_hash.sh"
    a = subprocess.run([str(script)], capture_output=True, text=True, cwd="/").stdout
    b = subprocess.run([str(script)], capture_output=True, text=True, cwd=ROOT).stdout
    assert a == b and len(a.strip()) == 16
