"""Local operations console.

Runs sessions (mock mode only), surfaces the approval queue, and shows traces, the
knowledge base, and the permission surface READ-ONLY.

Permissions are deliberately not editable here. Their value is that widening them is a
code change that shows up in a diff and a test run; a button that adds an allowed
command is how that guarantee quietly dies.
"""
import os
import threading
import traceback
import uuid
from pathlib import Path

# Console never runs live. Set before importing transport, which reads it at import.
os.environ["AGENT_MODE"] = "mock"

import json  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import agents as agents_mod  # noqa: E402
import config_store  # noqa: E402
import inventory  # noqa: E402
import transport  # noqa: E402
from graph import build  # noqa: E402
from registry import REGISTRY, load_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = str(ROOT / "graph.db")
TRACES = ROOT / "traces"
KB = ROOT / "kb"

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
_lock = threading.Lock()


def _set(sid: str, **kw) -> None:
    with _lock:
        SESSIONS.setdefault(sid, {}).update(kw)


def _run(sid: str, payload) -> None:
    """Drive the graph in a worker thread. Fresh sqlite connection per thread."""
    try:
        with SqliteSaver.from_conn_string(DB) as cp:
            out = graph().compile(checkpointer=cp).invoke(
                payload, {"configurable": {"thread_id": sid}})
        if "__interrupt__" in out:
            _set(sid, status="awaiting_approval",
                 pending=out["__interrupt__"][0].value)
        else:
            _set(sid, status="done", pending=None, report=out.get("report"),
                 saved=bool(out.get("saved")))
            _write_trace(sid, out)
    except Exception as e:  # noqa: BLE001 - surfaced in the UI, not swallowed
        _set(sid, status="error", error=f"{type(e).__name__}: {e}",
             detail=traceback.format_exc()[-2000:])


def _write_trace(sid: str, out: dict) -> None:
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps({"findings": out.get("findings", []),
                            "transcript": out.get("transcript", []),
                            "report": out.get("report"),
                            "saved": bool(out.get("saved"))}, default=str) + "\n")


def _live_state(sid: str) -> dict:
    """Read accumulated state from the checkpoint so the UI can watch agents fire."""
    try:
        with SqliteSaver.from_conn_string(DB) as cp:
            snap = graph().compile(checkpointer=cp).get_state(
                {"configurable": {"thread_id": sid}})
    except Exception:  # noqa: BLE001
        return {}
    v = snap.values or {}
    return {
        "visited": v.get("visited", []),
        "next": list(snap.next or []),
        "findings": [{"agent": f.get("agent"), "tool": f.get("tool"), "ok": f.get("ok")}
                     for f in v.get("findings", [])],
        "transcript": v.get("transcript", []),
        "report": v.get("report"),
    }


class RunReq(BaseModel):
    question: str


class DecideReq(BaseModel):
    approved: bool
    edited_md: str | None = None


def _effective_desc(name: str, entry: dict) -> str:
    over = config_store.get("tool_descriptions", {})
    return over.get(name) or entry["schema"].get("description", "")


@app.get("/api/meta")
def meta() -> dict:
    return {
        "mode": transport.MODE,
        "model": os.environ.get("AGENT_MODEL", "claude-opus-5"),
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
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
            "addressable_nodes": inventory.names(),
            "inventory": inventory.load(),
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


@app.get("/api/config")
def get_config() -> dict:
    return {"effective": {"agents": agents_mod.agents(), "order": agents_mod.order(),
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
    """Structure for the UI diagram, plus langgraph's own mermaid as an export."""
    ags, req = agents_mod.agents(), agents_mod.requires()
    try:
        mermaid = graph().compile().get_graph().draw_mermaid()
    except Exception:  # noqa: BLE001
        mermaid = ""
    order = agents_mod.order()
    ranked = sorted(ags, key=lambda n: order.index(n) if n in order else 1e6)
    return {
        "agents": [{"name": n, "tools": ags[n].get("tools", []),
                    "requires": req.get(n, []), "in_order": n in order,
                    "needs_context": agents_mod.needs_context().get(n, [])}
                   for n in ranked],
        "order": order,
        "supervisor_picks": agents_mod.supervisor_picks(),
        "post_agents": [{"name": n, "prompt": a.get("prompt", "")}
                        for n, a in agents_mod.post_agents().items()],
        "needs_context": agents_mod.needs_context(),
        "max_steps": agents_mod.MAX_AGENT_STEPS,
        "mermaid": mermaid,
    }


@app.post("/api/run")
def run(req: RunReq) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set in the server environment.")
    sid = uuid.uuid4().hex[:12]
    _set(sid, status="running", question=req.question, pending=None, report=None)
    threading.Thread(target=_run, args=(sid, {
        "question": req.question, "session_id": sid,
        "findings": [], "visited": [], "transcript": []}), daemon=True).start()
    return {"session_id": sid}


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


@app.get("/api/kb")
def kb_list() -> dict:
    return {"docs": [{"name": p.stem, "bytes": p.stat().st_size}
                     for p in sorted(KB.glob("*.md"))]}


@app.get("/api/kb/{name}")
def kb_doc(name: str) -> dict:
    p = (KB / f"{name}.md").resolve()
    if p.parent != KB.resolve() or not p.exists():
        raise HTTPException(404, "no such document")
    return {"name": name, "text": p.read_text()}


@app.get("/api/traces")
def traces() -> dict:
    TRACES.mkdir(exist_ok=True)
    return {"traces": [{"id": p.stem, "bytes": p.stat().st_size,
                        "mtime": p.stat().st_mtime}
                       for p in sorted(TRACES.glob("*.jsonl"),
                                       key=lambda p: -p.stat().st_mtime)]}


@app.get("/api/traces/{tid}")
def trace(tid: str) -> JSONResponse:
    p = (TRACES / f"{tid}.jsonl").resolve()
    if p.parent != TRACES.resolve() or not p.exists():
        raise HTTPException(404, "no such trace")
    return JSONResponse({"id": tid, "entries": [json.loads(l)
                                                for l in p.read_text().splitlines() if l]})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html")
