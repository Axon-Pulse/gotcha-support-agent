---
topics:
- deploy
- system
symptoms:
- the map is showing the wrong place
- we see targets that aren't there
- it's showing America / Texas / Dallas
- there are tracks but the radars are off
- the picture looks fake
---

# The UI shows invented tracks, often over Dallas, Texas

Entities moving around on the map when the sensors are switched off, or the whole picture
centred somewhere in the United States instead of their site. Sometimes they report it as
"the system is working but the location is wrong", which sounds like a configuration problem
and is not.

**The tell is the coordinates: latitude 32.7767, longitude -96.7970.** That is Dallas, Texas,
and it is hardcoded as the mock scenario's defense center in
`GUItcha30/gateway/src/main.py:191-193`. If a customer is looking at Dallas, nothing they are
seeing is real.

## Evidence

- `py_glue or protobuf not available`
- `Using MockDataProvider (py_glue not available, auto-fallback)`
- `Dallas Defense Center`

## Root cause

`GUItcha30/gateway/src/adapters/glue/__init__.py:158` catches *any* failure importing
`py_glue` or its protobuf modules and logs a single warning:

```
py_glue or protobuf not available: <the real error>
```

It then sets `GLUE_AVAILABLE = False`. What happens next depends on `data_provider.mode`:

- `auto` — silently falls back to `MockDataProvider`. **This is the dangerous one**, because
  the system comes up looking entirely normal.
- `ecal` / `glue` — raises, and the gateway fails loudly. This is the safe configuration for
  a real site.

The underlying import failure is usually one of: the wheel was never installed in the
gateway's venv, the wheel is stale against the current protobuf files, or the image was
rebuilt without the py_glue build step succeeding.

## Checks (read-only)

```
make logs SERVICE=gateway 2>&1 | grep -i "py_glue or protobuf not available"
```

One hit confirms it. The gateway could not load `py_glue`, fell back to synthetic data, and
has been generating a fake picture ever since — while reporting itself perfectly healthy.

## Fix

First get the real error — the warning line carries it after the colon:

```
make logs SERVICE=gateway 2>&1 | grep -A2 "py_glue or protobuf not available"
```

Then, depending on what it says:

- **`No module named 'py_glue'`** — the wheel is not installed in the gateway venv. On a
  deployed machine this means the image is broken; pull a known-good tag rather than trying
  to repair it in place. `make rollback TAG=<previous good tag>` then `make up`.
- **An `AttributeError` or a protobuf version complaint** — the wheel is stale. Same fix:
  roll back to a tag where it worked, and report the bad tag so it can be rebuilt.
- **It imports but some features are missing** — that is not this playbook; see
  `old-wheel-partial-degradation`.

**Then stop it happening silently again.** On a real site, `data_provider.mode` should not be
`auto`. Set it to `glue` in `gateway-config/config.yaml` so a broken wheel takes the gateway
down loudly instead of quietly inventing a picture:

```
make restart SERVICE=gateway
```

## Verify

```
curl -sS -m 5 http://<machine>:8080/health
```

`data_provider_available` must be `true`. Then confirm the map is centred on the customer's
actual site and that switching a radar off makes tracks stop appearing.

## If that didn't work

- If the defense center is wrong but the data is real, this is not mock fallback — the site's
  `spatial.defense_center` was never set. `make new-site` writes a Tel Aviv placeholder with
  a `# <-- SET THIS` marker and nothing validates that anyone changed it.
- If some panels are populated and others are empty, the wheel loaded but is missing newer
  features — `old-wheel-partial-degradation`.
- If the gateway will not start at all after switching off `auto`, you have converted a silent
  failure into a loud one, which is progress. The error it now prints is the real problem.
