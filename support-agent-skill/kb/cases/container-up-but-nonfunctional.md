---
topics:
- deploy
- system
symptoms:
- it says everything is up but nothing works
- we restarted it and it still doesn't work
- the status looks fine
- no errors, just no data
---

# Everything says it is running, and nothing works

`make status` shows every service `Up`. Nothing is obviously crashing. And the system does
nothing useful. The customer has usually already restarted it two or three times before
calling, which is why they are frustrated — restarting produces exactly the same reassuring
output.

## Evidence

- `restarting (1)`
- `HTTP/1.1 503 Service Unavailable`

## Root cause

Two things combine.

**There is no `HEALTHCHECK` anywhere** — not in the `Dockerfile`, not in
`docker-compose.yml`. Docker therefore has exactly one notion of health: *did the process
exit?* A gateway that started, failed to load its data provider, and is now serving 503s
forever has not exited, so it is `Up`.

**Every service is `restart: unless-stopped`.** A container that starts, fails, and dies is
restarted immediately. If it survives a few seconds each time, `docker compose ps` will
usually catch it mid-life and report it as running. The crash loop is invisible unless you
look at uptime or restart counts.

Together: `make status` reports process liveness, and the customer reads it as system health.
Those are not the same thing and on this system they are frequently opposite.

## Checks (read-only)

**Do not trust `make status`.** Ask the gateway how it actually feels:

```
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://<machine>:8080/health
```

- `200` — the gateway is genuinely healthy. The problem is upstream of it: sensors, the
  launcher, or the core. Go to `radar-connected-but-no-detections` or `node-died-no-message`.
- `503` — the gateway is running but has no working data provider. Go to
  `ui-shows-tracks-over-dallas`.
- connection refused — go to `gateway-bound-to-loopback`.

Then look for a container that is quietly cycling:

```
make status
```

A `Restarting` state, or an uptime that keeps resetting to seconds, means a crash loop
dressed up as a healthy service.

## Fix

There is nothing to fix here as such — this playbook exists to stop you wasting twenty
minutes trusting the wrong signal. Once triage tells you which component is actually
unhealthy, go to that component's playbook.

If a container is crash-looping, get the reason before it scrolls away:

```
make logs SERVICE=<name> 2>&1 | tail -100
```

Note that logs are capped at roughly 50 MB × 3 per service, so on a machine that has been
looping for days the original failure may already be gone. In that case stop the service,
start it, and watch it fail once cleanly:

```
make down SERVICE=<name>
make up   SERVICE=<name>
make logs SERVICE=<name>
```

## Verify

`GET /health` returns 200, and `make status` shows uptimes that keep increasing rather than
resetting.

## If that didn't work

- If `make up` fails with an image-not-found error, note that `make up` passes `--no-build`
  unconditionally — it will never build anything. On a machine that has never pulled or built
  an image you need `make pull` first.
- `make mock` is documented as "gateway + frontend only (no sensor hardware)" but does not
  pass `--no-deps`, and `gateway` declares `depends_on: [gotcha30]` — so it starts the sensor
  core too. If you used `make mock` expecting an isolated test, you did not get one.
- If the machine was rolled back recently, confirm the tag it landed on actually exists —
  `make rollback` writes whatever tag you give it into `.env` without validating it.
