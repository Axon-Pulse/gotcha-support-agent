"""The Trace tab's system filter follows the inventory, with no restart."""
import pytest
from fastapi.testclient import TestClient

import inventory


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(inventory, "SYSTEMS", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(srv, "TRACES", tmp_path / "traces")
    srv._reload_inventory()
    yield TestClient(srv.app)
    srv._reload_inventory()


def _systems(c):
    return c.get("/api/traces").json()["systems"]


def test_systems_added_and_removed_in_the_console_follow(client):
    assert _systems(client) == []
    assert client.put("/api/inventory/system/gotcha9", json={"site": "north"}).status_code == 200
    assert client.put("/api/inventory/system/gotcha1", json={}).status_code == 200
    assert _systems(client) == ["gotcha1", "gotcha9"]
    assert client.delete("/api/inventory/system/gotcha9").status_code == 200
    assert _systems(client) == ["gotcha1"]


def test_a_hand_edit_to_the_file_is_picked_up(client):
    inventory.SYSTEMS.write_text("version: 1\nsystems:\n  gotcha7: {components: {}}\n")
    assert _systems(client) == ["gotcha7"]


def test_a_broken_inventory_still_lists_the_traces(client):
    inventory.SYSTEMS.write_text("systems: [unclosed\n")
    r = client.get("/api/traces")
    assert r.status_code == 200 and r.json()["systems"] == []


def _lines(tmp_path, sid):
    import json
    return [json.loads(l) for l in (tmp_path / "traces" / f"{sid}.jsonl").read_text().splitlines()]


def test_a_failed_console_run_leaves_a_trace(client, tmp_path, monkeypatch):
    import console.server as srv

    def boom():
        raise RuntimeError("graph would not build")
    monkeypatch.setattr(srv, "graph", boom)
    monkeypatch.setattr(srv, "DB", str(tmp_path / "graph.db"))
    srv._run("f1", {"question": "no tracks"})
    lines = _lines(tmp_path, "f1")
    assert [l["status"] for l in lines] == ["running", "error"]
    assert lines[0]["question"] == "no tracks" and "graph would not build" in lines[1]["error"]
    row = client.get("/api/traces").json()["traces"][0]
    assert row["finished"] is False and row["status_label"] == "failed"


def test_a_session_in_several_lines_is_one_session_with_its_tokens_summed(client, tmp_path):
    import json
    d = tmp_path / "traces"
    d.mkdir(exist_ok=True)
    (d / "s.jsonl").write_text("\n".join(json.dumps(l) for l in [
        {"status": "running", "question": "q"},
        {"status": "awaiting_approval", "usage": {"input": 100, "output": 10}},
        {"status": "done", "usage": {"input": 5, "output": 1}}]) + "\n")
    got = client.get("/api/usage").json()
    assert got["sessions"] == 1
    assert got["lifetime"] == {"input": 105, "output": 11, "cache_read": 0}
    row = client.get("/api/traces").json()["traces"][0]
    assert row["tokens"] == 116 and row["finished"]
