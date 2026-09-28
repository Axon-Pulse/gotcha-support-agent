"""Trace summaries are drawn from recorded fields only; logs keep one value per line."""
import trace_summary as ts

REPORT = {"bottom_line": "magos is down; restart it.", "root_cause": "magos exited",
          "confidence": "high", "evidence": ["magos_node: no process"],
          "suggested_actions": ["check the pid"], "escalate": True,
          "escalate_reason": "needs a restart", "unknowns": ["why it exited"]}


def test_single_run_summary():
    s = ts.summarize([{"question": "no tracks", "report": REPORT, "findings": [1]}])
    assert s["problem"] == "no tracks" and s["symptoms"] == ["magos_node: no process"]
    assert s["diagnosis"] == "magos is down; restart it." and s["found"]
    assert s["next_steps"] == ["check the pid"] and s["escalate"]
    # A read-only check is not a fix.
    assert s["solution"] == ""


def test_proposed_runbook_fix_is_the_solution():
    r = {**REPORT, "propose_scenario": {"fix": "restart magos", "symptoms": ["x"]}}
    assert ts.summarize([{"report": r}])["solution"] == "restart magos"


def test_nothing_recorded_stays_empty():
    s = ts.summarize([{"findings": []}])
    assert (s["problem"], s["diagnosis"], s["symptoms"], s["found"]) == ("", "", [], False)


def test_conversation_uses_latest_report_and_lists_follow_ups():
    s = ts.summarize([
        {"conversation": "c", "question": "first", "report": {"root_cause": "old"}},
        {"conversation": "c", "question": "and now?", "answer": "still old"},
        {"conversation": "c", "question": "again", "report": REPORT}])
    assert s["via"] == "chat" and s["problem"] == "first"
    assert s["follow_ups"] == ["and now?", "again"]
    assert s["root_cause"] == "magos exited" and s["last_answer"] == "still old"


def test_bridge_question_comes_from_origin():
    s = ts.summarize([{"origin": {"via": "bridge", "question": "no signal"}, "report": {}}])
    assert (s["via"], s["problem"]) == ("bridge", "no signal")


def test_sections_are_labelled_and_explained():
    secs = ts.sections([{"report": REPORT, "findings": [1], "usage": {"input": 1},
                         "saved": True}])
    by = {x["key"]: x for x in secs}
    assert set(by) == {"report", "findings", "usage", "_other"}
    assert all(x["why"] for x in secs)
    assert by["_other"]["value"] == {"saved": True}


def test_multi_line_sections_keep_line_numbers():
    secs = ts.sections([{"findings": [1]}, {"totals": {"input": 2}}, {"findings": [3]}])
    by = {x["key"]: x["value"] for x in secs}
    assert by["findings"] == [{"line": 1, "value": [1]}, {"line": 3, "value": [3]}]
    assert by["usage"] == [{"line": 2, "value": {"totals": {"input": 2}}}]


def test_status_is_the_last_recorded_one():
    assert ts.status([{"status": "running"}]) == "running"
    assert ts.status([{"status": "running"}, {"status": "error", "error": "x"}]) == "error"
    s = ts.summarize([{"status": "running", "question": "q"},
                      {"status": "awaiting_approval", "report": REPORT},
                      {"status": "done", "report": REPORT}])
    assert s["finished"] and s["status_label"] == "finished"


def test_unfinished_statuses_are_not_finished():
    for st in ("running", "pending", "in_progress", "awaiting_approval", "error", "failed",
               "cancelled"):
        assert not ts.summarize([{"status": st}])["finished"], st


def test_old_traces_without_status():
    assert ts.status([{"report": REPORT}]) == "done"
    assert ts.status([{"findings": []}]) == "running"


def test_conversations_are_finished_once_a_turn_answered():
    assert ts.status([{"conversation": "c", "kind": "error"},
                      {"conversation": "c", "kind": "follow_up"}]) == "done"
    assert ts.status([{"conversation": "c", "kind": "error"}]) == "error"


def test_the_error_reaches_the_summary():
    s = ts.summarize([{"status": "running"}, {"status": "error", "error": "Boom: no db"}])
    assert s["error"] == "Boom: no db" and s["status_label"] == "failed"
