#!/usr/bin/env python3
"""Slack gateway — an ADAPTER, deliberately outside the state machine.

    Slack (Socket Mode) ──> this script ──> graph.invoke() ──> Slack reply
                              (adapter)      (unchanged)

Slack is not a node. The graph knows nothing about channels, threads or Slack users;
this file is the only thing that does. The same graph is driven by run.py (CLI) and
console/server.py (web) without modification.

AUDIENCE: Tier 1 engineers, not customers. They get the TECHNICAL report — root cause,
evidence quoting real tool output, what could not be checked. They do NOT get the
customer_communicator draft by default: that agent exists to strip node names, IPs, PIDs,
paths, topic names and tool names, which is precisely what an engineer needs. The draft is
still produced by the graph; `/draft <session>` posts it when someone is about to forward
something to a client.

Socket Mode: the app holds an outbound WebSocket to Slack, so no inbound port is exposed
and there is no request signature to verify — the `xapp-` token authenticates the
connection. Everything else the HTTP version got right still applies:

1. THE 3-SECOND ACK. A diagnostic run takes minutes; Bolt acks, we work in a thread.
2. REDELIVERY CAUSES DUPLICATES. Socket Mode redelivers envelopes just as the Events API
   retried POSTs. Without dedup on event_id you run the same diagnosis twice in parallel.
3. ONE MESSAGE IS ONE SESSION. Never resume a checkpoint for a new question — `findings`
   and `visited` are append-only, so a reused thread has every agent already visited, the
   supervisor finds nothing eligible, and it re-summarises OLD evidence against the NEW
   question. Silently.
   A Slack THREAD is still one conversation: each message is routed the way the console
   routes a turn (conversation.py) — answered from what the thread already found, or a
   fresh run — with the thread's earlier turns rebuilt from their traces. The graph never
   sees the thread; it gets one question, as before.
4. APPROVAL IS NOT A TIER 1 DECISION. Writing to the knowledge base is admin business and
   defaults to the console. It is available in Slack only if SLACK_APPROVERS names people.

Slack app scopes (bot token): app_mentions:read, chat:write, commands, im:history for
DMs, files:read for attachments, and channels:history + groups:history so a mention can
read what was posted in its thread since the bot last answered. Reinstall after adding.

Run:
    pip install slack-bolt
    export SLACK_BOT_TOKEN=xoxb-...       # bot token
    export SLACK_APP_TOKEN=xapp-...       # app-level token, connections:write
    export SLACK_ALLOWED_CHANNELS=C0...   # fail-closed: unset means nobody
    export SLACK_ADMIN_CHANNEL=C0...
    python slack_app.py
"""
import base64
import json
import logging
import os
import re
import sqlite3
import urllib.request
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

import env_file

# Before the SLACK_* constants below and before transport fixes MODE at import — this
# file reads its whole configuration once, at import, so a later load would be too late.
env_file.load()

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import attachments as att  # noqa: E402
import config_store  # noqa: E402
import conversation as convo  # noqa: E402
import inventory  # noqa: E402
import llm  # noqa: E402
import transport  # noqa: E402
from graph import build  # noqa: E402
from registry import load_tools  # noqa: E402

log = logging.getLogger("slack_gateway")
ROOT = Path(__file__).resolve().parent
TRACES = ROOT / "traces"

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
ADMIN_CHANNEL = os.environ.get("SLACK_ADMIN_CHANNEL", "")
DB = os.environ.get("GRAPH_DB", str(ROOT / "graph.db"))

_csv = lambda v: {x.strip() for x in os.environ.get(v, "").split(",") if x.strip()}  # noqa: E731
ALLOWED_CHANNELS = _csv("SLACK_ALLOWED_CHANNELS")
ALLOWED_USERS = _csv("SLACK_ALLOWED_USERS")     # empty = any user in an allowed channel
APPROVERS = _csv("SLACK_APPROVERS")             # empty = approval is console-only
ALLOW_DMS = os.environ.get("SLACK_ALLOW_DMS", "0") == "1"

MAX_CONCURRENT = int(os.environ.get("SLACK_MAX_CONCURRENT", "2"))
USER_COOLDOWN_S = int(os.environ.get("SLACK_USER_COOLDOWN_S", "20"))

load_tools()

