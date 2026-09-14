# Tailscale subnet routes hijack the sensor LAN

A tailnet peer advertising `192.168.40.0/24` (or any sensor subnet) as a subnet route
makes the kernel prefer `tailscale0` for sensor traffic. Recorded in
`configs/two_sensor_test.yaml`: "radar reachability required bringing Tailscale down —
it was advertising 192.168.40.0/24 and hijacking traffic to the sensor LAN."

**The diagnostic transport is a known cause of the faults it diagnoses.**

## Both outcomes are ambiguous, not just failure
- `ambiguous_route_hijack` — no reply AND a tailnet route covers the target. Cannot
  distinguish a dead sensor from our own route eating the packets.
- `reachable_via_tailnet` — replies arrived, but over `tailscale0`. The traffic tunnelled
  through a peer, so the **local** path is untested and the responder may not be the
  intended device.

In neither case may you report the sensor as faulty, or the network as healthy.

## Checks (read-only)
    ip route get <sensor-ip>            # look at `dev` — tailscale0 means hijacked
    tailscale status --json | jq '.Peer[] | select(.PrimaryRoutes)'

## Fix (requires approval — it disconnects the tailnet)
    sudo tailscale down
    # or, less disruptive:
    sudo tailscale set --accept-routes=false
