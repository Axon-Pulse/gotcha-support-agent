#!/usr/bin/env python3
"""PreToolUse hook: the only Bash commands the Slack bot may run.

Interactive Claude Code asks a human before an unfamiliar command. The bot has no human
to ask, so this file decides instead. It allows:

  - the skill's own scripts: run_triage.sh, list_systems.sh, remote_logs.sh (one container's
    `docker logs --tail`, redacted), code.sh (not `clone`), and only against the sites the bot
    is meant to reach (BRIDGE_ALLOWED_HOSTS, `axon-gotcha-<number>` by default)
  - `tailscale ping <site>`, `tailscale ip [<site>]`, `tailscale version`

Nothing else. The bot composes no command of its own on a site (no `ssh`, no `tailscale ssh`):
a check that would settle a question is given to the PM as a step.

FAIL CLOSED. A command this file cannot positively classify is denied, and so is a crash in
this file. Claude Code treats a hook that exits with anything other than 2 as a non-blocking
error and runs the command anyway, so every path out of here that is not an explicit allow
exits 2. A crash before this code runs at all (no python3, a syntax error, an import error)
is caught one level up: settings.json runs this file as `python3 bash_guard.py || exit 2`, and
the bridge refuses to start unless the hook blocks a test command (bridge.verify_guard).
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get("GOTCHA_SKILL_DIR")
                 or Path(__file__).resolve().parent.parent / "support-agent-skill").resolve()
SCRIPTS_DIR = SKILL_DIR / "scripts"

# The machines the bot may touch. bridge.py exports the same default (and
# list_systems.sh filters its listing to it), so a name outside it is neither listed nor reachable.
DEFAULT_ALLOWED_HOSTS = r"axon-gotcha-[0-9]+"
ALLOWED_HOSTS = re.compile(os.environ.get("BRIDGE_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS)

# Environment assignments a script invocation may be prefixed with (see run_triage.sh), and the
# shape each value must have: a value goes into a script, where a leading `-` or a path outside
# the site configs would be an argument or a file nobody meant. GOTCHA_SSH_USER is not here: the
# login is the bot host's own configuration, not something a message can choose.
ENV_OK = {
    "GOTCHA_CONFIG": re.compile(r"configs/[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}"),
    "GOTCHA_SSH": re.compile(r"tailscale|ssh"),
    "GOTCHA_TRIAGE_TIMEOUT": re.compile(r"[0-9]{1,3}"),
}
# `--ping` sends two packets to each sensor address on the customer's subnets. On by default,
# because "the APU answers but the radar doesn't" can only be confirmed with a ping;
# BRIDGE_ALLOW_PING=0 switches it off.
ALLOW_PING = os.environ.get("BRIDGE_ALLOW_PING", "1") == "1"

# Stderr folding is harmless and the model reaches for it constantly.
_HARMLESS_REDIRECTS = re.compile(r"\s+2>(&1|/dev/null)(?=\s|$)")
_OPERATORS = set("();<>|&")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,100}")
_REPO_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./+@-]{0,300}")
_LINE_RANGE = re.compile(r"[0-9]+(,[0-9]+)?")


class Denied(Exception):
    pass


def _tokens(cmd: str) -> list[str]:
    """Words, with every unquoted shell operator as a token of its own."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError as e:                      # unbalanced quotes
        raise Denied(f"could not parse the command ({e})")


def _is_op(tok: str) -> bool:
    return bool(tok) and set(tok) <= _OPERATORS


def _no_substitution(s: str, where: str) -> None:
    # Expanded even inside double quotes.
    if "`" in s or "$(" in s or "${" in s:
        raise Denied(f"command substitution is not allowed {where}")


def _host(name: str) -> None:
    """A site the bot may reach. Anything else on the tailnet (a laptop, a server) is not its to
    touch, and a name that starts with `-` would be read by ssh as an option."""
    if not _NAME.fullmatch(name) or not ALLOWED_HOSTS.fullmatch(name):
        raise Denied(f"{name!r} is not a site this bot may reach "
                     f"(allowed: {ALLOWED_HOSTS.pattern}); list_systems.sh shows the ones it can")


def _script(first: str, cwd: str) -> str | None:
    """The skill script `first` names, or None. Resolved, so a lookalike elsewhere fails."""
    p = Path(first).expanduser()
    p = (Path(cwd) / p if not p.is_absolute() else p).resolve()
    return p.name if p.parent == SCRIPTS_DIR else None