# Concurrent sessions write to one SQLite file. Without WAL the second one meets
# "database is locked" rather than waiting.
try:
    with sqlite3.connect(DB) as _c:
        _c.execute("PRAGMA journal_mode=WAL")
except sqlite3.Error as e:                        # noqa: BLE001 - not fatal
    log.warning("could not enable WAL on %s: %s", DB, e)


# --------------------------------------------------------------------------
# Graph, kept in step with the console
# --------------------------------------------------------------------------

_graph = None
_graph_sig: int | None = None
_graph_lock = threading.Lock()


def graph():
    """The compiled-from graph, rebuilt when the console edits the config.

    build() reads agents/order/requires at build time, so without this an admin who
    reorders agents in the console sees no effect here until someone restarts the bot —
    a silent seam, and exactly the one this architecture creates by splitting the two
    surfaces. One stat() per request is cheap enough to pay every time.
    """
    global _graph, _graph_sig
    try:
        sig = config_store.PATH.stat().st_mtime_ns
    except OSError:
        sig = 0
    with _graph_lock:
        if _graph is None or sig != _graph_sig:
            if _graph is not None:
                log.info("config changed on disk — rebuilding the graph")
            _graph, _graph_sig = build(), sig
        return _graph


# --------------------------------------------------------------------------
# Admission: dedup, allowlist, rate limit
# --------------------------------------------------------------------------

_seen: OrderedDict[str, float] = OrderedDict()
_sessions: OrderedDict[str, dict] = OrderedDict()
_last_request: dict[str, float] = {}
_running = threading.BoundedSemaphore(MAX_CONCURRENT)
_lock = threading.Lock()
_MAX_REMEMBERED = 500


def already_handled(event_id: str) -> bool:
    """Socket Mode redelivers; without this the same ticket is diagnosed twice."""
    with _lock:
        now = time.time()
        for k, t in list(_seen.items()):
            if now - t > 3600:
                _seen.pop(k, None)
        if event_id and event_id in _seen:
            return True
        if event_id:
            _seen[event_id] = now
        return False


def authorise(user: str, channel: str) -> tuple[bool, str]:
    """Fail closed. Workspace membership is not authorisation.

    Any member can DM a bot, and guests or Slack Connect users may be in the workspace,
    so an unconfigured allowlist admits nobody rather than everybody.
    """
    if not ALLOWED_CHANNELS and not ALLOWED_USERS:
        return False, ("This bot has no allowlist configured, so it is refusing everyone. "
                       "Set SLACK_ALLOWED_CHANNELS (and optionally SLACK_ALLOWED_USERS).")
    if channel.startswith("D"):
        if not ALLOW_DMS:
            return False, "Direct messages are disabled. Ask in an approved channel."
        if user not in ALLOWED_USERS:
            return False, "You are not on the allowlist for direct messages."
        return True, ""
    if ALLOWED_CHANNELS and channel not in ALLOWED_CHANNELS:
        return False, "This channel is not approved for diagnostics."
    if ALLOWED_USERS and user not in ALLOWED_USERS:
        return False, "You are not on the allowlist for this bot."
    return True, ""


def rate_limited(user: str) -> bool:
    """A chat surface invites casual use, and every run is several model calls."""
    with _lock:
        now = time.time()
        if now - _last_request.get(user, 0) < USER_COOLDOWN_S:
            return True
        _last_request[user] = now
        return False


def _remember(session_id: str, **kw) -> None:
    with _lock:
        _sessions.setdefault(session_id, {}).update(kw)
        while len(_sessions) > _MAX_REMEMBERED:
            _sessions.popitem(last=False)


# --------------------------------------------------------------------------
# Slack I/O
# --------------------------------------------------------------------------

_client = None


def post(channel: str, text: str, thread_ts: str | None = None) -> None:
    if _client is None:
        log.info("[slack:%s thread=%s] %s", channel, thread_ts, text[:400])
        return
    try:
        _client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except Exception:                              # noqa: BLE001 - never kill the worker
        log.exception("could not post to %s", channel)


# --------------------------------------------------------------------------
# Running a diagnosis
# --------------------------------------------------------------------------

