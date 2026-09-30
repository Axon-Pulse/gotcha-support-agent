---
topics:
- camera
- network
symptoms:
- the camera isn't working
- we can't move the camera
- the camera went offline
- the camera image is frozen
- slew commands do nothing
---

# The PTZ camera is offline or will not accept commands

The camera stops responding — either the picture freezes or disappears, or the picture is
fine but pan/tilt/zoom commands do nothing. Frequently reported as "the camera is broken"
when the camera itself is healthy and the network path to it is not.

## Evidence

- `camera OFFLINE — lost ONVIF link to`
- `ONVIF connect failed for`
- `onvif-zeep not installed; cannot connect`
- `No media profiles on camera`
- `PtzControl call timed out (camera`

## Root cause

The node maintains an ONVIF link to the camera and reports the loss explicitly at
`python/nodes/optic_ptz_node/optic_ptz_node.py:328`:

> `optic_ptz[<name>]: camera OFFLINE — lost ONVIF link to <ip>`

That message means the node is alive and the camera is not reachable — so this is a network,
power, or credentials problem, not a software one. `python/nodes/optic_ptz_node/onvif_client.py`
distinguishes the cases, and the exact message matters:

| Message | What it actually means |
|---|---|
| `ONVIF connect failed for <ip>` | Cannot reach the camera at all — wrong IP, no route, powered off, or ONVIF disabled on the camera |
| `No media profiles on camera <ip>` | Reached it and authenticated, but it exposes no usable stream — usually a camera-side configuration problem |
| `Profile <p> on <ip> has no PTZ configuration` | It is a video-only camera, or that profile is |
| `Camera <ip> has no PTZ support` | Wrong camera model for this role — it is a fixed camera |
| `onvif-zeep not installed; cannot connect` | Software: the ONVIF library is missing from the environment |

Note that **sensor nodes retry rather than exit when they cannot connect**. A camera that was
never reachable produces a node that starts cleanly, registers on eCAL, reports healthy, and
never delivers anything. There is no startup connectivity gate anywhere in this system.

## Checks (read-only)

**First, confirm this site even has a PTZ camera.** More than one long troubleshooting call
has been spent on a camera a site does not have:

```
docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config
```

Look for a node of type `python_optic_ptz`. If there isn't one, stop — the customer is
describing a different device, most likely the Meduza optic.

Then, the log:

```
make logs SERVICE=gotcha30 2>&1 | grep -i "optic_ptz\|onvif"
```

## Fix

Get the camera's address from the resolved config, then work outward from the network:

```
docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config | grep -A3 optic_ptz
```

With the IP in hand, from the gotcha machine:

```
ping -c3 <camera-ip>
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://<camera-ip>/onvif/device_service
```

- **No ping** — power, cable, switch, or the machine is not on the optic subnet. Optic devices
  are conventionally on `192.168.3.x`, and the host has to route to all three sensor subnets
  (`.1.x` radar, `.2.x` acoustic, `.3.x` optic).
- **Pings but ONVIF refuses** — ONVIF is disabled on the camera, or it is on a non-default
  port. The configured `onvif_port` is in the `--print-config` output.
- **Reachable but authentication fails** — the camera's credentials were changed on the camera
  and not in the site config.

Once the network is proven, restart the node so it re-establishes:

```
make restart SERVICE=gotcha30
```

## Verify

```
make logs SERVICE=gotcha30 2>&1 | grep -i "optic_ptz" | tail -20
```

No `camera OFFLINE` line, and a PTZ command from the UI actually moves the camera.

## If that didn't work

- If the camera responds to commands but there is no picture in the UI, that is a video
  pipeline problem, not an ONVIF one — go to `no-video-in-ui`.
- `PtzControl call timed out (camera /optic/<id> offline?)` from the director node means the
  director could not reach the *camera node*, not the camera. Check the camera node is running
  at all before chasing the camera.
- If the camera works when addressed directly but not through gotcha, compare the credentials
  in the site config against what the camera actually has. The config carries a `password`
  field, so treat that output as sensitive.
