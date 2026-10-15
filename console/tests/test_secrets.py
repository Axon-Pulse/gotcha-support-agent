"""The bot's secrets in the console: names for everyone, values for admins behind a code,
every change followed by a restart of the bot so it reads .env again."""
import json
import stat
import time

import pytest

from console import auth, conversations, ops
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}
CANARY = "sk-ant-CANARY-0123456789abcdef"
ENV = f"""# The bot's settings
BRIDGE_MODEL=claude-sonnet-5-5
SLACK_BOT_TOKEN=xoxb-OLD-TOKEN

# Anthropic
ANTHROPIC_API_KEY={CANARY}
"""


@pytest.fixture
def secrets(env, clock, monkeypatch):
    p = S.bridge_dir / ".env"
    p.write_text(ENV)
    p.chmod(0o600)
    auth.enroll("admin@example.com")
    monkeypatch.setattr(ops, "DEPLOY", ops.Deploy())
    monkeypatch.setattr(S, "restart_cmd", ["true"])
    monkeypatch.setattr(ops, "RESTART_POLL_S", 0.01)
    return p


def _stepup(client, clock):
    r = client.post("/api/auth/stepup", json={"code": clock.code("admin@example.com")},
                    headers=ADMIN)
    assert r.status_code == 200


def _save(client, clock, name, value, code=None):
    return client.put(f"/api/secrets/{name}", headers=ADMIN, json={
        "value": value, "code": clock.code("admin@example.com") if code is None else code})


def _wait_job():
    for _ in range(500):
        if not ops.DEPLOY.running:
            return ops.DEPLOY.snapshot()
        time.sleep(0.01)
    raise AssertionError("the restart never finished")


def _audit():
    return [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]


def test_everyone_sees_the_names_and_nobody_sees_a_value_in_the_list(client, secrets):
    for headers in ({}, ADMIN):
        r = client.get("/api/secrets", headers=headers)
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["secrets"]]
        assert names == ["ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN"]  # BRIDGE_MODEL is shown elsewhere
        assert CANARY not in r.text and "xoxb-OLD" not in r.text
    assert client.get("/api/secrets").json()["can_edit"] is False
    assert client.get("/api/secrets", headers=ADMIN).json()["can_edit"] is True


def test_a_viewer_can_neither_reveal_nor_change(client, secrets, clock):
    assert client.post("/api/secrets/ANTHROPIC_API_KEY/reveal", json={}).status_code == 403
    assert client.put("/api/secrets/ANTHROPIC_API_KEY",
                      json={"value": "x", "code": "123456"}).status_code == 403
    assert secrets.read_text() == ENV
    assert [e["action"] for e in _audit()] == ["refused", "refused"]


