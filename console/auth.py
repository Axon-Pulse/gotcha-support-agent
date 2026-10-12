"""Roles, two-factor codes and sessions.

The tailnet tells us who is asking (server.caller); this module decides whether they have
proven it with a code from their authenticator app, and what their role lets them do.

  viewer  everyone in CONSOLE_ALLOWED_USERS: the console as before, secret names only
  admin   also in CONSOLE_ADMINS: may reveal and change secrets, each behind a fresh code

Opening the console takes a code (a session of CONSOLE_SESSION_HOURS). Revealing a secret
takes a code no older than CONSOLE_STEPUP_SECONDS; saving one takes a code on that request.

Enrolment happens only on the server's shell (`make -C console totp-enroll EMAIL=...`).
The Tailscale-User-Login header can be sent by any process on this host, so enrolling from
the page would let such a process enrol as anyone who hasn't yet. With enrolment here, a
forged header alone gets neither a session nor a secret.

    python -m console.auth enroll <email> [--force]
    python -m console.auth reset <email>
    python -m console.auth recovery <email>     # a fresh set of recovery codes, same app entry
    python -m console.auth list
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

import pyotp

from console.settings import S

ISSUER = "Gotcha support console"
MAX_FAILS = 5
LOCK_SECONDS = 300
IDLE_SECONDS = 2 * 3600
RECOVERY_CODES = 8
# No 0/o, 1/l/i: a recovery code is read off paper.
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

_now = time.time  # tests move the clock through this


class AuthError(Exception):
    """A code that didn't prove anything. The message is safe to show the caller."""


def role(who: str) -> str:
    if who == "local":
        return "admin" if S.local_role == "admin" else "viewer"
    return "admin" if who in S.admins else "viewer"


# --------------------------------------------------------------------------
# Files only the console user may read
# --------------------------------------------------------------------------

def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, owner-only from the first byte, never half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# The TOTP store: console/state/totp.json
# --------------------------------------------------------------------------

_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(S.totp_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save(store: dict) -> None:
    write_private(S.totp_file, json.dumps(store, indent=1, sort_keys=True))


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _norm_recovery(code: str) -> str:
    return "".join(c for c in code.lower() if c.isalnum())


def enrolled(who: str) -> bool:
    return who in _load()


def enroll(who: str, force: bool = False) -> tuple[str, str, list[str]]:
    """A new secret for `who`: (base32 secret, otpauth URI, recovery codes). Shown once."""
    who = who.strip().lower()
    if not who or any(c.isspace() for c in who):
        raise AuthError("give the login as Tailscale shows it, e.g. name@company.com")
    secret = pyotp.random_base32()
    codes = _new_codes()
    with _LOCK:
        store = _load()
        if who in store and not force:
            raise AuthError(f"{who} is already enrolled (use --force to replace)")
        store[who] = {"secret": secret, "created": round(_now(), 3),
                      "recovery": [_hash(c) for c in codes], "last_step": 0,
                      "fails": 0, "locked_until": 0}
        _save(store)
    uri = pyotp.TOTP(secret).provisioning_uri(name=who, issuer_name=ISSUER)
    return secret, uri, [f"{c[:4]}-{c[4:]}" for c in codes]


def _new_codes() -> list[str]:
    return ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
            for _ in range(RECOVERY_CODES)]


def new_recovery_codes(who: str) -> list[str]:
    """Replace `who`'s recovery codes with a fresh set; the old ones stop working. The secret,
    and so the entry in their authenticator app, stays."""
    who = who.strip().lower()
    codes = _new_codes()
    with _LOCK:
        store = _load()
        if who not in store:
            raise AuthError(f"{who} is not enrolled")
        store[who]["recovery"] = [_hash(c) for c in codes]
        _save(store)
    return [f"{c[:4]}-{c[4:]}" for c in codes]


def reset(who: str) -> bool:
    """Remove `who`'s authenticator and their passkeys (a lost phone may hold both)."""
    who = who.strip().lower()
    with _LOCK:
        store = _load()
        if store.pop(who, None) is None:
            return False
        _save(store)
    from console import passkeys
    for k in passkeys.listing(who):
        passkeys.remove(who, k["id"])
    return True


