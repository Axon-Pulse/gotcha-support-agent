"""Slack: a thread is a conversation, and files posted in it reach the agents as text."""
import json

import pytest

import attachments as att
import slack_app as S

PNG_B = b"\\x89PNG fake"
DESC = "Radar page. Banner in red: 'magos_node PROC_EXITED_ERROR'."
REPORT = {"root_cause": "magos exited", "confidence": "high", "escalate": False,
          "evidence": ["magos_node: no process"], "unknowns": [], "suggested_actions": []}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "TRACES", tmp_path / "traces")
    monkeypatch.setattr(S, "DB", str(tmp_path / "graph.db"))
    monkeypatch.setattr(att, "DIR", tmp_path / "attachments")
    monkeypatch.setattr(S, "ALLOWED_CHANNELS", {"C_OPS"})
    monkeypatch.setattr(S, "ALLOWED_USERS", set())
    monkeypatch.setattr(S, "ALLOW_DMS", False)
    monkeypatch.setattr(S, "BOT_USER_ID", "UBOT")
    monkeypatch.setattr(S, "BOT_ID", "BBOT")
    monkeypatch.setattr(S, "_client", None)
    monkeypatch.setattr(S.llm, "describe_attachment", fake_describe)
    S._seen.clear(); S._sessions.clear(); S._last_request.clear()


def fake_describe(data, mt, name):
    """Like the real one, it books its tokens on the calling thread's running total —
    the session's own, since the gateway describes files inside the session's thread."""
    class U:
        input_tokens, output_tokens, cache_read_input_tokens = 10, 5, 0
    return DESC, S.llm._account(type("R", (), {"usage": U})())


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(S, "post", lambda ch, text, thread=None: sent.append((ch, text, thread)))
    return sent


def FILE(name="radar.png", mimetype="image/png", **kw):
    return {"name": name, "mimetype": mimetype, "size": len(PNG_B),
            "url_private_download": f"https://files.slack.com/{name}", **kw}


# ---------- which events are requests ----------

@pytest.mark.parametrize("event,wanted", [
    ({"user": "U1", "channel": "C_OPS", "ts": "1", "text": "<@UBOT> no tracks"}, "no tracks"),
    ({"user": "U1", "channel": "D1", "ts": "1", "text": "latency > 200ms"}, "latency > 200ms"),
    ({"user": "U1", "channel": "D1", "ts": "1", "subtype": "file_share", "text": "",
      "files": [FILE()]}, ""),
])
def test_messages_and_file_shares_are_requests(event, wanted):
    req = S.parse_event(event)
    assert req is not None and req["text"] == wanted


@pytest.mark.parametrize("event", [
    {"user": "U1", "channel": "C_OPS", "ts": "1", "subtype": "message_changed"},
    {"bot_id": "BBOT", "channel": "C_OPS", "ts": "1", "text": "my own answer"},
])
def test_edits_and_bot_posts_are_not(event):
    assert S.parse_event(event) is None


# ---------- admission ----------

def test_a_file_alone_is_enough_to_ask(monkeypatch):
    started = []
    monkeypatch.setattr(S, "start_session", lambda *a: started.append(a) or "sid")
    out = S.accept("E1", "U1", "C_OPS", "1.0", "", [FILE()], "1.0")
    assert out["status"] == "accepted" and "Reading 1 attached file" in out["reply"]
    assert started[0][4] == [FILE()]


def test_a_bare_mention_is_empty_at_the_top_but_not_in_a_thread(monkeypatch):
    monkeypatch.setattr(S, "start_session", lambda *a: "sid")
    assert S.accept("E1", "U1", "C_OPS", "1.0", "", [], "1.0")["status"] == "empty"
    assert S.accept("E2", "U2", "C_OPS", "1.0", "", [], "2.0")["status"] == "accepted"


# ---------- files ----------

def test_files_are_downloaded_and_described(monkeypatch):
    monkeypatch.setattr(S, "_download", lambda f: PNG_B)
    recs, skipped = S._files_to_attachments([FILE()])
    assert skipped == [] and recs[0]["text"] == DESC and recs[0]["kind"] == "image"


def test_unusable_files_are_skipped_with_a_reason_and_never_fetched(monkeypatch):
    fetched = []
    monkeypatch.setattr(S, "_download", lambda f: fetched.append(f["name"]) or b"MZ")
    recs, skipped = S._files_to_attachments([
        FILE("hidden.png", mode="hidden_by_limit"),
        FILE("huge.png", size=att.MAX_BYTES + 1),
        FILE("setup.exe", "application/x-msdownload")])
    assert recs == [] and fetched == ["setup.exe"], "hidden and oversized files downloaded"
    assert "file limit" in skipped[0] and "over the" in skipped[1]
    assert "not supported" in skipped[2]


# ---------- what the thread holds since the last answer ----------

class FakeClient:
    def __init__(self, messages=None, error=None):
        self.messages, self.error = messages or [], error

    def conversations_replies(self, **kw):
        if self.error:
            raise self.error
        return {"messages": self.messages}


THREAD = [
    {"ts": "100.0", "user": "U1", "text": "<@UBOT> no tracks"},            # the opener
    {"ts": "101.0", "user": "U2", "text": "old, before the answer", "files": [FILE("old.png")]},
    {"ts": "102.0", "user": "UBOT", "bot_id": "BBOT", "text": "*Diagnosis* …"},
    {"ts": "103.0", "user": "U2", "text": "here is the radar page", "files": [FILE()]},
    {"ts": "104.0", "user": "U3", "text": "<@UBOT> and the camera?"},     # its own request
    {"ts": "105.0", "user": "U_OUT", "text": "not allowed", "files": [FILE("x.png")]},
    {"ts": "106.0", "user": "U1", "text": "<@UBOT> what do you make of these?"},  # now
]


