"""The bridge without Slack or Claude: argv, result parsing, threads, admission."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bridge as B  # noqa: E402


def test_the_bot_runs_unattended_on_project_settings_only():
    argv = B.claude_argv("map is empty on gotcha 3", None)
    assert argv[:3] == [B.CLAUDE, "-p", "map is empty on gotcha 3"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--resume" not in argv


def test_the_prompt_says_which_mode_the_guard_is_in(monkeypatch):
    monkeypatch.setattr(B, "FOLLOWUPS", False)
    assert B.STRICT_NOTE in B.system_prompt()
    monkeypatch.setattr(B, "FOLLOWUPS", True)
    assert B.STRICT_NOTE not in B.system_prompt()


def test_a_thread_reply_resumes_its_session():
    argv = B.claude_argv("and the camera?", "sess-1")
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
