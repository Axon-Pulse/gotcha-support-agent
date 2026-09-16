"""Tower and platform checks. PLACEHOLDER — see tools/_placeholder.py.

What this will become: the composed orientation of everything on one platform. A tower
publishes no health of its own — nothing runs on a mast — so this is the one domain whose
real check reads CONFIGURATION rather than telemetry: platforms.<tower>.yaw_deg plus each
sensor's mount.yaw_deg, and whether any sensor also sets an azimuth_offset/yaw of its own
(which the launcher rejects). kb/tower.md has the reasoning; the discriminator is whether
an angular error is shared across the platform or belongs to one mount.

Until then this probes the platform's compute box for liveness, which says nothing about
alignment.
"""
from registry import tool
from tools._placeholder import probe, schema


@tool(schema("get_tower_status", "infrastructure",
             "read the composed orientation for every component on a platform — the "
             "tower yaw plus each mount offset — and flag a sensor that overrides it, so "
             "an angular error shared across the platform can be told apart from one "
             "belonging to a single mount."))
def get_tower_status(node: str) -> dict:
    return probe(node, domain="infrastructure", command="tower_status", planned=(
        "Read platforms.<tower>.yaw_deg and each component's mount.yaw_deg from the "
        "deployment config and report the composed world_yaw per sensor."))
