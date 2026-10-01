# How this system works

Read this when the symptom matches no recorded case. It describes the mechanism, so a
fault nobody has written up can still be reasoned about rather than guessed at. Recorded
cases are in `cases/`: grep their `symptoms:` and read the whole file before acting on it.

This is the skill's copy of the repo's `kb/system-model.md`, with the old bot's tool names
replaced by this skill's triage sections (named in quotes below). The mechanism facts come
from the code; when one changes in the original, change it here too.

## The shape of the system

A **system_launcher** reads one YAML config and spawns every node in it. Nodes talk over
**eCAL**, a pub/sub bus: there is no central broker and no request/response. A node that
is running but cannot reach the bus is invisible to everything except the launcher.

Three families of node:

- **Sensors** — radar (`magos_radar`, `elm2135_radar`), acoustic (`asu`), optics
  (`meduza_optic`, `python_optic_ptz`, `python_optic_verification`). They publish
  detections on their own topics.
- **Processing** — `python_tracker` fuses detections into `/Targets`;
  `python_event_manager` publishes `/events`.
- **Plumbing** — the launcher itself, and a C2 gateway that consumes health for the UI.

Every node publishes `/heartbeat` and `NodeHealth` on `/system/health`. The launcher
publishes `LauncherStatus` on `/launcher/status`.

**Health is a slow stream.** Each node publishes `/system/health` only every few
seconds — measured on a 6-node bench rig: tracker 4.0s, asu/magos/event_manager 5.0s,
c2_gateway 6.7s, system_launcher 8.0s, ~1.1 messages/second aggregate across all nodes.
`/heartbeat` is far faster (~3.6 Hz) and `/launcher/status` is ~1 Hz. This sets
how much a gap means: a node that has not yet reported is missing only because its turn had
not come round, so look at the node's latest status line in the "sensor node status" section
before calling it silent.

## Three independent views, and what each one cannot see

This is the most useful thing in this document. The views fail differently, so
disagreement between them localises a fault better than any single one.

| view | where in the triage | sees | blind to |
|---|---|---|---|
| node self-report | "sensor node status", "log tail", the gateway `/health` body | what a running node says about itself | a node that never started, or whose eCAL transport is broken |
| launcher truth | "launcher sessions + node processes", "containers", "resolved config" | every node the config says should exist, its process, uptime and restarts, and which config is running | anything about whether a running node is working |
| reachability | "sensor paths" (route, neighbour entry, and ping when it was run) | whether an address answers, **and which route would be used** | whether the responder is the intended device |

There is no fourth view of the eCAL bus (publishers and subscribers per topic) in this
triage. Say so rather than implying you checked the wiring.

A recorded case is found by its `symptoms:` and its signature, and has to be read whole: the
fix, its caveats and the "if that didn't work" branches come after the part a grep shows.

Consequences worth holding on to:

- **A node missing from health is not necessarily dead.** It may never have started, in
  which case only the launcher knows it should exist.
- **A node that reports health but whose data never reaches the UI** is a transport fault,
  not a node fault. The triage can't see the bus, so this is a conclusion from the node
  running with a long uptime while the UI shows it `OFFLINE`.
- **A subscriber with no publisher** means the node that should publish that topic is not
  running. This is how a missing node shows up downstream.
- The config the launcher was given (the `config:` line and the "resolved config" section)
  catches the case that is otherwise unknowable: you are debugging a different config from
  the one that is running.

## Health semantics

The proto enum is `UNKNOWN | HEALTHY | DEGRADED | CRITICAL | OFFLINE`. The C2 gateway
renames `HEALTHY` to **`LIVE`**; they are the same state, and a report using either word
is saying the same thing.

**`OFFLINE` does not mean the process is dead.** The gateway flips a node to `OFFLINE`
after `health_timeout_seconds = 10.0` — that means no health message arrived in ten
seconds, and nothing more. Always cross-check the launcher: `PROC_RUNNING` with a long
uptime alongside `OFFLINE` health means the fault is in the **transport between them**,
not at either end. This is the most common way a working node is reported as dead.

