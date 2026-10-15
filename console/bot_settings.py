"""The bot's settings in slack-bridge/.env that an admin may change from the Bot config tab.

Each has its own check, so a value the bot would misread, or one that would widen what it may
reach, never gets written. BOT_UID/BOT_GID (fixed when the image is built) and CLAUDE_BIN
(the program the bot runs) stay read-only: they are changed on the server.

A change replaces only that line of .env, as secrets_env does, and server.py then restarts the
bot, which reads .env only when it starts. None removes the line: back to the bot's default.
"""
import re

from console import ops
from console.files import env_line, env_update, file_lock, meta_load, meta_save, meta_touch
from console.settings import S

# Only these (a mistyped model would fail every answer); "[1m]" asks for the 1M-token context.
MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Slack IDs as Slack shows them: upper case, always with digits (so a name like GENERAL isn't
# one). Channels are C… only: the bot's `make check`, which Deploy runs, wants a C first.
CHANNEL_RX = re.compile(r"C(?=[A-Z]*[0-9])[A-Z0-9]{6,20}")
SLACK_USER_RX = re.compile(r"[UW](?=[A-Z]*[0-9])[A-Z0-9]{6,20}")
HOST_RX = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")   # one machine, as the tailnet names it
SITES = ops.DEFAULTS["BRIDGE_ALLOWED_HOSTS"]       # every axon-gotcha-<number>
SSH_USER_RX = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
MAX_ITEMS = 50
MAX_LEN = 4096
_IMAGE = ("fixed when the bot's image is built, and must match the server user that owns its "
          "files; change it on the server and rebuild")
READ_ONLY = {"BOT_UID": _IMAGE, "BOT_GID": _IMAGE,
             "CLAUDE_BIN": "the program the bot runs for every answer; changed on the server only"}
# Where empty means the same as the default (anyone / ssh's own login): the line goes.
EMPTY_IS_DEFAULT = ("SLACK_ALLOWED_USERS", "GOTCHA_SSH_USER")


class SettingError(ValueError):
    """A value this console won't write. The message is safe to show."""


def _choice(options):
    def check(v):
        if v not in options:
            raise SettingError(f"one of {', '.join(options)}")
        return v
    return check


def _int(lo, hi):
    def check(v):
        if not re.fullmatch(r"[0-9]{1,4}", v) or not lo <= int(v) <= hi:
            raise SettingError(f"a whole number from {lo} to {hi}")
        return str(int(v))
    return check


def _model(v):
    if v.removesuffix("[1m]") not in MODELS:
        raise SettingError(f"one of {', '.join(MODELS)} (optionally with [1m])")
    return v


def _ssh_user(v):
    """Never anything ssh would read as an option (a leading '-')."""
    if not SSH_USER_RX.fullmatch(v):
        raise SettingError("a login name: lower-case letters, digits, '_' and '-', starting "
                           "with a letter or '_'")
    return v


def _items(v: str, seps: str) -> list[str]:
    out = list(dict.fromkeys(x for x in re.split(f"[{seps}\\s]+", v) if x))
    if len(out) > MAX_ITEMS:
        raise SettingError(f"at most {MAX_ITEMS}")
    return out


def _slack_ids(rx, what):
    def check(v):
        ids = _items(v, ",")
        bad = next((x for x in ids if not rx.fullmatch(x)), None)
        if bad:
            raise SettingError(f"{bad!r} is not a Slack {what} ID (it is in the {what}'s details)")
        return ",".join(ids)
    return check


def _hosts(v):
    """Every axon-gotcha-<number> site and/or machines by exact name, never a wider pattern:
    this is what the bot's command guard lets it SSH into."""
    parts = _items(v, "|,")
    if not parts:
        raise SettingError("at least one machine")
    bad = next((x for x in parts if x != SITES and not HOST_RX.fullmatch(x)), None)
    if bad:
        raise SettingError(f"{bad!r} is not a machine name (lower-case letters, digits, '-'); "
                           "any other pattern is set on the server")
    return "|".join(parts)


FLAG = _choice(("0", "1"))
CHECKS = {
    "BRIDGE_MODEL": _model,
    "BRIDGE_EFFORT": _choice(EFFORTS),
    "BRIDGE_TIMEOUT_S": _int(30, 600),
    "BRIDGE_MAX_CONCURRENT": _int(1, 5),
    "BRIDGE_RTL_MARKS": FLAG,
    "BRIDGE_ALLOW_PING": FLAG,
    "BRIDGE_ALLOWED_HOSTS": _hosts,
    "SLACK_ALLOWED_CHANNELS": _slack_ids(CHANNEL_RX, "channel"),
    "SLACK_ALLOWED_USERS": _slack_ids(SLACK_USER_RX, "user"),
    "SLACK_ALLOW_DMS": FLAG,
    "GOTCHA_SSH_USER": _ssh_user,
}


def listing() -> dict:
    """ops.bot_config(), with what an admin may change here and who last changed each setting."""
    cfg = ops.bot_config()
    meta = meta_load(S.secrets_meta_file)
    for row in cfg["settings"]:
        m = meta.get(row["name"], {})
        row.update(editable=row["name"] in CHECKS, read_only=READ_ONLY.get(row["name"]),
                   updated_at=m.get("at"), updated_by=m.get("by"))
    return {**cfg, "models": MODELS, "efforts": EFFORTS, "sites": SITES}


def precheck(changes: dict[str, str | None]) -> dict[str, str | None]:
    """Every change checked, before the admin's code is spent: NAME -> the value to write, or
    None to remove the line."""
    if not (S.bridge_dir / ".env").exists():
        raise SettingError("there is no slack-bridge/.env on this host")
    if not changes:
        raise SettingError("nothing to change")
    out = {}
    for name, val in changes.items():
        if name in READ_ONLY:
            raise SettingError(f"{name} can't be changed here: {READ_ONLY[name]}")
        if name not in CHECKS:
            raise SettingError(f"{name} is not one of the bot's settings changed here")
        text = None if val is None else val.strip()
        if name == "SLACK_ALLOWED_CHANNELS" and not text:
            # The bot answers in no channel without it, and its `make check` (so Deploy) fails.
            raise SettingError("SLACK_ALLOWED_CHANNELS: at least one channel")
        if text is None or (not text and name in EMPTY_IS_DEFAULT):
            out[name] = None
            continue
        if len(text) > MAX_LEN:
            raise SettingError(f"{name}: longer than {MAX_LEN} characters")
        try:
            out[name] = CHECKS[name](text)
        except SettingError as e:
            raise SettingError(f"{name}: {e}") from None
    # bridge.authorise refuses everyone with neither list (a hand-edited .env may have no
    # channels): split as the bot splits them, so "," counts as empty.
    after = {**ops.bot_env(), **out}
    if not any(x.strip() for k in ("SLACK_ALLOWED_CHANNELS", "SLACK_ALLOWED_USERS")
               for x in (after.get(k) or "").split(",")):
        raise SettingError("with no allowed channels and no allowed users the bot answers nobody")
    return out


def put(changes: dict[str, str | None], who: str) -> dict[str, str | None]:
    """Write the changes into .env, leaving every other line as it was. Returns what was
    written for each line that really changed (None: removed); empty if nothing was."""
    with file_lock("bot-env"):
        values = precheck(changes)   # again, under the lock: the file may have changed
        now = ops.bot_env()
        changed = {k: v for k, v in values.items() if v != now.get(k)}
        if changed:
            env_update(S.bridge_dir / ".env",
                       {k: None if v is None else env_line(k, v) for k, v in changed.items()})
            meta = meta_load(S.secrets_meta_file)
            for k in changed:
                meta_touch(meta, k, who)
            meta_save(S.secrets_meta_file, meta)
    return changed
