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
    monkeypatch.setattr(S, "container", "no-such-container-for-tests")
    monkeypatch.setattr(S, "allowed_users", {"pm@example.com"})
    monkeypatch.setattr(S, "allow_local", True)
    return tmp_path


@pytest.fixture
def client(env):
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
