"""System access in the Secrets tab: every system's four categories and the components in
them, every component's own fields. Everyone signed in sees names and plain fields; secrets
(SSH passwords, secret fields) only an admin reveals, behind a code; every change is an
admin's, with a code, written field by field into the retired bot's two files."""
import json
import re
import stat

import pytest
import yaml

from console import access, auth
from console.settings import S

ADMIN = {"tailscale-user-login": "admin@example.com"}
INV = """version: 1
systems:
  gotcha5:
    site: Test site
    components:
      1:
        type: node
        domain: radar
        address: 10.0.0.1
        scheme: http
        access:
          ssh:
            user: admin
            password_env: NODE_1_SSH_PW
      cam:
        type: camera
        domain: optic
        address: 10.0.0.9
        port: 8080
      nuc:
        type: nuc
  gotcha6:
    site: Other site
    components:
      2:
        type: node
        access: {ssh: {user: admin, password_env: SHARED_SSH_PW}}
      3:
        type: node
        access: {ssh: {user: admin, password_env: SHARED_SSH_PW}}
"""
SECRETS = ("ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN\nNODE_1_SSH_PW=hunter2-CANARY\n"
           "SHARED_SSH_PW=shared-pw\n")


@pytest.fixture
def inv(env, clock):
    S.inventory_file.write_text(INV)
    S.device_secrets_file.write_text(SECRETS)
    S.device_secrets_file.chmod(0o600)
    auth.enroll("admin@example.com")
    return clock


def _doc():
    return yaml.safe_load(S.inventory_file.read_text())


def _comp(system, comp):
    return _doc()["systems"][system]["components"][comp]


def _secrets():
    return S.device_secrets_file.read_text()


def _audit():
    return [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]


def _call(client, clock, method, path, code=None, **body):
    body["code"] = clock.code("admin@example.com") if code is None else code
    return client.request(method, "/api/access/" + path, headers=ADMIN, json=body)


def _stepup(client, clock):
    assert client.post("/api/auth/stepup", headers=ADMIN,
                       json={"code": clock.code("admin@example.com")}).status_code == 200


def _cats(r_json, system):
    s = next(s for s in r_json["systems"] if s["name"] == system)
    return {c["key"]: [x["name"] for x in c["components"]] for c in s["categories"]}


def _find(r_json, system, comp):
    s = next(s for s in r_json["systems"] if s["name"] == system)
    return next(x for c in s["categories"] for x in c["components"] if x["name"] == comp)


REVEAL = "/api/access/system/gotcha5/component/1/reveal"


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_every_system_has_the_four_categories_in_order(client, inv):
    r = client.get("/api/access").json()
    g5 = r["systems"][0]
    assert [c["label"] for c in g5["categories"]] == ["Radars", "Acoustics", "Cameras",
                                                      "Compute & Infrastructure"]
    # domain radar -> Radars, optic -> Cameras, none -> Compute & Infrastructure
    assert _cats(r, "gotcha5") == {"radars": ["1"], "acoustics": [], "cameras": ["cam"], "compute": ["nuc"]}
    assert _cats(r, "gotcha6") == {"radars": [], "acoustics": [], "cameras": [], "compute": ["2", "3"]}
    assert [c["key"] for c in r["categories"]] == ["radars", "acoustics", "cameras", "compute"]


def test_everyone_sees_plain_fields_but_no_secret(client, inv):
    r = client.get("/api/access")
    c = _find(r.json(), "gotcha5", "1")
    assert (c["address"], c["user"], c["has_password"]) == ("10.0.0.1", "admin", True)
    assert "hunter2" not in r.text and "sk-ant" not in r.text and "password_env" not in r.text
    assert r.json()["can_edit"] is False
    assert client.get("/api/access", headers=ADMIN).json()["can_edit"] is True


def test_there_is_no_way_to_read_or_write_the_whole_file():
    """Every access route works on one system, component or field; none takes or returns
    the file as text."""
    from console.server import app
    paths = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/access")})
    assert paths == ["/api/access", "/api/access/system", "/api/access/system/{system}",
                     "/api/access/system/{system}/component",
                     "/api/access/system/{system}/component/{component}",
                     "/api/access/system/{system}/component/{component}/reveal"]


def test_every_access_route_needs_a_session(raw_client, inv):
    for method, path in [("GET", "/api/access"), ("POST", "/api/access/system"),
                         ("PUT", "/api/access/system/gotcha5"), ("DELETE", "/api/access/system/gotcha5"),
                         ("POST", "/api/access/system/gotcha5/component"),
                         ("PUT", "/api/access/system/gotcha5/component/1"),
                         ("DELETE", "/api/access/system/gotcha5/component/1"),
                         ("POST", "/api/access/system/gotcha5/component/1/reveal")]:
        r = raw_client.request(method, path, json={}, headers=ADMIN)
        assert r.status_code == 401, (method, path)


def test_an_admin_reveals_the_ssh_password_only_after_a_code(client, inv):
    assert client.post(REVEAL, json={}).status_code == 403  # a viewer
    r = client.post(REVEAL, json={}, headers=ADMIN)
    assert r.status_code == 403 and r.json()["detail"] == "stepup"
    _stepup(client, inv)
    r = client.post(REVEAL, json={}, headers=ADMIN)
    assert r.json()["value"] == "hunter2-CANARY" and r.headers["cache-control"] == "no-store"
    assert client.post("/api/access/system/gotcha5/component/cam/reveal", json={}, headers=ADMIN).status_code == 404
    assert client.post("/api/access/system/gotcha5/component/nope/reveal", json={}, headers=ADMIN).status_code == 404
    e = _audit()[-1]
    assert (e["action"], e["target"], e["field"]) == ("reveal-access", "gotcha5/1", "password")


# --------------------------------------------------------------------------
# Who may change, and with what
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "system", {"name": "x"}),
    ("PUT", "system/gotcha5", {"site": "x"}),
    ("DELETE", "system/gotcha5", {}),
    ("POST", "system/gotcha5/component", {"name": "x", "category": "radars"}),
    ("PUT", "system/gotcha5/component/1", {"address": "10.9.9.9"}),
    ("DELETE", "system/gotcha5/component/1", {}),
])
def test_every_change_is_for_admins_with_a_good_code(client, inv, method, path, body):
    assert client.request(method, "/api/access/" + path, json={**body, "code": "1"}).status_code == 403
    for code in ("", "000000"):
        assert _call(client, inv, method, path, code=code, **body).status_code == 401
    assert S.inventory_file.read_text() == INV and _secrets() == SECRETS