def _check_code_args(sub: str, args: list[str]) -> None:
    """code.sh puts some of its arguments into a sed script, so they are checked here as well
    as in the script: the version and file are plain names, the range is line numbers. (A range
    such as `1,2e <command>` is a sed `e` command, which runs it.)"""
    if sub == "sync":
        if args:
            raise Denied("code.sh sync takes no arguments")
        return
    if not args or not _VERSION.fullmatch(args[0]):
        raise Denied("code.sh: the version is a system name, release tag or commit")
    if sub == "show":
        if not 2 <= len(args) <= 3:
            raise Denied("usage: code.sh show <ver> <file> [N[,M]]")
        if not _REPO_FILE.fullmatch(args[1]):
            raise Denied("code.sh show: the file is a path in the repository")
        if len(args) == 3 and not _LINE_RANGE.fullmatch(args[2]):
            raise Denied("code.sh show: the range is line numbers, N or N,M")


def _check_tailscale(args: list[str]) -> None:
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "ping":
        if rest[:1] == ["-c"]:
            if len(rest) < 2 or not rest[1].isdigit():
                raise Denied("tailscale ping -c takes a count")
            rest = rest[2:]
        if len(rest) != 1:
            raise Denied("usage: tailscale ping [-c N] <site>")
        _host(rest[0])
    elif sub == "ip":
        if len(rest) > 1:
            raise Denied("usage: tailscale ip [<site>]")
        if rest:
            _host(rest[0])
    elif sub == "version":
        if rest:
            raise Denied("tailscale version takes no arguments")
    elif sub == "status":
        raise Denied("tailscale status lists every machine on the tailnet; "
                     "list_systems.sh lists the sites this bot may use")
    else:
        raise Denied(f"tailscale {sub} is not allowed")


def check(command: str, cwd: str) -> None:
    """Raise Denied unless `command` is allowed. Returns None to allow."""
    _no_substitution(command, "in the bot's commands")
    command = _HARMLESS_REDIRECTS.sub("", command.strip())
    tok = _tokens(command)
    if any(_is_op(t) for t in tok):
        raise Denied("one command at a time: no pipes, redirects, && or ; in the bot's commands")
    while tok and re.match(r"^[A-Z_][A-Z0-9_]*=", tok[0]):
        name, _, value = tok[0].partition("=")
        if name not in ENV_OK:
            raise Denied(f"setting {name} is not allowed")
        if not ENV_OK[name].fullmatch(value) or ".." in value:
            raise Denied(f"{name}={value!r} is not a value this bot may set")
        tok = tok[1:]
    if not tok:
        raise Denied("empty command")

    script = _script(tok[0], cwd)
    if script == "run_triage.sh":
        args = tok[1:]
        if not 1 <= len(args) <= 2 or (len(args) == 2 and args[1] != "--ping"):
            raise Denied("usage: run_triage.sh <site> [--ping]")
        _host(args[0])
        if len(args) == 2 and not ALLOW_PING:
            raise Denied("--ping is switched off (BRIDGE_ALLOW_PING=0); answer from the passive "
                         "route checks and name the ping as the step that would confirm it")
        return
    if script == "list_systems.sh":
        return                          # its words are the PM's; the listing is limited to the allowed sites
    if script == "remote_logs.sh":
        # <host> <container> [lines] [--grep REGEX]; the script validates again and is the
        # authority. Here: shape only, so nothing odd reaches it.
        args = tok[1:]
        if not 2 <= len(args) <= 5:
            raise Denied("usage: remote_logs.sh <site> <container> [lines] [--grep REGEX]")
        if not _NAME.fullmatch(args[1]):
            raise Denied("remote_logs.sh: the container is a plain name")
        _host(args[0])
        rest = args[2:]
        if rest and rest[0].isdigit():
            rest = rest[1:]
        if rest and not (len(rest) == 2 and rest[0] == "--grep"):
            raise Denied("remote_logs.sh: after the container only [lines] and [--grep REGEX]")
        return
    if script == "code.sh":
        if len(tok) < 2 or tok[1] not in ("resolve", "grep", "show", "log", "sync"):
            raise Denied("code.sh resolve|grep|show|log|sync only (clone is a setup step)")
        _check_code_args(tok[1], tok[2:])
        return
    if script:
        raise Denied(f"{script} is not a script the bot may run")

    if tok[0] == "tailscale" and len(tok) > 1 and tok[1] != "ssh":
        _check_tailscale(tok[1:])
        return
    raise Denied(f"{tok[0]!r} is not something the bot may run. It runs only the skill's "
                 "scripts; when one more check would settle a question, give it to the PM as a step")


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        check(str((event.get("tool_input") or {}).get("command") or ""),
              str(event.get("cwd") or os.getcwd()))
        return 0
    except Denied as e:
        print(f"Blocked by the Slack bot's read-only guard: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - fail closed; see the module docstring
        print(f"Blocked: the read-only guard could not check this command ({e})",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
