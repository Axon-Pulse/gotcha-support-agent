"""The running bot, seen from outside: its settings, its sites, its image, and Deploy.

Deploy is `make -C slack-bridge update`: the skill and KB are copied into the image, so an
edit reaches the bot only when the image is rebuilt. Whether it has been is answered by
comparing slack-bridge/skill_hash.sh over the files on disk with the label `make` stamped
on the image the container runs.
"""
import ast
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from console.settings import S

# Shown with their values. Anything else in slack-bridge/.env is shown as "set", never read
# out: that is where the Slack tokens and the Anthropic key live.
SHOWN = ("BRIDGE_MODEL", "BRIDGE_EFFORT", "BRIDGE_TIMEOUT_S", "BRIDGE_MAX_CONCURRENT",
         "BRIDGE_RTL_MARKS", "BRIDGE_ALLOWED_HOSTS", "BRIDGE_ALLOW_PING", "SLACK_ALLOWED_CHANNELS",
         "SLACK_ALLOWED_USERS", "SLACK_ALLOW_DMS", "GOTCHA_SSH_USER", "BOT_UID", "BOT_GID",
         "CLAUDE_BIN")
# bridge.py's defaults for the same names (a test keeps the two in step).
DEFAULTS = {"BRIDGE_MODEL": "claude-sonnet-5-5", "BRIDGE_EFFORT": "medium",
            "BRIDGE_TIMEOUT_S": "90", "BRIDGE_MAX_CONCURRENT": "2", "BRIDGE_RTL_MARKS": "1",
            "BRIDGE_ALLOWED_HOSTS": "axon-gotcha-[0-9]+", "BRIDGE_ALLOW_PING": "1",
            "SLACK_ALLOWED_CHANNELS": "", "SLACK_ALLOWED_USERS": "", "SLACK_ALLOW_DMS": "0",
            "GOTCHA_SSH_USER": "", "BOT_UID": "1000", "BOT_GID": "1000", "CLAUDE_BIN": "claude"}


def _run(argv: list[str], timeout: int = 20, **kw) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _env_lines() -> list[tuple[str, str]]:
    try:
        text = (S.bridge_dir / ".env").read_text()
    except (FileNotFoundError, PermissionError):
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def bot_env() -> dict[str, str]:
    """The allowlisted settings only. Nothing else from .env leaves this module."""
    return {k: v for k, v in _env_lines() if k in SHOWN}


def bot_config() -> dict:
    env = bot_env()
    settings = []
    for k in SHOWN:
        settings.append({"name": k, "value": env.get(k), "default": DEFAULTS.get(k, ""),
                         "set": k in env})
    hidden = sorted({k for k, _ in _env_lines() if k not in SHOWN})
    try:
        perms = json.loads((S.bridge_dir / "workspace" / ".claude" / "settings.json").read_text())
    except (FileNotFoundError, ValueError):
        perms = None
    guard_doc = ""
    try:
        guard_doc = ast.get_docstring(ast.parse((S.bridge_dir / "bash_guard.py").read_text())) or ""
    except (FileNotFoundError, SyntaxError):
        pass
    return {"env_file": (S.bridge_dir / ".env").exists(), "settings": settings,
            "secrets_set": hidden, "permissions": perms, "guard": guard_doc,
            "container": container()}


# --------------------------------------------------------------------------
# Container and image
# --------------------------------------------------------------------------

def container() -> dict:
    r = _run(["docker", "inspect", S.container])
    if r is None:
        return {"found": False, "error": "docker is not available to the console"}
    if r.returncode:
        return {"found": False, "error": (r.stderr or "").strip()[:300]}
    c = json.loads(r.stdout)[0]
    state = c.get("State") or {}
    out = {"found": True, "status": state.get("Status"), "running": state.get("Running"),
           "started": state.get("StartedAt"), "restarts": c.get("RestartCount"),
           "image_id": (c.get("Image") or "")[7:19], "skill_hash": None, "image_created": None}
    r = _run(["docker", "image", "inspect", c.get("Image", "")])
    if r is not None and not r.returncode:
        img = json.loads(r.stdout)[0]
        out["skill_hash"] = ((img.get("Config") or {}).get("Labels") or {}).get("gotcha.skill-hash")
        out["image_created"] = img.get("Created")
    return out


def disk_hash() -> str | None:
    r = _run([str(S.bridge_dir / "skill_hash.sh")], timeout=30)
    return r.stdout.strip() if r is not None and not r.returncode else None