def test_a_typo_is_refused_before_the_code_is_spent(client, inv):
    code = inv.code("admin@example.com")
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", code=code, address="10.0.0.1; rm -rf /")
    assert r.status_code == 400
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", code=code, address="10.0.0.5").status_code == 200


# --------------------------------------------------------------------------
# Systems
# --------------------------------------------------------------------------

def test_add_a_system(client, inv):
    r = _call(client, inv, "POST", "system", name="gotcha7", site="Haifa", description="new")
    assert r.status_code == 200
    assert _doc()["systems"]["gotcha7"] == {"site": "Haifa", "description": "new", "components": {}}
    assert _cats(r.json(), "gotcha7") == {"radars": [], "acoustics": [], "cameras": [], "compute": []}
    assert _call(client, inv, "POST", "system", name="gotcha7").status_code == 409  # taken
    assert stat.S_IMODE(S.inventory_file.stat().st_mode) == 0o600
    assert _audit()[-1]["action"] == "add-system"


def test_rename_a_system_and_change_its_location(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5", name="gotcha5-tlv", site="Tel Aviv")
    assert r.status_code == 200 and r.json()["changed"] == ["site", "name"]
    systems = _doc()["systems"]
    assert list(systems) == ["gotcha5-tlv", "gotcha6"]  # kept its place
    assert systems["gotcha5-tlv"]["site"] == "Tel Aviv"
    assert systems["gotcha5-tlv"]["components"][1]["access"]["ssh"]["password_env"] == "NODE_1_SSH_PW"
    e = _audit()[-1]
    assert e["action"] == "edit-system" and e["target"] == "gotcha5" and e["to"] == "gotcha5-tlv"
    assert _call(client, inv, "PUT", "system/gotcha5-tlv", name="gotcha6").status_code == 409


def test_a_field_left_out_is_left_alone_and_empty_removes_it(client, inv):
    _call(client, inv, "PUT", "system/gotcha5", description="x")
    assert _doc()["systems"]["gotcha5"]["site"] == "Test site"
    _call(client, inv, "PUT", "system/gotcha5", site="")
    assert "site" not in _doc()["systems"]["gotcha5"]


def test_delete_a_system_takes_its_secrets_but_no_others(client, inv):
    r = _call(client, inv, "DELETE", "system/gotcha5")
    assert r.status_code == 200 and [s["name"] for s in r.json()["systems"]] == ["gotcha6"]
    text = _secrets()
    assert "NODE_1_SSH_PW" not in text and "SHARED_SSH_PW=shared-pw" in text and "ANTHROPIC_API_KEY" in text
    assert _audit()[-1]["action"] == "delete-system" and _audit()[-1]["components"] == 3
    assert _call(client, inv, "DELETE", "system/gotcha5").status_code == 404


# --------------------------------------------------------------------------
# Components in categories
# --------------------------------------------------------------------------

def test_add_components_to_any_category_in_any_number(client, inv):
    for name, cat in [("switch-1", "compute"), ("switch-2", "compute"), ("mic-a", "acoustics"),
                      ("cam-2", "cameras"), ("cam-3", "cameras")]:
        assert _call(client, inv, "POST", "system/gotcha5/component", name=name, category=cat).status_code == 200
    cats = _cats(client.get("/api/access").json(), "gotcha5")
    assert cats == {"radars": ["1"], "acoustics": ["mic-a"], "cameras": ["cam", "cam-2", "cam-3"],
                    "compute": ["nuc", "switch-1", "switch-2"]}
    assert _comp("gotcha5", "switch-1") == {"domain": "infra"}  # an unmanaged switch: nothing else
    assert _comp("gotcha5", "mic-a") == {"domain": "acoustic"}


def test_a_component_needs_a_valid_category(client, inv):
    assert _call(client, inv, "POST", "system/gotcha5/component", name="x").status_code in (400, 422)
    assert _call(client, inv, "POST", "system/gotcha5/component", name="x", category="drones").status_code == 400


def test_move_a_component_to_another_category(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/nuc", category="radars")
    assert r.status_code == 200 and r.json()["changed"] == ["category"]
    assert _cats(r.json(), "gotcha5")["radars"] == ["1", "nuc"]
    assert _comp("gotcha5", "nuc")["domain"] == "radar"


def test_add_a_component_with_a_password(client, inv):
    r = _call(client, inv, "POST", "system/gotcha5/component", name="radar2", category="radars",
              type="radar", address="10.0.0.20", port=22, user="root", password="pw1")
    assert r.status_code == 200 and "pw1" not in r.text
    assert _comp("gotcha5", "radar2") == {
        "domain": "radar", "type": "radar", "address": "10.0.0.20", "port": 22,
        "access": {"ssh": {"user": "root", "password_env": "GOTCHA5_RADAR2_SSH_PW"}}}
    assert "GOTCHA5_RADAR2_SSH_PW=pw1\n" in _secrets()
    assert _call(client, inv, "POST", "system/gotcha5/component", name="radar2", category="radars").status_code == 409
    assert "pw1" not in (S.state_dir / "audit.jsonl").read_text()


def test_a_numeric_component_name_stays_a_number(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="12", category="radars")
    assert 12 in _doc()["systems"]["gotcha5"]["components"]


def test_edit_and_rename_a_component(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", name="node-1", role="primary",
              address="10.0.0.2", port=22, user="gotcha", scheme="HTTPS")
    assert r.status_code == 200
    assert r.json()["changed"] == ["name", "role", "scheme", "address", "port", "user"]
    comps = _doc()["systems"]["gotcha5"]["components"]
    assert list(comps) == ["node-1", "cam", "nuc"]
    c = comps["node-1"]
    assert (c["role"], c["scheme"], c["address"], c["port"], c["access"]["ssh"]["user"]) == \
        ("primary", "https", "10.0.0.2", 22, "gotcha")
    assert c["access"]["ssh"]["password_env"] == "NODE_1_SSH_PW"  # the password stays with it
    assert _call(client, inv, "PUT", "system/gotcha5/component/node-1", name="cam").status_code == 409


def test_nothing_sent_changes_nothing(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", address="10.0.0.1")
    assert r.json()["status"] == "unchanged" and S.inventory_file.read_text() == INV


def test_a_new_password_goes_only_to_the_secrets_file(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", password="new pass#$x")
    assert r.status_code == 200 and "new pass" not in r.text
    assert "NODE_1_SSH_PW=new pass#$x\n" in _secrets() and "ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN" in _secrets()
    assert stat.S_IMODE(S.device_secrets_file.stat().st_mode) == 0o600
    assert S.inventory_file.read_text() == INV  # only the password changed


def test_the_page_cant_point_a_component_at_another_secret(client, inv):
    """The retired console's hole: password_env set to ANTHROPIC_API_KEY, then revealed."""
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", password_env="ANTHROPIC_API_KEY", password="pw-new")
    assert r.status_code == 200
    assert "NODE_1_SSH_PW=pw-new\n" in _secrets() and "ANTHROPIC_API_KEY=sk-ant-NEVER-SHOWN\n" in _secrets()
    assert _comp("gotcha5", 1)["access"]["ssh"]["password_env"] == "NODE_1_SSH_PW"


def test_clearing_a_password_and_a_shared_one_is_refused(client, inv):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", clear_password=True).json()["changed"] == ["password"]
    assert "NODE_1_SSH_PW" not in _secrets()
    assert _comp("gotcha5", 1)["access"] == {"ssh": {"user": "admin"}}  # no dangling password_env
    for body in ({"clear_password": True}, {"password": "new"}):
        r = _call(client, inv, "PUT", "system/gotcha6/component/2", **body)
        assert r.status_code == 400 and "shared" in r.json()["detail"]
    assert "SHARED_SSH_PW=shared-pw" in _secrets()


def test_removing_the_user_leaves_no_empty_access_block(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="x", category="compute", user="root")
    _call(client, inv, "PUT", "system/gotcha5/component/x", user="")
    assert _comp("gotcha5", "x") == {"domain": "infra"}


def test_delete_a_component_keeps_a_shared_password(client, inv):
    assert _call(client, inv, "DELETE", "system/gotcha6/component/2").status_code == 200
    assert "SHARED_SSH_PW=shared-pw" in _secrets()
    _call(client, inv, "DELETE", "system/gotcha6/component/3")
    assert "SHARED_SSH_PW" not in _secrets()
    assert _doc()["systems"]["gotcha6"]["components"] == {}


@pytest.mark.parametrize("body", [
    {"address": "10.0.0.1; rm -rf /"}, {"port": 70000}, {"port": True}, {"user": "bad user"},
    {"password": "a\nB=c"}, {"name": "../etc"}, {"name": ""}, {"scheme": "ht tp"},
    {"type": "x" * 201}, {"role": "a\nb"}, {"category": "x"},
    {"fields": {"bad\nname": "x"}}, {"fields": {"ok": "a b"}},
    {"secret_fields": {"web pw": "a\x85b"}},
])
def test_bad_values_are_refused(client, inv, body):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", **body).status_code in (400, 422)
    assert S.inventory_file.read_text() == INV and _secrets() == SECRETS


# --------------------------------------------------------------------------
# Extra fields, plain and secret
# --------------------------------------------------------------------------

def test_plain_extra_fields_are_per_component_and_replaced_as_a_set(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", fields={"azimuth": "120", "firmware": "4.2"})
    assert r.status_code == 200 and r.json()["changed"] == ["fields"]
    assert _comp("gotcha5", 1)["fields"] == {"azimuth": "120", "firmware": "4.2"}
    assert _find(client.get("/api/access").json(), "gotcha5", "1")["fields"] == {"azimuth": "120", "firmware": "4.2"}
    assert "fields" not in _comp("gotcha5", "cam")  # only this component
    _call(client, inv, "PUT", "system/gotcha5/component/1", fields={"azimuth": "125"})
    assert _comp("gotcha5", 1)["fields"] == {"azimuth": "125"}
    _call(client, inv, "PUT", "system/gotcha5/component/1", fields={})
    assert "fields" not in _comp("gotcha5", 1)


@pytest.mark.parametrize("name", ["password", "web password", "API key", "snmp community", "token"])
def test_a_plain_field_named_like_a_secret_is_refused(client, inv, name):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", fields={name: "x"})
    assert r.status_code == 400 and "secret field" in r.json()["detail"]


def test_secret_fields_are_hidden_revealed_with_a_code_and_removed(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/cam",
              secret_fields={"web password": "cam-pw", "snmp community": "public2"})
    assert r.status_code == 200 and "cam-pw" not in r.text
    assert r.json()["changed"] == ["secret:web password", "secret:snmp community"]
    assert _comp("gotcha5", "cam")["secret_fields"] == [
        {"name": "web password", "env": "GOTCHA5_CAM_WEB_PASSWORD_PW"},
        {"name": "snmp community", "env": "GOTCHA5_CAM_SNMP_COMMUNITY_PW"}]
    assert "GOTCHA5_CAM_WEB_PASSWORD_PW=cam-pw\n" in _secrets()
    listed = _find(client.get("/api/access").json(), "gotcha5", "cam")
    assert listed["secret_fields"] == [{"name": "web password", "set": True}, {"name": "snmp community", "set": True}]
    path = "/api/access/system/gotcha5/component/cam/reveal"
    assert client.post(path, json={"field": "web password"}, headers=ADMIN).status_code == 403  # no step-up
    _stepup(client, inv)
    assert client.post(path, json={"field": "web password"}, headers=ADMIN).json()["value"] == "cam-pw"
    assert client.post(path, json={"field": "nope"}, headers=ADMIN).status_code == 404
    assert _audit()[-1]["field"] == "web password"
    assert client.post(path, json={"field": "web password"}).status_code == 403  # a viewer
    # change one, remove the other
    _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"web password": "pw2", "snmp community": None})
    assert _comp("gotcha5", "cam")["secret_fields"] == [{"name": "web password", "env": "GOTCHA5_CAM_WEB_PASSWORD_PW"}]
    assert "GOTCHA5_CAM_WEB_PASSWORD_PW=pw2\n" in _secrets() and "SNMP" not in _secrets()
    assert "pw2" not in (S.state_dir / "audit.jsonl").read_text()


def test_a_secret_field_cant_reuse_a_plain_fields_name(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/1", fields={"web": "http://x"})
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", secret_fields={"web": "x"})
    assert r.status_code == 400 and "plain field" in r.json()["detail"]


def test_deleting_a_component_takes_its_secret_fields(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"web password": "cam-pw"})
    _call(client, inv, "DELETE", "system/gotcha5/component/cam")
    assert "CAM" not in _secrets() and "ANTHROPIC_API_KEY" in _secrets()


def test_the_secret_field_reveal_cant_reach_other_entries(client, inv):
    """A hand-edited secret_fields entry naming another env is the only way; the page can't."""
    r = _call(client, inv, "PUT", "system/gotcha5/component/cam",
              secret_fields={"x": "v"}, **{"secret_fields_env": "ANTHROPIC_API_KEY"})
    assert r.status_code == 200
    assert _comp("gotcha5", "cam")["secret_fields"][0]["env"] == "GOTCHA5_CAM_X_PW"


# --------------------------------------------------------------------------
# From the review
# --------------------------------------------------------------------------

def test_a_long_name_still_gets_an_env_the_file_keeps(client, inv):
    long_sys = "gotcha-production-site-haifa-north"
    _call(client, inv, "POST", "system", name=long_sys)
    r = _call(client, inv, "POST", f"system/{long_sys}/component", name="thermal-camera-main-entrance",
              category="cameras", password="pw9", secret_fields={"a rather long field name here": "s9"})
    assert r.status_code == 200
    c = _comp(long_sys, "thermal-camera-main-entrance")
    for env in (c["access"]["ssh"]["password_env"], c["secret_fields"][0]["env"]):
        assert access.ENV_RX.fullmatch(env) and env.endswith("_PW")
    found = _find(r.json(), long_sys, "thermal-camera-main-entrance")
    assert found["has_password"] and found["secret_fields"][0]["set"]
    _call(client, inv, "PUT", "system/gotcha5/component/1", password="other")  # another save
    assert "=pw9\n" in _secrets() and "=s9\n" in _secrets()


def test_set_and_remove_password_together_is_refused_before_the_code(client, inv):
    code = inv.code("admin@example.com")
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", code=code, password="x", clear_password=True)
    assert r.status_code == 400 and "not both" in r.json()["detail"]
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", code=code, role="r").status_code == 200


@pytest.mark.parametrize("pw", ["a ANTHROPIC_API_KEY=evil", "a\x85B=c", "a\x1eB=c", "a\x0cB=c"])
def test_unicode_line_breaks_cant_smuggle_a_second_entry(client, inv, pw):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", password=pw).status_code == 400
    assert _secrets() == SECRETS


def test_saving_a_password_keeps_the_rest_of_the_secrets_file(client, inv):
    S.device_secrets_file.write_text("# a comment\nexport OTHER=1\nNODE_1_SSH_PW=old\nlower=2\n")
    _call(client, inv, "PUT", "system/gotcha5/component/1", password="new")
    assert _secrets() == "# a comment\nexport OTHER=1\nNODE_1_SSH_PW=new\nlower=2\n"
    _call(client, inv, "DELETE", "system/gotcha5/component/1")
    assert _secrets() == "# a comment\nexport OTHER=1\nlower=2\n"


def test_odd_shapes_in_a_hand_edited_file_dont_break_the_tab(client, inv):
    S.inventory_file.write_text(INV.replace("password_env: NODE_1_SSH_PW", "password_env: [X]")
                                .replace("        port: 8080\n", "        port: 8080\n        secret_fields: oops\n        fields: [1]\n")
                                + "  broken: 5\n")
    r = client.get("/api/access")
    assert r.status_code == 200
    assert _find(r.json(), "gotcha5", "1")["has_password"] is False
    assert _find(r.json(), "gotcha5", "cam")["secret_fields"] == [] and _find(r.json(), "gotcha5", "cam")["fields"] == {}
    assert _call(client, inv, "DELETE", "system/gotcha6").status_code == 200
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", password="x")
    assert r.status_code == 400 and "password_env" in r.json()["detail"]


def test_a_broken_file_error_shows_the_line_not_its_text(client, inv):
    S.inventory_file.write_text("systems:\n  a: {password: hunter2, [\n")
    err = client.get("/api/access").json()["error"]
    assert "line" in err and "hunter2" not in err
    assert _call(client, inv, "POST", "system", name="x").status_code == 400
    assert "hunter2" in S.inventory_file.read_text()  # not overwritten


def test_renames_carry_who_changed_what(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/1", role="x")
    _call(client, inv, "PUT", "system/gotcha5/component/1", name="n1")
    _call(client, inv, "PUT", "system/gotcha5", name="g5")
    meta = json.loads(S.access_meta_file.read_text())
    assert "g5/n1" in meta and "g5" in meta and not any(k.startswith("gotcha5") for k in meta)
    c = _find(client.get("/api/access").json(), "g5", "n1")
    assert c["updated_by"] == "admin@example.com"


def test_an_integer_keyed_component_renamed_to_a_number_stays_a_number(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/1", name="7")
    assert list(_doc()["systems"]["gotcha5"]["components"]) == [7, "cam", "nuc"]


def test_no_file_yet_and_the_first_system_creates_it(client, env, clock):
    auth.enroll("admin@example.com")
    r = client.get("/api/access").json()
    assert (r["systems"], r["inventory_file"]) == ([], False)
    assert _call(client, clock, "POST", "system", name="first").status_code == 200
    assert _doc() == {"systems": {"first": {"components": {}}}}


# --------------------------------------------------------------------------
# From the second review
# --------------------------------------------------------------------------

def test_field_names_are_one_name_whatever_the_case(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"serial": "a"})
    _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"Serial": "b"})
    assert _comp("gotcha5", "cam")["secret_fields"] == [{"name": "serial", "env": "GOTCHA5_CAM_SERIAL_PW"}]
    assert "GOTCHA5_CAM_SERIAL_PW=b\n" in _secrets()
    r = _call(client, inv, "PUT", "system/gotcha5/component/cam", fields={"SERIAL": "visible"})
    assert r.status_code == 400 and "secret field" in r.json()["detail"]
    r = _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"x": "1", "X": "2"})
    assert r.status_code == 400
    # a secret removed in the same request frees its name for a plain field
    r = _call(client, inv, "PUT", "system/gotcha5/component/cam", secret_fields={"serial": None}, fields={"serial": "ok"})
    assert r.status_code == 200 and _comp("gotcha5", "cam")["fields"] == {"serial": "ok"}