def _write_trace(session_id: str, out: dict, origin: dict, **extra) -> None:
    """Same shape the CLI and console write, plus who asked and from where.

    Slack is about to become the primary interface; an interface with no audit trail is
    the wrong one to make primary.
    """
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{session_id}.jsonl").open("a") as f:
        f.write(json.dumps({
            "status": "done",
            "origin": {"via": "slack", **origin, "mode": transport.MODE, "ts": time.time()},
            "findings": out.get("findings", []),
            "transcript": out.get("transcript", []),
            "report": out.get("report"),
            "customer_message": out.get("customer_message"),
            "blocked": out.get("blocked", []),
            # Session total, including the routing and synthesis calls that produce no
            # transcript entry. Thread-local, so it is this session's and no other's.
            "usage": llm.totals(),
            "saved": bool(out.get("saved")),
            **extra,
        }, default=str) + "\n")


def _trace_status(session_id: str, status: str, **fields) -> None:
    """A status line on its own: a start, a failure, a run that never began."""
    TRACES.mkdir(exist_ok=True)
    if "origin" in fields:
        fields["origin"] = {"via": "slack", **fields["origin"], "mode": transport.MODE}
    with (TRACES / f"{session_id}.jsonl").open("a") as f:
        f.write(json.dumps({"status": status, **fields}, default=str) + "\n")


def start_session(user: str, channel: str, thread_ts: str, text: str,
                  files: list[dict] | None = None, ts: str | None = None) -> str:
    """One message, one fresh session id. Never a reused checkpoint."""
    session_id = uuid.uuid4().hex[:12]
    _remember(session_id, channel=channel, thread_ts=thread_ts, user=user, question=text,
              status="running")
    threading.Thread(target=_diagnose, daemon=True,
                     args=(session_id, text, channel, thread_ts, user, files or [], ts)
                     ).start()
    return session_id


# --------------------------------------------------------------------------
# A thread is a conversation
# --------------------------------------------------------------------------

_thread_locks: dict[tuple[str, str], threading.Lock] = {}


def _thread_lock(channel: str, thread_ts: str) -> threading.Lock:
    """Turns in one thread run one at a time, so the second is routed knowing the first."""
    with _lock:
        return _thread_locks.setdefault((channel, thread_ts), threading.Lock())


def thread_turns(channel: str, thread_ts: str, exclude: str = "") -> list[dict]:
    """The thread's finished turns, oldest first, in conversation.py's shape.

    Rebuilt from the traces rather than held in memory, so a restarted gateway still
    knows what the thread already found.
    """
    if not TRACES.exists():
        return []
    needle = json.dumps(thread_ts)
    turns = []
    for p in TRACES.glob("*.jsonl"):
        if p.stem == exclude:
            continue
        text = p.read_text()
        if needle not in text:                       # cheap reject before parsing
            continue
        lines = [json.loads(l) for l in text.splitlines() if l.strip()]
        o = next((l.get("origin") for l in lines if l.get("origin")), None) or {}
        if o.get("via") != "slack" or o.get("channel") != channel \
                or o.get("thread_ts") != thread_ts:
            continue
        done = [l for l in lines if l.get("status") == "done"]
        if not done:
            continue                                 # failed or never finished
        last = done[-1]
        # The finished line's origin, not the first one: the system is chosen after the
        # start line is written, so only the end of the turn knows it.
        o = last.get("origin") or o
        extra = "\n".join(last.get("thread_context") or [])
        turns.append({
            "started_at": next((l["started_at"] for l in lines if l.get("started_at")), 0),
            "kind": last.get("kind") or "run",
            "system": o.get("system"),
            "question": (o.get("question") or "") + (f"\n\n{extra}" if extra else ""),
            "attachments": last.get("attachments") or [],
            "report": last.get("report"), "findings": last.get("findings") or [],
            "answer": last.get("answer")})
    return sorted(turns, key=lambda t: t["started_at"])


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

BOT_USER_ID = ""        # filled by build_app() from auth.test
BOT_ID = ""


def _download(f: dict) -> bytes:
    """A Slack file's bytes. Needs files:read; the URL is private to the workspace."""
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        raise att.AttachmentError(f"{f.get('name', 'file')}: Slack gave no download link")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:          # noqa: S310 - Slack URL
        if "text/html" in (r.headers.get("Content-Type") or ""):
            # Slack answers a missing scope with its login page, not an error status.
            raise att.AttachmentError(f"{f.get('name', 'file')}: Slack refused the "
                                      f"download — does the bot have files:read?")
        return r.read(att.MAX_BYTES + 1)


