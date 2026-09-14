"""Agent definitions and ordering.

ADD AN AGENT: add an entry here with a prompt and the tool names it may use.
ORDER + REQUIRES are a hard policy the supervisor cannot violate. Within that, the
supervisor decides whether an agent is needed at all.

- REQUIRES = {} and a supervisor call  -> model-driven routing
- every agent in ORDER, SUPERVISOR_PICKS = False -> fixed pipeline
"""
AGENTS: dict[str, dict] = {
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

ORDER = ["triage", "knowledge", "topology", "network"]
REQUIRES = {"topology": ["triage"], "network": ["triage"]}
SUPERVISOR_PICKS = True     # False = run ORDER as a fixed pipeline, no routing call
MAX_AGENT_STEPS = 6
