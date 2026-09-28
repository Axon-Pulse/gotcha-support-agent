"""A diagnosis is a conversation, and each turn is its own graph session.

The property worth protecting is the one conversation.py is built around: a follow-up
never reaches a tool. It answers from findings already in hand or it says it cannot —
so no amount of "now restart the node" in a follow-up can touch a device.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agents as A  # noqa: E402
import conversation as convo  # noqa: E402
import llm  # noqa: E402

REPORT = {"bottom_line": "Two launcher sessions are running the same config; stop the "
                         "older one (uptime 26690s). The sensor is fine.",
          "root_cause": "Two launcher sessions", "confidence": "high",
          "evidence": ["asu uptime_s 30 and 26690 in one sample"],
          "suggested_actions": ["stop the older session"], "escalate": False,
          "unknowns": []}

TURNS = [{"kind": "run", "question": "no tracks", "report": REPORT,
          "findings": [{"agent": "triage", "tool": "get_system_health", "ok": True,
                        "data": {"asu": {"uptime_s": 26690}}}],
          "usage": {"input": 100, "output": 10, "cache_read": 900}, "duration_s": 38.0}]


# ---------- routing ----------

def test_the_first_message_never_asks_the_model():
    """No history means nothing to answer from, so there is nothing to decide."""
    called = []
    orig = llm.ask_json
    llm.ask_json = lambda **kw: called.append(kw) or {}
    try:
        got = convo.route([], "the acoustic sensor shows no tracks")
    finally:
        llm.ask_json = orig
    assert got["kind"] == "run"
    assert not called, "asked the router a question it could only answer one way"


@pytest.mark.parametrize("kind", ["run", "follow_up"])
def test_the_router_decides_later_messages(monkeypatch, kind):
    monkeypatch.setattr(llm, "ask_json", lambda **kw: {"kind": kind, "why": "because"})
    got = convo.route(TURNS, "which one do I stop?")
    assert got == {"kind": kind, "why": "because"}


def test_an_unusable_router_answer_falls_back_to_a_full_run(monkeypatch):
    """Guessing cheaply is the expensive mistake: it answers from stale evidence."""
    monkeypatch.setattr(llm, "ask_json", lambda **kw: {"kind": "maybe?", "why": ""})
    got = convo.route(TURNS, "now the camera is offline")
    assert got["kind"] == "run"
    assert "router" in got["why"]


def test_a_failed_routing_call_falls_back_to_a_full_run(monkeypatch):
    """The bug this exists for: an APITimeoutError on the cheap call killed the turn.

    Routing decides whether the expensive path can be SKIPPED. If it cannot be reached,
    the answer is to do the work, not to give up on a conversation the full pipeline
    would have answered.
    """
    def boom(**kw):
        raise TimeoutError("Request timed out or interrupted.")

    monkeypatch.setattr(llm, "ask_json", boom)
    got = convo.route(TURNS, "which one do I stop?")
    assert got["kind"] == "run"
    assert "TimeoutError" in got["why"], "the operator is not told why it ran the long way"


def test_the_router_is_shown_the_evidence_not_just_the_questions(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "ask_json",
                        lambda **kw: seen.update(kw) or {"kind": "follow_up", "why": ""})
    convo.route(TURNS, "which one?")
    assert "26690" in seen["prompt"], "routed without the findings it needs to decide"
    assert "no tracks" in seen["prompt"], "routed without the original question"


# ---------- follow-ups cannot touch anything ----------

def test_the_follow_up_agent_has_no_tools():
    """The safety property, asserted on the config the console can edit."""
    cfg = A.post_agents().get("follow_up")
    assert cfg is not None, "the follow_up agent is gone; a follow-up would have no prompt"
    assert cfg.get("tools") == [], "a follow-up with tools can reach a device"


def test_a_follow_up_runs_a_text_agent_and_never_the_graph(monkeypatch):
    used = {}

    def fake(cfg, content, max_tokens=2000):
        used.update(cfg=cfg, content=content)
        return "The older one, PID 291846.", {"input": 5, "output": 2, "cache_read": 0}

    monkeypatch.setattr(llm, "run_text_agent", fake)
    text, usage = convo.answer_follow_up(TURNS, "which one do I stop?")

    assert text == "The older one, PID 291846."
    assert usage["input"] == 5
    assert used["cfg"].get("tools") == [], "handed a follow-up a tool-carrying agent"
    assert "26690" in used["content"], "answered without the evidence in front of it"


def test_a_follow_up_still_answers_if_the_agent_was_deleted(monkeypatch):
    """A console edit must not make a conversation unanswerable."""
    monkeypatch.setattr(A, "post_agents", lambda: {})
    monkeypatch.setattr(llm, "run_text_agent", lambda cfg, content, **k: (cfg["prompt"], {}))
    text, _ = convo.answer_follow_up(TURNS, "which one?")
    assert "evidence" in text.lower()


# ---------- what a turn records ----------

def test_a_turn_records_how_long_it_took(monkeypatch):
    t = {"started_at": 100.0}
    monkeypatch.setattr(convo.time, "time", lambda: 138.5)
    done = convo.finish_turn(t, kind="run", question="q")
    assert done["duration_s"] == 38.5 and done["ended_at"] == 138.5
    assert done["kind"] == "run" and done["question"] == "q"


def test_totals_add_up_across_turns():
    turns = [
        {"kind": "run", "duration_s": 38.0,
         "usage": {"input": 100, "output": 10, "cache_read": 900}},
        {"kind": "follow_up", "duration_s": 2.0,
         "usage": {"input": 20, "output": 5, "cache_read": 0}},
        {"kind": "run", "duration_s": 31.0, "usage": {"input": 80, "output": 8}},
    ]
    assert convo.totals(turns) == {
        "turns": 3, "runs": 2, "follow_ups": 1, "duration_s": 71.0,
        "input": 200, "output": 23, "cache_read": 900}


def test_totals_of_an_empty_conversation_are_zero_not_missing():
    assert convo.totals([])["turns"] == 0
    assert convo.totals([])["input"] == 0


def test_the_digest_carries_follow_ups_too():
    """A second follow-up must see what the first one was told, or it repeats itself."""
    turns = TURNS + [{"kind": "follow_up", "question": "which one?",
                      "answer": "The older one, PID 291846."}]
    d = convo.digest(turns)
    assert "PID 291846" in d and "which one?" in d


# ---------- the console surface ----------

@pytest.fixture
def client(monkeypatch, tmp_path):
    import console.server as srv
    monkeypatch.setattr(srv, "TRACES", tmp_path / "traces")
    monkeypatch.setattr(srv, "_credentials", lambda: {"ready": True, "source": "test"})
    srv.CONVERSATIONS.clear()
    yield TestClient(srv.app)
    srv.CONVERSATIONS.clear()


def test_a_conversation_is_created_then_answered(client, monkeypatch, tmp_path):
    import console.server as srv
    monkeypatch.setattr(convo, "route",
                        lambda turns, m: {"kind": "follow_up", "why": "asked about it"})
    monkeypatch.setattr(convo, "answer_follow_up",
                        lambda turns, m: ("PID 291846.", {"input": 5, "output": 2}))

    cid = client.post("/api/conversation").json()["conversation_id"]
    assert client.get(f"/api/conversation/{cid}").json()["turns"] == []

    srv._converse(cid, "which one do I stop?")          # run the worker inline
    c = client.get(f"/api/conversation/{cid}").json()

    assert len(c["turns"]) == 1
    turn = c["turns"][0]
    assert turn["kind"] == "follow_up" and turn["answer"] == "PID 291846."
    assert turn["question"] == "which one do I stop?"
    assert turn["duration_s"] >= 0 and turn["usage"] is not None
    assert c["totals"]["turns"] == 1 and c["totals"]["follow_ups"] == 1


def test_every_turn_is_appended_to_the_conversation_trace(client, monkeypatch, tmp_path):
    import console.server as srv
    monkeypatch.setattr(convo, "route", lambda turns, m: {"kind": "follow_up", "why": ""})
    monkeypatch.setattr(convo, "answer_follow_up", lambda turns, m: ("ok", {"input": 1}))

    cid = client.post("/api/conversation").json()["conversation_id"]
    srv._converse(cid, "first")
    srv._converse(cid, "second")

    lines = [json.loads(x) for x in
             (tmp_path / "traces" / f"{cid}.jsonl").read_text().splitlines() if x.strip()]
    assert len(lines) == 2, "a conversation must be readable back turn by turn"
    assert [x["question"] for x in lines] == ["first", "second"]
    assert all(x["conversation"] == cid for x in lines)
    assert lines[-1]["totals"]["turns"] == 2, "each line carries the running total"


def test_a_failed_turn_is_still_recorded(client, monkeypatch, tmp_path):
    """It spent tokens and took time; dropping it makes the trace a lie."""
    import console.server as srv
    monkeypatch.setattr(convo, "route", lambda turns, m: (_ for _ in ()).throw(
        RuntimeError("model unreachable")))

    cid = client.post("/api/conversation").json()["conversation_id"]
    srv._converse(cid, "no tracks")
    c = client.get(f"/api/conversation/{cid}").json()

    assert len(c["turns"]) == 1 and c["turns"][0]["kind"] == "error"
    assert "model unreachable" in c["turns"][0]["error"]
    assert (tmp_path / "traces" / f"{cid}.jsonl").exists()


def test_an_empty_message_is_refused(client):
    cid = client.post("/api/conversation").json()["conversation_id"]
    r = client.post(f"/api/conversation/{cid}/message", json={"message": "   "})
    assert r.status_code == 400


def test_a_busy_conversation_refuses_a_second_message(client):
    import console.server as srv
    cid = client.post("/api/conversation").json()["conversation_id"]
    srv._cset(cid, status="working:run")
    r = client.post(f"/api/conversation/{cid}/message", json={"message": "and another"})
    assert r.status_code == 409, "two turns at once would interleave their token counts"


def test_an_unknown_conversation_is_404(client):
    assert client.get("/api/conversation/nope").status_code == 404
    assert client.post("/api/conversation/nope/message",
                       json={"message": "hi"}).status_code == 404
