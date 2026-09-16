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
4. APPROVAL IS NOT A TIER 1 DECISION. Writing to the knowledge base is admin business and
   defaults to the console. It is available in Slack only if SLACK_APPROVERS names people.

Run:
    pip install slack-bolt
    export SLACK_BOT_TOKEN=xoxb-...       # bot token
    export SLACK_APP_TOKEN=xapp-...       # app-level token, connections:write
    export SLACK_ALLOWED_CHANNELS=C0...   # fail-closed: unset means nobody
    export SLACK_ADMIN_CHANNEL=C0...
    python slack_app.py
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import config_store
import transport
from graph import build
from registry import load_tools

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

def _write_trace(session_id: str, out: dict, origin: dict) -> None:
    """Same shape the CLI and console write, plus who asked and from where.

    Slack is about to become the primary interface; an interface with no audit trail is
    the wrong one to make primary.
    """
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{session_id}.jsonl").open("a") as f:
        f.write(json.dumps({
            "origin": {"via": "slack", **origin, "mode": transport.MODE, "ts": time.time()},
            "findings": out.get("findings", []),
            "transcript": out.get("transcript", []),
            "report": out.get("report"),
            "customer_message": out.get("customer_message"),
            "blocked": out.get("blocked", []),
            "saved": bool(out.get("saved")),
        }, default=str) + "\n")


def start_session(user: str, channel: str, thread_ts: str, text: str) -> str:
    """One message, one fresh session id. Never a reused checkpoint."""
    session_id = uuid.uuid4().hex[:12]
    _remember(session_id, channel=channel, thread_ts=thread_ts, user=user, question=text,
              status="running")
    threading.Thread(target=_diagnose, daemon=True,
                     args=(session_id, text, channel, thread_ts, user)).start()
    return session_id


def _diagnose(session_id: str, question: str, channel: str, thread_ts: str,
              user: str) -> None:
    origin = {"user": user, "channel": channel, "thread_ts": thread_ts,
              "question": question}
    acquired = _running.acquire(timeout=900)
    if not acquired:
        post(channel, "Still working through a queue of diagnoses — try again shortly.",
             thread_ts)
        return
    try:
        cfg = {"configurable": {"thread_id": session_id}}
        with SqliteSaver.from_conn_string(DB) as cp:
            out = graph().compile(checkpointer=cp).invoke(
                {"question": question, "session_id": session_id,
                 "findings": [], "visited": [], "transcript": [],
                 "context": {}, "blocked": []}, cfg)
        _write_trace(session_id, out, origin)
        _remember(session_id, status="done", report=out.get("report"),
                  customer_message=out.get("customer_message"))
        _deliver(session_id, out, channel, thread_ts)
    except Exception:                              # noqa: BLE001
        log.exception("session %s failed", session_id)
        _remember(session_id, status="error")
        post(channel, f"That run failed — session `{session_id}`. The error is in the "
                      f"gateway log; nothing was changed on the system.", thread_ts)
    finally:
        _running.release()


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

def accept(event_id: str, user: str, channel: str, thread_ts: str, text: str) -> dict:
    """Decide whether to run, and start it if so. Pure enough to test without Slack."""
    if already_handled(event_id):
        return {"status": "duplicate"}
    ok, why = authorise(user, channel)
    if not ok:
        return {"status": "denied", "reply": why}
    if not text.strip():
        return {"status": "empty",
                "reply": "Tell me what the system is doing and I'll look into it."}
    if rate_limited(user):
        return {"status": "rate_limited",
                "reply": f"One diagnosis per {USER_COOLDOWN_S}s per person — "
                         f"each run costs several model calls."}
    session_id = start_session(user, channel, thread_ts, text)
    ack = (f"Looking into it — session `{session_id}`. This usually takes a minute or two."
           + ("\n:warning: running against recorded fixtures, not the live system."
              if transport.MODE == "mock" else ""))
    return {"status": "accepted", "session_id": session_id, "reply": ack}


# --------------------------------------------------------------------------
# Socket Mode wiring. Imported here so the module loads without slack_bolt.
# --------------------------------------------------------------------------

def build_app():
    global _client
    from slack_bolt import App

    bolt = App(token=BOT_TOKEN)
    _client = bolt.client

    def _handle(event: dict, body: dict) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return
        channel, user = event.get("channel", ""), event.get("user", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        text = (event.get("text") or "").split(">", 1)[-1].strip()
        out = accept(body.get("event_id", ""), user, channel, thread_ts, text)
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
