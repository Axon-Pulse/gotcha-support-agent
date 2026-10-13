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
# Chained and filtered reads: split into parts, every part checked on its own
# --------------------------------------------------------------------------

SKILL = f"{WS}/.claude/skills/gotcha-support"

# What the bot sent in real conversations and the old guard refused (paths as in the container,
# moved to this checkout).
CHAINED = [
    f"cd {SKILL} && scripts/list_systems.sh gotcha 4",
    f"cd {SKILL} && scripts/list_systems.sh axon-gotcha-265",
    f"cd {SKILL} && scripts/list_systems.sh",
    f"cd {SKILL} && scripts/run_triage.sh axon-gotcha-4 --ping",
    f"cd {SKILL} && cat kb/escalation.md",
    f"cat {SKILL}/kb/cases/radar-transmitter-off.md",
    f"cd {SKILL} && scripts/list_systems.sh gotcha 4; grep -ril 'firmware' kb | head",
    f"cd {SKILL} && scripts/run_triage.sh axon-gotcha-4 --ping 2>&1 | "
    "grep -iE 'firmware|version|release|magos|radar|UNREACHABLE|TIMED|dir:' | head -40",
    f"cd {SKILL} && scripts/list_systems.sh gotcha 4 | head -20; "
    "scripts/run_triage.sh axon-gotcha-4 --ping 2>&1 | head -300",
    f"cd {SKILL} && scripts/list_systems.sh gotcha 2; scripts/list_systems.sh gotcha 3",
    f"{SKILL}/scripts/remote_logs.sh axon-gotcha-4 gotcha30 5000 "
    "--grep 'ASU processing restarted|freqs returned 500'",
    # and a few more of the same kind
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 gotcha30 all --grep 'error$' | tail -n 50",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 gotcha30 all | grep -c -i 'timeout'",
    f"{SCRIPTS}/remote_logs.sh axon-gotcha-4 gotcha30 20000 2>&1 | grep -v DEBUG | "
    "cut -c1-300 | sort | uniq -c | sort -rn | head -n 20",
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 && {SCRIPTS}/run_triage.sh axon-gotcha-4",
    f"{SCRIPTS}/run_triage.sh axon-gotcha-3 || {SCRIPTS}/list_systems.sh gotcha 3",
    f"{SCRIPTS}/list_systems.sh gotcha 3\n{SCRIPTS}/list_systems.sh gotcha 4",
    f"{SCRIPTS}/list_systems.sh; echo ---; {SCRIPTS}/code.sh resolve axon-gotcha-4",
    f"{SCRIPTS}/code.sh grep axon-gotcha-3 'reconnect|retry' src | head -30",
    f"grep -rn --include='*.md' 'transmitter' {SKILL}/kb",
    f"ls -la {SKILL}/kb/cases && wc -l {SKILL}/kb/cases/radar-transmitter-off.md",
    "ls /tmp/gotcha-triage",
    "tail -n 100 /tmp/gotcha-triage/axon-gotcha-4.txt",
]


@pytest.mark.parametrize("cmd", CHAINED)
def test_chained_and_filtered_reads_are_allowed(cmd):
    try:
        G.check(cmd, WS)
    except G.Denied as e:
        pytest.fail(f"{cmd!r}: {e}")


