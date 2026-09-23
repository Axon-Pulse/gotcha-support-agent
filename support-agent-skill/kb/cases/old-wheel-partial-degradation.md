---
topics:
- config
- system
symptoms:
- the events list is always empty
- some panels work and some don't
- the sensor status page is blank
- history doesn't show anything
- it mostly works but one part is missing
---

# Some UI panels are blank while the rest of the system works

The system is broadly working — tracks appear, the map is live, the picture is real — but one
specific area is permanently empty. Events never populate. Or the sensor status panel shows
nothing. Or scan-range telemetry never draws. Everything else is fine, which makes it look
like a UI bug or a permissions problem.

It is a version mismatch: the gateway is running against a `py_glue` wheel older than the
code that expects those features.

## Evidence

- `py_glue events unavailable (requires py_glue >= 0.6.0`
- `py_glue sensor contract unavailable (requires a wheel with`
- `py_glue scan-range telemetry unavailable (requires a wheel with`
- `py_glue wheel lacks event media support (needs >= 0.7.0)`

## Root cause

`GUItcha30/gateway/src/adapters/glue/__init__.py` wraps optional wheel features in separate,
nested `try/except` blocks rather than one big one. Three of them:

- **events** (`:94`) — `py_glue events unavailable (requires py_glue >= 0.6.0 with Event_pb2
  and event topics); event stream + history disabled`
- **sensor contract** (`:120`) — `py_glue sensor contract unavailable (requires a wheel with
  Sensor_pb2 and /sensors topics); sensor entity disabled`
- **scan range** (`:144`) — `py_glue scan-range telemetry unavailable (...); scan range
  disabled`

This structure is deliberate and it is the right design. If these were inside the top-level
import guard, an old wheel would knock out the *entire* provider and the gateway would fall
back to mock data — meaning a real site would silently start drawing synthetic tracks. See
`ui-shows-tracks-over-dallas` for what that looks like. Degrading one feature is enormously
better than that.

The cost is that the failure is quiet and partial, and presents as a UI fault rather than a
version problem.

There is a related one worth knowing, with a much more specific message, for manual
beamforming:

> `py_glue BeamFormRequest proto has no request_id field — the installed wheel predates the
> ASU beamform branch. Rebuild it from gotcha30 (python -m build) and reinstall. Manual
> beamforming is disabled until then.`

## Checks (read-only)

```
make logs SERVICE=gateway 2>&1 | grep -i "unavailable"
```

Each missing feature announces itself once at startup, and the message names the minimum
version it needs.

## Fix

The wheel and the gateway code ship in the same image, so a mismatch inside a single image
tag is a build problem, not a site problem. Check what the machine is running:

```
grep IMAGE_TAG .env
./build/bin/system_launcher --version
```

Then move it to an image where the wheel and the code agree:

```
make pull
make up
```

If the machine is already on the newest tag and the features are still missing, the image
itself was built with a stale wheel. Roll back to a known-good tag and report it:

```
make rollback TAG=<previous good tag>
make up
```

Do not try to install a wheel by hand inside a running container — it will not survive a
restart and it makes the machine's state undiagnosable.

## Verify

```
make logs SERVICE=gateway 2>&1 | grep -i "unavailable"
```

should return nothing. Then confirm the specific panel the customer reported now populates.

## If that didn't work

- If `/api/v1/events` returns **503** with `event history unavailable`, that is this problem.
  An empty **200** means the service is fine and there genuinely are no events — the API
  distinguishes those two deliberately, so believe it.
- If *every* panel is empty rather than one, the wheel did not load at all —
  `py-glue-stale-or-missing`, or mock fallback via `ui-shows-tracks-over-dallas`.
- Individual service clients are also wrapped one by one, logging `Could not create <x>
  service client`. If a single command or button fails rather than a whole panel, grep for
  that message instead.
