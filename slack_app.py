#!/usr/bin/env python3
"""Slack gateway — an ADAPTER, deliberately outside the state machine.

    Slack Events API ──> this script ──> graph.invoke() ──> Slack reply
                           (adapter)      (unchanged)

Slack is not a node. The graph knows nothing about channels, threads or Slack users;
this file is the only thing that does. The same graph is driven by run.py (CLI) and
console/server.py (web) without modification.

Four things this skeleton exists to get right, all of which bite in production:

1. THE 3-SECOND ACK. Slack retries any event it does not get a 200 for within 3s.
   A diagnostic run takes minutes. So: verify, enqueue, return 200 immediately, and
   do the work in the background.

2. RETRIES CAUSE DUPLICATES. Slack resends on timeout and on any 5xx. Without
   deduplication on event_id you run the same diagnosis several times in parallel.

3. THE APPROVAL INTERRUPT HAS TO GO SOMEWHERE — AND NOT TO THE CUSTOMER. The graph
   stops at propose_scenario waiting for a human. That decision is an internal one
   about our knowledge base; it must be routed to a staff channel, never posted in
   the customer's thread.

4. DRAFTS ARE NOT SENDS. customer_communicator produces a draft. If it leaked an
   internal identifier, or the report escalates, this gateway posts it for review
   instead of sending it to the client.

Run:
    pip install slack-sdk
    export SLACK_BOT_TOKEN=xoxb-...  SLACK_SIGNING_SECRET=...  SLACK_INTERNAL_CHANNEL=C0...
    uvicorn slack_app:app --port 3000
    # expose :3000 and point the Slack app's Event Subscriptions URL at /slack/events
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict

from fastapi import FastAPI, Request, Response
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from graph import build
from registry import load_tools

log = logging.getLogger("slack_gateway")

SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
INTERNAL_CHANNEL = os.environ.get("SLACK_INTERNAL_CHANNEL", "")
DB = os.environ.get("GRAPH_DB", "graph.db")
AUTO_SEND = os.environ.get("SLACK_AUTO_SEND", "0") == "1"

load_tools()
GRAPH = build()
app = FastAPI(title="support-agent slack gateway")

_seen: OrderedDict[str, float] = OrderedDict()   # event_id -> ts, for dedup
_threads: dict[str, dict] = {}                   # session_id -> slack origin
_lock = threading.Lock()


# --------------------------------------------------------------------------
# Slack plumbing
# --------------------------------------------------------------------------

def verify(body: bytes, ts: str, sig: str) -> bool:
    """Reject anything not signed by Slack. This endpoint is internet-facing."""
    if not SIGNING_SECRET or not ts or not sig:
        return False
    if abs(time.time() - int(ts)) > 60 * 5:       # replay window
        return False
    mac = hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256)
    return hmac.compare_digest(f"v0={mac.hexdigest()}", sig)


def already_handled(event_id: str) -> bool:
    """Slack resends on timeout; without this you diagnose the same ticket twice."""
    with _lock:
        now = time.time()
        for k, t in list(_seen.items()):
            if now - t > 3600:
                _seen.pop(k, None)
        if event_id in _seen:
            return True
        _seen[event_id] = now
        return False


def post(channel: str, text: str, thread_ts: str | None = None, blocks=None) -> None:
    """Replace with slack_sdk.WebClient in a real deployment."""
    try:
        from slack_sdk import WebClient
    except ImportError:
        log.info("[slack:%s thread=%s] %s", channel, thread_ts, text[:400])
        return
    WebClient(token=BOT_TOKEN).chat_postMessage(
        channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)


# --------------------------------------------------------------------------
# Graph driving
# --------------------------------------------------------------------------

def _diagnose(session_id: str, question: str, channel: str, thread_ts: str) -> None:
    """Run the graph to completion or to its first interrupt. Background thread."""
    cfg = {"configurable": {"thread_id": session_id}}
    with _lock:
        _threads[session_id] = {"channel": channel, "thread_ts": thread_ts}
    try:
        with SqliteSaver.from_conn_string(DB) as cp:
            out = GRAPH.compile(checkpointer=cp).invoke(
                {"question": question, "session_id": session_id,
                 "findings": [], "visited": [], "transcript": [],
                 "context": {}, "blocked": []}, cfg)
        _deliver(session_id, out, channel, thread_ts)
    except Exception:                              # noqa: BLE001
        log.exception("session %s failed", session_id)
        post(channel, "Sorry — something went wrong on our side while looking into this. "
                      "A support engineer has been notified.", thread_ts)
        if INTERNAL_CHANNEL:
            post(INTERNAL_CHANNEL, f":warning: session `{session_id}` crashed — see logs.")


def _deliver(session_id: str, out: dict, channel: str, thread_ts: str) -> None:
    """Customer-facing message to the thread; internal business to the staff channel."""
    msg = out.get("customer_message") or {}
    report = out.get("report") or {}

    if msg.get("text") and (msg.get("safe_to_send") or AUTO_SEND):
        post(channel, msg["text"], thread_ts)
    elif msg.get("text"):
        # Draft withheld: it leaked internal detail, or the report escalates.
        post(channel, "Thanks for flagging this — we're looking into it now and will "
                      "come back to you shortly with an update.", thread_ts)
        if INTERNAL_CHANNEL:
            leaks = ", ".join(x["kind"] for x in msg.get("leaks", [])) or "none"
            post(INTERNAL_CHANNEL,
                 f"*Customer reply held for review* — session `{session_id}`\n"
                 f"Reason: {msg.get('review_reason')} (flagged: {leaks})\n"
                 f"Thread: <#{channel}>\n\n>>> {msg['text']}")

    if INTERNAL_CHANNEL and report:
        blocked = "\n".join(f"• {b['note']}" for b in out.get("blocked", []))
        post(INTERNAL_CHANNEL,
             f"*Technical report* — session `{session_id}`\n"
             f"Root cause: {report.get('root_cause') or '_not determined_'}\n"
             f"Confidence: {report.get('confidence')}"
             f"{' · *escalated*' if report.get('escalate') else ''}\n"
             + (f"Agents blocked:\n{blocked}\n" if blocked else "")
             + f"Evidence:\n" + "\n".join(f"• {e}" for e in report.get("evidence", [])[:6]))

    # The approval interrupt is internal business. It never goes to the customer.
    if "__interrupt__" in out:
        req = out["__interrupt__"][0].value
        if INTERNAL_CHANNEL:
            post(INTERNAL_CHANNEL,
                 f"*New runbook scenario proposed* — session `{session_id}`\n"
                 f"Approve with `/approve {session_id}`, reject with `/reject {session_id}`, "
                 f"or use the console.\n\n```{req['preview_md'][:2500]}```")
        else:
            log.info("session %s awaiting approval; no internal channel configured", session_id)


def _resume(session_id: str, approved: bool, by: str) -> None:
    """Resume a graph parked at the approval gate. Called by the slash-command route."""
    cfg = {"configurable": {"thread_id": session_id}}
    with SqliteSaver.from_conn_string(DB) as cp:
        out = GRAPH.compile(checkpointer=cp).invoke(
            Command(resume={"approved": approved}), cfg)
    if INTERNAL_CHANNEL:
        post(INTERNAL_CHANNEL,
             f"Scenario for `{session_id}` {'saved to the knowledge base' if out.get('saved') else 'discarded'} "
             f"by <@{by}>.")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.post("/slack/events")
async def events(request: Request) -> Response:
    raw = await request.body()
    if not verify(raw, request.headers.get("X-Slack-Request-Timestamp", ""),
                  request.headers.get("X-Slack-Signature", "")):
        return Response(status_code=401)

    payload = json.loads(raw)
    if payload.get("type") == "url_verification":         # one-time handshake
        return Response(content=payload["challenge"], media_type="text/plain")

    # Ack a retry without re-running: Slack resends when we are slow, and we are slow.
    if request.headers.get("X-Slack-Retry-Num"):
        return Response(status_code=200)

    event = payload.get("event") or {}
    if (event.get("type") not in ("app_mention", "message")
            or event.get("bot_id") or event.get("subtype")):
        return Response(status_code=200)
    if already_handled(payload.get("event_id", "")):
        return Response(status_code=200)

    text = (event.get("text") or "").split(">", 1)[-1].strip()
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    session_id = uuid.uuid4().hex[:12]

    post(channel, "Got it — taking a look now. This usually takes a minute or two.", thread_ts)
    threading.Thread(target=_diagnose,
                     args=(session_id, text, channel, thread_ts), daemon=True).start()

    # Return inside 3s or Slack retries. The work continues in the thread above.
    return Response(status_code=200)


@app.post("/slack/commands")
async def commands(request: Request) -> dict:
    """`/approve <session>` and `/reject <session>`, restricted to the staff channel."""
    raw = await request.body()
    if not verify(raw, request.headers.get("X-Slack-Request-Timestamp", ""),
                  request.headers.get("X-Slack-Signature", "")):
        return {"text": "unauthorised"}

    form = dict(p.split("=", 1) for p in raw.decode().split("&") if "=" in p)
    from urllib.parse import unquote_plus
    cmd = unquote_plus(form.get("command", ""))
    session_id = unquote_plus(form.get("text", "")).strip()
    user = unquote_plus(form.get("user_id", ""))

    if INTERNAL_CHANNEL and unquote_plus(form.get("channel_id", "")) != INTERNAL_CHANNEL:
        return {"text": "Approvals are only accepted in the internal support channel."}
    if cmd not in ("/approve", "/reject") or not session_id:
        return {"text": "Usage: /approve <session_id>"}

    threading.Thread(target=_resume,
                     args=(session_id, cmd == "/approve", user), daemon=True).start()
    return {"text": f"Recorded. Processing {session_id}…"}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "pending_sessions": len(_threads),
            "internal_channel_configured": bool(INTERNAL_CHANNEL)}