@pytest.mark.parametrize("name,secret", [
    ("admin pw", True), ("pwd", True), ("wifi psk", True), ("API key", True), ("web_password", True),
    ("snmp community", True), ("otp", True), ("auth token", True),
    ("compass heading", False), ("passive mode", False), ("keyframe interval", False),
    ("author", False), ("spin", False), ("azimuth", False), ("keyboard layout", False),
])
def test_what_sounds_like_a_secret(name, secret):
    assert bool(access.SECRET_WORDS.search(name)) is secret


def test_a_hand_edited_secret_env_that_isnt_a_name_is_ignored(client, inv):
    S.inventory_file.write_text(INV.replace("        port: 8080\n",
                                            "        port: 8080\n        secret_fields: [{name: x, env: foo bar}]\n"))
    c = _find(client.get("/api/access").json(), "gotcha5", "cam")
    assert c["secret_fields"] == []


def test_missing_and_taken_names_answer_404_and_409(client, inv):
    assert _call(client, inv, "PUT", "system/nope", site="x").status_code == 404
    assert _call(client, inv, "PUT", "system/gotcha5/component/nope", role="x").status_code == 404
    assert _call(client, inv, "POST", "system/nope/component", name="x", category="radars").status_code == 404
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", name="cam").status_code == 409
    assert _call(client, inv, "PUT", "system/gotcha5", name="gotcha6").status_code == 409


