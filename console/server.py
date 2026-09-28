"""Local operations console.

Runs sessions (mock mode only), surfaces the approval queue, and shows traces, the
knowledge base, and the permission surface READ-ONLY.

Permissions are deliberately not editable here. Their value is that widening them is a
code change that shows up in a diff and a test run; a button that adds an allowed
command is how that guarantee quietly dies.
"""
import logging
import os
import re
import unicodedata
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path

import env_file

# .env first, so a key written where the README says to put it is actually present.
env_file.load()

# Console never runs live. This must stay ABOVE `import transport`, which reads MODE
# once at import and never again — below it, an AGENT_MODE=live from the environment or
# from .env would already have been read and the browser console would be issuing real
# commands at hardware. (.env cannot set it anyway, since env_file never overrides, but
# an exported one could; this line is what makes that unconditional.) Live runs stay a
# deliberate command-line act.
os.environ["AGENT_MODE"] = "mock"

import json  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import yaml  # noqa: E402

import agents as agents_mod  # noqa: E402
import attachments as attachments_mod  # noqa: E402
import bridge  # noqa: E402
import config_store  # noqa: E402
import conversation as convo  # noqa: E402
import inventory  # noqa: E402
import llm  # noqa: E402
import secrets_store  # noqa: E402
import trace_summary  # noqa: E402
import trace_tag  # noqa: E402
import transport  # noqa: E402
import graph as graph_mod  # noqa: E402
from graph import build  # noqa: E402
from registry import REGISTRY, load_tools  # noqa: E402

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
DB = str(ROOT / "graph.db")
TRACES = ROOT / "traces"
# The editable half. The system model beside it is code-derived and served read-only.
KB = ROOT / "kb" / "cases"
SYSTEM_MODEL = ROOT / "kb" / "system-model.md"
# Topics somebody created but no case uses yet. Without this a new topic would exist only
# in the browser tab that made it, and vanish on refresh — the list is otherwise derived
# from what cases actually carry, and a topic on nothing is carried by nothing.
TOPICS_FILE = ROOT / "kb" / "topics.json"

load_tools()
app = FastAPI(title="support-agent console")

_graph = build()
_graph_lock = threading.Lock()


def graph():
    with _graph_lock:
        return _graph


def rebuild_graph() -> None:
    """Config edits can add or remove agents, which changes the graph topology."""
    global _graph
    with _graph_lock:
        _graph = build()

# session id -> {status, question, error}
SESSIONS: dict[str, dict] = {}
# conversation id -> {id, created_at, turns: [...], status, error}. A conversation is a
# list of turns, NOT a long-lived graph thread — see conversation.py for why reusing a
# thread would silently re-summarise old evidence against a new question.
CONVERSATIONS: dict[str, dict] = {}
_lock = threading.Lock()


def _set(sid: str, **kw) -> None:
    with _lock:
        SESSIONS.setdefault(sid, {}).update(kw)


def _run(sid: str, payload) -> None:
    """Drive the graph in a worker thread. Fresh sqlite connection per thread."""
    if isinstance(payload, dict):
        # Written before any work, so a run the server never finished — a crash, a
        # restart mid-run — still leaves a trace, as "running" with nothing after it.
        # The attachments' description tokens were spent on upload; this line is where
        # they join the session's total.
        s = SESSIONS.get(sid) or {}
        extra = ({"attachments": s["attachments"],
                  "usage": attachments_mod.usage_of(s["attachments"])}
                 if s.get("attachments") else {})
        _trace_line(sid, status="running", question=s.get("question",
                    payload.get("question")), system=payload.get("system"),
                    started_at=time.time(), **extra)
    try:
        # llm's totals are thread-local, and this thread is this session — so resetting
        # here scopes the count to one run without a session id ever reaching llm.py.
        # The trace line each segment writes therefore holds only that segment's
        # tokens, and the lines of one session add up to its total.
        llm.reset_usage()
        with SqliteSaver.from_conn_string(DB) as cp:
            out = graph().compile(checkpointer=cp).invoke(
                payload, {"configurable": {"thread_id": sid}})
        if "__interrupt__" in out:
            _set(sid, status="awaiting_approval", usage=llm.totals(),
                 pending=out["__interrupt__"][0].value)
            _write_trace(sid, out, status="awaiting_approval")
        else:
            _set(sid, status="done", pending=None, report=out.get("report"),
                 usage=llm.totals(), saved=bool(out.get("saved")))
            _write_trace(sid, out)
    except Exception as e:  # noqa: BLE001 - surfaced in the UI, not swallowed
        # A run that failed at the fourth agent still spent the first three agents'
        # tokens. Reporting nothing there would understate the bill exactly when
        # somebody is retrying and spending it again.
        _set(sid, status="error", error=f"{type(e).__name__}: {e}", usage=llm.totals(),
             detail=traceback.format_exc()[-2000:])
        _trace_line(sid, status="error", error=f"{type(e).__name__}: {e}",
                    usage=llm.totals())


def _add_usage(a: dict, b: dict) -> dict:
    return {k: int(a.get(k) or 0) + int(b.get(k) or 0)
            for k in ("input", "output", "cache_read")}


def _trace_line(sid: str, **fields) -> None:
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps(fields, default=str) + "\n")


def _write_trace(sid: str, out: dict, status: str = "done") -> None:
    # What was typed, not the text with attachment descriptions folded in: those are in
    # the start line's `attachments`, and the summary shows them there.
    _trace_line(sid, status=status,
                question=(SESSIONS.get(sid) or {}).get("question") or out.get("question"),
                findings=out.get("findings", []), transcript=out.get("transcript", []),
                report=out.get("report"),
                # This segment's total, including the routing and synthesis calls that
                # produce no transcript entry of their own.
                usage=llm.totals(), saved=bool(out.get("saved")))


# ---------------------------------------------------------------------------
# Conversations
#
# One file per conversation under traces/, one JSON line per turn, appended as the turn
# finishes. A conversation that is still going is therefore already durable: the trace
# is not written at the end, because there is no end until somebody stops typing.
# ---------------------------------------------------------------------------

def _cset(cid: str, **kw) -> None:
    with _lock:
        CONVERSATIONS.setdefault(cid, {"id": cid, "turns": []}).update(kw)


def _cturns(cid: str) -> list[dict]:
    with _lock:
        return list((CONVERSATIONS.get(cid) or {}).get("turns") or [])


def _append_turn(cid: str, turn: dict) -> None:
    with _lock:
        c = CONVERSATIONS.setdefault(cid, {"id": cid, "turns": []})
        c["turns"].append(turn)
        c["totals"] = convo.totals(c["turns"])
    _write_turn_trace(cid, turn, (CONVERSATIONS.get(cid) or {}).get("totals") or {})


def _write_turn_trace(cid: str, turn: dict, totals: dict) -> None:
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{cid}.jsonl").open("a") as f:
        f.write(json.dumps({"conversation": cid, **turn, "totals": totals},
                           default=str) + "\n")


def _converse(cid: str, message: str, recs: list[dict] | None = None,
              system: str | None = None) -> None:
    """One turn, in its own thread. Routes, does the work, records it.

    `message` is what the operator typed; `asked` is what the agents read — the same
    text with each attachment's description after it. The turn keeps them apart so the
    conversation reads as typed, and digest() puts the attachments back for later turns.
    """
    recs = recs or []
    asked = attachments_mod.compose(message, recs)
    turn = convo.start_turn()
    if recs:
        turn["attachments"] = attachments_mod.public(recs)
    if system:
        turn["system"] = system
    # Thread-local, and this thread is this turn — so the token count covers the
    # routing call as well as whatever the routing decided to do. The descriptions were
    # made on upload, in another thread, so their tokens are added here by hand.
    llm.reset_usage()

    def spent() -> dict:
        return _add_usage(llm.totals(), attachments_mod.usage_of(recs))

    try:
        decision = convo.route(_cturns(cid), asked)
        kind = decision["kind"]
        _cset(cid, status=f"working:{kind}", current={"question": message, **decision,
                                                        "attachments": turn.get("attachments", [])})

        if kind == "follow_up":
            text, _ = convo.answer_follow_up(_cturns(cid), asked)
            _append_turn(cid, convo.finish_turn(
                turn, kind=kind, why=decision["why"], question=message, answer=text,
                usage=spent()))
            _cset(cid, status="idle", current=None)
            return

        # A run gets its OWN graph session. Registered in SESSIONS as well so the
        # Approvals tab keeps working exactly as it does for a one-shot run.
        sid = uuid.uuid4().hex[:12]
        _set(sid, status="running", question=message, conversation=cid,
             pending=None, report=None, system=system)
        with SqliteSaver.from_conn_string(DB) as cp:
            out = graph().compile(checkpointer=cp).invoke(
                {"question": asked, "session_id": sid, "system": system, "findings": [], "visited": [],
                 "transcript": []}, {"configurable": {"thread_id": sid}})

        if "__interrupt__" in out:
            # The scenario proposal is waiting on a human. The DIAGNOSIS is finished
            # and is what the operator asked for, so the turn closes with it rather
            # than leaving the conversation hanging on an admin decision.
            _set(sid, status="awaiting_approval", usage=spent(),
                 pending=out["__interrupt__"][0].value)
            state = _live_state(sid)
            out = {**state, "report": state.get("report")}
        else:
            _set(sid, status="done", pending=None, report=out.get("report"),
                 usage=spent(), saved=bool(out.get("saved")))

        _append_turn(cid, convo.finish_turn(
            turn, kind=kind, why=decision["why"], question=message, session_id=sid,
            report=out.get("report"), findings=out.get("findings", []),
            transcript=out.get("transcript", []), blocked=out.get("blocked", []),
            agents=[t.get("agent") for t in out.get("transcript", [])],
            usage=spent()))
        _cset(cid, status="idle", current=None)
    except Exception as e:  # noqa: BLE001 - surfaced in the UI, not swallowed
        # The failed turn is still recorded: it spent tokens and took time, and a
        # conversation that drops a turn on the floor cannot be read back afterwards.
        _append_turn(cid, convo.finish_turn(
            turn, kind="error", question=message, usage=spent(),
            error=f"{type(e).__name__}: {e}"))
        _cset(cid, status="error", current=None,
              error=f"{type(e).__name__}: {e}", detail=traceback.format_exc()[-2000:])


