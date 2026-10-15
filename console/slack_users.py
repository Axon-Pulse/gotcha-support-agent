"""Slack user IDs (U0…), as the bot logs who asked, turned into the names people know.

Looked up with the bot's own token (users.info, which needs the app's `users:read` scope) and
kept in console/state/slack_users.json, so each person is asked of Slack about once a week.
The token stays in this process: it goes only in the request's Authorization header.
"""
import json
import re
import time
import urllib.parse
import urllib.request

from console import secrets_env
from console.files import file_lock, meta_load, meta_save
from console.safe_text import has_control
from console.settings import S

ID_RX = re.compile(r"[UW][A-Z0-9]{6,20}")
FRESH_S = 7 * 86400         # a known name is asked again after a week (people rename)
RETRY_S = 600               # an ID Slack didn't resolve is tried again after 10 minutes
MAX_LOOKUPS = 25            # per call, so a page never waits on Slack for long
TIMEOUT_S = 4
# Errors that are about the token or the app, not the user: no point asking about the others.
APP_ERRORS = {"missing_scope": "the Slack app needs the users:read permission to show names "
                               "(api.slack.com → the app → OAuth & Permissions, then reinstall)",
              "not_authed": "the bot's Slack token is missing", "invalid_auth": "the bot's Slack "
              "token is not valid", "token_revoked": "the bot's Slack token was revoked",
              "account_inactive": "the bot's Slack token was revoked"}


def _path():
    return S.state_dir / "slack_users.json"


def _fetch(token: str, uid: str) -> dict:
    """Slack's answer to users.info for one ID."""
    req = urllib.request.Request(
        "https://slack.com/api/users.info?" + urllib.parse.urlencode({"user": uid}),
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 (a fixed https URL)
        return json.load(r)


def _name_of(user: dict) -> str | None:
    p = user.get("profile") or {}
    for v in (p.get("real_name"), user.get("real_name"), p.get("display_name"), user.get("name")):
        if isinstance(v, str) and v.strip() and not has_control(v):
            return v.strip()[:80]
    return None


def names(ids) -> tuple[dict[str, str], str | None]:
    """{ID: name} for the IDs whose name is known, and why some may be missing (or None).
    A name past its week is still returned while it is asked again."""
    ids = sorted({i for i in ids if isinstance(i, str) and ID_RX.fullmatch(i)})
    now = time.time()
    with file_lock("slack-users"):
        cache = meta_load(_path())
        out = {i: cache[i]["name"] for i in ids if (cache.get(i) or {}).get("name")}
        due = [i for i in ids if now - (cache.get(i) or {}).get("at", 0)
               >= (FRESH_S if (cache.get(i) or {}).get("name") else RETRY_S)]
        if not due:
            return out, None
        token = secrets_env.hidden().get("SLACK_BOT_TOKEN")
        if not token:
            return out, APP_ERRORS["not_authed"]
        problem, changed = None, False
        for uid in due[:MAX_LOOKUPS]:
            try:
                answer = _fetch(token, uid)
            except (OSError, ValueError):   # no network, a timeout, or not JSON: try later
                problem = "Slack could not be reached to look up names"
                break
            err = answer.get("error")
            if err in APP_ERRORS:
                problem = APP_ERRORS[err]
                break
            if err == "ratelimited":
                break
            name = _name_of(answer.get("user") or {}) if answer.get("ok") else None
            cache[uid] = {"name": name or (cache.get(uid) or {}).get("name"), "at": now}
            changed = True
            if cache[uid]["name"]:
                out[uid] = cache[uid]["name"]
        if changed:
            meta_save(_path(), cache)
    return out, problem