def verify(who: str, code: str) -> None:
    """Raise AuthError unless `code` is `who`'s current TOTP code or an unused recovery code.

    A TOTP code is accepted once: its time step is remembered, so a code seen over someone's
    shoulder can't be used again in the same 90 seconds. Five wrong codes lock the login for
    five minutes."""
    code = (code or "").strip().replace(" ", "")
    with _LOCK:
        store = _load()
        entry = store.get(who)
        if entry is None:
            raise AuthError(f"{who} has no authenticator set up. Ask an admin to run "
                            f"`make -C console totp-enroll EMAIL={who}` on the server.")
        now = _now()
        if entry.get("locked_until", 0) > now:
            wait = int(entry["locked_until"] - now) + 1
            raise AuthError(f"too many wrong codes; try again in {wait}s")
        ok = reused = False
        if code.isdigit() and len(code) == 6:
            totp = pyotp.TOTP(entry["secret"])
            step_now = int(now // totp.interval)
            for step in (step_now - 1, step_now, step_now + 1):
                if hmac.compare_digest(totp.at(step * totp.interval), code):
                    if step > entry.get("last_step", 0):
                        entry["last_step"] = step
                        ok = True
                    else:
                        reused = True
                    break
        elif len(_norm_recovery(code)) == 8:
            h = _hash(_norm_recovery(code))
            if h in entry["recovery"]:
                entry["recovery"].remove(h)
                ok = True
        if ok:
            entry["fails"] = 0
        elif reused:
            pass  # the right code, just spent: not a guess, so it doesn't count towards the lock
        else:
            entry["fails"] = entry.get("fails", 0) + 1
            if entry["fails"] >= MAX_FAILS:
                entry["fails"], entry["locked_until"] = 0, now + LOCK_SECONDS
        _save(store)
    if reused:
        raise AuthError("that code was already used; wait for the app to show the next one")
    if not ok:
        raise AuthError("wrong code")


# --------------------------------------------------------------------------
# Sessions: in memory, so a restart of the console logs everyone out
# --------------------------------------------------------------------------

COOKIE = "console_session"
SESSIONS: dict[str, dict] = {}
_SLOCK = threading.Lock()


def new_session(who: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _SLOCK:
        SESSIONS[token] = {"who": who, "created": now, "last_seen": now, "stepup_until": 0,
                           "enrolled": _load().get(who, {}).get("created")}
    return token


def get_session(token: str | None, who: str) -> dict | None:
    """The session behind this cookie, if it is still good for this caller."""
    if not token:
        return None
    now = _now()
    with _SLOCK:
        s = SESSIONS.get(token)
        if s is None:
            return None
        expired = (now - s["created"] > S.session_hours * 3600
                   or now - s["last_seen"] > IDLE_SECONDS)
        # A reset or re-enrolment on the shell ends the sessions made with the old secret.
        stale = _load().get(s["who"], {}).get("created") != s["enrolled"]
        if expired or stale:
            SESSIONS.pop(token, None)
            return None
        if s["who"] != who:
            return None
        s["last_seen"] = now
        return s


def drop_session(token: str | None) -> None:
    with _SLOCK:
        SESSIONS.pop(token or "", None)


def grant_stepup(session: dict) -> float:
    session["stepup_until"] = _now() + S.stepup_seconds
    return session["stepup_until"]


def stepup_ok(session: dict | None) -> bool:
    return bool(session) and _now() < session["stepup_until"]


# --------------------------------------------------------------------------
# Command line, run on the server
# --------------------------------------------------------------------------

def _print_qr(uri: str) -> None:
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.print_ascii(invert=True)


def _cli(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in ("enroll", "reset", "recovery", "list"):
        print("\n".join(__doc__.strip().splitlines()[-4:]))
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        store = _load()
        if not store:
            print("Nobody is enrolled.")
        for who, e in sorted(store.items()):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["created"]))
            print(f"{who:40} enrolled {when}  recovery codes left: {len(e['recovery'])}"
                  f"  role: {role(who)}")
        return 0
    if not args:
        print(f"usage: python -m console.auth {cmd} <email>")
        return 2
    who = args[0].strip().lower()
    if cmd == "recovery":
        try:
            codes = new_recovery_codes(who)
        except AuthError as e:
            print(e)
            return 1
        print(f"New recovery codes for {who}, one use each; the old ones no longer work:")
        for c in codes:
            print(f"    {c}")
        return 0
    if cmd == "reset":
        if reset(who):
            print(f"Removed {who}'s authenticator and passkeys; their sessions end now. "
                  f"Enrol again with: make -C console totp-enroll EMAIL={who}")
            return 0
        print(f"{who} was not enrolled.")
        return 1
    try:
        secret, uri, codes = enroll(who, force="--force" in args)
    except AuthError as e:
        print(e)
        return 1
    print(f"\nScan this with Google Authenticator, Authy or similar, as {who}:\n")
    _print_qr(uri)
    print(f"\nOr type the key in by hand: {secret}\n")
    print("Recovery codes, one use each, for a lost phone. Keep them somewhere safe:")
    for c in codes:
        print(f"    {c}")
    try:
        first = input("\nEnter the 6-digit code the app shows now, to confirm: ")
        verify(who, first)
    except (AuthError, EOFError, KeyboardInterrupt) as e:
        reset(who)
        print(f"\nNot confirmed ({e or 'cancelled'}); enrolment undone. Run it again.")
        return 1
    print(f"\nDone. {who} signs in to the console with the app's code (role: {role(who)}).")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
