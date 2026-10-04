"""The read-only guard: what the unattended bot may run, and — mostly — what it may not."""
import json
import os
import re
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


SCRIPT_CALLS = [
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3",
    f"{VIA_LINK}/run_triage.sh axon-gotcha-3",           # through the workspace symlink
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 --ping",
    f"GOTCHA_CONFIG=configs/axon-gotcha-4/full_system4.yaml {SCRIPTS}/run_triage.sh axon-gotcha-4",
    f"GOTCHA_SSH=ssh {SCRIPTS}/run_triage.sh axon-gotcha-3",
    f"GOTCHA_TRIAGE_TIMEOUT=18 {SCRIPTS}/run_triage.sh axon-gotcha-3",
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 2>&1",
    f"{SCRIPTS}/list_systems.sh gotcha 3",
    f"{SCRIPTS}/list_systems.sh",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo 100",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 gotcha30 200 --grep 'asu1|error'",
    f"{SCRIPTS}/code.sh grep axon-gotcha-3 'First node completed' src",
    f"{SCRIPTS}/code.sh show v1.3.0 src/launcher/main.cpp 140,170",
    f"{SCRIPTS}/code.sh show stable README.md 5",
    f"{SCRIPTS}/code.sh show axon-gotcha-4 src/nodes/magos_node/magos_client_node.cpp 526,568",
    f"{SCRIPTS}/code.sh show v1.3.0-38-g7c9b0167 GUItcha30/src/components/RadarTxDialog.tsx",
    f"{SCRIPTS}/code.sh log stable src/nodes",
    f"{SCRIPTS}/code.sh resolve axon-gotcha-4",
    f"{SCRIPTS}/code.sh sync",
    "tailscale ping axon-gotcha-3",
    "tailscale ping -c 1 axon-gotcha-3",
    "tailscale ip",
    "tailscale ip axon-gotcha-3",
    "tailscale version",
]


@pytest.mark.parametrize("cmd", SCRIPT_CALLS)
def test_the_bot_runs_the_skill_scripts(cmd):
    assert allowed(cmd), cmd


@pytest.mark.parametrize("cmd", [
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4",                         # no container
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 'dumbo; rm -rf /'",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo 100 -f",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo --follow",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo 100 --grep a --grep b",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo | tee /tmp/x",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 dumbo 100 --grep $(id)",
    f"{SCRIPTS}/lib_ssh.sh",
])
def test_remote_logs_is_one_container_read_and_nothing_else(cmd):
    assert not allowed(cmd), cmd


# --------------------------------------------------------------------------
# The bot composes no command of its own on a site
# --------------------------------------------------------------------------

NO_SSH = [
    "tailscale ssh axon-gotcha-3 'docker ps -a'",
    "tailscale ssh axon-gotcha-3 'uptime'",
    "tailscale ssh axon-gotcha-3 'docker logs --tail 200 gotcha30 2>&1 | grep -i error | tail -20'",
    "tailscale ssh axon-gotcha-3 'cat /home/gotcha/deploy/.en?'",
    "tailscale ssh axon-gotcha-3 'find /tmp -fprintf /tmp/x %p'",
    "tailscale ssh axon-gotcha-3 'sort --output=/tmp/x /etc/hostname'",
    "tailscale ssh axon-gotcha-3 'curl -XPOST http://127.0.0.1:8080/x'",
    "tailscale ssh axon-gotcha-3 'curl http://127.0.0.1:8080/ file:///etc/hostname'",
    "tailscale ssh axon-gotcha-3 'date -s 2020-01-01'",
    "tailscale ssh axon-gotcha-3",
    "ssh -o BatchMode=yes gotcha@axon-gotcha-3 'uptime'",
    "ssh axon-gotcha-3 uptime",
    "ssh -L 8080:127.0.0.1:8080 axon-gotcha-3 uptime",
    "ssh -F /tmp/cfg axon-gotcha-3 uptime",
    "scp axon-gotcha-3:/etc/hostname /tmp/x",
    "tailscale ssh axon-gotcha-3 'uptime' 'unbalanced",
]


