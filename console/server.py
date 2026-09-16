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

import yaml  # noqa: E402

import agents as agents_mod  # noqa: E402
import bridge  # noqa: E402
import config_store  # noqa: E402
import inventory  # noqa: E402
import secrets_store  # noqa: E402
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


def _credentials() -> dict:
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


def _effective_desc(name: str, entry: dict) -> str:
    over = config_store.get("tool_descriptions", {})
    return over.get(name) or entry["schema"].get("description", "")


@app.get("/api/meta")
def meta() -> dict:
    return {
        "mode": transport.MODE,
        "model": os.environ.get("AGENT_MODEL", "claude-opus-5"),
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
    empty = {"topics": [], "symptoms": [], "body": text or ""}
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
    return {"topics": topics,
            "symptoms": [str(x) for x in sym if str(x).strip()],
            "body": text[m.end():]}


def _render_doc(topics, symptoms: list[str], body: str) -> str:
    """Frontmatter + body. Emitted only when there is something to record.

    Always a list under `topics:`, however many there are — one shape to write and one
    to read back, so the round-trip cannot depend on how many tags a case happens to
    carry.
    """
    tags = _as_list(topics)
    meta = {}
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
    docs = []
    for p in sorted(KB.glob("*.md")):
        d = _parse_doc(p.read_text())
        docs.append({"name": p.stem, "bytes": p.stat().st_size,
                     "topics": d["topics"],
                     "symptom_count": len(d["symptoms"]),
                     "structured": bool(d["topics"] or d["symptoms"])})
    return {"docs": docs, **_topics_payload()}


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
    text = _render_doc(req.tags(), req.symptoms, req.markdown())
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
    p.write_text(_render_doc(req.tags(), req.symptoms, body))
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
    description: str | None = None
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
                        "description": spec.get("description"),
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
    if req.description:
        entry["description"] = req.description
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


@app.post("/api/bridge/ticket")
def bridge_submit(req: TicketReq) -> dict:
    try:
        sid = bridge.submit(req.question)
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
