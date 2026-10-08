#!/usr/bin/env python3
"""Slack -> Claude Code (headless) running the gotcha-support skill -> Slack.

The whole bot is this file. There is no agent code here: the skill in
support-agent-skill/ is the brain, and this only carries messages in and answers out.

    @bot the map is empty on gotcha 3
        │  Socket Mode (outbound WebSocket, no inbound port)
        ▼
    bridge.py ── posts "Checking…" in the thread at once
        │
        │  claude -p --resume <thread's session>  (message on stdin)   cwd = workspace/
        │    · the skill loads from workspace/.claude/skills/gotcha-support
        │    · permission mode dontAsk: anything not allowed is refused, nobody is asked
        │    · bash_guard.py vets every Bash call; only read-only checks run
        ▼
    chat.update the "Checking…" message with the answer

ONE SLACK THREAD IS ONE CLAUDE SESSION. A reply in the thread resumes the same session,
so a follow-up ("what about the camera?") sees what was already found, and a new
top-level message starts fresh. The mapping lives in state/threads.json.

WHY IT MUST RUN ON THE TAILNET. The skill reaches the sites with `tailscale ssh`, so this
runs on a machine that can. Claude in Slack runs in Anthropic's cloud and cannot reach a
private network, which is the reason this file exists at all.

Run:
    pip install -r requirements.txt
    export SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... SLACK_ALLOWED_CHANNELS=C0...
    python bridge.py
"""
import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("slack_bridge")
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"
STATE = HERE / "state" / "threads.json"

_csv = lambda v: {x.strip() for x in os.environ.get(v, "").split(",") if x.strip()}  # noqa: E731
ALLOWED_CHANNELS = _csv("SLACK_ALLOWED_CHANNELS")
ALLOWED_USERS = _csv("SLACK_ALLOWED_USERS")      # empty = anyone in an allowed channel
ALLOW_DMS = os.environ.get("SLACK_ALLOW_DMS") == "1"

CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
MODEL = os.environ.get("BRIDGE_MODEL", "claude-sonnet-5-5")       # empty = Claude Code's default
EFFORT = os.environ.get("BRIDGE_EFFORT", "medium")
TIMEOUT_S = int(os.environ.get("BRIDGE_TIMEOUT_S", "30"))
MAX_CONCURRENT = int(os.environ.get("BRIDGE_MAX_CONCURRENT", "2"))
# The machines the bot may reach. bash_guard.py reads the same variable (it inherits this
# environment through claude), and list_systems.sh lists only these. Keep the default in step
# with bash_guard.DEFAULT_ALLOWED_HOSTS (a test checks).
ALLOWED_HOSTS = os.environ.get("BRIDGE_ALLOWED_HOSTS") or r"axon-gotcha-[0-9]+"
_SLACK_LIMIT = 3900                              # keep one answer in one readable message

# Appended to Claude Code's system prompt on every turn. The skill says how to diagnose;
# this says who is asking, where the answer lands, and what the bot may not do.
SLACK_PROMPT = """\
You are answering a PM in a Slack thread about a deployed gotcha system. Use the
gotcha-support skill for anything about a gotcha site, sensor, UI or config.

Be fast. Take the skill's shortest path to a supported answer: one triage, the cases it
points at, a source lookup (skill §4) for any fix you are about to recommend, and stop.
Do not start a second investigation — if it is still unclear, say so and name the single
next check.

Reply in Slack mrkdwn (*bold*, `code`, ``` blocks; no # headings, no tables), in this
shape, under about 150 words not counting commands:

*<system>: <one-line verdict in plain words>*
*Tell the customer:* 1–2 sentences the PM can paste as-is. No node names, IPs, PIDs,
paths, container or tool names. If it escalates, a holding message.
*You do:*
1. <step> — `exact command`, copied from the KB case, with real values filled in
*Confirm fixed:* one line.
*Confidence:* high/medium/low · verified on the machine or not · what was ruled out.

You are read-only and unattended. You cannot write files or change a system, so never
offer to; every fix is a step for the PM. If the PM did not say which system and more
than one could match, ask one short question listing the candidates, and stop.

Only the skill's scripts are available here (run_triage.sh, remote_logs.sh, list_systems.sh,
code.sh). You cannot run your own commands on a site, so skip the skill's targeted follow-ups
(§5): answer from the triage output and the KB, and when one more check would settle
it, give that check to the PM as a step with its exact command.
"""


