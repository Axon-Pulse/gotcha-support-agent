"""Operator-defined component types, per-component fields, and where things are.

The tension this code sits on: `inventory._PUBLIC_FIELDS` is an allowlist, and its whole
value is that a key nobody anticipated cannot ride along into a prompt. Operator-defined
fields are, by definition, keys nobody anticipated — and the POINT is for them to reach
the model, because an azimuth the agent cannot see is a field nobody bothered to fill in.

So they are allowlisted as ONE namespaced dict rather than by widening the list per
field, and the credential checks still run inside it. That is what these tests hold.
"""
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store  # noqa: E402
import inventory  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(inventory, "SYSTEMS", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(config_store, "PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", tmp_path / "audit.jsonl")
    srv._reload_inventory()
    yield TestClient(srv.app)
    srv._reload_inventory()


def put(client, name="tower1", **kw):
    body = {"new_name": name, "components": [], **kw}
    return client.put(f"/api/inventory/system/{name}", json=body)


RADAR = {"name": "magos9", "type": "magos_radar", "role": "sensor",
         "domain": "radar", "address": "10.0.0.9", "scheme": "tcp"}


# ---------- where things are ----------

def test_a_system_records_its_coordinates(client, tmp_path):
    assert put(client, lat=32.3891, lon=34.8703).status_code == 200
    doc = yaml.safe_load((tmp_path / "systems_inventory.yaml").read_text())
    assert doc["systems"]["tower1"]["lat"] == 32.3891
    assert doc["systems"]["tower1"]["lon"] == 34.8703


def test_half_a_coordinate_is_refused(client):
    """It points at the Atlantic, and a plausible wrong location is worse than none."""
    r = put(client, lat=32.0)
    assert r.status_code == 400 and "both lat and lon" in r.json()["detail"]
    assert put(client, lon=34.0).status_code == 400


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_a_coordinate_off_the_planet_is_refused(client, lat, lon):
    assert put(client, lat=lat, lon=lon).status_code == 400


def test_a_component_inherits_the_systems_location(client):
    """Sensors on one mast share a location; repeating it per sensor is how they drift."""
    put(client, lat=32.3891, lon=34.8703, components=[RADAR])
    rec = inventory.load()["magos9"]
    assert rec["location"] == {"lat": 32.3891, "lon": 34.8703}


def test_a_component_may_override_it(client):
    put(client, lat=32.0, lon=34.0,
        components=[{**RADAR, "lat": 31.5, "lon": 35.5}])
    assert inventory.load()["magos9"]["location"] == {"lat": 31.5, "lon": 35.5}


def test_no_coordinates_is_not_a_location(client):
    put(client, components=[RADAR])
    assert inventory.load()["magos9"]["location"] is None


def test_the_location_reaches_the_model(client):
    put(client, lat=32.3891, lon=34.8703, components=[RADAR])
    assert "32.38910,34.87030" in inventory.as_prompt()


# ---------- operator-defined fields ----------

def test_a_custom_field_is_stored_and_reaches_the_model(client, tmp_path):
    """The point of recording an azimuth is that the agent can read it."""
    put(client, components=[{**RADAR, "fields": {"azimuth": "137", "elevation": "4.5"}}])
    doc = yaml.safe_load((tmp_path / "systems_inventory.yaml").read_text())
    assert doc["systems"]["tower1"]["components"]["magos9"]["fields"] == {
        "azimuth": "137", "elevation": "4.5"}
    prompt = inventory.as_prompt()
    assert "azimuth=137" in prompt and "elevation=4.5" in prompt


@pytest.mark.parametrize("key", ["password", "token", "secret", "api_key", "credential"])
def test_a_credential_named_field_is_refused(client, key):
    """`fields` is allowlisted as a whole, so without this it is the one place a
    password could legitimately be typed into a git-tracked file."""
    r = put(client, components=[{**RADAR, "fields": {key: "hunter2"}}])
    assert r.status_code == 400
    assert "credential name" in r.json()["detail"]


def test_a_nested_value_is_refused(client):
    """as_prompt() renders these into the system prompt; a dict becomes str() noise
    the model reads as fact."""
    for bad in ({"a": 1}, [1, 2]):
        r = put(client, components=[{**RADAR, "fields": {"extra": bad}}])
        assert r.status_code == 400 and "single value" in r.json()["detail"]


def test_a_badly_named_field_is_refused(client):
    r = put(client, components=[{**RADAR, "fields": {"Az imuth!": "1"}}])
    assert r.status_code == 400 and "lowercase" in r.json()["detail"]


def test_an_empty_field_is_dropped_rather_than_stored(client, tmp_path):
    put(client, components=[{**RADAR, "fields": {"azimuth": "", "elevation": None}}])
    doc = yaml.safe_load((tmp_path / "systems_inventory.yaml").read_text())
    assert "fields" not in doc["systems"]["tower1"]["components"]["magos9"]


def test_the_registry_still_refuses_an_inline_secret_anywhere(client, tmp_path):
    """The guarantee that predates all of this must still hold."""
    p = tmp_path / "systems_inventory.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "systems": {"t": {
        "components": {"n": {"type": "x", "fields": {"azimuth": "1"},
                             "password": "hunter2"}}}}}))
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    with pytest.raises(inventory.InventoryError):
        inventory.load()


