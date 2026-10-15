"""The support bot's console: its conversations, its knowledge base, and Deploy.

    make -C console dev        # http://127.0.0.1:8766, local requests allowed
    make -C console run        # as the service runs it (see console/README.md)

It binds to 127.0.0.1 only. Teammates reach it over the tailnet through `tailscale serve`,
which puts the caller's Tailscale login in the Tailscale-User-Login header; only logins in
CONSOLE_ALLOWED_USERS get in. A request without that header came from this machine, and is
refused unless CONSOLE_ALLOW_LOCAL=1. On top of that, everyone signs in with a code from
their authenticator app, and only CONSOLE_ADMINS may reveal or change the bot's secrets
(see console/auth.py).

The console never imports the bot (slack-bridge/) or the retired bot (legacy/). It reads
and writes their files: support-agent-skill/ for the KB, slack-bridge/state/ for the turns.
"""
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, StrictFloat, StrictInt

from console import access, auth, conversations, kb, ops, passkeys, secrets_env, sites
from console.settings import S

HERE = Path(__file__).resolve().parent
app = FastAPI(title="gotcha support console", docs_url=None, redoc_url=None, openapi_url=None)

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
MAX_BODY = 64 * 1024  # every request here is a small JSON body
# Reachable before signing in: the page itself, and what it needs to show the sign-in form.
NO_SESSION = {"/", "/api/auth/status", "/api/auth/login", "/api/passkeys/options"}


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
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "request too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "bad Content-Length"}, status_code=400)
    if S.allowed_hosts:
        # The Host the page was opened as. Set it, and a page elsewhere that points its own
        # name at this machine (DNS rebinding) gets nothing back.
        host = passkeys.host_name(request).lower()
        if host not in S.allowed_hosts:
            return JSONResponse({"detail": f"unexpected Host {host!r} (CONSOLE_ALLOWED_HOSTS)"},
                                status_code=421)
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
    request.state.role = auth.role(who)
    request.state.session = auth.get_session(request.cookies.get(auth.COOKIE), who)
    if request.state.session is None and request.url.path not in NO_SESSION:
        return JSONResponse({"detail": "login"}, status_code=401)
    return await call_next(request)


def audit(request: Request, action: str, target: str, **detail) -> None:
    """One line per change, who made it and in what role. Never pass a secret's value."""
    S.state_dir.mkdir(parents=True, exist_ok=True)
    line = {"ts": round(time.time(), 3), "who": request.state.who,
            "role": getattr(request.state, "role", None), "action": action,
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


def require_admin(request: Request, what: str) -> None:
    if request.state.role != "admin":
        audit(request, "refused", what)
        raise HTTPException(403, "only an admin (CONSOLE_ADMINS) may do this")


def check_code(request: Request, code: str | None, action: str, target: str,
               passkey: dict | None = None, purpose: str = "confirm") -> str:
    """The second factor for one action: a passkey's signed answer to the challenge issued
    for `purpose`, or else a code from the authenticator app. Returns which one it was."""
    try:
        if passkey:
            passkeys.authenticate(request.state.who, purpose, passkey)
            return "passkey"
        auth.verify(request.state.who, code or "")
        return "code"
    except auth.AuthError as e:
        audit(request, f"{action}-failed", target)
        raise HTTPException(401, str(e))


# --------------------------------------------------------------------------
# Signing in, and the second code for secrets
# --------------------------------------------------------------------------

class Code(BaseModel):
    code: str = ""
    passkey: dict | None = None


def _auth_status(request: Request) -> dict:
    """Before sign-in only what the sign-in form needs; role and passkeys once signed in, so
    a request with a forged header alone learns no more than whether a login is set up."""
    s = request.state.session
    out = {"who": request.state.who, "enrolled": auth.enrolled(request.state.who),
           "logged_in": s is not None, "stepup_seconds": S.stepup_seconds,
           "edit_seconds": S.edit_seconds}
    if s is not None:
        out.update(role=request.state.role, passkeys=passkeys.count(request.state.who),
                   stepup_until=s["stepup_until"] if auth.stepup_ok(s) else None,
                   edit_until=s["edit_until"] if auth.edit_ok(s) else None)
    return out


@app.get("/api/auth/status")
def auth_status(request: Request):
    return _auth_status(request)


@app.post("/api/auth/login")
def login(req: Code, request: Request, response: Response):
    how = check_code(request, req.code, "login", request.state.who, req.passkey, "login")
    token = auth.new_session(request.state.who)
    https = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", path="/",
                        secure=https, max_age=int(S.session_hours * 3600))
    request.state.session = auth.get_session(token, request.state.who)
    audit(request, "login", request.state.who, method=how)
    return _auth_status(request)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.drop_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    audit(request, "logout", request.state.who)
    return {"logged_in": False}


