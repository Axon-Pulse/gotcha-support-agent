"""The bot's settings from the Bot config tab: an admin changes the editable ones with a code,
each value checked first, only its line of .env rewritten, then the bot restarted."""
import json
import stat
import time

import pytest

from console import auth, ops
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}
CANARY = "sk-ant-CANARY-0123456789abcdef"
ENV = f"""# The bot's settings
BRIDGE_MODEL=claude-sonnet-5-5
SLACK_ALLOWED_CHANNELS=C0123ABCD
BOT_UID=1000

# Anthropic
ANTHROPIC_API_KEY={CANARY}
"""


@pytest.fixture
def dotenv(env, clock, monkeypatch):
    p = S.bridge_dir / ".env"
    p.write_text(ENV)
    p.chmod(0o600)
    auth.enroll("admin@example.com")
    monkeypatch.setattr(ops, "DEPLOY", ops.Deploy())
    monkeypatch.setattr(S, "restart_cmd", ["true"])
    monkeypatch.setattr(ops, "RESTART_POLL_S", 0.01)
    return p


def _save(client, clock, code=None, **changes):
    return client.put("/api/bot/settings", headers=ADMIN, json={
        "changes": changes, "code": clock.code("admin@example.com") if code is None else code})


def _wait_job():
    for _ in range(500):
        if not ops.DEPLOY.running:
            return
        time.sleep(0.01)
    raise AssertionError("the restart never finished")


def _audit():
    return [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]


def test_the_listing_says_what_can_be_changed(client, dotenv):
    b = client.get("/api/bot", headers=ADMIN).json()
    rows = {s["name"]: s for s in b["settings"]}
    assert b["can_edit"] is True and client.get("/api/bot").json()["can_edit"] is False
    assert rows["BRIDGE_EFFORT"]["editable"] and rows["BRIDGE_ALLOWED_HOSTS"]["editable"]
    for name in ("BOT_UID", "BOT_GID", "CLAUDE_BIN"):
        assert not rows[name]["editable"] and rows[name]["read_only"]
    assert "claude-sonnet-5-5" in b["models"] and "xhigh" in b["efforts"]
    assert CANARY not in json.dumps(b)


def test_an_admin_changes_settings_and_only_their_lines_move(client, clock, dotenv):
    r = _save(client, clock, BRIDGE_EFFORT="high", BRIDGE_TIMEOUT_S="240",
              BRIDGE_ALLOWED_HOSTS="axon-gotcha-[0-9]+, lab-pc-1", BRIDGE_MODEL=None,
              SLACK_ALLOWED_USERS="U0AAAAAAA U0BBBBBBB")
    assert r.status_code == 200, r.text
    assert r.json()["restart_triggered"] is True
    assert set(r.json()["changed"]) == {"BRIDGE_EFFORT", "BRIDGE_TIMEOUT_S", "BRIDGE_ALLOWED_HOSTS",
                                        "BRIDGE_MODEL", "SLACK_ALLOWED_USERS"}
    _wait_job()
    assert dotenv.read_text() == f"""# The bot's settings
SLACK_ALLOWED_CHANNELS=C0123ABCD
BOT_UID=1000

# Anthropic
ANTHROPIC_API_KEY={CANARY}
BRIDGE_EFFORT=high
BRIDGE_TIMEOUT_S=240
BRIDGE_ALLOWED_HOSTS='axon-gotcha-[0-9]+|lab-pc-1'
SLACK_ALLOWED_USERS=U0AAAAAAA,U0BBBBBBB
"""
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    # Read back as the bot reads it: the quotes are the file's, not the value's.
    assert ops.bot_env()["BRIDGE_ALLOWED_HOSTS"] == "axon-gotcha-[0-9]+|lab-pc-1"
    rows = {s["name"]: s for s in client.get("/api/bot").json()["settings"]}
    assert rows["BRIDGE_EFFORT"]["updated_by"] == "admin@example.com"
    log = [e for e in _audit() if e["action"] == "set-setting"]
    assert {(e["target"], e["to"]) for e in log} >= {("BRIDGE_EFFORT", "high"), ("BRIDGE_MODEL", "(default)")}
    assert any(e["action"] == "restart-bot" for e in _audit())


def test_the_same_value_again_changes_nothing_and_restarts_nothing(client, clock, dotenv):
    r = _save(client, clock, BRIDGE_MODEL="claude-sonnet-5-5")
    assert r.json() == {"status": "unchanged", "changed": []}
    assert dotenv.read_text() == ENV


