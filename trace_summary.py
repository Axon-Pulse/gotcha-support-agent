"""A readable summary of one trace, and its raw log split into labelled sections.

Nothing here asks a model. Every line of the summary is a field some agent already
wrote into the trace, so the summary can be wrong only where the trace is — and a
field that was never recorded is left empty rather than paraphrased into existence.
"""
from __future__ import annotations

# (section key, title, what it is for). Order is the order they are shown in.
SECTIONS = [
    ("report", "Full diagnosis",
     "The structured report the final agent wrote. The summary above is drawn from it."),
    ("attachments", "Attachments",
     "Files attached to the question, and the text the agents read in their place."),
    ("findings", "Tool results",
     "What each diagnostic tool actually returned. Every evidence line in the report "
     "should trace back to one of these."),
    ("transcript", "Agent reasoning",
     "Each agent's own write-up of what it checked and concluded, with the tokens it "
     "spent."),
    ("blocked", "Skipped agents",
     "Agents that could not run, and why — usually a prerequisite nothing produced."),
    ("commands", "Commands run",
     "Commands the chat-side agent ran on the machine for this bridge ticket."),
    ("kb_gaps", "Knowledge-base gaps",
     "What the agent looked for in the knowledge base and did not find."),
    ("notes", "Notes", "Free-form notes left by the chat-side agent."),
    ("customer_message", "Customer message",
     "The plain-language reply drafted for the person who asked."),
    ("origin", "Origin", "Where the question came from: channel, user and time."),
    ("usage", "Token usage", "Tokens spent: input, output and cache reads."),
    ("_other", "Run bookkeeping",
     "Timing, the routing decision and which agents ran — how the session was driven "
     "rather than what it found."),
]
_USAGE_KEYS = {"usage", "totals"}
_KNOWN = {k for k, *_ in SECTIONS} | _USAGE_KEYS


def _question(e: dict) -> str:
    return str(e.get("question") or (e.get("origin") or {}).get("question") or "").strip()


def _via(entries: list[dict]) -> str:
    for e in entries:
        if (o := e.get("origin") or {}).get("via"):
            return str(o["via"])
        if e.get("conversation"):
            return "chat"
    return "run"


# Last recorded status -> what the Trace page calls it. Only "done" is finished: every
# other one is a session that stopped, or has not yet got, to its answer.
STATUS_LABELS = {"done": "finished", "running": "not finished",
                 "in_progress": "not finished", "pending": "not picked up",
                 "awaiting_approval": "awaiting approval", "error": "failed",
                 "failed": "failed", "cancelled": "cancelled"}


def status(entries: list[dict]) -> str:
    if any(e.get("conversation") for e in entries):
        # A conversation writes a turn only once it is over, so there is no "running"
        # line to find. It has an answer if any turn produced one.
        return "done" if any(e.get("kind") in ("run", "follow_up") for e in entries) \
            else "error"
    marked = [e["status"] for e in entries if e.get("status")]
    if marked:
        return str(marked[-1])
    # Written before statuses were: those were only ever written for finished runs.
    return "done" if any(isinstance(e.get("report"), dict) for e in entries) else "running"


def summarize(entries: list[dict]) -> dict:
    """Problem, symptoms, diagnosis and solution, from the latest report in the trace."""
    questions = [q for e in entries if (q := _question(e))]
    reported = [e for e in entries if isinstance(e.get("report"), dict)]
    r = reported[-1]["report"] if reported else {}
    scenario = r.get("propose_scenario") if isinstance(r.get("propose_scenario"), dict) else {}
    diagnosis = str(r.get("bottom_line") or r.get("root_cause") or "").strip()
    fix = str(scenario.get("fix") or "").strip()
    # A follow-up answered from an earlier diagnosis has no report of its own.
    answers = [str(e["answer"]).strip() for e in entries if e.get("answer")]
    st = status(entries)
    errors = [str(e["error"]) for e in entries if e.get("error")]
    seen, atts = set(), []
    for e in entries:
        for a in e.get("attachments") or []:
            if isinstance(a, dict) and a.get("id") not in seen:
                seen.add(a.get("id"))
                atts.append(a)
    return {
        "status": st, "status_label": STATUS_LABELS.get(st, st),
        "finished": st == "done",
        "error": errors[-1] if errors else "",
        "attachments": atts,
        "via": _via(entries),
        "problem": questions[0] if questions else "",
        "follow_ups": questions[1:],
        # Observations, not conclusions: the evidence items quote tool output.
        "symptoms": [str(x) for x in r.get("evidence") or []]
                    or [str(x) for x in scenario.get("symptoms") or []],
        "diagnosis": diagnosis,
        "root_cause": str(r.get("root_cause") or "").strip(),
        "confidence": r.get("confidence") or "",
        # Only a fix somebody wrote down counts as a solution. suggested_actions are
        # read-only checks, so they are next steps, not the answer.
        "solution": fix,
        "next_steps": [str(x) for x in r.get("suggested_actions") or []],
        "escalate": bool(r.get("escalate")),
        "escalate_reason": str(r.get("escalate_reason") or "").strip(),
        "unknowns": [str(x) for x in r.get("unknowns") or []],
        "needs_permission": [p for p in r.get("needs_permission") or []
                             if isinstance(p, dict)],
        "last_answer": answers[-1] if answers else "",
        "found": bool(diagnosis and r.get("root_cause")),
        "turns": len(entries),
    }


def sections(entries: list[dict]) -> list[dict]:
    """The raw log, grouped. A multi-line trace (a conversation) keeps one value per
    line, labelled by line number, so turn 2's findings are not mistaken for turn 1's."""
    out = []
    many = len(entries) > 1
    for key, title, why in SECTIONS:
        vals = []
        for i, e in enumerate(entries, 1):
            if key == "usage":
                v = {k: e[k] for k in _USAGE_KEYS if e.get(k)}
            elif key == "_other":
                v = {k: val for k, val in e.items() if k not in _KNOWN}
            else:
                v = e.get(key)
            if v not in (None, "", [], {}):
                vals.append({"line": i, "value": v} if many else v)
        if vals:
            out.append({"key": key, "title": title, "why": why,
                        "value": vals if many else vals[0]})
    return out
