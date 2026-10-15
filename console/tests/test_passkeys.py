"""Passkeys. A real signed answer needs a real authenticator, so the library's two checks are
replaced by stand-ins that accept or refuse; everything around them is the real code."""
import json
import stat
import time
from types import SimpleNamespace

import pytest

from console import auth, passkeys
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}
CRED = {"id": "Y3JlZC0x", "rawId": "Y3JlZC0x", "type": "public-key", "response": {}}


@pytest.fixture
def pk(env, clock, monkeypatch):
    auth.enroll("local")
    auth.enroll("admin@example.com")
    state = {"ok": True, "seen": []}

    def reg(**kw):
        state["seen"].append(("register", kw["expected_rp_id"], kw["expected_origin"],
                              kw["require_user_verification"]))
        if not state["ok"]:
            raise ValueError("bad signature")
        return SimpleNamespace(credential_id=b"cred-1", credential_public_key=b"pub", sign_count=0)

    def authn(**kw):
        state["seen"].append(("auth", kw["expected_rp_id"], kw["credential_current_sign_count"],
                              kw["require_user_verification"]))
        if not state["ok"]:
            raise ValueError("bad signature")
        return SimpleNamespace(new_sign_count=kw["credential_current_sign_count"] + 1)

    monkeypatch.setattr(passkeys, "_verify_registration", reg)
    monkeypatch.setattr(passkeys, "_verify_authentication", authn)
    return state


def _register(c, clock, who="local", headers=None):
    h = headers or {}
    r = c.post("/api/passkeys/register/options", json={"code": clock.code(who)}, headers=h)
    assert r.status_code == 200, r.text
    opts = r.json()
    assert opts["rp"]["id"] == "testserver" and opts["authenticatorSelection"]["userVerification"] == "required"
    return c.post("/api/passkeys/register", json={"credential": CRED, "name": "my phone"}, headers=h)


def test_registering_needs_a_code_and_then_lists_the_passkey(client, pk, clock):
    assert client.post("/api/passkeys/register/options", json={"code": "000000"}).status_code == 401
    r = _register(client, clock)
    assert r.status_code == 200 and r.json()["passkey"]["name"] == "my phone"
    assert [k["name"] for k in client.get("/api/passkeys").json()["passkeys"]] == ["my phone"]
    assert client.get("/api/auth/status").json()["passkeys"] == 1
    assert stat.S_IMODE(S.passkeys_file.stat().st_mode) == 0o600
    assert pk["seen"][-1] == ("register", "testserver", "http://testserver", True)


def test_register_without_options_or_with_a_bad_answer_fails(client, pk, clock):
    r = client.post("/api/passkeys/register", json={"credential": CRED})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    pk["ok"] = False
    assert _register(client, clock).status_code == 400
    assert client.get("/api/passkeys").json()["passkeys"] == []


def test_signing_in_with_a_passkey(client, raw_client, pk, clock):
    _register(client, clock)
    assert raw_client.get("/api/meta").status_code == 401
    r = raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    assert r.status_code == 200 and r.json()["allowCredentials"][0]["id"] == "Y3JlZC0x"
    r = raw_client.post("/api/auth/login", json={"passkey": CRED})
    assert r.status_code == 200 and r.json()["logged_in"]
    assert raw_client.get("/api/meta").status_code == 200
    log = [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]
    assert log[-1]["action"] == "login" and log[-1]["method"] == "passkey"


def test_a_challenge_is_used_once_and_expires(client, raw_client, pk, clock):
    _register(client, clock)
    raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    assert raw_client.post("/api/auth/login", json={"passkey": CRED}).status_code == 200
    assert raw_client.post("/api/auth/login", json={"passkey": CRED}).status_code == 401
    raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    clock.t += passkeys.CHALLENGE_SECONDS + 1
    assert raw_client.post("/api/auth/login", json={"passkey": CRED}).status_code == 401


def test_the_sign_count_moves_on(client, raw_client, pk, clock):
    _register(client, clock)
    for _ in range(2):
        raw_client.post("/api/passkeys/options", json={"purpose": "login"})
        raw_client.post("/api/auth/login", json={"passkey": CRED})
    counts = [s[2] for s in pk["seen"] if s[0] == "auth"]
    assert counts == [0, 1]