@app.post("/api/auth/stepup")
def stepup(req: Code, request: Request):
    require_admin(request, "step-up")
    how = check_code(request, req.code, "step-up", request.state.who, req.passkey, "stepup")
    until = auth.grant_stepup(request.state.session)
    audit(request, "step-up", request.state.who, method=how)
    return {"stepup_until": until}


# --------------------------------------------------------------------------
# Passkeys
# --------------------------------------------------------------------------

class PasskeyOptions(BaseModel):
    purpose: str = "login"


class PasskeyAdd(BaseModel):
    credential: dict
    name: str = ""


def _json(text: str) -> Response:
    return Response(content=text, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/passkeys/options")
def passkey_options(req: PasskeyOptions, request: Request):
    """A challenge for signing in (no session needed), or for unlocking reveals or confirming
    a save (an admin's session)."""
    if req.purpose != "login":
        if request.state.session is None:
            return JSONResponse({"detail": "login"}, status_code=401)
        require_admin(request, f"passkey {req.purpose}")
    try:
        return _json(passkeys.authentication_options(request.state.who, req.purpose, request))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))


@app.get("/api/passkeys")
def list_passkeys(request: Request):
    return {"passkeys": passkeys.listing(request.state.who)}


@app.post("/api/passkeys/register/options")
def passkey_register_options(req: Code, request: Request):
    # A code from the app first: the tailnet header alone must not be enough to add a passkey.
    check_code(request, req.code, "passkey-add", request.state.who)
    return _json(passkeys.registration_options(request.state.who, request))


@app.post("/api/passkeys/register")
def passkey_register(req: PasskeyAdd, request: Request):
    try:
        row = passkeys.register(request.state.who, req.credential, req.name)
    except auth.AuthError as e:
        audit(request, "passkey-add-failed", request.state.who)
        raise HTTPException(400, str(e))
    audit(request, "passkey-added", request.state.who, name=row["name"])
    return {"passkey": row, "passkeys": passkeys.listing(request.state.who)}


@app.delete("/api/passkeys/{cred_id}")
def passkey_remove(cred_id: str, req: Code, request: Request):
    # A code here too: someone at an unlocked browser shouldn't be able to strip the passkeys.
    check_code(request, req.code, "passkey-remove", request.state.who, req.passkey)
    if not passkeys.remove(request.state.who, cred_id):
        raise HTTPException(404, "no such passkey")
    audit(request, "passkey-removed", request.state.who)
    return {"passkeys": passkeys.listing(request.state.who)}


# --------------------------------------------------------------------------
# The bot's secrets (the hidden entries of slack-bridge/.env)
# --------------------------------------------------------------------------

class SecretSave(BaseModel):
    value: str
    code: str = ""
    passkey: dict | None = None


@app.get("/api/secrets")
def list_secrets(request: Request):
    return {"secrets": secrets_env.listing(), "can_edit": request.state.role == "admin",
            "env_file": (S.bridge_dir / ".env").exists()}


