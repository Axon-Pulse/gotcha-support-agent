"""The Access tab: each system's addresses and SSH users for everyone, its SSH passwords for
admins behind a code, written back to the retired bot's two files."""
import json
import stat

import pytest
import yaml

from console import auth
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}
INV = """version: 1
systems:
  gotcha5:
    site: Test site
    components:
      1:
        type: node
        address: 10.0.0.1
        scheme: http
        access:
          ssh:
            user: admin
            password_env: NODE_1_SSH_PW
      cam:
        type: camera
        address: 10.0.0.9
        port: 8080
"""
SECRETS = "ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN\nNODE_1_SSH_PW=hunter2-CANARY\n"


@pytest.fixture
def inv(env, clock):
    S.inventory_file.write_text(INV)
    S.device_secrets_file.write_text(SECRETS)
    S.device_secrets_file.chmod(0o600)
    auth.enroll("admin@example.com")
    return clock


def _stepup(client, clock):
    assert client.post("/api/auth/stepup", headers=ADMIN,
                       json={"code": clock.code("admin@example.com")}).status_code == 200


def _save(client, clock, comp, code=None, **body):
    body["code"] = clock.code("admin@example.com") if code is None else code
    return client.put(f"/api/access/gotcha5/{comp}", headers=ADMIN, json=body)


def _audit():
    return [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]


def test_everyone_sees_addresses_and_users_but_no_password(client, inv):
    r = client.get("/api/access")
    assert r.status_code == 200
    comps = r.json()["systems"][0]["components"]
    assert [(c["name"], c["address"], c["user"], c["has_password"]) for c in comps] == [
        ("1", "10.0.0.1", "admin", True), ("cam", "10.0.0.9", None, False)]
    assert "hunter2" not in r.text and "sk-ant" not in r.text
    assert r.json()["can_edit"] is False
    assert client.get("/api/access", headers=ADMIN).json()["can_edit"] is True


def test_a_viewer_can_neither_reveal_nor_change(client, inv):
    assert client.post("/api/access/gotcha5/1/reveal", json={}).status_code == 403
    assert client.put("/api/access/gotcha5/1", json={"address": "10.9.9.9", "code": "1"}).status_code == 403
    assert S.inventory_file.read_text() == INV


def test_an_admin_reveals_a_password_only_after_a_code(client, inv):
    r = client.post("/api/access/gotcha5/1/reveal", json={}, headers=ADMIN)
    assert r.status_code == 403 and r.json()["detail"] == "stepup"
    _stepup(client, inv)
    r = client.post("/api/access/gotcha5/1/reveal", json={}, headers=ADMIN)
    assert r.json()["password"] == "hunter2-CANARY" and r.headers["cache-control"] == "no-store"
    assert client.post("/api/access/gotcha5/cam/reveal", json={}, headers=ADMIN).status_code == 404
    assert client.post("/api/access/gotcha5/nope/reveal", json={}, headers=ADMIN).status_code == 404
    assert _audit()[-1]["action"] == "reveal-password"


def test_the_page_cant_point_a_component_at_another_secret(client, inv):
    """The old console's hole: set password_env to ANTHROPIC_API_KEY, then read it back as
    the component's password. Here password_env is not a field the page can send."""
    r = _save(client, inv, "1", password_env="ANTHROPIC_API_KEY", password="pw-new")
    assert r.status_code == 200
    text = S.device_secrets_file.read_text()
    assert "NODE_1_SSH_PW=pw-new\n" in text and "ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN\n" in text
    c = yaml.safe_load(S.inventory_file.read_text())["systems"]["gotcha5"]["components"][1]
    assert c["access"]["ssh"]["password_env"] == "NODE_1_SSH_PW"


def test_a_save_needs_a_good_code(client, inv):
    for code in ("", "000000"):
        assert _save(client, inv, "1", code=code, address="10.9.9.9").status_code == 401
    assert S.inventory_file.read_text() == INV


def test_changing_address_port_and_user_rewrites_the_yaml(client, inv):
    r = _save(client, inv, "1", address="10.0.0.2", port=22, user="gotcha")
    assert r.status_code == 200 and r.json()["changed"] == ["address", "port", "user"]
    c = yaml.safe_load(S.inventory_file.read_text())["systems"]["gotcha5"]["components"][1]
    assert (c["address"], c["port"], c["access"]["ssh"]["user"]) == ("10.0.0.2", 22, "gotcha")
    assert stat.S_IMODE(S.inventory_file.stat().st_mode) == 0o600
    assert S.device_secrets_file.read_text() == SECRETS  # passwords untouched
    assert _audit()[-1]["fields"] == ["address", "port", "user"]


def test_a_new_password_goes_only_to_the_secrets_file(client, inv):
    r = _save(client, inv, "1", password="new pass#$x")
    assert r.status_code == 200 and "new pass" not in r.text
    text = S.device_secrets_file.read_text()
    assert "NODE_1_SSH_PW=new pass#$x\n" in text and "ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN" in text
    assert stat.S_IMODE(S.device_secrets_file.stat().st_mode) == 0o600
    assert "new pass" not in S.inventory_file.read_text()
    assert "new pass" not in (S.state_dir / "audit.jsonl").read_text()


def test_a_component_without_one_gets_a_password_env(client, inv):
    assert _save(client, inv, "cam", user="root", password="pw1").json()["changed"] == \
        ["user", "password_env", "password"]
    c = yaml.safe_load(S.inventory_file.read_text())["systems"]["gotcha5"]["components"]["cam"]
    assert c["access"]["ssh"] == {"user": "root", "password_env": "GOTCHA5_CAM_SSH_PW"}
    assert "GOTCHA5_CAM_SSH_PW=pw1\n" in S.device_secrets_file.read_text()


def test_clearing_a_password(client, inv):
    assert _save(client, inv, "1", clear_password=True).json()["changed"] == ["password"]
    assert "NODE_1_SSH_PW" not in S.device_secrets_file.read_text()


@pytest.mark.parametrize("body", [
    {"address": "10.0.0.1; rm -rf /"}, {"port": 70000}, {"user": "bad user"},
    {"password": "a\nB=c"}, {"password": ""},
])
def test_bad_values_are_refused(client, inv, body):
    assert _save(client, inv, "1", **body).status_code == 400
    assert S.inventory_file.read_text() == INV and S.device_secrets_file.read_text() == SECRETS
