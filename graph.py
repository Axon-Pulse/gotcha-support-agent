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
from agents import AGENTS, MAX_AGENT_STEPS, ORDER, REQUIRES, SUPERVISOR_PICKS
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


def _eligible(visited: list[str]) -> list[str]:
    return [a for a in ORDER if a not in visited
            and all(r in visited for r in REQUIRES.get(a, []))]


def supervisor(s: S) -> dict:
    visited = s.get("visited", [])
    elig = _eligible(visited)
    if not elig or len(visited) >= MAX_AGENT_STEPS:
        return {"next": "synthesize"}
    if not SUPERVISOR_PICKS:
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
                + ", ".join(f"{a} ({AGENTS[a]['prompt'][:60]}...)" for a in elig)),
    ).get("next", elig[0])
    return {"next": "synthesize" if picked == "done" else picked}


def _digest(s: S, limit: int = 4000) -> str:
    return json.dumps([{"agent": f["agent"], "tool": f["tool"], "ok": f["ok"],
                        "data": f["data"]} for f in s.get("findings", [])],
                      default=str)[:limit]


def make_agent_node(name: str):
    def node(s: S) -> dict:
        text, findings, usage = llm.run_agent(
            name, AGENTS[name], s["question"],
            context=f"Findings from earlier agents:\n{_digest(s)}" if s.get("findings") else "")
        log.info("agent=%s tools=%d cache_read=%d", name, len(findings), usage["cache_read"])
        return {"findings": findings, "visited": [name],
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
    g.add_node("supervisor", supervisor)
    for name in AGENTS:
        g.add_node(name, make_agent_node(name))
    g.add_node("synthesize", synthesize)
    g.add_node("propose_scenario", propose_scenario)
    g.add_node("save", save)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next"],
                            {**{a: a for a in AGENTS}, "synthesize": "synthesize"})
    for name in AGENTS:
        g.add_edge(name, "supervisor")
    g.add_edge("synthesize", "propose_scenario")
    g.add_edge("save", END)
    return g
