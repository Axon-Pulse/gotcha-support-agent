r"""The graph.

    START -> supervisor -> {functional agents, domain experts} -> supervisor
                        -> synthesize -> customer_communicator
                        -> propose_scenario -[INTERRUPT]-> save -> END
                                            \-- rejected --> END

Two kinds of agent share the supervisor pool:

  functional  triage, knowledge, topology, network — breadth. They establish which
              nodes are suspect and whether the fault is in shared plumbing.
  domain      acoustic_deep_dive, radar_deep_dive — depth on one hardware type.
              Unlocked by DOMAINS below, from node types found in tool payloads,
              never by the supervisor inferring a domain from the ticket text.

Only `save` writes to kb/, and it is reachable only via Command(goto="save") from the
approval branch, on a value that came from the operator's terminal. The agent has no
write tool at all — registry.load_tools() refuses to register one.
"""
import json
import logging
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import llm
import agents as A
from agents import MAX_AGENT_STEPS
from state import S

log = logging.getLogger(__name__)
KB_DIR = Path(__file__).resolve().parent / "kb" / "cases"

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "description": "One sentence, or empty if unknown."},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "Each item quotes a specific tool observation."},
        "suggested_actions": {"type": "array", "items": {"type": "string"},
                              "description": "Read-only checks a human performs."},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "propose_scenario": {
            "type": "object",
            "description": "A new runbook entry, ONLY if this pattern is not already "
                           "covered by the runbook and the diagnosis is well supported. "
                           "Omit otherwise.",
            "properties": {
                "id": {"type": "string", "description": "kebab-case slug"},
                "title": {"type": "string"},
                "topics": {"type": "array", "items": {"type": "string"},
                           "description": "One or more tags for matching this case to a "
                                          "future ticket, e.g. radar, acoustic, camera, "
                                          "network, config, infrastructure, general."},
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "root_cause": {"type": "string"},
                "checks": {"type": "array", "items": {"type": "string"}},
                "fix": {"type": "string"},
            },
            "required": ["id", "title", "symptoms", "root_cause", "checks", "fix"],
        },
    },
    "required": ["root_cause", "confidence", "evidence", "suggested_actions",
                 "escalate", "unknowns"],
}


_UNHEALTHY = {"DEGRADED", "CRITICAL", "OFFLINE", "UNKNOWN"}

# Hardware domain -> every spelling of the node type that means it.
#
# THE TWO VOCABULARIES ARE NOT THE SAME and neither is a prefix of the other:
#
#     node    inventory.py (config)   health proto node_type
#     magos   magos_radar             radar_magos
#     asu     asu                     acoustic_asu
#
# A substring or prefix match gets `magos_radar` and `radar_magos` right half the time,
# which is worse than getting them wrong consistently. Enumerate both spellings and
# match on set membership. tests/test_domain_routing.py asserts this table stays in
# sync with both sources, so a new node type fails a test rather than silently routing
# to nothing. Health also reports c2_gateway and system_launcher, which are not config
# nodes at all — they belong to no hardware domain and are correctly absent here.
#
# This table is the FALLBACK. A system in systems_inventory.yaml may declare `domain:`
# outright, which wins and needs no entry here — that is how a new hardware class
# (cameras, say) becomes routable by editing the registry rather than this file.
DOMAINS: dict[str, set[str]] = {
    # The two vocabularies are reversed word order — X_radar in a config is radar_X in
    # health — so both spellings of every model are listed. Sourced from gotcha30:
    # configs/*.yaml for the left column, node_type literals in the node sources for
    # the right. A model present in only one column silently routes to nothing.
    "radar": {
        "magos_radar", "radar_magos",          # Magos AR-300
        "elm2135_radar", "radar_elm2135",      # ELM-2135 (configs/elm.yaml, spatial.yaml)
        "radar",                               # generic, emitted by the base radar node
    },
    "acoustic": {
        "asu", "python_asu", "acoustic_asu",
    },
    "camera": {
        "meduza_optic", "optic_meduza",        # Meduza EO/IR
        "python_optic_ptz", "python_optic_ptz_mock", "optic_ptz",
        "python_optic_verification", "optic_verification",
        "python_ptz_director",
        # Named like an ASU but emitted by python/nodes/optic_scanning_node — it drives
        # optics from acoustic cues. It is a camera-domain node, not an acoustic one.
        "optic_scanning_asu",
    },
}


