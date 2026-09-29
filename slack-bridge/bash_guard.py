#!/usr/bin/env python3
"""PreToolUse hook: the only Bash commands the Slack bot may run.

Interactive Claude Code asks a human before an unfamiliar command. The bot has no human
to ask, so this file decides instead. It allows:

  - the skill's own scripts: run_triage.sh, list_systems.sh, code.sh (not `clone`)
  - `tailscale status|ping|ip|version`
  - only with BRIDGE_FOLLOWUPS=1 (off by default — "strict mode"):
    `tailscale ssh <host> '<cmd>'` / `ssh <host> '<cmd>'` where <cmd> is on the read-only
    list the skill itself documents (SKILL.md §5): docker ps/inspect/logs, grep, ls, ss,
    ip route get, ip neigh, curl GETs to the machine's own ports, --print-config, ...

FAIL CLOSED. A command this file cannot positively classify as read-only is denied, and
so is a crash in this file: Claude Code treats a hook that exits with anything other
than 2 as a non-blocking error and runs the command anyway, so every path out of here
that is not an explicit allow exits 2.

This is a SECOND line. Claude Code permission rules match the local command, and they
cannot see what a `tailscale ssh host '...'` runs on the far side; this file parses that
string, but the real guarantee is a read-only account on the gotcha machine itself.
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

# Environment assignments a script invocation may be prefixed with (see run_triage.sh).
ENV_OK = {"GOTCHA_SSH_USER", "GOTCHA_CONFIG", "GOTCHA_SSH", "GOTCHA_TRIAGE_TIMEOUT"}
# `--ping` puts traffic on the customer's sensor subnets; the skill wants a person's OK.
ALLOW_PING = os.environ.get("BRIDGE_ALLOW_PING") == "1"
# STRICT BY DEFAULT: the bot runs the skill's scripts and nothing it composed itself.
# With BRIDGE_FOLLOWUPS=1 it may also run its own `tailscale ssh <host> '<cmd>'`
# follow-ups (SKILL.md §5), each one vetted by check_remote() below.
FOLLOWUPS = os.environ.get("BRIDGE_FOLLOWUPS") == "1"

# Stderr folding is harmless and the model reaches for it constantly.
_HARMLESS_REDIRECTS = re.compile(r"\s+2>(&1|/dev/null)(?=\s|$)")
_OPERATORS = set("();<>|&")

# Files whose contents must never be printed: .env holds tokens, site configs hold
# camera passwords, and the rest are key material.
_SECRET = re.compile(r"(^|/)\.env$|(^|/)\.env\.(?!example$)|secret|passw|credential"
                     r"|id_rsa|id_ed25519|\.pem$|\.key$|(^|/)shadow$|(^|/)\.ssh(/|$)", re.I)
# The one way the skill allows into .env: `grep '^KEY=' .env`, for a single named key.
_KEY_PATTERN = re.compile(r"^\^[A-Z_][A-Z0-9_]*=?$")
# Site and gateway configs carry camera credentials. They may be grepped for a key, never
# printed whole, and never grepped for the credential itself.
_SITE_CONFIG = re.compile(r"(^|/)(configs|gateway-config)/.*\.ya?ml$", re.I)
_SECRET_WORD = re.compile(r"pass|secret|token|cred|key|auth", re.I)


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
    # Expanded even inside double quotes — and a remote string is re-read by the remote
    # shell, so single quotes on this side protect nothing over there.
    if "`" in s or "$(" in s or "${" in s:
        raise Denied(f"command substitution is not allowed {where}")


# --------------------------------------------------------------------------
# Remote side: what a gotcha machine may be asked to run
# --------------------------------------------------------------------------

_SIMPLE = {"grep", "egrep", "fgrep", "zgrep", "head", "tail", "wc", "uniq", "cut", "tr",
           "ls", "ss", "uptime", "df", "free", "date", "hostname", "id", "whoami", "cat",
           "which", "stat", "ps", "nproc", "lsblk", "sort", "file"}
_IP_WRITE = {"add", "del", "delete", "flush", "change", "replace", "set", "append",
             "prepend", "exec"}
_DOCKER_READ = {"ps", "inspect", "logs", "images", "info", "version", "top"}
_EXEC_READ = {"ls", "which", "cat", "command"}
_LOCAL_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)")
_CURL_WRITE = re.compile(r"^(-X|--request|-d|--data.*|-F|--form.*|-T|--upload-file|-O|"
                         r"--remote-name.*|-K|--config|--output-dir)$")


def _check_file_args(cmd: str, args: list[str]) -> None:
    words = [a for a in args if not a.startswith("-")]
    pattern = words[0] if words else ""
    grep = cmd in ("grep", "egrep", "fgrep", "zgrep")
    if configs := [a for a in words if _SITE_CONFIG.search(a)]:
        if not grep:
            raise Denied(f"{configs[0]!r} holds camera credentials; grep it for a key, "
                         "or read the redacted --print-config in the triage output")
        if _SECRET_WORD.search(pattern):
            raise Denied("grepping a site config for credentials is not allowed")
    secret = [a for a in words if _SECRET.search(a)]
    if not secret:
        return
    if grep and _KEY_PATTERN.match(pattern) and secret == [words[-1]]:
        return                                   # grep '^IMAGE_TAG=' .env
    raise Denied(f"{secret[0]!r} may hold secrets; read one key with grep '^KEY=' instead")


def _check_curl(args: list[str]) -> None:
    urls = []
    it = iter(range(len(args)))
    for i in it:
        a = args[i]
        if a in ("-o", "--output"):
            if i + 1 >= len(args) or args[i + 1] != "/dev/null":
                raise Denied("curl may only discard its body (-o /dev/null)")
            next(it, None)
        elif _CURL_WRITE.match(a):
            raise Denied(f"curl {a} is not a read-only GET")
        elif a.startswith("http"):
            urls.append(a)
    if not urls or not all(_LOCAL_URL.match(u) for u in urls):
        raise Denied("curl may only GET the machine's own ports (localhost / 127.0.0.1)")


def _check_segment(tok: list[str]) -> None:
    if tok[:2] == ["sudo", "-n"] and len(tok) > 2 and tok[2] == "docker":
        tok = tok[2:]                            # the triage's own docker fallback
    if not tok:
        raise Denied("empty command")
    cmd, args = os.path.basename(tok[0]), tok[1:]

    if cmd in _SIMPLE:
        if cmd == "sort" and any(a.startswith("-o") or a == "--output" for a in args):
            raise Denied("sort -o writes a file")
        _check_file_args(cmd, args)
        return
    if cmd == "find":
        if any(a in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fls")
               for a in args):
            raise Denied("find may only list")
        return
    if cmd == "ip":
        if any(a in _IP_WRITE for a in args):
            raise Denied("ip may only show routes, neighbours and addresses")
        return
    if cmd == "docker":
        sub = args[0] if args else ""
        if sub in _DOCKER_READ:
            if sub == "logs" and ("-f" in args or "--follow" in args):
                raise Denied("docker logs -f never returns")
            return
        if sub == "exec":
            rest = args[1:]
            if len(rest) < 2 or rest[0].startswith("-"):
                raise Denied("docker exec takes no flags: docker exec <container> ls|which|cat ...")
            inner = os.path.basename(rest[1])
            if inner not in _EXEC_READ:
                raise Denied(f"inside a container only {sorted(_EXEC_READ)} are allowed")
            _check_file_args(inner, rest[2:])
            return
        raise Denied(f"docker {sub} is not read-only")
    if cmd == "curl":
        _check_curl(args)
        return
    if cmd == "system_launcher":
        if "--print-config" not in args:
            raise Denied("system_launcher only with --print-config, or it starts a session")
        return
    if cmd == "systemctl":
        if not args or args[0] not in ("status", "is-active", "is-enabled", "list-units"):
            raise Denied("systemctl may only report status")
        return
    if cmd == "journalctl":
        if "-f" in args or "--follow" in args:
            raise Denied("journalctl -f never returns")
        return
    if cmd == "tailscale":
        if not args or args[0] not in ("status", "ip", "version", "netcheck"):
            raise Denied("tailscale may only report status")
        return
    raise Denied(f"{cmd!r} is not on the read-only list for a gotcha machine")


def check_remote(remote: str) -> None:
    if not remote.strip():
        raise Denied("an interactive remote shell is not allowed; pass one command")
    _no_substitution(remote, "on the remote side")
    remote = _HARMLESS_REDIRECTS.sub("", remote)
    segment: list[str] = []
    for tok in _tokens(remote) + ["|"]:
        if tok == "|":
            _check_segment(segment)
            segment = []
        elif _is_op(tok):
            raise Denied(f"{tok!r} is not allowed on the remote side; only pipes into "
                         "read-only commands")
        else:
            segment.append(tok)


# --------------------------------------------------------------------------
# Local side: what the bot host may run
# --------------------------------------------------------------------------

def _script(first: str, cwd: str) -> str | None:
    """The skill script `first` names, or None. Resolved, so a lookalike elsewhere fails."""
    p = Path(first).expanduser()
    p = (Path(cwd) / p if not p.is_absolute() else p).resolve()
    return p.name if p.parent == SCRIPTS_DIR else None


def _check_ssh_opts(opts: list[str]) -> None:
    it = iter(opts)
    for o in it:
        if o == "-o":
            kv = next(it, "")
            if kv.split("=", 1)[0] not in ("BatchMode", "ConnectTimeout"):
                raise Denied(f"ssh option {kv!r} is not allowed")
        elif o in ("-p", "-l"):
            next(it, None)
        else:
            raise Denied(f"ssh flag {o!r} is not allowed (no tunnels, no config files)")


def check(command: str, cwd: str) -> None:
    """Raise Denied unless `command` is allowed. Returns None to allow."""
    _no_substitution(command, "in the bot's commands")
    command = _HARMLESS_REDIRECTS.sub("", command.strip())
    tok = _tokens(command)
    if any(_is_op(t) for t in tok):
        raise Denied("one command at a time: no pipes, redirects, && or ; on this side. "
                     "Pipe on the remote side, inside the quoted command.")
    while tok and re.match(r"^[A-Z_][A-Z0-9_]*=", tok[0]):
        name = tok[0].split("=", 1)[0]
        if name not in ENV_OK:
            raise Denied(f"setting {name} is not allowed")
        tok = tok[1:]
    if not tok:
        raise Denied("empty command")

    script = _script(tok[0], cwd)
    if script == "run_triage.sh":
        if "--ping" in tok[2:] and not ALLOW_PING:
            raise Denied("--ping puts traffic on the customer's sensor subnets; ask a "
                         "person to run it, or answer from the passive route checks")
        if len(tok) < 2 or len(tok) > 3:
            raise Denied("usage: run_triage.sh <tailnet-host> [--ping]")
        return
    if script == "list_systems.sh":
        return
    if script == "code.sh":
        if len(tok) < 2 or tok[1] not in ("resolve", "grep", "show", "log", "sync"):
            raise Denied("code.sh resolve|grep|show|log|sync only (clone is a setup step)")
        return
    if script:
        raise Denied(f"{script} is not a script the bot may run")

    if tok[0] == "tailscale" and len(tok) > 1 and tok[1] != "ssh":
        if tok[1] not in ("status", "ping", "ip", "version"):
            raise Denied(f"tailscale {tok[1]} is not allowed")
        return
    if tok[0] in ("tailscale", "ssh") and not FOLLOWUPS:
        raise Denied("strict mode: the bot runs only the skill's scripts, not commands of "
                     "its own on a site. Answer from the triage output, and name the one "
                     "check that would settle it as a step for the PM.")
    if tok[0] == "tailscale":                    # tailscale ssh [user@]host cmd...
        if len(tok) < 4:
            raise Denied("tailscale ssh needs a host and one command")
        check_remote(" ".join(tok[3:]))
        return
    if tok[0] == "ssh":
        i = 1
        while i < len(tok) and tok[i].startswith("-"):
            i += 2 if tok[i] in ("-o", "-p", "-l") else 1
        _check_ssh_opts(tok[1:i])
        if len(tok) < i + 2:
            raise Denied("ssh needs a host and one command")
        check_remote(" ".join(tok[i + 1:]))
        return
    raise Denied(f"{tok[0]!r} is not something the bot may run. Use the skill's scripts, "
                 "or tailscale ssh <host> '<read-only command>'")


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