CHAINED_BUT_NOT_SAFE = [
    (f"{SCRIPTS}/list_systems.sh\nrm -rf /tmp/x", "a newline is a separator: rm"),
    (f"{SCRIPTS}/list_systems.sh x\ntouch /tmp/x", "a newline is a separator: touch"),
    (f"{SCRIPTS}/list_systems.sh #\ntouch /tmp/x", "a comment must not hide the next line"),
    (f"{SCRIPTS}/list_systems.sh 'a\nb'", "a newline inside quotes"),
    (f"{SCRIPTS}/list_systems.sh\r\ntouch /tmp/x", "carriage return"),
    (f"{SCRIPTS}/list_systems.sh | sh", "pipe into a shell"),
    (f"{SCRIPTS}/list_systems.sh | xargs rm", "xargs"),
    (f"{SCRIPTS}/list_systems.sh | sed -n 1p", "sed (its e/w commands)"),
    (f"{SCRIPTS}/list_systems.sh | awk '{{print}}'", "awk"),
    (f"{SCRIPTS}/list_systems.sh | tee /tmp/x", "tee writes"),
    (f"{SCRIPTS}/list_systems.sh >> /tmp/x", "append redirect"),
    (f"{SCRIPTS}/list_systems.sh 2>/tmp/x", "stderr to a file"),
    (f"grep x < /etc/passwd", "input redirect"),
    (f"{SCRIPTS}/list_systems.sh &", "background"),
    (f"{SCRIPTS}/list_systems.sh & touch /tmp/x", "background then a command"),
    (f"( {SCRIPTS}/list_systems.sh )", "subshell"),
    (f"{SCRIPTS}/list_systems.sh |& cat", "|&"),
    (f"{SCRIPTS}/list_systems.sh &&", "dangling &&"),
    ("; ls", "empty first part"),
    ("cat $HOME/.ssh/id_rsa", "a $variable"),
    ('cat "$HOME/.ssh/id_rsa"', "a $variable in double quotes"),
    (f"cat {SKILL}/kb/$'\\x2e\\x2e'/x", "ANSI-C quoting"),
    ("cat ~/.ssh/id_rsa", "~"),
    ("cat /etc/passwd", "outside the read roots"),
    (f"cat {SKILL}/../slack-bridge/.env", "outside, through .."),
    (f"cat {SKILL}/kb/*", "an unquoted glob"),
    (f"cat {SKILL}/kb/{{x,../../../etc/passwd}}", "a brace expansion"),
    (f"grep {{-f,/etc/shadow}} {SKILL}/kb/glossary.md", "a brace expansion into options"),
    (f"cat {WS}/.env", "a secret name inside the workspace"),
    (f"cat {SKILL}/id_rsa", "a key name inside the skill"),
    (f"grep -f /etc/shadow {SKILL}/kb/glossary.md", "grep -f reads a file"),
    (f"grep --file=/etc/shadow {SKILL}/kb/glossary.md", "grep --file"),
    (f"grep -r password /etc", "grep -r outside"),
    (f"sort -o /tmp/x {SKILL}/kb/glossary.md", "sort -o writes"),
    (f"{SCRIPTS}/list_systems.sh | sort --compress-program=sh", "sort runs a program"),
    (f"tail -f {SKILL}/kb/glossary.md", "tail -f never ends"),
    (f"tail --follow {SKILL}/kb/glossary.md", "tail --follow"),
    (f"uniq {SKILL}/kb/glossary.md /tmp/x", "uniq's second name is its output"),
    ("cd / && cat etc/passwd", "cd outside the read roots"),
    (f"cd {SKILL}/kb; cat ../../slack-bridge/.env", "cd then out again"),
    (f"{SCRIPTS}/list_systems.sh || cd /tmp && ls", "cd that may not run"),
    (f"{SCRIPTS}/list_systems.sh | cd {SKILL}", "cd in a pipe"),
    ("cd", "cd home"),
    (f"cd {SKILL}/kb/nonexistent && ls", "cd into nothing"),
    (f"cd {SKILL} && ./scripts/lib_ssh.sh", "a skill file that is not an entry point"),
    ("run_triage.sh axon-gotcha-3", "a bare name is looked up on PATH"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 && tailscale ssh axon-gotcha-3 uptime", "ssh later"),
    (f"{SCRIPTS}/run_triage.sh axon-gotcha-3 | grep x | bash", "a shell at the end"),
    (f"echo x | {SCRIPTS}/run_triage.sh prod-db", "a wrong site later in the pipe"),
    (f"LC_ALL=C grep x {SKILL}/kb/glossary.md", "environment for a text tool"),
]


@pytest.mark.parametrize("cmd,why", CHAINED_BUT_NOT_SAFE)
def test_every_part_of_a_chain_is_checked(cmd, why):
    assert not allowed(cmd), f"{why}: {cmd!r}"


def test_an_unquoted_regex_gets_told_to_quote_it():
    """The bot sent this; the `|` in its regex was a pipe to a command named `freqs`."""
    cmd = (f"{SKILL}/scripts/remote_logs.sh axon-gotcha-4 gotcha30 5000 "
           "--grep ASU processing restarted|freqs returned 500")
    with pytest.raises(G.Denied, match="single quotes"):
        G.check(cmd, WS)


def test_grep_r_with_no_path_searches_the_cwd_which_must_be_readable():
    assert allowed("grep -r 'transmitter'", WS)
    assert not allowed("grep -r password", "/etc")
    assert not allowed("ls", "/")


def test_a_relative_script_path_follows_the_cd():
    assert allowed(f"cd {SCRIPTS} && ./list_systems.sh")
    assert not allowed("./list_systems.sh", WS)
    assert allowed("./list_systems.sh", str(SCRIPTS))


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
    r = run_settings_hook(ws, bash("touch /tmp/gotcha-guard-probe"), path)
    assert r.returncode == 2, (name, r.returncode, r.stderr)


def test_a_guard_that_exits_0_still_lets_a_command_through(tmp_path):
    """The wrapper turns failures into blocks; it must not turn every run into one."""
    ws = fake_project(tmp_path, "import sys; sys.exit(0)\n")
    assert run_settings_hook(ws, bash("x")).returncode == 0


def test_a_deliberate_block_does_not_claim_the_guard_failed():
    r = run_settings_hook(Path(WS), bash("cd /tmp && ls"))
    assert r.returncode == 2
    assert "Blocked by the Slack bot's read-only guard" in r.stderr
    assert "did not run cleanly" not in r.stderr


def test_a_guard_that_crashes_blocks_and_says_so(tmp_path):
    ws = fake_project(tmp_path, "raise SystemExit(1)\n")
    r = run_settings_hook(ws, bash("x"))
    assert r.returncode == 2 and "did not run cleanly" in r.stderr


def test_the_real_hook_command_allows_and_blocks():
    assert run_settings_hook(Path(WS), bash(f"{SCRIPTS}/list_systems.sh")).returncode == 0
    assert run_settings_hook(Path(WS), bash("touch /tmp/gotcha-guard-probe")).returncode == 2
    assert run_settings_hook(Path(WS), bash("tailscale ssh axon-gotcha-3 uptime")).returncode == 2


def outside_quotes(sh: str) -> str:
    """The shell text with the inside of every quoted string and every comment removed, so a `>`
    in a message (`echo "re-run with configs/<x> run_triage.sh"`) isn't read as a redirect.

    Quotes are paired the way the shell pairs them: a ' inside "..." is text, a " inside '...' is
    text, and a backslash escapes. The code inside a `$( ... )` is kept even where it sits in
    double quotes, because that part is real shell. A regex over "..." and '...' can't do this:
    one apostrophe in an awk one-liner shifts every pair after it."""
    out: list[str] = []
    stack: list = ["code"]      # "code", "dq" (inside "..."), or the open-paren depth of a $( ... )
    i, n = 0, len(sh)
    while i < n:
        c, top = sh[i], stack[-1]
        if top == "dq":
            if c == "\\":
                i += 2
            elif c == '"':
                stack.pop()
                out.append('""')
                i += 1
            elif sh.startswith("$(", i):
                stack.append(1)
                out.append("$(")
                i += 2
            else:
                i += 1
            continue
        if c == "\\":
            out.append("_")                 # an escaped character is text
            i += 2
        elif c == "'":
            j = sh.find("'", i + 1)
            out.append("''")
            i = n if j < 0 else j + 1
        elif c == '"':
            stack.append("dq")
            i += 1
        elif sh.startswith("$(", i):
            stack.append(1)
            out.append("$(")
            i += 2
        elif c == "#" and (i == 0 or sh[i - 1].isspace()):
            j = sh.find("\n", i)            # a comment runs to the end of the line
            i = n if j < 0 else j
        else:
            if isinstance(top, int):
                if c == "(":
                    stack[-1] = top + 1
                elif c == ")":
                    stack[-1] = top - 1
                    if stack[-1] == 0:
                        stack.pop()
            out.append(c)
            i += 1
    return "".join(out)


def test_outside_quotes_drops_text_but_keeps_real_redirects():
    f = outside_quotes
    assert ">" not in f('echo "(re-run with a config: GOTCHA_CONFIG=configs/<x> run_triage.sh <host>)"')
    assert ">" not in f('echo "it' + "'" + 's a -> b"')          # an apostrophe inside double quotes
    assert ">" not in f("echo 'a \"quoted\" > b'")               # a double quote inside single quotes
    assert ">" not in f("echo hi   # a > b")                     # a trailing comment
    assert ">" not in f('echo "${#arr[@]} > 1"')                 # `#` that isn't a comment
    # one apostrophe in an earlier awk program must not shift the pairing of what follows
    assert ">" not in f("awk '{print $1}' f; echo \"a > b\"")
    # and a write is still a write, plain or hidden inside $( ) in a quoted string
    assert ">" in f("echo hi > /tmp/x")
    assert ">" in f("echo hi >/tmp/x")
    assert ">" in f('echo "done" 2>/tmp/err')
    assert ">" in f('echo "$(echo a > /tmp/x)"')
    assert ">" in f('x="$(printf %s "$(date)" > /tmp/x)"')


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
    bare = outside_quotes(code)
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