def _extract_context(findings: list[dict]) -> dict:
    """Pull the facts later agents depend on out of the findings just produced.

    Deliberately reads the structured tool payloads, not the model's prose: an agent
    that narrates "the radar looks unreachable" without a tool actually returning a
    node must not unlock the network agent.
    """
    import inventory
    observed, suspect, seen_types = [], [], {}
    for f in findings:
        if not f.get("ok"):
            continue
        data = f.get("data") or {}
        for row in data.get("nodes", []) or []:
            name = row.get("node") or row.get("name")
            if not name:
                continue
            observed.append(name)
            # Health rows carry the proto node_type; launcher rows carry no type at all.
            if row.get("type"):
                seen_types[name] = row["type"]
            status, state = row.get("status"), row.get("state")
            if status in _UNHEALTHY or (state and state != "PROC_RUNNING"):
                suspect.append(name)
        suspect += [n for n in data.get("never_started", []) or []]
        suspect += [e.get("name") for e in data.get("exited_with_error", []) or [] if e.get("name")]

    observed, suspect = sorted(set(observed)), sorted(set(suspect))
    # Only nodes we actually saw AND that have an address are probeable.
    inv = inventory.load()
    targets = sorted({n for n in (suspect or observed)
                      if ((inv.get(n) or {}).get("endpoint") or {}).get("host")})
    ctx = {"observed_nodes": observed, "suspect_nodes": suspect, "probe_targets": targets}

    # Which hardware domains are actually implicated. A node is placed by the domain its
    # registry entry declares, or failing that by either spelling of its type — so a
    # fault visible only to the launcher (no health message, therefore no proto type)
    # still routes correctly, and a domain the registry names but DOMAINS does not know
    # still produces targets.
    declared = {d for n in suspect if (d := (inv.get(n) or {}).get("domain"))}
    for domain in sorted(set(DOMAINS) | declared):
        spellings = DOMAINS.get(domain, set())
        ctx[f"{domain}_targets"] = sorted(
            n for n in suspect
            if (inv.get(n) or {}).get("domain") == domain
            or {(inv.get(n) or {}).get("type"), seen_types.get(n)} & spellings)

    # A platform fault has no telemetry of its own — nothing runs on a mast, so no node
    # ever reports "the tower is misaligned". Its signature is SEVERAL components on one
    # platform going wrong together, which is exactly the discriminator kb/tower.md uses:
    # an error shared across a tower is the tower, an error on one sensor is that mount.
    # One suspect is therefore deliberately not enough to unlock the expert.
    by_platform: dict[str, list[str]] = {}
    for n in suspect:
        if platform := (inv.get(n) or {}).get("system"):
            by_platform.setdefault(platform, []).append(n)
    ctx["infrastructure_targets"] = sorted(p for p, members in by_platform.items()
                                           if len(members) >= 2)

    return {k: v for k, v in ctx.items() if v}


def _eligible(visited: list[str], context: dict | None = None) -> list[str]:
    """Agents that may run now.

    Four gates, all hard: not already run; every `requires` agent has run; every
    `needs_context` key has actually been extracted; and every `scope_context` key is
    present. The last two gate identically here — they differ only in how _blocked()
    reports the miss, since one means "we could not find out" and the other means
    "there was nothing to find out".
    """
    req, needs = A.requires(), A.needs_context()
    scope, ctx = A.scope_context(), context or {}
    out = []
    for a in A.order():
        if a in visited:
            continue
        if not all(r in visited for r in req.get(a, [])):
            continue
        if not all(ctx.get(k) for k in needs.get(a, []) + scope.get(a, [])):
            continue
        out.append(a)
    return out


def _blocked(visited: list[str], context: dict | None = None) -> list[dict]:
    """Agents that could not run, with the reason — surfaced in the report.

    A missing `scope_context` key is NOT reported: a radar expert that sat out a run
    with no radar fault is inapplicable, and listing it as a check we failed to perform
    would make the report read as less complete than it is.

    The exception is a blind run. If we learned nothing at all, an empty `radar_targets`
    is ignorance rather than absence of a radar fault, and saying "not applicable" would
    be a claim the evidence does not support. So on a blind run, scope misses are
    reported like any other gap.

    "Blind" means no facts about any node, which is NOT the same as no *observed* nodes:
    a node that never started publishes no health message, so the launcher yields a
    suspect with nothing observed. That run knows something, and its out-of-scope
    experts are inapplicable rather than unchecked.
    """
    req, needs = A.requires(), A.needs_context()
    scope, ctx = A.scope_context(), context or {}
    blind = not (ctx.get("observed_nodes") or ctx.get("suspect_nodes"))
    out = []
    for a in A.order():
        if a in visited:
            continue
        deps = req.get(a, [])
        if not all(r in visited for r in deps):
            continue                       # upstream still pending; not blocked yet
        missing_scope = [k for k in scope.get(a, []) if not ctx.get(k)]
        if missing_scope and not blind:
            continue                       # out of scope: nothing here to investigate
        missing = [k for k in needs.get(a, []) if not ctx.get(k)] + missing_scope
        if not missing:
            continue
        ran_deps = [r for r in deps if r in visited]
        out.append({"agent": a, "reason": "missing_context", "missing": missing,
                    "note": f"{a} needs {', '.join(missing)}, which "
                            f"{' and '.join(ran_deps) or 'its prerequisites'} did not produce."})
    return out


