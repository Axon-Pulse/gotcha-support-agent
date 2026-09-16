"""Editable config: validation, propagation, and the invariants that must survive it."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store as C  # noqa: E402


# Isolation of config_store.PATH / AUDIT is handled for every test by tests/conftest.py.


def test_override_layers_over_default_and_reverts():
    import agents
    assert agents.agents()["triage"]["prompt"] == agents.DEFAULT_AGENTS["triage"]["prompt"]
    C.put("agents", {"solo": {"prompt": "p", "tools": ["search_runbook"]}})
    assert list(agents.agents()) == ["solo"]
    C.clear("agents")
    assert list(agents.agents()) == list(agents.DEFAULT_AGENTS)


def test_command_edits_reach_the_transport():
    import transport
    assert "topology" in transport.allowed()
    C.put("allowed_commands", {"only": ["echo", "hi"]})
    assert list(transport.allowed()) == ["only"]
    with pytest.raises(KeyError):
        transport.run("topology")          # no longer allowlisted


def test_tool_description_override_reaches_the_model_schema():
    from registry import load_tools, schemas
    load_tools()
    C.put("tool_descriptions", {"search_runbook": "CUSTOM"})
    got = [s for s in schemas(["search_runbook"])][0]["description"]
    assert got == "CUSTOM"
    C.put("tool_descriptions", {})
    assert schemas(["search_runbook"])[0]["description"] != "CUSTOM"


def test_every_write_is_audited():
    C.put("supervisor_picks", False, note="turn routing off")
    C.clear("supervisor_picks")
    rows = [json.loads(l) for l in C.AUDIT.read_text().splitlines() if l.strip()]
    assert [r["change"] for r in rows] == ["turn routing off", "reset supervisor_picks"]


@pytest.mark.parametrize("agents_cfg,msg", [
    ({}, "at least one agent"),
    ({"a": {"prompt": "", "tools": ["search_runbook"]}}, "needs a prompt"),
    ({"a": {"prompt": "p", "tools": []}}, "no tools"),
    ({"a": {"prompt": "p", "tools": ["ghost"]}}, "unregistered"),
    ({"save": {"prompt": "p", "tools": ["search_runbook"]}}, "reserved"),
    ({"Bad Name": {"prompt": "p", "tools": ["search_runbook"]}}, "lowercase"),
])
def test_agent_validation_rejects(agents_cfg, msg):
    with pytest.raises(C.ConfigError, match=msg):
        C.validate_agents(agents_cfg, {"search_runbook"})


@pytest.mark.parametrize("order,req,msg", [
    (["a"], {"a": ["a"]}, "cannot require itself"),
    (["a", "b"], {"a": ["b"], "b": ["a"]}, "cycle"),
    (["b", "a"], {"b": ["a"]}, "runs before it"),
    (["ghost"], {}, "undefined agents"),
    (["a", "a"], {}, "duplicates"),
])
def test_flow_validation_rejects(order, req, msg):
    with pytest.raises(C.ConfigError, match=msg):
        C.validate_flow(order, req, {"a", "b"})


def test_shell_binaries_warn_but_argv_stays_a_list():
    assert C.validate_commands({"ok": ["ecal_mon_cli", "-l"]}) == []
    assert "arbitrary commands" in C.validate_commands({"x": ["bash", "-c", "id"]})[0]
    with pytest.raises(C.ConfigError):
        C.validate_commands({"x": "rm -rf /"})       # string, not argv list


def test_agent_still_cannot_widen_its_own_permissions():
    """The whole point: config is editable by a human, never by the model."""
    from registry import REGISTRY, load_tools
    load_tools()
    for name, entry in REGISTRY.items():
        src = Path(entry["fn"].__code__.co_filename).read_text()
        assert "config_store" not in src, f"tool {name} can reach the config store"
        assert entry["side_effect"] == "none"