@pytest.mark.parametrize("changes", [
    {"BRIDGE_ALLOWED_HOSTS": ".*"},                       # anything on the tailnet
    {"BRIDGE_ALLOWED_HOSTS": "axon-.*"},
    {"BRIDGE_ALLOWED_HOSTS": "lab.pc"},
    {"BRIDGE_ALLOWED_HOSTS": ""},
    {"GOTCHA_SSH_USER": "-oProxyCommand=touch /tmp/x"},   # read by ssh as an option
    {"GOTCHA_SSH_USER": "root x"},
    {"BRIDGE_EFFORT": "extreme"},
    {"BRIDGE_TIMEOUT_S": "5"}, {"BRIDGE_TIMEOUT_S": "601"}, {"BRIDGE_TIMEOUT_S": "1e3"},
    {"BRIDGE_MAX_CONCURRENT": "0"}, {"BRIDGE_MAX_CONCURRENT": "9"},
    {"BRIDGE_MODEL": "gpt-4"}, {"BRIDGE_MODEL": "claude-x; rm -rf /"},
    {"BRIDGE_RTL_MARKS": "yes"},
    {"SLACK_ALLOWED_CHANNELS": "general"}, {"SLACK_ALLOWED_CHANNELS": "GENERAL"},
    {"SLACK_ALLOWED_CHANNELS": "c0123abcd"}, {"SLACK_ALLOWED_USERS": "U1"},
    {"SLACK_ALLOWED_USERS": ",".join(f"U0AAAAA{i:03}" for i in range(51))},   # too many
    {"SLACK_ALLOWED_CHANNELS": None}, {"SLACK_ALLOWED_CHANNELS": " "},   # make check (Deploy) needs one
    {"SLACK_ALLOWED_CHANNELS": "G0123ABCD"},              # make check wants a C… first
    {"BRIDGE_MODEL": "claude-sonet-5-5"},                 # a typo would fail every answer
    {"GOTCHA_SSH_USER": "a" * 5000},
    {"BOT_UID": "0"}, {"CLAUDE_BIN": "/bin/sh"},          # read-only here
    {"ANTHROPIC_API_KEY": "x"}, {"LD_PRELOAD": "/tmp/x.so"},
    {"BRIDGE_EFFORT": "high\nLD_PRELOAD=/tmp/x.so"},
])
def test_bad_or_read_only_values_are_refused_before_a_code_is_spent(client, clock, dotenv, changes):
    code = clock.code("admin@example.com")
    r = client.put("/api/bot/settings", headers=ADMIN, json={"changes": changes, "code": code})
    assert r.status_code == 400, r.text
    assert dotenv.read_text() == ENV
    assert auth.verify("admin@example.com", code) is None   # the code is still unused


def test_a_viewer_or_a_wrong_code_changes_nothing(client, clock, dotenv):
    assert client.put("/api/bot/settings", json={"changes": {"BRIDGE_EFFORT": "high"}}).status_code == 403
    assert _save(client, clock, code="000000", BRIDGE_EFFORT="high").status_code == 401
    assert _save(client, clock, code="", BRIDGE_EFFORT="high").status_code == 401
    assert dotenv.read_text() == ENV


def test_empty_users_or_ssh_user_means_the_default(client, clock, dotenv):
    dotenv.write_text(ENV + "SLACK_ALLOWED_USERS=U0AAAAAAA\nGOTCHA_SSH_USER=gotcha\n")
    r = _save(client, clock, SLACK_ALLOWED_USERS="", GOTCHA_SSH_USER="")
    assert r.status_code == 200, r.text
    _wait_job()
    assert dotenv.read_text() == ENV


def test_a_setting_not_sent_keeps_its_line_exactly(client, clock, dotenv):
    # BRIDGE_MODEL= (empty: Claude Code's own default) is not the same as no line (sonnet).
    dotenv.write_text(ENV.replace("BRIDGE_MODEL=claude-sonnet-5-5", "BRIDGE_MODEL="))
    assert _save(client, clock, BRIDGE_EFFORT="low").status_code == 200
    _wait_job()
    assert "\nBRIDGE_MODEL=\n" in dotenv.read_text()


def test_nothing_is_written_without_a_dotenv(client, clock, dotenv):
    dotenv.unlink()
    assert _save(client, clock, BRIDGE_EFFORT="low").status_code == 400
    assert not dotenv.exists()


def test_a_quoted_value_is_read_as_the_bot_reads_it(client, clock, dotenv):
    dotenv.write_text(ENV + "BRIDGE_ALLOWED_HOSTS='axon-gotcha-[0-9]+|lab-pc-1'\n")
    rows = {s["name"]: s for s in client.get("/api/bot").json()["settings"]}
    assert rows["BRIDGE_ALLOWED_HOSTS"]["value"] == "axon-gotcha-[0-9]+|lab-pc-1"
    # The same machines typed with other spacing: nothing to write, and no restart.
    r = _save(client, clock, BRIDGE_ALLOWED_HOSTS="axon-gotcha-[0-9]+ | lab-pc-1")
    assert r.json() == {"status": "unchanged", "changed": []}


def test_a_model_may_ask_for_the_long_context(client, clock, dotenv):
    assert _save(client, clock, BRIDGE_MODEL="claude-opus-5-5[1m]").status_code == 200
    _wait_job()
    assert "\nBRIDGE_MODEL='claude-opus-5-5[1m]'\n" in dotenv.read_text()


@pytest.mark.parametrize("users", ["", ","])
def test_a_bot_allowed_by_users_alone_keeps_someone_allowed(client, clock, dotenv, users):
    # A hand-edited .env with no channels: removing the users would leave the bot answering nobody.
    dotenv.write_text("SLACK_ALLOWED_USERS=U0AAAAAAA\nSLACK_ALLOWED_CHANNELS=,\n")
    r = _save(client, clock, SLACK_ALLOWED_USERS=users)
    assert r.status_code == 400 and "nobody" in r.text
