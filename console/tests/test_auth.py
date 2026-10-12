"""Signing in with an authenticator code, and the second code for secrets."""
import json
import stat

import pytest

from console import auth
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}


@pytest.fixture
def enrolled(env, clock):
    auth.enroll("local")
    auth.enroll("admin@example.com")
    return clock


def _login(c, clock, who="local", headers=None):
    return c.post("/api/auth/login", json={"code": clock.code(who)}, headers=headers or {})


def test_nothing_but_the_page_and_sign_in_without_a_session(raw_client):
    assert raw_client.get("/").status_code == 200
    assert raw_client.get("/api/auth/status").json()["logged_in"] is False
    for path in ("/api/meta", "/api/conversations", "/api/cases", "/api/bot", "/api/secrets",
                 "/api/audit", "/api/systems", "/api/deploy"):
        r = raw_client.get(path)
        assert r.status_code == 401 and r.json()["detail"] == "login", path


def test_the_right_code_signs_in_and_logout_signs_out(raw_client, enrolled):
    r = _login(raw_client, enrolled)
    assert r.status_code == 200 and r.json()["logged_in"] and r.json()["role"] == "viewer"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert raw_client.get("/api/meta").status_code == 200
    assert raw_client.post("/api/auth/logout", json={}).status_code == 200
    assert raw_client.get("/api/meta").status_code == 401


def test_a_wrong_code_and_a_reused_code_are_refused(raw_client, enrolled):
    assert raw_client.post("/api/auth/login", json={"code": "000000"}).status_code == 401
    code = enrolled.code("local")
    assert raw_client.post("/api/auth/login", json={"code": code}).status_code == 200
    r = raw_client.post("/api/auth/login", json={"code": code})
    assert r.status_code == 401 and "already used" in r.json()["detail"]


def test_five_wrong_codes_lock_the_login_for_a_while(raw_client, enrolled):
    for _ in range(5):
        raw_client.post("/api/auth/login", json={"code": "123456"})
    r = _login(raw_client, enrolled)
    assert r.status_code == 401 and "too many" in r.json()["detail"]
    enrolled.t += auth.LOCK_SECONDS
    assert _login(raw_client, enrolled).status_code == 200


def test_a_recovery_code_works_once(raw_client, env, clock):
    _, _, codes = auth.enroll("local")
    assert raw_client.post("/api/auth/login", json={"code": codes[0].upper()}).status_code == 200
    assert raw_client.post("/api/auth/login", json={"code": codes[0]}).status_code == 401


def test_someone_not_enrolled_is_told_how_to_get_a_code(raw_client, env, clock):
    st = raw_client.get("/api/auth/status").json()
    assert st["enrolled"] is False
    r = raw_client.post("/api/auth/login", json={"code": "123456"})
    assert r.status_code == 401 and "totp-enroll" in r.json()["detail"]


def test_a_session_is_only_good_for_the_login_that_made_it(raw_client, enrolled):
    _login(raw_client, enrolled, "admin@example.com", ADMIN)
    assert raw_client.get("/api/meta", headers=ADMIN).status_code == 200
    r = raw_client.get("/api/meta", headers={"tailscale-user-login": "pm@example.com"})
    assert r.status_code == 401


def test_a_session_ends_after_its_hours_and_when_idle(raw_client, enrolled):
    _login(raw_client, enrolled)
    enrolled.t += auth.IDLE_SECONDS + 1
    assert raw_client.get("/api/meta").status_code == 401
    _login(raw_client, enrolled)
    for _ in range(int(S.session_hours) + 1):
        enrolled.t += 3600
        raw_client.get("/api/meta")
    assert raw_client.get("/api/meta").status_code == 401


def test_a_reset_on_the_shell_ends_that_users_sessions(raw_client, enrolled):
    _login(raw_client, enrolled)
    auth.reset("local")
    assert raw_client.get("/api/meta").status_code == 401


def test_the_step_up_lasts_thirty_minutes_and_is_for_admins(raw_client, enrolled):
    _login(raw_client, enrolled)
    r = raw_client.post("/api/auth/stepup", json={"code": enrolled.code("local")})
    assert r.status_code == 403
    _login(raw_client, enrolled, "admin@example.com", ADMIN)
    r = raw_client.post("/api/auth/stepup", json={"code": enrolled.code("admin@example.com")},
                        headers=ADMIN)
    assert r.status_code == 200
    start = enrolled.t
    assert raw_client.get("/api/auth/status", headers=ADMIN).json()["stepup_until"] == start + 1800
    enrolled.t = start + 29 * 60
    assert raw_client.get("/api/auth/status", headers=ADMIN).json()["stepup_until"]
    enrolled.t = start + 31 * 60
    assert raw_client.get("/api/auth/status", headers=ADMIN).json()["stepup_until"] is None


def test_sign_ins_and_failures_are_audited(raw_client, enrolled):
    raw_client.post("/api/auth/login", json={"code": "000000"})
    _login(raw_client, enrolled)
    log = [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]
    assert [(e["action"], e["who"], e["role"]) for e in log] == [
        ("login-failed", "local", "viewer"), ("login", "local", "viewer")]


def test_the_code_store_is_owner_only(env, clock):
    auth.enroll("someone@example.com")
    assert stat.S_IMODE(S.totp_file.stat().st_mode) == 0o600
    with pytest.raises(auth.AuthError):
        auth.enroll("someone@example.com")
    auth.enroll("someone@example.com", force=True)


def test_enrolling_on_the_shell_is_undone_without_a_confirming_code(env, clock, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "000000")
    assert auth._cli(["enroll", "new@example.com"]) == 1
    assert not auth.enrolled("new@example.com")
    monkeypatch.setattr("builtins.input",
                        lambda _: __import__("pyotp").TOTP(auth._load()["new@example.com"]["secret"]).at(clock.t))
    assert auth._cli(["enroll", "new@example.com"]) == 0
    out = capsys.readouterr().out
    assert "Recovery codes" in out and "Done." in out
    assert auth.enrolled("new@example.com")


def test_roles(env, monkeypatch):
    assert auth.role("admin@example.com") == "admin"
    assert auth.role("pm@example.com") == "viewer"
    assert auth.role("local") == "viewer"
    monkeypatch.setattr(S, "local_role", "admin")
    assert auth.role("local") == "admin"


def test_new_recovery_codes_replace_the_old_and_keep_the_app_entry(raw_client, env, clock):
    secret, _, old = auth.enroll("local")
    new = auth.new_recovery_codes("local")
    assert auth._load()["local"]["secret"] == secret and set(new).isdisjoint(old)
    assert raw_client.post("/api/auth/login", json={"code": old[0]}).status_code == 401
    assert raw_client.post("/api/auth/login", json={"code": new[0]}).status_code == 200
    with pytest.raises(auth.AuthError):
        auth.new_recovery_codes("nobody@example.com")
