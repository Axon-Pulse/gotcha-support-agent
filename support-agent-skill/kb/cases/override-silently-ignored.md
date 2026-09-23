---
topics:
- config
symptoms:
- we disabled it but it's still running
- we changed the config and it made no difference
- we removed that sensor but it still shows up
- the setting isn't being picked up
---

# A config change was ignored, and the old setting is still running

Someone edited the site's config to change, disable, or remove a node — and the system came
up behaving exactly as before. No error, no warning in the obvious place, and the launcher
started cleanly. The natural conclusion is that the edit didn't save or the wrong file was
edited, so people re-do the same edit and get the same result.

## Evidence

- `declares no runnable node (no 'type' or 'cmd' anywhere beneath it)`
- `its key shape does not match the config it extends, and the override is being ignored`

## Root cause

Node keys in these configs are **path-shaped compound keys**, not nested YAML. The reference
set writes them like this:

```yaml
nodes:
  /sensors/optic:
    meduza:
      ...
```

`/sensors/optic:` is one key. Writing it as nested mappings —

```yaml
nodes:
  /sensors:
    optic:
      meduza:
        ...
```

— builds the same *node path* but a **different key in the tree**. The merge is by key, so
your override matches nothing in the parent config. It doesn't fail; it just adds a dead
branch alongside the inherited node, and the inherited node carries on running with its
original values.

That failure points the wrong way: a node you meant to remove stays up, which reads as "the
system ignored me" rather than "my key was misspelled". The launcher does say so, at
`src/launcher/config/config_loader.cpp:319-323`:

> `'<path>' declares no runnable node (no 'type' or 'cmd' anywhere beneath it). If this was
> meant to override an inherited node, its key shape does not match the config it extends,
> and the override is being ignored.`

but it is a `WARN` in a wall of startup output, so in practice nobody sees it.

## Checks (read-only)

Ask the launcher what it actually resolved, without starting anything:

```
./build/bin/system_launcher --config configs/<site>/full_system.yaml --print-config
```

This prints every node it would run, with its type, whether it is enabled, the config file it
reads, and its full argv — and it does it without eCAL and without spawning a single process,
so it is completely safe on a live machine. **Compare that output against what the customer
thinks they configured.** The disagreement is the answer.

Then look for the warning, which goes to stderr:

```
./build/bin/system_launcher --config configs/<site>/full_system.yaml --print-config 2>&1 >/dev/null
```

## Fix

Match the key shape of the config you are extending, exactly. Open the parent —
`configs/default/processing.yaml` or `configs/default/full_system.yaml` — and copy the key
verbatim, including the leading slash and any slashes inside it.

```
grep -n '^  /' configs/default/full_system.yaml
```

Those are the real key shapes. Your override file must use the same ones.

Then confirm before restarting anything:

```
./build/bin/system_launcher --config configs/<site>/full_system.yaml --print-config
```

## Verify

`--print-config` shows the node with your values, or absent if you were removing it, and the
warning is gone. Nodes are printed sorted by path specifically so you can diff two systems or
two revisions line by line:

```
./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
```

## If that didn't work

- A node config file named as a bare filename (`tracker.yaml`) resolves in the launched
  config's directory first, then falls back to `configs/default/`. If you created an override
  file in the wrong directory it will be silently ignored in favour of the default.
- `node_name` set in YAML is discarded — the launcher injects `--node-name` from the tree key.
- If the override is being applied but rejected, you would see a `FATAL CONFIG` with an
  aggregated list of errors instead — that is a validation failure, a different problem,
  and it is loud.
- Values pinned by a site can be listed with `make show-overrides SITE=<site>`. Note this
  needs PyYAML on the host and the script is missing from some provisioning paths.
