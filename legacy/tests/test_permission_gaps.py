"""A refused check is not a failed check, and the report has to ask for it.

The distinction the code turns on: "the thing I looked at is broken" is evidence, and
"I was not allowed to look" is a gap somebody can close by granting something. Only the
second belongs in needs_permission, and it is read off what the tools actually returned
rather than asked of the model — which permission would have helped is a fact about the
run, and a model asked to remember it across a long transcript sometimes will not.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import graph as G  # noqa: E402


def finding(tool, error, agent="triage", ok=False):
    return {"agent": agent, "tool": tool, "ok": ok, "data": {"error": error}}


# ---------- what counts as a refusal ----------

@pytest.mark.parametrize("error,what,why_contains", [
    ("KeyError: command 'radar_status' is not allowlisted: ['health']",
     "radar_status", "transport.ALLOWED"),
    ("KeyError: unknown node 'dumbo9'; known nodes: ['dumbo1']",
     "dumbo9", "inventory"),
    ("KeyError: 'tower1' has no access block in systems_inventory.yaml",
     "tower1", "SSH"),
    ("KeyError: 'camera2' has no address; cannot reach it",
     "camera2", "address"),
])
def test_a_refusal_is_recognised_and_named(error, what, why_contains):
    got = G.permission_gaps([finding("get_radar_status", error)])
    assert len(got) == 1, f"not recognised as a refusal: {error}"
    assert got[0]["what"] == what
    assert why_contains.lower() in got[0]["why"].lower()
    assert got[0]["tool"] == "get_radar_status"


def test_a_broken_device_is_not_a_permission_problem():
    """The distinction the whole feature rests on."""
    noise = [
        finding("get_system_health", "TimeoutError: command timed out after 30s"),
        finding("probe_endpoint", "ConnectionRefusedError: [Errno 111]"),
        finding("get_asu_service_status", "ValueError: could not parse docker output"),
    ]
    assert G.permission_gaps(noise) == [], \
        "asked for a permission that would not have helped"


def test_a_successful_call_is_never_a_gap():
    ok = {"agent": "triage", "tool": "get_system_health", "ok": True,
          "data": {"error": "unknown node 'x'"}}   # error-shaped text in a GOOD result
    assert G.permission_gaps([ok]) == []


def test_the_same_refusal_twice_is_asked_for_once():
    """Three agents hitting one missing command should not produce three asks."""
    err = "KeyError: command 'radar_status' is not allowlisted: ['health']"
    got = G.permission_gaps([finding("get_radar_status", err, agent=a)
                             for a in ("triage", "radar_deep_dive", "network")])
    assert len(got) == 1


def test_different_refusals_are_all_listed():
    got = G.permission_gaps([
        finding("get_radar_status", "command 'radar_status' is not allowlisted: []"),
        finding("probe_endpoint", "unknown node 'dumbo9'; known nodes: []", agent="network"),
    ])
    assert {g["what"] for g in got} == {"radar_status", "dumbo9"}


def test_findings_without_an_error_field_do_not_crash():
    assert G.permission_gaps([{"agent": "a", "tool": "t", "ok": False}]) == []
    assert G.permission_gaps([]) == []
    assert G.permission_gaps(None) == []


# ---------- how it reaches the report ----------

def _synth(monkeypatch, findings, model_report):
    seen = {}
    monkeypatch.setattr(G.llm, "ask_json",
                        lambda **kw: seen.update(kw) or dict(model_report))
    out = G.synthesize({"question": "no tracks", "findings": findings,
                        "transcript": [], "blocked": []})
    return out["report"], seen


BASE = {"bottom_line": "b", "root_cause": "r", "confidence": "low", "evidence": [],
        "suggested_actions": [], "escalate": False, "unknowns": []}


def test_a_refusal_reaches_the_report_even_if_the_model_forgets(monkeypatch):
    """The merge exists because a dropped ask looks like a complete report."""
    err = "KeyError: command 'radar_status' is not allowlisted: ['health']"
    report, _ = _synth(monkeypatch, [finding("get_radar_status", err)], BASE)
    assert [p["what"] for p in report["needs_permission"]] == ["radar_status"]
    assert report["needs_permission"][0]["why"]


def test_the_model_is_told_what_it_was_refused(monkeypatch):
    err = "KeyError: command 'radar_status' is not allowlisted: ['health']"
    _, seen = _synth(monkeypatch, [finding("get_radar_status", err)], BASE)
    assert "REFUSED" in seen["prompt"]
    assert "radar_status" in seen["prompt"], \
        "synthesised a diagnosis without being told which check was refused"


def test_the_models_own_asks_are_kept_alongside(monkeypatch):
    err = "KeyError: unknown node 'dumbo9'; known nodes: []"
    model = {**BASE, "needs_permission": [
        {"what": "get_ecal_topology on dumbo9", "why": "would show the publisher"}]}
    report, _ = _synth(monkeypatch, [finding("probe_endpoint", err)], model)
    whats = [p["what"] for p in report["needs_permission"]]
    assert "get_ecal_topology on dumbo9" in whats, "dropped what the model asked for"
    assert "dumbo9" in whats, "dropped the refusal that actually happened"


def test_the_same_thing_is_not_asked_for_twice(monkeypatch):
    err = "KeyError: command 'radar_status' is not allowlisted: []"
    model = {**BASE, "needs_permission": [{"what": "radar_status", "why": "would confirm"}]}
    report, _ = _synth(monkeypatch, [finding("get_radar_status", err)], model)
    assert len(report["needs_permission"]) == 1
    assert report["needs_permission"][0]["why"] == "would confirm", \
        "the model's own reason should win over the generic one"


def test_a_clean_run_asks_for_nothing(monkeypatch):
    report, _ = _synth(monkeypatch, [{"agent": "triage", "tool": "get_system_health",
                                      "ok": True, "data": {}}], BASE)
    assert not report.get("needs_permission"), \
        "a run that was never blocked must not nag about permissions"


def test_needs_permission_is_optional_in_the_schema():
    """Most runs are not blocked; requiring the field would make every report carry it."""
    assert "needs_permission" not in G.REPORT_SCHEMA["required"]
    assert "needs_permission" in G.REPORT_SCHEMA["properties"]