def _live_state(sid: str) -> dict:
    """Read accumulated state from the checkpoint so the UI can watch agents fire."""
    try:
        with SqliteSaver.from_conn_string(DB) as cp:
            snap = graph().compile(checkpointer=cp).get_state(
                {"configurable": {"thread_id": sid}})
    except Exception:  # noqa: BLE001
        return {}
    v = snap.values or {}
    transcript = v.get("transcript", [])
    # A running session has no final total yet — llm.totals() belongs to the worker
    # thread, not to this request. The checkpoint is what both threads can see, so the
    # live figure is summed from the agents that have reported so far and lands on the
    # final number once the run writes its own.
    running = dict.fromkeys(("input", "output", "cache_read"), 0)
    for t in transcript:
        for k in running:
            running[k] += int((t.get("usage") or {}).get(k) or 0)
    return {
        "visited": v.get("visited", []),
        "next": list(snap.next or []),
        "findings": [{"agent": f.get("agent"), "tool": f.get("tool"), "ok": f.get("ok")}
                     for f in v.get("findings", [])],
        "transcript": transcript,
        "usage": running,
        "report": v.get("report"),
    }


class RunReq(BaseModel):
    question: str
    attachments: list[str] = []   # ids from POST /api/attachments
    system: str | None = None     # else: named in the question, or the only one there is


def _pick_system(text: str, explicit: str | None, remembered: str | None = None) -> str | None:
    """The session's system, or a 400 saying exactly what to add."""
    try:
        chosen, why = inventory.pick_system(text, explicit or None, remembered)
    except inventory.InventoryError as e:
        raise HTTPException(400, f"the inventory does not load: {e}") from e
    if why:
        raise HTTPException(400, why)
    return chosen


class DecideReq(BaseModel):
    approved: bool
    edited_md: str | None = None


# The SDK resolves credentials in this order (first match wins): ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN, the active OAuth profile from `ant auth login`, Workload Identity
# Federation. Checking only the first one told an operator with a working OAuth profile
# that the console was "not ready" and disabled the Run button.
_WIF_VARS = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
             "ANTHROPIC_SERVICE_ACCOUNT_ID")


def _oauth_profile() -> str | None:
    """Name of a stored `ant auth login` profile, if one is on disk."""
    cfg = os.environ.get("ANTHROPIC_CONFIG_DIR")
    base = Path(cfg) if cfg else (Path(os.environ.get("APPDATA", "")) / "Anthropic"
                                  if os.name == "nt" else Path.home() / ".config/anthropic")
    creds = base / "credentials"
    if not creds.is_dir():
        return None
    if wanted := os.environ.get("ANTHROPIC_PROFILE"):
        return wanted if (creds / f"{wanted}.json").exists() else None
    found = sorted(p.stem for p in creds.glob("*.json"))
    return found[0] if found else None


KEY_NAME = "ANTHROPIC_API_KEY"


def _resolve() -> dict:
    """Which credential source the SDK would use, without making a request."""
    warning = ""
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        warning = ("Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are set. The SDK "
                   "sends both and the API rejects that — unset one.")
    if "ANTHROPIC_API_KEY" in os.environ and not os.environ["ANTHROPIC_API_KEY"]:
        return {"ready": False, "source": None,
                "warning": "ANTHROPIC_API_KEY is set but empty. An empty value still wins "
                           "its place in the resolution order and authenticates with an "
                           "empty key — unset it to fall through to a profile."}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"ready": True, "source": "ANTHROPIC_API_KEY", "warning": warning}
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {"ready": True, "source": "ANTHROPIC_AUTH_TOKEN", "warning": warning}
    if profile := _oauth_profile():
        return {"ready": True, "source": f"OAuth profile ({profile})", "warning": warning}
    if all(os.environ.get(v) for v in _WIF_VARS) and (
            os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE")
            or os.environ.get("ANTHROPIC_IDENTITY_TOKEN")):
        return {"ready": True, "source": "workload identity federation", "warning": warning}
    return {"ready": False, "source": None, "warning": warning}


# Whether the value in os.environ[KEY_NAME] is one this console put there. Clearing the
# stored key must not unset a credential the console does not own — an exported key is
# the operator's, and a .env key belongs to the file, which is still there afterwards.
_promoted = False


def _promote_stored_key() -> bool:
    """Put a console-entered key into os.environ, but only as a LAST resort.

    The SDK reads os.environ and knows nothing about secrets.local.env, so a stored key
    has to be promoted to work at all. It is promoted only when nothing else resolves,
    because an unconditional assignment would take precedence over the operator's own
    exported key and over an OAuth profile — silently authenticating as somebody else
    with a value they set once, months ago, through a web form.
    """
    global _promoted
    if _resolve()["ready"]:
        return False
    stored = secrets_store.load().get(KEY_NAME)
    if not stored:
        return False
    os.environ[KEY_NAME] = stored
    _promoted = True
    llm.reset_client()
    return True


def _mask(value: str) -> str:
    """Enough to recognise which key this is, not enough to use it."""
    return f"{value[:12]}{'•' * 8}{value[-4:]}" if len(value) > 20 else "•" * 12


def _credentials() -> dict:
    """Resolution status for the UI, after giving a stored key its chance."""
    _promote_stored_key()
    out = _resolve()
    stored = secrets_store.load().get(KEY_NAME)
    if stored and out["source"] == KEY_NAME and os.environ.get(KEY_NAME) == stored:
        # The value in the environment is the one this console put there.
        out["source"] = "secrets.local.env"
    out["stored"] = bool(stored)
    out["masked"] = _mask(stored) if stored else None
    # There is no login on this server, no CORS policy and no auth check anywhere. The
    # entire trust model is uvicorn's default bind, and an API key is spendable by anyone
    # who reads it — unlike a device password, which at least needs to reach the LAN.
    out["localhost_only"] = ("This console has no authentication. Serve it on 127.0.0.1 "
                             "only — never --host 0.0.0.0.")
    return out


def _effective_desc(name: str, entry: dict) -> str:
    over = config_store.get("tool_descriptions", {})
    return over.get(name) or entry["schema"].get("description", "")


@app.get("/api/meta")
def meta() -> dict:
    try:
        systems = sorted(inventory.systems())
    except inventory.InventoryError:
        systems = []
    return {
        "mode": transport.MODE,
        "model": llm.model(),
        "systems": systems,
        "credentials": _credentials(),
        "agents": [{"name": n, "prompt": a["prompt"], "tools": a.get("tools", [])}
                   for n, a in agents_mod.agents().items()],
        "order": agents_mod.order(),
        "requires": agents_mod.requires(),
        "supervisor_picks": agents_mod.supervisor_picks(),
        "tools": [{"name": n, "side_effect": t["side_effect"],
                   "description": _effective_desc(n, t),
                   "default_description": t["schema"].get("description", ""),
                   "params": sorted((t["schema"].get("input_schema") or {})
                                    .get("properties", {}))}
                  for n, t in sorted(REGISTRY.items())],
        "permissions": {
            "allowed_commands": transport.allowed(),
            "write_tools_registered": [n for n, t in REGISTRY.items()
                                       if t["side_effect"] != "none"],
        },
        "overridden_keys": sorted(config_store.load()),
    }


class AgentReq(BaseModel):
    prompt: str
    tools: list[str]


class FlowReq(BaseModel):
    order: list[str]
    requires: dict[str, list[str]]
    supervisor_picks: bool


class DescReq(BaseModel):
    description: str


class CommandsReq(BaseModel):
    commands: dict[str, list[str]]
    confirm: bool = False


def _bad(e: Exception):
    return HTTPException(400, str(e))


SUPERVISOR_CARD = {
    "name": "supervisor",
    "read_only": True,
    "role": ("Routing engine, not a diagnostic agent. On every hop it asks the model which "
             "of the currently ELIGIBLE agents should run next, or whether there is enough "
             "to conclude. It cannot widen eligibility: order, requires, needs_context and "
             "scope_context are evaluated in code before it is consulted, so the worst a bad "
             "routing choice can do is pick a different legal agent. It has no prompt of its "
             "own to edit and no tools — which is why it is read-only here."),
}


