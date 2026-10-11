---
topics:
- general
symptoms:
- a node keeps flapping between healthy and critical
- the same node reports two very different uptimes
- duplicate detections
---

# Two launcher sessions running at once

Captured in `tests/fixtures/` — `health_faulty.txt` and `topics_faulty.txt` are one
recording of this fault.

## Root cause

A second `system_launcher` was started without stopping the first, so every node ran
twice. Health is stored per `node_id`, so the two instances overwrote each other and the
node appeared to flap. Nothing was wrong with any node.

## Checks (read-only)

    # health: the same node with uptimes an order of magnitude apart
    #   asu  uptime_s 30      instance 1 of 2
    #   asu  uptime_s 26690   instance 2 of 2
    #
    # topology: two disjoint PID groups publishing identical topic sets
    #   pids 291846 / 498063  -> /asu/beamform, /asu/detections, /asu/pseudo_spectrum
    #
    # launcher: more process rows than total_nodes
    #   total_nodes 4, but 8 rows returned

Any one of the three views is suggestive; together they are conclusive.

## Fix

Stop the older session — the one with the long uptime — and confirm the PID groups
collapse to one. Do not restart the node itself; it was never the problem.
