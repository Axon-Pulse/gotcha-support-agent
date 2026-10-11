"""Camera and optics domain checks. PLACEHOLDER — see tools/_placeholder.py.

What this will become: the state of the optic chain — the Meduza EO/IR head, the PTZ
controller and the verification node. The faults worth separating are a camera that is
reachable but not streaming, a PTZ that accepts commands but does not move, and a mount
whose pan-zero is off (python/nodes/optic_ptz_node/orientation.py composes a mount flip
with a compass offset, so a bearing can be wrong while every node is healthy).
"""
from registry import tool
from tools._placeholder import probe, schema


@tool(schema("get_camera_status", "camera",
             "report stream state, PTZ pose and whether the pan-zero offset matches the "
             "mount, so a camera that is reachable but not streaming, or pointing the "
             "wrong way, can be told apart from one that is simply down."))
def get_camera_status(node: str) -> dict:
    return probe(node, domain="camera", command="camera_status", planned=(
        "Query the camera for stream state and PTZ pose, and compare the reported "
        "bearing against the configured mount orientation."))
