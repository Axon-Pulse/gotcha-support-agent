r"""The graph.

    START -> supervisor -> {triage, knowledge, topology, network} -> supervisor
                        -> synthesize -> propose_scenario -[INTERRUPT]-> save -> END
                                                          \-- rejected --> END

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
KB_DIR = Path(__file__).resolve().parent / "kb"

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


def _extract_context(findings: list[dict]) -> dict:
    """Pull the facts later agents depend on out of the findings just produced.

    Deliberately reads the structured tool payloads, not the model's prose: an agent
    that narrates "the radar looks unreachable" without a tool actually returning a
    node must not unlock the network agent.
    """
    import inventory
    observed, suspect = [], []
    for f in findings:
        if not f.get("ok"):
            continue
        data = f.get("data") or {}
        for row in data.get("nodes", []) or []:
            name = row.get("node") or row.get("name")
            if not name:
                continue
            observed.append(name)
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
    return {k: v for k, v in ctx.items() if v}


def _eligible(visited: list[str], context: dict | None = None) -> list[str]:
    """Agents that may run now.

    Three gates, all hard: not already run; every `requires` agent has run; and every
    `needs_context` key has actually been extracted. The third is what stops an agent
    running on a prerequisite that executed but produced nothing.
    """
    req, needs, ctx = A.requires(), A.needs_context(), context or {}
    out = []
    for a in A.order():
        if a in visited:
            continue
        if not all(r in visited for r in req.get(a, [])):
            continue
        if not all(ctx.get(k) for k in needs.get(a, [])):
            continue
        out.append(a)
    return out


def _blocked(visited: list[str], context: dict | None = None) -> list[dict]:
    """Agents that can never run now, with the reason — surfaced in the report."""
    req, needs, ctx = A.requires(), A.needs_context(), context or {}
    out = []
    for a in A.order():
        if a in visited:
            continue
        missing_ctx = [k for k in needs.get(a, []) if not ctx.get(k)]
        ran_deps = [r for r in req.get(a, []) if r in visited]
        if missing_ctx and all(r in visited for r in req.get(a, [])):
            out.append({"agent": a, "reason": "missing_context", "missing": missing_ctx,
                        "note": f"{a} needs {', '.join(missing_ctx)}, which "
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
    report = llm.ask_json(
        prompt=(f"Question: {s['question']}\n\n"
                f"Agent conclusions:\n"
                + "\n\n".join(f"[{t['agent']}] {t['text']}" for t in s.get("transcript", []))
                + f"\n\nRaw findings:\n{_digest(s, 8000)}"),
        schema=REPORT_SCHEMA,
        system=("Write the final diagnosis for an L1 technician. Every evidence item must "
                "quote something a tool actually returned. If a tool failed or the "
                "evidence is ambiguous, set escalate=true and list the unknowns rather "
                "than guessing. Propose a new runbook scenario ONLY if this pattern is "
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
    (re.compile(r"\b(?:ecal_mon_cli|docker\s+ps|tailscale|dumbo-backend|systemctl)\b", re.I),
     "internal command or component"),
    (re.compile(r"\b(?:get_system_health|get_process_table|get_ecal_topology|"
                r"probe_endpoint|search_runbook)\b"), "tool name"),
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
    checks = "\n".join(f"    {c}" for c in sc.get("checks", []))
    symptoms = "\n".join(f"- {x}" for x in sc.get("symptoms", []))
    return (f"# {sc['title']}\n\n## Symptoms\n{symptoms}\n\n"
            f"## Root cause\n{sc['root_cause']}\n\n## Checks (read-only)\n{checks}\n\n"
            f"## Fix\n{sc['fix']}\n")


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
