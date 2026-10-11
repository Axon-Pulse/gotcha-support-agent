"""A case's created time, and the sessions that ended up being it."""
import json

import pytest
from fastapi.testclient import TestClient

import graph


@pytest.fixture
def env(tmp_path, monkeypatch):
    import console.server as srv
    kb, traces = tmp_path / "kb", tmp_path / "traces"
    kb.mkdir(), traces.mkdir()
    monkeypatch.setattr(srv, "KB", kb)
    monkeypatch.setattr(srv, "TRACES", traces)
    srv._GIT_ADDED.clear()
    yield TestClient(srv.app), kb, traces
    srv._GIT_ADDED.clear()


def _doc(c, name):
    return next(d for d in c.get("/api/kb").json()["docs"] if d["name"] == name)


def test_created_is_recorded_on_create_and_survives_an_edit(env):
    c, kb, _ = env
    c.post("/api/kb", json={"name": "flap", "topics": ["radar"], "symptoms": [], "body": "# F"})
    first = _doc(c, "flap")
    assert first["created_source"] == "recorded" and first["created"]
    c.put("/api/kb/flap", json={"topics": ["radar", "network"], "symptoms": ["x"],
                                "body": "# F edited"})
    assert _doc(c, "flap")["created"] == first["created"]
    assert "created:" in (kb / "flap.md").read_text()


def test_a_case_from_before_the_field_gets_an_estimate(env):
    c, kb, _ = env
    (kb / "legacy.md").write_text("# Legacy\n")        # not in git: the file's mtime
    d = _doc(c, "legacy")
    assert d["created"] and d["created_source"] == "file"


def test_sessions_count_diagnoses_and_the_creator_not_search_hits(env):
    c, kb, traces = env
    (kb / "flap.md").write_text("# Flap\n")
    (traces / "a.jsonl").write_text(json.dumps({"report": {"matched_case": "flap"}}) + "\n")
    # Two lines of one session name it twice: still one session.
    (traces / "b.jsonl").write_text("\n".join(json.dumps(l) for l in [
        {"status": "awaiting_approval", "report": {"matched_case": "flap"}},
        {"status": "done", "report": {"matched_case": "flap"}}]) + "\n")
    (traces / "creator.jsonl").write_text(json.dumps(
        {"saved": True, "report": {"propose_scenario": {"id": "Flap"}}}) + "\n")
    (traces / "searched.jsonl").write_text(json.dumps({"findings": [
        {"tool": "search_runbook", "data": {"hits": [{"doc": "flap"}]}}]}) + "\n")
    # Proposed but rejected: it did not create the case.
    (traces / "rejected.jsonl").write_text(json.dumps(
        {"saved": False, "report": {"propose_scenario": {"id": "flap"}}}) + "\n")
    d = _doc(c, "flap")
    assert (d["sessions"], d["search_hits"]) == (3, 1)


def test_an_approved_case_is_stamped_whatever_text_it_was_saved_with():
    assert graph._stamp_created("# Plain\n").startswith("---\ncreated: '")
    fm = graph._stamp_created("---\ntopics:\n- radar\n---\n\n# T\n")
    assert fm.startswith("---\ncreated: '") and "topics:\n- radar" in fm
    kept = "---\ncreated: '2026-01-01T00:00:00'\n---\n\n# T\n"
    assert graph._stamp_created(kept) == kept


def test_matched_case_is_optional_in_the_report_schema():
    assert "matched_case" in graph.REPORT_SCHEMA["properties"]
    assert "matched_case" not in graph.REPORT_SCHEMA["required"]