Process states are `PROC_NOT_STARTED | PROC_STARTING | PROC_RUNNING | PROC_TERMINATING |
PROC_EXITED_NORMAL | PROC_EXITED_ERROR | PROC_CRASHED | PROC_KILLED | PROC_STOPPED`.
Anything other than `PROC_RUNNING` makes a node suspect.

Three different counters are all called some variant of "errors": `common.error_count`
(the canonical one, surfaced as `errors`), a node-specific `salient.error_count`, and
`salient.asu_error_count`. They measure different things. Quote which one you read, and
never add them together.

## Duplicate instances mean two launcher sessions

Health is stored per `node_id`, so when two launcher sessions run at once the second
overwrites the first and a node looks like it is flapping. The signature is the same node
reporting uptimes an order of magnitude apart — 30 s and 26,690 s. The triage shows it as
`system_launcher processes: 2+` and the same node listed twice with very different uptimes.
The fix is to stop the older session; the node is not at fault.

## Reachability is not liveness

The "sensor paths" section gives each sensor address a route, a neighbour-cache state and, with
`--ping`, a ping result. Read them as four cases, and **two of them cannot support a
conclusion**:

- the ping answers over the direct path. Usable.
- no ping answer, and no route via `tailscale0` covers the address. Usable.
- no answer **and** the route goes via `tailscale0` (flagged `ROUTED VIA TAILNET (hijack)`):
  a Tailscale peer advertises a subnet containing the target. A dead sensor and a hijacked
  route are indistinguishable.
- an answer that arrived over `tailscale0`. The traffic tunnelled through a peer, so the local
  path is untested and the responder may not be the intended device.

On either ambiguous case, the honest finding is "the route must be cleared before this
device can be assessed". Never call the sensor faulty; never call the network healthy.
A blank neighbour entry, or a section that did not run, is not a finding either.

**The diagnostic transport is a known cause of the faults it diagnoses.** A tailnet peer
advertising a sensor subnet makes the kernel prefer `tailscale0` for sensor traffic.

Two further outcomes mean the check never ran: no address for that sensor in the site config,
and an address that is a hostname rather than an IP (it is not resolved).

## Not everything on the network is on the network

The ASU acoustic sensor is a **local Docker service** ("Dumbo") with an HTTP API on this
machine, not a device on the sensor LAN. So `asu_connected: false` is a statement about a
container here, and no reachability probe can explain it. The triage's `dumbo` container line and the
`:8000` check separate four cases: container absent, container exited (with exit code — 137
is SIGKILL, typically the OOM killer), container up but API unreachable, and healthy.

The counters discriminate further: `request_count` climbing with `success_count` at zero
means the backend was never reachable since start; a flat `success_count` with recent
errors means it worked and then stopped.

## Geometry faults look like software faults

Sensors mounted on a shared platform reference it with `platform:`. The launcher composes

    world_yaw = platforms.<tower>.yaw_deg + <sensor>.mount.yaw_deg

and writes the result into each sensor's own yaw-role key — `azimuth_offset` for radar,
`yaw` for the ASU. Nothing publishes health for a mast, so an alignment fault presents as
tracks in the wrong place with every node healthy.

The discriminator: an angular error **shared by every sensor on one platform** is the
tower's `yaw_deg`; an error on **one sensor alone** is that sensor's `mount.yaw_deg`.
Setting `azimuth_offset`/`yaw` directly on a sensor that also has a `platform:` is
mutually exclusive and the launcher rejects it — which is why a node sometimes fails to
start right after someone "fixed" an alignment by hand.

## How to reason when there is no recorded case

1. **Establish scope before cause.** Which nodes are suspect, and do the views
   agree? Disagreement is itself the finding.
2. **Rule out shared plumbing before blaming hardware.** A transport fault, a hijacked
   route or a second launcher session explains many nodes at once; a hardware fault
   usually explains one.
3. **Prefer the view that can see the failure mode.** If a node is missing entirely, ask
   the launcher, not health.
4. **Treat absence of evidence carefully.** A section or command that errored produced no information;
   it did not produce a negative result.
5. **Do not upgrade an ambiguous reading into a conclusion.** If the evidence supports two
   stories, say so and list what would separate them. Escalating costs less than a
   confident wrong answer.
