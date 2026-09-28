"""Turn a one-shot diagnosis into a conversation, without reusing a graph session.

THE CONSTRAINT THIS IS BUILT AROUND. Graph state is append-only — `findings`, `visited`
and `transcript` all reduce with operator.add. Feeding a second question into the same
LangGraph thread therefore finds every agent already in `visited`, leaves the supervisor
with nothing eligible, and re-summarises the OLD evidence against the NEW question.
Silently. slack_app.py documents the same trap as "ONE MESSAGE IS ONE SESSION".

So a conversation is not one long graph run. It is a sequence of turns, and each turn
that needs evidence gets its own fresh graph session; earlier turns reach it as context,
never as state. The conversation lives out here, where appending is safe.

TWO KINDS OF TURN, because they cost different amounts:

    run        a new problem. Full pipeline: supervisor, agents, tools, report.
    follow_up  a question about what was already found. No graph, NO TOOLS, one text
               call against evidence that is already in hand.

route() decides which. Getting it wrong is not symmetric, and the prompt says so: a
follow-up misrouted as a run wastes a minute and a full pipeline's tokens, while a new
problem misrouted as a follow-up answers it from stale evidence that was gathered about
something else — so when the two readings are close, route to a run.

WHY THE FOLLOW-UP AGENT HAS NO TOOLS. It is not an efficiency; it is the safety
property. A tool-less agent cannot reach transport.py, so no follow-up can touch a
device or a command, whatever the message asks for. It answers from the findings it is
handed or it says it cannot.
"""
import json
import logging
import time

import agents as A
import attachments
import llm

log = logging.getLogger(__name__)

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string", "enum": ["run", "follow_up"],
            "description": "run = gather fresh evidence; follow_up = answer from what "
                           "is already known.",
        },
        "why": {"type": "string", "description": "One short sentence, shown to the operator."},
    },
    "required": ["kind", "why"],
}

_ROUTE_SYSTEM = (
    "You decide whether a support message needs a fresh diagnostic run or can be "
    "answered from evidence already gathered in this conversation.\n\n"
    "follow_up — asks about, clarifies or challenges something already established: "
    "what a term meant, which node was implicated, why a conclusion was reached, what "
    "to do first, whether an earlier answer is right.\n"
    "run — reports a NEW or CHANGED symptom, names a component not yet investigated, "
    "or says an earlier fix did not work. Anything needing an observation nobody has "
    "made yet is a run.\n\n"
    "The two errors are not equally bad. A follow-up sent for a run costs a minute and "
    "a lot of tokens and is otherwise harmless. A run sent as a follow-up answers a new "
    "problem out of evidence collected about a different one, and reads as confident. "
    "When it is close, choose run."
)


def digest(turns: list[dict], limit: int = 6000) -> str:
    """What earlier turns established, as the context a later turn is answered against.

    Deliberately the REPORT and the tool findings, not the whole transcript: the
    per-agent prose is long, duplicated in the report, and the thing most likely to push
    the real evidence out of the window.
    """
    out = []
    for t in turns:
        # What the turn's agents actually read: the typed question plus any attachment
        # descriptions. Without them a follow-up about "the error in the screenshot"
        # would be answered by a model that never heard of one.
        asked = attachments.compose(t.get("question") or "", t.get("attachments") or [])
        if t.get("kind") == "follow_up":
            out.append({"asked": asked, "answered": t.get("answer")})
            continue
        r = t.get("report") or {}
        out.append({
            "asked": asked,
            "found": {k: r.get(k) for k in
                      ("bottom_line", "root_cause", "confidence", "evidence",
                       "unknowns", "suggested_actions", "escalate")},
            "tools": [{"agent": f.get("agent"), "tool": f.get("tool"), "ok": f.get("ok"),
                       "data": f.get("data")} for f in (t.get("findings") or [])],
        })
    return json.dumps(out, default=str)[:limit]


def route(turns: list[dict], message: str) -> dict:
    """Pick the kind of turn. The first message in a conversation is always a run.

    NEVER RAISES. Routing is an optimisation — it decides whether the expensive path can
    be skipped — so a failure here must cost the cheap call, not the operator's turn.
    Letting an APITimeoutError out of this function meant a blip on a three-second
    request destroyed a conversation that the full pipeline would have answered fine.
    """
    if not turns:
        return {"kind": "run", "why": "first message in the conversation"}
    try:
        got = llm.ask_json(
            prompt=(f"Conversation so far:\n{digest(turns)}\n\n"
                    f"New message from the operator:\n{message}"),
            schema=ROUTE_SCHEMA,
            system=_ROUTE_SYSTEM,
        )
    except Exception as e:  # noqa: BLE001 - deliberately everything; see the docstring
        log.warning("routing failed (%s); running the full pipeline", e)
        return {"kind": "run",
                "why": f"could not decide ({type(e).__name__}); ran the full pipeline"}
    kind = got.get("kind")
    if kind not in ("run", "follow_up"):
        # An unusable answer is not a reason to guess cheaply. Run is the safe default:
        # it gathers evidence rather than inventing it.
        log.warning("router returned %r; defaulting to a full run", kind)
        return {"kind": "run", "why": "router gave no usable answer; ran the full pipeline"}
    return {"kind": kind, "why": got.get("why") or ""}


def answer_follow_up(turns: list[dict], message: str) -> tuple[str, dict]:
    """Answer from evidence already gathered. Returns (text, usage)."""
    cfg = A.post_agents().get("follow_up") or {"prompt": _FALLBACK_PROMPT, "tools": []}
    return llm.run_text_agent(
        cfg,
        f"Evidence and conclusions from this conversation:\n{digest(turns)}\n\n"
        f"The operator now asks:\n{message}",
        max_tokens=1200,
    )


# Used only if somebody deletes the follow_up post-agent from the console. The turn
# still has to be answerable — without this it would raise on a config edit.
_FALLBACK_PROMPT = (
    "Answer the operator's question using only the evidence given. Two or three "
    "sentences. If the evidence does not answer it, say exactly that and say which "
    "check would."
)


def start_turn() -> dict:
    """The half of a turn that exists before any work happens."""
    return {"started_at": time.time()}


def finish_turn(turn: dict, **fields) -> dict:
    """Close a turn: stamp the clock and fold in whatever the turn produced.

    Duration is measured here rather than taken from the model, because what an
    operator waited for is wall-clock including tool calls and queueing, not the time
    the API spent generating.
    """
    ended = time.time()
    return {**turn, **fields, "ended_at": ended,
            "duration_s": round(ended - turn.get("started_at", ended), 2)}


def totals(turns: list[dict]) -> dict:
    """Conversation-level aggregates, for the header the operator reads first."""
    agg = {"turns": len(turns), "runs": 0, "follow_ups": 0, "duration_s": 0.0,
           "input": 0, "output": 0, "cache_read": 0}
    for t in turns:
        agg["runs" if t.get("kind") == "run" else "follow_ups"] += 1
        agg["duration_s"] += float(t.get("duration_s") or 0)
        for k in ("input", "output", "cache_read"):
            agg[k] += int((t.get("usage") or {}).get(k) or 0)
    agg["duration_s"] = round(agg["duration_s"], 2)
    return agg
