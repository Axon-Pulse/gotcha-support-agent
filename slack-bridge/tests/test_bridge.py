"""The bridge without Slack or Claude: argv, result parsing, threads, admission."""
import json
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bridge as B  # noqa: E402


def test_the_bot_runs_unattended_on_project_settings_only():
    argv = B.claude_argv(None)
    assert argv[:2] == [B.CLAUDE, "-p"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--resume" not in argv


def test_the_prompt_tells_the_bot_it_has_only_the_skills_scripts():
    """Otherwise the model spends turns on follow-up commands the guard will only refuse."""
    prompt = B.system_prompt()
    assert "Only the skill's scripts are available" in prompt
    assert not hasattr(B, "FOLLOWUPS") and not hasattr(B, "STRICT_NOTE")


def test_the_allowed_sites_reach_the_guard_and_the_listing(monkeypatch):
    monkeypatch.setattr(B, "ALLOWED_HOSTS", r"axon-gotcha-[0-9]+")
    env = B.child_env()
    assert env["BRIDGE_ALLOWED_HOSTS"] == env["GOTCHA_SYSTEMS_REGEX"] == r"axon-gotcha-[0-9]+"


def test_a_message_cannot_widen_the_allowed_sites_through_the_environment(monkeypatch):
    """The bridge sets these itself on every turn, over whatever the process had."""
    monkeypatch.setenv("GOTCHA_SYSTEMS_REGEX", ".*")
    assert B.child_env()["GOTCHA_SYSTEMS_REGEX"] == B.ALLOWED_HOSTS


# --------------------------------------------------------------------------
# The bridge proves the guard works before it starts listening
# --------------------------------------------------------------------------

def _workspace_with_hook(tmp_path, monkeypatch, hook_command: str):
    ws = tmp_path / "workspace"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command}]}]}}))
    monkeypatch.setattr(B, "WORKSPACE", ws)


def test_the_real_guard_passes_the_startup_check():
    B.verify_guard()


@pytest.mark.parametrize("hook_command,reason", [
    ("true", "a hook that allows everything"),
    ("exit 1", "a hook that fails with 1, which Claude Code treats as 'run the command anyway'"),
])
def test_the_bridge_refuses_to_start_when_the_guard_does_not_block(tmp_path, monkeypatch, hook_command, reason):
    _workspace_with_hook(tmp_path, monkeypatch, hook_command)
    with pytest.raises(SystemExit, match="did not block"):
        B.verify_guard()


@pytest.mark.parametrize("hook_command", [
    "exit 2",
    "python3 /nonexistent/bash_guard.py",      # a missing file makes python exit 2: safe, but useless
])
def test_the_bridge_refuses_to_start_when_the_guard_blocks_the_skills_own_scripts(tmp_path, monkeypatch, hook_command):
    _workspace_with_hook(tmp_path, monkeypatch, hook_command)
    with pytest.raises(SystemExit, match="blocked the skill's own"):
        B.verify_guard()


def test_a_thread_reply_resumes_its_session():
    argv = B.claude_argv("sess-1")
    assert argv[argv.index("--resume") + 1] == "sess-1"


def test_slack_tokens_never_reach_the_model(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    env = B.child_env()
    assert not any(k.startswith("SLACK_") for k in env)


def test_a_successful_result_is_parsed():
    res = B.parse_result(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "*axon-gotcha-3: map shows demo data*", "session_id": "abc",
        "duration_ms": 41200, "num_turns": 6, "total_cost_usd": 0.31}))
    assert res["ok"] and res["session_id"] == "abc" and res["seconds"] == 41
    assert B.footer(res) == "_41s · 6 steps · $0.31_"


def test_an_error_or_garbage_result_is_still_an_answer():
    assert not B.parse_result("not json")["ok"]
    res = B.parse_result(json.dumps({"subtype": "error_max_turns", "is_error": True,
                                     "session_id": "abc"}))
    assert not res["ok"] and "error_max_turns" in res["text"] and res["session_id"] == "abc"


