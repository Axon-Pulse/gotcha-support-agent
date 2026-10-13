#!/usr/bin/env python3
"""PreToolUse hook: the only Bash commands the Slack bot may run.

Interactive Claude Code asks a human before an unfamiliar command. The bot has no human
to ask, so this file decides instead. A command may be several segments joined by `&&`, `||`,
`;`, a newline or `|`; each segment is checked on its own, and every one must be one of:

  - the skill's own scripts: run_triage.sh, list_systems.sh, remote_logs.sh (one container's
    `docker logs`, redacted), code.sh (not `clone`), and only against the sites the bot
    is meant to reach (BRIDGE_ALLOWED_HOSTS, `axon-gotcha-<number>` by default)
  - `tailscale ping <site>`, `tailscale ip [<site>]`, `tailscale version`
  - a read-only text tool to cut output down or read a KB/triage file: grep, head, tail, wc,
    sort, uniq, cut, cat, ls, echo, each with a list of allowed flags, and with every file it
    names inside READ_ROOTS (the skill, the workspace, the saved triages) or the bot's own
    saved outputs (TOOL_RESULTS_BASE/<session>/tool-results): what the Read tool may open;
    and not a secret-looking file
  - `cd <dir>` inside READ_ROOTS, at the start of a list (so it always runs), never in a pipe

Nothing else: no redirect to a file, no `&`, no subshell, no command substitution, no
`$variable`, no unquoted glob, brace or `~` (bash would expand those into words this file
never saw). The bot composes no command of its own on a site (no `ssh`, no `tailscale ssh`):
a check that would settle a question is given to the PM as a step.

FAIL CLOSED. A command this file cannot positively classify is denied, and so is a crash in
this file. Claude Code treats a hook that exits with anything other than 2 as a non-blocking
error and runs the command anyway, so every path out of here that is not an explicit allow
exits 2. A crash before this code runs at all (no python3, a syntax error, an import error)
is caught one level up: settings.json turns any exit other than 0 or 2 into a block, and
the bridge refuses to start unless the hook blocks a test command (bridge.verify_guard).
"""
import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get("GOTCHA_SKILL_DIR")
                 or Path(__file__).resolve().parent.parent / "support-agent-skill").resolve()
SCRIPTS_DIR = SKILL_DIR / "scripts"
WORKSPACE = (Path(__file__).resolve().parent / "workspace").resolve()
TRIAGE_DIR = Path("/tmp/gotcha-triage").resolve()

# Where the text tools may read: the same places settings.json and bridge.permission_overlay let
# the Read tool open. Secret-looking names are refused inside them too (the same list as there).
READ_ROOTS = (SKILL_DIR, WORKSPACE, TRIAGE_DIR)
# ...and the bot's own outputs that were too big to return inline: Claude Code saves each under
# <config dir>/projects/<workspace path, non-alphanumerics as `-`>/<session>/tool-results/ and
# hands the model a preview and the path. Only those folders, not the transcripts beside them.
# Keep in step with bridge.tool_results_base (a test checks).
TOOL_RESULTS_BASE = (Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()
                     / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WORKSPACE)))
_SECRET_FILES = (".env", ".env.*", "secrets.local.env", "*.pem", "*.key", "id_*")
_SECRET_DIRS = {".ssh", ".aws", ".config", ".gnupg"}

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
_HARMLESS_REDIRECTS = re.compile(r"\s+2>(&1|/dev/null)(?=[\s|;&]|$)")
_OPERATORS = set("();<>|&\n")
# Bash expands these when they are not quoted, into words this file would not have checked.
_UNQUOTED_EXPANSIONS = set("*?[{~")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,100}")
_REPO_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./+@-]{0,300}")
_LINE_RANGE = re.compile(r"[0-9]+(,[0-9]+)?")
_COUNT = re.compile(r"[+-]?[0-9]+")
_LIST = re.compile(r"[0-9,-]+")

