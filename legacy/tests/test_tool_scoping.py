"""Domain experts see their own domain and nothing else, and placeholders stay honest."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agents as A  # noqa: E402
import graph as G  # noqa: E402
import inventory  # noqa: E402
import transport  # noqa: E402
from registry import REGISTRY, call, load_tools  # noqa: E402

load_tools()
FIXTURES = ROOT / "tests" / "fixtures"

# Tools that read the system as a whole. An expert must not hold one: triage already
# called them and every agent is handed the findings digest.
GENERIC = {"get_system_health", "get_process_table", "get_ecal_topology", "probe_endpoint"}
DOMAIN_TOOL = {
    "acoustic_deep_dive": "get_asu_service_status",
    "radar_deep_dive": "get_radar_status",
    "camera_deep_dive": "get_camera_status",
    "infrastructure_expert": "get_tower_status",
}
PLACEHOLDERS = ["get_radar_status", "get_camera_status", "get_tower_status"]


# ---------- 1. the scoping contract ----------

@pytest.mark.parametrize("agent,tool", sorted(DOMAIN_TOOL.items()))
def test_an_expert_holds_its_own_domain_tool_and_the_runbook_only(agent, tool):
    assert set(A.agents()[agent]["tools"]) == {tool, "search_runbook"}


def test_no_expert_holds_a_generic_system_tool():
    """The health rows are already in the digest; the tool invites a redundant call."""
    for name in A.scope_context():
        leaked = GENERIC & set(A.agents()[name]["tools"])
        assert not leaked, f"{name} can call {sorted(leaked)}"


def test_no_expert_holds_another_domains_tool():
    others = set(DOMAIN_TOOL.values())
    for name, own in DOMAIN_TOOL.items():
        foreign = (others - {own}) & set(A.agents()[name]["tools"])
        assert not foreign, f"{name} can diagnose hardware it is not expert in: {foreign}"


def test_every_scope_gated_agent_is_covered_by_this_file():
    """Adding an expert without scoping it should fail here, not ship."""
    assert set(A.scope_context()) == set(DOMAIN_TOOL)


def test_generic_agents_keep_the_system_tools():
    assert set(A.agents()["triage"]["tools"]) == {"get_system_health", "get_process_table"}
    assert set(A.agents()["network"]["tools"]) == {"probe_endpoint"}
    assert set(A.agents()["topology"]["tools"]) == {"get_ecal_topology"}


def test_an_expert_prompt_never_names_a_tool_it_cannot_call():
    """A prompt telling the model to call a tool it does not have wastes a whole turn."""
    for name in A.scope_context():
        spec = A.agents()[name]
        for t in REGISTRY:
            if t in spec["prompt"] and t not in spec["tools"]:
                pytest.fail(f"{name}'s prompt names {t}, which it cannot call")


def test_the_new_experts_ship_disabled_like_the_others():
    for name in ("camera_deep_dive", "infrastructure_expert"):
        assert name in A.agents()
        assert name not in A.DEFAULT_ORDER
        assert A.requires()[name] == ["triage"]


# ---------- 2. placeholders must not fabricate ----------

@pytest.mark.parametrize("tool", PLACEHOLDERS)
def test_a_placeholder_says_so_and_invents_nothing(tool):
    out = call(tool, {"node": "magos"})
    assert out["ok"] is True
    data = out["data"]
    assert data["implemented"] is False, "the model must not read this as a clean result"
    assert "planned" in data and data["planned"]


@pytest.mark.parametrize("tool", PLACEHOLDERS)
def test_a_placeholder_never_feeds_the_routing_gate(tool):
    """_extract_context reads `nodes` out of tool payloads to decide who may run."""
    data = call(tool, {"node": "magos"})["data"]
    for key in ("nodes", "never_started", "exited_with_error"):
        assert key not in data, f"{tool} would gate real agents on placeholder output"
    assert G._extract_context([{"agent": "x", "tool": tool, "ok": True, "data": data}]) == {}


@pytest.mark.parametrize("tool", PLACEHOLDERS)
def test_a_placeholder_describes_what_it_will_do(tool):
    """The description is what the model reads to decide whether to call it."""
    desc = REGISTRY[tool]["schema"]["description"]
    assert "PLACEHOLDER" in desc and "NOT implemented" in desc
    assert "will" in desc


def test_a_placeholder_refuses_a_node_from_another_domain(tmp_path, monkeypatch):
    f = tmp_path / "reg.yaml"
    f.write_text("version: 1\nsystems:\n  t1:\n    components:\n      cam1:\n"
                 "        type: meduza_optic\n        domain: camera\n"
                 "        address: 192.168.40.71\n")
    monkeypatch.setattr(inventory, "SYSTEMS", f)
    inventory.load.cache_clear(); inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()
    data = call("get_radar_status", {"node": "cam1"})["data"]
    assert data["implemented"] is False and "refused" in data
    assert "camera" in data["refused"]


def test_a_failed_probe_is_a_finding_not_a_broken_tool(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("no route to host")
    monkeypatch.setattr(transport, "run_on", boom)
    out = call("get_radar_status", {"node": "magos"})
    assert out["ok"] is True, "a unit that does not answer is signal, not a crash"
    assert out["data"]["probe"]["ok"] is False
    assert "no route to host" in out["data"]["probe"]["error"]


# ---------- 3. the permission surface exists ----------

@pytest.mark.parametrize("key", ["radar_status", "camera_status", "tower_status"])
def test_each_domain_probe_is_allowlisted_with_a_fixture(key):
    assert key in transport.DEFAULT_ALLOWED
    assert (FIXTURES / transport.FIXTURES[key]).exists()


def test_the_domain_probes_are_read_only():
    for key in ("radar_status", "camera_status", "tower_status"):
        argv = " ".join(transport.DEFAULT_ALLOWED[key])
        assert any(w in argv for w in ("uptime", "curl")), argv
        assert not any(w in argv for w in ("rm", "reboot", "systemctl", "kill")), argv


def test_curls_own_template_is_not_read_as_our_placeholder():
    """%{http_code} belongs to curl. Treating it as ours makes every camera probe raise."""
    filled = transport._fill(list(transport.DEFAULT_ALLOWED["camera_status"]),
                             {"host": "192.168.40.71"})
    assert "%{http_code}" in filled
    assert "http://192.168.40.71/" in filled


def test_a_probe_with_no_login_needs_no_credentials(tmp_path, monkeypatch):
    """A camera with no SSH block must still be probeable over HTTP."""
    f = tmp_path / "reg.yaml"
    f.write_text("version: 1\nsystems:\n  t1:\n    components:\n      cam1:\n"
                 "        type: meduza_optic\n        domain: camera\n"
                 "        address: 192.168.40.71\n")
    monkeypatch.setattr(inventory, "SYSTEMS", f)
    inventory.load.cache_clear(); inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()
    monkeypatch.setattr(inventory, "credentials",
                        lambda n: pytest.fail("resolved a credential for an HTTP probe"))
    seen = {}

    class R:
        stdout = "200"

    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport.subprocess, "run",
                        lambda argv, **k: (seen.update(argv=argv) or R()))
    assert transport.run_on("camera_status", "cam1") == "200"
    assert "http://192.168.40.71/" in seen["argv"]


# ---------- 4. the platform-fault signature ----------

@pytest.fixture
def platform(tmp_path, monkeypatch):
    f = tmp_path / "reg.yaml"
    f.write_text(
        "version: 1\nsystems:\n  tower1:\n    site: s\n    components:\n"
        "      magos:\n        type: magos_radar\n        domain: radar\n"
        "        address: 192.168.40.60\n"
        "      cam1:\n        type: meduza_optic\n        domain: camera\n"
        "        address: 192.168.40.71\n"
        "  tower2:\n    site: s\n    components:\n"
        "      magos2:\n        type: magos_radar\n        domain: radar\n"
        "        address: 192.168.40.62\n")
    monkeypatch.setattr(inventory, "SYSTEMS", f)
    inventory.load.cache_clear(); inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()
    monkeypatch.setattr(A, "order", lambda: A.DEFAULT_ORDER + sorted(A.scope_context()))


def _h(rows):
    return [{"agent": "triage", "tool": "get_system_health", "ok": True,
             "data": {"nodes": rows}}]


def test_two_faults_on_one_platform_is_the_tower_signature(platform):
    ctx = G._extract_context(_h([{"node": "magos", "status": "CRITICAL"},
                                 {"node": "cam1", "status": "DEGRADED"}]))
    assert ctx["infrastructure_targets"] == ["tower1"]
    assert "infrastructure_expert" in G._eligible(["triage"], ctx)


def test_one_fault_alone_is_that_sensor_not_the_tower(platform):
    """kb/tower.md's discriminator: a shared error is the tower, a lone error is a mount."""
    ctx = G._extract_context(_h([{"node": "magos", "status": "CRITICAL"}]))
    assert "infrastructure_targets" not in ctx
    assert "infrastructure_expert" not in G._eligible(["triage"], ctx)


def test_faults_on_two_different_platforms_do_not_combine(platform):
    ctx = G._extract_context(_h([{"node": "magos", "status": "CRITICAL"},
                                 {"node": "magos2", "status": "CRITICAL"}]))
    assert "infrastructure_targets" not in ctx, "one fault each, on two towers"


def test_the_expert_is_not_reported_as_blocked_when_no_platform_is_implicated(platform):
    ctx = G._extract_context(_h([{"node": "magos", "status": "CRITICAL"}]))
    names = [b["agent"] for b in G._blocked(["triage", "knowledge", "topology", "network",
                                             "radar_deep_dive"], ctx)]
    assert "infrastructure_expert" not in names
