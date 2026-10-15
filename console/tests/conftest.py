"""Every test runs against its own copy of the skill and an empty bridge state, never the
real KB or the live bot's turns: the console writes, and a test that wrote to the real
support-agent-skill/ would change what the bot answers."""
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from console.settings import S  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    skill = tmp_path / "skill"
    shutil.copytree(ROOT / "support-agent-skill", skill, symlinks=True,
                    ignore=shutil.ignore_patterns("state", "sshkey*"))
    bridge = tmp_path / "bridge"
    (bridge / "state").mkdir(parents=True)
    (bridge / "workspace" / ".claude").mkdir(parents=True)
    shutil.copy(ROOT / "slack-bridge" / "workspace" / ".claude" / "settings.json",
                bridge / "workspace" / ".claude" / "settings.json")
    shutil.copy(ROOT / "slack-bridge" / "bash_guard.py", bridge / "bash_guard.py")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    monkeypatch.setattr(S, "skill_dir", skill)
    monkeypatch.setattr(S, "bridge_dir", bridge)
    monkeypatch.setattr(S, "state_dir", tmp_path / "console-state")
    monkeypatch.setattr(S, "transcript_dirs", [transcripts])
    monkeypatch.setattr(S, "systems_tsv", tmp_path / "systems.tsv")
    monkeypatch.setattr(S, "triage_dirs", [bridge / "state" / "triage"])
    monkeypatch.setattr(S, "container", "no-such-container-for-tests")
    monkeypatch.setattr(S, "allowed_users", {"pm@example.com", "admin@example.com"})
    monkeypatch.setattr(S, "allow_local", True)
    monkeypatch.setattr(S, "allowed_hosts", set())
    monkeypatch.setattr(S, "admins", {"admin@example.com"})
    monkeypatch.setattr(S, "local_role", "viewer")
    monkeypatch.setattr(S, "inventory_file", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(S, "device_secrets_file", tmp_path / "secrets.local.env")
    from console import auth
    monkeypatch.setattr(auth, "SESSIONS", {})
    from console import passkeys
    passkeys.reset_challenges()
    return tmp_path


class Clock:
    """auth's clock, moved by hand: each code() steps it on, as a real authenticator's
    codes do, so two codes in one test are never the same (which auth refuses)."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def code(self, who: str) -> str:
        import pyotp
        from console import auth
        self.t += 30
        return pyotp.TOTP(auth._load()[who]["secret"]).at(self.t)


@pytest.fixture
def clock(env, monkeypatch):
    from console import auth
    c = Clock()
    monkeypatch.setattr(auth, "_now", c)
    return c


def _signed_in_client():
    """A client already signed in as whoever its headers say it is, so the tests written
    before sign-in existed run unchanged. Sessions are made directly, not through
    /api/auth/login, so they leave no line in the audit log."""
    import httpx
    from fastapi.testclient import TestClient
    from console import auth
    from console.server import app

    class SignedIn(TestClient):
        tokens: dict[str, str] = {}

        def request(self, method, url, **kw):
            h = httpx.Headers(self.headers)
            h.update(kw.get("headers") or {})
            who = (h.get("tailscale-user-login") or "").strip().lower() or "local"
            if who not in self.tokens:
                self.tokens[who] = auth.new_session(who)
            self.cookies.set(auth.COOKIE, self.tokens[who])
            return super().request(method, url, **kw)

    c = SignedIn(app)
    c.tokens = {}
    c.headers.update({"x-console": "1"})
    return c


@pytest.fixture
def client(env, clock):
    return _signed_in_client()


@pytest.fixture
def raw_client(env, clock):
    """Not signed in: for the sign-in tests themselves."""
    from fastapi.testclient import TestClient
    from console.server import app
    c = TestClient(app)
    c.headers.update({"x-console": "1"})
    return c


def iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def write_transcript(env: Path, sid: str, turns: list[dict]) -> Path:
    """A Claude Code transcript in the shape it writes: per turn a prompt, tool calls with
    their results, the answer, and the running cost. turns: [{q, at, steps, answer, cost}]"""
    lines, total = [{"type": "queue-operation", "operation": "enqueue", "sessionId": sid}], 0.0
    for n, t in enumerate(turns):
        at = t["at"]
        lines.append({"type": "user", "timestamp": iso(at), "sessionId": sid,
                      "message": {"role": "user", "content": t["q"]}})
        lines.append({"type": "user", "isMeta": True, "timestamp": iso(at + 1),
                      "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill"}]}})
        for i, (name, inp, out, denied) in enumerate(t.get("steps", [])):
            tid = f"toolu_{n}_{i}"
            lines.append({"type": "assistant", "timestamp": iso(at + 2 + i), "message": {
                "role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
            res = {"type": "user", "timestamp": iso(at + 2.5 + i), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": denied}]}}
            if denied:
                res["toolDenialKind"] = "permission-rule"
            lines.append(res)
        if t.get("answer"):
            lines.append({"type": "assistant", "timestamp": iso(at + t.get("took", 30)), "message": {
                "role": "assistant", "content": [{"type": "text", "text": t["answer"]}]}})
        total += t.get("cost", 0.1)
        lines.append({"type": "cost-state", "totalCostUSD": total, "sessionId": sid})
    p = S.transcript_dirs[0] / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p


def write_turns(lines: list[dict]) -> None:
    S.turns_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