def _files_to_attachments(files: list[dict]) -> tuple[list[dict], list[str]]:
    """(stored attachments, one line per file that was skipped and why)."""
    recs, skipped = [], []
    for f in files:
        name = f.get("name") or f.get("title") or "file"
        if f.get("mode") in ("hidden_by_limit", "tombstone"):
            skipped.append(f"{name}: not available — hidden by the workspace's file "
                           f"limit, or deleted")
            continue
        if int(f.get("size") or 0) > att.MAX_BYTES:
            skipped.append(f"{name}: {int(f['size']) / 1048576:.1f} MB is over the "
                           f"{att.MAX_BYTES // 1048576} MB limit")
            continue
        try:
            data = _download(f)
            recs.append(att.save(name, f.get("mimetype") or "",
                                 base64.b64encode(data).decode("ascii"),
                                 describe=llm.describe_attachment))
        except att.AttachmentError as e:
            skipped.append(str(e))
        except Exception as e:                       # noqa: BLE001 - one bad file only
            log.warning("could not read Slack file %s: %s", name, e)
            skipped.append(f"{name}: could not be downloaded ({type(e).__name__})")
    return recs, skipped


def att_system(text: str, remembered: str | None) -> tuple[str | None, str]:
    """The message's system, or why the bot has to ask. Never raises on a bad inventory:
    the thread gets the reason instead of a silent failure."""
    try:
        return inventory.pick_system(text, None, remembered)
    except inventory.InventoryError as e:
        return None, f":warning: The inventory does not load, so no system can be chosen: {e}"


def _mentions_bot(text: str) -> bool:
    return bool(BOT_USER_ID) and f"<@{BOT_USER_ID}>" in (text or "")


def thread_since_last_answer(channel: str, thread_ts: str, before_ts: str
                             ) -> tuple[list[dict], list[str], list[str]]:
    """What was posted in the thread since the bot last spoke, without mentioning it.

    Returns (files, "<@user>: text" lines, notes). Messages that DID mention the bot are
    left out — each of those was its own request. Authors outside the allowlist are left
    out too: being in the thread is not being allowed to ask. DMs are skipped, since
    every DM message reaches the bot on its own.
    """
    if _client is None or channel.startswith("D") or not thread_ts \
            or thread_ts == before_ts:
        return [], [], []
    msgs, cursor = [], None
    try:
        while True:
            r = _client.conversations_replies(channel=channel, ts=thread_ts, limit=200,
                                              **({"cursor": cursor} if cursor else {}))
            msgs += r.get("messages") or []
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:                           # noqa: BLE001 - still answer the mention
        resp = getattr(e, "response", None)          # SlackApiError carries Slack's code
        err = (resp.get("error") if hasattr(resp, "get") else None) or type(e).__name__
        log.warning("conversations.replies failed in %s: %s", channel, err)
        return [], [], [f"I could not read the earlier messages in this thread ({err}) — "
                        f"the bot needs channels:history (groups:history in a private "
                        f"channel). Only this message was used."]
    mine = lambda m: bool(m.get("bot_id")) and (  # noqa: E731
        m.get("user") == BOT_USER_ID or m.get("bot_id") == BOT_ID)
    last = max((float(m["ts"]) for m in msgs if mine(m) and float(m["ts"]) < float(before_ts)),
               default=0.0)
    files, lines = [], []
    for m in msgs:
        t = float(m.get("ts") or 0)
        if not (last < t < float(before_ts)) or m.get("bot_id") or _mentions_bot(m.get("text")):
            continue
        if not authorise(m.get("user", ""), channel)[0]:
            continue
        files += m.get("files") or []
        if (m.get("text") or "").strip():
            lines.append(f"<@{m.get('user', '?')}>: {m['text'].strip()}")
    return files, lines, []


# --------------------------------------------------------------------------
# One turn
# --------------------------------------------------------------------------

