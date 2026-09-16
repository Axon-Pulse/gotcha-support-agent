---
topics:
- acoustic
symptoms:
- the acoustic sensor shows no tracks
- acoustic sensor is critical but the machine is fine
- no audio detections since start-up
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
    curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/

`get_asu_service_status` does both and reports which of the four cases it is.

## Fix

Start the container, then confirm `success_count` climbs. The container must be up before
the system launches.