@app.get("/api/config")
def get_config() -> dict:
    return {"supervisor": {**SUPERVISOR_CARD,
                           "picks": agents_mod.supervisor_picks(),
                           "max_steps": agents_mod.MAX_AGENT_STEPS,
                           "eligible_pool": agents_mod.order()},
            "effective": {"agents": agents_mod.agents(), "order": agents_mod.order(),
                          "requires": agents_mod.requires(),
                          "supervisor_picks": agents_mod.supervisor_picks(),
                          "post_agents": agents_mod.post_agents(),
                          "allowed_commands": transport.allowed(),
                          "tool_descriptions": config_store.get("tool_descriptions", {})},
            "defaults": {**agents_mod.defaults(),
                         "allowed_commands": {k: list(v) for k, v in
                                              transport.DEFAULT_ALLOWED.items()}},
            "overridden_keys": sorted(config_store.load())}


@app.put("/api/config/agent/{name}")
def put_agent(name: str, req: AgentReq) -> dict:
    cur = dict(agents_mod.agents())
    cur[name] = {"prompt": req.prompt, "tools": req.tools}
    try:
        config_store.validate_agents(cur, set(REGISTRY))
        order = agents_mod.order()
        if name not in order:
            order = order + [name]
        config_store.validate_flow(order, agents_mod.requires(), set(cur))
    except config_store.ConfigError as e:
        raise _bad(e) from e
    config_store.put("agents", cur, note=f"edit agent {name}")
    config_store.put("order", order, note=f"ensure {name} in order")
    rebuild_graph()
    return {"ok": True, "agents": cur, "order": order}


@app.delete("/api/config/agent/{name}")
def delete_agent(name: str) -> dict:
    cur = {k: v for k, v in agents_mod.agents().items() if k != name}
    order = [a for a in agents_mod.order() if a != name]
    req = {k: [d for d in v if d != name]
           for k, v in agents_mod.requires().items() if k != name}
    try:
        config_store.validate_agents(cur, set(REGISTRY))
        config_store.validate_flow(order, req, set(cur))
    except config_store.ConfigError as e:
        raise _bad(e) from e
    config_store.put("agents", cur, note=f"delete agent {name}")
    config_store.put("order", order, note="drop from order")
    config_store.put("requires", req, note="drop from requires")
    rebuild_graph()
    return {"ok": True}


@app.put("/api/config/post_agent/{name}")
def put_post_agent(name: str, req: DescReq) -> dict:
    """Post-synthesis agents (e.g. customer_communicator) — prompt only, no tools."""
    cur = dict(agents_mod.post_agents())
    if name not in cur:
        raise HTTPException(404, f"no post agent {name!r}")
    cur[name] = {**cur[name], "prompt": req.description, "tools": []}
    try:
        config_store.validate_post_agents(cur)
    except config_store.ConfigError as e:
        raise _bad(e) from e
    config_store.put("post_agents", cur, note=f"edit post agent {name}")
    return {"ok": True}


@app.put("/api/config/flow")
def put_flow(req: FlowReq) -> dict:
    try:
        config_store.validate_flow(req.order, req.requires, set(agents_mod.agents()))
    except config_store.ConfigError as e:
        raise _bad(e) from e
    config_store.put("order", req.order, note="edit order")
    config_store.put("requires", req.requires, note="edit requires")
    config_store.put("supervisor_picks", req.supervisor_picks, note="edit routing mode")
    rebuild_graph()
    return {"ok": True}


@app.put("/api/config/tool/{name}")
def put_tool_desc(name: str, req: DescReq) -> dict:
    if name not in REGISTRY:
        raise HTTPException(404, f"no registered tool {name!r}")
    over = dict(config_store.get("tool_descriptions", {}))
    if req.description.strip():
        over[name] = req.description
    else:
        over.pop(name, None)      # empty reverts to the code default
    config_store.put("tool_descriptions", over, note=f"edit description {name}")
    return {"ok": True, "description": _effective_desc(name, REGISTRY[name])}


@app.put("/api/config/commands")
def put_commands(req: CommandsReq) -> dict:
    try:
        warnings = config_store.validate_commands(req.commands)
    except config_store.ConfigError as e:
        raise _bad(e) from e
    if warnings and not req.confirm:
        return {"ok": False, "needs_confirmation": True, "warnings": warnings}
    config_store.put("allowed_commands", req.commands, note="edit allowed commands")
    return {"ok": True, "warnings": warnings}


@app.post("/api/config/reset")
def reset_config(key: str | None = None) -> dict:
    config_store.clear(key)
    rebuild_graph()
    return {"ok": True, "overridden_keys": sorted(config_store.load())}


@app.get("/api/config/audit")
def audit() -> dict:
    p = ROOT / "config_audit.jsonl"
    if not p.exists():
        return {"entries": []}
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return {"entries": [{"ts": r["ts"], "by": r["by"], "change": r["change"]}
                        for r in rows[-100:]][::-1]}


@app.get("/api/graph")
def graph_shape() -> dict:
    """Structure for the UI diagram and the visual flow editor."""
    ags, req = agents_mod.agents(), agents_mod.requires()
    order = agents_mod.order()
    ranked = sorted(ags, key=lambda n: order.index(n) if n in order else 1e6)
    return {
        "agents": [{"name": n, "tools": ags[n].get("tools", []),
                    "requires": req.get(n, []), "in_order": n in order,
                    "needs_context": agents_mod.needs_context().get(n, []),
                    "scope_context": agents_mod.scope_context().get(n, [])}
                   for n in ranked],
        "order": order,
        "supervisor_picks": agents_mod.supervisor_picks(),
        "post_agents": [{"name": n, "prompt": a.get("prompt", "")}
                        for n, a in agents_mod.post_agents().items()],
        "needs_context": agents_mod.needs_context(),
        "scope_context": agents_mod.scope_context(),
        "max_steps": agents_mod.MAX_AGENT_STEPS,
    }


@app.post("/api/run")
def run(req: RunReq) -> dict:
    if not _credentials()["ready"]:
        raise HTTPException(400, "No Anthropic credentials resolve in this environment. "
                                 "Export ANTHROPIC_API_KEY, or run `ant auth login` to "
                                 "store an OAuth profile the SDK picks up automatically.")
    recs = _attachments(req.attachments)
    if not req.question.strip() and not recs:
        raise HTTPException(400, "describe the problem, or attach a screenshot of it")
    system = _pick_system(req.question, req.system)
    sid = uuid.uuid4().hex[:12]
    _set(sid, status="running", question=req.question, pending=None, report=None,
         attachments=attachments_mod.public(recs), system=system)
    threading.Thread(target=_run, args=(sid, {
        "question": attachments_mod.compose(req.question, recs), "session_id": sid,
        "system": system,
        "findings": [], "visited": [], "transcript": []}), daemon=True).start()
    return {"session_id": sid}


class MessageReq(BaseModel):
    message: str
    attachments: list[str] = []   # ids from POST /api/attachments
    system: str | None = None


def _attachments(ids: list[str]) -> list[dict]:
    try:
        return attachments_mod.load_many(ids)
    except attachments_mod.AttachmentError as e:
        raise HTTPException(400, str(e)) from e


class AttachmentReq(BaseModel):
    name: str
    media_type: str = ""
    data: str                     # base64; JSON keeps this free of a multipart dependency


@app.post("/api/attachments")
def attachment_add(req: AttachmentReq) -> dict:
    """Store an upload and describe it now, so the operator reads what the agents will
    read before sending — and can drop it if the description missed the point."""
    try:
        return attachments_mod.save(req.name, req.media_type, req.data,
                                    describe=_describe)
    except attachments_mod.AttachmentError as e:
        raise HTTPException(400, str(e)) from e


def _describe(data: bytes, media_type: str, name: str) -> tuple[str, dict]:
    if not _credentials()["ready"]:
        raise attachments_mod.AttachmentError(
            "describing an image needs Anthropic credentials, and none resolve here")
    # Its own thread-local count, so these tokens never leak into a turn's total; the
    # record carries them to whichever session the attachment is sent with.
    llm.reset_usage()
    return llm.describe_attachment(data, media_type, name)


@app.get("/api/attachments/{aid}/file")
def attachment_file(aid: str) -> FileResponse:
    try:
        rec = attachments_mod.load(aid)
    except attachments_mod.AttachmentError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(attachments_mod.file_path(aid), media_type=rec["media_type"],
                        filename=rec["file"],
                        content_disposition_type="inline")


def _require_credentials() -> None:
    if not _credentials()["ready"]:
        raise HTTPException(400, "No Anthropic credentials resolve in this environment. "
                                 "Export ANTHROPIC_API_KEY, or run `ant auth login` to "
                                 "store an OAuth profile the SDK picks up automatically.")


@app.post("/api/conversation")
def conversation_new() -> dict:
    cid = uuid.uuid4().hex[:12]
    _cset(cid, created_at=time.time(), status="idle", turns=[], totals=convo.totals([]))
    return {"conversation_id": cid}


@app.post("/api/conversation/{cid}/message")
def conversation_send(cid: str, req: MessageReq) -> dict:
    _require_credentials()
    msg = (req.message or "").strip()
    recs = _attachments(req.attachments)
    if not msg and not recs:
        raise HTTPException(400, "an empty message has nothing to diagnose")
    # A conversation stays on the system it started with unless a message names another.
    earlier = [t.get("system") for t in _cturns(cid) if t.get("system")]
    system = _pick_system(msg, req.system, earlier[-1] if earlier else None)
    with _lock:
        c = CONVERSATIONS.get(cid)
        if c is None:
            raise HTTPException(404, "unknown conversation")
        if str(c.get("status", "")).startswith("working"):
            raise HTTPException(409, "this conversation is still working on the last "
                                     "message")
    _cset(cid, status="working", error=None,
          current={"question": msg, "attachments": attachments_mod.public(recs)})
    threading.Thread(target=_converse, args=(cid, msg, recs, system), daemon=True).start()
    return {"ok": True}


