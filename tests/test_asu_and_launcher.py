"""The ASU backend tool, the launcher fixture, and the guard that keeps mock mode whole."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import transport  # noqa: E402
from registry import REGISTRY, call, load_tools  # noqa: E402
from tools import asu  # noqa: E402
from tools.process import _redact  # noqa: E402

load_tools()
FIXTURES = ROOT / "tests" / "fixtures"

RUNNING = "dumbo-backend\trunning\tUp 7 hours\tdumbo/backend:2.4.1"
EXITED = "dumbo-backend\texited\tExited (137) 7 hours ago\tdumbo/backend:2.4.1"
FRONTEND_ONLY = "dumbo-frontend\trunning\tUp 7 hours\tdumbo/frontend:2.4.1"


def _asu(monkeypatch, container: str, api: str) -> dict:
    monkeypatch.setattr(asu.transport, "run",
                        lambda key, **kw: {"asu_container": container, "asu_api": api}[key])
    return asu.get_asu_service_status()


# ---------- 1. mock mode must be whole ----------

def test_every_allowlisted_command_has_a_fixture_that_exists():
    """The guard for the bug this fixture set was missing.

    `launcher` had a FIXTURES entry pointing at a file nobody had recorded, so
    get_process_table failed on every mock run — half of triage's evidence, silently
    gone. A mapping is not a fixture; assert the bytes are on disk.
    """
    missing = [k for k in transport.DEFAULT_ALLOWED
               if not (FIXTURES / transport.FIXTURES.get(k, f"{k}.txt")).exists()]
    assert missing == [], f"allowlisted commands with no fixture: {missing}"


@pytest.mark.parametrize("name", sorted(
    n for n, t in REGISTRY.items()
    if not t["schema"]["input_schema"].get("required")
    and n != "search_runbook"))
def test_every_zero_argument_tool_succeeds_in_mock_mode(name):
    out = call(name, {})
    assert out["ok"] is True, f"{name} failed in mock mode: {out.get('error')}"


# ---------- 2. the launcher fixture ----------

def test_process_table_parses_the_launcher_fixture():
    d = call("get_process_table", {})["data"]
    assert d["launcher_state"] == "RUNNING"
    assert d["config_path"] == "configs/two_sensor_test.yaml"
    assert {r["name"] for r in d["nodes"]} == {"magos", "asu", "tracker", "event_manager"}
    assert d["never_started"] == [] and d["exited_with_error"] == []


def test_the_fixture_shows_the_duplicate_session():
    """Two launchers are running: 8 process rows against a configured total of 4.

    This is the same fault get_ecal_topology sees as two disjoint PID groups. The
    fixtures are one recorded system, so the two tools must agree.
    """
    d = call("get_process_table", {})["data"]
    assert d["total"] == 4
    assert len(d["nodes"]) == 8, "one row per node per launcher session"
    pids = {r["name"]: sorted(x["pid"] for x in d["nodes"] if x["name"] == r["name"])
            for r in d["nodes"]}
    assert all(len(v) == 2 for v in pids.values())

    topo = call("get_ecal_topology", {})["data"]["duplicate_instances"]
    assert topo["detected"] is True
    topo_pids = {p for g in topo["groups"] for p in g["pids"]}
    assert {str(p) for v in pids.values() for p in v} <= topo_pids


def test_redact_strips_credentials_from_a_command_line():
    assert "hunter2" not in _redact("./node --password hunter2 --ip 10.0.0.1")
    assert "s3cr3t" not in _redact("./node --api-key=s3cr3t")
    assert "joe:pw" not in _redact("./node --url https://joe:pw@host/api")
    assert "--ip 10.0.0.1" in _redact("./node --password hunter2 --ip 10.0.0.1")


# ---------- 3. the ASU backend tool ----------

def test_exited_container_is_the_recorded_fault():
    d = call("get_asu_service_status", {})["data"]
    assert d["verdict"] == "backend_container_not_running"
    assert d["backend"]["exit_code"] == 137
    assert "SIGKILL" in d["note"]


@pytest.mark.parametrize("container,api,verdict", [
    (FRONTEND_ONLY, "000", "backend_container_absent"),
    ("", "000", "backend_container_absent"),
    (EXITED, "000", "backend_container_not_running"),
    (RUNNING, "000", "backend_running_api_unreachable"),
    (RUNNING, "", "backend_running_api_unreachable"),
    (RUNNING, "503", "backend_running_api_erroring"),
    (RUNNING, "404", "backend_running_api_erroring"),
    (RUNNING, "200", "backend_up"),
])
def test_verdicts(monkeypatch, container, api, verdict):
    assert _asu(monkeypatch, container, api)["verdict"] == verdict


def test_absent_and_exited_are_different_faults(monkeypatch):
    """`docker ps -a` is load-bearing: without -a these two collapse into one."""
    absent = _asu(monkeypatch, FRONTEND_ONLY, "000")
    exited = _asu(monkeypatch, EXITED, "000")
    assert absent["backend"] is None
    assert exited["backend"]["exit_code"] == 137
    assert absent["verdict"] != exited["verdict"]
    assert "-a" in transport.DEFAULT_ALLOWED["asu_container"]


def test_probe_url_agrees_with_the_configured_base_url(monkeypatch):
    """A silent disagreement here would make every other verdict meaningless."""
    assert "config_mismatch" not in _asu(monkeypatch, RUNNING, "200")


def test_config_mismatch_is_reported(monkeypatch):
    monkeypatch.setattr(asu, "_configured_url", lambda: "http://localhost:9999/")
    out = _asu(monkeypatch, RUNNING, "200")
    assert "config_mismatch" in out
    assert "9999" in out["config_mismatch"]


def test_tool_is_read_only_and_registered():
    assert REGISTRY["get_asu_service_status"]["side_effect"] == "none"
    assert REGISTRY["get_asu_service_status"]["schema"]["input_schema"]["properties"] == {}