_SHELL_KEYWORDS = {"for", "while", "until", "select", "do", "done", "if", "then", "elif", "else",
                   "fi", "case", "esac", "function"}
_NO_LOOPS = ("no for/while loops or if blocks in the bot's commands: write each command out in "
             "full, joined with ; (for several patterns in one log, one search with a combined "
             "--grep 'a|b|c', split locally with | grep, is faster)")
_QUOTE_HINT = ("if the `|` was part of a --grep or code.sh grep regex, put the whole regex in "
               "single quotes")


class Denied(Exception):
    pass


def _tokens(cmd: str) -> list[str]:
    """Words, with every unquoted shell operator (newline included) as a token of its own.
    `#` is an ordinary character here: shlex would read it as a comment and swallow the newline
    after it, hiding the command on the next line."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as e:                      # unbalanced quotes
        raise Denied(f"could not parse the command ({e})")


def _is_op(tok: str) -> bool:
    return bool(tok) and set(tok) <= _OPERATORS


def _separator(tok: str) -> str:
    """The separator an operator token stands for: `&&`, `||`, `|` or `;` (a newline is a `;`)."""
    if set(tok) == {"\n"}:
        return ";"
    core = tok.rstrip("\n")                      # `a &&⏎b`: bash carries on after && || | ;
    if core in ("&&", "||", "|", ";"):
        return core
    if "(" in core or ")" in core:
        raise Denied("no subshells ( … ) in the bot's commands")
    if core == "&":
        raise Denied("no background jobs (&) in the bot's commands")
    if "<" in core or ">" in core:
        raise Denied("no redirects in the bot's commands (2>&1 and 2>/dev/null are fine): "
                     "the bot writes no files; filter with | grep / | tail instead")
    raise Denied(f"{core.strip()!r} is not a separator the bot may use (&&, ||, ;, |)")


def _no_substitution(s: str, where: str) -> None:
    # Expanded even inside double quotes.
    if "`" in s or "$(" in s or "${" in s:
        raise Denied(f"command substitution is not allowed {where}")


def _no_expansions(cmd: str) -> None:
    """Refuse what bash would expand before running: `$name` (outside single quotes), and an
    unquoted glob, brace, `~` or `#` comment. With none of these, the words shlex sees are the
    words bash runs. A `$` before whitespace or at the end (a regex anchor) stays literal."""
    quote = ""
    i = 0
    while i < len(cmd):
        c = cmd[i]
        nxt = cmd[i + 1] if i + 1 < len(cmd) else ""
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            i += 1                               # the next character is literal
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "'" and not quote:
            quote = "'"
        elif c == "$" and not (nxt == "" or nxt in " \t\n|;&)" or (quote == '"' and nxt == '"')):
            raise Denied("no $variables in the bot's commands: write values out in full (a "
                         "regex `$` goes in single quotes)")
        elif not quote and c in _UNQUOTED_EXPANSIONS:
            raise Denied(f"an unquoted {c!r} is expanded by the shell: put patterns, regexes and "
                         "globs in single quotes")
        elif not quote and c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            raise Denied("no # comments in the bot's commands")
        i += 1


def _host(name: str) -> None:
    """A site the bot may reach. Anything else on the tailnet (a laptop, a server) is not its to
    touch, and a name that starts with `-` would be read by ssh as an option."""
    if not _NAME.fullmatch(name) or not ALLOWED_HOSTS.fullmatch(name):
        raise Denied(f"{name!r} is not a site this bot may reach "
                     f"(allowed: {ALLOWED_HOSTS.pattern}); list_systems.sh shows the ones it can")


def _resolve(path: str, cwd: str) -> Path:
    p = Path(path)
    return (Path(cwd) / p if not p.is_absolute() else p).resolve()


def _script(first: str, cwd: str) -> str | None:
    """The skill script `first` names, or None. Resolved, so a lookalike elsewhere fails."""
    if "/" not in first:
        return None                              # a bare name is looked up on PATH, not here
    p = _resolve(first, cwd)
    return p.name if p.parent == SCRIPTS_DIR else None


def _tool_result(p: Path) -> bool:
    """<TOOL_RESULTS_BASE>/<session>/tool-results, or anything in it."""
    try:
        rel = p.relative_to(TOOL_RESULTS_BASE)
    except ValueError:
        return False
    return len(rel.parts) >= 2 and rel.parts[1] == "tool-results" and ".." not in rel.parts


def _readable(path: str, cwd: str) -> Path:
    """A file or directory the text tools may read: inside READ_ROOTS, not a secret."""
    p = _resolve(path, cwd)
    if not any(p == r or r in p.parents for r in READ_ROOTS) and not _tool_result(p):
        raise Denied(f"{path!r} is outside what the bot may read (the skill, its KB, the "
                     "saved triages and its own saved outputs)")
    if any(part in _SECRET_DIRS for part in p.parts) or \
            any(fnmatch.fnmatch(p.name, pat) for pat in _SECRET_FILES):
        raise Denied(f"{path!r} looks like a secret; the bot does not read it")
    return p


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


# --------------------------------------------------------------------------
# Read-only text tools
# --------------------------------------------------------------------------

class _Tool:
    """What one text tool accepts. `flags`: short options without a value (they may be grouped,
    `-iE`); `valued`: short options that take one, with the shape the value must have (None: any);
    `long`/`long_valued` likewise. Anything not listed is refused, which is what keeps out
    `sort -o`, `tail -f`, `grep -f` and the like."""

    def __init__(self, flags="", valued=None, long=(), long_valued=None, numeric=False,
                 max_files=None):
        self.flags = set(flags)
        self.valued = valued or {}
        self.long = set(long)
        self.long_valued = long_valued or {}
        self.numeric = numeric                  # `head -20`, `grep -3`
        self.max_files = max_files


_GREP = _Tool(
    "iEFPGvnclLowxhHrRsaqIZz",
    {"A": re.compile(r"[0-9]+"), "B": re.compile(r"[0-9]+"), "C": re.compile(r"[0-9]+"),
     "m": re.compile(r"[0-9]+"), "e": None},
    {"--ignore-case", "--extended-regexp", "--fixed-strings", "--perl-regexp", "--basic-regexp",
     "--invert-match", "--line-number", "--count", "--files-with-matches",
     "--files-without-match", "--only-matching", "--word-regexp", "--line-regexp",
     "--no-filename", "--with-filename", "--recursive", "--dereference-recursive",
     "--no-messages", "--text", "--quiet", "--silent", "--null"},
    {"--include": None, "--exclude": None, "--exclude-dir": None,
     "--color": re.compile(r"auto|never|always"), "--colour": re.compile(r"auto|never|always"),
     "--max-count": re.compile(r"[0-9]+"), "--context": re.compile(r"[0-9]+"),
     "--after-context": re.compile(r"[0-9]+"), "--before-context": re.compile(r"[0-9]+"),
     "--regexp": None},
    numeric=True)
_HEAD = _Tool("qv", {"n": _COUNT, "c": _COUNT}, {"--quiet", "--silent", "--verbose"},
              {"--lines": _COUNT, "--bytes": _COUNT}, numeric=True)
TOOLS = {
    "grep": _GREP, "egrep": _GREP, "fgrep": _GREP,
    "head": _HEAD,
    "tail": _HEAD,                              # same options; -f/-F/--follow are not among them
    "wc": _Tool("lwcmL", long={"--lines", "--words", "--bytes", "--chars"}),
    "sort": _Tool("nruhVfbdMgis", {"k": None, "t": None}),
    "uniq": _Tool("cduiz", {"f": re.compile(r"[0-9]+"), "s": re.compile(r"[0-9]+"),
                            "w": re.compile(r"[0-9]+")}, max_files=1),   # a 2nd name is written
    "cut": _Tool("sz", {"d": re.compile(r".", re.S), "f": _LIST, "c": _LIST, "b": _LIST},
                 {"--complement", "--only-delimited"},
                 {"--delimiter": re.compile(r".", re.S), "--fields": _LIST}),
    "cat": _Tool("nAsvbeET"),
    "ls": _Tool("lahRt1SrdF"),
}


def _parse(name: str, tool: _Tool, args: list[str]) -> tuple[set[str], list[str]]:
    """Check the options; return (the options seen, the operands)."""
    seen: set[str] = set()
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            operands += args[i:]
            break
        if a == "-" or not a.startswith("-"):
            operands.append(a)
            continue
        if tool.numeric and re.fullmatch(r"-[0-9]+", a):
            continue
        if a.startswith("--"):
            opt, eq, value = a.partition("=")
            if opt in tool.long and not eq:
                seen.add(opt)
                continue
            if opt in tool.long_valued:
                if not eq:
                    if i >= len(args):
                        raise Denied(f"{name} {opt} takes a value")
                    value = args[i]
                    i += 1
                shape = tool.long_valued[opt]
                if shape and not shape.fullmatch(value):
                    raise Denied(f"{name} {opt}: {value!r} is not a value it takes here")
                seen.add(opt)
                continue
            raise Denied(f"{name} {opt} is not an option the bot may use")
        j = 1
        while j < len(a):
            c = a[j]
            if c in tool.flags:
                seen.add(c)
                j += 1
                continue
            if c in tool.valued:
                value = a[j + 1:]
                if not value:
                    if i >= len(args):
                        raise Denied(f"{name} -{c} takes a value")
                    value = args[i]
                    i += 1
                shape = tool.valued[c]
                if shape and not shape.fullmatch(value):
                    raise Denied(f"{name} -{c}: {value!r} is not a value it takes here")
                seen.add(c)
                break
            raise Denied(f"{name} -{c} is not an option the bot may use")
    return seen, operands


def _check_tool(name: str, args: list[str], cwd: str) -> None:
    tool = TOOLS[name]
    seen, operands = _parse(name, tool, args)
    if name in ("grep", "egrep", "fgrep"):
        if not seen & {"e", "--regexp"}:
            if not operands:
                raise Denied(f"usage: {name} [options] PATTERN [file...]")
            operands = operands[1:]             # the pattern
        if not operands and seen & {"r", "R", "--recursive", "--dereference-recursive"}:
            operands = ["."]
    elif name == "ls" and not operands:
        operands = ["."]
    if tool.max_files is not None and len(operands) > tool.max_files:
        raise Denied(f"{name} takes at most {tool.max_files} file here")
    for f in operands:
        if f != "-":
            _readable(f, cwd)


# --------------------------------------------------------------------------
# One segment, then the whole command
# --------------------------------------------------------------------------

def _check_segment(tok: list[str], cwd: str) -> None:
    """One simple command, without separators. Raise Denied unless it is allowed."""
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
        # <host> <container> [lines|all] [--grep REGEX]; the script validates again and is the
        # authority. Here: shape only, so nothing odd reaches it.
        args = tok[1:]
        if len(args) < 2:
            raise Denied("usage: remote_logs.sh <site> <container> [lines|all] [--grep 'REGEX']")
        if not _NAME.fullmatch(args[1]):
            raise Denied("remote_logs.sh: the container is a plain name")
        _host(args[0])
        rest = args[2:]
        if rest and (rest[0].isdigit() or rest[0] == "all"):
            rest = rest[1:]
        if rest[:1] == ["--grep"] and len(rest) > 2:
            raise Denied("remote_logs.sh: --grep takes one quoted regex and no grep options "
                         "(-A, -v, -c, ...); filter or add context with a pipe, | grep ...")
        if rest and not (len(rest) == 2 and rest[0] == "--grep"):
            raise Denied("remote_logs.sh: after the container only [lines|all] and "
                         "[--grep 'REGEX'] (one quoted regex)")
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
    if tok[0] in TOOLS:
        _check_tool(tok[0], tok[1:], cwd)
        return
    if tok[0] == "echo":
        return                          # prints its words; no redirect can follow it here
    raise Denied(f"{tok[0]!r} is not something the bot may run. It runs only the skill's "
                 "scripts and read-only text tools (grep, head, tail, wc, sort, uniq, cut, cat, "
                 "ls); when one more check would settle a question, give it to the PM as a step")


def _grep_like(tok: list[str], cwd: str) -> bool:
    """A segment whose regex, left unquoted, would have its `|` read as a pipe."""
    script = _script(tok[0], cwd) if tok else None
    return (script == "remote_logs.sh" and "--grep" in tok) or \
        (script == "code.sh" and tok[1:2] == ["grep"])


def _known(tok: list[str], cwd: str) -> bool:
    """Whether a segment starts with something this file knows how to check."""
    while tok and re.match(r"^[A-Z_][A-Z0-9_]*=", tok[0]):
        tok = tok[1:]
    return bool(tok) and (tok[0] in TOOLS or tok[0] in ("echo", "cd", "tailscale")
                          or _script(tok[0], cwd) is not None)


def check(command: str, cwd: str) -> None:
    """Raise Denied unless `command` is allowed. Returns None to allow."""
    _no_substitution(command, "in the bot's commands")
    if "\r" in command:
        raise Denied("no carriage returns in the bot's commands")
    command = _HARMLESS_REDIRECTS.sub("", command.strip())

    # Split into segments, each with the separator before and after it.
    segments: list[list[str]] = [[]]
    seps: list[str] = []
    bad_separator: Denied | None = None          # raised below, after the loop check
    for t in _tokens(command):
        if _is_op(t):
            try:
                seps.append(_separator(t))
            except Denied as e:
                bad_separator = bad_separator or e
                seps.append(";")
            segments.append([])
        elif "\n" in t:
            raise Denied("no newlines inside a quoted word in the bot's commands")
        else:
            segments[-1].append(t)
    while len(segments) > 1 and not segments[-1] and seps[-1] == ";":
        segments.pop()                  # a trailing `;` or newline
        seps.pop()
    # Loops and bare assignments are denied anyway (by the `$` they carry, or as a setting this
    # file doesn't know); this says what to write instead, so the retry is the right one.
    if any(tok[:1] and tok[0] in _SHELL_KEYWORDS for tok in segments):
        raise Denied(_NO_LOOPS)
    if any(tok and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) for t in tok) for tok in segments):
        raise Denied("no NAME=value assignments in the bot's commands: write the script "
                     "path or value out in full every time it is used")
    if bad_separator:
        raise bad_separator
    if not any(segments):
        raise Denied("empty command")
    _no_expansions(command)

    for n, tok in enumerate(segments):
        before = seps[n - 1] if n else None
        after = seps[n] if n < len(seps) else None
        try:
            if tok[:1] == ["cd"]:
                # Only at the start of a list, so it always runs, and only into a directory that
                # exists, so it succeeds: what follows then runs where this file thinks it does.
                if before not in (None, ";") or after == "|" or before == "|":
                    raise Denied("cd only at the start of the command (or after ;), followed "
                                 "by &&, never in a pipe")
                if len(tok) != 2 or tok[1].startswith("-"):
                    raise Denied("usage: cd <directory in the skill or workspace>")
                target = _readable(tok[1], cwd)
                if not target.is_dir():
                    raise Denied(f"cd: {tok[1]!r} is not a directory")
                cwd = str(target)
                continue
            _check_segment(tok, cwd)
        except Denied as e:
            # Only when what follows the `|` is no command at all: then the `|` most likely
            # belonged to a regex. `--grep 'x' -A 0 | wc -l` is a different mistake.
            if (after == "|" and _grep_like(tok, cwd) and not _known(segments[n + 1], cwd)) or \
                    (before == "|" and _grep_like(segments[n - 1], cwd) and not _known(tok, cwd)):
                raise Denied(f"{e} ({_QUOTE_HINT})")
            raise


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