@app.post("/api/secrets/{name}/reveal")
def reveal_secret(name: str, request: Request):
    require_admin(request, f"reveal {name}")
    if not auth.stepup_ok(request.state.session):
        raise HTTPException(403, "stepup")
    try:
        val = secrets_env.value(name)
    except KeyError:
        raise HTTPException(404, f"{name} is not a secret in slack-bridge/.env")
    audit(request, "reveal-secret", name)
    return JSONResponse({"name": name, "value": val},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.put("/api/secrets/{name}")
def save_secret(name: str, req: SecretSave, request: Request):
    require_admin(request, f"set {name}")
    # A code on every save, even inside the step-up window: a change is the one thing here
    # that can lock the bot out of Slack or Anthropic.
    try:
        secrets_env.precheck(name, req.value)  # a typo shouldn't use up the admin's code
    except secrets_env.SecretError as e:
        raise HTTPException(400, str(e)) from None
    check_code(request, req.code, "set-secret", name, req.passkey)
    try:
        row = secrets_env.put(name, req.value, request.state.who)
    except secrets_env.SecretError as e:
        raise HTTPException(400, str(e)) from None
    audit(request, "set-secret", name)
    out = {"status": "saved", "secret": row}
    try:
        note = ops.DEPLOY.restart(request.state.who)
        out["restart_triggered"] = True
        if note:
            out["restart_note"] = note
        audit(request, "restart-bot", S.container, reason=f"set {name}")
    except Exception as e:  # the save stands; the page offers "Restart now"
        out["restart_triggered"], out["restart_error"] = False, str(e)
    return out


@app.post("/api/bot/restart")
def restart_bot(request: Request):
    require_admin(request, "restart-bot")
    note = ops.DEPLOY.restart(request.state.who)
    audit(request, "restart-bot", S.container)
    return {"restart_triggered": True, "restart_note": note, **ops.DEPLOY.snapshot()}


# --------------------------------------------------------------------------
# Access: each system's categories and components, and their fields and secrets
# --------------------------------------------------------------------------
# Reading is for everyone signed in, and never includes a password. Every change, and every
# reveal, is an admin's and audited without values. A reveal needs a step-up; a change needs a
# code or passkey, except that one unlocks further changes for CONSOLE_EDIT_SECONDS (not
# deletes, which always need one).

class SystemFields(Code):
    site: str | None = None
    description: str | None = None
    latitude: StrictInt | StrictFloat | str | None = None
    longitude: StrictInt | StrictFloat | str | None = None
    altitude: StrictInt | StrictFloat | str | None = None


class SystemAdd(SystemFields):
    name: str


class SystemEdit(SystemFields):
    name: str | None = None


class ComponentFields(Code):
    category: str | None = None
    type: str | None = None
    role: str | None = None
    scheme: str | None = None
    address: str | None = None
    port: StrictInt | None = None
    user: str | None = None
    password: str | None = None
    clear_password: bool = False
    fields: dict[str, str] | None = None                 # the whole set of plain extra fields
    secret_fields: dict[str, str | None] | None = None   # name -> new value, or null to remove
    # a sensor
    version: str | None = None
    gui: str | None = None
    gui_user: str | None = None
    gui_password: str | None = None
    azimuth: StrictInt | StrictFloat | str | None = None
    elevation: StrictInt | StrictFloat | str | None = None
    fov_from: StrictInt | StrictFloat | str | None = None
    fov_to: StrictInt | StrictFloat | str | None = None
    range: StrictInt | StrictFloat | str | None = None
    # a computer
    model: str | None = None
    sensor: str | None = None
    web: str | None = None
    web_password: str | None = None
    rustdesk_ip: str | None = None
    rustdesk_password: str | None = None
    users: dict[str, str | None] | None = None   # name -> new password, "" keep, null remove
    clear: list[str] | None = None               # fixed passwords to remove, e.g. ["gui_password"]


class ComponentAdd(ComponentFields):
    name: str
    category: str


class ComponentEdit(ComponentFields):
    name: str | None = None


def _changes(req: BaseModel) -> dict:
    """Only the fields the page sent: a field left out is left alone."""
    return {k: getattr(req, k) for k in req.model_fields_set if k not in ("code", "passkey")}


def _access_error(e: "access.AccessError") -> HTTPException:
    status = 404 if isinstance(e, access.NotFound) else 409 if isinstance(e, access.Conflict) else 400
    return HTTPException(status, str(e))


def _access_change(request: Request, req: Code, action: str, target: str, fn, *,
                   system: str | None = None, component: str | None = None, window: bool = True):
    """Check the change (before the code is spent), the admin and the code, then make it.

    One code (or passkey) opens a short window for further System access edits, so setting up
    a system's components doesn't take a new code for each one. A code sent anyway is still
    checked, and extends the window. A delete (window=False) always takes its own code.
    Returns what `fn` returns, and how the change was confirmed: code, passkey or edit-window."""
    require_admin(request, f"{action} {target}")
    try:
        changes = _changes(req)
        fields_now = access.current_fields(system, component) if "fields" in changes and component else None
        coords_now = access.current_coords(system) if system and not component else None
        access.precheck(changes, fields_now, coords_now)
    except access.AccessError as e:  # a typo shouldn't use up the admin's code
        raise _access_error(e) from None
    session = request.state.session
    if window and auth.edit_ok(session) and not req.code and not req.passkey:
        how = "edit-window"
    else:
        how = check_code(request, req.code, action, target, req.passkey)
    try:
        result = fn()
    except access.AccessError as e:
        raise _access_error(e) from None
    if how != "edit-window":
        auth.grant_edit(session)  # only once a change has gone through
    return result, how


def _access_out(request: Request, **extra) -> dict:
    """What every System access route answers: the systems, and the editing window."""
    s = request.state.session
    return {**extra, **access.listing(), "can_edit": request.state.role == "admin",
            "edit_until": s["edit_until"] if auth.edit_ok(s) else None}


@app.get("/api/access")
def list_access(request: Request):
    return _access_out(request)


@app.post("/api/access/system")
def add_system(req: SystemAdd, request: Request):
    name, how = _access_change(request, req, "add-system", req.name, lambda: access.add_system(
        req.name, request.state.who, {k: v for k, v in _changes(req).items() if k != "name"}))
    audit(request, "add-system", name, method=how)
    return _access_out(request, status="saved")


@app.put("/api/access/system/{system}")
def edit_system(system: str, req: SystemEdit, request: Request):
    changed, how = _access_change(request, req, "edit-system", system, lambda: access.edit_system(
        system, request.state.who, _changes(req)), system=system)
    if changed:
        audit(request, "edit-system", system, fields=changed, method=how,
              **({"to": req.name.strip()} if "name" in changed else {}))
    return _access_out(request, status="saved" if changed else "unchanged", changed=changed)


@app.delete("/api/access/system/{system}")
def delete_system(system: str, req: Code, request: Request):
    n, how = _access_change(request, req, "delete-system", system, lambda: access.delete_system(system),
                            system=system, window=False)
    audit(request, "delete-system", system, components=n, method=how)
    return _access_out(request, status="deleted")


@app.post("/api/access/system/{system}/component")
def add_component(system: str, req: ComponentAdd, request: Request):
    target = f"{system}/{req.name}"
    changed, how = _access_change(request, req, "add-component", target, lambda: access.add_component(
        system, req.name, request.state.who, {k: v for k, v in _changes(req).items() if k != "name"}),
        system=system, component=req.name)
    audit(request, "add-component", target, fields=changed, method=how)
    return _access_out(request, status="saved", changed=changed)


@app.put("/api/access/system/{system}/component/{component}")
def edit_component(system: str, component: str, req: ComponentEdit, request: Request):
    target = f"{system}/{component}"
    changed, how = _access_change(request, req, "edit-component", target, lambda: access.edit_component(
        system, component, request.state.who, _changes(req)), system=system, component=component)
    if changed:
        audit(request, "edit-component", target, fields=changed, method=how,
              **({"to": req.name.strip()} if "name" in changed else {}))
    return _access_out(request, status="saved" if changed else "unchanged", changed=changed)


@app.delete("/api/access/system/{system}/component/{component}")
def delete_component(system: str, component: str, req: Code, request: Request):
    target = f"{system}/{component}"
    _, how = _access_change(request, req, "delete-component", target,
                            lambda: access.delete_component(system, component),
                            system=system, component=component, window=False)
    audit(request, "delete-component", target, method=how)
    return _access_out(request, status="deleted")


class RevealReq(BaseModel):
    """Which secret: a secret extra field, a fixed password, a user's password, or (none of
    them) the SSH password."""
    field: str | None = None
    slot: str | None = None    # gui_password, web_password, rustdesk_password
    user: str | None = None


@app.post("/api/access/system/{system}/component/{component}/reveal")
def reveal_access(system: str, component: str, req: RevealReq, request: Request):
    """An SSH password (no field), or one secret extra field."""
    target = f"{system}/{component}"
    require_admin(request, f"reveal {target}")
    if not auth.stepup_ok(request.state.session):
        raise HTTPException(403, "stepup")
    try:
        value = access.secret(system, component, req.field, req.slot, req.user)
    except access.AccessError as e:
        raise _access_error(e) from None
    if value is None:
        raise HTTPException(404, f"{target} has no value set")
    # Named as the page and `changed` name it: a field, a slot, user:<name>, or password (SSH).
    which = req.field or req.slot or (f"user:{req.user}" if req.user else "password")
    audit(request, "reveal-access", target, field=which)
    return JSONResponse({"system": system, "component": component, "which": which, "value": value},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# --------------------------------------------------------------------------
# Page and header
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/meta")
def meta(request: Request):
    return {"who": request.state.who, "role": request.state.role, "skill_dir": str(S.skill_dir),
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


def _site_name(host: str) -> str:
    allowed = ops.bot_env().get("BRIDGE_ALLOWED_HOSTS") or ops.DEFAULTS["BRIDGE_ALLOWED_HOSTS"]
    if not re.fullmatch(allowed, host):
        raise HTTPException(404, f"{host} is not one of the bot's sites ({allowed})")
    return allowed


@app.get("/api/systems/{host}")
def site(host: str):
    _site_name(host)
    return sites.site(host)


@app.post("/api/systems/{host}/triage")
def triage(host: str, request: Request):
    """Runs the skill's read-only triage of the site now, as the bot does for a question."""
    allowed = _site_name(host)
    audit(request, "run-triage", host)
    try:
        return sites.run_triage(host, ops.bot_env().get("GOTCHA_SSH_USER"), allowed)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


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