def _hashed_files() -> list[Path]:
    """The same set skill_hash.sh hashes, for naming what changed since the image was built."""
    roots = [S.skill_dir / "SKILL.md", S.skill_dir / "kb", S.skill_dir / "scripts",
             S.bridge_dir / "bridge.py", S.bridge_dir / "bash_guard.py",
             S.bridge_dir / "requirements.txt", S.bridge_dir / "Dockerfile",
             S.bridge_dir / "docker-compose.yml",
             S.bridge_dir / "workspace"]
    out = []
    for r in roots:
        if r.is_file():
            out.append(r)
        elif r.is_dir():
            out += [p for p in r.rglob("*") if p.is_file() and not p.is_symlink()
                    and p.suffix != ".pyc"]
    return out


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", iso.replace("Z", "+00:00"))).timestamp()
    except ValueError:
        return None


def git_changes() -> list[str]:
    root = S.skill_dir.parent
    r = _run(["git", "-C", str(root), "status", "--porcelain", "--",
              str(S.skill_dir), str(S.bridge_dir)])
    if r is None or r.returncode:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------

class Deploy:
    """One `make update` at a time, run in the background, its output kept for the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.started = self.finished = None
        self.exit_code = None
        self.by = None

    def snapshot(self) -> dict:
        return {"running": self.running, "log": "".join(self.log)[-40000:],
                "started": self.started, "finished": self.finished,
                "exit_code": self.exit_code, "by": self.by}

    def start(self, by: str) -> None:
        from console import conversations, kb
        with self.lock:
            if self.running:
                raise RuntimeError("a deploy is already running")
            busy = conversations.in_flight()
            if busy:
                raise RuntimeError(f"{len(busy)} question(s) are being answered right now; a "
                                   "deploy restarts the bot and would cut them off. Try again "
                                   "in a minute.")
            t = kb.selftest()
            if not t["ok"]:
                raise RuntimeError("selftest.sh fails, so this KB is not deployed: "
                                   + "; ".join(t["failures"][:5]))
            self.running, self.log, self.exit_code, self.by = True, [], None, by
            self.started, self.finished = time.time(), None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        code = None
        try:
            env = {**os.environ, "TERM": "dumb"}
            p = subprocess.Popen(S.deploy_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, env=env, cwd=S.bridge_dir)
            for line in p.stdout:
                self.log.append(line)
            code = p.wait()
        except OSError as e:
            self.log.append(f"could not run {' '.join(S.deploy_cmd)}: {e}\n")
            code = -1
        finally:
            self.exit_code, self.finished, self.running = code, time.time(), False


DEPLOY = Deploy()


def deploy_status() -> dict:
    c = container()
    disk = disk_hash()
    built = _ts(c.get("image_created")) if c.get("found") else None
    changed = []
    if built:
        changed = sorted(str(p.relative_to(S.skill_dir.parent)) for p in _hashed_files()
                         if p.stat().st_mtime > built)
    image = c.get("skill_hash") if c.get("found") else None
    if not c.get("found"):
        state = "no-container"
    elif not image or image == "unknown":
        state = "unknown"          # built before the label existed, or by hand
    else:
        state = "deployed" if image == disk else "changed"
    return {"state": state, "disk_hash": disk, "image_hash": image, "changed": changed,
            "git": git_changes(), "container": c, "deploy": DEPLOY.snapshot()}


# --------------------------------------------------------------------------
# Systems
# --------------------------------------------------------------------------

def systems() -> dict:
    """list_systems.sh, narrowed to the bot's sites as the bridge narrows it, plus what each
    site last ran (systems.tsv, written by the triage)."""
    allowed = bot_env().get("BRIDGE_ALLOWED_HOSTS") or DEFAULTS["BRIDGE_ALLOWED_HOSTS"]
    r = _run([str(S.skill_dir / "scripts" / "list_systems.sh")], timeout=30,
             env={**os.environ, "GOTCHA_SYSTEMS_REGEX": allowed})
    rows, error = [], None
    if r is None:
        error = "list_systems.sh did not finish in 30s"
    elif r.returncode:
        error = (r.stderr or r.stdout).strip()[:500] or f"exit {r.returncode}"
    else:
        for line in r.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 5 and not line.startswith(("NOTE", "no peer")):
                rows.append({"name": f[0], "hostname": f[1], "ip": f[2], "state": f[3],
                             "via": f[4], "last_seen": " ".join(f[5:])})
    last = {}
    try:
        for line in S.systems_tsv.read_text().splitlines():
            f = line.split("\t")
            if len(f) >= 3:
                last[f[0]] = {"triaged": f[1], "release": f[2],
                              "tag": f[3] if len(f) > 3 else "",
                              "config": f[4] if len(f) > 4 else ""}
    except FileNotFoundError:
        pass
    for row in rows:
        row["last"] = last.get(row["name"]) or last.get(row["hostname"])
    names = {r["name"] for r in rows} | {r["hostname"] for r in rows}
    unlisted = [{"name": k, "last": v} for k, v in sorted(last.items()) if k not in names]
    return {"allowed": allowed, "systems": rows, "unlisted": unlisted, "error": error}
