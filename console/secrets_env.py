"""The hidden entries of slack-bridge/.env: the Slack tokens, the Anthropic key, and anything
else there that ops.SHOWN doesn't list. Their names go to everyone let in; a value leaves
this module only through value(), which server.py calls for an admin with a fresh code.

The file stays the bot's: same place, same NAME=value lines, comments and order kept, so
`docker compose` (env_file) and `make dev` (`. ./.env`) read it as before. Who changed what,
and when, is kept beside it in console/state/secrets_meta.json, without values.
"""
import json
import re
import threading
import time

from console import ops
from console.auth import write_private
from console.settings import S

NAME_RX = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
LINE_RX = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$")
# Written bare when it is only these; otherwise in single quotes, which both docker compose
# and the shell take literally (no $VAR expansion, no # comment).
BARE_RX = re.compile(r"[A-Za-z0-9._:/+=@,%-]*")
MAX_LEN = 4096

_LOCK = threading.Lock()


class SecretError(ValueError):
    """A name or value this console won't write. The message is safe to show."""


def _path():
    return S.bridge_dir / ".env"


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _meta() -> dict:
    try:
        return json.loads(S.secrets_meta_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def hidden() -> dict[str, str]:
    """name -> value for the entries the console doesn't show. Stays in this process."""
    return {k: _unquote(v) for k, v in ops._env_lines() if k not in ops.SHOWN}


def listing() -> list[dict]:
    meta = _meta()
    return [{"name": k, "set": bool(v), "updated_at": meta.get(k, {}).get("at"),
             "updated_by": meta.get(k, {}).get("by")}
            for k, v in sorted(hidden().items())]


def value(name: str) -> str:
    vals = hidden()
    if name not in vals:
        raise KeyError(name)
    return vals[name]


def _line(name: str, val: str) -> str:
    if BARE_RX.fullmatch(val):
        return f"{name}={val}"
    if "'" in val:
        raise SecretError("a value with a single quote and other special characters can't be "
                          "written safely to .env; edit the file by hand")
    return f"{name}='{val}'"


def put(name: str, val: str, who: str) -> dict:
    """Set one hidden entry, leaving every other line of .env as it was."""
    if not NAME_RX.fullmatch(name or ""):
        raise SecretError("a name is capital letters, digits and _, starting with a letter")
    if name in ops.SHOWN:
        raise SecretError(f"{name} is a setting shown on the Bot page, not a secret")
    if not val:
        raise SecretError("the value is empty")
    if len(val) > MAX_LEN:
        raise SecretError(f"the value is longer than {MAX_LEN} characters")
    if any(c in val for c in "\n\r\0"):
        raise SecretError("the value has a line break in it")
    new = _line(name, val)
    with _LOCK:
        p = _path()
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        done = False
        for i, line in enumerate(lines):
            m = LINE_RX.match(line)
            if m and m.group(1) == name:
                if done:
                    lines[i] = ""  # a duplicate later in the file would win; drop it
                else:
                    lines[i], done = new, True
        if not done:
            lines.append(new)
        write_private(p, "\n".join(lines) + "\n")
        meta = _meta()
        meta[name] = {"at": round(time.time(), 3), "by": who}
        write_private(S.secrets_meta_file, json.dumps(meta, indent=1, sort_keys=True))
    return next(r for r in listing() if r["name"] == name)
