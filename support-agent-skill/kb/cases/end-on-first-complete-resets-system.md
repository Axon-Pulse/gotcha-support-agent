---
topics:
- system
- deploy
symptoms:
- the system did a reset while I was looking at the GUI
- I restarted one node and everything restarted
- the whole system reset when we restarted a sensor
- we moved the tower and the system reset
- the map went blank for a few seconds and came back
- is it the docker or something else
---

# One node restart resets the whole system (`end_on_first_complete: true`)

The operator restarts a single node, or repositions a platform in the GUI, and the whole
system resets. Every node restarts, the GUI blanks and comes back, and the core container's
restart count goes up. It looks like a Docker crash or a machine fault, but it isn't one. The
launcher is doing exactly what the site config tells it to do.

First seen on axon-gotcha-5, 2026-09-23. There were three resets in eight minutes: two
operator restarts of one node, and one tower reposition from the GUI.

## Evidence

- `First node completed:`
- `Initiating shutdown: First node completed`
- `Executing restart for <node>: Operator restart`
- `Executing restart for <node>: Platform <id> repositioned` (routine on its own: every operator
  reposition logs it. Only a fault when `First node completed:` follows, so the triage signature
  uses the first line only)

## Root cause

`session.end_on_first_complete` is meant for test runs: "stop all nodes when the first node
completes". Its default is `false` (`configs/default/full_system.yaml`, schema default). A site
`full_system.yaml` that sets it to `true` turns every node exit into a full system shutdown.

The launcher can't tell a node that finished from a node it is restarting itself:

- A restart (`handleRestartNode`, `src/launcher/system_launcher.cpp`) stops the old process
  with SIGTERM. The process monitor records that exit as the *first exited* node
  (`process_manager.cpp`, `first_exited_name_`), whatever the reason for the stop.
- On its next loop `onLoop()` sees `end_on_first_complete && hasAnyExited()`, logs
  `First node completed: <node>` and stops the whole session. The replacement process it just
  spawned is killed along with everything else.
- The launcher then exits with code 0. Compose has `restart: unless-stopped`, so Docker starts
  the container again. In the dockerd journal this shows as `restarting container ... exitCode=0
  manualRestart=false`, which is why it looks like a Docker problem.

What triggers it:

| trigger | log line before the shutdown |
|---|---|
| operator restarts a node (GUI or `keyboard_node` `r`) | `Executing restart for <node>: Operator restart` |
| operator repositions a platform in the GUI (`CMD_SET_PLATFORM_POSE`) | `Received command: 7 target=<platform> source=c2_gateway reason=Operator repositioned platform <platform>`, then `Executing restart for <node>: Platform <platform> repositioned` for **every node on that platform** |
| a node really exits or crashes on its own | `Process exited: <node> ...` with no `Executing restart` line just before it |

The last row is the only one that isn't operator-driven. There the flag hides the real fault:
one crashing node takes the whole system with it. Go to `node-died-no-message` for that node.

## Checks (read-only)

Is the flag set on this site?

```
grep -n 'end_on_first_complete' configs/<site>/full_system.yaml
```

What set off each reset? The container's json log can be partly corrupted after an unclean
shutdown (dockerd `Error decoding log file`), and then `--since` stops reading early. Read from
the tail instead:

```
docker logs --tail 30000 -t gotcha-gotcha30-1 2>&1 | grep -a -e 'Executing restart' -e 'repositioned' -e 'First node completed'
```

Confirm the container is restarting itself (exit 0, policy restart) and not being stopped by
someone. A `make restart` shows as `kill` on all three containers at once:

```
docker events --since 1h --until 0s --filter type=container --format '{{.Time}} {{.Actor.Attributes.name}} {{.Action}} {{.Actor.Attributes.exitCode}}'
```

## Fix

Turn the flag off in the site config. A field system must never end its session because one
node exited. This is a one-line config change, not a source edit:

```
# configs/<site>/full_system.yaml
session:
  title: "<site>"
  end_on_first_complete: false
```

Deleting the line works too, because the default is `false`. Setting it explicitly to `false`
documents the choice. Then:

```
make restart SERVICE=gotcha30
```

This restart resets the system once more, so do it when the operator is ready.

Also report it to engineering. A new site config should never ship with this flag set to
`true`, and whoever created this one probably copied it from a test config
(`docs/launcher/user_guide.md` and `docs/launcher/troubleshooting.md` both show examples
with `true`).

## Verify

Restart a single node (or reposition the platform) and watch the container:

```
docker inspect -f 'restarts={{.RestartCount}} started={{.State.StartedAt}}' gotcha-gotcha30-1
docker logs --tail 200 gotcha-gotcha30-1 2>&1 | grep -a -e 'Restarted node' -e 'First node completed'
```

You should see `Restarted node: <node>` and no `First node completed`, and the restart count
and start time should not change.

## If that didn't work

- The flag is `false` but the whole system still resets. Check the resolved config: the value
  may be coming from somewhere you didn't edit (`override-silently-ignored`), or the running
  launcher is using a different `--config` than the file you changed. Compare with the
  `launcher:` line in triage.
- The container still exits and the log ends with `All processes exited`: every node died on
  its own. That's a different fault. Start at `node-died-no-message`.
- The resets line up with no `Executing restart` line at all: a node really is exiting on its
  own, and the flag was only amplifying it. Find that node's own failure.
