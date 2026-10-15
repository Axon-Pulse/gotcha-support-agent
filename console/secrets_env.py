"""The hidden entries of slack-bridge/.env: the Slack tokens, the Anthropic key, and anything
else there that ops.SHOWN doesn't list. Their names go to everyone let in; a value leaves
this module only through value(), which server.py calls for an admin with a fresh code.

The file stays the bot's: same place, same NAME=value lines, comments and order kept, so
`docker compose` (env_file) and `make dev` (`. ./.env`) read it as before. Who changed what,
and when, is kept beside it in console/state/secrets_meta.json, without values.
"""
import re

from console import ops
from console.files import env_update, file_lock, meta_load, meta_save, meta_touch
from console.safe_text import has_control
from console.settings import S

NAME_RX = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
# A NEW entry must look like a secret. Anything else the bot reads from its environment
# (CLAUDE_BIN, LD_PRELOAD, HTTPS_PROXY, ANTHROPIC_BASE_URL…) could change what it runs or
# where it sends the key, which is more than managing a secret. Existing entries stay editable.
NEW_NAME_RX = re.compile(r"[A-Z][A-Z0-9_]{0,55}_(TOKEN|KEY|SECRET|PASSWORD|PW)")
# Written bare when it is only these; otherwise in single quotes, which both docker compose
# and the shell take literally (no $VAR expansion, no # comment).
BARE_RX = re.compile(r"[A-Za-z0-9._:/+=@,%-]*")
MAX_LEN = 4096


class SecretError(ValueError):
    """A name or value this console won't write. The message is safe to show."""


def _path():
    return S.bridge_dir / ".env"


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def hidden() -> dict[str, str]:
    """name -> value for the entries the console doesn't show. Stays in this process."""
    return {k: _unquote(v) for k, v in ops.bot_env_entries() if k not in ops.SHOWN}


def listing() -> list[dict]:
    meta = meta_load(S.secrets_meta_file)
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


def precheck(name: str, val: str) -> str:
    """Everything that can be said about a name and value without the file: run before the
    admin's code is spent. Returns the line that would be written."""
    if not NAME_RX.fullmatch(name or ""):
        raise SecretError("a name is capital letters, digits and _, starting with a letter")
    if name in ops.SHOWN:
        raise SecretError(f"{name} is a setting shown on the Bot page, not a secret")
    if not val:
        raise SecretError("the value is empty")
    if len(val) > MAX_LEN:
        raise SecretError(f"the value is longer than {MAX_LEN} characters")
    if has_control(val):
        raise SecretError("the value has a line break or another control character in it")
    if name not in hidden() and not NEW_NAME_RX.fullmatch(name):
        raise SecretError("a new secret's name must end in _TOKEN, _KEY, _SECRET, _PASSWORD "
                          "or _PW; other settings of the bot are changed on the server")
    return _line(name, val)


def put(name: str, val: str, who: str) -> dict:
    """Set one hidden entry, leaving every other line of .env as it was."""
    with file_lock("bot-env"):
        line = precheck(name, val)  # again, under the lock: the file may have changed
        env_update(_path(), {name: line})
        meta = meta_load(S.secrets_meta_file)
        meta_touch(meta, name, who)
        meta_save(S.secrets_meta_file, meta)
    return next(r for r in listing() if r["name"] == name)
