"""The page: every API call it makes has a route, and every tab renders with real answers."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from console.server import app
from console.settings import S
from console.tests.conftest import SID, stub, write_transcript, write_turns
from console.tests.test_sites import TRIAGE

HERE = Path(__file__).resolve().parent
PAGE = (HERE.parent / "index.html").read_text()


def test_every_api_call_in_the_page_has_a_route():
    calls = set(re.findall(r"api\('(/[a-z]+)", PAGE))
    routes = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
    prefixes = {"/" + p.split("/")[2] for p in routes}
    missing = {c for c in calls if c not in prefixes}
    assert calls and not missing, missing


def test_the_page_loads_nothing_from_outside():
    assert not re.search(r"<(script|link)[^>]+(src|href)=", PAGE)


def test_the_page_has_a_title_and_a_dark_mode():
    assert "<title>Gotcha support console</title>" in PAGE
    assert "prefers-color-scheme:dark" in PAGE


def _capture(client, env) -> dict:
    """The answers the harness replays, captured from the real server in the order it asks."""
    t0 = time.time() - 3600
    write_transcript(env, SID, [
        {"q": "the map is empty on gotcha 4", "at": t0, "answer": "*axon-gotcha-4: ok*", "steps": [
            ("Bash", {"command": "scripts/run_triage.sh axon-gotcha-4"}, "=== containers ===", False),
            ("Read", {"file_path": "/x/kb/cases/radar-transmitter-off.md"}, "1\t---", False)]},
        {"q": "and now?", "at": t0 + 600}])
    S.threads_file.write_text(json.dumps({"C1:100.1": SID}))
    write_turns([
        {"event": "start", "ts": t0 - 1, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": "U1"},
        {"event": "end", "ts": t0 + 31, "key": "C1:100.1", "session_id": SID, "outcome": "ok", "seconds": 31},
        {"event": "start", "ts": t0 + 599, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": "U1"},
        {"event": "end", "ts": t0 + 690, "key": "C1:100.1", "session_id": SID, "outcome": "timeout", "seconds": 90}])
    (S.bridge_dir / ".env").write_text("BRIDGE_TIMEOUT_S=90\nSLACK_BOT_TOKEN=xoxb-x\n")

    r = {}
    get = lambda p: r.__setitem__(p, client.get("/api" + p).json())  # noqa: E731
    # A stand-in for the tailnet, put back at once: selftest.sh checks the real script.
    real = S.skill_dir / "scripts" / "list_systems.sh"
    saved = real.read_text()
    stub(real, "printf 'tailnet name hostname ip state via last\\n"
         "axon-gotcha-4 axon-gotcha-4 100.0.0.4 online tailscale\\n'\n")
    get("/systems")
    real.write_text(saved)
    (S.triage_dirs[0]).mkdir(parents=True, exist_ok=True)
    (S.triage_dirs[0] / "axon-gotcha-4-20261001-100000.txt").write_text(TRIAGE)
    get("/systems/axon-gotcha-4")
    for p in ["/meta", "/selftest", "/deploy", "/conversations", "/conversations/C1:100.1", "/cases",
              "/cases/radar-transmitter-off", "/docs", "/docs/glossary", "/docs/skill", "/stats",
              "/bot", "/audit"]:
        get(p)
    d = r["/cases/radar-transmitter-off"]
    r["PUT /cases/radar-transmitter-off"] = client.put("/api/cases/radar-transmitter-off", json={
        "base": d["etag"], "raw": d["raw"]}).json()
    r["POST /cases"] = client.post("/api/cases", json={"name": "Radar flaps", "topics": []}).json()
    get("/cases/radar-flaps")
    r["POST /cases/radar-flaps/rename"] = client.post("/api/cases/radar-flaps/rename",
                                                      json={"name": "renamed case"}).json()
    get("/cases/renamed-case")
    r["DELETE /cases/renamed-case"] = client.request("DELETE", "/api/cases/renamed-case", json={}).json()
    g = r["/docs/glossary"]
    r["PUT /docs/glossary"] = client.put("/api/docs/glossary", json={"text": g["text"], "base": g["etag"]}).json()
    return r


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_every_tab_and_editor_renders(client, env, tmp_path):
    f = tmp_path / "responses.json"
    f.write_text(json.dumps(_capture(client, env)))
    out = subprocess.run(["node", str(HERE / "page_harness.js"), "render", str(f)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    failed = [r for r in res["results"] if r[0] != "ok"]
    assert not failed, "\n\n".join(" — ".join(r[1:]) for r in failed)
    assert len(res["results"]) >= 25
