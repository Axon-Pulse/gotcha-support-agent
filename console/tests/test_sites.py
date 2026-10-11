"""A site's sensors from its newest saved triage, and running one on demand."""
import time

from console import sites
from console.settings import S
from console.tests.conftest import stub

# The three sections of a triage that describe sensors, in run_triage.sh's own format. Made up:
# addresses and paths are not a real site's.
TRIAGE = """=== host ===
axon-gotcha-7  up 3 days
=== sensor node status: latest line per node (core-1, last 5000 log lines) ===
2026-10-01T10:00:00Z magos0: CONNECTED | ch 3 | det 0.0/s | radar=stopped | err+0
=== resolved config (--print-config, redacted) ===
lots of config
=== site config: sensor addresses (node, type, address; from configs/site7/full_system.yaml) ===
file: /opt/gotcha/configs/site7/full_system.yaml
30:  /site/tower1:
31:    magos0:
32:      type: magos_radar
36:        ip: "10.1.2.60"      # TODO: confirm radar IP at install
40:    cam0:
41:      type: onvif_camera
44:        host: 10.1.2.20
45:        ip: 10.1.2.21
50:    asu1:
51:      type: asu
55:        base_url: "http://localhost:9000"
60:  /processing:
61:    tracker:
62:      type: python_tracker
NOTE: magos_radar ip = the radar's APU (10.1.2.60), not the radar itself; the radar's own address is not in the config (see sensor paths)
=== acoustic backend (ASU) ===
listening
=== sensor paths (route taken, neighbour cache, ping) ===
10.1.2.20        dev eth0 src 10.1.2.1             neigh=REACHABLE  ping OK
10.1.2.21        dev tailscale0                    neigh=none       ping DOWN  <-- ROUTED VIA TAILNET (hijack)
10.1.2.60        dev eth0 src 10.1.2.1             neigh=REACHABLE  ping OK  <- magos APU (config ip)
10.1.2.50        dev eth0 src 10.1.2.1             neigh=FAILED     ping DOWN  <- magos RADAR (derived from APU 10.1.2.60, not in config)
10.9.9.9         dev eth0 src 10.1.2.1             neigh=STALE      ping OK
=== network links (physical NICs, default route) ===
eth0 up 1000 full
=== end of triage ===
"""


def _save(name: str, text: str = TRIAGE):
    d = S.triage_dirs[0]
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def test_a_site_with_no_saved_triage_says_so(client):
    assert client.get("/api/systems/axon-gotcha-7").json() == {"host": "axon-gotcha-7", "triage": None}


def test_the_sensors_come_from_the_newest_triage(client):
    _save("axon-gotcha-7-20261001-090000.txt", TRIAGE.replace("10.1.2.60", "10.0.0.1"))
    _save("axon-gotcha-7-20261001-100000.txt")
    _save("axon-gotcha-70-20261002-100000.txt", "=== host ===\nnot this site\n")
    t = client.get("/api/systems/axon-gotcha-7").json()["triage"]
    assert t["file"].endswith("axon-gotcha-7-20261001-100000.txt") and t["count"] == 2
    assert t["config"]["file"] == "/opt/gotcha/configs/site7/full_system.yaml"

    [tower, processing] = t["config"]["groups"]
    assert tower["platform"] == "/site/tower1" and processing["platform"] == "/processing"
    magos, cam, asu = tower["nodes"]
    assert (magos["name"], magos["type"]) == ("magos0", "magos_radar")
    assert magos["addresses"][0]["value"] == "10.1.2.60"
    assert magos["addresses"][0]["note"] == "TODO: confirm radar IP at install"
    assert magos["addresses"][0]["path"]["ping"] == "OK"
    assert magos["status"].startswith("magos0: CONNECTED") and "radar=stopped" in magos["status"]
    assert [a["key"] for a in cam["addresses"]] == ["host", "ip"]
    assert cam["addresses"][1]["path"]["hijack"] is True
    assert asu["addresses"][0] == {"key": "base_url", "value": "http://localhost:9000", "note": "",
                                   "path": None}
    assert processing["nodes"] == [{"name": "tracker", "type": "python_tracker", "addresses": [],
                                    "status": None}]

    other = {p["ip"]: p for p in t["other_paths"]}
    assert set(other) == {"10.1.2.50", "10.9.9.9"}
    assert other["10.1.2.50"]["neigh"] == "FAILED" and "derived from APU" in other["10.1.2.50"]["label"]
    assert t["config"]["notes"][0].startswith("NOTE: magos_radar ip")
    assert "sensor paths (route taken, neighbour cache, ping)" in t["raw"]
    assert not t["unreachable"]


def test_a_triage_that_could_not_reach_the_site_is_flagged(client):
    _save("axon-gotcha-7-20261001-100000.txt", "UNREACHABLE: no reply from axon-gotcha-7 within 5s\n")
    t = client.get("/api/systems/axon-gotcha-7").json()["triage"]
    assert t["unreachable"] and t["config"]["groups"] == []


def test_only_the_bots_sites_can_be_asked_for(client):
    for h in ["toms-laptop", "axon-gotcha-*", "..", "axon-gotcha-4x"]:
        assert client.get(f"/api/systems/{h}").status_code == 404
        assert client.post(f"/api/systems/{h}/triage", json={}).status_code == 404


def test_run_triage_runs_the_skills_script_and_shows_the_new_one(client, monkeypatch):
    (S.bridge_dir / ".env").write_text("GOTCHA_SSH_USER=support\n")
    stub(S.skill_dir / "scripts" / "run_triage.sh", f"""
[ "$1" = axon-gotcha-7 ] || exit 9
[ "$GOTCHA_SSH_USER" = support ] || {{ echo "ssh user not passed" >&2; exit 8; }}
cat > "$GOTCHA_TRIAGE_DIR/$1-$(date +%Y%m%d-%H%M%S).txt" <<'EOF'
{TRIAGE}
EOF
echo "=== end of triage ==="
""")
    r = client.post("/api/systems/axon-gotcha-7/triage", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"], body["output_tail"]
    assert body["site"]["triage"]["config"]["groups"][0]["platform"] == "/site/tower1"
    assert '"run-triage"' in (S.state_dir / "audit.jsonl").read_text()


def test_a_failed_triage_is_reported(client):
    stub(S.skill_dir / "scripts" / "run_triage.sh", "echo 'UNREACHABLE: no reply' ; exit 10\n")
    body = client.post("/api/systems/axon-gotcha-7/triage", json={}).json()
    assert not body["ok"] and "UNREACHABLE" in body["output_tail"]


def test_one_triage_of_a_site_at_a_time(client, monkeypatch):
    monkeypatch.setitem(sites._running, "axon-gotcha-7", time.time())
    r = client.post("/api/systems/axon-gotcha-7/triage", json={})
    assert r.status_code == 409 and "already running" in r.json()["detail"]


def test_the_bot_saves_its_triages_where_the_console_reads_them():
    from console.tests.conftest import ROOT
    compose = (ROOT / "slack-bridge" / "docker-compose.yml").read_text()
    assert "./state/triage:/tmp/gotcha-triage" in compose
    settings = (ROOT / "slack-bridge" / "workspace" / ".claude" / "settings.json").read_text()
    assert "Read(//tmp/gotcha-triage/**)" in settings, "the model must still read its saved triage"
