"""The approval gate, driven end to end with the model stubbed out."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import graph as G  # noqa: E402

SCENARIO = {"id": "test-scenario", "title": "Test Scenario",
            "symptoms": ["sensor quiet"], "root_cause": "backend down",
            "checks": ["docker ps"], "fix": "start it"}


def _stub(monkeypatch, propose=True):
    """Skip every agent; jump straight to a synthesized report."""
    monkeypatch.setattr(G, "supervisor", lambda s: {"next": "synthesize"})
    rep = {"root_cause": "backend down", "confidence": "high", "evidence": ["x"],
           "suggested_actions": [], "escalate": False, "unknowns": []}
    if propose:
        rep["propose_scenario"] = SCENARIO
    monkeypatch.setattr(G.llm, "ask_json", lambda **kw: rep)
    # customer_communicator now sits between synthesize and the gate.
    monkeypatch.setattr(G.llm, "run_text_agent",
                        lambda *a, **k: ("A plain client update.",
                                         {"input": 0, "output": 0, "cache_read": 0}))


def _run(tmp_path, monkeypatch, decision, propose=True):
    monkeypatch.setattr(G, "KB_DIR", tmp_path)
    _stub(monkeypatch, propose)
    g = G.build()
    with SqliteSaver.from_conn_string(str(tmp_path / "t.db")) as cp:
        app = g.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": "t"}}
        out = app.invoke({"question": "q", "session_id": "t",
                          "findings": [], "visited": [], "transcript": []}, cfg)
        if "__interrupt__" in out and decision is not None:
            out = app.invoke(Command(resume=decision), cfg)
    return out, tmp_path


def test_approval_is_required_before_any_write(tmp_path, monkeypatch):
    out, kb = _run(tmp_path, monkeypatch, decision=None)
    assert "__interrupt__" in out, "graph must stop and wait"
    assert list(kb.glob("*.md")) == [], "nothing may be written before approval"


def test_rejection_writes_nothing(tmp_path, monkeypatch):
    out, kb = _run(tmp_path, monkeypatch, decision={"approved": False})
    assert not out.get("saved")
    assert list(kb.glob("*.md")) == []


def test_approval_writes_the_file(tmp_path, monkeypatch):
    out, kb = _run(tmp_path, monkeypatch, decision={"approved": True})
    assert out.get("saved") is True
    written = list(kb.glob("*.md"))
    assert [p.name for p in written] == ["test-scenario.md"]
    assert "backend down" in written[0].read_text()


def test_edited_text_is_what_gets_saved(tmp_path, monkeypatch):
    edited = dict(SCENARIO, _md="# Edited by the operator\n")
    out, kb = _run(tmp_path, monkeypatch,
                   decision={"approved": True, "edited": edited})
    text = (kb / "test-scenario.md").read_text()
    # The operator's text, verbatim, under the creation stamp the console reads back.
    assert text.endswith("\n---\n\n# Edited by the operator\n")
    assert text.startswith("---\ncreated: '")


def test_no_proposal_means_no_interrupt(tmp_path, monkeypatch):
    out, kb = _run(tmp_path, monkeypatch, decision=None, propose=False)
    assert "__interrupt__" not in out
    assert list(kb.glob("*.md")) == []


def test_scenario_id_cannot_escape_the_kb_dir(tmp_path, monkeypatch):
    evil = dict(SCENARIO, id="../../etc/pwned")
    monkeypatch.setattr(G, "KB_DIR", tmp_path)
    _stub(monkeypatch)
    monkeypatch.setattr(G.llm, "ask_json", lambda **kw: {
        "root_cause": "x", "confidence": "high", "evidence": [], "suggested_actions": [],
        "escalate": False, "unknowns": [], "propose_scenario": evil})
    monkeypatch.setattr(G.llm, "run_text_agent",
                        lambda *a, **k: ("x", {"input": 0, "output": 0, "cache_read": 0}))
    g = G.build()
    with SqliteSaver.from_conn_string(str(tmp_path / "t.db")) as cp:
        app = g.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": "t"}}
        app.invoke({"question": "q", "session_id": "t",
                    "findings": [], "visited": [], "transcript": []}, cfg)
        app.invoke(Command(resume={"approved": True}), cfg)
    assert all(p.parent == tmp_path for p in tmp_path.rglob("*.md"))
