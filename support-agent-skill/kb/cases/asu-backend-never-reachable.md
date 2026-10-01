---
topics:
- acoustic
symptoms:
- the acoustic sensor shows no tracks
- acoustic sensor is critical but the machine is fine
- no audio detections since start-up
- the dashboard shows zeroes for the acoustic panel while everything else looks alive
- operator says the web page does not make sense, or contradicts the launcher page
- yaw and pitch both read zero and processing_status reads unknown
---

# ASU backend was never reachable since start-up

Captured in `tests/fixtures/health_faulty.txt`.

## Root cause

The `dumbo-backend` container was not running, so the ASU node failed every request it
ever made. The ASU is a local Docker service, not hardware on the sensor LAN, so nothing
about the sensor network was involved.

The counters say "never worked" rather than "stopped working":

    request_count  26654
    success_count      0
    error_count    26654     ~ one failed request per second since start
    asu_connected  false
    connection_details  "HTTP API: http://localhost:8000"

A backend that worked and then stopped looks different: `success_count` high but flat,
with errors only recent.

## Checks (read-only)

    docker ps -a --filter name=dumbo        # -a matters: an exited container is a
                                            # different fault from an absent one
    curl -s -o /dev/null -w '%{http_code}' <the URL asu_node was started with>

The port differs per site (`:8000` here, `localhost:9000` on `axon-gotcha-4`); take it from
the triage's "acoustic backend (ASU)" section, which also lists images, compose files and
services, instead of assuming `:8000` or a container called `dumbo`. `get_asu_service_status`
does the container and URL checks and reports which of the four cases it is.

If no container is found, say the backend is *not running*, not that it is *not installed*:
the first is what the output shows, the second needs someone to look at the machine.

## What the operator sees

This arrives as a UI complaint far more often than as "the ASU is down", because the
page is where the zeros surface. Every ASU value the dashboard renders is zero or
`unknown` — `yaw`, `pitch`, `active_tracks`, `confirmed_tracks`, `tentative_tracks`,
`average_confidence`, `processing_status` — while the launcher page in the same browser
shows every node RUNNING and the radar panel updates normally. One dead panel among
live ones reads to an operator as "the page is wrong", not as "a node has failed", so
the ticket usually says nothing about the ASU at all.

Confirmed a second time on the bench, 2026-09-16, with `request_count 440`,
`success_count 0`, `asu_connected false` at `uptime_seconds 440` — same 1:1 ratio, and
nothing listening on port 8000 (`ss -ltn`).

## Fix

Start the container, then confirm `success_count` climbs. The container must be up before
the system launches.
