"""The ASU acoustic backend: the Docker service behind the node.

The ASU node is a thin eCAL wrapper around "Dumbo", a LOCAL Docker service with an HTTP
API. It is not hardware on the sensor LAN — configs/two_sensor_test.yaml says so, and
kb/asu.md is where that belongs: `asu_connected: false` is a statement about a
container on THIS machine, so probing the sensor LAN can never explain it.

get_system_health knows the node's view of the backend. Only this tool knows whether the
backend process exists at all. The pair is what separates "the container is gone" from
"the container is up but its API is failing" — different faults, different fixes, and
indistinguishable from health alone.
"""
import re

import inventory
import transport
from registry import tool

BACKEND = "dumbo-backend"
PROBE_URL = "http://localhost:8000/"


def _containers(text: str) -> list[dict]:
    """Parse the tab-separated `docker ps` template into one row per container."""
    rows = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip() or "\t" not in line:
            continue
        name, state, status, image = (x.strip() for x in (line.split("\t") + ["", "", ""])[:4])
        exit_code = int(m.group(1)) if (m := re.search(r"Exited \((\d+)\)", status)) else None
        rows.append({"name": name, "state": state.lower(), "status": status[:60],
                     "image": image, "exit_code": exit_code})
    return rows


def _http_code(text: str) -> str | None:
    """curl -w '%{http_code}' prints 000 when it could not connect at all."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1] if lines and re.fullmatch(r"\d{3}", lines[-1]) else None


def _configured_url() -> str | None:
    ep = (inventory.load().get("asu") or {}).get("endpoint") or {}
    if not ep.get("host"):
        return None
    port = f":{ep['port']}" if ep.get("port") else ""
    return f"{ep.get('scheme', 'http')}://{ep['host']}{port}/"


@tool({
    "name": "get_asu_service_status",
    "description": (
        "Ground truth for the ASU acoustic backend: whether the 'dumbo-backend' Docker "
        "container exists and is running, and what its local HTTP API answers. Use this "
        "whenever health shows asu_connected=false — that flag describes a LOCAL container, "
        "not the sensor network, so a reachability probe cannot diagnose it. Distinguishes "
        "container absent / container exited (with exit code) / container up but API "
        "unreachable / healthy."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
})
def get_asu_service_status() -> dict:
    containers = _containers(transport.run("asu_container"))
    code = _http_code(transport.run("asu_api"))
    backend = next((c for c in containers if c["name"] == BACKEND), None)

    if backend is None:
        verdict = "backend_container_absent"
        note = (f"No container named {BACKEND!r}. It was never started, or was removed. "
                f"The ASU node will fail every request for as long as this is true.")
    elif backend["state"] != "running":
        verdict = "backend_container_not_running"
        exit_note = {137: " (SIGKILL — typically the OOM killer)",
                     139: " (SIGSEGV — the backend crashed)"}.get(backend["exit_code"], "")
        note = (f"{BACKEND} exists but is {backend['state']} ({backend['status']}). "
                f"Exit code {backend['exit_code']}{exit_note}. The ASU node cannot reach "
                f"it until it is restarted.")
    elif code is None or code == "000":
        verdict = "backend_running_api_unreachable"
        note = (f"{BACKEND} is running but nothing answered on {PROBE_URL}. The process is "
                f"up and the port is not serving — check the container's own logs, not the "
                f"node.")
    elif code.startswith(("4", "5")):
        verdict = "backend_running_api_erroring"
        note = f"{BACKEND} is running and answered {code} — the API is up but failing."
    else:
        verdict = "backend_up"
        note = f"{BACKEND} is running and answered {code}."

    out = {
        "verdict": verdict, "note": note, "probe_url": PROBE_URL, "http_code": code,
        "backend": backend,
        "other_containers": [c["name"] for c in containers if c["name"] != BACKEND],
    }
    # A base_url pointing somewhere else means the node and this check disagree about
    # where the backend lives — which is a fault in itself, not a detail.
    configured = _configured_url()
    if configured and configured != PROBE_URL:
        out["config_mismatch"] = (
            f"The asu node's base_url is {configured}, but this check probed {PROBE_URL}. "
            f"They must agree before either result means anything.")
    return out
