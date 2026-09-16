"""Magos radar domain checks. PLACEHOLDER — see tools/_placeholder.py.

What this will become: the radar's own view of its link and its scan. `get_system_health`
already exposes `connection_state`, `connection_errors` and `read_errors` for a Magos
node, and kb/magos_radar.md is where the reading of those belongs. What health cannot
tell you is whether the WebSocket is *flapping* (reconnecting repeatedly while reporting
CONNECTED) or genuinely down, and whether the unit is scanning at all. That distinction
needs a query to the radar, which is what this file is reserved for.
"""
from registry import tool
from tools._placeholder import probe, schema


@tool(schema("get_radar_status", "radar",
             "read the Magos WebSocket link state behind connection_errors/read_errors, "
             "separating a flapping link from a dead one, and report scan and detection "
             "rates from the unit itself rather than from the node wrapping it."))
def get_radar_status(node: str) -> dict:
    return probe(node, domain="radar", command="radar_status", planned=(
        "Query the Magos unit for link state, reconnect count and scan status, so a "
        "flapping WebSocket can be told apart from a dead radar."))