def test_long_answers_are_cut_at_a_line():
    text = "\n".join(f"line {i}" for i in range(2000))
    out = B.fit(text)
    assert len(out) < B._SLACK_LIMIT + 40 and out.endswith("cut to fit one message._")


def test_threads_survive_a_restart(tmp_path):
    p = tmp_path / "threads.json"
    B.ThreadStore(p).put("C1", "111.1", "sess-1")
    assert B.ThreadStore(p).get("C1", "111.1") == "sess-1"
    assert B.ThreadStore(p).get("C1", "222.2") is None


def test_an_unconfigured_allowlist_refuses_everyone(monkeypatch):
    monkeypatch.setattr(B, "ALLOWED_CHANNELS", set())
    monkeypatch.setattr(B, "ALLOWED_USERS", set())
    assert B.authorise("U1", "C1")[0] is False


def test_channel_and_dm_admission(monkeypatch):
    monkeypatch.setattr(B, "ALLOWED_CHANNELS", {"C_SUPPORT"})
    monkeypatch.setattr(B, "ALLOWED_USERS", set())
    monkeypatch.setattr(B, "ALLOW_DMS", False)
    assert B.authorise("U1", "C_SUPPORT")[0]
    assert not B.authorise("U1", "C_OTHER")[0]
    assert not B.authorise("U1", "D123")[0]


def test_the_socket_mode_app_starts_without_a_signing_secret(monkeypatch):
    """Built with the real slack_bolt: this is the call that failed on first start."""
    pytest.importorskip("slack_bolt")
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    # token_verification_enabled=False: no auth.test call to Slack from a test.
    app = B.make_app("xoxb-test", token_verification_enabled=False)
    assert app is not None


class FakeSlack:
    def __init__(self):
        self.posts, self.updates = [], []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"ack-{len(self.posts)}"}

    def chat_update(self, **kw):
        self.updates.append(kw)


def test_a_mention_acks_at_once_then_edits_in_the_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "ALLOWED_CHANNELS", {"C1"})
    monkeypatch.setattr(B, "run_claude", lambda text, sid: {
        "ok": True, "text": f"answer to: {text}", "session_id": "s-new", "seconds": 30})
    started = []
    monkeypatch.setattr(B.threading, "Thread",
                        lambda target, args, daemon: started.append((target, args))
                        or type("T", (), {"start": lambda self: target(*args)})())
    slack = FakeSlack()
    b = B.Bridge(slack, B.ThreadStore(tmp_path / "t.json"))
    b.handle({"channel": "C1", "user": "U1", "ts": "111.1",
              "text": "<@UBOT> map is empty on gotcha 3"}, "Ev1")
    assert slack.posts[0]["text"].startswith(":mag: Checking")
    assert slack.updates[0]["ts"] == "ack-1"
    assert slack.updates[0]["text"].startswith("answer to: map is empty on gotcha 3")
    assert b.store.get("C1", "111.1") == "s-new"

    b.handle({"channel": "C1", "user": "U1", "ts": "111.1",
              "text": "<@UBOT> map is empty on gotcha 3"}, "Ev1")   # redelivered
    assert len(slack.posts) == 1, "a redelivered event must not run twice"


# --------------------------------------------------------------------------
# A Slack message is data, never a flag of the claude CLI
# --------------------------------------------------------------------------

@pytest.fixture
def echo_cli(tmp_path, monkeypatch):
    """A stand-in for `claude` that reports what it was given on stdin and as arguments."""
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "seen = {'stdin': sys.stdin.read(), 'argv': sys.argv[1:]}\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "                  'result': json.dumps(seen), 'session_id': 's'}))\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(B, "CLAUDE", str(cli))
    return cli


@pytest.mark.parametrize("message", [
    "--version",                                  # ran `claude --version` when it was an argument
    "--dangerously-skip-permissions",
    "--allowedTools=Bash",
    "--add-dir=/",
    "-c",
    "--settings={\"permissions\":{\"allow\":[\"Read\"]}}",
    "--allowedTools=Bash\nwhat is wrong with gotcha 3?",
    "map is empty on gotcha 3",
])
def test_a_slack_message_reaches_claude_on_stdin_and_never_as_an_argument(echo_cli, message):
    res = B.run_claude(message, None)
    assert res["ok"], res
    seen = json.loads(res["text"])
    assert seen["stdin"] == message
    assert message not in seen["argv"]
    assert not any(a == message or a.startswith(message) for a in seen["argv"])


