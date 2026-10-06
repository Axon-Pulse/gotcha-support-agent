---
topics:
- optic
symptoms:
- the thirdeye is red or yellow and there are no optic detections
- the meduza is not connected
- how do we connect the thirdeye
- how do we start the meduza
- the thirdeye address answers but nothing comes from it
- the meduza stays on initializing
---

# The Meduza has to be connected or started from its own web page

ThirdEye and Meduza are the same sensor (`meduza_optic` in the config). When the node can reach
the sensor but the sensor itself isn't connected or started, the fix is not in the C2 UI and not
a restart. It is done on the sensor's own web page.

Recorded from the team on 2026-10-06. It is what they do in the field; it was **not** read from
the source or tried on a sensor by the bot. See "Not known yet" before relying on a detail.

## What the team does

Open `http://<the Meduza's IP>:6010` in a browser. The sensor serves a page that walks through
connecting and starting it. If it is already connected, press **Next** and follow the site. It is
not complicated.

Unlike the radar (`radar-transmitter-off`), there is no control for this in the product: the C2
UI only receives the Meduza's data and sends it redirections, and nothing under `GUItcha30/`
connects, starts or resets it (searched at v1.4.0). So the sensor's own page is the route, and
`SKILL.md`'s "look for the product control first" rule has this one exception.

## When it applies

The link has to be fine first. Otherwise the page won't load and the fault is `N1`-shaped (see
"If that didn't work"). Check in the triage:

- the Meduza's address answers (`ping OK` in sensor paths) and its route is not via `tailscale0`
- the node reaches the sensor but the sensor reports a state that needs attention. The source
  turns the sensor's own status into node health: `Not Connected (0x00)` is CRITICAL
  (`src/nodes/meduza_node/meduza_node.cpp:1382-1386`), and `Initializing (0x01)` for over
  30 s is DEGRADED (`:1394-1398`). The node logs `System status changed from ... to ...`, and
  its `[Meduza Health]` line shows `Sensor: <status>` and `Connected: YES/NO`.

Careful with `[Meduza Health] Status: OK`: that line calls 0x01-0x05 all fine (`:109-126`), but the
Node Health window does not. Judge by the sensor status text and the Node Health state.

## Fix

Someone on a device that can reach the sensor's address, which is on the sensor LAN (a laptop on
site, or the gotcha machine): open `http://<ip>:6010` in a browser. The `<ip>` is the `ip` of the
site's `meduza_optic` node, in the triage's "site config: sensor addresses" lines. **The port is
6010, not the node's `port`** (that one, `9088` in the default config, is the data connection).
6010 was verified by the team on a sensor on 2026-10-06 and appears nowhere in the source, so don't
look for it there. If the page doesn't load on 6010, say so and go to "If that didn't work"; don't try
other ports. If it is already connected, press **Next** and follow the page. Give the PM those words
and nothing more detailed: the page's own steps are not recorded here, so don't describe buttons or
screens.

Don't restart the node. It reconnects by itself (1 s doubling to 30 s, `:337-343`), and each time it
connects it sends the designator control and the scan range again (`:226-277`), so once the
sensor is up the node recovers on its own.

A route over `tailscale0` to the sensor's subnet is the hijack case in `system-model.md`
("Reachability is not liveness"). A browser from the tailnet won't reach the sensor properly
through it, and clearing the route comes first.

## Verify

The node's `System status changed from ... to Running (0x02)` (or a scanning state), `[Meduza
Health]` showing `Connected: YES` with `Detections` and `Video` counts climbing, the node health
back to healthy in the C2 UI, and optic detections on the map.

## Not known yet

Ask the team and fill these in; until then, say they aren't recorded:

- which sensor states the page clears (not connected, stuck Initializing, both)
- whether it is safe while the sensor is scanning or designating, and who should do it (PM or a
  field engineer)
- anything the page asks for, such as a login

## If that didn't work

- **The address doesn't answer, or the page doesn't load**: the problem is the link or the sensor's
  power, not the page. Power, cable or network at the Meduza, checked on site. The node keeps
  retrying by itself. Source for the reconnect and health rules: `meduza_node.cpp:337-343`,
  `:1376-1446`.
- **The page loads and the sensor still doesn't leave Initializing** (or goes back to it): escalate
  with the triage lines quoted and what was tried on the page.
