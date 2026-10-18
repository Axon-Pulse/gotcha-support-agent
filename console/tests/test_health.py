"""A site's live health and speed tests: the tailnet ping read, the site's answers parsed, one
check of a site at a time, and the routes' rules."""
import json
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from console import health
from console.settings import S

SUMMARY = {"overall_system_status": "LIVE", "total_nodes": 11, "healthy_nodes": 11, "degraded_nodes": 0,
           "critical_nodes": 0, "offline_nodes": 0, "release_version": "v1.5.1"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(health, "_checks", {})
    monkeypatch.setattr(health, "_check_locks", {})
    monkeypatch.setattr(health, "_speed_busy", set())


def _tailscale(monkeypatch, stdout, code=0):
    def run(argv, **kw):
        assert argv[:2] == ["tailscale", "ping"]
        return subprocess.CompletedProcess(argv, code, stdout, "")
    monkeypatch.setattr(health.subprocess, "run", run)


@pytest.mark.parametrize("out,code,want", [
    ("pong from axon-gotcha-2 (100.1.1.1) via 1.2.3.4:41641 in 23ms\n", 0, {"ok": True, "ms": 23.0, "via": "direct"}),
    # A relayed pong, then exit 1 because no direct link came up: still an answer.
    ("pong from axon-gotcha-2 (100.1.1.1) via DERP(fra) in 253ms\ndirect connection not established\n", 1,
     {"ok": True, "ms": 253.0, "via": "relayed (fra)"}),
    ("pong from axon-gotcha-2 (100.1.1.1) via peer-relay(100.2.2.2:7777:vni:5) in 40ms\n", 0,
     {"ok": True, "ms": 40.0, "via": "via peer-relay(100.2.2.2:7777:vni:5)"}),
    ("ping \"100.1.1.1\" timed out\nno reply\n", 1, {"ok": False, "error": "ping \"100.1.1.1\" timed out\nno reply"}),
])
def test_ping_reads_the_answer_whatever_the_exit_code(monkeypatch, out, code, want):
    _tailscale(monkeypatch, out, code)
    assert health.ping("axon-gotcha-2") == want


def test_a_check_reads_the_site_once_a_minute(env, monkeypatch):
    monkeypatch.setattr(health, "ping", lambda h: {"ok": True, "ms": 20.0, "via": "direct"})
    calls = []

    def run(argv, timeout, data=None):
        calls.append(argv)
        assert data == health.HEALTH_PY.encode() and argv[-1] == "python3 -"
        return json.dumps({"summary": SUMMARY, "problems": []})
    monkeypatch.setattr(health, "_run", run)
    first = health.check("axon-gotcha-2", "gotcha")
    assert first["summary"] == SUMMARY and calls[0][-2] == "gotcha@axon-gotcha-2"
    assert health.check("axon-gotcha-2", "gotcha") is first and len(calls) == 1
    monkeypatch.setattr(health.time, "time", lambda: first["at"] + health.CHECK_FRESH_S + 1)
    health.check("axon-gotcha-2", "gotcha")
    assert len(calls) == 2
    monkeypatch.setattr(health.time, "time", lambda: first["at"] + health.CHECK_FRESH_S + 2)
    health.check("axon-gotcha-2", "gotcha", fresh=True)   # "check again", within the minute
    assert len(calls) == 3


def test_a_second_check_waits_for_the_first_and_takes_its_answer(monkeypatch):
    monkeypatch.setattr(health, "ping", lambda h: {"ok": True, "ms": 20.0, "via": "direct"})
    calls = []

    def run(*a, **k):
        calls.append(1)
        time.sleep(0.3)
        return json.dumps({"summary": {"total_nodes": 1}, "problems": []})
    monkeypatch.setattr(health, "_run", run)
    got = []
    threads = [threading.Thread(target=lambda: got.append(health.check("axon-gotcha-2", None, fresh=True)))
               for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and len(got) == 3 and got[0] is got[1] is got[2]


def test_an_unreachable_site_is_not_connected_to(monkeypatch):
    monkeypatch.setattr(health, "ping", lambda h: {"ok": False, "error": "no reply"})
    monkeypatch.setattr(health, "_run", lambda *a, **k: pytest.fail("connected to an unreachable site"))
    assert health.check("axon-gotcha-5", None) == {"host": "axon-gotcha-5", "at": pytest.approx(time.time(), abs=5),
                                                  "ping": {"ok": False, "error": "no reply"}}


def test_a_site_whose_health_cant_be_read_says_why(monkeypatch):
    monkeypatch.setattr(health, "ping", lambda h: {"ok": True, "ms": 20.0, "via": "direct"})

    def run(*a, **k):
        raise health.SiteError("the gateway refused the connection")
    monkeypatch.setattr(health, "_run", run)
    assert health.check("axon-gotcha-2", None)["error"] == \
        "the site's health couldn't be read: the gateway refused the connection"


def test_one_check_of_a_site_at_a_time(monkeypatch):
    health._speed_busy.add("axon-gotcha-2")
    with pytest.raises(health.SiteBusy):
        health.speed("axon-gotcha-2", None, "internet")
    # Another site is free.
    monkeypatch.setattr(health, "_run", lambda *a, **k: "8000000 4.5 0.5 0.12 2000000 4.2 0.2")
    assert health.speed("axon-gotcha-4", None, "internet")["download_mbps"] == 16.0
    assert "axon-gotcha-4" not in health._speed_busy


def test_the_sites_internet_speed(monkeypatch):
    monkeypatch.setattr(health, "_run", lambda argv, timeout: "8000000 4.5 0.5 0.12\n2000000 4.2 0.2\n")
    assert health.speed("axon-gotcha-2", None, "internet") == {
        "kind": "internet", "download_mbps": 16.0, "upload_mbps": 4.0, "connect_ms": 120}
    monkeypatch.setattr(health, "_run", lambda argv, timeout: "")
    with pytest.raises(health.SiteError, match="no result"):
        health.speed("axon-gotcha-2", None, "internet")
    monkeypatch.setattr(health, "_run", lambda argv, timeout: "0 0.1 0 0\n0 0.1 0\n")   # no internet
    with pytest.raises(health.SiteError, match="couldn't reach the speed test"):
        health.speed("axon-gotcha-2", None, "internet")


def test_the_link_speed_both_ways(monkeypatch):
    # The site is this machine: the same scripts, run here, for one second each way.
    monkeypatch.setattr(health, "SPEED_S", 1)
    monkeypatch.setattr(health, "SEND_PY", health.SEND_PY.replace("time.time() + 5", "time.time() + 1"))
    monkeypatch.setattr(health, "_ssh", lambda host, user, remote: ["bash", "-c", remote])
    monkeypatch.setattr(health, "ping", lambda h: {"ok": True, "ms": 1.0, "via": "direct"})
    r = health.speed("axon-gotcha-2", None, "link")
    assert r["kind"] == "link" and r["download_mbps"] > 10 and r["upload_mbps"] > 10, r


def test_a_link_test_that_cant_connect_says_why(monkeypatch):
    monkeypatch.setattr(health, "_ssh", lambda host, user, remote: ["bash", "-c", "echo 'Permission denied' >&2; exit 255"])
    monkeypatch.setattr(health, "ping", lambda h: {"ok": True, "ms": 1.0, "via": "direct"})
    t = time.time()
    with pytest.raises(health.SiteError, match="Permission denied"):
        health.speed("axon-gotcha-2", None, "link")
    assert time.time() - t < 5


def _fake_gateway(summary):
    """A gateway on a free port: the websocket sends a ping, then the summary in two fragments;
    the events endpoint answers one open health event."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)

    def serve():
        ws, _ = srv.accept()
        f = ws.makefile("rb")
        while f.readline() not in (b"\r\n", b""):
            pass
        ws.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        b1, b2 = f.read(2)
        assert b1 == 0x81 and b2 & 0x80 and (b2 & 0x7F) < 126   # one masked frame, short length form
        f.read(4 + (b2 & 0x7F))
        body = json.dumps({"type": "update", "data": {"system_health": summary}}).encode()
        half = len(body) // 2
        ws.sendall(bytes([0x89, 0]))                                         # a ping
        ws.sendall(bytes([0x01, 126]) + struct.pack(">H", half) + body[:half])  # text, not final
        ws.sendall(bytes([0x8A, 0]))                                         # a pong in between
        ws.sendall(bytes([0x80, 126]) + struct.pack(">H", len(body) - half) + body[half:])
        http, _ = srv.accept()
        http.makefile("rb").readline()
        events = json.dumps({"events": [{"node_name": "magos0", "worst_status": "degraded"}]}).encode()
        http.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(events)
                     + events)
        http.close()
        ws.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_the_site_script_reads_the_gateway():
    port = _fake_gateway(SUMMARY)
    script = health.HEALTH_PY.replace("8080", str(port))
    r = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"summary": SUMMARY, "problems": [{"node": "magos0", "status": "degraded"}]}


def test_a_file_on_the_site(tmp_path, monkeypatch):
    # The site is this machine: the same command, run here.
    monkeypatch.setattr(health, "_ssh", lambda host, user, remote: ["bash", "-c", remote])
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "cfg dir").mkdir()
    (tmp_path / "cfg dir" / "full_system.yaml").write_text("x")
    assert health.file_on_site("h", None, str(tmp_path / "cfg dir" / "full_system.yaml"))
    assert health.file_on_site("h", None, "~/cfg dir/full_system.yaml")
    assert not health.file_on_site("h", None, "~/cfg dir/other.yaml")


def test_the_routes(client, monkeypatch):
    monkeypatch.setattr(health, "check", lambda host, user, fresh: {"host": host, "fresh": fresh})
    assert client.get("/api/systems/-oProxyCommand=x/health").status_code == 404
    assert client.get("/api/systems/axon-gotcha-2/health").json() == {"host": "axon-gotcha-2", "fresh": False}
    assert client.get("/api/systems/axon-gotcha-2/health?fresh=1").json()["fresh"] is True
    assert client.get("/api/systems/my-laptop/health").status_code == 404   # not one of the bot's sites
    assert client.post("/api/systems/axon-gotcha-2/speed", json={"kind": "everything"}).status_code == 422

    def busy(*a):
        raise health.SiteBusy("a check of axon-gotcha-2 is already running")
    monkeypatch.setattr(health, "speed", busy)
    assert client.post("/api/systems/axon-gotcha-2/speed", json={"kind": "link"}).status_code == 409
    log = [json.loads(x) for x in (S.state_dir / "audit.jsonl").read_text().splitlines()]
    assert {"action": "speed-test", "target": "axon-gotcha-2", "kind": "link"}.items() <= log[-1].items()
