"""Threads put together from Claude's transcripts and the bridge's turn log."""
import json
import time

from console import conversations
from console.settings import S
from console.tests.conftest import SID, write_transcript, write_turns

T0 = time.time() - 3600
STEPS = [
    ("Skill", {"skill": "gotcha-support", "args": "map empty"}, "Launching skill", False),
    ("Bash", {"command": "scripts/run_triage.sh axon-gotcha-4 | head"}, "Blocked by the Slack bot's read-only guard", True),
    ("Bash", {"command": "scripts/run_triage.sh axon-gotcha-4"}, "=== containers ===\n" + "x\n" * 10, False),
    ("Read", {"file_path": "/app/support-agent-skill/kb/cases/gateway-bound-to-loopback.md"}, "1\t---", False),
]


def _thread(env):
    write_transcript(env, SID, [
        {"q": "the map is empty on gotcha 4", "at": T0, "steps": STEPS,
         "answer": "*axon-gotcha-4: the gateway listens on loopback only*", "cost": 0.2, "took": 40},
        {"q": "and the camera?", "at": T0 + 600, "answer": "*camera: fine*", "cost": 0.05, "took": 10},
    ])
    S.threads_file.write_text(json.dumps({"C1:100.1": SID}))


def test_a_thread_is_rebuilt_from_its_transcript_without_a_log(client, env):
    _thread(env)
    r = client.get("/api/conversations").json()
    assert not r["turn_log"]
    [t] = r["threads"]
    assert (t["key"], t["count"], t["question"]) == ("C1:100.1", 2, "the map is empty on gotcha 4")
    assert t["cost_usd"] == 0.25 and t["users"] == []

    d = client.get("/api/conversations/C1:100.1").json()
    first, second = d["turns"]
    assert first["answer"].startswith("*axon-gotcha-4") and first["seconds"] == 40
    assert first["cases_read"] == ["gateway-bound-to-loopback"]
    tools = [s for s in first["steps"] if s["kind"] == "tool"]
    assert [s["name"] for s in tools] == ["Skill", "Bash", "Bash", "Read"]
    assert tools[1]["denied"] and not tools[2]["denied"]
    assert tools[2]["input"] == "scripts/run_triage.sh axon-gotcha-4"
    assert second["cost_usd"] == 0.05 and second["cases_read"] == []


def test_the_log_adds_who_asked_and_how_it_ended(client, env):
    _thread(env)
    write_turns([
        {"event": "start", "ts": T0 - 1, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": "U_PM"},
        {"event": "end", "ts": T0 + 41, "key": "C1:100.1", "session_id": SID, "outcome": "ok",
         "seconds": 41, "turns": 6, "cost_usd": 0.21, "resumed": False},
        {"event": "start", "ts": T0 + 599, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": "U_PM"},
        {"event": "end", "ts": T0 + 690, "key": "C1:100.1", "session_id": SID, "outcome": "timeout",
         "seconds": 90, "resumed": True},
    ])
    d = client.get("/api/conversations/C1:100.1").json()
    first, second = d["turns"]
    assert first["user"] == "U_PM" and first["seconds"] == 41 and first["cost_usd"] == 0.21
    assert first["question"] == "the map is empty on gotcha 4" and first["source"] == "log"
    assert second["outcome"] == "timeout" and second["answer"] is None
    assert second["question"] == "and the camera?" and second["steps"] is not None
    s = client.get("/api/conversations").json()["threads"][0]
    assert s["users"] == ["U_PM"] and s["outcomes"] == ["ok", "timeout"]


def test_turns_from_before_the_log_stay_in_the_thread(client, env):
    _thread(env)
    write_turns([
        {"event": "start", "ts": T0 + 599, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": "U_PM"},
        {"event": "end", "ts": T0 + 611, "key": "C1:100.1", "session_id": SID, "outcome": "ok", "seconds": 12},
    ])
    first, second = client.get("/api/conversations/C1:100.1").json()["turns"]
    assert (first["source"], first["user"]) == ("transcript", "")
    assert (second["source"], second["user"], second["seconds"]) == ("log", "U_PM", 12)


def test_a_turn_with_a_start_and_no_end_is_running_then_lost(client, env, monkeypatch):
    write_turns([{"event": "start", "ts": time.time() - 5, "key": "C1:200.2", "channel": "C1",
                  "thread_ts": "200.2", "user": "U_PM"}])
    assert [t["key"] for t in conversations.in_flight()] == ["C1:200.2"]
    t = client.get("/api/conversations").json()["threads"][0]
    assert t["running"] and t["outcomes"] == ["running"]

    write_turns([{"event": "start", "ts": time.time() - 3600, "key": "C1:200.2", "channel": "C1",
                  "thread_ts": "200.2", "user": "U_PM"}])
    assert conversations.in_flight() == []
    assert client.get("/api/conversations").json()["threads"][0]["outcomes"] == ["lost"]


def test_the_in_flight_window_follows_the_bots_timeout(env):
    (S.bridge_dir / ".env").write_text("BRIDGE_TIMEOUT_S=600\n")
    write_turns([{"event": "start", "ts": time.time() - 300, "key": "C1:1.1", "channel": "C1",
                  "thread_ts": "1.1"}])
    assert len(conversations.in_flight()) == 1


def test_a_half_written_last_line_is_skipped(client, env):
    S.turns_file.write_text(json.dumps({"event": "start", "ts": T0, "key": "C1:1.1", "channel": "C1",
                                        "thread_ts": "1.1"}) + "\n" + '{"event": "end", "ts"')
    assert client.get("/api/conversations").status_code == 200


def test_search_and_filters(client, env):
    _thread(env)
    assert len(client.get("/api/conversations?q=CAMERA").json()["threads"]) == 1
    assert client.get("/api/conversations?q=nothing-like-this").json()["threads"] == []
    assert client.get("/api/conversations?outcome=timeout").json()["threads"] == []
    assert client.get("/api/conversations?channel=C2").json()["threads"] == []


def test_a_session_id_cannot_name_a_file_outside_the_transcripts(env):
    assert conversations.transcript_path("../../etc/passwd") is None
    assert conversations.transcript_path("") is None


def test_an_unknown_thread_is_404(client):
    assert client.get("/api/conversations/C9:9.9").status_code == 404


def test_stats_count_questions_cost_time_and_cases(client, env):
    _thread(env)
    s = client.get("/api/stats?days=7").json()
    assert s["questions"] == 2 and s["threads"] == 1
    assert s["cost_usd"] == 0.25
    assert s["cases_read"] == [{"slug": "gateway-bound-to-loopback", "count": 1}]
    assert len(s["per_day"]) == 7 and sum(d["questions"] for d in s["per_day"]) == 2
    assert s["p50_s"] in (10, 40)
