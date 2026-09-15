"""Strict prerequisites, the customer_communicator node, and the Slack adapter."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agents as A  # noqa: E402
import graph as G  # noqa: E402


# ---------- 1. prerequisites ----------

def test_network_blocked_until_triage_produces_targets():
    assert "network" not in G._eligible([], {})
    assert "network" not in G._eligible(["triage"], {}), "ran but produced nothing"
    assert "network" not in G._eligible(["triage"], {"probe_targets": []}), "empty is not satisfied"
    assert "network" in G._eligible(["triage"], {"probe_targets": ["magos"]})


def test_ordering_prereq_is_independent_of_context():
    assert "topology" not in G._eligible([], {})
    assert "topology" in G._eligible(["triage"], {})


def test_blocked_agents_are_reported_with_a_reason():
    blocked = G._blocked(["triage", "knowledge", "topology"], {})
    assert [b["agent"] for b in blocked] == ["network"]
    assert blocked[0]["missing"] == ["probe_targets"]
    assert "triage did not produce" in blocked[0]["note"]


def test_context_comes_from_tool_payloads_not_prose():
    findings = [{"agent": "triage", "tool": "get_system_health", "ok": True, "data": {
        "nodes": [{"node": "asu", "status": "CRITICAL"}, {"node": "magos", "status": "HEALTHY"}]}}]
    ctx = G._extract_context(findings)
    assert ctx["suspect_nodes"] == ["asu"]
    assert "magos" in ctx["observed_nodes"]
    # A failed tool contributes nothing, so a dependant stays gated.
    assert G._extract_context([{"ok": False, "data": {"nodes": [{"node": "asu"}]}}]) == {}


def test_only_addressable_nodes_become_probe_targets():
    # tracker has no ip/base_url in the config, so it is not probeable.
    ctx = G._extract_context([{"ok": True, "data": {
        "nodes": [{"node": "tracker", "status": "CRITICAL"}]}}])
    assert ctx.get("probe_targets", []) == []
    ctx = G._extract_context([{"ok": True, "data": {
        "nodes": [{"node": "magos", "status": "DEGRADED"}]}}])
    assert ctx["probe_targets"] == ["magos"]


# ---------- 2. customer_communicator ----------

REPORT = {"root_cause": "The acoustic backend was unreachable", "confidence": "high",
          "escalate": False, "evidence": ["asu_connected=false"], "unknowns": [],
          "suggested_actions": ["restart dumbo-backend"]}


def _comm(monkeypatch, text, report=REPORT):
    monkeypatch.setattr(G.llm, "run_text_agent",
                        lambda *a, **k: (text, {"input": 1, "output": 1, "cache_read": 0}))
    return G.customer_communicator({"question": "no tracks", "report": report})


def test_clean_draft_is_sendable(monkeypatch):
    out = _comm(monkeypatch, "Your acoustic sensor could not reach a service it relies on. "
                             "We have restored it and are monitoring.")
    assert out["customer_message"]["safe_to_send"] is True
    assert out["customer_message"]["leaks"] == []


@pytest.mark.parametrize("draft,kind", [
    ("We saw errors from 192.168.40.60 overnight.", "IP address"),
    ("The asu node reported a fault.", "internal node name"),
    ("Status was CRITICAL for 20 minutes.", "internal status field"),
    ("Run docker ps to confirm the container.", "internal command or component"),
])
def test_leaked_internals_block_sending(monkeypatch, draft, kind):
    out = _comm(monkeypatch, draft)
    msg = out["customer_message"]
    assert msg["safe_to_send"] is False
    assert kind in [x["kind"] for x in msg["leaks"]]


def test_escalating_report_is_never_sent_as_an_answer(monkeypatch):
    esc = {**REPORT, "escalate": True, "confidence": "low", "root_cause": None}
    out = _comm(monkeypatch, "We are still looking into this and will update you.", esc)
    assert out["customer_message"]["safe_to_send"] is False
    assert "escalates" in out["customer_message"]["review_reason"]


def test_communicator_runs_after_synthesize_and_before_the_gate():
    g = G.build()
    assert "customer_communicator" in g.nodes
    edges = set(g.edges)
    assert ("synthesize", "customer_communicator") in edges
    assert ("customer_communicator", "propose_scenario") in edges
    assert ("synthesize", "propose_scenario") not in edges


def test_communicator_is_not_in_the_supervisor_pool():
    """It transforms the report; it must never be routed to as an evidence gatherer."""
    assert "customer_communicator" not in A.agents()
    assert "customer_communicator" not in A.order()
    assert "customer_communicator" in A.post_agents()


# ---------- 3. slack adapter ----------

def test_slack_is_not_a_graph_node():
    g = G.build()
    assert not [n for n in g.nodes if "slack" in n.lower()]
    for f in (ROOT / "graph.py", ROOT / "agents.py", ROOT / "llm.py"):
        assert "slack" not in f.read_text().lower(), f"{f.name} must not know about Slack"


def test_signature_verification_and_dedup():
    import hashlib
    import hmac
    import time

    import slack_app as S
    S.SIGNING_SECRET = "shh"
    ts, body = str(int(time.time())), b'{"ok":1}'
    sig = "v0=" + hmac.new(b"shh", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert S.verify(body, ts, sig)
    assert not S.verify(body + b"x", ts, sig)                 # tampered
    assert not S.verify(body, str(int(time.time()) - 10**6), sig)   # replayed
    S._seen.clear()
    assert (S.already_handled("E9"), S.already_handled("E9")) == (False, True)
