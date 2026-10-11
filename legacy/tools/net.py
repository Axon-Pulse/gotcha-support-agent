"""Reachability with route attribution.

A ping timeout is NOT proof a sensor is down. configs/two_sensor_test.yaml documents
Tailscale advertising 192.168.40.0/24 and hijacking traffic to the sensor LAN — the
diagnostic transport is a known cause of the faults it diagnoses.

So the verdict is three-way. If a tailnet route covers the target prefix, the verdict is
FORCED into an ambiguous value: `unreachable` is unreachable from that branch, and no
amount of prompting can talk the model past it.
"""
import ipaddress
import json
import re
import shutil

import inventory
import transport
from registry import tool


def _route(ip: str) -> dict:
    out = transport.run_argv(["ip", "route", "get", ip], timeout=5)
    dev = re.search(r"\bdev\s+(\S+)", out)
    src = re.search(r"\bsrc\s+(\S+)", out)
    return {"dev": dev.group(1) if dev else None,
            "src": src.group(1) if src else None, "raw": out.strip()[:200]}


def _tailnet_routes_covering(ip: str) -> list[dict]:
    """Tailnet peers advertising a subnet that contains the target."""
    if not shutil.which("tailscale"):
        return []
    try:
        st = json.loads(transport.run_argv(["tailscale", "status", "--json"], timeout=8) or "{}")
    except (json.JSONDecodeError, OSError):
        return []
    addr, hits = ipaddress.ip_address(ip), []
    for peer in list((st.get("Peer") or {}).values()) + [st.get("Self") or {}]:
        for r in (peer.get("PrimaryRoutes") or []):
            try:
                if addr in ipaddress.ip_network(r, strict=False):
                    hits.append({"peer": peer.get("HostName", "?"),
                                 "route": r, "online": peer.get("Online", False)})
            except ValueError:
                continue
    return hits


def _ping(ip: str, iface: str | None = None) -> dict:
    argv = ["ping", "-c", "3", "-W", "1"] + (["-I", iface] if iface else []) + [ip]
    out = transport.run_argv(argv, timeout=8)
    m = re.search(r"(\d+) received", out)
    return {"received": int(m.group(1)) if m else 0, "iface": iface}


@tool({
    "name": "probe_endpoint",
    "description": (
        "Check whether a node's endpoint is reachable, WITH route attribution. Returns "
        "one of: reachable | reachable_via_tailnet | unreachable | ambiguous_route_hijack. The two middle values mean a "
        "Tailscale peer advertises a subnet containing the target, so the failure may be "
        "our own routing rather than the device. NEVER conclude a sensor is down or "
        "faulty from an 'ambiguous' verdict — say the route must be cleared first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"node": {"type": "string", "enum": inventory.names(),
                                "description": "Node name from the inventory."}},
        "required": ["node"], "additionalProperties": False,
    },
})
def probe_endpoint(node: str) -> dict:
    n = inventory.require(node)          # refuses anything not in the allowlist
    ep = n.get("endpoint")
    if not ep or not ep.get("host"):
        return {"node": node, "verdict": "no_endpoint_configured",
                "note": "This node has no ip/base_url in the config; nothing to probe."}

    host = ep["host"]
    try:
        ip = str(ipaddress.ip_address(host))
    except ValueError:
        if host in ("localhost", "127.0.0.1"):
            ip = "127.0.0.1"
        else:
            return {"node": node, "host": host, "verdict": "indeterminate",
                    "note": "Hostname is not an IP literal; DNS resolution not attempted."}

    hijack = _tailnet_routes_covering(ip)
    route = _route(ip)
    default = _ping(ip)
    reachable = default["received"] > 0

    # A tailnet route covering the target makes the result ambiguous in BOTH
    # directions: a failure may be our route, and a success may be a tunnel to a
    # peer's LAN rather than our own path to the device.
    via_tailnet = bool(hijack) and (route["dev"] or "").startswith("tailscale")
    if via_tailnet and reachable:
        verdict, liveness = "reachable_via_tailnet", "alive_but_path_unverified"
    elif via_tailnet:
        verdict, liveness = "ambiguous_route_hijack", "unknown"
    elif reachable:
        verdict, liveness = "reachable", "alive"
    else:
        verdict, liveness = "unreachable", "probably_down"

    out = {
        "node": node, "type": n["type"], "target": ip, "port": ep.get("port"),
        "verdict": verdict, "device_liveness": liveness,
        "route": {"selected_dev": route["dev"], "selected_src": route["src"]},
        "tailnet_routes_covering_target": hijack,
        "ping": {"received": default["received"], "sent": 3},
        "l2_check": "unavailable (arping not installed; install it and grant "
                    "cap_net_raw to distinguish a dead device from a hijacked route)"
                    if not shutil.which("arping") else None,
    }
    if verdict == "reachable_via_tailnet":
        peers = ", ".join(f"{h['peer']} advertises {h['route']}" for h in hijack)
        out["note"] = (f"Replies arrived, but over dev={route['dev']} — {peers}. Traffic "
                       f"is tunnelling through the tailnet, so the LOCAL path to the "
                       f"sensor LAN is untested and the responder may not be the intended "
                       f"device. Do not conclude the sensor network is healthy. Re-test "
                       f"with `tailscale down` to verify the direct path.")
    if verdict == "ambiguous_route_hijack":
        peers = ", ".join(f"{h['peer']} advertises {h['route']}" for h in hijack)
        out["note"] = (f"Target is unreachable BUT {peers}, and the kernel selected "
                       f"dev={route['dev']}. This is indistinguishable from a dead device "
                       f"until the tailnet route is removed (`tailscale down`, or "
                       f"`tailscale set --accept-routes=false`). Do not report the device "
                       f"as faulty on this evidence.")
    return out