def _diagnose(session_id: str, question: str, channel: str, thread_ts: str,
              user: str, files: list[dict] | None = None, ts: str | None = None) -> None:
    origin = {"user": user, "channel": channel, "thread_ts": thread_ts,
              "question": question}
    # Before any work, so a diagnosis the gateway never finishes still shows in Traces.
    _trace_status(session_id, "running", origin=origin, started_at=time.time())
    acquired = _running.acquire(timeout=900)
    if not acquired:
        post(channel, "Still working through a queue of diagnoses — try again shortly.",
             thread_ts)
        _trace_status(session_id, "cancelled", reason="queue full, never started")
        return
    try:
        # This thread is this session, and llm's totals are thread-local — so the count
        # covers describing the attachments and routing the turn, not just the run.
        llm.reset_usage()
        with _thread_lock(channel, thread_ts):
            _turn(session_id, question, channel, thread_ts, ts, files or [], origin)
    except Exception:                              # noqa: BLE001
        log.exception("session %s failed", session_id)
        _remember(session_id, status="error")
        _trace_status(session_id, "error", error=traceback.format_exc(limit=1)[-500:],
                      usage=llm.totals())
        post(channel, f"That run failed — session `{session_id}`. The error is in the "
                      f"gateway log; nothing was changed on the system.", thread_ts)
    finally:
        _running.release()


def _turn(session_id: str, question: str, channel: str, thread_ts: str,
          ts: str | None, files: list[dict], origin: dict) -> None:
    more_files, context, notes = thread_since_last_answer(channel, thread_ts, ts or thread_ts)
    recs, skipped = _files_to_attachments(files + more_files)
    notes += [f"Skipped {x}" for x in skipped]
    if notes:
        post(channel, "\n".join(f":warning: {n}" for n in notes), thread_ts)
    if not question.strip() and not recs and not context:
        post(channel, "Tell me what the system is doing and I'll look into it.", thread_ts)
        _trace_status(session_id, "cancelled", reason="nothing to diagnose")
        return

    typed = question + ("\n\nAlso posted in this thread since the last answer:\n"
                        + "\n".join(context) if context else "")
    asked = att.compose(typed, recs)
    extra = {"attachments": att.public(recs)} if recs else {}
    if context:
        extra["thread_context"] = context

    turns = thread_turns(channel, thread_ts, exclude=session_id)
    # One channel serves every system: the message names it, or the thread already
    # settled it. When neither does and there is more than one, ask — a diagnosis run
    # against the wrong site reads as confidently as one run against the right site.
    earlier = [t["system"] for t in turns if t.get("system")]
    system, why = att_system(typed, earlier[-1] if earlier else None)
    if why:
        post(channel, why, thread_ts)
        _trace_status(session_id, "cancelled", reason="system not named")
        return
    if system:
        origin["system"] = system
    decision = convo.route(turns, asked)
    if decision["kind"] == "follow_up":
        answer, _ = convo.answer_follow_up(turns, asked)
        _trace_status(session_id, "done", kind="follow_up", why=decision["why"],
                      origin={**origin, "ts": time.time()}, answer=answer,
                      usage=llm.totals(), **extra)
        _remember(session_id, status="done")
        post(channel, f"{answer}\n\n_answered from what this thread already found · "
                      f"session `{session_id}`_", thread_ts)
        return

    cfg = {"configurable": {"thread_id": session_id}}
    with SqliteSaver.from_conn_string(DB) as cp:
        out = graph().compile(checkpointer=cp).invoke(
            {"question": asked, "session_id": session_id, "system": system,
             "findings": [], "visited": [], "transcript": [],
             "context": {}, "blocked": []}, cfg)
    _write_trace(session_id, out, origin, kind="run", why=decision["why"], **extra)
    _remember(session_id, status="done", report=out.get("report"),
              customer_message=out.get("customer_message"))
    _deliver(session_id, out, channel, thread_ts)


