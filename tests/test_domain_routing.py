"""Domain-expert routing: the gate that decides WHICH hardware specialist may run.

The whole point of this file is to prove the routing is correct with the model stubbed
out entirely. Not one test here needs an API key: eligibility is a pure function of
facts extracted from structured tool payloads, so it can be pinned down exactly.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agents as A  # noqa: E402
import graph as G  # noqa: E402
import inventory  # noqa: E402
from registry import call, load_tools  # noqa: E402

load_tools()

# From DEFAULT_AGENTS, not the effective config: this is module-level, so it runs before
# any fixture can isolate overrides.json — reading the live config here meant an agent
# deleted in the console silently emptied the list and the assertions passed vacuously.
EXPERTS = sorted(n for n, spec in A.DEFAULT_AGENTS.items() if spec.get("scope_context"))


@pytest.fixture
def enabled(monkeypatch):
    """Put the domain experts into the running order, as a console edit would.

    They ship out of DEFAULT_ORDER (staged rollout), so every gate test has to enable
    them explicitly — which is itself the proof that shipping them is inert.
    """
    monkeypatch.setattr(A, "order", lambda: A.DEFAULT_ORDER + EXPERTS)


def _health(rows: list[dict]) -> list[dict]:
    return [{"agent": "triage", "tool": "get_system_health", "ok": True,
             "data": {"nodes": rows}}]


# ---------- 1. the vocabulary trap ----------

def test_domain_map_covers_both_vocabularies():
    """The regression guard: a new node type must fail here, not route to nothing."""
    spellings = set().union(*G.DOMAINS.values())
    health_type = {r["node"]: r["type"] for r in call("get_system_health", {})["data"]["nodes"]}
    inv = inventory.load()

    for node, domain in (("asu", "acoustic"), ("magos", "radar")):
        assert inv[node]["type"] in G.DOMAINS[domain], f"{node}: config spelling missing"
        assert health_type[node] in G.DOMAINS[domain], f"{node}: health spelling missing"
        assert inv[node]["type"] in spellings and health_type[node] in spellings

    # Documents WHY the map is a set rather than a substring rule: the two sources
    # genuinely disagree, and for magos neither string is a prefix of the other.
    assert inv["magos"]["type"] != health_type["magos"]
    assert inv["asu"]["type"] != health_type["asu"]

    # Nodes that belong to no hardware domain must not be claimed by one.
    for n in ("tracker", "event_manager"):
        assert inv[n]["type"] not in spellings


# ---------- 2. extraction picks the right domain ----------

def test_acoustic_fault_produces_only_acoustic_targets():
    ctx = G._extract_context(_health([
        {"node": "asu", "type": "acoustic_asu", "status": "CRITICAL"},
        {"node": "magos", "type": "radar_magos", "status": "HEALTHY"}]))
    assert ctx["acoustic_targets"] == ["asu"]
    assert "radar_targets" not in ctx, "a healthy radar is not a radar fault"


def test_radar_fault_produces_only_radar_targets():
    ctx = G._extract_context(_health([
        {"node": "magos", "type": "radar_magos", "status": "DEGRADED"},
        {"node": "asu", "type": "acoustic_asu", "status": "HEALTHY"}]))
    assert ctx["radar_targets"] == ["magos"]
    assert "acoustic_targets" not in ctx


def test_launcher_only_fault_routes_by_the_config_vocabulary():
    """A node that never started publishes no health, so it has no proto type at all.

    It must still reach its domain expert, via the inventory spelling.
    """
    ctx = G._extract_context([{"agent": "triage", "tool": "get_process_table", "ok": True,
                               "data": {"never_started": ["magos"]}}])
    assert ctx["radar_targets"] == ["magos"]
    assert "acoustic_targets" not in ctx


def test_domain_targets_come_from_tool_payloads_not_prose():
    """An agent narrating "the radar looks bad" must not unlock the radar expert."""
    failed = [{"agent": "triage", "tool": "get_system_health", "ok": False,
               "data": {"nodes": [{"node": "magos", "type": "radar_magos",
                                   "status": "CRITICAL"}]}}]
    assert G._extract_context(failed) == {}


def test_unknown_node_type_claims_no_domain():
    ctx = G._extract_context(_health([{"node": "c2_gateway", "type": "base_node",
                                       "status": "CRITICAL"}]))
    assert "radar_targets" not in ctx and "acoustic_targets" not in ctx


# ---------- 3. the gate ----------

def test_acoustic_fault_unlocks_only_the_acoustic_expert(enabled):
    ctx = {"observed_nodes": ["asu", "magos"], "acoustic_targets": ["asu"]}
    elig = G._eligible(["triage"], ctx)
    assert "acoustic_deep_dive" in elig
    assert "radar_deep_dive" not in elig, "no radar fault; the expert must be unpickable"


def test_radar_fault_unlocks_only_the_radar_expert(enabled):
    ctx = {"observed_nodes": ["asu", "magos"], "radar_targets": ["magos"]}
    elig = G._eligible(["triage"], ctx)
    assert "radar_deep_dive" in elig
    assert "acoustic_deep_dive" not in elig


def test_expert_stays_locked_until_triage_has_run(enabled):
    ctx = {"observed_nodes": ["asu"], "acoustic_targets": ["asu"]}
    assert "acoustic_deep_dive" not in G._eligible([], ctx)
    assert "acoustic_deep_dive" in G._eligible(["triage"], ctx)


def test_empty_target_list_is_not_satisfied(enabled):
    ctx = {"observed_nodes": ["asu"], "acoustic_targets": []}
    assert "acoustic_deep_dive" not in G._eligible(["triage"], ctx)


# ---------- 4. inapplicable is not the same as blocked ----------

def test_out_of_scope_expert_is_not_reported_as_blocked(enabled):
    """An ASU-only fault must not produce "we could not check the radar"."""
    ctx = {"observed_nodes": ["asu", "magos"], "suspect_nodes": ["asu"],
           "acoustic_targets": ["asu"], "probe_targets": ["asu"]}
    names = [b["agent"] for b in G._blocked(["triage", "knowledge", "topology", "network",
                                             "acoustic_deep_dive"], ctx)]
    assert names == [], f"inapplicable experts leaked into the report: {names}"


def test_blind_run_reports_experts_as_blocked_not_inapplicable(enabled):
    """Nothing observed means ignorance, not absence of a fault — say so."""
    blocked = G._blocked(["triage"], {})
    names = [b["agent"] for b in blocked]
    assert set(EXPERTS) <= set(names), "a blind run must admit the experts never ran"
    assert "network" in names
    assert all("did not produce" in b["note"] for b in blocked)


def test_genuinely_blocked_agent_is_still_reported(enabled):
    """Scope handling must not weaken the needs_context gate it sits beside."""
    ctx = {"observed_nodes": ["tracker"], "suspect_nodes": ["tracker"]}
    blocked = {b["agent"]: b for b in G._blocked(["triage", "knowledge", "topology"], ctx)}
    assert "network" in blocked, "tracker has no address, so network could not run"
    assert blocked["network"]["missing"] == ["probe_targets"]
    assert set(EXPERTS) & set(blocked) == set(), "no hardware fault: experts are N/A"


# ---------- 5. the traps stay fixed ----------

def test_domain_experts_ship_out_of_the_default_order():
    assert set(EXPERTS) <= set(A.agents()), "defined, so the console can enable them"
    assert not (set(EXPERTS) & set(A.DEFAULT_ORDER)), "but inert until someone opts in"
    assert not (set(EXPERTS) & set(G._eligible(["triage"], {"radar_targets": ["magos"],
                                                           "acoustic_targets": ["asu"]})))


def test_no_agent_requires_a_gated_agent():
    """`requires` means "has run", and a gated agent may never run.

    Requiring one would deadlock its dependant permanently the first time the gate
    closes. This is the deadlock guard: it fails the moment someone writes
    requires: ["network"] on a deep-dive.
    """
    gated = set(A.needs_context()) | set(A.scope_context())
    for agent, deps in A.requires().items():
        assert not (set(deps) & gated), (
            f"{agent} requires {sorted(set(deps) & gated)}, which is itself gated and may "
            f"never run — {agent} would then be blocked forever")


def test_experts_are_graph_nodes_so_enabling_them_needs_no_code_change():
    nodes = G.build().nodes
    assert all(e in nodes for e in EXPERTS)


def test_step_budget_fits_the_whole_pool():
    assert A.MAX_AGENT_STEPS >= len(A.DEFAULT_ORDER) + len(EXPERTS)


# ---------- 6. gaps reach the report ----------

def _capture_synthesize(monkeypatch, state):
    seen = {}
    monkeypatch.setattr(G.llm, "ask_json",
                        lambda prompt, schema, system="": (seen.update(prompt=prompt, system=system)
                                                           or {"root_cause": "", "confidence": "low"}))
    G.synthesize({"question": "no tracks", "transcript": [], "findings": [], **state})
    return seen


def test_blocked_reaches_the_synthesize_prompt(monkeypatch):
    note = "network needs probe_targets, which triage did not produce."
    seen = _capture_synthesize(monkeypatch, {"blocked": [
        {"agent": "network", "reason": "missing_context",
         "missing": ["probe_targets"], "note": note}]})
    assert "could NOT be performed" in seen["prompt"]
    assert note in seen["prompt"]
    assert "must appear in unknowns" in seen["system"]


def test_a_complete_run_gets_no_gap_section(monkeypatch):
    seen = _capture_synthesize(monkeypatch, {"blocked": []})
    assert "could NOT be performed" not in seen["prompt"]


# ---------- 7. end to end on the real fixtures ----------

def test_real_fixtures_route_to_both_experts():
    """The recorded faulty run has a CRITICAL asu AND a DEGRADED magos."""
    findings = [{"agent": "triage", "tool": t, "ok": True, "data": call(t, {})["data"]}
                for t in ("get_system_health", "get_process_table")]
    ctx = G._extract_context(findings)
    assert ctx["acoustic_targets"] == ["asu"]
    assert ctx["radar_targets"] == ["magos"]
    assert ctx["suspect_nodes"] == ["asu", "magos"]


def test_a_launcher_only_fault_is_not_a_blind_run(enabled):
    """A node that never started yields a suspect with nothing observed.

    That run knows something, so an out-of-scope expert is inapplicable, not unchecked.
    Keying "blind" on observed_nodes alone got this wrong.
    """
    ctx = G._extract_context([{"agent": "triage", "tool": "get_process_table", "ok": True,
                               "data": {"never_started": ["magos"]}}])
    assert not ctx.get("observed_nodes") and ctx["suspect_nodes"] == ["magos"]
    names = [b["agent"] for b in G._blocked(["triage", "knowledge", "topology", "network",
                                             "radar_deep_dive"], ctx)]
    assert "acoustic_deep_dive" not in names, "no acoustic fault: the expert is N/A"
    assert names == []


# ---------- 8. the type table against the real gotcha30 vocabularies ----------

def test_domains_do_not_overlap():
    """One node type in two domains would unlock two experts for one fault."""
    pairs = [(a, b, G.DOMAINS[a] & G.DOMAINS[b])
             for a in G.DOMAINS for b in G.DOMAINS if a < b and G.DOMAINS[a] & G.DOMAINS[b]]
    assert pairs == []


@pytest.mark.parametrize("config_type,health_type,domain", [
    ("magos_radar", "radar_magos", "radar"),
    ("elm2135_radar", "radar_elm2135", "radar"),
    ("asu", "acoustic_asu", "acoustic"),
    ("meduza_optic", "optic_meduza", "camera"),
    ("python_optic_ptz", "optic_ptz", "camera"),
    ("python_optic_verification", "optic_verification", "camera"),
])
def test_both_spellings_of_every_model_are_covered(config_type, health_type, domain):
    """The vocabularies are reversed word order, so each model needs both entries."""
    assert config_type in G.DOMAINS[domain], f"{config_type} (config side) unmapped"
    assert health_type in G.DOMAINS[domain], f"{health_type} (health side) unmapped"


def test_the_optic_scanning_node_is_a_camera_not_an_acoustic_sensor():
    """Named like an ASU, emitted by python/nodes/optic_scanning_node. The name lies."""
    assert "optic_scanning_asu" in G.DOMAINS["camera"]
    assert "optic_scanning_asu" not in G.DOMAINS["acoustic"]


def test_a_camera_fault_unlocks_the_camera_expert(enabled):
    """Adding the expert was an agents.py entry; the routing already produced the key."""
    ctx = G._extract_context(_health([{"node": "meduza", "type": "optic_meduza",
                                       "status": "CRITICAL"}]))
    assert ctx["camera_targets"] == ["meduza"]
    elig = G._eligible(["triage"], {**ctx, "observed_nodes": ["meduza"]})
    assert "camera_deep_dive" in elig
    assert "radar_deep_dive" not in elig and "acoustic_deep_dive" not in elig


def test_a_node_whose_two_type_sources_disagree_lands_in_both_domains():
    """Deliberate, and worth knowing about.

    A node is placed by its config type OR its health type, so when the two disagree —
    the wrong config loaded, or a node renamed without the config following — it unlocks
    both experts rather than silently picking one. Wasting a step is the cheaper error:
    picking the wrong source would diagnose the wrong hardware with full confidence.
    """
    ctx = G._extract_context(_health([{"node": "magos", "type": "optic_meduza",
                                       "status": "CRITICAL"}]))
    assert ctx["radar_targets"] == ["magos"], "config says magos_radar"
    assert ctx["camera_targets"] == ["magos"], "health says optic_meduza"