def test_only_what_was_posted_since_the_answer_and_not_already_a_request(monkeypatch):
    monkeypatch.setattr(S, "_client", FakeClient(THREAD))
    monkeypatch.setattr(S, "ALLOWED_USERS", {"U1", "U2", "U3"})
    files, lines, notes = S.thread_since_last_answer("C_OPS", "100.0", "106.0")
    assert [f["name"] for f in files] == ["radar.png"]
    assert lines == ["<@U2>: here is the radar page"] and notes == []


def test_without_the_history_scope_it_says_so_and_carries_on(monkeypatch):
    class SlackApiError(Exception):
        response = {"error": "missing_scope"}
    monkeypatch.setattr(S, "_client", FakeClient(error=SlackApiError()))
    files, lines, notes = S.thread_since_last_answer("C_OPS", "100.0", "106.0")
    assert files == [] and "missing_scope" in notes[0] and "channels:history" in notes[0]


def test_dms_and_top_level_messages_do_not_look_back(monkeypatch):
    monkeypatch.setattr(S, "_client", FakeClient(THREAD))
    assert S.thread_since_last_answer("D1", "100.0", "106.0") == ([], [], [])
    assert S.thread_since_last_answer("C_OPS", "106.0", "106.0") == ([], [], [])


# ---------- a thread is a conversation ----------

class FakeGraph:
    def __init__(self):
        self.asked = []

    def compile(self, checkpointer=None):
        return self

    def invoke(self, payload, cfg):
        self.asked.append(payload["question"])
        return {"question": payload["question"], "report": REPORT, "findings": [],
                "transcript": [], "customer_message": None, "blocked": []}


def test_a_thread_runs_first_then_follows_up_from_what_it_found(monkeypatch, posts):
    g = FakeGraph()
    monkeypatch.setattr(S, "graph", lambda: g)
    monkeypatch.setattr(S, "_download", lambda f: PNG_B)

    # 1: the opener, with a screenshot. No earlier turns, so it is a run.
    S._diagnose("s1", "no tracks", "C_OPS", "100.0", "U1", [FILE()], "100.0")
    assert "no tracks" in g.asked[0] and DESC in g.asked[0], "the agents never read it"
    first = [json.loads(l) for l in (S.TRACES / "s1.jsonl").read_text().splitlines()]
    assert first[-1]["status"] == "done" and first[-1]["kind"] == "run"
    assert first[-1]["origin"]["question"] == "no tracks", "trace shows what was typed"
    assert first[-1]["attachments"][0]["text"] == DESC
    assert first[-1]["usage"]["input"] >= 10, "the description's tokens were dropped"

    # 2: a follow-up in the same thread is routed knowing turn 1 and its screenshot.
    seen = {}

    def route(turns, message):
        seen["turns"] = turns
        return {"kind": "follow_up", "why": "about the same fault"}
    monkeypatch.setattr(S.convo, "route", route)
    monkeypatch.setattr(S.convo, "answer_follow_up",
                        lambda turns, m: ("magos exited — see the banner.", {}))
    S._diagnose("s2", "why did it stop?", "C_OPS", "100.0", "U1", [], "107.0")
    assert len(seen["turns"]) == 1 and seen["turns"][0]["report"] == REPORT
    assert DESC in S.convo.digest(seen["turns"]), "the follow-up forgot the screenshot"
    assert len(g.asked) == 1, "a follow-up ran the whole pipeline"
    assert any("magos exited — see the banner." in p[1] for p in posts)
    second = json.loads((S.TRACES / "s2.jsonl").read_text().splitlines()[-1])
    assert second["kind"] == "follow_up" and second["status"] == "done"


def test_another_thread_is_another_conversation(monkeypatch):
    monkeypatch.setattr(S, "graph", lambda: FakeGraph())
    S._diagnose("s1", "no tracks", "C_OPS", "100.0", "U1", [], "100.0")
    assert S.thread_turns("C_OPS", "200.0") == []
    assert len(S.thread_turns("C_OPS", "100.0")) == 1
    assert S.thread_turns("C_OPS", "100.0", exclude="s1") == []


def test_a_failed_turn_is_not_context_for_the_next(monkeypatch):
    def boom():
        raise RuntimeError("graph broke")
    monkeypatch.setattr(S, "graph", boom)
    S._diagnose("s1", "no tracks", "C_OPS", "100.0", "U1", [], "100.0")
    assert S.thread_turns("C_OPS", "100.0") == []


def test_skipped_files_are_reported_in_the_thread(monkeypatch, posts):
    monkeypatch.setattr(S, "graph", lambda: FakeGraph())
    S._diagnose("s1", "no tracks", "C_OPS", "100.0", "U1",
                [FILE("setup.exe", "application/x-msdownload")], "100.0")
    assert any("Skipped" in p[1] and "setup.exe" in p[1] for p in posts)


def test_a_bare_mention_with_nothing_new_asks_for_detail(monkeypatch, posts):
    monkeypatch.setattr(S, "graph", lambda: pytest.fail("ran with nothing to diagnose"))
    S._diagnose("s1", "", "C_OPS", "100.0", "U1", [], "106.0")
    assert any("Tell me what the system is doing" in p[1] for p in posts)