def format_report(session_id: str, out: dict) -> str:
    """The TECHNICAL report. This is what a Tier 1 engineer needs to act."""
    r = out.get("report") or {}
    lines = [f"*Diagnosis* — session `{session_id}`"]
    if transport.MODE == "mock":
        lines.append(":warning: *mock mode* — this ran against recorded fixtures, "
                     "not the live system.")
    lines.append(f"*Root cause:* {r.get('root_cause') or '_not determined_'}")
    conf = r.get("confidence")
    lines.append(f"*Confidence:* {conf}"
                 + (f"  ·  :rotating_light: *escalate* — {r.get('escalate_reason', '')}"
                    if r.get("escalate") else ""))

    if ev := r.get("evidence"):
        lines.append("*Evidence*\n" + "\n".join(f"• {e}" for e in ev[:8]))
    if unk := r.get("unknowns"):
        lines.append("*Unknowns*\n" + "\n".join(f"• {u}" for u in unk[:6]))
    if act := r.get("suggested_actions"):
        lines.append("*Next checks (read-only)*\n" + "\n".join(f"• {a}" for a in act[:6]))
    if blocked := out.get("blocked"):
        lines.append("*Could not be checked*\n"
                     + "\n".join(f"• {b['note']}" for b in blocked))

    ran = [t.get("agent") for t in out.get("transcript", []) if t.get("agent")]
    tools = [f"{f.get('tool')}{'' if f.get('ok') else ' :x:'}"
             for f in out.get("findings", [])]
    lines.append(f"_agents: {', '.join(ran) or 'none'}_")
    if tools:
        lines.append(f"_tools: {', '.join(tools)}_")
    lines.append(f"_trace: traces/{session_id}.jsonl_")

    msg = out.get("customer_message") or {}
    if msg.get("text"):
        state = "ready" if msg.get("safe_to_send") else f"needs review — {msg.get('review_reason')}"
        lines.append(f"_A client-facing draft exists ({state}). "
                     f"Post it with_ `/draft {session_id}`")
    return "\n\n".join(lines)


def _deliver(session_id: str, out: dict, channel: str, thread_ts: str) -> None:
    """Technical report to the engineer. Approval, if any, to the admin channel."""
    post(channel, format_report(session_id, out), thread_ts)

    if "__interrupt__" in out:
        req = out["__interrupt__"][0].value
        _remember(session_id, status="awaiting_approval")
        where = ADMIN_CHANNEL or channel
        how = (f"Approve with `/approve {session_id}` or `/reject {session_id}`."
               if APPROVERS else
               "Review it in the console — Slack approval is disabled.")
        post(where,
             f"*New runbook case proposed* — session `{session_id}`\n{how}\n\n"
             f"```{req['preview_md'][:2500]}```")
        if not ADMIN_CHANNEL:
            log.warning("no SLACK_ADMIN_CHANNEL; approval request went to %s", channel)


def draft_for(session_id: str) -> dict | None:
    """The client-facing draft, on request. Memory first, then the trace on disk."""
    with _lock:
        s = dict(_sessions.get(session_id) or {})
    if s.get("customer_message"):
        return s["customer_message"]
    p = TRACES / f"{session_id}.jsonl"
    if not p.exists():
        return None
    for line in reversed(p.read_text().splitlines()):
        if line.strip() and (msg := json.loads(line).get("customer_message")):
            return msg
    return None


def resume_approval(session_id: str, user: str, channel: str, approved: bool) -> str:
    """Writing to the knowledge base is an admin decision, not a Tier 1 one."""
    if not APPROVERS:
        return ("Approvals are handled in the console. Set SLACK_APPROVERS to allow "
                "specific people to approve from Slack.")
    if user not in APPROVERS:
        return "You are not an approver for the knowledge base."
    if ADMIN_CHANNEL and channel != ADMIN_CHANNEL:
        return "Approvals are only accepted in the admin channel."

    def work() -> None:
        try:
            with SqliteSaver.from_conn_string(DB) as cp:
                out = graph().compile(checkpointer=cp).invoke(
                    Command(resume={"approved": approved}), {"configurable":
                                                             {"thread_id": session_id}})
            saved = bool(out.get("saved"))
            _remember(session_id, status="saved" if saved else "rejected")
            post(ADMIN_CHANNEL or channel,
                 f"Case for `{session_id}` "
                 f"{'saved to the knowledge base' if saved else 'discarded'} by <@{user}>.")
        except Exception:                          # noqa: BLE001
            log.exception("resume %s failed", session_id)
            post(ADMIN_CHANNEL or channel, f":warning: could not resume `{session_id}`.")

    threading.Thread(target=work, daemon=True).start()
    return f"Recorded. Processing `{session_id}`…"


# --------------------------------------------------------------------------
# Admission gate, shared by every entry point
# --------------------------------------------------------------------------

