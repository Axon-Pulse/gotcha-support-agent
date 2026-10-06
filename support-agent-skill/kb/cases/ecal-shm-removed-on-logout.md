---
topics:
- system
- deploy
symptoms:
- the recorder is eating all the CPU
- every core is at 100% and it gets worse over time
- the machine is churning CPU after someone logged out
- restarting fixed it, then it came back
- recordings are tiny / empty but it says it's recording
- it started some time after I disconnected from SSH
---

# eCAL shared memory deleted when the last login session of the stack's user ends

First seen on `axon-gotcha-3` and a test Raspberry Pi, 2026-10 (release v1.4.0 compose). Found
after hours of hunting; fixed in a minute with a logind setting. The fault is in the OS and
eCAL, not in gotcha code. It shows up only some time **after** a logout, which makes it very
hard to connect cause and symptom.

## Root cause

Ubuntu's `systemd-logind` defaults to `RemoveIPC=yes`. When a user's **last** login session
ends (SSH or any other), logind deletes every POSIX shared-memory file that user owns in
`/dev/shm`. Root and system users (UID < 1000) are exempt.

The core container runs as the user that ran `make init`: `user: "${HOST_UID}:${HOST_GID}"`,
with `ipc: host` (`docker-compose.yml:31,33`, and `HOST_UID=$(id -u)` in `scripts/init.sh:24-26`).
So eCAL's segments are files in the **host's** `/dev/shm`, owned by that user (e.g. `gotcha`).
Logging out of the last `gotcha` session deletes them from under the running stack.

- Nodes that are already running keep working, because they still hold the old mappings.
- The recorder is restarted on every segment rotation (`checkSegmentRotation`,
  `src/nodes/data_manager/data_manager_node.cpp:532-574`). The new `ecal_rec` can't connect to
  the deleted segments. Because of a busy-loop in eCAL's connect path, it burns one full core per
  failed connection, and the load climbs until the machine is saturated.
- `ecal_rec`'s own stdout/stderr go to `/dev/null` (`ecal_recorder.cpp:85-91`), so the failure
  is never logged. The data manager still reports RECORDING.
- Segments made after the logout are a few KB instead of hundreds of MB.

Not affected:
- deployments where `make init` ran as root (`HOST_UID=0`)
- machines where someone always stays logged in as the stack's user (nevatim), because that
  user's last session never ends

## Evidence

- `ecal_rec` at far more than 100% CPU (`top`, or the triage's `ecal_rec processes` line)
- core log: `Recording validated: N files, M channels, X bytes`, where X is KB-sized
  (`[ecal-shm-removed-on-logout]` signature, which matches under 1 MB). Normal segments
  (`segment_duration_min`, 10 min on axon-gotcha-3) are hundreds of MB
- triage `resources`: `logind RemoveIPC: yes` and `core container user: uid 1000` (any UID
  >= 1000), flagged `AT RISK`
- `/dev/shm ecal segments` low or 0 while nodes are running
- `last` / `journalctl -u systemd-logind` shows a session of that user ending before the load
  started

The `AT RISK` line is a finding even on a healthy machine. Logging out of the stack's user will
trigger the fault later.

## Checks

```
busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager RemoveIPC
docker inspect -f '{{.Config.User}}' $(docker ps -qf label=com.docker.compose.service=gotcha30)
ps -C ecal_rec -o pid,etimes,pcpu,nlwp,args
ls -la /dev/shm | grep -i ecal
```

## Fix

On the host, with sudo:

```
# the drop-in directory doesn't exist by default on Ubuntu/Debian
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nRemoveIPC=no\n' | sudo tee /etc/systemd/logind.conf.d/10-gotcha-keep-ipc.conf
# applies the setting; existing sessions are not disturbed
sudo systemctl restart systemd-logind
```

Then restart the stack (`make restart` in the gotcha directory). The segments are already gone,
and only a restart recreates them. Restarting just the recorder is not enough, because the
publishers' segments are missing too.

Apply this on **every** machine where the stack runs as a non-root user, not only the one that
showed the fault.

## Verify

- `busctl get-property ... RemoveIPC` prints `b false`
- after the restart, log out of every session of that user, wait at least one segment rotation,
  then check `ecal_rec` CPU is back to its usual one core at most
- the next `Recording validated` line shows hundreds of MB

## If that didn't work

- `RemoveIPC` still `b true`: another drop-in or `/etc/systemd/logind.conf` overrides it, or
  the file name sorts earlier than the overriding drop-in. Check with
  `systemd-analyze cat-config systemd/logind.conf`.
- CPU still saturated with `RemoveIPC` false and a fresh stack: this is a different recorder
  problem. Escalate, with `ps -C ecal_rec -o pid,etimes,pcpu,nlwp,args` and the
  `Recording validated` lines attached.
