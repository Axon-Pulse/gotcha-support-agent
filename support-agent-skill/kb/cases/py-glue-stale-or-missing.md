---
topics:
- config
- system
symptoms:
- it won't start at all
- it worked before we updated
- the processing nodes are dead
- we rebuilt it and now nothing runs
---

# Python nodes will not start — py_glue is missing or stale

Python nodes — tracker, target, shadow, spatial, event_manager — fail immediately at startup.
Depending on the terminal backend this shows as nodes that never appear, or as the launcher
reporting processes that exited straight away. On a dev machine it usually follows a `git
pull`; on a field machine it follows an image update.

Unlike most failures in this system, **this one is loud**. That is a good sign: the error
message names the fix.

## Evidence

- `py_glue.configmerge is missing — the compiled extension is absent or stale`
- `needs the transport bindings (py_glue.so), which failed to import`
- `ModuleNotFoundError: No module named 'py_glue'`

## Root cause

**`py_glue.configmerge is missing — the compiled extension is absent or stale.`**

Raised at import time by `python/common/config.py:27-30`. Every Python node loads its config
through `py_glue.configmerge`, which is a compiled binding shared with the C++ launcher
specifically so that the two can never disagree about how configs merge. If the compiled
`.so` is absent, or is an older build that predates `configmerge`, the import raises and the
node cannot start. It fails at import rather than at first use, deliberately — a node that
started and then merged configs differently from the launcher would be far worse.

**`py_glue.<name> needs the transport bindings (py_glue.so), which failed to import: ...`**

Raised lazily at first attribute access. The protobuf-only parts of the package imported
fine, but the compiled transport did not. This is deferred on purpose so that tools which
only need the generated protobuf modules still work without a full build.

Both mean the same underlying thing: the compiled extension on this machine does not match
the Python code on this machine.

## Checks (read-only)

```
make logs SERVICE=gotcha30 2>&1 | grep -i "py_glue"
```

You are looking for one of two distinct messages, which mean different things.

## Fix

**On a development machine**, rebuild and reinstall the extension from the repo root:

```
pip install -e .
```

If that fails at the CMake configure step complaining about pybind11, the submodule was never
initialised — this is the single most common source-build failure:

```
git submodule update --init --recursive
pip install -e .
```

**On a field machine, do not build anything.** The extension is baked into the image, so a
mismatch means the image is broken or the machine is running an image it was not meant to.
Check what it is on:

```
grep IMAGE_TAG .env
make status
```

Then roll back to a tag that worked and bring it up:

```
make rollback TAG=<the previous good tag>
make up
```

Report the bad tag — an image that ships a stale extension is a build problem, not a site
problem, and fixing it in the field just hides it.

## Verify

```
make logs SERVICE=gotcha30 2>&1 | grep -i "py_glue"
```

should return nothing, and the Python nodes should stay up. Confirm with `make status` that
uptimes are increasing rather than resetting.

On a dev machine:

```
python -c "from py_glue import configmerge; print('ok')"
```

## If that didn't work

- If `py_glue` imports but only *some* gateway features are missing, the wheel is older than
  the code rather than broken — see `old-wheel-partial-degradation`.
- If the gateway specifically cannot see it while the core nodes are fine, remember the image
  carries two separate virtualenvs and the wheel is installed into both. A failure in only one
  of them points at the image build, not the machine.
- `make rollback` writes whatever tag you give it into `.env` without checking it exists. If
  the machine gets worse after a rollback, check the tag is real before anything else.