@pytest.mark.parametrize("cmd", NO_SSH)
def test_the_bot_never_runs_ssh_or_a_command_of_its_own_on_a_site(cmd):
    """There used to be an opt-in mode (BRIDGE_FOLLOWUPS=1) that vetted such commands. Its list
    of read-only commands let through file writes (find -fprintf, sort --output=), POSTs
    (curl -XPOST) and secret reads (cat .en?), so the mode was deleted."""
    assert not allowed(cmd), cmd


def test_follow_up_mode_is_gone(monkeypatch):
    assert not hasattr(G, "FOLLOWUPS") and not hasattr(G, "check_remote")
    monkeypatch.setenv("BRIDGE_FOLLOWUPS", "1")                 # a stale setting does nothing
    assert not allowed("tailscale ssh axon-gotcha-3 'docker ps -a'")


@pytest.mark.parametrize("cmd,why", [
    ("rm -rf /", "local"),
    ("cat ~/.ssh/id_ed25519", "local"),
    ("bash -c 'tailscale ssh h reboot'", "shell"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 | tee /tmp/x", "local pipe"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 && reboot", "local chaining"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 > /tmp/x", "redirect"),
    (f"{SCRIPTS}/run_triage.sh $(id)", "substitution"),
    (f"{SCRIPTS}/code.sh clone", "setup step"),
    (f"{SCRIPTS}/triage_remote.sh", "not a bot entry point"),
    (f"{SCRIPTS}/redact.sed", "not a bot entry point"),
    ("PATH=/tmp/evil " + f"{SCRIPTS}/run_triage.sh axon-gotcha-3", "env"),
    ("/tmp/run_triage.sh axon-gotcha-3", "a lookalike outside the skill"),
    ("tailscale up --reset", "tailscale config"),
    ("tailscale down", "tailscale config"),
    ("tailscale set --accept-routes", "tailscale config"),
    ("make restart SERVICE=gateway", "make"),
    ("sudo reboot", "sudo"),
    ("curl http://127.0.0.1:8080/health", "the bot makes no requests of its own"),
    ("", "empty"),
])
def test_everything_else_is_closed(cmd, why):
    assert not allowed(cmd), f"{why}: {cmd}"


# --------------------------------------------------------------------------
# Only the gotcha sites: not a laptop, a server, or any other machine on the tailnet
# --------------------------------------------------------------------------

NOT_SITES = ["some-laptop", "prod-db", "axon-gotcha-", "axon-gotcha-x", "axon-gotcha-3x",
             "axon-gotcha-3.tailnet.ts.net", "root@axon-gotcha-3", "100.64.0.5", "localhost",
             "-oProxyCommand=id", "--help", "axon-gotcha-3;id", "AXON-GOTCHA-3"]


@pytest.mark.parametrize("host", NOT_SITES)
def test_a_name_that_is_not_a_site_is_refused_by_every_script_and_by_tailscale(host):
    quoted = f"'{host}'"
    for cmd in (f"{SCRIPTS}/run_triage.sh {quoted}",
                f"{SCRIPTS}/run_triage.sh {quoted} --ping",
                f"{SCRIPTS}/remote_logs.sh {quoted} dumbo",
                f"tailscale ping {quoted}",
                f"tailscale ping -c 1 {quoted}",
                f"tailscale ip {quoted}"):
        assert not allowed(cmd), cmd


def test_which_sites_are_allowed_is_one_setting_the_guard_and_the_bridge_share(monkeypatch):
    import bridge
    assert bridge.ALLOWED_HOSTS == G.DEFAULT_ALLOWED_HOSTS
    monkeypatch.setattr(G, "ALLOWED_HOSTS", re.compile(r"site-[0-9]+"))
    assert allowed(f"{SCRIPTS}/run_triage.sh site-7")
    assert not allowed(f"{SCRIPTS}/run_triage.sh axon-gotcha-3")


@pytest.mark.parametrize("cmd", [
    "tailscale status",
    "tailscale status --json",
    "tailscale status --peers=false",
])
def test_the_bot_cannot_list_the_whole_tailnet_with_tailscale_itself(cmd):
    """list_systems.sh is how it finds a site, and it lists only the allowed sites."""
    with pytest.raises(G.Denied, match="list_systems.sh"):
        G.check(cmd, WS)


@pytest.mark.parametrize("cmd", [
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 --foo",
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 --ping extra",
    f"{SCRIPTS}/run_triage.sh",
    "tailscale ping",
    "tailscale ping -c x axon-gotcha-3",
    "tailscale ping axon-gotcha-3 axon-gotcha-4",
    "tailscale ip axon-gotcha-3 axon-gotcha-4",
    "tailscale version --json",
])
def test_malformed_arguments_are_refused(cmd):
    assert not allowed(cmd), cmd


@pytest.mark.parametrize("assignment", [
    "GOTCHA_SSH_USER=gotcha",                             # the login is the host's own setting
    "GOTCHA_SSH_USER=root",
    "GOTCHA_SSH_USER='-oProxyCommand=touch /tmp/x #'",    # read by ssh as an option
    "GOTCHA_CONFIG=../../etc/passwd",
    "GOTCHA_CONFIG=/etc/passwd",
    "GOTCHA_CONFIG=-x",
    "GOTCHA_CONFIG=configs/../../x",
    "GOTCHA_SSH=-oProxyCommand=id",
    "GOTCHA_SSH=bash",
    "GOTCHA_TRIAGE_TIMEOUT=-k",
    "GOTCHA_TRIAGE_TIMEOUT=18s",
    "GOTCHA_STATE_DIR=/tmp/x",
    "GOTCHA_TRIAGE_DIR=/etc",
    "GOTCHA_SKILL_DIR=/tmp/evil",
    "BRIDGE_ALLOWED_HOSTS=.*",
    "GOTCHA_SYSTEMS_REGEX=.*",
    "LD_PRELOAD=/tmp/x.so",
])
def test_the_bot_cannot_set_the_environment_a_script_runs_in(assignment):
    assert not allowed(f"{assignment} {SCRIPTS}/run_triage.sh axon-gotcha-3"), assignment


# --------------------------------------------------------------------------
# The hook process, and what happens when it cannot run
# --------------------------------------------------------------------------

def run_hook(payload) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "bash_guard.py")],
                          input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True)


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "cwd": WS, "tool_input": {"command": command}}


