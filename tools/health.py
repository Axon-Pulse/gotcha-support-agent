"""Node self-reported health from /system/health.

Each node publishes up to 28 numeric + 5 string + 5 boolean custom metrics per message.
Verbatim that is ~40 lines per node per sample. We keep a `salient` map instead.

EDIT POLARITY/CORE below when you add a node type. The polarity table matters: a naive
"any false boolean is interesting" rule fires on magos.has_mastership=false and
recording_active=false, both of which are CORRECT on a bench run — and the agent then
chases two red herrings on every single call.
"""
import re

import transport
from registry import tool

# Booleans whose *expected* value is not True. Anything differing from expectation is salient.
POLARITY = {
    "magos": {"has_mastership": False, "recording_active": False, "tx_enabled": True},
    "asu":   {"asu_connected": True},
}
# Always-show keys per node type.
CORE = {
    "acoustic_asu": ["success_rate", "request_count", "success_count", "processing_status"],
    "radar_magos":  ["active_tracks", "alerts_count"],
}
_ERRISH = re.compile(r"error|fail|drop|retry|timeout", re.I)
_RATEISH = re.compile(r"rate|ratio", re.I)
_MAX_SALIENT = 12


def _blocks(text: str) -> list[str]:
    """Split the protobuf-text dump into one string per NodeHealth message."""
    out, cur = [], []
    for line in text.splitlines():
        if line.startswith("node_id:") and cur:
            out.append("\n".join(cur))
            cur = []
        if line.strip() and not line.startswith("[eCAL]") and not line.startswith("echo "):
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _parse_block(b: str) -> dict:
    """Flatten one NodeHealth protobuf-text message."""
    top, common, conns, num, s_str, boo = {}, {}, [], {}, {}, {}
    section, pending, depth = None, {}, 0
    for raw in b.splitlines():
        line = raw.strip()
        if line.endswith("{"):
            name = line[:-1].strip()
            if depth == 0:
                section, pending = name, {}
            depth += 1
            continue
        if line == "}":
            depth -= 1
            if depth == 0:
                if section == "connections":
                    conns.append(pending)
                elif section and section.startswith("custom_"):
                    k, v = pending.get("key"), pending.get("value")
                    if k is not None:
                        {"custom_numeric_metrics": num, "custom_string_metrics": s_str,
                         "custom_boolean_metrics": boo}[section][k] = v
                section, pending = None, {}
            continue
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if v.startswith('"'):
            val = v.strip('"')
        elif v in ("true", "false"):
            val = v == "true"
        else:
            try:
                val = float(v) if "." in v else int(v)
            except ValueError:
                val = v
        target = pending if depth else (common if section == "common" else top)
        if depth == 0 and section is None:
            top[k] = val
        else:
            target[k] = val
        if section == "common" and depth == 1:
            common[k] = val
    return {"top": top, "common": common, "conns": conns,
            "num": num, "str": s_str, "bool": boo}


def _salient(node: str, ntype: str, p: dict) -> tuple[dict, int]:
    out, pol = {}, POLARITY.get(node, {})
    for k in CORE.get(ntype, []):
        for src in (p["num"], p["str"], p["bool"]):
            if k in src:
                out[k] = src[k]
    for k, v in p["bool"].items():
        if v != pol.get(k, True):
            out[k] = v
    for k, v in p["num"].items():
        if _ERRISH.search(k) and isinstance(v, (int, float)) and v > 0:
            out[k] = v
        elif _RATEISH.search(k) and isinstance(v, (int, float)) and v == 0:
            out[k] = v
    for k in ("processing_status", "last_error", "connection_state"):
        if p["str"].get(k):
            out[k] = p["str"][k]
    omitted = (len(p["num"]) + len(p["str"]) + len(p["bool"])) - len(out)
    return dict(list(out.items())[:_MAX_SALIENT]), max(omitted, 0)


_RANK = {"HEALTHY": 0, "UNKNOWN": 1, "DEGRADED": 2, "CRITICAL": 3, "OFFLINE": 4}


def _cluster(samples: list[dict]) -> list[list[dict]]:
    """Split a node's samples into instances by uptime magnitude.

    Two concurrent launcher sessions produce uptimes an order of magnitude apart
    (e.g. 20s and 26680s). Merging them — which is what the gateway does — hides
    the duplicate and makes one node look like it is flapping.
    """
    ordered = sorted(samples, key=lambda p: p["common"].get("uptime_seconds") or 0)
    groups, cur = [], []
    for s in ordered:
        u = s["common"].get("uptime_seconds") or 0
        prev = (cur[-1]["common"].get("uptime_seconds") or 0) if cur else None
        if cur and prev is not None and u > 10 * max(prev, 1):
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    return groups


def _row(nid: str, group: list[dict]) -> dict:
    """Reduce one instance's samples to a single row, worst status wins."""
    worst = max(group, key=lambda p: _RANK.get(p["top"].get("overall_status", "UNKNOWN"), 1))
    latest = max(group, key=lambda p: p["common"].get("uptime_seconds") or 0)
    ntype = worst["top"].get("node_type", "")
    sal, omitted = _salient(nid, ntype, worst)
    conns = [c for p in group for c in p["conns"]]
    return {
        "node": nid, "type": ntype,
        "status": worst["top"].get("overall_status", "UNKNOWN"),
        "health_score": worst["top"].get("health_score"),
        "reason": str(worst["top"].get("status_reason", ""))[:120],
        "uptime_s": latest["common"].get("uptime_seconds"),
        "cpu_pct": round(float(latest["common"].get("cpu_usage_percent", 0) or 0), 2),
        "mem_mb": round(float(latest["common"].get("memory_usage_mb", 0) or 0), 1),
        "errors": max((p["common"].get("error_count", 0) or 0) for p in group),
        # Dedupe on the rendered triple: raw samples differ in byte counters we drop.
        "conns": list({(c["connected"], c["drops"], c["details"]): c for c in (
            {"connected": x.get("is_connected", False),
             "drops": x.get("connection_drops", 0),
             "details": str(x.get("connection_details", ""))[:120]} for x in conns)}.values()),
        "salient": sal, "omitted_metric_count": omitted, "samples": len(group),
    }


@tool({
    "name": "get_system_health",
    "description": (
        "Self-reported health of every node, from /system/health. One compact row per "
        "node INSTANCE. If the same node name appears twice with very different uptimes, "
        "two launcher sessions are running at once — confirm with get_ecal_topology. "
        "Note: `errors` is common.error_count; node-specific counters live in `salient` "
        "and may use the same names with different meanings."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
})
def get_system_health() -> dict:
    parsed = [_parse_block(b) for b in _blocks(transport.run("health"))]
    by_node: dict[str, list[dict]] = {}
    for p in parsed:
        nid = p["top"].get("node_id")
        if nid:
            by_node.setdefault(nid, []).append(p)

    rows, dup = [], []
    for nid, samples in sorted(by_node.items()):
        groups = _cluster(samples)
        if len(groups) > 1:
            dup.append(nid)
        for i, g in enumerate(groups):
            r = _row(nid, g)
            if len(groups) > 1:
                r["instance"] = i + 1
                r["of_instances"] = len(groups)
            rows.append(r)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"samples": len(parsed), "node_count": len(by_node), "row_count": len(rows),
            "duplicate_instance_nodes": dup, "status_counts": counts, "nodes": rows}