def test_an_edit_is_audited_as_edit_component(client, inv):
    _call(client, inv, "PUT", "system/gotcha5/component/1", role="x")
    assert _audit()[-1]["action"] == "edit-component"


# --------------------------------------------------------------------------
# The rest of the file stays as it was
# --------------------------------------------------------------------------

HAND = """# Inventory, kept by hand too.
version: 1
systems:
  gotcha5:   # the Haifa site
    site: Haifa
    components:
      1: {type: node, domain: radar, address: 10.0.0.1}   # flow style
      cam:
        type: camera
        domain: optic
        fields: {a: yes, b: 1.10, c: ~, d: 010, e: 2024-01-02, multi: "x\\ny", pad: " p "}
        secret_fields: [{name: web password, env: CAM_WEB_PW}]
"""


@pytest.fixture
def hand(env, clock):
    S.inventory_file.write_text(HAND)
    S.device_secrets_file.write_text("CAM_WEB_PW=old\n")
    auth.enroll("admin@example.com")
    return clock


def test_an_edit_changes_only_what_it_changes(client, hand):
    r = _call(client, hand, "PUT", "system/gotcha5/component/1", address="10.0.0.2")
    assert r.status_code == 200
    assert S.inventory_file.read_text() == HAND.replace("address: 10.0.0.1}", "address: 10.0.0.2}")


