---
topics:
- network
symptoms:
- we can't find the new modem
- the modem is powered through PoE but unreachable
- the switch says the link is down but the device has power
- the link is slow but ping works
- the connection to the system is very slow
- the laptop is stuck at 10 Mbit
- the link speed dropped from 1 Gbit
- the connection works but everything is slow
---

# A link is down or negotiated far below its speed (cable or port)

A device that has power can still have no network link, and a link that pings fine can be running
at a tenth or a hundredth of its speed. Both are usually the physical layer: the cable or the port,
not settings and not the device. Ping cannot tell a 10 Mbit link from a 1 Gbit one, so a slow
system with "no packet loss" is not a healthy link until the speed has been read.

Recorded from a field investigation on 2026-10-08 (a Teltonika modem brought to a site). It is
what the team found and concluded on site. It was **not** tried by the bot, and the cause below is
**suspected, not confirmed**: nobody had yet swapped the cable when this was written. See "Not known
yet".

## What was seen

- **Modem not found.** A Teltonika modem was turned on through PoE (its LEDs lit and made sense) but
  was unreachable on the network. The switch itself reported the port's link as **down** even though
  the port was supplying power.
- **Slow link.** While investigating, the laptop-to-switch connection was found capped at
  **10 Mbit/s**, although both sides reported they should allow gigabit. It pinged fine.

## What the triage shows, and what it can't

The triage's "network links" section reads this machine's own side of each physical NIC: link state,
negotiated speed, duplex, CRC and other error counts since boot, how many times the link came up
since boot, and which neighbours answered on it. It flags speed below 1000, half duplex, CRC errors
and a link that came up more than 3 times. It also shows each default gateway with its neighbour
state (and ping with `--ping`), and for wifi the signal level in dBm, flagged below -70.

- A `<-- BELOW 1000 Mbit/s` line on a wired NIC is the finding for the slow-link case. "Below 1000"
  is a heuristic: a machine that really has a 100 Mbit NIC shows it too, so say "check", not
  "faulty", and ask what the NIC is.
- CRC errors or a flapping link point at the cable or port. The counters are since boot, so read
  them next to the `up:` time in the host section.
- The modem is not a gotcha sensor, so it is not in the site config. It shows up (or not) under
  `neighbours seen` for the NIC it is plugged into, or as the default gateway if it is the uplink.
  A neighbour that is `FAILED` or missing means nothing on that wire answered.

**What the bot cannot see:** the switch's own view of the port, PoE state, the modem's LEDs and
status, or the far end of the cable. This machine's NIC reports only this machine's port. In the
modem story, "the switch says the link is down" is information only someone at the switch has. So
the bot cannot tell a bad cable from a bad port from a dead modem. That is settled by swapping parts.

If the modem is the machine's only uplink and it is down, the machine is offline and nothing here
can be triaged. Use "When you can't connect" in `SKILL.md`.

## Fix

Physical, on site, one change at a time, so the result says which part was bad:

1. **Replace the cable.** The most likely cause for both symptoms.
2. **Try another port on the switch.** Then, if there is a managed switch, read the port's speed and
   error counters there.
3. **Only then suspect the modem.** The team judged a bricked modem unlikely here because its LEDs
   all worked and made sense, but LEDs are not proof. Try it on a known-good cable and port first.

For the slow laptop link: after a new cable, read the negotiated speed again (on a Linux laptop,
`cat /sys/class/net/<nic>/speed`; on other systems, the adapter's status page). It should be 1000.

The two findings were on the same switch, so a switch port or the switch itself is worth keeping in
mind if a new cable doesn't fix both. That is a suggestion from the evidence, not something the team
found.

Don't change the modem's IP, reset it, or call it bricked as a first answer. None of those can fix a
link that is down at the physical layer.

## Verify

The triage's network line for that NIC shows `speed=1000 duplex=full` with `crc` not rising between
two runs, the modem's address answers (neighbour `REACHABLE`, ping OK), and the switch port's link
light is on.

## Not known yet

Ask the team and fill these in; until then, say they aren't recorded:

- which part was actually bad (cable, port, modem) once the swaps were done
- the modem's address on the site LAN and which NIC it is plugged into
- whether the site's switch is managed (so port speed and errors can be read from it)
- whether the 10 Mbit laptop link and the modem's dead link shared a cause

## If that didn't work

- **Still no link on a known-good cable and port, with the modem powered:** the modem is the
  suspect. It goes back to the team with what was tried, in order.
- **Speed is 1000 but it is still slow:** not this case. Look at the uplink (wifi signal, DERP in the
  tailnet ping, the carrier) and say what the bot can't see: modem signal, carrier and throughput
  are not visible from the machine.