def test_an_admin_reveals_only_after_a_fresh_code(client, secrets, clock):
    r = client.post("/api/secrets/ANTHROPIC_API_KEY/reveal", json={}, headers=ADMIN)
    assert r.status_code == 403 and r.json()["detail"] == "stepup"
    _stepup(client, clock)
    r = client.post("/api/secrets/ANTHROPIC_API_KEY/reveal", json={}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["value"] == CANARY
    assert r.headers["cache-control"] == "no-store"
    assert client.post("/api/secrets/NOPE/reveal", json={}, headers=ADMIN).status_code == 404
    assert client.post("/api/secrets/BRIDGE_MODEL/reveal", json={}, headers=ADMIN).status_code == 404
    clock.t += S.stepup_seconds + 1
    r = client.post("/api/secrets/ANTHROPIC_API_KEY/reveal", json={}, headers=ADMIN)
    assert r.status_code == 403


def test_a_save_needs_a_good_code_on_that_request(client, secrets, clock):
    _stepup(client, clock)  # a step-up alone is not enough to save
    for code in ("", "000000"):
        r = _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-NEW", code=code)
        assert r.status_code == 401
    assert secrets.read_text() == ENV


def test_a_save_changes_only_that_line_keeps_the_file_private_and_restarts_the_bot(client, secrets, clock):
    r = _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-NEW-TOKEN")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "saved" and body["restart_triggered"] is True
    assert body["secret"]["updated_by"] == "admin@example.com"
    assert "xoxb-NEW" not in r.text
    assert secrets.read_text() == ENV.replace("xoxb-OLD-TOKEN", "xoxb-NEW-TOKEN")
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    job = _wait_job()
    assert job["kind"] == "restart" and job["exit_code"] == 0 and job["by"] == "admin@example.com"
    assert [e["action"] for e in _audit()][-2:] == ["set-secret", "restart-bot"]


def test_a_new_secret_is_added_and_odd_characters_are_quoted(client, secrets, clock):
    assert _save(client, clock, "LINEAR_API_KEY", "abc$HOME #x").status_code == 200
    assert secrets.read_text().endswith("LINEAR_API_KEY='abc$HOME #x'\n")
    _stepup(client, clock)
    r = client.post("/api/secrets/LINEAR_API_KEY/reveal", json={}, headers=ADMIN)
    assert r.json()["value"] == "abc$HOME #x"


@pytest.mark.parametrize("name,value", [
    ("BRIDGE_MODEL", "x"),          # a shown setting, not a secret
    ("lower_case", "x"),
    ("SLACK_BOT_TOKEN", "a\nEVIL=1"),
    ("SLACK_BOT_TOKEN", ""),
    ("SLACK_BOT_TOKEN", "it's $odd"),
    ("SLACK_BOT_TOKEN", "a\u2028BRIDGE_ALLOWED_HOSTS=.*"),   # str.splitlines() splits here
    ("SLACK_BOT_TOKEN", "a\x85B=c"),
    ("SLACK_BOT_TOKEN", "a\x0bB=c"),
    ("CLAUDE_BIN_X", "x"),          # a new name that isn't a secret's
    ("LD_PRELOAD", "/tmp/x.so"),
    ("HTTPS_PROXY", "http://evil"),
])
def test_names_and_values_that_cant_be_written_safely_are_refused(client, secrets, clock, name, value):
    assert _save(client, clock, name, value).status_code == 400
    assert secrets.read_text() == ENV


def test_no_response_or_audit_line_carries_a_value(client, secrets, clock):
    _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-SECOND-CANARY")
    _wait_job()
    seen = [client.get(p, headers=ADMIN).text for p in
            ("/api/secrets", "/api/bot", "/api/audit", "/api/deploy", "/api/meta")]
    for text in seen + [(S.state_dir / "audit.jsonl").read_text(),
                        S.secrets_meta_file.read_text()]:
        assert CANARY not in text and "xoxb-SECOND-CANARY" not in text


def test_a_restart_waits_for_questions_being_answered(client, secrets, clock, monkeypatch):
    busy = [[{"outcome": "running"}]] * 3
    monkeypatch.setattr(conversations, "in_flight", lambda: busy.pop() if busy else [])
    assert _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-NEW").json()["restart_triggered"]
    job = _wait_job()
    assert "waiting for 1 question" in job["log"] and job["exit_code"] == 0


def test_a_restart_asked_for_during_another_job_runs_after_it(client, secrets, clock):
    ops.DEPLOY._begin("deploy", "pm@example.com")  # a deploy is running
    r = _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-NEW").json()
    assert r["restart_triggered"] and "queued after the deploy" in r["restart_note"]
    assert ops.DEPLOY.snapshot()["restart_queued"]
    ops.DEPLOY.running = False  # the deploy ends; its runner would start the queued restart
    ops.DEPLOY._run(["true"], False)
    job = _wait_job()
    assert job["kind"] == "restart" and job["by"] == "admin@example.com"


def test_the_save_stands_when_the_restart_cant_start(client, secrets, clock, monkeypatch):
    def boom(by):
        raise RuntimeError("docker is not here")
    monkeypatch.setattr(ops.DEPLOY, "restart", boom)
    body = _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-NEW").json()
    assert body["status"] == "saved" and body["restart_triggered"] is False
    assert "docker" in body["restart_error"]
    assert "xoxb-NEW" in secrets.read_text()


def test_restart_now_is_for_admins(client, secrets):
    assert client.post("/api/bot/restart", json={}).status_code == 403
    r = client.post("/api/bot/restart", json={}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["restart_triggered"]
    _wait_job()


def test_a_new_secret_must_be_named_like_one_but_existing_ones_stay_editable(client, secrets, clock):
    S.bridge_dir.joinpath(".env").write_text(ENV + "OLD_STYLE_NAME=x\n")
    assert _save(client, clock, "OLD_STYLE_NAME", "y").status_code == 200
    assert _save(client, clock, "LINEAR_API_KEY", "k").status_code == 200
    assert _save(client, clock, "ANTHROPIC_BASE_URL", "http://evil").status_code == 400


def test_a_second_save_during_a_waiting_restart_doesnt_restart_twice(client, secrets, clock, monkeypatch):
    import threading
    from console import conversations
    done = threading.Event()  # questions stay in flight until the test says so
    monkeypatch.setattr(conversations, "in_flight", lambda: [] if done.is_set() else [{"outcome": "running"}])
    _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-1")
    r = _save(client, clock, "SLACK_BOT_TOKEN", "xoxb-2").json()
    assert r["restart_triggered"] and "already waiting" in r["restart_note"]
    assert not ops.DEPLOY.snapshot()["restart_queued"]
    done.set()
    _wait_job()


def test_a_typo_in_a_bot_secret_doesnt_use_up_the_code(client, secrets, clock):
    code = clock.code("admin@example.com")
    for name, value in [("SLACK_BOT_TOKEN", "a\nb"), ("BRIDGE_MODEL", "x"), ("LD_PRELOAD", "x")]:
        r = client.put(f"/api/secrets/{name}", headers=ADMIN, json={"value": value, "code": code})
        assert r.status_code == 400
    r = client.put("/api/secrets/SLACK_BOT_TOKEN", headers=ADMIN, json={"value": "xoxb-ok", "code": code})
    assert r.status_code == 200
    _wait_job()
