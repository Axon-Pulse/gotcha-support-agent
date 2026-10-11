"""The console <-> chat bridge: queue, report shape, and the audit trail."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402

GOOD = {"root_cause": "The dumbo-backend container was not running", "confidence": "high",
        "evidence": ["asu_connected=false", "request_count 26654, success_count 0"],
        "suggested_actions": ["docker ps -a --filter name=dumbo"],
        "escalate": False, "unknowns": []}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(bridge, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(bridge, "TRACES", tmp_path / "traces")


@pytest.fixture
def client():
    import console.server as srv
    return TestClient(srv.app)


# ---------- the round trip ----------

def test_a_ticket_goes_out_and_a_report_comes_back(client):
    sid = client.post("/api/bridge/ticket",
                      json={"question": "no tracks"}).json()["id"]
    assert client.get(f"/api/bridge/session/{sid}").json()["status"] == "pending"
    bridge.claim(sid)
    assert client.get(f"/api/bridge/session/{sid}").json()["status"] == "in_progress"
    bridge.publish(sid, GOOD)
    got = client.get(f"/api/bridge/session/{sid}").json()
    assert got["status"] == "done"
    assert got["report"]["root_cause"] == GOOD["root_cause"]


def test_an_empty_ticket_is_refused(client):
    assert client.post("/api/bridge/ticket", json={"question": "   "}).status_code == 400


def test_an_unknown_session_is_a_404(client):
    assert client.get("/api/bridge/session/nope").status_code == 404


def test_the_queue_counts_what_is_outstanding(client):
    for q in ("a", "b"):
        client.post("/api/bridge/ticket", json={"question": q})
    assert client.get("/api/bridge/queue").json()["pending"] == 2
    bridge.claim(bridge.pending()[0]["id"])
    q = client.get("/api/bridge/queue").json()
    assert (q["pending"], q["working"]) == (1, 1)


def test_pending_is_oldest_first_and_excludes_claimed():
    a, b = bridge.submit("first"), bridge.submit("second")
    assert [s["id"] for s in bridge.pending()] == [a, b]
    bridge.claim(a)
    assert [s["id"] for s in bridge.pending()] == [b]


# ---------- the report is held to the pipeline's shape ----------

@pytest.mark.parametrize("bad,msg", [
    ({k: v for k, v in GOOD.items() if k != "unknowns"}, "missing required"),
    ({**GOOD, "confidence": "quite sure"}, "low, medium or high"),
    ({**GOOD, "evidence": "a string"}, "evidence must be a list"),
    ({**GOOD, "escalate": "no"}, "escalate must be true or false"),
])
def test_a_malformed_report_is_refused(bad, msg):
    """`unknowns` and `escalate` are what a confident wrong answer leaves out."""
    sid = bridge.submit("q")
    with pytest.raises(bridge.BridgeError, match=msg):
        bridge.publish(sid, bad)
    assert bridge.status(sid)["status"] == "pending", "a refused publish changes nothing"


def test_the_shape_is_the_pipelines_own(client):
    """If REPORT_SCHEMA grows a required field, the bridge must demand it too."""
    import graph
    sid = bridge.submit("q")
    for field in graph.REPORT_SCHEMA["required"]:
        with pytest.raises(bridge.BridgeError):
            bridge.publish(sid, {k: v for k, v in GOOD.items() if k != field})


def test_a_failed_session_needs_no_report():
    sid = bridge.submit("q")
    bridge.publish(sid, {}, status_="failed", notes="the host was unreachable")
    assert bridge.status(sid)["status"] == "failed"


# ---------- audit ----------

def test_every_session_leaves_a_trace(tmp_path):
    sid = bridge.submit("no tracks")
    bridge.publish(sid, GOOD,
                   commands=[{"cmd": "docker ps -a", "readonly": True, "output": "..."}],
                   kb_gaps=["exit code 137 semantics are not documented"])
    row = json.loads((tmp_path / "traces" / f"{sid}.jsonl").read_text().strip())
    assert row["origin"]["via"] == "bridge"
    assert row["origin"]["question"] == "no tracks"
    assert row["commands"][0]["cmd"] == "docker ps -a"
    assert row["kb_gaps"] == ["exit code 137 semantics are not documented"]


def test_documentation_gaps_survive_to_the_console(client):
    """The gap log is the point of the exercise — it must reach the operator, not the
    chat transcript."""
    sid = bridge.submit("q")
    bridge.publish(sid, GOOD, kb_gaps=["OFFLINE timeout value is not in the KB"])
    assert client.get(f"/api/bridge/session/{sid}").json()["kb_gaps"] == \
        ["OFFLINE timeout value is not in the KB"]


def test_commands_record_whether_they_changed_anything():
    sid = bridge.submit("q")
    bridge.publish(sid, GOOD, commands=[
        {"cmd": "ping -c 3 192.168.40.50", "readonly": True, "output": "3 received"},
        {"cmd": "docker start dumbo-backend", "readonly": False, "output": "started"}])
    cmds = bridge.status(sid)["commands"]
    assert [c["readonly"] for c in cmds] == [True, False]


# ---------- it is not the pipeline ----------

def test_the_bridge_never_runs_the_graph():
    """Live with chat bypasses LangGraph entirely; Run is the tab that exercises it."""
    src = (ROOT / "bridge.py").read_text()
    assert "invoke(" not in src and "SqliteSaver" not in src
    assert "from graph import REPORT_SCHEMA" in src, "only the report shape is shared"


def test_the_page_wires_the_tab():
    html = (ROOT / "console" / "index.html").read_text()
    for hook in ('data-t="chat"', "loadChat", "submitChat", "openChat",
                 "/bridge/ticket", "/bridge/queue"):
        assert hook in html, f"missing {hook}"
    nav = html[html.index('<nav id="tabs">'):html.index("</nav>")]
    import re
    assert re.findall(r'data-t="(\w+)"', nav)[:2] == ["run", "chat"]
