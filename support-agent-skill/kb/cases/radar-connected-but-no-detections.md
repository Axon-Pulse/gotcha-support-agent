---
topics:
- radar
- network
symptoms:
- no targets at all
- the radar isn't detecting anything
- everything started but the map stays empty
- it was working yesterday
- no alarms even when we fly a drone
---

# The system starts cleanly and no detections ever arrive

The system comes up perfectly. Every container is running, every node is registered, nothing
is in an error state — and nothing is ever detected, including during a deliberate flight
test. This is the highest-stakes silent failure in the system, because every indicator the
customer can see says the system is fine.

## Evidence

- `SetParams failed`
- `Error: Cannot open binary file`

## Root cause

Ranked by how often it is actually the answer:

1. **The APU is up but the radar behind it is not connected.** The config's `magos_radar` `ip`
   is the APU (`.6x`); the radar itself is the same address ending `.5x` and is not in the
   config. The APU answers, the node connects, everything looks healthy, and the radar is
   unpowered or unplugged. If the APU answers and the derived radar address (`.60` -> `.50`)
   does not, this is the probable cause.
2. **The machine cannot reach the sensor subnet.** The sensor types sit on separate
   subnets (which ones is in the site's own config, e.g. `192.168.44.x` for the radar link on
   axon-gotcha-4), and the host must route to all of them. A NIC change, a switch reconfiguration, or a VLAN edit on the customer's
   side takes out an entire class of sensor at once while leaving the software untouched.
3. **The sensor is powered off, unplugged, or has been re-addressed.** The config still names
   the old address and the node retries forever against nothing.
4. **The site config declares sensors this system does not physically have**, or omits ones it
   does. `--print-config` against reality settles it.
5. **Detections arrive but stop before reaching the UI.** Rarer, and it changes the answer
   completely — see below.

## Checks (read-only)

**There is no startup connectivity gate anywhere in gotcha.** Sensor nodes retry rather than
exit when they cannot reach their hardware — this is stated outright in
`configs/default/full_system.yaml:41-42` — so a wrong IP, an unplugged cable, or a radar on a
different subnet produces a system that starts, registers on eCAL, reports healthy, and
delivers nothing.

So the question is never "did it start". It is "can it reach the sensor".

First, what should this site have?

```
docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config
```

Then, can the machine reach each of those addresses? A `magos_radar` `ip` here is the **APU**.
The radar itself is the same address with `.6x` replaced by `.5x`, so check both. The triage's
sensor paths section already lists the derived radar line; this is the same check by hand:

```
for ip in <the APU IPs from --print-config> <each APU's radar: .6x -> .5x>; do
  ping -c2 -W2 "$ip" >/dev/null 2>&1 && echo "OK   $ip" || echo "DOWN $ip"
done
```

> On a live customer site, ask before putting traffic on the sensor subnets.

## Fix

Work the network first, because that is where it usually is. When the radar is the missing
piece, the fix is on site: power, cable or the APU-to-radar link. It is not a software
restart.

**Once the radar is back, restart nothing.** The `magos_radar` node holds a websocket to the
APU and reconnects on its own, without a retry limit: 1 s backoff doubling up to 30 s, and a
10 s no-data watchdog that drops a dead link (`ReconnectConfig`, `src/radar/magos/magos.h:42`;
`scheduleReconnect`, `magos.cpp:335`). The APU stayed up, so the node never lost its link,
and detections resume when the radar rejoins the APU. Expect them within about a minute.

Only if nothing appears after a couple of minutes, restart **just that node**, not the whole
system. Either of these:

- in the C2 UI, Node Health window: select the node (e.g. `magos0`) and press Restart
- on the machine, `./build/bin/keyboard_node` in the core container: `l` lists nodes, `r`
  restarts one

Check `grep -n end_on_first_complete configs/<site>/full_system.yaml` first. If it is `true`,
even a single-node restart resets the whole system (`end-on-first-complete-resets-system`),
so warn the operator.

`make restart SERVICE=gotcha30` restarts every node and blanks the UI. Keep it for when a
single-node restart didn't help, or for a change that needs a new launcher session (a
site-config edit).

If every address is reachable and there are still no detections, find out where in the chain
the data stops. The path is:

```
radar --> /radar/detections --> tracker --> /radar/tracks --> target --> /Targets --> gateway --> UI
```

Ask the gateway whether it is receiving anything at all:

```
curl -sS -m 5 http://<machine>:8080/health
```

`data_provider_available: false` means nothing is arriving at the gateway and the problem is
upstream — or the gateway never connected to the core at all, which is
`ui-shows-tracks-over-dallas`.

## Verify

Fly a controlled test and confirm targets appear in the UI. A silent system that has been
"fixed" without a live test has not been verified — this failure mode looks identical to
working, which is the entire problem.

## If that didn't work

- **If tracks appear but are never classified**, the detection chain is fine and you want
  `tracker-never-classifies-drone`.
- **If tracks appear over Dallas, Texas**, none of this is real data —
  `ui-shows-tracks-over-dallas`.
- **If a node is silently absent** rather than merely quiet, check for a heartbeat timeout —
  `node-died-no-message`.
- **If a sensor was recently removed from the config and is somehow still running**, the
  override probably never applied — `override-silently-ignored`.
- For a radar replaying from a recording, `Error: Cannot open binary file: <path>` means the
  replay file is missing, not that the radar is unreachable.