def test_untouched_fields_keep_their_exact_values_when_one_is_added(client, hand):
    fields = _find(client.get("/api/access").json(), "gotcha5", "cam")["fields"]
    r = _call(client, hand, "PUT", "system/gotcha5/component/cam", fields={**fields, "new": "yes"})
    assert r.status_code == 200, r.text
    text = S.inventory_file.read_text()
    assert "fields: {a: yes, b: 1.10, c: ~, d: 010, e: 2024-01-02, multi: \"x\\ny\", pad: \" p \", new: 'yes'}" in text
    assert yaml.safe_load(text)["systems"]["gotcha5"]["components"]["cam"]["fields"]["new"] == "yes"


def test_an_emptied_plain_field_is_removed(client, hand):
    fields = _find(client.get("/api/access").json(), "gotcha5", "cam")["fields"]
    _call(client, hand, "PUT", "system/gotcha5/component/cam", fields={**fields, "a": ""})
    assert "a" not in _comp("gotcha5", "cam")["fields"]


def test_a_new_secret_value_leaves_the_yaml_byte_for_byte(client, hand):
    _call(client, hand, "PUT", "system/gotcha5/component/cam", secret_fields={"web password": "new"})
    assert S.inventory_file.read_text() == HAND and "CAM_WEB_PW=new\n" in _secrets()


def test_a_rename_keeps_the_comments_and_the_place(client, hand):
    _call(client, hand, "PUT", "system/gotcha5", name="gotcha5-haifa")
    text = S.inventory_file.read_text()
    assert text.startswith("# Inventory, kept by hand too.\n") and "# flow style" in text
    assert list(_doc()["systems"]) == ["gotcha5-haifa"]


@pytest.mark.parametrize("value", ["yes", "no", "on", "010", "1.0", "0x10", "null", "~", "2024-01-02", "a: b"])
def test_values_an_older_reader_would_misread_are_quoted(client, hand, value):
    _call(client, hand, "PUT", "system/gotcha5/component/1", role=value)
    assert yaml.safe_load(S.inventory_file.read_text())["systems"]["gotcha5"]["components"][1]["role"] == value


def test_components_sharing_yaml_through_an_anchor_are_not_edited(client, inv):
    S.inventory_file.write_text("""systems:
  s:
    components:
      a: {access: &acc {ssh: {user: root}}}
      b: {access: *acc}
""")
    r = _call(client, inv, "PUT", "system/s/component/a", password="pw-of-a")
    assert r.status_code == 400 and "anchor" in r.json()["detail"]
    assert "pw-of-a" not in _secrets()


# --------------------------------------------------------------------------
# One code, then a few minutes of editing
# --------------------------------------------------------------------------