def system_prompt() -> str:
    return SLACK_PROMPT


SKILL_DIR = (WORKSPACE / ".claude" / "skills" / "gotcha-support").resolve()
# Secret-looking file names, denied under the skill directory as well (see permission_overlay).
_SECRET_FILES = (".env", ".env.*", "secrets.local.env", "*.pem", "*.key", "id_*")


def permission_overlay() -> str:
    """Read rules that need this machine's absolute paths, passed as --settings JSON.

    workspace/.claude/settings.json allows reading the workspace and nothing else, but the
    skill in it is a symlink, and a read is judged on the path it resolves to, so the real
    skill directory has to be allowed by its absolute path. The `**/.env` style denies in
    settings.json only match under the workspace, so the same file names are denied here for
    the skill directory. Everything else on the host stays unreadable. (`//path` is Claude
    Code's absolute-path form.)"""
    root = f"/{SKILL_DIR}"
    return json.dumps({"permissions": {
        "allow": [f"Read({root}/**)"],
        "deny": [f"Read({root}/**/{name})" for name in _SECRET_FILES]}})


def claude_argv(session_id: str | None) -> list[str]:
    """The command line. The Slack message is NOT in it: it goes in on stdin (run_claude).
    As an argument, a message such as `--version` or `--allowedTools=Bash` is parsed as a
    flag of the claude CLI, and anyone in the channel could set flags."""
    argv = [CLAUDE, "-p",
            "--output-format", "json",
            "--permission-mode", "dontAsk",
            # Project settings only: this host's own ~/.claude settings must not widen
            # what the bot may do. Managed settings still apply, as they should.
            "--setting-sources", "project",
            "--settings", permission_overlay(),
            "--append-system-prompt", system_prompt(),
            "--effort", EFFORT]
    if MODEL:
        argv += ["--model", MODEL]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def child_env() -> dict[str, str]:
    """The bot's environment minus its Slack tokens. Nothing the model runs needs them."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    # What the guard allows and what list_systems.sh lists: set here, never by a message.
    env["BRIDGE_ALLOWED_HOSTS"] = ALLOWED_HOSTS
    env["GOTCHA_SYSTEMS_REGEX"] = ALLOWED_HOSTS
    # The triage must end, with whatever it has, well before the turn is killed: a killed turn
    # answers nothing, a short triage answers with less. Leave the model time to write.
    env.setdefault("GOTCHA_TRIAGE_TIMEOUT", str(max(10, TIMEOUT_S - 12)))
    return env


def claude_config_file() -> Path:
    """The file Claude Code keeps its per-project state in: under CLAUDE_CONFIG_DIR when that is
    set (the container sets it to the volume), else ~/.claude.json."""
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) / ".claude.json" if d else Path.home() / ".claude.json"


def ensure_workspace_trusted() -> None:
    """Mark the bot's workspace as trusted in Claude Code's own config.

    Without it Claude Code ignores the `permissions.allow` list in workspace/.claude/settings.json
    ("this workspace has not been trusted"), so in dontAsk mode every Bash and Read is refused and
    the bot answers "I couldn't run any checks". On a host that is a trust prompt somebody accepted
    once; a fresh container has none to accept. The workspace and its settings are ours (root-owned
    in the image, and the bot has no write tool), so trusting them is the intent."""
    path = claude_config_file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except ValueError:
        raise SystemExit(f"{path} is not valid JSON; not starting")
    project = data.setdefault("projects", {}).setdefault(str(WORKSPACE), {})
    if project.get("hasTrustDialogAccepted") is True:
        return
    project["hasTrustDialogAccepted"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    log.info("marked %s as trusted in %s", WORKSPACE, path)


def verify_guard() -> None:
    """Refuse to run unless the Bash guard, started the way Claude Code starts it, blocks a
    command it must and passes one it should. Claude Code runs a command when its hook fails any
    way other than exit 2, so a guard that can't start (no python3, a syntax error, a moved file)
    would leave Bash unrestricted. settings.json makes such a failure a block; this proves it."""
    hook = json.loads((WORKSPACE / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]
    command = hook["hooks"][0]["command"]
    env = {**child_env(), "CLAUDE_PROJECT_DIR": str(WORKSPACE)}

    def run(cmd: str) -> subprocess.CompletedProcess:
        event = {"tool_name": "Bash", "cwd": str(WORKSPACE), "tool_input": {"command": cmd}}
        return subprocess.run(["/bin/sh", "-c", command], input=json.dumps(event), env=env,
                              capture_output=True, text=True, timeout=20)

    blocked = run("rm -rf /")
    if blocked.returncode != 2:
        raise SystemExit("the Bash guard did not block `rm -rf /` "
                         f"(exit {blocked.returncode}: {blocked.stderr.strip()[:200]}); not starting")
    passed = run(f"{SKILL_DIR}/scripts/list_systems.sh")
    if passed.returncode != 0:
        raise SystemExit("the Bash guard blocked the skill's own list_systems.sh "
                         f"(exit {passed.returncode}: {passed.stderr.strip()[:200]}); not starting")


def parse_result(stdout: str) -> dict:
    """claude -p --output-format json -> {text, session_id, ok, seconds, turns, cost}."""
    try:
        r = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "text": "The assistant returned something unreadable.",
                "session_id": None}
    text = (r.get("result") or "").strip()
    ok = not r.get("is_error") and r.get("subtype") == "success" and bool(text)
    return {"ok": ok,
            "text": text or f"The assistant stopped without an answer ({r.get('subtype')}).",
            "session_id": r.get("session_id"),
            "seconds": round((r.get("duration_ms") or 0) / 1000),
            "turns": r.get("num_turns"),
            "cost": r.get("total_cost_usd")}


def footer(res: dict) -> str:
    bits = [f"{res['seconds']}s" if res.get("seconds") else None,
            f"{res['turns']} steps" if res.get("turns") else None,
            f"${res['cost']:.2f}" if isinstance(res.get("cost"), (int, float)) else None]
    return "_" + " · ".join(b for b in bits if b) + "_" if any(bits) else ""


def fit(text: str) -> str:
    if len(text) <= _SLACK_LIMIT:
        return text
    return text[:_SLACK_LIMIT].rsplit("\n", 1)[0] + "\n_…cut to fit one message._"


# --------------------------------------------------------------------------
# Thread -> session
# --------------------------------------------------------------------------

class ThreadStore:
    """channel:thread_ts -> Claude session id, on disk so a restart keeps threads."""

    def __init__(self, path: Path = STATE):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, channel: str, thread_ts: str) -> str | None:
        with self._lock:
            return self._load().get(f"{channel}:{thread_ts}")

    def put(self, channel: str, thread_ts: str, session_id: str) -> None:
        with self._lock:
            d = self._load()
            d[f"{channel}:{thread_ts}"] = session_id
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1))
            tmp.replace(self.path)


# --------------------------------------------------------------------------
# Running one turn
# --------------------------------------------------------------------------

def run_claude(message: str, session_id: str | None) -> dict:
    """One headless Claude Code turn. Never raises: a failure is an answer to post."""
    started = time.monotonic()
    # Own process group, so a timeout also ends the ssh sessions the turn started.
    p = subprocess.Popen(claude_argv(session_id), cwd=WORKSPACE, env=child_env(),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(input=message, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return {"ok": False, "session_id": session_id,
                "text": f"No answer within {TIMEOUT_S}s — the site link may be slow. "
                        "Nothing was changed on the system; ask again, or name the one "
                        "thing to check."}
    res = parse_result(out)
    if p.returncode and not res["ok"]:
        log.error("claude exited %s: %s", p.returncode, (err or "")[-500:])
    res["seconds"] = res.get("seconds") or round(time.monotonic() - started)
    res["session_id"] = res.get("session_id") or session_id
    return res


def authorise(user: str, channel: str) -> tuple[bool, str]:
    """Fail closed: an unset allowlist admits nobody."""
    if not ALLOWED_CHANNELS and not ALLOWED_USERS:
        return False, "This bot has no allowlist configured, so it is refusing everyone."
    if channel.startswith("D"):
        if ALLOW_DMS and user in ALLOWED_USERS:
            return True, ""
        return False, "Direct messages are off for this bot. Ask in the support channel."
    if ALLOWED_CHANNELS and channel not in ALLOWED_CHANNELS:
        return False, "This channel is not approved for the support bot."
    if ALLOWED_USERS and user not in ALLOWED_USERS:
        return False, "You are not on the allowlist for this bot."
    return True, ""


class Bridge:
    def __init__(self, client, store: ThreadStore | None = None):
        self.client = client
        self.store = store or ThreadStore()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._threads: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def _duplicate(self, event_id: str) -> bool:
        """Socket Mode redelivers; without this one question runs twice."""
        with self._lock:
            if not event_id or event_id in self._seen:
                return bool(event_id)
            self._seen[event_id] = None
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
            return False

    def _thread_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._threads.setdefault(key, threading.Lock())

    def handle(self, event: dict, event_id: str = "") -> None:
        if event.get("bot_id") or event.get("subtype") or self._duplicate(event_id):
            return
        channel, user = event.get("channel", ""), event.get("user", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        text = " ".join(w for w in (event.get("text") or "").split()
                        if not w.startswith("<@")).strip()
        ok, why = authorise(user, channel)
        if not ok:
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=why)
            return
        if not text:
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                         text="Tell me what the system is doing and I'll look.")
            return
        ack = self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                           text=":mag: Checking…")
        threading.Thread(target=self._answer, daemon=True,
                         args=(channel, thread_ts, ack["ts"], text)).start()

    def _answer(self, channel: str, thread_ts: str, ack_ts: str, text: str) -> None:
        # One turn at a time per thread, so a quick follow-up resumes the session the
        # first turn created instead of starting a second one beside it.
        with self._thread_lock(f"{channel}:{thread_ts}"), self._slots:
            res = run_claude(text, self.store.get(channel, thread_ts))
            if res.get("session_id"):
                self.store.put(channel, thread_ts, res["session_id"])
        body = fit(res["text"]) + (f"\n{footer(res)}" if footer(res) else "")
        self.client.chat_update(channel=channel, ts=ack_ts, text=body)


def make_app(token: str, **kw):
    """The Bolt app. Signature verification is for Slack's HTTP mode: under Socket Mode
    no request is signed (the xapp- token authenticates the connection), and Bolt 1.27
    refuses to start without a signing secret unless the check is switched off."""
    from slack_bolt import App
    return App(token=token, request_verification_enabled=False, **kw)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot, app_token = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN")
    if not (bot and app_token):
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are both required.")
    if not (ALLOWED_CHANNELS or ALLOWED_USERS):
        log.warning("no allowlist configured — every request will be refused")
    if not (WORKSPACE / ".claude" / "skills" / "gotcha-support" / "SKILL.md").exists():
        raise SystemExit(f"the skill is not reachable from {WORKSPACE}/.claude/skills")

    ensure_workspace_trusted()
    verify_guard()
    app = make_app(bot)
    bridge = Bridge(app.client)

    @app.event("app_mention")
    def on_mention(event, body):
        bridge.handle(event, body.get("event_id", ""))

    @app.event("message")
    def on_message(event, body):
        if event.get("channel_type") == "im":   # channel posts arrive as app_mention
            bridge.handle(event, body.get("event_id", ""))

    log.info("bridge up: effort=%s model=%s timeout=%ss sites=%s", EFFORT,
             MODEL or "default", TIMEOUT_S, ALLOWED_HOSTS)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
