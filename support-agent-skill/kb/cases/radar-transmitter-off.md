---
topics:
- radar
symptoms:
- the radar is connected but there are no detections
- the radar is on and the cable is fine but nothing is detected
- radar says stopped
- TX OFF on the radar
- the radar was reconnected and still doesn't detect
---

# The radar is connected but its transmitter is off

The APU answers, the `magos_radar` node reports `CONNECTED`, the radar's cable and power have
been checked, and there are still no detections. The radar has stopped transmitting, so it
sees nothing. Nothing is broken: a stopped radar is a valid state, and it stays stopped until
someone starts it. It does not start itself when it is reconnected or power-cycled.

First seen on axon-gotcha-4, 2026-09-30, after the radar was reconnected. The Vcc alerts on
the same radar (`Vcc1V2`, `Vcc2V5`, `Vcc3V3`, `Vcc5V0` "exceeding threshold") were a red
herring there: the site team confirmed they are not limiting.

## Evidence

- `radar=stopped`

The `magos_radar` node prints one status line about every few seconds:
`magos0: CONNECTED | ch ... | det 0.0/s ... | radar=stopped | alerts 6 | err+0`.
`CONNECTED` is the link to the APU. `radar=` is the radar's own state. In the C2 GUI the
sensor shows `TX OFF` and a coverage arc that is unmonitored.

## Root cause

The Magos reports its own operating state, and `stopped` is the one state the vendor documents
as "transmitter off" (`src/nodes/magos_node/radar_state.h:65-71`). The node maps it to
`CONN_DISCONNECTED` and "this sector is unwatched", but it doesn't try to start the radar; only
an operator command does. Someone or something stopped the transmitter (a person on site, a
power cycle that left it stopped), and nobody started it.

## Checks (read-only)

Read the state from the node's own status line:

```
docker logs --tail 200 <core container> 2>&1 | grep -a 'radar=' | tail -3
```

`radar=stopped` is this case. `radar=?` is "the radar hasn't reported a state on this link"
and not the same thing: do not read it as off (`radar_state.h` and the GUI text both say so).
`radar=starting` means wait. Other values: see `radar_state.h`.

Ask whether anyone **stopped it on purpose**. People working near the radar is a normal reason,
and starting it radiates over the whole arc.

## Fix

This is done in the C2 GUI, by an operator. Not on the machine, not on the radar's own
interface, and not by a restart.

1. Confirm nobody is working inside the radar's arc. The GUI's dialog quotes the arc
   (bearing, width, range) for exactly this reason.
2. In the C2 GUI, right-click the radar on the map or the sensor list and choose
   **Transmitter...**. It opens "Transmitter - <node>" showing `TX OFF`. Press
   **Start transmitting** and confirm.
   (`GUItcha30/src/components/RadarTxDialog.tsx`; the entry is greyed out with "This radar is
   not reachable - the command cannot be delivered" when the GUI counts the radar as unreachable,
   which is a different fault.)
3. The GUI sends the command to the `magos_radar` node, which logs
   `SetParams: tx_enabled=ON` and asks the radar to start
   (`src/nodes/magos_node/magos_client_node.cpp`, `commandTx`). The radar goes
   `starting` and then `operational`.

Do **not** restart the node or the container for this. A restart re-reads the radar's state
and it is still `stopped`.

## Verify

`radar=operational` in the node's status line, `TX ON` in the GUI, and `det` above 0/s with
targets on a test flight.

## If that didn't work

- **The Transmitter entry is greyed out**: the GUI considers the radar unreachable. Go back to
  `radar-connected-but-no-detections` (the APU-to-radar link).
- **`SetParams failed: ...` in the node log** (or "another transmitter command is already in
  flight", status 409): the radar refused or didn't answer. Wait a few seconds and try once
  more. If it repeats, escalate with the message text and the saved triage file.
- **It goes back to `stopped` after a start**: something is stopping it. Ask on site who or what
  (a schedule, a second operator, the radar's own interface), and escalate to the radar vendor
  with the saved triage file.
