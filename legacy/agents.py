"""Agent definitions, ordering and prerequisites.

DEFAULTS below are the code baseline. The console writes overrides into overrides.json;
always read through the accessors so a UI edit is picked up without a restart.

Two kinds of prerequisite, both enforced in graph._eligible():

  requires      — agent B cannot run until agent A has run at all (ordering).
  needs_context — agent B cannot run until a named fact has actually been EXTRACTED
                  from some earlier agent's findings. This is the strict one: if
                  triage runs but every tool errors, it produces no context, so a
                  dependant stays ineligible instead of probing blind.
  scope_context — like needs_context, but its absence means "there is nothing here to
                  investigate", not "we failed to find out". A radar expert with no
                  radar fault in scope is not blocked, it is inapplicable, and must
                  not appear in the report as a check we could not perform. The one
                  exception is a blind run — see graph._blocked().

`provides` documents which context keys an agent is expected to yield; the extractors
that populate them live in graph._extract_context.
"""
import config_store

DEFAULT_AGENTS: dict[str, dict] = {
    "triage": {
        "prompt": "Get the overall picture: which nodes are unhealthy, which are not "
                  "running at all. Report what you see; do not speculate about causes yet. "
                  "Name the affected nodes explicitly — later agents depend on that list.",
        "tools": ["get_system_health", "get_process_table"],
        "provides": ["observed_nodes", "suspect_nodes"],
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
        "requires": ["triage"],
    },
    "network": {
        "prompt": "Check reachability of the endpoints triage flagged. A ping result is "
                  "only meaningful together with its route: report the verdict verbatim "
                  "and never upgrade an ambiguous verdict into a conclusion about hardware.",
        "tools": ["probe_endpoint"],
        "requires": ["triage"],
        # Hard gate: without addressable nodes extracted from triage's findings there is
        # nothing to probe, and probing the whole inventory on a guess is worse than
        # reporting that we could not get far enough to check.
        "needs_context": ["probe_targets"],
    },
    # --- Domain experts -----------------------------------------------------
    # Unlocked by hardware domain extracted from tool payloads, never by the supervisor
    # inferring "this sounds like a radar problem" from the ticket text. All are OUT of
    # DEFAULT_ORDER: define first, enable from the console when you want them.
    #
    # SCOPING: an expert gets its OWN domain tool and search_runbook, and nothing else.
    # It deliberately does NOT get get_system_health: triage already called it, and every
    # agent is handed the full findings digest, so the health rows are in front of the
    # expert without a second call. Giving it the tool invites a redundant call, and
    # giving it another domain's tool invites a diagnosis of hardware it is not expert in.
    #
    # `requires` is triage ONLY, never a gated agent. `requires` means "has run", and a
    # blocked agent never runs — so requiring `network` here would deadlock the expert
    # permanently the moment network is blocked on probe_targets.
    "acoustic_deep_dive": {
        "prompt": "The acoustic (ASU) node is implicated. Its backend is a LOCAL Docker "
                  "service, not hardware on the sensor LAN — so a reachability probe can "
                  "never explain asu_connected=false. Establish the container's actual "
                  "state with your tool, then correlate it with the request counters "
                  "ALREADY IN THE FINDINGS DIGEST you were given: request_count climbing "
                  "with success_count at zero means the backend was never reachable since "
                  "start; a flat success_count with recent errors means it worked and then "
                  "stopped. Say which of the two it is, or say plainly that the counters "
                  "do not distinguish them.",
        "tools": ["get_asu_service_status", "search_runbook"],
        "requires": ["triage"],
        "scope_context": ["acoustic_targets"],
        "provides": ["acoustic_verdict"],
    },
    "radar_deep_dive": {
        "prompt": "The radar (Magos) node is implicated. Read its link counters from the "
                  "findings digest: connection_state CONNECTED alongside a large "
                  "connection_errors count means the WebSocket is flapping, not down, and "
                  "a flapping link and a dead radar need different fixes. Check the "
                  "runbook. Your domain tool is a PLACEHOLDER — it reports liveness only, "
                  "so if the answer turns on link behaviour, say the radar check does not "
                  "exist yet rather than inferring it. If the network verdict is ambiguous "
                  "or a route was hijacked, say the radar cannot be assessed on this "
                  "evidence — never call the hardware faulty on an ambiguous route.",
        "tools": ["get_radar_status", "search_runbook"],
        "requires": ["triage"],
        "scope_context": ["radar_targets"],
        "provides": ["radar_verdict"],
    },
    "camera_deep_dive": {
        "prompt": "A camera or optic node is implicated — the Meduza head, the PTZ "
                  "controller or the verification node. Separate three faults that look "
                  "alike from a display: reachable but not streaming, accepting PTZ "
                  "commands but not moving, and pointing the wrong way because the mount "
                  "orientation is wrong. The third is NOT a camera fault — if bearings are "
                  "wrong but the stream is fine, say so and point at the mount. Your "
                  "domain tool is a PLACEHOLDER reporting liveness only; check the runbook "
                  "and say what you would need rather than inferring stream or pose state.",
        "tools": ["get_camera_status", "search_runbook"],
        "requires": ["triage"],
        "scope_context": ["camera_targets"],
        "provides": ["camera_verdict"],
    },
    "infrastructure_expert": {
        "prompt": "Several components on one platform are implicated at once, which is the "
                  "signature of a tower or mount fault rather than a sensor fault. The "
                  "discriminator: an angular error SHARED across every sensor on a tower is "
                  "the tower's yaw; an error on one sensor alone is that sensor's mount. "
                  "Nothing publishes health for a mast, so this is diagnosed from "
                  "configuration, not telemetry — check the runbook for the composition "
                  "rule. Your domain tool is a PLACEHOLDER that probes liveness and says "
                  "nothing about alignment, so do not report an alignment verdict from it; "
                  "say which configuration values you would need to read.",
        "tools": ["get_tower_status", "search_runbook"],
        "requires": ["triage"],
        "scope_context": ["infrastructure_targets"],
        "provides": ["platform_verdict"],
    },
}

