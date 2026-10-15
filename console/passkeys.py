"""Passkeys (WebAuthn): sign in, unlock reveals and confirm saves with a fingerprint, face or
phone instead of typing a code.

A passkey is registered from the page by someone already signed in, and only after a code
from their authenticator app, so a forged Tailscale-User-Login header can't register one.
The authenticator app and recovery codes stay as the fallback.

The browser allows passkeys only on a secure page: https:// or localhost. The relying-party
ID is the host name the page was opened on (or CONSOLE_RP_ID), so a passkey made on
127.0.0.1 works only there, and one made on the tailnet name works only there.

Each ceremony is two calls: options (the server picks a random challenge, kept here for
CHALLENGE_SECONDS, usable once) and verify (the browser's signed answer to it).
"""
import json
import threading
import time

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from console import auth
from console.auth import AuthError
from console.files import file_lock, write_private
from console.settings import S

RP_NAME = "Gotcha support console"
CHALLENGE_SECONDS = 300
_LOCK = threading.Lock()
# (who, purpose) -> (challenge bytes, issued at, origin, rp_id)
_CHALLENGES: dict[tuple[str, str], tuple[bytes, float, str, str]] = {}

# The library's checks, behind names a test can replace: a real signed answer needs a real
# authenticator.
_verify_registration = webauthn.verify_registration_response
_verify_authentication = webauthn.verify_authentication_response


# --------------------------------------------------------------------------
# Store: console/state/passkeys.json, {who: [{id, public_key, sign_count, name, ...}]}
# --------------------------------------------------------------------------

def _load() -> dict:
    try:
        return json.loads(S.passkeys_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save(store: dict) -> None:
    write_private(S.passkeys_file, json.dumps(store, indent=1, sort_keys=True))


def listing(who: str) -> list[dict]:
    return [{"id": k["id"], "name": k.get("name"), "created": k.get("created"),
             "last_used": k.get("last_used")} for k in _load().get(who, [])]


def remove_all(who: str) -> None:
    with file_lock("passkeys"):
        store = _load()
        if store.pop(who, None) is not None:
            _save(store)


def count(who: str) -> int:
    return len(_load().get(who, []))


def remove(who: str, cred_id: str) -> bool:
    with file_lock("passkeys"):
        store = _load()
        keys = store.get(who, [])
        left = [k for k in keys if k["id"] != cred_id]
        if len(left) == len(keys):
            return False
        store[who] = left
        _save(store)
    return True


# --------------------------------------------------------------------------
# Where the page is
# --------------------------------------------------------------------------

def host_name(request) -> str:
    """The host name the page was opened as, without its port ([::1]:8766 -> ::1)."""
    host = request.headers.get("host") or request.url.netloc
    if host.startswith("["):
        return host[1:host.index("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def origin_of(request) -> tuple[str, str]:
    """(origin, rp_id) of the page as the browser sees it. The rp_id is the bare host name;
    browsers refuse an IP address there, so open the page as localhost, not 127.0.0.1."""
    host = request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}", S.rp_id or host_name(request)


def _issue(who: str, purpose: str, challenge: bytes, origin: str, rp_id: str) -> None:
    with _LOCK:
        now = auth._now()
        for k in [k for k, v in _CHALLENGES.items() if now - v[1] > CHALLENGE_SECONDS]:
            _CHALLENGES.pop(k, None)
        _CHALLENGES[(who, purpose)] = (challenge, now, origin, rp_id)


def _take(who: str, purpose: str) -> tuple[bytes, str, str]:
    with _LOCK:
        c = _CHALLENGES.pop((who, purpose), None)
    if c is None or auth._now() - c[1] > CHALLENGE_SECONDS:
        raise AuthError("the passkey request expired; try again")
    return c[0], c[2], c[3]


# --------------------------------------------------------------------------
# Registering a passkey
# --------------------------------------------------------------------------

def registration_options(who: str, request) -> str:
    origin, rp_id = origin_of(request)
    existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"]))
                for k in _load().get(who, [])]
    opts = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_name=who, user_display_name=who,
        user_id=who.encode(), exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED))
    _issue(who, "register", opts.challenge, origin, rp_id)
    return webauthn.options_to_json(opts)


def register(who: str, credential: dict, name: str) -> dict:
    challenge, origin, rp_id = _take(who, "register")
    try:
        v = _verify_registration(credential=credential, expected_challenge=challenge,
                                 expected_rp_id=rp_id, expected_origin=origin,
                                 require_user_verification=True)
    except Exception as e:
        raise AuthError(f"the passkey could not be registered: {e}") from e
    cred_id = bytes_to_base64url(v.credential_id)
    entry = {"id": cred_id, "public_key": bytes_to_base64url(v.credential_public_key),
             "sign_count": v.sign_count, "rp_id": rp_id,
             "name": (name or "").strip()[:60] or f"passkey {time.strftime('%Y-%m-%d')}",
             "created": round(auth._now(), 3), "last_used": None}
    with file_lock("passkeys"):
        store = _load()
        if any(k["id"] == cred_id for keys in store.values() for k in keys):
            raise AuthError("that passkey is already registered")
        store.setdefault(who, []).append(entry)
        _save(store)
    return {k: entry[k] for k in ("id", "name", "created")}


# --------------------------------------------------------------------------
# Using a passkey: sign in, step up, confirm a save
# --------------------------------------------------------------------------

PURPOSES = ("login", "stepup", "confirm")


def authentication_options(who: str, purpose: str, request) -> str:
    if purpose not in PURPOSES:
        raise AuthError("unknown passkey request")
    origin, rp_id = origin_of(request)
    keys = [k for k in _load().get(who, []) if k.get("rp_id", rp_id) == rp_id]
    if not keys:
        raise AuthError(f"{who} has no passkey for {rp_id}; sign in with a code and register one")
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["id"]))
                           for k in keys])
    _issue(who, purpose, opts.challenge, origin, rp_id)
    return webauthn.options_to_json(opts)


def authenticate(who: str, purpose: str, credential: dict) -> None:
    """Raise AuthError unless `credential` is `who`'s passkey answering this purpose's challenge."""
    challenge, origin, rp_id = _take(who, purpose)
    cred_id = (credential or {}).get("id") or (credential or {}).get("rawId")
    with file_lock("passkeys"):
        store = _load()
        key = next((k for k in store.get(who, []) if k["id"] == cred_id), None)
        if key is None:
            raise AuthError("that passkey is not registered for you")
        try:
            v = _verify_authentication(
                credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                expected_origin=origin, credential_public_key=base64url_to_bytes(key["public_key"]),
                credential_current_sign_count=key["sign_count"], require_user_verification=True)
        except Exception as e:
            raise AuthError(f"the passkey was not accepted: {e}") from e
        key["sign_count"], key["last_used"] = v.new_sign_count, round(auth._now(), 3)
        _save(store)


def reset_challenges() -> None:
    with _LOCK:
        _CHALLENGES.clear()