def test_one_code_unlocks_system_access_edits_for_ten_minutes(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", role="a")
    assert r.status_code == 200 and r.json()["edit_until"] == pytest.approx(inv.t + 600, abs=1)
    # further changes need no code while the window is open
    for body in ({"role": "b"}, {"address": "10.0.0.7"}):
        r = client.put("/api/access/system/gotcha5/component/1", headers=ADMIN, json=body)
        assert r.status_code == 200, r.text
    assert client.post("/api/access/system/gotcha5/component", headers=ADMIN,
                       json={"name": "mic", "category": "acoustics"}).status_code == 200
    assert _audit()[-1]["method"] == "edit-window"
    # ten minutes after the code, a code again
    inv.t += 601
    r = client.put("/api/access/system/gotcha5/component/1", headers=ADMIN, json={"role": "c"})
    assert r.status_code == 401
    assert _comp("gotcha5", 1)["role"] == "b"


def test_a_wrong_code_opens_no_window(client, inv):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", code="000000", role="a").status_code == 401
    assert client.put("/api/access/system/gotcha5/component/1", headers=ADMIN, json={"role": "a"}).status_code == 401


def test_the_window_is_only_for_system_access(client, inv):
    S.bridge_dir.joinpath(".env").write_text("SLACK_BOT_TOKEN=xoxb-old\n")
    _call(client, inv, "PUT", "system/gotcha5/component/1", role="a")
    # a bot secret still takes a code every time
    r = client.put("/api/secrets/SLACK_BOT_TOKEN", headers=ADMIN, json={"value": "xoxb-new"})
    assert r.status_code == 401
    # and revealing still needs its own step-up
    r = client.post("/api/access/system/gotcha5/component/1/reveal", headers=ADMIN, json={})
    assert r.status_code == 403 and r.json()["detail"] == "stepup"


def test_a_viewer_gets_no_window(client, inv):
    assert client.put("/api/access/system/gotcha5/component/1", json={"role": "a"}).status_code == 403


# --------------------------------------------------------------------------
# Sensor fields, and each sensor's computer
# --------------------------------------------------------------------------

def test_a_sensor_has_its_own_fields(client, inv):
    r = _call(client, inv, "POST", "system/gotcha5/component", name="rdr-2", category="radars",
              version="4.2.1", address="10.0.0.22", port=443, gui="https://10.0.0.22/ui",
              gui_user="admin@site", gui_password="gui-pw", azimuth=120, elevation="-2.5")
    assert r.status_code == 200, r.text
    assert _comp("gotcha5", "rdr-2") == {
        "domain": "radar", "version": "4.2.1", "address": "10.0.0.22", "port": 443,
        "gui": "https://10.0.0.22/ui", "gui_user": "admin@site", "azimuth": 120, "elevation": -2.5,
        "gui_password_env": "GOTCHA5_RDR_2_GUI_PW"}
    assert "GOTCHA5_RDR_2_GUI_PW=gui-pw\n" in _secrets() and "gui-pw" not in r.text
    c = _find(r.json(), "gotcha5", "rdr-2")
    assert (c["version"], c["gui"], c["gui_user"], c["azimuth"], c["elevation"], c["has_gui_password"]) == \
        ("4.2.1", "https://10.0.0.22/ui", "admin@site", 120, -2.5, True)


def test_a_sensor_keeps_its_own_computer_and_a_computer_its_web_user(client, inv):
    r = _call(client, inv, "POST", "system/gotcha5/component", name="rdr-3", category="radars",
              computer_ip="10.0.0.60", computer_web="http://10.0.0.60:8080", computer_web_user="ops",
              computer_web_password="pc-web-pw")
    assert r.status_code == 200, r.text
    assert _comp("gotcha5", "rdr-3") == {
        "domain": "radar", "computer_ip": "10.0.0.60", "computer_web": "http://10.0.0.60:8080",
        "computer_web_user": "ops", "computer_web_password_env": "GOTCHA5_RDR_3_PC_WEB_PW"}
    assert "GOTCHA5_RDR_3_PC_WEB_PW=pc-web-pw\n" in _secrets() and "pc-web-pw" not in r.text
    c = _find(r.json(), "gotcha5", "rdr-3")
    assert (c["computer_ip"], c["computer_web_user"], c["has_computer_web_password"]) == ("10.0.0.60", "ops", True)
    r = _call(client, inv, "PUT", "system/gotcha5/component/rdr-3", web_user="admin", clear=["computer_web_password"])
    assert r.status_code == 200, r.text
    assert _comp("gotcha5", "rdr-3")["web_user"] == "admin"
    assert "computer_web_password_env" not in _comp("gotcha5", "rdr-3") and "PC_WEB_PW" not in _secrets()


@pytest.mark.parametrize("body", [
    {"computer_ip": "1.2.3.4; x"}, {"computer_web": "javascript:alert(1)"}, {"web_user": "a\nb"},
    {"azimuth": 361}, {"azimuth": -1}, {"azimuth": True}, {"azimuth": "north"},
    {"fov_from": -200}, {"fov_from": 10, "fov_to": -10}, {"range": 0}, {"range": "far"},
    {"elevation": 91}, {"elevation": -90.5}, {"gui": "not a url at all"}, {"gui_user": "a\nb"}, {"gui_user": "x" * 65}, {"gui": "javascript:alert(1)"},
    {"web": "http://a b"}, {"rustdesk_ip": "1.2.3.4; x"}, {"users": {"bad\nname": "x"}},
    {"users": {"root": "a b"}}, {"gui_password": "x", "clear": ["gui_password"]},
    {"clear": ["ssh_password"]},
])
def test_sensor_and_computer_values_are_checked(client, inv, body):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", **body).status_code in (400, 422)
    assert S.inventory_file.read_text() == INV and _secrets() == SECRETS


def test_a_computer_with_users_web_and_rustdesk(client, inv):
    r = _call(client, inv, "POST", "system/gotcha5/component", name="pc-rdr1", category="compute",
              model="NUC 13 Pro", sensor="1", address="10.0.0.50", web="http://10.0.0.50:9090",
              web_password="web-pw", rustdesk_ip="10.0.0.50", rustdesk_password="rd-pw",
              users={"gotcha": "u1-pw", "root": "u2-pw", "viewer": ""})
    assert r.status_code == 200, r.text
    c = _comp("gotcha5", "pc-rdr1")
    assert c["model"] == "NUC 13 Pro" and c["sensor"] == "1" and c["rustdesk_ip"] == "10.0.0.50"
    assert c["users"] == [{"name": "gotcha", "password_env": "GOTCHA5_PC_RDR1_USER_GOTCHA_PW"},
                          {"name": "root", "password_env": "GOTCHA5_PC_RDR1_USER_ROOT_PW"},
                          {"name": "viewer"}]
    s = _secrets()
    for v in ("web-pw", "rd-pw", "u1-pw", "u2-pw"):
        assert f"={v}\n" in s and v not in S.inventory_file.read_text() and v not in r.text
    listed = _find(r.json(), "gotcha5", "pc-rdr1")
    assert listed["users"] == [{"name": "gotcha", "set": True}, {"name": "root", "set": True},
                               {"name": "viewer", "set": False}]
    assert listed["has_web_password"] and listed["has_rustdesk_password"]
    # the sensor lists its computer
    assert _find(r.json(), "gotcha5", "1")["computers"] == ["pc-rdr1"]


def test_users_change_one_by_one(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
          users={"a": "pa", "b": "pb"})
    r = _call(client, inv, "PUT", "system/gotcha5/component/pc", users={"a": "pa2", "b": None, "c": ""})
    assert r.status_code == 200 and r.json()["changed"] == ["user:a", "user:b", "user:c"]
    assert _comp("gotcha5", "pc")["users"] == [{"name": "a", "password_env": "GOTCHA5_PC_USER_A_PW"},
                                               {"name": "c"}]
    s = _secrets()
    assert "GOTCHA5_PC_USER_A_PW=pa2\n" in s and "USER_B" not in s
    # a password for a user that had none
    _call(client, inv, "PUT", "system/gotcha5/component/pc", users={"c": "pc"})
    assert "GOTCHA5_PC_USER_C_PW=pc\n" in _secrets()


def test_each_secret_is_revealed_on_its_own(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
          web_password="w", rustdesk_password="r", users={"root": "u"})
    _call(client, inv, "PUT", "system/gotcha5/component/1", gui_password="g")
    _stepup(client, inv)
    path = "/api/access/system/gotcha5/component/{}/reveal"
    get = lambda c, body: client.post(path.format(c), headers=ADMIN, json=body)  # noqa: E731
    assert get("pc", {"slot": "web_password"}).json()["value"] == "w"
    assert get("pc", {"slot": "rustdesk_password"}).json()["value"] == "r"
    assert get("pc", {"user": "root"}).json()["value"] == "u"
    assert get("1", {"slot": "gui_password"}).json()["value"] == "g"
    assert get("1", {}).json()["value"] == "hunter2-CANARY"   # still the SSH password
    assert get("pc", {"user": "nobody"}).status_code == 404
    assert get("pc", {"slot": "ANTHROPIC_API_KEY"}).status_code == 404
    assert client.post(path.format("pc"), json={"user": "root"}).status_code == 403  # a viewer
    reveals = [e["field"] for e in _audit() if e["action"] == "reveal-access"]
    assert reveals == ["web_password", "rustdesk_password", "user:root", "gui_password", "password"]


def test_removing_fixed_passwords(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
          web_password="w", rustdesk_password="r")
    r = _call(client, inv, "PUT", "system/gotcha5/component/pc", clear=["web_password"])
    assert r.json()["changed"] == ["web_password"]
    c = _comp("gotcha5", "pc")
    assert "web_password_env" not in c and "rustdesk_password_env" in c
    assert "WEB" not in _secrets() and "RUSTDESK" in _secrets()


def test_a_computer_can_only_serve_a_sensor_of_its_system(client, inv):
    for sensor in ("nuc", "nope", "cam-x"):  # nuc is compute; nope doesn't exist
        r = _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute", sensor=sensor)
        assert r.status_code == 400, sensor
    assert _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
                 sensor="cam").status_code == 200