def test_a_denial_blocks_with_exit_2_and_says_why():
    r = run_hook(bash("tailscale ssh axon-gotcha-3 'docker restart gateway'"))
    assert r.returncode == 2
    assert "not something the bot may run" in r.stderr


def test_an_allowed_command_passes_the_hook():
    assert run_hook(bash(f"{SCRIPTS}/run_triage.sh axon-gotcha-3")).returncode == 0


def test_a_malformed_event_fails_closed():
    """Any exit other than 2 lets the command run, so a crash must still be exit 2."""
    assert run_hook("not json").returncode == 2
    assert run_hook({"tool_name": "Bash", "tool_input": None}).returncode == 2


def test_other_tools_are_left_to_the_permission_rules():
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}).returncode == 0


SETTINGS = json.loads((HERE / "workspace" / ".claude" / "settings.json").read_text())
HOOK_COMMAND = SETTINGS["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def run_settings_hook(project_dir: Path, event: dict, path: str | None = None):
    """The hook command from settings.json, run the way Claude Code runs it."""
    env = {"CLAUDE_PROJECT_DIR": str(project_dir), "PATH": path if path is not None else os.environ["PATH"]}
    return subprocess.run(["/bin/sh", "-c", HOOK_COMMAND], input=json.dumps(event), env=env,
                          capture_output=True, text=True)


def fake_project(tmp_path: Path, guard_source: str | None) -> Path:
    """tmp/workspace/ with, beside it, a bash_guard.py: where the hook command looks."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    if guard_source is not None:
        (tmp_path / "bash_guard.py").write_text(guard_source)
    return ws


@pytest.mark.parametrize("name,guard_source,path", [
    ("a syntax error in the guard", "def broken(:\n", None),
    ("an import error in the guard", "import a_module_that_does_not_exist\n", None),
    ("a guard that crashes at start", "raise RuntimeError('boom')\n", None),
    ("a guard that exits 1", "import sys; sys.exit(1)\n", None),
    ("a guard that is missing", None, None),
    ("no python3 on the path", "import sys; sys.exit(0)\n", "/nonexistent"),
])
def test_when_the_guard_cannot_run_the_command_is_blocked_not_allowed(tmp_path, name, guard_source, path):
    """Claude Code runs the command when a hook exits with anything but 2. This is the case
    that used to leave Bash unrestricted (e.g. python3 older than 3.10 failing on `str | None`)."""
    ws = fake_project(tmp_path, guard_source)
    r = run_settings_hook(ws, bash("rm -rf /"), path)
    assert r.returncode == 2, (name, r.returncode, r.stderr)


def test_a_guard_that_exits_0_still_lets_a_command_through(tmp_path):
    """The wrapper turns failures into blocks; it must not turn every run into one."""
    ws = fake_project(tmp_path, "import sys; sys.exit(0)\n")
    assert run_settings_hook(ws, bash("x")).returncode == 0


def test_the_real_hook_command_allows_and_blocks():
    assert run_settings_hook(Path(WS), bash(f"{SCRIPTS}/list_systems.sh")).returncode == 0
    assert run_settings_hook(Path(WS), bash("rm -rf /")).returncode == 2
    assert run_settings_hook(Path(WS), bash("tailscale ssh axon-gotcha-3 uptime")).returncode == 2


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


# --------------------------------------------------------------------------
# code.sh: its range goes into a sed script, and sed's `e` command runs a shell command
# --------------------------------------------------------------------------

# Each is code execution, or a file read or write, if it reaches `sed -n "${s},${e}{=;p}"`.
BAD_RANGES = [
    "1,2e touch {p}",            # sed `e`: runs the command. This ran as the bot user.
    "1e touch {p}",
    "1,2p;e touch {p}",
    "1,2w {p}",                  # sed `w`: writes a file
    "1,1r {p}",                  # sed `r`: reads one into the output
    "1,2{{e touch {p}}}",
    "1,$(touch {p})",            # command substitution, in case it is ever expanded
    "1,`touch {p}`",
    "$((1))",
    "a[$(touch {p})]",           # bash arithmetic subscripts evaluate substitutions too
    "1,",
    ",5",
    "1,2,3",
    "-5",
    "0x10",
    "1 2",
    "1;2",
    "",                          # empty is fine for the script (whole file); not for the guard's arg
]


@pytest.mark.parametrize("rng", [r for r in BAD_RANGES if r])
def test_the_guard_refuses_a_show_range_that_is_not_line_numbers(strict, rng):
    cmd = f"{SCRIPTS}/code.sh show stable README.md '{rng.format(p='/tmp/x')}'"
    assert not allowed(cmd), cmd


@pytest.mark.parametrize("cmd", [
    f"{SCRIPTS}/code.sh show 'v1;id' README.md 1,5",                 # version
    f"{SCRIPTS}/code.sh show --output=/tmp/x README.md 1,5",
    f"{SCRIPTS}/code.sh show stable 'a b' 1,5",                       # file
    f"{SCRIPTS}/code.sh show stable -x 1,5",
    f"{SCRIPTS}/code.sh show stable README.md 1,5 extra",             # too many arguments
    f"{SCRIPTS}/code.sh show stable",                                 # no file
    f"{SCRIPTS}/code.sh grep -p x src",                               # a version cannot be a flag
    f"{SCRIPTS}/code.sh log '--output=/tmp/x' src",
    f"{SCRIPTS}/code.sh resolve ''",
    f"{SCRIPTS}/code.sh sync origin",
    f"{SCRIPTS}/code.sh clone",
])
def test_the_guard_refuses_malformed_code_sh_arguments(strict, cmd):
    assert not allowed(cmd), cmd


@pytest.mark.parametrize("rng", [r for r in BAD_RANGES if r])
def test_code_sh_itself_refuses_such_a_range_before_doing_anything(tmp_path, rng):
    """The script is the authority; the guard is the second line. This calls the real script."""
    canary = tmp_path / "pwn"
    env = {**os.environ, "HOME": str(tmp_path), "GOTCHA_REPO": str(tmp_path / "none")}
    r = subprocess.run([str(SCRIPTS / "code.sh"), "show", "stable", "README.md",
                        rng.format(p=canary)], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "line numbers" in r.stderr
    assert not canary.exists(), "the range was executed"


@pytest.mark.parametrize("rng", ["5", "140,170", "1,2", ""])
def test_code_sh_still_accepts_a_real_range(tmp_path, rng):
    """Past the range check, with no clone to read the script stops at 'no gotcha30 clone'."""
    env = {**os.environ, "HOME": str(tmp_path), "GOTCHA_REPO": str(tmp_path / "none")}
    r = subprocess.run([str(SCRIPTS / "code.sh"), "show", "stable", "README.md", rng],
                       env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 3 and "no gotcha30 clone" in r.stderr, (r.returncode, r.stderr)


# --------------------------------------------------------------------------
# code.sh: its range goes into a sed script, and sed's `e` command runs a shell command
# --------------------------------------------------------------------------

# Each is code execution, or a file read or write, if it reaches `sed -n "${s},${e}{=;p}"`.
BAD_RANGES = [
    "1,2e touch {p}",            # sed `e`: runs the command. This ran as the bot user.
    "1e touch {p}",
    "1,2p;e touch {p}",
    "1,2w {p}",                  # sed `w`: writes a file
    "1,1r {p}",                  # sed `r`: reads one into the output
    "1,2{{e touch {p}}}",
    "1,$(touch {p})",            # command substitution, in case it is ever expanded
    "1,`touch {p}`",
    "$((1))",
    "a[$(touch {p})]",           # bash arithmetic subscripts evaluate substitutions too
    "1,",
    ",5",
    "1,2,3",
    "-5",
    "0x10",
    "1 2",
    "1;2",
    "",                          # empty is fine for the script (whole file); not for the guard's arg
]


@pytest.mark.parametrize("rng", [r for r in BAD_RANGES if r])
def test_the_guard_refuses_a_show_range_that_is_not_line_numbers(rng):
    cmd = f"{SCRIPTS}/code.sh show stable README.md '{rng.format(p='/tmp/x')}'"
    assert not allowed(cmd), cmd


@pytest.mark.parametrize("cmd", [
    f"{SCRIPTS}/code.sh show 'v1;id' README.md 1,5",                 # version
    f"{SCRIPTS}/code.sh show --output=/tmp/x README.md 1,5",
    f"{SCRIPTS}/code.sh show stable 'a b' 1,5",                       # file
    f"{SCRIPTS}/code.sh show stable -x 1,5",
    f"{SCRIPTS}/code.sh show stable README.md 1,5 extra",             # too many arguments
    f"{SCRIPTS}/code.sh show stable",                                 # no file
    f"{SCRIPTS}/code.sh grep -p x src",                               # a version cannot be a flag
    f"{SCRIPTS}/code.sh log '--output=/tmp/x' src",
    f"{SCRIPTS}/code.sh resolve ''",
    f"{SCRIPTS}/code.sh sync origin",
    f"{SCRIPTS}/code.sh clone",
])
def test_the_guard_refuses_malformed_code_sh_arguments(cmd):
    assert not allowed(cmd), cmd


@pytest.mark.parametrize("rng", [r for r in BAD_RANGES if r])
def test_code_sh_itself_refuses_such_a_range_before_doing_anything(tmp_path, rng):
    """The script is the authority; the guard is the second line. This calls the real script."""
    canary = tmp_path / "pwn"
    env = {**os.environ, "HOME": str(tmp_path), "GOTCHA_REPO": str(tmp_path / "none")}
    r = subprocess.run([str(SCRIPTS / "code.sh"), "show", "stable", "README.md",
                        rng.format(p=canary)], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "line numbers" in r.stderr
    assert not canary.exists(), "the range was executed"


@pytest.mark.parametrize("rng", ["5", "140,170", "1,2", ""])
def test_code_sh_still_accepts_a_real_range(tmp_path, rng):
    """Past the range check, with no clone to read the script stops at 'no gotcha30 clone'."""
    env = {**os.environ, "HOME": str(tmp_path), "GOTCHA_REPO": str(tmp_path / "none")}
    r = subprocess.run([str(SCRIPTS / "code.sh"), "show", "stable", "README.md", rng],
                       env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 3 and "no gotcha30 clone" in r.stderr, (r.returncode, r.stderr)
