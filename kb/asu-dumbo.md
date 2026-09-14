# ASU acoustic sensor is not network hardware

The ASU is a **local Docker service** ("Dumbo"), HTTP API on `http://localhost:8000`,
dashboard on `:5173`. It is not a device on the sensor LAN. The `dumbo-backend`
container must be running before the system launches.

## Symptom: asu node CRITICAL, no tracks
Look for `asu_connected: false` with `connection_details: "HTTP API: http://localhost:8000"`.

Then distinguish two different faults using request counters:
- `request_count` high, `success_count: 0`, `error_count ≈ uptime_seconds`
  → roughly one failed request per second since start, never a single success.
  The backend **was never reachable**. Check `docker ps | grep dumbo`.
- `success_count` high but flat, errors recent
  → it worked and then stopped. Check the container logs and the link, not the config.

## Checks (read-only)
    docker ps --filter name=dumbo
    curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/