# Domain experts are absent here on purpose: staged rollout. graph.build() still creates
# a node for every agent in DEFAULT_AGENTS, so enabling one is an ORDER edit, not a code
# change — and the Graph tab greys out anything the supervisor can never pick.
DEFAULT_ORDER = ["triage", "knowledge", "topology", "network"]
DEFAULT_REQUIRES = {"topology": ["triage"], "network": ["triage"]}
DEFAULT_SUPERVISOR_PICKS = True
MAX_AGENT_STEPS = 10

# ---------------------------------------------------------------------------
# Post-synthesis agents. NOT part of the supervisor-routed pool: they run at a
# fixed point in the graph, take no tools, and transform text rather than gather
# evidence. Kept here so the console edits them alongside everything else.
# ---------------------------------------------------------------------------

DEFAULT_POST_AGENTS: dict[str, dict] = {
    # Answers a follow-up in a conversation from evidence already gathered. The empty
    # tool list is the safety property, not an optimisation: with no tools it cannot
    # reach transport.py, so no follow-up can touch a device however it is phrased.
    "follow_up": {
        "prompt": (
            "You are answering an engineer's follow-up about a diagnosis already made in "
            "this conversation. You have the reports and the raw tool output behind "
            "them, and nothing else — you cannot run a check or look anything up.\n\n"
            "Answer in two or three sentences. Be specific and technical: name the node, "
            "the PID, the uptime, the verdict the tool actually returned. No preamble, "
            "no restating the question, no offers to investigate further.\n\n"
            "If the evidence does not answer the question, say so in one sentence and "
            "name the check that would. Never fill the gap by reasoning about what is "
            "probably true — an answer invented from adjacent evidence is exactly the "
            "failure this whole pipeline exists to prevent, and it reads as confident."
        ),
        "tools": [],
    },
    "customer_communicator": {
        "prompt": (
            "You are a customer support representative writing to the client who reported "
            "this issue. You are given an internal technical report. Rewrite it as a short, "
            "polite message to the client.\n\n"
            "Write it so that:\n"
            "- It opens by restating, in plain language, what they told us they were seeing.\n"
            "- It says what we found and what happens next, in the order the client cares "
            "about: impact first, cause second, next step third.\n"
            "- It uses no jargon. No node names, IP addresses, process IDs, topic paths, "
            "file paths, tool names, container names or log excerpts. If a detail cannot be "
            "said without one of those, describe it by its effect instead — 'the acoustic "
            "sensor could not reach the service it depends on', not 'asu_connected=false on "
            "localhost:8000'.\n"
            "- It matches the report's certainty exactly. If the report escalates or has low "
            "confidence, say plainly that we are still investigating and do not name a cause "
            "as if it were confirmed. Never promise a fix time, a refund, or a root cause the "
            "report did not establish.\n"
            "- It is 4-8 sentences, warm but not chatty, and signs off without a placeholder "
            "name.\n\n"
            "Return only the message body. No subject line, no markdown headings, no preamble."
        ),
        "tools": [],
    },
}


def agents() -> dict[str, dict]:
    return config_store.get("agents", DEFAULT_AGENTS)


def post_agents() -> dict[str, dict]:
    return config_store.get("post_agents", DEFAULT_POST_AGENTS)


def order() -> list[str]:
    o = config_store.get("order", DEFAULT_ORDER)
    known = agents()
    return [a for a in o if a in known]


def requires() -> dict[str, list[str]]:
    """Agent-level ordering prerequisites, merged with any declared on the agent itself."""
    explicit = config_store.get("requires", DEFAULT_REQUIRES)
    merged = {k: list(v) for k, v in explicit.items()}
    for name, spec in agents().items():
        for dep in spec.get("requires", []):
            merged.setdefault(name, [])
            if dep not in merged[name]:
                merged[name].append(dep)
    return merged


def needs_context() -> dict[str, list[str]]:
    """Context keys each agent must have available before it may run."""
    return {n: list(spec.get("needs_context", []))
            for n, spec in agents().items() if spec.get("needs_context")}


def scope_context() -> dict[str, list[str]]:
    """Context keys that decide whether an agent APPLIES at all.

    Missing means out of scope, not blocked. Kept separate from needs_context so the
    report never says "we could not check the radar" about a run that had no radar fault.
    """
    return {n: list(spec.get("scope_context", []))
            for n, spec in agents().items() if spec.get("scope_context")}


def supervisor_picks() -> bool:
    return bool(config_store.get("supervisor_picks", DEFAULT_SUPERVISOR_PICKS))


def defaults() -> dict:
    return {"agents": DEFAULT_AGENTS, "order": DEFAULT_ORDER,
            "requires": DEFAULT_REQUIRES, "supervisor_picks": DEFAULT_SUPERVISOR_PICKS,
            "post_agents": DEFAULT_POST_AGENTS}