def accept(event_id: str, user: str, channel: str, thread_ts: str, text: str,
           files: list[dict] | None = None, ts: str | None = None) -> dict:
    """Decide whether to run, and start it if so. Pure enough to test without Slack."""
    if already_handled(event_id):
        return {"status": "duplicate"}
    ok, why = authorise(user, channel)
    if not ok:
        return {"status": "denied", "reply": why}
    in_thread = bool(ts) and ts != thread_ts
    # A bare mention is a request when it brings files, or when it is in a thread that
    # may hold something posted since the last answer — the worker checks which.
    if not text.strip() and not files and not in_thread:
        return {"status": "empty",
                "reply": "Tell me what the system is doing and I'll look into it."}
    if rate_limited(user):
        return {"status": "rate_limited",
                "reply": f"One diagnosis per {USER_COOLDOWN_S}s per person — "
                         f"each run costs several model calls."}
    session_id = start_session(user, channel, thread_ts, text, files, ts)
    ack = (f"Looking into it — session `{session_id}`. This usually takes a minute or two."
           + (f"\nReading {len(files)} attached file{'s' * (len(files) > 1)} first."
              if files else "")
           + ("\n:warning: running against recorded fixtures, not the live system."
              if transport.MODE == "mock" else ""))
    return {"status": "accepted", "session_id": session_id, "reply": ack}


# --------------------------------------------------------------------------
# Socket Mode wiring. Imported here so the module loads without slack_bolt.
# --------------------------------------------------------------------------

def parse_event(event: dict) -> dict | None:
    """A Slack message event -> a request, or None if it is not one."""
    # file_share is an ordinary message with files on it; every other subtype (edits,
    # joins, bot posts) is not a request.
    if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        return None
    return {"channel": event.get("channel", ""), "user": event.get("user", ""),
            "thread_ts": event.get("thread_ts") or event.get("ts"), "ts": event.get("ts"),
            # Only the LEADING mention is removed. Splitting on the first ">" also cut a
            # DM like "latency > 200ms" down to "200ms".
            "text": re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", event.get("text") or "").strip(),
            "files": event.get("files") or []}


def build_app():
    global _client
    from slack_bolt import App

    global BOT_USER_ID, BOT_ID
    bolt = App(token=BOT_TOKEN)
    _client = bolt.client
    # Who "the bot" is, to find its last answer in a thread and to tell a message that
    # mentioned it from one that did not.
    me = _client.auth_test()
    BOT_USER_ID, BOT_ID = me.get("user_id", ""), me.get("bot_id", "")

    def _handle(event: dict, body: dict) -> None:
        req = parse_event(event)
        if req is None:
            return
        channel, thread_ts = req["channel"], req["thread_ts"]
        out = accept(body.get("event_id", ""), req["user"], channel, thread_ts, req["text"],
                     req["files"], req["ts"])
        if reply := out.get("reply"):
            post(channel, reply, thread_ts)

    @bolt.event("app_mention")
    def on_mention(event, body):
        _handle(event, body)

    @bolt.event("message")
    def on_message(event, body):
        if event.get("channel_type") == "im":      # channel posts arrive as app_mention
            _handle(event, body)

    @bolt.command("/draft")
    def on_draft(ack, command, respond):
        ack()
        sid = (command.get("text") or "").strip()
        okd, why = authorise(command.get("user_id", ""), command.get("channel_id", ""))
        if not okd:
            return respond(why)
        msg = draft_for(sid)
        if not msg:
            return respond(f"No draft for `{sid}`.")
        flags = ", ".join(x["kind"] for x in msg.get("leaks", [])) or "none"
        respond(f"*Client-facing draft* — `{sid}`\n"
                f"_{'safe to send' if msg.get('safe_to_send') else 'NEEDS REVIEW: ' + str(msg.get('review_reason'))}"
                f" · flagged: {flags}_\n\n>>> {msg['text']}")

    @bolt.command("/approve")
    def on_approve(ack, command, respond):
        ack()
        respond(resume_approval((command.get("text") or "").strip(),
                                command.get("user_id", ""),
                                command.get("channel_id", ""), True))

    @bolt.command("/reject")
    def on_reject(ack, command, respond):
        ack()
        respond(resume_approval((command.get("text") or "").strip(),
                                command.get("user_id", ""),
                                command.get("channel_id", ""), False))

    return bolt


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not (BOT_TOKEN and APP_TOKEN):
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are both required.")
    if not (ALLOWED_CHANNELS or ALLOWED_USERS):
        log.warning("no allowlist configured — every request will be refused")
    log.info("mode=%s approvers=%s admin_channel=%s",
             transport.MODE, len(APPROVERS) or "console-only", ADMIN_CHANNEL or "unset")
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    SocketModeHandler(build_app(), APP_TOKEN).start()


if __name__ == "__main__":
    main()
