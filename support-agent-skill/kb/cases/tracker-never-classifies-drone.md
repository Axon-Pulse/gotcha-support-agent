---
topics:
- config
- radar
symptoms:
- it detects things but never says what they are
- everything is unknown
- the classification isn't working
- we get tracks but no drone alerts
---

# Tracks appear but nothing is ever classified as a drone

The system tracks things perfectly well — targets appear, move, and persist — but the
classification column stays empty or everything reads as unknown. Because tracking works,
this gets reported as a tuning or sensitivity problem, and people start adjusting thresholds.
No threshold will help: the classifier is not running at all.

## Evidence

- `load_model: failed to load model`
- `bundle has neither 'classifier' nor 'model'`
- `load_model: no model name configured; nothing loaded.`

## Root cause

`python/models/loader.py` ends with:

```python
    except Exception:  # noqa: BLE001 — degrade to no-op on any failure
        logger.exception("load_model: failed to load model '%s'", name)
        return None
```

**Any** failure — missing file, unreadable file, wrong bundle format, a missing `torch` for
the CNN architecture — is caught, logged once, and turned into `None`. The classifier stage
then has no scorer, `ready` is `False`, and every call returns `None`.

So the pipeline runs end to end with a hole in it. `classifier.enabled` is `true`, the config
validates, the node reports healthy, tracks flow — and the classification stage is a no-op.
The only trace anywhere is a single `logger.exception` line emitted once when the node
started, which on a machine that has been up for a week is long gone from the rotated logs.

This is deliberate: a missing model degrades the system rather than taking it down. That is
the right call for a live site and it is also why this is so easy to miss.

## Checks (read-only)

The model weights are **gitignored and not in the repo** — they are supplied at deploy time.
So the first question is simply whether the file exists on this machine:

```
ls -la python/models/weights/
```

An empty or missing `python/models/weights/` directory is the whole answer. The configured
default is `radar_td_time_xgb_peakalign` (`configs/default/tracker.yaml:396`), which expects
`python/models/weights/radar_td_time_xgb_peakalign.pkl`.

Then confirm from the logs, at tracker startup:

```
make logs SERVICE=gotcha30 2>&1 | grep -i "load_model"
```

## Fix

Put the weights on the machine. They are deploy-provided artefacts, not something to rebuild
in the field:

```
ls python/models/weights/          # what's there now
# copy the .pkl for the configured model into python/models/weights/
make restart SERVICE=gotcha30
```

If the file is present and it still fails, read the actual exception — it is on the line
after the `load_model: failed to load model` message and it distinguishes the cases:

- `bundle has neither 'classifier' nor 'model'` — the file is a pickle, but not one this
  loader understands. Wrong artefact, or an older/newer bundle format.
- `No module named 'torch'` — the config selects the CNN architecture (`radar_td_cnn`), which
  imports torch at module level. **Torch is not in `requirements-runtime.txt`**, so it is not
  on field machines. Either switch the configured model to one of the XGBoost variants, or
  this machine cannot run that architecture.
- A file-not-found on a path you did not expect — the site config may be pointing at a
  different model name. Check with `--print-config`.

## Verify

Restart and watch for the success line rather than the failure:

```
make logs SERVICE=gotcha30 2>&1 | grep "load_model: loaded model"
```

Then confirm classifications actually appear in the UI for a real target.

## If that didn't work

- Confirm which model the site is actually configured for — a site override may have changed
  it from the default: `docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config`
- If tracks themselves are wrong rather than unclassified, this is the wrong playbook.
- If nothing at all reaches the tracker, start at `radar-connected-but-no-detections`.
- The tracker rejects unenriched detection frames from old recordings with a message about
  `native u/v/r and sourceLocation.heading/.tilt are required` — that is a different failure
  and it is loud.
