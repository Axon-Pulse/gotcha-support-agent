"""Agent definitions and ordering.

DEFAULTS below are the code baseline. The console writes overrides into
overrides.json; read the EFFECTIVE values through the accessors, never the
DEFAULT_* constants, so a UI edit is picked up without a restart.

- REQUIRES = {} and SUPERVISOR_PICKS -> model-driven routing
- every agent in ORDER, SUPERVISOR_PICKS = False -> fixed pipeline
"""
import config_store

DEFAULT_AGENTS: dict[str, dict] = {
    "triage": {
        "prompt": "Get the overall picture: which nodes are unhealthy, which are not "
                  "running at all. Report what you see; do not speculate about causes yet.",
        "tools": ["get_system_health", "get_process_table"],
    },
    "knowledge": {
        "prompt": "Search the runbook for the symptom pattern described. Quote the "
                  "relevant guidance. If nothing matches, say so plainly.",
        "tools": ["search_runbook"],
    },
    "topology": {
        "prompt": "Check eCAL wiring. A subscriber with no publisher means the node that "
                  "should publish it is not running. Two disjoint PID groups publishing "
                  "the same topics means two launcher sessions are running at once.",
        "tools": ["get_ecal_topology"],
    },
    "network": {
        "prompt": "Check reachability of sensor endpoints. A ping result is only "
                  "meaningful together with its route: report the verdict verbatim and "
                  "never upgrade an ambiguous verdict into a conclusion about hardware.",
        "tools": ["probe_endpoint"],
    },
}

DEFAULT_ORDER = ["triage", "knowledge", "topology", "network"]
DEFAULT_REQUIRES = {"topology": ["triage"], "network": ["triage"]}
DEFAULT_SUPERVISOR_PICKS = True
MAX_AGENT_STEPS = 6


def agents() -> dict[str, dict]:
    return config_store.get("agents", DEFAULT_AGENTS)


def order() -> list[str]:
    o = config_store.get("order", DEFAULT_ORDER)
    known = agents()
    return [a for a in o if a in known]


def requires() -> dict[str, list[str]]:
    return config_store.get("requires", DEFAULT_REQUIRES)


def supervisor_picks() -> bool:
    return bool(config_store.get("supervisor_picks", DEFAULT_SUPERVISOR_PICKS))


def defaults() -> dict:
    return {"agents": DEFAULT_AGENTS, "order": DEFAULT_ORDER,
            "requires": DEFAULT_REQUIRES, "supervisor_picks": DEFAULT_SUPERVISOR_PICKS}