def supervisor(s: S) -> dict:
    visited, ctx = s.get("visited", []), s.get("context", {})
    elig = _eligible(visited, ctx)
    if not elig or len(visited) >= MAX_AGENT_STEPS:
        blocked = _blocked(visited, ctx)
        if blocked:
            log.info("blocked: %s", [b["agent"] for b in blocked])
        return {"next": "synthesize", "blocked": blocked}
    if not A.supervisor_picks():
        return {"next": elig[0]}
    picked = llm.ask_json(
        prompt=(f"Question: {s['question']}\n\n"
                f"Already run: {visited or 'none'}\n"
                f"Findings so far:\n{_digest(s)}\n\n"
                f"Which should run next, or 'done' if there is enough to conclude?"),
        schema={"type": "object",
                "properties": {"next": {"type": "string", "enum": elig + ["done"]},
                               "why": {"type": "string"}},
                "required": ["next"]},
        system=("You route between diagnostic agents. Available now: "
                + ", ".join(f"{a} ({A.agents()[a]['prompt'][:60]}...)" for a in elig)),
    ).get("next", elig[0])
    return {"next": "synthesize" if picked == "done" else picked}


def _digest(s: S, limit: int = 4000) -> str:
    return json.dumps([{"agent": f["agent"], "tool": f["tool"], "ok": f["ok"],
                        "data": f["data"]} for f in s.get("findings", [])],
                      default=str)[:limit]


def make_agent_node(name: str):
    def node(s: S) -> dict:
        text, findings, usage = llm.run_agent(
            name, A.agents()[name], s["question"],
            context=f"Findings from earlier agents:\n{_digest(s)}" if s.get("findings") else "")
        ctx = _extract_context(findings)
        log.info("agent=%s tools=%d cache_read=%d context=%s",
                 name, len(findings), usage["cache_read"], sorted(ctx))
        return {"findings": findings, "visited": [name], "context": ctx,
                "transcript": [{"agent": name, "text": text, "usage": usage}]}
    return node


def synthesize(s: S) -> dict:
    # Checks that could not be performed are part of the diagnosis. Without this the
    # report silently reads as complete while a gated agent never ran, which is exactly
    # the overconfidence the gates exist to prevent.
    gaps = "".join(f"\n- {b['note']}" for b in s.get("blocked", []))
    report = llm.ask_json(
        prompt=(f"Question: {s['question']}\n\n"
                f"Agent conclusions:\n"
                + "\n\n".join(f"[{t['agent']}] {t['text']}" for t in s.get("transcript", []))
                + f"\n\nRaw findings:\n{_digest(s, 8000)}"
                + (f"\n\nChecks that could NOT be performed:{gaps}" if gaps else "")),
        schema=REPORT_SCHEMA,
        system=("Write the final diagnosis for an L1 technician. Every evidence item must "
                "quote something a tool actually returned. If a tool failed or the "
                "evidence is ambiguous, set escalate=true and list the unknowns rather "
                "than guessing. Any check listed as not performed must appear in "
                "unknowns — never present the diagnosis as complete when a planned check "
                "was skipped. Propose a new runbook scenario ONLY if this pattern is "
                "genuinely not already in the runbook."),
    )
    return {"report": report, "scenario": report.get("propose_scenario")}


# Patterns that must never reach a customer. Checked after generation rather than
# trusted to the prompt: "do not leak internal identifiers" is exactly the kind of
# instruction a model follows 95% of the time, and 95% is not good enough outbound.
_LEAKS = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "IP address"),
    (re.compile(r"\blocalhost:\d+|\b\w+://[^\s]+"), "URL or host:port"),
    (re.compile(r"(?m)^\s*/\w[\w/.-]*"), "path or eCAL topic"),
    (re.compile(r"\bPID\s*\d+|\bpid[=:]\s*\d+", re.I), "process id"),
    (re.compile(r"\b(?:ecal_mon_cli|docker\s+ps|tailscale|dumbo[\w-]*|systemctl)\b", re.I),
     "internal command or component"),
    (re.compile(r"\b(?:get_system_health|get_process_table|get_ecal_topology|"
                r"probe_endpoint|search_runbook|get_asu_service_status)\b"), "tool name"),
    (re.compile(r"\b(?:CRITICAL|DEGRADED|PROC_\w+|asu_connected|health_score)\b"),
     "internal status field"),
]


