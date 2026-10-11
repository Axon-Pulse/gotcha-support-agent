"""The console's inventory editor: it may store a credential, never publish one."""
import stat
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inventory  # noqa: E402
import secrets_store  # noqa: E402

PW = "correct-horse-battery-staple"
SYS = {
    "site": "beit-yanai", "description": "Shared tower",
    "sensors": [{
        "name": "magos", "type": "magos_radar", "domain": "radar",
        "address": "192.168.40.60", "port": 8080, "scheme": "tcp",
        "hardware": "Magos AR-300", "software_version": "2.4.1",
        "ssh": {"user": "magos", "port": 22, "key_file": "~/.ssh/k", "password": PW},
    }],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(inventory, "SYSTEMS", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(secrets_store, "PATH", tmp_path / "secrets.local.env")
    srv._reload_inventory()
    yield TestClient(srv.app)
    srv._reload_inventory()


def _put(client, name="tower1", payload=None):
    r = client.put(f"/api/inventory/system/{name}", json=payload or SYS)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1. the split: registry holds the name, the store holds the value ----------

def test_password_never_lands_in_the_registry_file(client, tmp_path):
    _put(client)
    raw = (tmp_path / "systems_inventory.yaml").read_text()
    assert PW not in raw, "the registry must stay safe to commit"
    assert "password_env: MAGOS_SSH_PW" in raw, "only the variable NAME is recorded"
    assert "password:" not in raw
    assert PW in (tmp_path / "secrets.local.env").read_text()


def test_the_registry_it_writes_is_one_inventory_can_load(client, tmp_path):
    _put(client)
    doc = yaml.safe_load((tmp_path / "systems_inventory.yaml").read_text())
    assert doc["systems"]["tower1"]["components"]["magos"]["type"] == "magos_radar"
    assert doc["systems"]["tower1"]["components"]["magos"]["role"] == "sensor"
    assert inventory.require("magos")["hardware"] == "Magos AR-300"
    assert inventory.credentials("magos")["password"].reveal() == PW


def test_the_secret_file_is_owner_only(client, tmp_path):
    _put(client)
    mode = stat.S_IMODE((tmp_path / "secrets.local.env").stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, f"mode is {oct(mode)}"


def test_a_password_env_name_is_derived_when_the_admin_gives_none(client):
    got = _put(client)
    ssh = got["systems"][0]["sensors"][0]["ssh"]
    assert ssh["password_env"] == "MAGOS_SSH_PW"


# ---------- 2. the admin sees it; the model never does ----------

def test_the_admin_can_read_the_password_back(client):
    got = client.get("/api/inventory").json() if _put(client) else None
    assert got["systems"][0]["sensors"][0]["ssh"]["password"] == PW


def test_the_agent_view_carries_no_credential_material(client):
    got = _put(client)
    blob = str(got["agent_view"]) + got["agent_prompt"]
    for forbidden in (PW, "password", "MAGOS_SSH_PW", "key_file", ".ssh/k", "access"):
        assert forbidden not in blob, f"{forbidden!r} reached the agent-facing view"
    assert "magos" in blob and "Magos AR-300" in blob, "useful context still gets through"


def test_agent_view_is_exactly_what_inventory_exposes(client):
    got = _put(client)
    assert {r["name"] for r in got["agent_view"]} == set(inventory.names())
    for r in got["agent_view"]:
        assert set(r) == set(inventory._PUBLIC_FIELDS)


# ---------- 3. a rejected edit changes nothing ----------

def test_an_edit_that_would_not_load_is_rolled_back(client, tmp_path):
    _put(client)
    before = (tmp_path / "systems_inventory.yaml").read_text()
    bad = {"sensors": [{"name": "cam", "type": "optic", "address": "not-an-ip"}]}
    r = client.put("/api/inventory/system/other", json=bad)
    assert r.status_code == 400 and "nothing was changed" in r.text
    assert (tmp_path / "systems_inventory.yaml").read_text() == before
    assert inventory.names() == sorted(inventory.names()), "inventory still loads"


def test_a_duplicate_sensor_name_is_refused_and_rolled_back(client, tmp_path):
    _put(client)
    before = (tmp_path / "systems_inventory.yaml").read_text()
    clash = {"sensors": [{"name": "magos", "type": "magos_radar",
                          "address": "192.168.40.61"}]}
    r = client.put("/api/inventory/system/tower2", json=clash)
    assert r.status_code == 400 and "defined twice" in r.text
    assert (tmp_path / "systems_inventory.yaml").read_text() == before


@pytest.mark.parametrize("name", ["../etc", "UPPER", "with space", "", "a" * 80])
def test_bad_system_names_are_refused(client, name):
    assert client.put(f"/api/inventory/system/{name}", json=SYS).status_code in (400, 404, 405)


def test_a_sensor_needs_a_hardware_type(client):
    r = client.put("/api/inventory/system/t", json={"sensors": [{"name": "x", "type": " "}]})
    assert r.status_code == 400 and "hardware type" in r.text


# ---------- 4. deleting ----------

def test_deleting_a_system_reports_the_secret_it_orphans(client, tmp_path):
    _put(client)
    r = client.delete("/api/inventory/system/tower1").json()
    assert r["orphaned_secrets"] == ["MAGOS_SSH_PW"]
    assert PW in (tmp_path / "secrets.local.env").read_text(), \
        "deleting a system must not silently destroy a credential"
    assert r["systems"] == []


def test_clearing_a_password_removes_it_from_the_store(client, tmp_path):
    _put(client)
    payload = {**SYS, "sensors": [{**SYS["sensors"][0],
                                   "ssh": {"user": "magos", "password_env": "MAGOS_SSH_PW",
                                           "clear_password": True}}]}
    _put(client, payload=payload)
    assert PW not in (tmp_path / "secrets.local.env").read_text()


def test_a_secret_from_the_process_environment_cannot_be_deleted(client, monkeypatch):
    monkeypatch.setenv("MAGOS_SSH_PW", "from-env")
    r = client.delete("/api/inventory/secret/MAGOS_SSH_PW")
    assert r.status_code == 400 and "process environment" in r.text


def test_environment_wins_over_the_stored_file(client, monkeypatch):
    _put(client)
    monkeypatch.setenv("MAGOS_SSH_PW", "from-env")
    assert inventory.credentials("magos")["password"].reveal() == "from-env"


# ---------- 5. the supervisor is visible but not editable ----------

def test_supervisor_is_exposed_read_only(client):
    sup = client.get("/api/config").json()["supervisor"]
    assert sup["name"] == "supervisor" and sup["read_only"] is True
    assert "supervisor" not in client.get("/api/config").json()["effective"]["agents"]
    assert client.put("/api/config/agent/supervisor",
                      json={"prompt": "x", "tools": []}).status_code == 400


def test_mermaid_export_is_gone(client):
    assert "mermaid" not in client.get("/api/graph").json()


def test_addressable_nodes_block_is_gone_from_meta(client):
    assert "addressable_nodes" not in client.get("/api/meta").json()["permissions"]


# ---------- 6. the widened hardware model ----------

MIXED = {"site": "beit-yanai", "description": "Edge rack", "components": [
    {"name": "cam1", "type": "meduza_optic", "role": "sensor", "domain": "camera",
     "address": "192.168.40.71", "port": 80, "scheme": "http",
     "hardware": "Meduza EO/IR", "software_version": "5.7.3"},
    {"name": "edge1", "type": "compute_box", "role": "compute",
     "address": "192.168.40.10", "hardware": "Advantech ARK", "software_version": "22.04",
     "ssh": {"user": "ops", "key_file": "~/.ssh/ops"}},
    {"name": "sw1", "type": "poe_switch", "role": "network", "address": "192.168.40.2",
     "hardware": "Netgear GS308P"},
    {"name": "field1", "type": "rugged_laptop", "role": "laptop",
     "address": "192.168.40.120", "software_version": "Win11 23H2"},
]}


def test_a_system_can_hold_more_than_sensors(client, tmp_path):
    r = _put(client, "rack1", MIXED)
    roles = {c["name"]: c["role"] for c in r["systems"][0]["components"]}
    assert roles == {"cam1": "sensor", "edge1": "compute", "sw1": "network",
                     "field1": "laptop"}
    assert set(inventory.names()) >= {"cam1", "edge1", "sw1", "field1"}


def test_non_sensor_components_are_addressable_like_any_other(client):
    _put(client, "rack1", MIXED)
    assert inventory.require("sw1")["role"] == "network"
    assert inventory.credentials("edge1")["user"] == "ops"
    assert "sw1" in inventory.as_prompt()


def test_a_camera_component_lands_in_the_camera_domain(client):
    import graph as G
    _put(client, "rack1", MIXED)
    assert inventory.require("cam1")["domain"] == "camera"
    ctx = G._extract_context([{"agent": "triage", "tool": "get_system_health", "ok": True,
                               "data": {"nodes": [{"node": "cam1", "status": "CRITICAL"}]}}])
    assert ctx["camera_targets"] == ["cam1"]


def test_an_unknown_role_is_refused(client):
    r = client.put("/api/inventory/system/x", json={"components": [
        {"name": "a", "type": "t", "role": "toaster"}]})
    assert r.status_code == 400 and "allowed" in r.text


def test_legacy_sensors_key_still_loads_and_defaults_to_sensor(client, tmp_path):
    (tmp_path / "systems_inventory.yaml").write_text(
        "version: 1\nsystems:\n  old:\n    sensors:\n      magos:\n"
        "        type: magos_radar\n        address: 192.168.40.60\n")
    import console.server as srv
    srv._reload_inventory()
    assert inventory.require("magos")["role"] == "sensor"
    assert client.get("/api/inventory").json()["systems"][0]["components"][0]["role"] == "sensor"


def test_the_editor_offers_roles_and_a_type_catalog(client):
    r = client.get("/api/inventory").json()
    assert r["roles"] == list(inventory.ROLES)
    assert "camera" in r["domains"]
    assert "meduza_optic" in r["type_catalog"]["sensor"]
    assert "poe_switch" in r["type_catalog"]["network"]


# ---------- 7. an access block must be usable, not merely parseable ----------

@pytest.mark.parametrize("ssh,msg", [
    ({"port": -1}, "out of range"),
    ({"port": 70000}, "out of range"),
    ({"user": "a b", "key_file": "~/.ssh/k"}, "not a valid username"),
    ({"key_file": "~/.ssh/k"}, "give an SSH user"),
    ({"password": "x"}, "give an SSH user"),
])
def test_an_unusable_access_block_is_refused(client, ssh, msg):
    """credentials() raises on these at run time; catch them at the keystroke instead."""
    r = client.put("/api/inventory/system/t", json={"components": [
        {"name": "c1", "type": "x", "role": "other", "address": "10.0.0.1", "ssh": ssh}]})
    assert r.status_code == 400 and msg in r.text


def test_a_port_with_nobody_to_log_in_as_is_dropped_not_stored(client, tmp_path):
    """Storing it produces a block credentials() can never use."""
    _put(client, "t", {"components": [
        {"name": "c1", "type": "x", "role": "other", "address": "10.0.0.1",
         "ssh": {"port": 22}}]})
    assert "access" not in (tmp_path / "systems_inventory.yaml").read_text()
    assert not inventory.has_credentials("c1")


def test_a_pre_existing_bad_block_does_not_veto_an_unrelated_edit(client, tmp_path):
    """Old half-filled data must not make the whole registry read-only."""
    (tmp_path / "systems_inventory.yaml").write_text(
        "version: 1\nsystems:\n  old:\n    components:\n      broken:\n"
        "        type: x\n        address: 10.0.0.9\n"
        "        access:\n          ssh:\n            port: -1\n")
    import console.server as srv
    srv._reload_inventory()
    r = client.put("/api/inventory/system/fresh", json={"components": [
        {"name": "good", "type": "y", "role": "compute", "address": "10.0.0.2",
         "ssh": {"user": "ops", "key_file": "~/.ssh/k"}}]})
    assert r.status_code == 200, r.text
    assert "good" in inventory.names() and "broken" in inventory.names()


# ---------- 8. renaming a system ----------

def test_a_system_can_be_renamed(client, tmp_path):
    """The URL addresses what exists; new_name carries the change."""
    _put(client)
    r = client.put("/api/inventory/system/tower1",
                   json={**SYS, "new_name": "mast-north"}).json()
    assert r["name"] == "mast-north", "the caller needs the new name to re-open the row"
    assert [s["name"] for s in r["systems"]] == ["mast-north"]
    assert "tower1:" not in (tmp_path / "systems_inventory.yaml").read_text()


def test_renaming_keeps_the_system_in_place(client):
    """Otherwise a rename jumps to the end of the file and the diff is unreadable."""
    _put(client, "alpha")
    _put(client, "beta", {"sensors": [{"name": "b1", "type": "x", "address": "10.0.0.2"}]})
    _put(client, "gamma", {"sensors": [{"name": "g1", "type": "x", "address": "10.0.0.3"}]})
    r = client.put("/api/inventory/system/beta", json={
        "new_name": "bravo",
        "sensors": [{"name": "b1", "type": "x", "address": "10.0.0.2"}]}).json()
    assert [s["name"] for s in r["systems"]] == ["alpha", "bravo", "gamma"]


def test_renaming_does_not_orphan_a_stored_secret(client, tmp_path):
    """password_env is derived from the COMPONENT name, which a rename does not touch."""
    _put(client)
    assert "MAGOS_SSH_PW" in (tmp_path / "secrets.local.env").read_text()
    client.put("/api/inventory/system/tower1", json={
        "new_name": "mast-north",
        "sensors": [{**SYS["sensors"][0],
                     "ssh": {"user": "magos", "password_env": "MAGOS_SSH_PW"}}]})
    assert "MAGOS_SSH_PW" in (tmp_path / "secrets.local.env").read_text()
    assert inventory.credentials("magos")["password"].reveal() == PW


def test_renaming_onto_an_existing_name_is_refused(client, tmp_path):
    _put(client, "tower1")
    _put(client, "tower2", {"sensors": [{"name": "m2", "type": "x", "address": "10.0.0.9"}]})
    before = (tmp_path / "systems_inventory.yaml").read_text()
    r = client.put("/api/inventory/system/tower2",
                   json={"new_name": "tower1", "sensors": []})
    assert r.status_code == 409 and "already exists" in r.text
    assert (tmp_path / "systems_inventory.yaml").read_text() == before


@pytest.mark.parametrize("bad", ["Mast North", "", "../etc", "a" * 80, "-x"])
def test_renaming_to_a_bad_name_is_refused(client, bad):
    _put(client)
    assert client.put("/api/inventory/system/tower1",
                      json={**SYS, "new_name": bad}).status_code == 400
    assert "tower1" in [s["name"] for s in client.get("/api/inventory").json()["systems"]]


def test_omitting_new_name_is_a_plain_update(client):
    _put(client)
    r = client.put("/api/inventory/system/tower1",
                   json={**SYS, "site": "elsewhere"}).json()
    assert r["name"] == "tower1" and r["systems"][0]["site"] == "elsewhere"


# ---------- 9. the simplified component form ----------

def test_a_web_address_is_stored_and_reaches_the_agent(client, tmp_path):
    """Not a credential — it is where a human goes to look, and worth the agent knowing."""
    _put(client, "t", {"components": [
        {"name": "1", "type": "meduza_optic", "role": "sensor", "domain": "camera",
         "address": "192.168.40.71", "web": "http://192.168.40.71:5173"}]})
    assert "web: http://192.168.40.71:5173" in (tmp_path / "systems_inventory.yaml").read_text()
    assert inventory.require("1")["web"] == "http://192.168.40.71:5173"
    assert client.get("/api/inventory").json()["systems"][0]["components"][0]["web"] \
        == "http://192.168.40.71:5173"


def test_a_username_is_still_an_ssh_user_underneath(client, tmp_path):
    """The label changed; the storage did not, so credentials() and run_on keep working."""
    _put(client, "t", {"components": [
        {"name": "1", "type": "x", "role": "compute", "address": "10.0.0.5",
         "ssh": {"user": "ops", "password": "pw"}}]})
    assert "user: ops" in (tmp_path / "systems_inventory.yaml").read_text()
    assert inventory.credentials("1")["user"] == "ops"
    assert inventory.credentials("1")["port"] == 22, "the default still applies"


def test_a_component_with_no_scheme_defaults_to_tcp(client):
    """Scheme is no longer typed by hand; the section sets it or the default applies."""
    _put(client, "t", {"components": [
        {"name": "1", "type": "x", "role": "other", "address": "10.0.0.5"}]})
    assert inventory.require("1")["endpoint"]["scheme"] == "tcp"


def test_numeric_component_names_are_accepted(client):
    """The form numbers them 1, 2, 3 — the slug rules must allow that."""
    r = _put(client, "t", {"components": [
        {"name": "1", "type": "x", "role": "other", "address": "10.0.0.1"},
        {"name": "2", "type": "x", "role": "other", "address": "10.0.0.2"}]})
    assert [c["name"] for c in r["systems"][0]["components"]] == ["1", "2"]


def test_a_number_reused_across_systems_is_still_refused(client):
    """Which is why the form numbers from the whole inventory, not per system."""
    _put(client, "t1", {"components": [
        {"name": "1", "type": "x", "role": "other", "address": "10.0.0.1"}]})
    r = client.put("/api/inventory/system/t2", json={"components": [
        {"name": "1", "type": "x", "role": "other", "address": "10.0.0.2"}]})
    assert r.status_code == 400 and "defined twice" in r.text


# ---------- 10. a save must not be blocked or lost by what the form cannot see ----------

BAD_PORT = ("version: 1\nsystems:\n  t1:\n    components:\n"
            "      a:\n        type: x\n        address: 10.0.0.1\n"
            "      b:\n        type: y\n        address: 10.0.0.2\n"
            "        access:\n          ssh:\n            port: -1\n")


def test_a_bad_value_the_form_cannot_show_does_not_block_a_save(client, tmp_path):
    """The reported bug: editing component `a` failed because `b` carried an SSH port
    of -1 — a field the editor no longer has, so it could not be seen or fixed."""
    (tmp_path / "systems_inventory.yaml").write_text(BAD_PORT)
    import console.server as srv
    srv._reload_inventory()
    r = client.put("/api/inventory/system/t1", json={"components": [
        {"name": "a", "type": "x", "role": "other", "address": "10.0.0.1",
         "hardware": "Magos AR-300"},
        {"name": "b", "type": "y", "role": "other", "address": "10.0.0.2"}]})
    assert r.status_code == 200, r.text
    comps = {c["name"]: c for c in r.json()["systems"][0]["components"]}
    assert comps["a"]["hardware"] == "Magos AR-300", "the edit must actually land"


def test_ssh_settings_the_form_does_not_edit_survive_a_save(client, tmp_path):
    """Port and key file are no longer fields; a save must carry them forward."""
    (tmp_path / "systems_inventory.yaml").write_text(
        "version: 1\nsystems:\n  t1:\n    components:\n      a:\n        type: x\n"
        "        address: 10.0.0.1\n        access:\n          ssh:\n"
        "            user: ops\n            port: 2222\n            key_file: ~/.ssh/k\n")
    import console.server as srv
    srv._reload_inventory()
    client.put("/api/inventory/system/t1", json={"components": [
        {"name": "a", "type": "x", "role": "other", "address": "10.0.0.1",
         "hardware": "new model", "ssh": {"user": "ops"}}]})
    creds = inventory.credentials("a")
    assert creds["port"] == 2222, "the SSH port was not carried forward"
    assert creds["key_file"].endswith(".ssh/k"), "the key file was not carried forward"
    assert inventory.require("a")["hardware"] == "new model"


def test_clearing_the_username_still_clears_it(client, tmp_path):
    """Carrying values forward must not make a field impossible to empty."""
    _put(client, "t1", {"components": [
        {"name": "a", "type": "x", "role": "other", "address": "10.0.0.1",
         "ssh": {"user": "ops", "key_file": "~/.ssh/k"}}]})
    assert inventory.credentials("a")["user"] == "ops"
    client.put("/api/inventory/system/t1", json={"components": [
        {"name": "a", "type": "x", "role": "other", "address": "10.0.0.1"}]})
    assert not inventory.has_credentials("a")