@app.get("/api/conversation/{cid}")
def conversation_get(cid: str) -> dict:
    with _lock:
        c = CONVERSATIONS.get(cid)
        if c is None:
            raise HTTPException(404, "unknown conversation")
        c = json.loads(json.dumps(c, default=str))
    # A run turn in flight has no entry in `turns` yet; the checkpoint is the only
    # place its progress is visible, and watching agents fire is half the point.
    live = (c.get("current") or {}).get("session_id")
    running = [s for s, v in SESSIONS.items()
               if v.get("conversation") == cid and v.get("status") == "running"]
    if not live and running:
        live = running[-1]
    c["live"] = _live_state(live) if live else {}
    return c


@app.get("/api/conversations")
def conversations() -> dict:
    with _lock:
        return {"conversations": [
            {"id": k, "created_at": v.get("created_at"), "status": v.get("status"),
             "totals": v.get("totals") or {},
             "opened_with": (v.get("turns") or [{}])[0].get("question")}
            for k, v in sorted(CONVERSATIONS.items(),
                               key=lambda kv: -(kv[1].get("created_at") or 0))]}


class ModelReq(BaseModel):
    model: str


@app.get("/api/models")
def models(refresh: bool = False) -> dict:
    """What this account may use, plus which one is in force."""
    return {**llm.models(refresh=refresh), "current": llm.model(),
            "default": llm.DEFAULT_MODEL}


@app.put("/api/model")
def set_model(req: ModelReq) -> dict:
    """Change the model for every later call. Persisted, and written to the audit log.

    Validated against the catalogue rather than accepted as typed: a model id with a
    date suffix or a typo is accepted silently by nothing and fails at the next run,
    minutes later, as an opaque 404 in the middle of a diagnosis.
    """
    want = (req.model or "").strip()
    cat = llm.models()
    known = {m["id"] for m in cat["models"]}
    if want not in known:
        # When the list itself is the fallback we cannot be sure it is wrong, so say so
        # rather than blocking an operator whose account has a model we could not list.
        detail = (f"{want!r} is not in this account's model list: {sorted(known)}"
                  if cat["source"] == "api" else
                  f"{want!r} is not in the built-in list and the account's models could "
                  f"not be fetched ({cat.get('why')}) — check credentials, or use one of "
                  f"{sorted(known)}")
        raise HTTPException(400, detail)
    config_store.put("model", want, note=f"model -> {want}")
    # Caches are model-scoped, so the next run rebuilds the cached prefix from scratch.
    log.info("model set to %s; the prompt cache starts cold", want)
    return {"ok": True, "current": llm.model()}


@app.delete("/api/model")
def clear_model() -> dict:
    """Back to AGENT_MODEL, or the code default if that is unset."""
    config_store.clear("model")
    return {"ok": True, "current": llm.model()}


@app.get("/api/sessions")
def sessions() -> dict:
    with _lock:
        return {"sessions": [{"id": k, **{x: y for x, y in v.items()
                                          if x in ("status", "question", "error")}}
                             for k, v in sorted(SESSIONS.items())]}


@app.get("/api/session/{sid}")
def session(sid: str) -> dict:
    with _lock:
        s = dict(SESSIONS.get(sid) or {})
    if not s:
        raise HTTPException(404, "unknown session")
    return {"id": sid, **s, "state": _live_state(sid)}


@app.post("/api/session/{sid}/decide")
def decide(sid: str, req: DecideReq) -> dict:
    with _lock:
        s = SESSIONS.get(sid)
    if not s or s.get("status") != "awaiting_approval":
        raise HTTPException(400, "no approval pending for this session")
    sc = dict(s["pending"]["scenario"])
    if req.approved and req.edited_md:
        sc["_md"] = req.edited_md
    _set(sid, status="running", pending=None)
    threading.Thread(target=_run, args=(
        sid, Command(resume={"approved": req.approved, "edited": sc})), daemon=True).start()
    return {"ok": True}


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _slugify(raw: str) -> str:
    """Free text in, kebab-case out.

    Applied ONLY when a case is created, because that is the one moment the filename is
    chosen. Every other route still resolves a name strictly through _kb_path: slugifying
    on read or delete would mean a request for one file could silently reach another, and
    the traversal guard would have nothing left to refuse.
    """
    txt = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")[:64].strip("-")


def _kb_path(name: str, must_exist: bool = True) -> Path:
    """Resolve a KB document name, refusing anything that escapes kb/."""
    if not _SLUG.match(name or ""):
        raise HTTPException(400, "name must be kebab-case: lowercase letters, digits, hyphens")
    p = (KB / f"{name}.md").resolve()
    if p.parent != KB.resolve():
        raise HTTPException(400, "path escapes the knowledge base directory")
    if must_exist and not p.exists():
        raise HTTPException(404, "no such document")
    return p


class SectionsReq(BaseModel):
    title: str = ""
    intro: str = ""
    root_cause: str = ""
    checks: str = ""
    fix: str = ""
    extra: str = ""


class KbReq(BaseModel):
    """A case carries TOPICS, not a domain.

    Deliberately a different word from `domain`: a routing domain in graph.DOMAINS
    decides which agent may run, and a topic here only decides which past case a ticket
    matches. Calling both "domain" invited exactly the confusion of expecting a new tag
    to summon an expert. `domains`/`domain` are still read so an older caller keeps
    working.
    """
    topics: list[str] = []
    domains: list[str] = []   # legacy
    domain: str = ""          # legacy
    symptoms: list[str] = []
    sections: SectionsReq | None = None
    body: str = ""            # accepted when a caller has raw markdown instead

    def markdown(self) -> str:
        return _join_body(self.sections.model_dump()) if self.sections else self.body

    def tags(self) -> list[str]:
        out = [t.strip() for t in (self.topics or self.domains or []) if t and t.strip()]
        if not out and self.domain.strip():
            out = [self.domain.strip()]
        return list(dict.fromkeys(out))          # de-duplicated, order preserved


class KbNewReq(KbReq):
    name: str


_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)

# The body of a case, as separate fields instead of one markdown box. The three headings
# are the shape graph._render writes, so an agent-proposed case and a hand-written one
# open in the same form.
CASE_SECTIONS = {"root cause": "root_cause",
                 "checks (read-only)": "checks",
                 "fix": "fix"}
_H1 = re.compile(r"(?m)^#\s+(.+?)\s*$")
_H2 = re.compile(r"(?m)^##\s+(.+?)\s*$")


def _split_body(body: str) -> dict:
    """Markdown body -> titled sections.

    Anything under a heading this form does not know about is kept verbatim in `extra`
    rather than dropped — a case may carry an Evidence section, and an editor that
    silently deletes what it cannot display is worse than one big textarea.
    """
    out = {k: "" for k in ("title", "intro", "root_cause", "checks", "fix", "extra")}
    text = body or ""
    if m := _H1.search(text):
        out["title"] = m.group(1)
        text = text[m.end():]

    heads = list(_H2.finditer(text))
    out["intro"] = (text[:heads[0].start()] if heads else text).strip()
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunk = text[h.end():end].strip("\n").rstrip()
        key = CASE_SECTIONS.get(h.group(1).strip().lower())
        if key and not out[key]:
            out[key] = chunk
        else:                        # unknown heading, or a duplicate of a known one
            out["extra"] = (out["extra"] + f"\n\n## {h.group(1)}\n\n{chunk}").strip()
    return out


def _join_body(sec: dict) -> str:
    """Sections -> markdown body. One canonical shape, so the join is idempotent."""
    # strip("\n").rstrip() and NOT .strip(): the checks section is an indented code
    # block, so removing leading spaces would turn its commands into prose.
    g = lambda k: str(sec.get(k) or "").strip("\n").rstrip()  # noqa: E731
    title, intro = str(sec.get("title") or "").strip(), str(sec.get("intro") or "").strip()
    parts = [f"# {title}" if title else ""]
    if intro:
        parts.append(intro)
    for label, key in (("Root cause", "root_cause"),
                       ("Checks (read-only)", "checks"), ("Fix", "fix")):
        parts.append(f"## {label}\n\n{g(key)}".rstrip())
    if g("extra"):
        parts.append(g("extra"))
    return "\n\n".join(p for p in parts if p) + "\n"


