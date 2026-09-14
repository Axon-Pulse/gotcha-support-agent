# Reading node health correctly

## Status names differ between the core and the gateway
The proto enum is `UNKNOWN | HEALTHY | DEGRADED | CRITICAL | OFFLINE`.
The C2 gateway renames `HEALTHY` to the string **`LIVE`**. They are the same state.

## OFFLINE does not mean the process is dead
`store.py` flips a node to `OFFLINE` after `health_timeout_seconds = 10.0` — that means
**no health message arrived in 10 s**, nothing more. The process may be running fine with
a broken eCAL transport. Always confirm with `get_process_table` before saying a node
crashed: if the launcher reports `PROC_RUNNING` with a long uptime while health says
`OFFLINE`, the fault is in the transport between them, not at either end.

## Three different error counters share names
- `errors` in a health row is `common.error_count` — the canonical one.
- `salient.error_count` is a node-specific counter with its own meaning.
- `salient.asu_error_count` is different again.
Do not add them together or assume they agree.

## Duplicate instances
The gateway stores health keyed by `node_id`, so if two launcher sessions are running the
second overwrites the first and the node looks like it is flapping. `get_system_health`
splits them into separate rows (`instance: 1 of 2`) by uptime; `get_ecal_topology`
confirms it via disjoint PID groups publishing identical topic sets.
Symptom: the same node reporting uptimes an order of magnitude apart (e.g. 30 s and
26,690 s) in one capture.