def _scan_for_leaks(text: str, node_names: list[str]) -> list[dict]:
    found = [{"kind": kind, "match": m.group(0)[:60]}
             for rx, kind in _LEAKS if (m := rx.search(text))]
    for n in node_names:
        if len(n) >= 3 and re.search(rf"\b{re.escape(n)}\b", text, re.I):
            found.append({"kind": "internal node name", "match": n})
    return found


def customer_communicator(s: S) -> dict:
    """Rewrite the technical report as a client-facing message.

    Runs after synthesize, outside the supervisor-routed pool: it gathers no evidence,
    it transforms the report that the evidence produced.
    """
    import inventory

    report = s.get("report") or {}
    cfg = A.post_agents().get("customer_communicator")
    if not cfg or not report:
        return {}

    brief = json.dumps({k: report.get(k) for k in
                        ("root_cause", "confidence", "escalate", "escalate_reason",
                         "evidence", "unknowns", "suggested_actions")}, indent=1, default=str)
    text, usage = llm.run_text_agent(
        cfg, f"The client reported:\n{s['question']}\n\nInternal technical report:\n{brief}")

    leaks = _scan_for_leaks(text, list(inventory.load()))
    msg = {
        "text": text,
        "leaks": leaks,
        # An escalating or low-confidence report must not be sent as if it were an answer.
        "safe_to_send": not leaks and not report.get("escalate")
                        and report.get("confidence") in ("medium", "high"),
        "review_reason": ("internal details in the draft" if leaks else
                          "report escalates or is low-confidence" if report.get("escalate")
                          or report.get("confidence") == "low" else ""),
    }
    if leaks:
        log.warning("customer draft withheld: %s", [x["kind"] for x in leaks])
    return {"customer_message": msg,
            "transcript": [{"agent": "customer_communicator", "text": text, "usage": usage}]}


def _render(sc: dict) -> str:
    """One case, in the same shape the console editor reads and writes.

    Symptoms go in the frontmatter, not the body — that is where the structured editor
    keeps them, and duplicating them in both would drift the moment someone edits one.
    """
    import yaml
    tags = sc.get("topics") or sc.get("domains") or sc.get("domain") or "general"
    tags = [tags] if isinstance(tags, str) else [str(t).strip() for t in tags if str(t).strip()]
    meta = {"topics": tags}
    if clean := [str(x).strip() for x in sc.get("symptoms", []) if str(x).strip()]:
        meta["symptoms"] = clean
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    checks = "\n".join(f"    {c}" for c in sc.get("checks", []))
    # Same section layout the console editor writes, so an agent-proposed case and a
    # hand-written one open in exactly the same form.
    return (f"---\n{head}\n---\n\n# {sc['title']}\n\n"
            f"## Root cause\n\n{sc['root_cause']}\n\n"
            f"## Checks (read-only)\n\n{checks}\n\n## Fix\n\n{sc['fix']}\n")


def propose_scenario(s: S) -> Command:
    sc = s.get("scenario")
    if not sc:
        return Command(goto=END)
    decision = interrupt({                     # graph stops; state is checkpointed
        "kind": "new_scenario",
        "scenario": sc,
        "preview_md": _render(sc),
    })
    if isinstance(decision, dict) and decision.get("approved"):
        return Command(goto="save",
                       update={"scenario": decision.get("edited") or sc})
    return Command(goto=END, update={"saved": False})


def save(s: S) -> dict:
    """The ONLY writer. Reachable only from an approved interrupt."""
    sc = s["scenario"]
    slug = re.sub(r"[^a-z0-9-]+", "-", str(sc["id"]).lower()).strip("-") or "scenario"
    KB_DIR.mkdir(parents=True, exist_ok=True)
    path = KB_DIR / f"{slug}.md"
    path.write_text(sc["_md"] if "_md" in sc else _render(sc))
    log.info("wrote %s", path)
    return {"saved": True}


def build():
    g = StateGraph(S)
    names = list(A.agents())
    g.add_node("supervisor", supervisor)
    for name in names:
        g.add_node(name, make_agent_node(name))
    g.add_node("synthesize", synthesize)
    g.add_node("customer_communicator", customer_communicator)
    g.add_node("propose_scenario", propose_scenario)
    g.add_node("save", save)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next"],
                            {**{a: a for a in names}, "synthesize": "synthesize"})
    for name in names:
        g.add_edge(name, "supervisor")
    g.add_edge("synthesize", "customer_communicator")
    g.add_edge("customer_communicator", "propose_scenario")
    g.add_edge("save", END)
    return g