def test_a_thread_reply_still_resumes_its_session_with_the_message_on_stdin(echo_cli):
    seen = json.loads(B.run_claude("and the camera?", "sess-1")["text"])
    assert seen["argv"][seen["argv"].index("--resume") + 1] == "sess-1"
    assert seen["stdin"] == "and the camera?"


def test_the_argv_carries_no_user_text_at_all():
    argv = B.claude_argv("sess-1")
    # Everything in it is fixed by the bridge, apart from the session id.
    assert all(not a.startswith("<@") for a in argv)
    assert B.claude_argv.__code__.co_varnames[:B.claude_argv.__code__.co_argcount] == ("session_id",)


# --------------------------------------------------------------------------
# What the bot may read: an allow-list, not a deny-list
# --------------------------------------------------------------------------

SETTINGS = json.loads((B.WORKSPACE / ".claude" / "settings.json").read_text())
ALLOW, DENY = SETTINGS["permissions"]["allow"], SETTINGS["permissions"]["deny"]


def test_reading_is_scoped_to_paths_never_granted_wholesale():
    """A bare `Read` (or Grep or Glob) lets the bot read any file on the host. The `**/.env`
    style denies only match under the workspace, so they cannot make a bare grant safe."""
    for tool in ("Read", "Grep", "Glob"):
        assert tool not in ALLOW, f"bare {tool} in allow: the whole filesystem is readable"
    reads = [a for a in ALLOW if a.startswith("Read(")]
    assert reads, "the bot has to read the workspace (the skill) and the saved triage files"
    assert "Read(./**)" in ALLOW and "Read(//tmp/gotcha-triage/**)" in ALLOW
    for rule in reads:
        assert not rule.startswith(("Read(//**", "Read(/**", "Read(~/**", "Read(**")), rule


def test_the_support_skill_is_allowed_by_name():
    """Checked against the real claude: this does NOT stop other skills (the bundled ones, such
    as `simplify`) from loading, because an allow rule does not limit which skills load. What
    contains them is that the tools they would use (Write, Edit, Agent) are denied, below."""
    assert "Skill" not in ALLOW
    assert "Skill(gotcha-support)" in ALLOW


def test_the_write_and_network_tools_stay_denied_and_the_guard_hook_stays_wired():
    for tool in ("Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent"):
        assert tool in DENY
    hooks = SETTINGS["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "Bash"
    assert "bash_guard.py" in hooks[0]["hooks"][0]["command"]


def test_the_skill_is_read_through_an_absolute_rule_the_bridge_adds():
    """The skill is a symlink, and a read is judged on the path it resolves to, so a relative
    `./**` rule does not cover it. The bridge adds the absolute rule for this machine."""
    assert (B.SKILL_DIR / "SKILL.md").is_file()
    overlay = json.loads(B.permission_overlay())["permissions"]
    assert overlay["allow"] == [f"Read(//{str(B.SKILL_DIR).lstrip('/')}/**)"]
    assert f"/{B.SKILL_DIR}/**".startswith("//")        # Claude Code's absolute-path form


def test_secret_file_names_are_denied_inside_the_skill_directory_too():
    """settings.json's `**/.env` patterns only match under the workspace; the skill directory
    is elsewhere, so the same names are denied there by absolute path."""
    deny = json.loads(B.permission_overlay())["permissions"]["deny"]
    root = f"//{str(B.SKILL_DIR).lstrip('/')}"
    for name in (".env", ".env.*", "secrets.local.env", "*.pem", "*.key", "id_*"):
        assert f"Read({root}/**/{name})" in deny, name


def test_the_overlay_is_passed_on_every_turn_and_cannot_come_from_a_message():
    for sid in (None, "sess-1"):
        argv = B.claude_argv(sid)
        assert argv[argv.index("--settings") + 1] == B.permission_overlay()
