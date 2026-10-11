"""The health capture must be sized to the rate it samples, and say so when it is not.

A short window on a slow stream returns a partial view. Reported as-is it reads like a
system with one node — which is how a CRITICAL node hid behind a one-sample capture on
the bench, 2026-09-16. See kb/cases/asu-backend-never-reachable.md.
"""
import pytest

import transport
from tools.health import get_system_health

HEALTH = get_system_health.__wrapped__ if hasattr(get_system_health, "__wrapped__") else get_system_health

ONE = """node_id: "c2_gateway"
node_type: "base_node"
overall_status: HEALTHY
common {
  uptime_seconds: 440
}
"""


def _requested() -> int:
    argv = transport.DEFAULT_ALLOWED["health"]
    return int(argv[argv.index("-c") + 1])


def test_requested_count_is_reachable_within_its_timeout():
    """`-c` must bind before the timeout does, or output is cut at a block boundary.

    Rate measured on the bench: ~1.1 health messages/second for 6 nodes.
    """
    want = _requested()
    window = transport.TIMEOUTS["health"]
    assert want / 1.1 < window, (
        f"-c {want} needs ~{want / 1.1:.0f}s at the measured rate but the timeout is "
        f"{window}s, so the capture is always killed mid-stream"
    )


def test_health_timeout_exceeds_the_default():
    assert transport.TIMEOUTS["health"] > transport.DEFAULT_TIMEOUT


def test_truncated_live_capture_is_flagged(monkeypatch):
    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport, "run", lambda key, timeout=None: ONE)
    out = HEALTH()
    assert out["samples"] == 1
    cap = out.get("capture")
    assert cap and cap["complete"] is False
    assert cap["requested"] == _requested()
    assert "absence" in cap["warning"]


def test_complete_live_capture_is_not_flagged(monkeypatch):
    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport, "run", lambda key, timeout=None: ONE * _requested())
    assert "capture" not in HEALTH()


def test_mock_mode_never_flags(monkeypatch):
    """A fixture is whatever it is; completeness is meaningless against it."""
    monkeypatch.setattr(transport, "MODE", "mock")
    monkeypatch.setattr(transport, "run", lambda key, timeout=None: ONE)
    assert "capture" not in HEALTH()


def test_run_uses_the_per_key_timeout(monkeypatch):
    seen = {}

    class R:
        stdout = ""

    def fake(argv, **kw):
        seen["argv"] = argv
        return R()

    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport.subprocess, "run", fake)
    transport.run("health")
    assert seen["argv"][:2] == ["timeout", str(transport.TIMEOUTS["health"])]
    transport.run("topology")
    assert seen["argv"][:2] == ["timeout", str(transport.DEFAULT_TIMEOUT)]
