---
topics:
- acoustic
symptoms:
- the acoustic sensor still shows degraded after the backend was started
- acoustic is degraded but detections are flowing
- ASU degraded after recovery
- the backend is back but the GUI still shows a warning for the acoustic node
---

# ASU stays DEGRADED after its backend comes back

First seen on `axon-gotcha-4`, 2026-10-01 (release `v1.4.0-61-g4de8ac64`), right after the
backend was started following a reboot (see `asu-backend-never-reachable`). Fixed by
restarting the `asu1` node. Confirmed by the PM.

## Root cause

`AsuNode::evaluateNodeHealth` returns DEGRADED when `error_count_ / request_count_ > 0.2`
(`src/nodes/asu_node/asu_node.cpp:471-475`). It also returns DEGRADED when the node's overall
error count is over 50 (`:479`) or its remote error count is over 50 (`:484`). These counters
run from the moment the node starts and are never reset. While the backend was down the node
failed about one request per second, so after the backend is back the ratio takes hours to
fall under 20%, and the error counts never fall. The status stays DEGRADED while detections
flow normally. It is leftover history, not a current fault.

## Evidence

- the backend answers: the triage's `acoustic backend:` line says something listens, and the
  configured URL returns a normal HTTP code
- gateway log shows acoustic detections being processed
- core log: `asu-backend-never-reachable` lines from before the backend started, none since
- the GUI shows acoustic **degraded**, not offline or critical

## Fix

1. Check `end_on_first_complete` in the site config. If it is `true`, one node restart resets
   everything (`end-on-first-complete-resets-system`).
2. Restart only `asu1`: C2 UI Node Health window, or `keyboard_node` `r`. Not `make restart`.

## Verify

`asu1` shows operational in the GUI.

## If that didn't work

Look for `ASU processing restarted (start_time ...) re-running bring-up` and `ASU state: unknown`
in the core log, about every 6 seconds. That is a separate backend fault, not leftover
counters. The v1.4.0 source has no match for that message, so read the backend's own log:
`scripts/remote_logs.sh <host> dumbo 100` (container name may differ).
