---
topics:
- system
symptoms:
- one part of the system just stopped
- it disappeared without any error
- something died overnight
- we lost one sensor but the rest is fine
- it keeps dropping out
---

# A node disappeared without an error message

One component stops — a single sensor, or one processing node — while everything else keeps
running normally. There is no error dialog and often nothing obvious in the logs, because the
node that died is by definition not the one writing messages about it.

## Evidence

- `heartbeat timeout (last seen`
- `Failed to exec`
- `Failed to change directory to`
- `Node not responding`
- `Node experiencing critical issues`

## Root cause

Every node publishes a heartbeat once per second. The launcher watches them and declares a
node dead after `HEARTBEAT_TIMEOUT_MS = 5000` (`src/launcher/system_launcher.h:231`), logging:

> `Node <name> heartbeat timeout (last seen <N>ms ago)`

and publishing a `CRASHED` event. So the launcher always knows. Whether *you* find out
depends on where that output went — in the default aggregated backend it is interleaved with
every other node's output, which is why it gets missed.

How the process ended is classified from its exit status:

| What happened | Reported as |
|---|---|
| Exited 0 | `EXITED_NORMAL` |
| Exited non-zero | `EXITED_ERROR` + a crash event |
| Killed by SIGTERM | `EXITED_NORMAL` (a deliberate stop) |
| Killed by SIGKILL | `KILLED` — **frequently the OOM killer** |
| Any other signal | `CRASHED` |

Two specific cases worth recognising immediately, both from
`src/launcher/process/managed_process.cpp` and both meaning the node never actually ran:

> `Failed to exec <cmd>: <reason>` — the binary or interpreter is not where the config says
> `Failed to change directory to <dir>: <reason>` — the working directory does not exist

Either produces exit code 127 and looks exactly like a crash, but the node never started at
all. That is a config or image problem, not a runtime one.

## Checks (read-only)

The launcher notices within five seconds and says so. That message is what you want:

```
make logs SERVICE=gotcha30 2>&1 | grep -i "heartbeat timeout\|CRASHED\|Failed to exec"
```

## Fix

**Find out how it died before restarting it.** A restart destroys the evidence and, if this is
a memory or disk problem, buys only minutes.

```
make logs SERVICE=gotcha30 2>&1 | grep -B10 "heartbeat timeout" | tail -40
```

Then, by case:

- **`Failed to exec` / `Failed to change directory`** — the node never ran. Check the resolved
  command: `docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config`.
  The `argv` line shows exactly what it tried to run.
- **`KILLED`** — suspect memory. Check `dmesg -T | grep -i "killed process"` on the host and
  look at free memory and disk:
  ```
  df -h; free -h; ls /dev/shm/ | grep -c ecal
  ```
  A large number of leftover `/dev/shm/ecal_*` segments points at repeated unclean shutdowns.
- **`CRASHED` with a real signal** — the node's own stderr just before the timeout carries the
  reason. Python nodes print tracebacks there.
- **Recurring on a cycle** — if it dies at the same time each day, look at anything scheduled:
  log rotation, backups, or the recording segment rotation.

Then restart just that node rather than the whole system. The launcher exposes runtime control
over eCAL via the keyboard node:

```
./build/bin/keyboard_node      # l = list nodes and status, r = restart, t = start
```

## Verify

Watch it stay up for longer than it did before — and confirm no new heartbeat timeout:

```
make logs SERVICE=gotcha30 2>&1 | grep -i "heartbeat timeout" | tail -5
```

For a sensor node, confirm real data resumes rather than just that the process is alive; a
sensor node that cannot reach its hardware stays up forever delivering nothing
(`radar-connected-but-no-detections`).

## If that didn't work

- If the whole container is cycling rather than one node, this is the wrong level — see
  `container-up-but-nonfunctional`.
- If the node exits immediately every time, it is very likely a config failure at startup;
  run `--print-config` and check for `FATAL CONFIG` in the logs.
- If a node you expected is absent and never appeared at all, it may not be in the resolved
  config — `override-silently-ignored`.
- Logs are capped at roughly 50 MB × 3 per service, so on a busy system the original failure
  may already be gone. In that case stop the service, start it, and capture the failure live.
