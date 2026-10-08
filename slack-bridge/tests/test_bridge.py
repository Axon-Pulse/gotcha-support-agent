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


def test_the_prompt_asks_for_the_reply_in_the_pms_language():
    """The reply template is English; without this a Hebrew question got an English answer."""
    assert "language of the PM's latest message" in B.system_prompt()


def test_the_prompt_keeps_commands_out_of_hebrew_sentences():
    assert "never put a command or a log line inside a Hebrew sentence" in B.system_prompt()


def test_the_prompt_gives_the_hebrew_labels_instead_of_leaving_them_to_the_model():
    prompt = B.system_prompt()
    for label in ("*מה אומרים ללקוח:*", "*מה עושים:*", "*לוודא שהתקלה נפתרה:*", "*רמת ביטחון:*"):
        assert label in prompt


def test_an_english_answer_is_left_exactly_as_it_is():
    text = "*gotcha 4: radar off*\n1. Run `docker restart gotcha_c2`\n```\nlog line\n```"
    assert B.rtl_fix(text) == text


def test_a_hebrew_line_is_isolated_rtl_and_its_inline_code_ltr_outside_the_backticks():
    out = B.rtl_fix("1. מריצים `docker restart x` ובודקים")
    assert out == "\u2067\u20661\u2069. מריצים \u2066`docker restart x`\u2069 ובודקים\u2069"


def test_each_english_run_is_isolated_so_an_arrow_cannot_join_two_of_them():
    out = B.rtl_fix("ימני ← Transmitter ← Start transmitting).")
    assert "\u2066Transmitter\u2069 ← \u2066Start transmitting\u2069)." in out


def test_an_ellipsis_that_belongs_to_a_menu_name_stays_inside_the_english_run():
    assert "\u2066Transmitter…\u2069" in B.rtl_fix("בחרו Transmitter… עכשיו")


def test_an_ip_keeps_its_dots_and_a_closing_full_stop_stays_outside():
    out = B.rtl_fix("הכתובת 192.168.44.60, ועונה ל-ping.")
    assert "\u2066192.168.44.60\u2069," in out and "\u2066ping\u2069." in out


def test_double_asterisk_bold_becomes_slacks_single_asterisk_in_a_hebrew_answer():
    out = B.rtl_fix("לחצו **Start** עכשיו")
    assert "*" in out and "**" not in out


def test_slack_links_and_emoji_are_left_alone_in_a_hebrew_line():
    out = B.rtl_fix("ראו <https://x.io/a|doc> :warning: עכשיו")
    assert "<https://x.io/a|doc>" in out and ":warning:" in out and "\u2066warning" not in out


def test_code_blocks_and_english_lines_inside_a_hebrew_answer_are_untouched():
    text = "מריצים את הפקודה:\n```\ndocker restart x  # שורה\n```\nDone."
    lines = B.rtl_fix(text).split("\n")
    assert lines[0] == "\u2067מריצים את הפקודה:\u2069"
    assert lines[1:] == text.split("\n")[1:]


def test_the_direction_marks_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(B, "RTL_MARKS", False)
    assert B.rtl_fix("שלום") == "שלום"


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


def test_a_first_turn_gets_its_session_id_from_the_bridge(echo_cli):
    seen = json.loads(B.run_claude("map is empty on gotcha 3", None)["text"])
    assert "--resume" not in seen["argv"]
    assert len(seen["argv"][seen["argv"].index("--session-id") + 1]) == 36   # a uuid


def test_a_resumed_turn_does_not_set_a_session_id(echo_cli):
    seen = json.loads(B.run_claude("and the camera?", "sess-1")["text"])
    assert "--session-id" not in seen["argv"]


def test_a_turn_killed_at_the_timeout_still_keeps_its_thread_session(tmp_path, monkeypatch):
    """Else the PM's next message starts a new session that never saw the report."""
    cli = tmp_path / "claude"
    cli.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(B, "CLAUDE", str(cli))
    monkeypatch.setattr(B, "TIMEOUT_S", 1)
    res = B.run_claude("map is empty on gotcha 3", None)
    assert not res["ok"] and res["text"].startswith("No answer within 1s")
    assert res["session_id"] and len(res["session_id"]) == 36


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


def test_the_workspace_is_trusted_so_the_allow_list_applies(tmp_path, monkeypatch):
    """Claude Code ignores permissions.allow from an untrusted workspace: in a fresh container every
    Bash and Read was refused and the bot said it couldn't check anything."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    B.ensure_workspace_trusted()
    f = tmp_path / "cfg" / ".claude.json"
    assert json.loads(f.read_text())["projects"][str(B.WORKSPACE)]["hasTrustDialogAccepted"] is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_trusting_the_workspace_keeps_what_claude_already_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    f = tmp_path / ".claude.json"
    f.write_text(json.dumps({"userID": "u", "projects": {"/elsewhere": {"x": 1}, str(B.WORKSPACE): {"y": 2}}}))
    B.ensure_workspace_trusted()
    B.ensure_workspace_trusted()   # a second start changes nothing
    d = json.loads(f.read_text())
    assert d["userID"] == "u" and d["projects"]["/elsewhere"] == {"x": 1}
    assert d["projects"][str(B.WORKSPACE)] == {"y": 2, "hasTrustDialogAccepted": True}


def test_the_bridge_does_not_start_over_a_config_it_cannot_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude.json").write_text("{not json")
    with pytest.raises(SystemExit):
        B.ensure_workspace_trusted()