# ---------- the type catalogue ----------

def test_the_built_in_types_are_served(client):
    got = client.get("/api/inventory/types").json()
    assert {"radar", "camera", "acoustic", "computers", "other"} <= set(got["types"])
    assert got["overridden"] is False


def test_the_default_field_names_are_the_systems_own():
    """Not invented ones.

    kb/system-model.md: alignment is written "into each sensor's own yaw-role key —
    `azimuth_offset` for radar, `yaw` for the ASU". A registry field called `azimuth`
    where the config says `azimuth_offset` is the two-vocabularies trap graph.DOMAINS
    warns about: it reads as a match and is not one. Pinned against the document so a
    rename there fails here rather than drifting quietly.
    """
    import console.server as srv
    model = (ROOT / "kb" / "system-model.md").read_text()
    keys = {t: [f["key"] for f in spec["fields"]]
            for t, spec in srv.DEFAULT_TYPES.items()}
    assert "azimuth_offset" in keys["radar"], "radar uses the config's own yaw key"
    assert "yaw" in keys["acoustic"], "the ASU's yaw-role key"
    for named in ("azimuth_offset", "yaw"):
        assert named in model, f"{named} is no longer the name the system model uses"
    assert keys["other"] == [], "the catch-all invents fields nobody asked for"


def test_the_acoustic_default_points_at_the_container(client):
    """The ASU is a local Docker service, so `asu_connected: false` is about a
    container — and which one is the thing a probe cannot tell you."""
    import console.server as srv
    fields = {f["key"]: f["default"] for f in srv.DEFAULT_TYPES["acoustic"]["fields"]}
    assert fields.get("container"), "no container to look up when the API goes quiet"


def test_a_type_says_which_standard_fields_it_has(client):
    """The two editors have to agree: a type describes the WHOLE component, not just
    the extras bolted onto it."""
    import console.server as srv
    got = client.get("/api/inventory/types").json()
    assert got["builtin_fields"], "the editor has no list of standard fields to offer"
    assert {"address", "port", "ssh", "location"} <= {
        f["key"] for f in got["builtin_fields"]}
    assert set(got["always_shown"]) == {"name", "type"}, \
        "the logical name and the hardware type are not optional"
    # A camera is reached over its own HTTP API, and the ASU is a local Docker service.
    assert got["types"]["camera"]["builtin"]["ssh"] is False
    assert got["types"]["acoustic"]["builtin"]["address"] is False
    assert got["types"]["radar"]["builtin"]["address"] is True