def test_links_follow_a_sensor_renamed_moved_or_deleted(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute", sensor="cam")
    _call(client, inv, "PUT", "system/gotcha5/component/cam", name="cam-ptz")
    assert _comp("gotcha5", "pc")["sensor"] == "cam-ptz"
    assert _find(client.get("/api/access").json(), "gotcha5", "cam-ptz")["computers"] == ["pc"]
    _call(client, inv, "PUT", "system/gotcha5/component/cam-ptz", category="compute")
    assert "sensor" not in _comp("gotcha5", "pc")
    _call(client, inv, "PUT", "system/gotcha5/component/cam-ptz", category="cameras")
    _call(client, inv, "PUT", "system/gotcha5/component/pc", sensor="cam-ptz")
    _call(client, inv, "DELETE", "system/gotcha5/component/cam-ptz")
    assert "sensor" not in _comp("gotcha5", "pc")


def test_deleting_a_computer_takes_all_its_passwords(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
          web_password="w", rustdesk_password="r", users={"a": "pa"})
    _call(client, inv, "DELETE", "system/gotcha5/component/pc")
    s = _secrets()
    assert "GOTCHA5_PC_" not in s and "ANTHROPIC_API_KEY" in s


def test_viewers_see_the_fields_but_no_password(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute",
          web="http://x", web_password="SECRET-W", rustdesk_password="SECRET-R", users={"root": "SECRET-U"})
    r = client.get("/api/access")
    assert "SECRET" not in r.text and "_PW" not in r.text and "http://x" in r.text


def test_users_and_secret_fields_keep_their_one_line_style(client, inv):
    S.inventory_file.write_text(INV.replace("      nuc:\n        type: nuc\n",
        "      nuc:\n        type: nuc\n        users: [{name: a, password_env: NUC_A_PW}]   # kept on one line\n"
        "        secret_fields: [{name: s1, env: NUC_S1_PW}]\n"))
    _call(client, inv, "PUT", "system/gotcha5/component/nuc", users={"b": "pb"}, secret_fields={"s2": "v2"})
    text = S.inventory_file.read_text()
    assert re.search(r"users: \[\{name: a, password_env: NUC_A_PW\}, \{name: b, password_env: "
                     r"GOTCHA5_NUC_USER_B_PW\}\] +# kept on one line", text), text
    assert "secret_fields: [{name: s1, env: NUC_S1_PW}, {name: s2, env: GOTCHA5_NUC_S2_PW}]" in text
    _call(client, inv, "PUT", "system/gotcha5/component/nuc", users={"a": None})
    assert "users: [{name: b, password_env: GOTCHA5_NUC_USER_B_PW}]" in S.inventory_file.read_text()



def test_a_sensors_coverage_for_the_tower_view(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", azimuth=30, fov_from=-45, fov_to=45, range=1200)
    assert r.status_code == 200, r.text
    assert _find(r.json(), "gotcha5", "1")["coverage"] == {
        "from": -45, "to": 45, "range": 1200, "default_fov": False, "default_range": False}
    # a camera with nothing set gets gotcha30's defaults; compute has no coverage
    assert _find(r.json(), "gotcha5", "cam")["coverage"] == {
        "from": -30, "to": 30, "range": 500, "default_fov": True, "default_range": True}
    assert "coverage" not in _find(r.json(), "gotcha5", "nuc")


# --------------------------------------------------------------------------
# Where a system stands
# --------------------------------------------------------------------------

def test_a_new_system_with_its_coordinates(client, inv):
    r = _call(client, inv, "POST", "system", name="haifa", site="Haifa port", latitude="32.8191234567",
              longitude=34.9983, altitude=12)
    assert r.status_code == 200, r.text
    assert _doc()["systems"]["haifa"] == {"site": "Haifa port", "latitude": 32.8191235,
                                          "longitude": 34.9983, "altitude": 12, "components": {}}
    s = next(x for x in r.json()["systems"] if x["name"] == "haifa")
    assert (s["latitude"], s["longitude"], s["altitude"]) == (32.8191235, 34.9983, 12)


@pytest.mark.parametrize("body", [
    {"latitude": 91, "longitude": 0}, {"latitude": 0, "longitude": 181}, {"latitude": "north", "longitude": 1},
    {"latitude": 32}, {"longitude": 35}, {"latitude": 1, "longitude": 1, "altitude": 20000},
])
def test_coordinates_are_checked(client, inv, body):
    assert _call(client, inv, "POST", "system", name="x", **body).status_code == 400
    assert "x" not in _doc()["systems"]


def test_moving_and_removing_a_systems_coordinates(client, inv):
    _call(client, inv, "PUT", "system/gotcha5", latitude=32.1, longitude=35.2)
    r = _call(client, inv, "PUT", "system/gotcha5", latitude=32.2)
    assert r.json()["changed"] == ["latitude"] and _doc()["systems"]["gotcha5"]["latitude"] == 32.2
    assert _call(client, inv, "PUT", "system/gotcha5", latitude="").status_code == 400  # not alone
    r = _call(client, inv, "PUT", "system/gotcha5", latitude="", longitude="")
    assert r.status_code == 200 and "latitude" not in _doc()["systems"]["gotcha5"]



# --------------------------------------------------------------------------
# From the third review
# --------------------------------------------------------------------------

def test_a_delete_always_takes_its_own_code(client, inv):
    _call(client, inv, "PUT", "system/gotcha5", site="x")  # opens the window
    for path in ("system/gotcha6/component/2", "system/gotcha6"):
        r = client.request("DELETE", "/api/access/" + path, headers=ADMIN, json={})
        assert r.status_code == 401, path
    assert "gotcha6" in _doc()["systems"]
    assert _call(client, inv, "DELETE", "system/gotcha6").status_code == 200


def test_a_failed_change_opens_no_window(client, inv):
    assert _call(client, inv, "PUT", "system/nope", site="x").status_code == 404
    r = client.put("/api/access/system/gotcha5", headers=ADMIN, json={"site": "y"})
    assert r.status_code == 401


def test_a_code_inside_the_window_is_still_checked_and_extends_it(client, inv):
    r = _call(client, inv, "PUT", "system/gotcha5", site="a")
    first = r.json()["edit_until"]
    assert client.get("/api/auth/status", headers=ADMIN).json()["edit_until"] == first
    assert _call(client, inv, "PUT", "system/gotcha5", code="000000", site="b").status_code == 401
    inv.t += 100
    assert _call(client, inv, "PUT", "system/gotcha5", site="c").json()["edit_until"] == pytest.approx(first + 130, abs=1)


@pytest.mark.parametrize("body", [{"fov_from": 10, "fov_to": -10}, {"latitude": 32}])
def test_a_typo_found_before_the_code_leaves_the_code_usable(client, inv, body):
    code = inv.code("admin@example.com")
    path = "system/gotcha5/component/1" if "fov_from" in body else "system/gotcha5"
    assert _call(client, inv, "PUT", path, code=code, **body).status_code == 400
    assert _call(client, inv, "PUT", "system/gotcha5", code=code, site="ok").status_code == 200


def test_too_many_users_or_secret_fields_are_refused_before_the_code(client, inv):
    users = {f"u{i}": "" for i in range(21)}
    assert _call(client, inv, "PUT", "system/gotcha5/component/nuc", users=users).status_code == 400
    many = {f"s{i}": "v" for i in range(41)}
    assert _call(client, inv, "PUT", "system/gotcha5/component/nuc", secret_fields=many).status_code == 400


def test_a_shared_user_password_cant_be_changed_from_one_computer(client, inv):
    S.inventory_file.write_text(INV.replace("      nuc:\n        type: nuc\n",
        "      nuc:\n        type: nuc\n        users: [{name: root, password_env: SHARED_SSH_PW}]\n"))
    r = _call(client, inv, "PUT", "system/gotcha5/component/nuc", users={"root": "new"})
    assert r.status_code == 400 and "shared" in r.json()["detail"]


def test_one_line_user_entries_stay_one_line_when_others_change(client, inv):
    S.inventory_file.write_text(INV.replace("      nuc:\n        type: nuc\n",
        "      nuc:\n        type: nuc\n        users: [{name: op, password_env: NUC_OP_PW}, {name: root}]\n"))
    _call(client, inv, "POST", "system/gotcha5/component", name="r9", category="radars")
    _call(client, inv, "PUT", "system/gotcha5/component/nuc", users={"newbie": ""})
    assert "users: [{name: op, password_env: NUC_OP_PW}, {name: root}, {name: newbie}]" in S.inventory_file.read_text()


def test_hand_written_odd_user_entries_survive_and_duplicates_dont_become_anchors(client, inv):
    S.inventory_file.write_text(INV.replace("      nuc:\n        type: nuc\n",
        "      nuc:\n        type: nuc\n        users: [{name: dup}, {name: dup}, plain-string, {name: 7}]\n"))
    _call(client, inv, "PUT", "system/gotcha5/component/nuc", users={"dup": "pw"})
    text = S.inventory_file.read_text()
    assert "&id" not in text and "plain-string" in text and "{name: 7}" in text
    assert _call(client, inv, "PUT", "system/gotcha5", site="still editable").status_code == 200


def test_a_link_to_a_numbered_sensor_reads_as_text(client, inv):
    _call(client, inv, "POST", "system/gotcha5/component", name="pc", category="compute", sensor="1")
    assert _find(client.get("/api/access").json(), "gotcha5", "pc")["sensor"] == "1"
    S.inventory_file.write_text(S.inventory_file.read_text().replace("sensor: '1'", "sensor: 1"))
    assert _find(client.get("/api/access").json(), "gotcha5", "pc")["sensor"] == "1"


@pytest.mark.parametrize("bad", ["azimuth: .nan", "range: .inf", "elevation: -.inf"])
def test_non_finite_numbers_in_the_file_dont_break_the_listing(client, inv, bad):
    S.inventory_file.write_text(INV.replace("        port: 8080\n", f"        port: 8080\n        {bad}\n"))
    r = client.get("/api/access")
    assert r.status_code == 200 and _find(r.json(), "gotcha5", "cam")["coverage"]["range"] > 0


@pytest.mark.parametrize("url,ok", [
    ("http://10.0.0.5:8080/ui", True), ("10.0.0.5:8080", True), ("radar.local/ui", True), ("localhost:9000", True),
    ("https://[fe80::1]:443/x", True), ("javascript:1/alert(1)", False), ("data:1/x", False),
    ("http://a.b/\"><img>", False), ("vbscript:1/x", False),
])
def test_web_addresses(client, inv, url, ok):
    r = _call(client, inv, "PUT", "system/gotcha5/component/1", gui=url)
    assert (r.status_code == 200) is ok, r.text


def test_the_ssh_password_can_be_cleared_through_clear_too(client, inv):
    assert _call(client, inv, "PUT", "system/gotcha5/component/1", clear=["password"]).json()["changed"] == ["password"]
    assert "NODE_1_SSH_PW" not in _secrets()


def test_the_categories_carry_their_default_coverage(client, inv):
    cats = {c["key"]: c.get("coverage") for c in client.get("/api/access").json()["categories"]}
    assert cats == {"radars": {"from": -60, "to": 60, "range": 800}, "acoustics": {"from": -180, "to": 180, "range": 600},
                    "cameras": {"from": -30, "to": 30, "range": 500}, "compute": None}
