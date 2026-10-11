"""The support bot's console: its conversations, its knowledge base, and Deploy.

    make -C console dev        # http://127.0.0.1:8766, local requests allowed
    make -C console run        # as the service runs it (see console/README.md)

It binds to 127.0.0.1 only. Teammates reach it over the tailnet through `tailscale serve`,
which puts the caller's Tailscale login in the Tailscale-User-Login header; only logins in
CONSOLE_ALLOWED_USERS get in. A request without that header came from this machine, and is
refused unless CONSOLE_ALLOW_LOCAL=1.

The console never imports the bot (slack-bridge/) or the retired bot (legacy/). It reads
and writes their files: support-agent-skill/ for the KB, slack-bridge/state/ for the turns.
"""
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from console import conversations, kb, ops
from console.settings import S

HERE = Path(__file__).resolve().parent
app = FastAPI(title="gotcha support console", docs_url=None, redoc_url=None, openapi_url=None)

WRITES = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------

def caller(request: Request) -> str | None:
    """The tailnet login, `local`, or None for someone not let in."""
    login = (request.headers.get("tailscale-user-login") or "").strip().lower()
    if login:
        return login if login in S.allowed_users else None
    return "local" if S.allow_local else None


@app.middleware("http")
async def gate(request: Request, call_next):
    who = caller(request)
    if who is None:
        login = request.headers.get("tailscale-user-login")
        why = (f"{login} is not on CONSOLE_ALLOWED_USERS" if login
               else "local requests are off (CONSOLE_ALLOW_LOCAL); open the console through "
                    "its tailnet address")
        return JSONResponse({"detail": f"Not allowed: {why}."}, status_code=403)
    if request.method in WRITES:
        # A custom header and a JSON body: a page on another site can't send either without a
        # CORS preflight, which this server never answers, so it can't post a form here.
        if request.headers.get("x-console") != "1" or \
                not (request.headers.get("content-type") or "").startswith("application/json"):
            return JSONResponse({"detail": "writes need X-Console: 1 and a JSON body"},
                                status_code=403)
    request.state.who = who
    return await call_next(request)


def audit(request: Request, action: str, target: str, **detail) -> None:
    S.state_dir.mkdir(parents=True, exist_ok=True)
    line = {"ts": round(time.time(), 3), "who": request.state.who, "action": action,
            "target": target, **detail}
    with open(S.state_dir / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _kb_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except kb.KbError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, f"not found: {e}")
    except FileExistsError as e:
        raise HTTPException(409, f"already exists: {e}")


# --------------------------------------------------------------------------
# Page and header
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/meta")
def meta(request: Request):
    return {"who": request.state.who, "skill_dir": str(S.skill_dir),
            "bridge_dir": str(S.bridge_dir)}


# --------------------------------------------------------------------------
# Conversations and stats
# --------------------------------------------------------------------------

@app.get("/api/conversations")
def list_conversations(q: str = "", outcome: str = "", channel: str = ""):
    rows = conversations.summaries(q, outcome, channel)
    return {"threads": rows, "turn_log": S.turns_file.exists(),
            "channels": sorted({r["channel"] for r in rows})}


@app.get("/api/conversations/{key}")
def get_conversation(key: str):
    try:
        return conversations.thread(key)
    except FileNotFoundError:
        raise HTTPException(404, f"no thread {key}")


@app.get("/api/stats")
def get_stats(days: int = 30):
    return conversations.stats(max(1, min(days, 365)))


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

class CaseNew(BaseModel):
    name: str
    topics: list[str] = []


class Section(BaseModel):
    heading: str
    body: str = ""


class IndexRow(BaseModel):
    says: str
    evidence: str


class CaseSave(BaseModel):
    base: str | None = None
    raw: str | None = None
    topics: list[str] = []
    symptoms: list[str] = []
    title: str = ""
    intro: str = ""
    sections: list[Section] = []
    index: IndexRow | None = None
    signatures: list[str] | None = None


class Rename(BaseModel):
    name: str


@app.get("/api/cases")
def list_cases():
    return {"cases": _kb_call(kb.list_cases), "topics": kb.all_topics()}


@app.get("/api/cases/{slug}")
def get_case(slug: str):
    d = _kb_call(kb.get_case, slug)
    d["references"] = kb.references(slug)
    return d


@app.post("/api/cases")
def new_case(req: CaseNew, request: Request):
    slug = _kb_call(kb.create_case, req.name, req.topics)
    audit(request, "create-case", slug)
    return {"slug": slug, "selftest": kb.selftest()}


@app.put("/api/cases/{slug}")
def save_case(slug: str, req: CaseSave, request: Request):
    parts = None if req.raw is not None else {
        "topics": req.topics, "symptoms": req.symptoms, "title": req.title,
        "intro": req.intro, "sections": [s.model_dump() for s in req.sections]}
    tag = _kb_call(kb.save_case, slug, base=req.base, raw=req.raw, parts=parts,
                   index=req.index.model_dump() if req.index else None,
                   signatures=req.signatures)
    audit(request, "save-case", slug, raw=req.raw is not None,
          index=req.index is not None, signatures=req.signatures)
    return {"slug": slug, "etag": tag, "selftest": kb.selftest()}


@app.post("/api/cases/{slug}/rename")
def rename_case(slug: str, req: Rename, request: Request):
    refs = kb.references(slug)
    new = _kb_call(kb.rename_case, slug, req.name)
    audit(request, "rename-case", slug, to=new)
    return {"slug": new, "references": refs, "selftest": kb.selftest()}


@app.delete("/api/cases/{slug}")
def delete_case(slug: str, request: Request):
    refs = kb.references(slug)
    _kb_call(kb.delete_case, slug)
    audit(request, "delete-case", slug)
    return {"deleted": slug, "references": refs, "selftest": kb.selftest()}


# --------------------------------------------------------------------------
# Reference docs
# --------------------------------------------------------------------------

class DocSave(BaseModel):
    text: str
    base: str | None = None


@app.get("/api/docs")
def list_docs():
    return {"docs": [{"name": k, "path": v, "read_only": k in kb.READ_ONLY_DOCS}
                     for k, v in kb.DOCS.items()]}


@app.get("/api/docs/{name}")
def get_doc(name: str):
    return _kb_call(kb.get_doc, name)


@app.put("/api/docs/{name}")
def save_doc(name: str, req: DocSave, request: Request):
    tag = _kb_call(kb.save_doc, name, req.text, req.base)
    audit(request, "save-doc", name)
    return {"name": name, "etag": tag, "selftest": kb.selftest()}


@app.get("/api/selftest")
def run_selftest():
    return kb.selftest()


# --------------------------------------------------------------------------
# The bot: config, systems, deploy
# --------------------------------------------------------------------------

@app.get("/api/bot")
def bot():
    return ops.bot_config()


@app.get("/api/systems")
def systems():
    return ops.systems()


@app.get("/api/deploy")
def deploy_status():
    return ops.deploy_status()


@app.post("/api/deploy")
def deploy(request: Request):
    try:
        ops.DEPLOY.start(request.state.who)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    audit(request, "deploy", S.container)
    return ops.DEPLOY.snapshot()


@app.get("/api/audit")
def get_audit(limit: int = 200):
    p = S.state_dir / "audit.jsonl"
    if not p.exists():
        return {"entries": []}
    lines = p.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 2000)):]
    return {"entries": [json.loads(x) for x in reversed(lines) if x.strip()]}