def test_hiding_a_field_does_not_delete_what_is_stored(client, tmp_path):
    """Hiding is presentational. A value somebody typed before the field was turned off
    must survive, or the catalogue becomes a way to silently lose the registry."""
    put(client, components=[{**RADAR, "web": "http://10.0.0.9:5173",
                             "hardware": "AR-300"}])
    client.put("/api/inventory/types", json={"types": {"radar": {
        "label": "Radar", "role": "sensor", "domain": "radar",
        "builtin": {"web": False, "hardware": False}, "fields": []}}})
    row = [c for c in client.get("/api/inventory").json()["systems"][0]["components"]
           if c["name"] == "magos9"][0]
    assert row["web"] == "http://10.0.0.9:5173", "a hidden field was dropped from storage"
    assert row["hardware"] == "AR-300"
    assert "AR-300" in inventory.as_prompt(), "a hidden field stopped reaching the model"


def test_an_unknown_builtin_key_is_dropped_not_rejected(client):
    """A catalogue saved against a different build should still load."""
    r = client.put("/api/inventory/types", json={"types": {"r": {
        "label": "R", "role": "sensor",
        "builtin": {"address": False, "telepathy": True}, "fields": []}}})
    assert r.status_code == 200
    b = r.json()["types"]["r"]["builtin"]
    assert b["address"] is False and "telepathy" not in b


def test_a_type_with_no_builtin_block_shows_everything(client):
    """An older catalogue must not render components with no fields at all."""
    r = client.put("/api/inventory/types", json={"types": {
        "r": {"label": "R", "role": "sensor", "fields": []}}})
    assert all(r.json()["types"]["r"]["builtin"].values())


def test_a_new_type_is_saved_and_audited(client, tmp_path):
    types = {"weather": {"label": "Weather", "role": "other", "domain": "",
                         "scheme": "http",
                         "fields": [{"key": "wind_sensor", "label": "Wind", "default": "gill"}]}}
    r = client.put("/api/inventory/types", json={"types": types})
    assert r.status_code == 200
    got = r.json()
    assert got["overridden"] is True
    assert got["types"]["weather"]["fields"][0]["default"] == "gill"
    assert "component types" in (tmp_path / "audit.jsonl").read_text()


def test_the_global_reset_is_the_only_way_back(client):
    """There is deliberately no type-only revert: "Revert everything to code defaults"
    on the permissions page already clears every override, this one included."""
    client.put("/api/inventory/types", json={"types": {
        "only": {"label": "Only", "role": "other", "fields": []}}})
    assert client.get("/api/inventory/types").json()["overridden"] is True
    assert client.delete("/api/inventory/types").status_code == 405, \
        "a type-only reset is back; one way home was the decision"

    client.post("/api/config/reset")
    got = client.get("/api/inventory/types").json()
    assert got["overridden"] is False
    assert "radar" in got["types"]


@pytest.mark.parametrize("types,msg", [
    ({}, "at least one type"),
    ({"Radar!": {"role": "sensor"}}, "lowercase"),
    ({"r": {"role": "wizard"}}, "role"),
    ({"r": {"role": "sensor", "fields": [{"key": "password"}]}}, "credential name"),
    ({"r": {"role": "sensor", "fields": [{"key": "Az!"}]}}, "lowercase"),
    ({"r": {"role": "sensor", "fields": [{"key": "az"}, {"key": "az"}]}}, "twice"),
    ({"r": {"role": "sensor", "fields": [{"key": "az", "default": {"a": 1}}]}},
     "single value"),
])
def test_a_malformed_catalogue_is_refused(client, types, msg):
    r = client.put("/api/inventory/types", json={"types": types})
    assert r.status_code == 400, f"accepted {types}"
    assert msg in r.json()["detail"]


def test_a_refused_catalogue_changes_nothing(client):
    before = client.get("/api/inventory/types").json()["types"]
    client.put("/api/inventory/types", json={"types": {"r": {"role": "wizard"}}})
    assert client.get("/api/inventory/types").json()["types"] == before