def _as_list(v) -> list[str]:
    """A topic key may hold one string or several. Both shapes load."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


def _parse_doc(text: str) -> dict:
    """Split YAML frontmatter from the markdown body.

    Documents written before the structured editor have no frontmatter at all. They
    are not broken and must not be treated as such: the whole file is the body, and
    the fields come back empty for the editor to fill in.

    `topics` is always a list. `domain:` and `domains:` are read as older spellings of
    the same field, so cases written before the rename keep opening.
    """
    empty = {"topics": [], "symptoms": [], "created": "", "body": text or ""}
    m = _FM.match(text or "")
    if not m:
        return empty
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return empty
    if not isinstance(meta, dict):
        return empty
    sym = meta.get("symptoms") or []
    if isinstance(sym, str):
        sym = [x.strip() for x in sym.splitlines() if x.strip()]
    topics = (_as_list(meta.get("topics")) or _as_list(meta.get("domains"))
              or _as_list(meta.get("domain")))
    created = meta.get("created")
    # YAML reads an unquoted timestamp as a datetime; either way it leaves as text.
    created = created.isoformat() if hasattr(created, "isoformat") else str(created or "")
    return {"topics": topics,
            "symptoms": [str(x) for x in sym if str(x).strip()],
            "created": created,
            "body": text[m.end():]}


def _render_doc(topics, symptoms: list[str], body: str, created: str = "") -> str:
    """Frontmatter + body. Emitted only when there is something to record.

    Always a list under `topics:`, however many there are — one shape to write and one
    to read back, so the round-trip cannot depend on how many tags a case happens to
    carry.
    """
    tags = _as_list(topics)
    meta = {}
    if created:
        meta["created"] = str(created)
    if tags:
        meta["topics"] = tags
    clean = [s.strip() for s in symptoms if s and s.strip()]
    if clean:
        meta["symptoms"] = clean
    body = (body or "").strip()
    if not meta:
        return body + "\n"
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{body}\n"


_TOPIC = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _declared_topics() -> list[str]:
    """Topics created in the console that no case uses yet."""
    if not TOPICS_FILE.exists():
        return []
    try:
        data = json.loads(TOPICS_FILE.read_text() or "[]")
    except json.JSONDecodeError:
        return []
    return [t for t in data if isinstance(t, str) and _TOPIC.match(t)]


def _used_topics() -> set[str]:
    return {t for p in KB.glob("*.md") for t in _parse_doc(p.read_text())["topics"]} \
        if KB.exists() else set()


def _seeded_topics() -> set[str]:
    """The routing domains and whatever the inventory declares — there on a fresh install."""
    declared = {r.get("domain") for r in inventory.load().values()}
    return {t for t in (set(graph_mod.DOMAINS) | declared) if t}


def _kb_topics() -> list[str]:
    return sorted(_seeded_topics() | _used_topics() | set(_declared_topics()))


def _removable_topics() -> list[str]:
    """Only what a person created and nothing uses.

    A topic a case carries is not removable here: it would reappear the moment the list
    is rebuilt, so offering the button would be a lie. Delete it from the cases instead.
    """
    return sorted(set(_declared_topics()) - _used_topics() - _seeded_topics())


def _topics_payload() -> dict:
    return {"all_topics": _kb_topics(), "removable_topics": _removable_topics()}


class TopicReq(BaseModel):
    name: str


@app.post("/api/topics")
def topic_add(req: TopicReq) -> dict:
    name = (req.name or "").strip().lower()
    if not _TOPIC.match(name):
        raise HTTPException(400, "a topic is lowercase letters, digits, - or _, "
                                 "starting with a letter (e.g. tower)")
    if name not in _kb_topics():
        TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOPICS_FILE.write_text(json.dumps(sorted(set(_declared_topics()) | {name}),
                                          indent=2) + "\n")
        log.info("topic added: %s", name)
    return {"ok": True, **_topics_payload()}


@app.delete("/api/topics/{name}")
def topic_delete(name: str) -> dict:
    if name in _used_topics():
        raise HTTPException(400, f"{name!r} is in use by a case; remove it there first")
    rest = [t for t in _declared_topics() if t != name]
    TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_FILE.write_text(json.dumps(rest, indent=2) + "\n")
    log.info("topic removed: %s", name)
    return {"ok": True, **_topics_payload()}


@app.get("/api/system-model")
def system_model() -> dict:
    """Read-only on purpose.

    It is derived from the code and a test pins its facts against the source. Hand-editing
    it here is how that drifts, so the console shows it and does not offer to save it.
    """
    return {"path": str(SYSTEM_MODEL),
            "exists": SYSTEM_MODEL.exists(),
            "text": SYSTEM_MODEL.read_text() if SYSTEM_MODEL.exists() else "",
            "read_only": True,
            "why_read_only": ("Generated from the code. Edit the source and regenerate, "
                              "or the document drifts from what the system actually does.")}


@app.get("/api/kb")
def kb_list() -> dict:
    KB.mkdir(parents=True, exist_ok=True)
    links = _case_links()
    docs = []
    for p in sorted(KB.glob("*.md")):
        d = _parse_doc(p.read_text())
        created, source = _case_created(p, d["created"])
        ln = links.get(p.stem, {})
        docs.append({"name": p.stem, "bytes": p.stat().st_size,
                     "topics": d["topics"],
                     "symptom_count": len(d["symptoms"]),
                     "structured": bool(d["topics"] or d["symptoms"]),
                     "created": created, "created_source": source,
                     "sessions": len(ln.get("diagnosed", ())),
                     "search_hits": len(ln.get("searched", ()))})
    return {"docs": docs, **_topics_payload()}


_GIT_ADDED: dict[str, str] = {}


def _case_created(p: Path, recorded: str) -> tuple[str, str]:
    """When the case was created, and how sure that is.

    "recorded" is the frontmatter, written when the case is created. Cases older than
    that field get an estimate: the commit that added the file, else its mtime — which
    an edit moves, so it is the least trustworthy and labelled as such.
    """
    if recorded:
        return recorded, "recorded"
    if p.name not in _GIT_ADDED:
        try:
            out = subprocess.run(
                ["git", "log", "--diff-filter=A", "--format=%aI", "-1", "--", str(p)],
                cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        _GIT_ADDED[p.name] = out[:19]
    if _GIT_ADDED[p.name]:
        return _GIT_ADDED[p.name], "git"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)), "file"


def _case_links() -> dict[str, dict[str, set]]:
    """Per case: sessions DIAGNOSED as it, and sessions whose runbook search found it.

    Diagnosed means the final report named it in matched_case, or the session is the
    one that created it. A search hit is only lexical — the same words — so it is
    counted apart and never promoted to "this was that fault".
    """
    out: dict[str, dict[str, set]] = {}
    add = lambda case, kind, sid: out.setdefault(  # noqa: E731
        str(case), {"diagnosed": set(), "searched": set()})[kind].add(sid)
    TRACES.mkdir(exist_ok=True)
    for p in TRACES.glob("*.jsonl"):
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line) if line.strip() else {}
            except json.JSONDecodeError:
                continue
            r = e.get("report") if isinstance(e.get("report"), dict) else {}
            if r.get("matched_case"):
                add(r["matched_case"], "diagnosed", p.stem)
            sc = r.get("propose_scenario") or {}
            if e.get("saved") and isinstance(sc, dict) and sc.get("id"):
                slug = re.sub(r"[^a-z0-9-]+", "-", str(sc["id"]).lower()).strip("-")
                add(slug, "diagnosed", p.stem)
            for f in e.get("findings") or []:
                if isinstance(f, dict) and f.get("tool") == "search_runbook":
                    for h in (f.get("data") or {}).get("hits") or []:
                        if isinstance(h, dict) and h.get("doc"):
                            add(h["doc"], "searched", p.stem)
    return out


@app.get("/api/kb/{name}")
def kb_doc(name: str) -> dict:
    p = _kb_path(name)
    text = p.read_text()
    parsed = _parse_doc(text)
    return {"name": name, **parsed, "sections": _split_body(parsed["body"]),
            "text": text, **_topics_payload()}


@app.put("/api/kb/{name}")
def kb_save(name: str, req: KbReq) -> dict:
    """Edit a runbook document. It is re-read into the cached prompt on the next session."""
    p = _kb_path(name)
    # The form never sends `created`, so it is carried over from the file — an edit is
    # not a new case.
    created = _parse_doc(p.read_text())["created"]
    text = _render_doc(req.tags(), req.symptoms, req.markdown(), created)
    p.write_text(text)
    log.info("kb edited: %s (%d bytes)", p.name, len(text))
    return {"ok": True, "name": name, "bytes": len(text)}


@app.post("/api/kb")
def kb_create(req: KbNewReq) -> dict:
    KB.mkdir(parents=True, exist_ok=True)
    name = _slugify(req.name)
    if not name:
        raise HTTPException(400, f"{req.name!r} has no letters or digits to make a name "
                                 f"from — describe the fault in words")
    p = _kb_path(name, must_exist=False)      # still the last word on where this lands
    if p.exists():
        raise HTTPException(409, f"{name}.md already exists")
    body = req.markdown().strip() or _join_body({"title": name.replace("-", " ").title()})
    p.write_text(_render_doc(req.tags(), req.symptoms, body,
                             time.strftime("%Y-%m-%dT%H:%M:%S")))
    log.info("kb created: %s", p.name)
    return {"ok": True, "name": name}


@app.delete("/api/kb/{name}")
def kb_delete(name: str) -> dict:
    p = _kb_path(name)
    p.unlink()
    log.info("kb deleted: %s", p.name)
    return {"ok": True}


@app.get("/api/traces")
def traces() -> dict:
    TRACES.mkdir(exist_ok=True)
    # Read from the file on every call, so a system added or removed in the inventory
    # (here or by hand) is in the Trace filter on the next load, with no restart.
    try:
        systems = sorted(str(k) for k in _inv_doc()["systems"])
    except Exception:  # noqa: BLE001 - a broken inventory must not hide the traces
        log.exception("traces: inventory unreadable, system filter left empty")
        systems = []
    return {"traces": trace_tag.describe(TRACES), "systems": systems}


@app.get("/api/traces/{tid}")
def trace(tid: str) -> JSONResponse:
    p = (TRACES / f"{tid}.jsonl").resolve()
    if p.parent != TRACES.resolve() or not p.exists():
        raise HTTPException(404, "no such trace")
    meta = next((r for r in trace_tag.describe(TRACES) if r["id"] == tid), {})
    entries = [json.loads(l) for l in p.read_text().splitlines() if l]
    return JSONResponse({"id": tid, "tag": meta.get("tag"), "meta": meta,
                         "summary": trace_summary.summarize(entries),
                         "sections": trace_summary.sections(entries),
                         "entries": entries})


# ---------------------------------------------------------------------------
# Credentials
#
# Same split the device passwords use: the value lives in secrets.local.env (0600,
# gitignored) and never in a git-tracked file. Two things differ, because this key is a
# billing credential rather than a way onto a customer's LAN — it is masked on the way
# back out rather than rendered, and it is promoted into os.environ only when nothing
# else resolves, so it can never shadow the operator's own credential.
# ---------------------------------------------------------------------------

class KeyReq(BaseModel):
    key: str


@app.get("/api/credentials")
def credentials() -> dict:
    return _credentials()


@app.put("/api/credentials")
def set_key(req: KeyReq) -> dict:
    key = req.key.strip()
    if not key:
        raise HTTPException(400, "empty key; use Clear to remove the stored one")
    if any(c.isspace() for c in key):
        raise HTTPException(400, "key contains whitespace — it was probably truncated or "
                                 "pasted with a line break")
    try:
        secrets_store.put(KEY_NAME, key)
    except secrets_store.SecretError as e:
        raise HTTPException(400, str(e)) from e
    # An exported variable outranks the file everywhere else in this codebase, and a
    # stale one here would mean the key just typed appears stored but is never sent. The
    # console is the writer, so it takes the value it was given.
    global _promoted
    os.environ[KEY_NAME] = key
    _promoted = True
    llm.reset_client()
    log.info("anthropic key stored (%s)", _mask(key))
    return _credentials()


@app.delete("/api/credentials")
def clear_key() -> dict:
    """Remove the stored key and fall back to whatever else was there.

    Only un-export what this console put there: a key the operator exported is not the
    console's to remove. And after un-exporting, reload .env — a key in the file is a
    legitimate source that was merely being outranked, and it is still on disk. Without
    that reload, clearing a console key left a console with a perfectly good .env
    reporting "no credentials" until someone restarted it.
    """
    global _promoted
    removed = secrets_store.delete(KEY_NAME)
    if removed and _promoted:
        os.environ.pop(KEY_NAME, None)
        _promoted = False
        env_file.load()
    llm.reset_client()
    return _credentials()


@app.get("/api/usage")
def usage() -> dict:
    """Lifetime token totals, summed from the traces on disk.

    Traces written before usage was recorded carry no "usage" key, so fall back to
    summing the per-agent numbers in the transcript. Those older sessions therefore
    count, just without the routing and synthesis calls nothing was recording yet.
    """
    TRACES.mkdir(exist_ok=True)
    total = dict.fromkeys(("input", "output", "cache_read"), 0)
    sessions = 0
    for p in TRACES.glob("*.jsonl"):
        # One file is one session, however many lines it has: a run that paused for
        # approval, or failed, writes a line per segment, each with its own tokens.
        counted = False
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows = ([entry["usage"]] if isinstance(entry.get("usage"), dict)
                    else [t["usage"] for t in entry.get("transcript", [])
                          if isinstance(t, dict) and isinstance(t.get("usage"), dict)])
            if not rows:
                # Traces older than the transcript format carry no token data at all.
                # Counting them would label a real zero as "0 tokens over 3 sessions",
                # which reads as a broken counter rather than as missing history.
                continue
            if not counted:
                sessions, counted = sessions + 1, True
            for row in rows:
                for k in total:
                    total[k] += int(row.get(k) or 0)
    return {"lifetime": total, "sessions": sessions}


# ---------------------------------------------------------------------------
# Inventory & systems
#
# The console is the only writer. It renders a password to the admin at localhost,
# but the VALUE never lands in the registry file: secrets.local.env holds it and the
# YAML keeps only the env var name. The agent-facing view is rebuilt by inventory.py
# from a field allowlist, so nothing below can widen what a model sees.
# ---------------------------------------------------------------------------

# A leading digit is allowed: the editor numbers components 1, 2, 3 by default. These
# names are YAML keys and tool-enum values, never filesystem paths, and PyYAML quotes a
# numeric-looking key so it loads back as a string.
_INV_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# ---------------------------------------------------------------------------
# Component types
#
# What a kind of component IS: which section it lands in, the defaults it starts with,
# and the extra fields it carries. Operator-editable, because a radar gaining an
# azimuth should not be a code change — but the extras land in the namespaced `fields`
# dict, never as loose keys, so inventory.py's allowlist guarantee still holds.
# ---------------------------------------------------------------------------

# The field names are the SYSTEM'S OWN, taken from kb/system-model.md, not invented:
# alignment is written "into each sensor's own yaw-role key — `azimuth_offset` for radar,
# `yaw` for the ASU", and the generic mount key is `mount.yaw_deg`. A registry field
# called `azimuth` where the config says `azimuth_offset` is the same two-vocabularies
# trap graph.DOMAINS warns about: it reads as a match and is not one.
# The fields every component has, whatever its type. A type says which of them it
# SHOWS — a camera with no shell has no use for a username, and the ASU is a local
# Docker service with no address on the sensor LAN. Hiding is presentational only: a
# value already stored is still loaded, still saved and still reaches the model, so
# turning a field off cannot lose data somebody typed.
BUILTIN_FIELDS = [
    {"key": "address", "label": "IP address"},
    {"key": "port", "label": "Port"},
    {"key": "hardware", "label": "Hardware model"},
    {"key": "software_version", "label": "Version"},
    {"key": "web", "label": "Web address"},
    {"key": "location", "label": "Latitude / longitude"},
    {"key": "ssh", "label": "Username & password"},
]
# Not optional: the logical name is what a tool argument resolves against, and the
# server refuses a component with no hardware type.
ALWAYS_SHOWN = ("name", "type")


def _default_builtin(**off) -> dict:
    return {f["key"]: not off.get(f["key"], False) for f in BUILTIN_FIELDS}


DEFAULT_TYPES: dict[str, dict] = {
    "radar":    {"label": "Radar", "role": "sensor", "domain": "radar", "scheme": "tcp",
                 "builtin": _default_builtin(),
                 "fields": [{"key": "azimuth_offset", "label": "Azimuth offset °",
                             "default": ""},
                            {"key": "elevation", "label": "Elevation °", "default": ""}]},
    "camera":   {"label": "Camera", "role": "sensor", "domain": "camera", "scheme": "http",
                 # Optics are reached over their own HTTP API, not a shell.
                 "builtin": _default_builtin(ssh=True),
                 "fields": [{"key": "mount_yaw_deg", "label": "Mount yaw °",
                             "default": ""}]},
    # `yaw` is the ASU's own alignment key. `container` because the ASU is a local Docker
    # service, not a device on the sensor LAN — so `asu_connected: false` is a statement
    # about a container, and which one is what get_asu_service_status goes and reads.
    "acoustic": {"label": "Acoustic", "role": "sensor", "domain": "acoustic",
                 "scheme": "http",
                 # A local Docker service: no address on the sensor LAN to record,
                 # which is exactly the confusion that makes `asu_connected: false`
                 # look like a network fault.
                 "builtin": _default_builtin(address=True, ssh=True),
                 "fields": [{"key": "yaw", "label": "Yaw °", "default": ""},
                            {"key": "container", "label": "Docker container",
                             "default": "dumbo-backend"}]},
    # The config a box is SUPPOSED to run. The launcher reports the one it actually
    # started from, and the mismatch between the two is the fault that is otherwise
    # unknowable — you are looking at a node started from a different config.
    "computers": {"label": "Computers", "role": "compute", "domain": "", "scheme": "tcp",
                  "builtin": _default_builtin(),
                  "fields": [{"key": "os", "label": "OS", "default": ""},
                             {"key": "expected_config", "label": "Expected config",
                              "default": ""}]},
    # Deliberately empty. It is the catch-all: fields invented for it would be noise on
    # every component that did not fit anywhere else, and noise reaches the prompt.
    "other":    {"label": "Other", "role": "other", "domain": "", "scheme": "tcp",
                 "builtin": _default_builtin(),
                 "fields": []},
}


def component_types() -> dict:
    return config_store.get("component_types", DEFAULT_TYPES)


def _check_types(types: dict) -> dict:
    """Hold an edited catalogue to what the rest of the console can actually render."""
    if not isinstance(types, dict) or not types:
        raise HTTPException(400, "a catalogue needs at least one type")
    out: dict[str, dict] = {}
    for key, spec in types.items():
        k = str(key).strip().lower()
        if not _INV_NAME.match(k):
            raise HTTPException(400, f"type name {key!r} must be lowercase letters, "
                                     f"digits, - or _")
        if not isinstance(spec, dict):
            raise HTTPException(400, f"type {k!r} must be an object")
        role = str(spec.get("role") or inventory.DEFAULT_ROLE)
        if role not in inventory.ROLES:
            raise HTTPException(400, f"type {k!r} has role {role!r}; allowed: "
                                     f"{list(inventory.ROLES)}")
        fields, seen = [], set()
        for f in spec.get("fields") or []:
            if not isinstance(f, dict):
                raise HTTPException(400, f"type {k!r}: each field must be an object")
            fk = str(f.get("key") or "").strip().lower()
            if not _FIELD_KEY.match(fk):
                raise HTTPException(400, f"type {k!r}: field name {f.get('key')!r} must "
                                         f"be lowercase letters, digits or underscore")
            if fk in inventory.FORBIDDEN or fk in inventory._INLINE_SECRET_KEYS:
                raise HTTPException(400, f"type {k!r}: {fk!r} is a credential name; "
                                         f"secrets live in secrets.local.env")
            if fk in seen:
                raise HTTPException(400, f"type {k!r}: field {fk!r} is defined twice")
            seen.add(fk)
            default = f.get("default")
            if isinstance(default, (dict, list)):
                raise HTTPException(400, f"type {k!r}: the default for {fk!r} must be a "
                                         f"single value")
            fields.append({"key": fk, "label": str(f.get("label") or fk),
                           "default": "" if default is None else default})
        # Unknown keys are dropped rather than rejected: a catalogue saved before a
        # built-in existed should keep loading, and one saved after it was removed
        # should not wedge the editor.
        known = {f["key"] for f in BUILTIN_FIELDS}
        sent = spec.get("builtin")
        builtin = ({b: bool(sent.get(b, True)) for b in known}
                   if isinstance(sent, dict) else _default_builtin())
        out[k] = {"label": str(spec.get("label") or k).strip() or k,
                  "role": role,
                  "domain": str(spec.get("domain") or "").strip().lower(),
                  "scheme": str(spec.get("scheme") or "").strip().lower(),
                  "builtin": builtin,
                  "fields": fields}
    return out


@app.get("/api/inventory/types")
def inventory_types() -> dict:
    return {"types": component_types(), "defaults": DEFAULT_TYPES,
            "roles": list(inventory.ROLES),
            "builtin_fields": BUILTIN_FIELDS, "always_shown": list(ALWAYS_SHOWN),
            "overridden": "component_types" in config_store.load()}


@app.put("/api/inventory/types")
def inventory_types_put(req: dict) -> dict:
    checked = _check_types(req.get("types") or {})
    config_store.put("component_types", checked, note="component types")
    return {"ok": True, **inventory_types()}


# No type-only reset endpoint. "Revert everything to code defaults" on the Agents &
# permissions page already clears every override, component_types included, so a second
# way back was surface without a purpose.
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _check_coords(lat, lon, what: str) -> dict:
    """Both or neither, and on the planet. Half a coordinate points at the Atlantic."""
    if lat is None and lon is None:
        return {}
    if lat is None or lon is None:
        raise HTTPException(400, f"{what} needs both lat and lon, or neither")
    if not (-90 <= lat <= 90):
        raise HTTPException(400, f"{what}: latitude {lat} is outside -90..90")
    if not (-180 <= lon <= 180):
        raise HTTPException(400, f"{what}: longitude {lon} is outside -180..180")
    return {"lat": float(lat), "lon": float(lon)}


def _check_fields(fields, what: str) -> dict:
    """Operator-defined values, held to what can safely reach a prompt.

    Scalars only: a nested structure here would be rendered into the system prompt by
    as_prompt() as whatever str() makes of it, and a list of dicts becomes noise the
    model reads as fact. And the key is checked against the same forbidden names the
    registry refuses elsewhere — `fields` is allowlisted as a whole, so without this it
    would be the one place a password could legitimately be typed.
    """
    if not fields:
        return {}
    if not isinstance(fields, dict):
        raise HTTPException(400, f"{what}: fields must be a mapping")
    out = {}
    for k, v in fields.items():
        key = str(k).strip().lower()
        if not _FIELD_KEY.match(key):
            raise HTTPException(400, f"{what}: field name {k!r} must be lowercase "
                                     f"letters, digits or underscore")
        if key in inventory.FORBIDDEN or key in inventory._INLINE_SECRET_KEYS:
            raise HTTPException(400, f"{what}: {key!r} is a credential name; secrets "
                                     f"live in secrets.local.env, never in the registry")
        if isinstance(v, (dict, list)):
            raise HTTPException(400, f"{what}: field {key!r} must be a single value, "
                                     f"not a {type(v).__name__}")
        if v in (None, ""):
            continue
        out[key] = v
    return out


class SshReq(BaseModel):
    user: str | None = None
    port: int | None = None
    key_file: str | None = None
    password_env: str | None = None
    password: str | None = None        # write-only: stored in secrets.local.env
    clear_password: bool = False


class ComponentReq(BaseModel):
    name: str
    type: str
    lat: float | None = None
    lon: float | None = None
    # Operator-defined per-type values (az, elevation, …). Free-form by design, so the
    # registry can grow without a code change — but see _check_fields: it is still held
    # to scalars and to names that cannot be mistaken for a credential.
    fields: dict | None = None
    role: str = inventory.DEFAULT_ROLE
    domain: str | None = None
    address: str | None = None
    port: int | None = None
    scheme: str | None = None
    hardware: str | None = None
    software_version: str | None = None
    web: str | None = None
    description: str | None = None
    ssh: SshReq | None = None


class SystemReq(BaseModel):
    new_name: str | None = None   # rename: the URL carries the CURRENT name
    site: str | None = None
    lat: float | None = None
    lon: float | None = None
    description: str | None = None
    # The component gotcha runs on, and that system's gotcha config as a path ON it.
    # None = not sent, keep what the file has; "" = cleared.
    host: str | None = None
    config: str | None = None
    components: list[ComponentReq] = []
    sensors: list[ComponentReq] = []      # accepted as an alias for older callers

    def members(self) -> list[ComponentReq]:
        return self.components or self.sensors


# Offered in the editor's type dropdown. Sensor types are the config-side spellings from
# gotcha30; graph.DOMAINS also knows each one's health-side spelling, so either resolves.
TYPE_CATALOG: dict[str, list[str]] = {
    "sensor": ["magos_radar", "elm2135_radar", "asu", "python_asu", "meduza_optic",
               "python_optic_ptz", "python_optic_verification", "python_ptz_director"],
    "compute": ["compute_box", "edge_appliance", "server", "nuc", "industrial_pc"],
    "laptop": ["laptop", "workstation", "rugged_laptop"],
    "network": ["switch", "router", "firewall", "access_point", "media_converter",
                "poe_switch"],
    "power": ["ups", "poe_injector", "power_supply", "generator"],
    "other": [],
}


def _default_ssh_user() -> str:
    return str((((_inv_doc().get("defaults") or {}).get("ssh") or {}).get("user")) or "")


def _check_access(c: "ComponentReq") -> None:
    """Reject an access block that credentials() could never use.

    Validated on the way in rather than by re-validating the whole registry on the way
    out: a half-filled block somebody saved earlier must not veto an unrelated edit, and
    the operator gets the field name rather than a rollback message.
    """
    ssh = c.ssh
    if not ssh:
        return
    if ssh.port is not None and not 1 <= ssh.port <= 65535:
        raise HTTPException(400, f"component {c.name!r}: SSH port {ssh.port} is out of "
                                 f"range (1-65535)")
    user = (ssh.user or "").strip()
    if user and not inventory._USER_RX.match(user):
        raise HTTPException(400, f"component {c.name!r}: SSH user {user!r} is not a "
                                 f"valid username")
    # Only matters if a block is actually going to be written — see the port-only case.
    identifies = bool(user or ssh.key_file or ssh.password_env or ssh.password)
    if identifies and not (user or _default_ssh_user()):
        raise HTTPException(400, f"component {c.name!r}: give an SSH user, or set a "
                                 f"defaults.ssh.user in the registry — a key or password "
                                 f"with nobody to log in as cannot be used")


def _reload_inventory() -> None:
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()


def _inv_doc() -> dict:
    p = inventory.SYSTEMS
    if not p.exists():
        return {"version": 1, "systems": {}}
    doc = yaml.safe_load(p.read_text()) or {}
    doc.setdefault("systems", {})
    return doc


def _save_inv_doc(doc: dict, note: str) -> None:
    """Write, then prove the result still loads. A rejected edit is rolled back.

    inventory.load() is the real validator — it is the same code the agent path uses,
    so anything it refuses (an inline secret, a duplicate sensor name, a hostname where
    an address belongs) is refused here too rather than only at the next session.
    """
    p = inventory.SYSTEMS
    old = p.read_text() if p.exists() else None
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    try:
        _reload_inventory()
        inventory.load()
    except Exception as e:  # noqa: BLE001 - shown to the operator
        if old is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(old)
        _reload_inventory()
        raise HTTPException(400, f"rejected, nothing was changed: {e}") from e
    log.info("inventory: %s", note)


def _component_out(name: str, spec: dict, inherited: dict) -> dict:
    ssh = {**inherited, **((spec.get("access") or {}).get("ssh") or {})}
    env = ssh.get("password_env")
    return {
        "name": name, "type": spec.get("type", ""),
        "role": spec.get("role") or inventory.DEFAULT_ROLE,
        "domain": spec.get("domain"),
        "address": spec.get("address"), "port": spec.get("port"),
        "scheme": spec.get("scheme"), "hardware": spec.get("hardware"),
        "software_version": spec.get("software_version"),
        "web": spec.get("web"),
        "description": spec.get("description"),
        "lat": spec.get("lat"), "lon": spec.get("lon"),
        "fields": spec.get("fields") or {},
        "ssh": {
            "user": ssh.get("user"), "port": ssh.get("port"),
            "key_file": ssh.get("key_file"), "password_env": env,
            # Shown to the human admin at localhost, never to a model.
            "password": (secrets_store.get(env) if env else None),
            "password_in_environment": bool(env and secrets_store.is_in_environment(env)),
        } if (ssh or env) else None,
    }


@app.get("/api/inventory")
def inventory_get() -> dict:
    doc = _inv_doc()
    systems = []
    for sys_name, spec in (doc.get("systems") or {}).items():
        if not isinstance(spec, dict):
            continue
        inherited = ((spec.get("access") or {}).get("ssh") or {})
        members = spec.get("components")
        if not isinstance(members, dict):
            members = spec.get("sensors")
        if isinstance(members, dict):
            rows = [_component_out(n, sv, inherited) for n, sv in members.items()
                    if isinstance(sv, dict)]
        else:
            rows = [_component_out(sys_name, spec, {})] if "type" in spec else []
        systems.append({"name": sys_name, "site": spec.get("site"),
                        "lat": spec.get("lat"), "lon": spec.get("lon"),
                        "description": spec.get("description"),
                        "host": spec.get("host"), "config": spec.get("config"),
                        "flat": not isinstance(members, dict),
                        "components": rows, "sensors": rows})
    return {
        "path": str(inventory.SYSTEMS), "exists": inventory.SYSTEMS.exists(),
        "secrets_path": str(secrets_store.PATH), "systems": systems,
        "defaults": (doc.get("defaults") or {}).get("ssh") or {},
        "roles": list(inventory.ROLES),
        "type_catalog": TYPE_CATALOG,
        "domains": sorted(set(graph_mod.DOMAINS)
                          | {d for r in inventory.load().values() if (d := r.get("domain"))}),
        # Exactly what reaches the model. Rendered in the UI beside the editor so the
        # difference between what an admin stores and what an agent sees is visible.
        "agent_view": sorted(inventory.load().values(), key=lambda r: r["name"]),
        "agent_prompt": inventory.as_prompt(),
    }


@app.put("/api/inventory/system/{name}")
def inventory_put(name: str, req: SystemReq) -> dict:
    if not _INV_NAME.match(name):
        raise HTTPException(400, "system name must be lowercase letters, digits, - or _")

    # A rename writes under the new key and drops the old one in the same save, so the
    # file is never briefly holding both. Component names are untouched: they live in
    # their own flat namespace, and password_env is derived from the COMPONENT name, so
    # renaming a system cannot orphan a stored secret.
    # Absent means "no rename"; present but blank means the field was cleared, which is
    # a mistake worth reporting rather than silently ignoring.
    target = name if req.new_name is None else req.new_name.strip()
    if not _INV_NAME.match(target):
        raise HTTPException(400, "system name must be lowercase letters, digits, - or _")
    existing = _inv_doc().get("systems") or {}
    if target != name and target in existing:
        raise HTTPException(409, f"a system called {target!r} already exists")

    members = req.members()
    for s_ in members:
        if not _INV_NAME.match(s_.name):
            raise HTTPException(400, f"component name {s_.name!r} must be lowercase "
                                     f"letters, digits, - or _")
        if not (s_.type or "").strip():
            raise HTTPException(400, f"component {s_.name!r} needs a hardware type")
        if s_.role not in inventory.ROLES:
            raise HTTPException(400, f"component {s_.name!r} has role {s_.role!r}; "
                                     f"allowed: {list(inventory.ROLES)}")
        _check_access(s_)

    doc = _inv_doc()
    # What this system already holds, so a save can keep the parts the form does not
    # edit. Without this the editor round-trips stale values back — and a bad one the
    # user cannot even see (the SSH port is no longer a field) blocks every later save.
    _cur = existing.get(name) or {}
    _cur_members = _cur.get("components")
    if not isinstance(_cur_members, dict):
        _cur_members = _cur.get("sensors")
    _cur_members = _cur_members if isinstance(_cur_members, dict) else {}

    components: dict[str, dict] = {}
    for s_ in members:
        spec: dict = {"type": s_.type.strip(), "role": s_.role}
        for k in ("domain", "address", "port", "scheme", "hardware",
                  "software_version", "web", "description"):
            if (v := getattr(s_, k)) not in (None, ""):
                spec[k] = v
        spec.update(_check_coords(s_.lat, s_.lon, f"component {s_.name!r}"))
        if (extra := _check_fields(s_.fields, f"component {s_.name!r}")):
            spec["fields"] = extra
        if s_.ssh:
            ssh = {k: v for k in ("user", "port", "key_file")
                   if (v := getattr(s_.ssh, k)) not in (None, "")}
            # Carry forward what was not submitted. `user` is deliberately not carried:
            # clearing the username in the form should clear it.
            _prev = ((_cur_members.get(s_.name) or {}).get("access") or {}).get("ssh") or {}
            ssh = {**{k: v for k, v in _prev.items() if k in ("port", "key_file")}, **ssh}
            env = (s_.ssh.password_env or "").strip()
            if s_.ssh.password and not env:
                # Derive a stable variable name so an admin never has to invent one.
                # Components are numbered by default, and "1_SSH_PW" is not a legal
                # environment variable — prefix anything that does not start with a
                # letter rather than failing the save.
                env = re.sub(r"[^A-Z0-9]+", "_", s_.name.upper()) + "_SSH_PW"
                if not env[0].isalpha():
                    env = "NODE_" + env
            if env:
                try:
                    if s_.ssh.clear_password:
                        secrets_store.delete(env)
                    elif s_.ssh.password:
                        secrets_store.put(env, s_.ssh.password)
                except secrets_store.SecretError as e:
                    raise HTTPException(400, str(e)) from e
                ssh["password_env"] = env      # the NAME lands here; the value never does
            # A port with nothing to log in AS is not an access block, it is a stray
            # field that makes credentials() raise the first time anything uses it.
            # Drop it rather than storing something unusable.
            if ssh and not (set(ssh) <= {"port"}):
                spec["access"] = {"ssh": ssh}
        components[s_.name] = spec

    entry: dict = {}
    if req.site:
        entry["site"] = req.site
    entry.update(_check_coords(req.lat, req.lon, f"system {target!r}"))
    if req.description:
        entry["description"] = req.description
    for k in ("host", "config"):
        v = getattr(req, k)
        v = _cur.get(k) if v is None else v.strip()
        if v:
            entry[k] = v
    entry["components"] = components

    # Rebuild in order so a renamed system keeps its place in the file rather than
    # jumping to the end of a diff.
    systems = doc.get("systems") or {}
    rebuilt = {(target if k == name else k): (entry if k == name else v)
               for k, v in systems.items()}
    if name not in systems:
        rebuilt[target] = entry
    doc["systems"] = rebuilt

    note = (f"rename system {name} -> {target}" if target != name
            else f"upsert system {name} ({len(components)} components)")
    _save_inv_doc(doc, note)
    return {"ok": True, "name": target, **inventory_get()}


@app.delete("/api/inventory/system/{name}")
def inventory_delete(name: str) -> dict:
    doc = _inv_doc()
    if name not in (doc.get("systems") or {}):
        raise HTTPException(404, f"no system {name!r}")
    # The secret outlives the registry entry on purpose: deleting a system should not
    # silently destroy a credential the admin may still need. It is named in the reply.
    orphaned = []
    spec = doc["systems"][name]
    _members = spec.get("components")
    if not isinstance(_members, dict):
        _members = spec.get("sensors")
    for sv in _members.values() if isinstance(_members, dict) else [spec]:
        if env := ((sv.get("access") or {}).get("ssh") or {}).get("password_env"):
            if secrets_store.get(env):
                orphaned.append(env)
    del doc["systems"][name]
    _save_inv_doc(doc, f"delete system {name}")
    return {"ok": True, "orphaned_secrets": sorted(set(orphaned)), **inventory_get()}


@app.delete("/api/inventory/secret/{env_name}")
def inventory_delete_secret(env_name: str) -> dict:
    if secrets_store.is_in_environment(env_name):
        raise HTTPException(400, f"{env_name} comes from the process environment; "
                                 f"unset it and restart the console to remove it")
    return {"ok": secrets_store.delete(env_name)}


# ---------------------------------------------------------------------------
# Live with chat — the console <-> chat bridge
#
# A ticket here does NOT run the graph. It is queued on disk for the assistant in the
# Claude Code session, which reads the KB, runs read-only checks against the real
# hardware, and publishes a report in the same shape the pipeline produces. The Run tab
# is the other half: it exercises the pipeline against recorded fixtures and never
# touches the bench.
# ---------------------------------------------------------------------------


class TicketReq(BaseModel):
    question: str
    attachments: list[str] = []


@app.post("/api/bridge/ticket")
def bridge_submit(req: TicketReq) -> dict:
    recs = _attachments(req.attachments)
    try:
        sid = bridge.submit(attachments_mod.compose(req.question, recs),
                            typed=req.question, attachments=attachments_mod.public(recs))
    except bridge.BridgeError as e:
        raise _bad(e) from e
    log.info("bridge ticket %s queued", sid)
    return {"ok": True, "id": sid, **(bridge.status(sid) or {})}


@app.get("/api/bridge/queue")
def bridge_queue() -> dict:
    q = bridge.queue()
    return {"sessions": q,
            "pending": sum(1 for s in q if s.get("status") == "pending"),
            "working": sum(1 for s in q if s.get("status") == "in_progress")}


@app.get("/api/bridge/session/{sid}")
def bridge_session(sid: str) -> dict:
    s = bridge.status(sid)
    if not s:
        raise HTTPException(404, "unknown session")
    return s


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html")
