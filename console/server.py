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
import inventory  # noqa: E402
import transport  # noqa: E402
from graph import build  # noqa: E402
from registry import REGISTRY, load_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = str(ROOT / "graph.db")
TRACES = ROOT / "traces"
KB = ROOT / "kb"

load_tools()
GRAPH = build()
app = FastAPI(title="support-agent console")

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
            out = GRAPH.compile(checkpointer=cp).invoke(
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
            snap = GRAPH.compile(checkpointer=cp).get_state(
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


@app.get("/api/meta")
def meta() -> dict:
    return {
        "mode": transport.MODE,
        "model": os.environ.get("AGENT_MODEL", "claude-opus-5"),
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "agents": [{"name": n, "prompt": a["prompt"], "tools": a["tools"]}
                   for n, a in agents_mod.AGENTS.items()],
        "order": agents_mod.ORDER,
        "requires": agents_mod.REQUIRES,
        "supervisor_picks": agents_mod.SUPERVISOR_PICKS,
        "tools": [{"name": n, "side_effect": t["side_effect"],
                   "description": t["schema"].get("description", ""),
                   "params": sorted((t["schema"].get("input_schema") or {})
                                    .get("properties", {}))}
                  for n, t in sorted(REGISTRY.items())],
        "permissions": {
            "allowed_commands": {k: list(v) for k, v in transport.ALLOWED.items()},
            "addressable_nodes": inventory.names(),
            "inventory": inventory.load(),
            "write_tools_registered": [n for n, t in REGISTRY.items()
                                       if t["side_effect"] != "none"],
        },
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