def test_a_refused_or_unknown_passkey_doesnt_sign_in(client, raw_client, pk, clock):
    _register(client, clock)
    raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    assert raw_client.post("/api/auth/login", json={"passkey": {**CRED, "id": "other"}}).status_code == 401
    pk["ok"] = False
    raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    assert raw_client.post("/api/auth/login", json={"passkey": CRED}).status_code == 401


def test_no_passkey_means_no_options(raw_client, pk):
    r = raw_client.post("/api/passkeys/options", json={"purpose": "login"})
    assert r.status_code == 400 and "no passkey" in r.json()["detail"]


def test_an_admin_unlocks_reveals_and_confirms_a_save_with_a_passkey(client, pk, clock, monkeypatch):
    from console import ops
    monkeypatch.setattr(ops, "DEPLOY", ops.Deploy())
    monkeypatch.setattr(S, "restart_cmd", ["true"])
    S.bridge_dir.joinpath(".env").write_text("SLACK_BOT_TOKEN=xoxb-old\n")
    _register(client, clock, "admin@example.com", ADMIN)
    client.post("/api/passkeys/options", json={"purpose": "stepup"}, headers=ADMIN)
    assert client.post("/api/auth/stepup", json={"passkey": CRED}, headers=ADMIN).status_code == 200
    r = client.post("/api/secrets/SLACK_BOT_TOKEN/reveal", json={}, headers=ADMIN)
    assert r.json()["value"] == "xoxb-old"
    # a stepup challenge can't confirm a save: purposes are kept apart
    client.post("/api/passkeys/options", json={"purpose": "stepup"}, headers=ADMIN)
    r = client.put("/api/secrets/SLACK_BOT_TOKEN", json={"value": "xoxb-new", "passkey": CRED}, headers=ADMIN)
    assert r.status_code == 401
    client.post("/api/passkeys/options", json={"purpose": "confirm"}, headers=ADMIN)
    r = client.put("/api/secrets/SLACK_BOT_TOKEN", json={"value": "xoxb-new", "passkey": CRED}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "saved"
    for _ in range(500):
        if not ops.DEPLOY.running:
            break
        time.sleep(0.01)


def test_a_viewer_cant_ask_for_stepup_or_confirm_challenges(client, pk, clock):
    _register(client, clock)
    assert client.post("/api/passkeys/options", json={"purpose": "stepup"}).status_code == 403
    assert client.post("/api/passkeys/options", json={"purpose": "confirm"}).status_code == 403


def test_stepup_and_confirm_challenges_need_a_session(raw_client, pk):
    assert raw_client.post("/api/passkeys/options", json={"purpose": "stepup"}, headers=ADMIN).status_code == 401


def test_removing_a_passkey_and_a_reset_removes_them_all(client, pk, clock):
    _register(client, clock)
    kid = client.get("/api/passkeys").json()["passkeys"][0]["id"]
    assert client.request("DELETE", f"/api/passkeys/{kid}", json={}).status_code == 401  # needs a code
    r = client.request("DELETE", f"/api/passkeys/{kid}", json={"code": clock.code("local")})
    assert r.json()["passkeys"] == []
    _register(client, clock)
    auth.reset("local")
    assert passkeys.listing("local") == []


def test_the_rp_id_is_the_host_name_without_port(monkeypatch):
    req = SimpleNamespace(headers={"host": "localhost:8777"}, url=SimpleNamespace(netloc="", scheme="http"))
    assert passkeys.origin_of(req) == ("http://localhost:8777", "localhost")
    req.headers = {"host": "gotcha-server.tail1234.ts.net", "x-forwarded-proto": "https"}
    assert passkeys.origin_of(req) == ("https://gotcha-server.tail1234.ts.net", "gotcha-server.tail1234.ts.net")
    monkeypatch.setattr(S, "rp_id", "example.ts.net")
    assert passkeys.origin_of(req)[1] == "example.ts.net"


def test_a_forced_re_enrolment_removes_passkeys_once_confirmed(client, pk, clock, monkeypatch):
    _register(client, clock)
    old = auth._load()["local"]["secret"]
    monkeypatch.setattr("builtins.input", lambda _: "000000")
    assert auth._cli(["enroll", "local", "--force"]) == 1
    assert auth._load()["local"]["secret"] == old and passkeys.count("local") == 1  # kept
    import pyotp
    monkeypatch.setattr("builtins.input",
                        lambda _: pyotp.TOTP(auth._load()["local"]["secret"]).at(clock.t + 30))
    assert auth._cli(["enroll", "local", "--force"]) == 0
    assert auth._load()["local"]["secret"] != old and passkeys.count("local") == 0
