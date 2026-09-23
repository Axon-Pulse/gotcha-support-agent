---
name: gotcha-support
description: Fast first-line support for deployed gotcha counter-drone systems. A PM reports a problem ("the map is empty on gotcha 3", "radar isn't detecting", "camera went offline", "it's showing Dallas", "everything says up but nothing works"); this skill finds the system on the tailnet, connects over it (tailscale ssh, or plain ssh where Tailscale SSH isn't enabled), runs a read-only triage bundle, matches the evidence against the recorded support cases and the gotcha30 source at the exact version the machine runs, and replies with a short diagnosis and the steps to fix it. It still answers from the KB and source when the site is offline or the link is bad, and it answers how-does-this-work questions about gotcha behaviour, config keys and log messages. Use it whenever someone describes a symptom on a gotcha / axon-gotcha site, system, C2 UI, sensor (Magos radar, ASU acoustic, Meduza, PTZ camera), tracker, gateway or launcher, even if they don't ask for "support" or name the machine, and even for a one-line complaint.
---

# gotcha support

A PM messages you about a problem on a deployed gotcha system. They want a short, correct
answer quickly: what is wrong, how sure you are, and what to do about it. They usually are
not the one who will run the fix, so the answer has to make sense to them and be concrete
enough to forward to a field engineer.

You work read-only. You look; you never change a customer's system. Every fix goes into your
reply as a recommendation, never into a command you run.

Paths below are relative to this skill's directory.

## 0. Should this be troubleshot at all?

Skip triage and say it needs escalation straight away (see `kb/escalation.md`) when the
report is about:

- **safety**: an effector fired unexpectedly, or did not fire when it should have
- **lost data**: recordings or events that mattered and are gone
- **security**: unexpected access, credentials exposed, a machine reachable from somewhere odd
- **a change, not a fault**: new sensors, new geometry, new thresholds. That is a project.

Not every message is a live fault. For "what does `end_on_first_complete` do?" or "why would
the tracker drop a track?", skip steps 1–2. Answer from the KB and the source (step 4) at the
version they care about: a named system's version, otherwise `stable`, which is what deployed
machines run.

## 1. Which system?

If the PM didn't name a system, or the name is ambiguous, ask. Asking costs a few seconds;
diagnosing the wrong machine costs the whole conversation. Make the question easy to answer
by running this first:

```
scripts/list_systems.sh [whatever they called it]
```

It lists tailnet peers (tailnet name, hostname, IP, online/offline) and fuzzy-matches the PM's
words, so "gotcha 3" finds `axon-gotcha-3`. Then:

- one online match: use it, and say which machine you connected to in your reply
- several matches, or none: show the short list of online systems and ask which one
- the match is **offline**: tell the PM straight away that you can't reach it and when it was
  last seen. An offline machine is often the finding itself (power, uplink, site network).
  Then keep going with **"When you can't connect"** below. The PM still gets an answer.

## 2. Triage in one round-trip

```
scripts/run_triage.sh <tailnet-name>
```

This pipes `scripts/triage_remote.sh` over `tailscale ssh` and runs it on the machine. It
auto-detects the gotcha directory, then prints one section per view:

| section | answers |
|---|---|
| gotcha dir + version | `release:` (the exact source version compiled into the launcher, e.g. `v1.3.0-38-g7c9b0167`), `IMAGE_TAG`, `MODE`, `.env` keys the release expects but this machine lacks |
| containers | status, restart count, start time, exit code, OOM flag, for every container |
| launcher sessions + node processes | how many `system_launcher`s are running (more than one is its own fault), each node's uptime |
| listeners + http checks | what's bound on 8080/5173/8000, gateway `/health` on loopback vs LAN IP, the health body |
| log signatures | every known error line from the KB, tagged `[case-slug]`, with count and first/last time |
| log tail | the latest lines from the core and the gateway |
| resolved config | `--print-config` for the running config, with secrets redacted: which nodes and sensors this site actually has |
| sensor paths | for each sensor IP: the route the kernel would use, the neighbour-cache state, and a flag when traffic would go via `tailscale0` |
| gateway config, weights, ffmpeg | `host`/`mode` lines, model weight files, ffmpeg present in each container |
| resources | disk, memory, `/dev/shm` eCAL leftovers, recent OOM kills |

Output is also saved to `/tmp/gotcha-triage/<host>-<time>.txt`; that file is what you attach
to an escalation. A successful run also records the system's release, image tag and config in
`state/systems.tsv`, so later lookups know its version even when it's unreachable.

Options, all passed as environment variables or flags:

- `GOTCHA_SSH_USER=<user>` if the default login is refused.
- `GOTCHA_CONFIG=configs/<...>` when no launcher is running and the output says it can't tell
  which config the site uses. Pick the one matching the system name.
- `--ping` also pings every sensor. That puts traffic on the customer's sensor subnets, so
  use it only after the PM has said that's OK, and only when reachability is the open question.
  The route and neighbour-cache columns are passive and usually answer it without pinging.

**How it connects.** Only some machines run Tailscale SSH; `list_systems.sh` shows which
under `via`. The script uses `tailscale ssh` where it's available. Everywhere else it uses
plain `ssh` over the tailnet name with the user's `~/.ssh/config` (user, keys), in batch
mode so it fails fast instead of waiting for a password. `GOTCHA_SSH=tailscale|ssh` forces a
transport.

If the connection itself fails, don't retry blindly. Tell the user the one thing they need
to do, since these need a human at a keyboard once:

- **timeout** (tailscale): it's waiting for a browser "check" login. The user runs
  `! tailscale ssh <host> true` once, then you retry.
- **`Permission denied (publickey,password)`** (plain ssh): the machine wants a password and
  there's no key for it. The user runs `! ssh-copy-id <user>@<host>` once, then you retry.
  If they don't know the user, ask; then set `GOTCHA_SSH_USER`.
- **host key unknown / changed**: don't bypass it. The user runs `! ssh <host> true` and
  checks the fingerprint themselves.
- **"no docker access"** in the output: most sections will be empty. Say so rather than
  reading the gaps as evidence that nothing is wrong.

## 3. Match the evidence to a case

Tagged log signatures are the strongest lead, but they aren't the only one. Several faults
never log anything, and they show up in the other sections. Use this index to pick one or two
candidate cases, then **read those case files in `kb/cases/` before you tell anyone what to
do**. The index is enough to recognise a case, but the fix, its caveats and the "if that didn't
work" branches are only in the file.

| case | PM usually says | decisive triage evidence |
|---|---|---|
| `container-up-but-nonfunctional` | "everything is up but nothing works", "we restarted it" | containers Up but restart count climbing or uptime seconds; gateway `/health` 503 |
| `gateway-bound-to-loopback` | "map empty from my laptop", "page loads, no data" | `listening 127.0.0.1:8080`; loopback 200 but LAN IP 000 |
| `ui-shows-tracks-over-dallas` | "shows Texas/Dallas", "fake tracks", "tracks with radars off" | `[ui-shows-tracks-over-dallas]` signature; `mode: auto`; `data_provider_available: false` |
| `py-glue-stale-or-missing` | "won't start", "broke after the update" | `[py-glue-stale-or-missing]`; python nodes missing or with seconds of uptime |
| `old-wheel-partial-degradation` | "events always empty", "one panel blank" | `[old-wheel-partial-degradation]` in gateway log |
| `node-died-no-message` | "one sensor just stopped", "died overnight" | `[node-died-no-message]`; node absent from launcher's children; OOM kills |
| `two-launcher-sessions` | "node flapping", "duplicate detections" | `system_launcher processes: 2+`; same node twice with very different uptimes |
| `radar-connected-but-no-detections` | "no targets", "nothing detected in a flight test" | sensor IPs `no route`, neigh `FAILED`/`none`, or routed via tailscale0; config has no/wrong radars |
| `asu-backend-never-reachable` | "acoustic panel all zeros", "page contradicts launcher" | dumbo container absent/exited (137 = OOM); `asu api` 000; nothing on :8000 |
| `camera-offline-onvif` | "camera offline", "can't move the camera" | `[camera-offline-onvif]`; no `python_optic_ptz` node in config means they mean another device |
| `no-video-in-ui` | "camera moves but black video" | `[no-video-in-ui]`; `ffmpeg MISSING` in a container |
| `tracker-never-classifies-drone` | "everything unknown", "no drone alerts" | `[tracker-never-classifies-drone]`, no `ok:classifier-loaded`; weights dir empty |
| `override-silently-ignored` | "we disabled it but it's still running" | `[override-silently-ignored]`; resolved config disagrees with what they say they set |
| `env-missing-vars-after-upgrade` | "setting does nothing on the old machine" | `.env` missing keys non-empty |
| `sensors-aint-centered-at-the-map` | "changed coordinates, center didn't move" | config coordinates changed; tower position not |
| `end-on-first-complete-resets-system` | "the system reset while I was looking at the GUI", "restarted one node and everything restarted" | `[end-on-first-complete-resets-system]` signature; core `restarts` climbing with `exit=0`; config line `end_on_first_complete is TRUE` |

If the PM's words fit a case the index doesn't list, `grep -ril '<key phrase>' kb/cases/`.
Cases get added over time, and each file's frontmatter `symptoms:` are the phrases people use.

Keep two things apart. A case's "Checks" commands are things you may run. Its "Fix" commands
are things you recommend.

**Log age matters.** Each signature section prints when the log starts. Startup-only
messages (`load_model`, `py_glue ... unavailable`) are gone once a container has run long
enough for its logs to rotate. On a long-running container, the absence of such a signature
proves nothing.

## 4. Look it up in the source

The KB records faults someone has already met. The source explains everything else. Whenever
the triage shows a log line, a config key or a behaviour that no case explains, look it up
**before** you reason about it from general knowledge. That includes a line you think you
understand. One real miss came from exactly this: the triage showed
`First node completed: magos2` three times, and nobody knew it meant "the site config has
`end_on_first_complete: true`, so one node's exit shuts the whole system down". One search
of the source would have said so.

```
scripts/code.sh grep <ver> '<literal part of the log line>' [src python GUItcha30 ...]
scripts/code.sh show <ver> <file> <start>,<end>      # read the function around the hit
scripts/code.sh grep <ver> '<config_key>' configs docs   # defaults, docs, and site configs
scripts/code.sh log  <ver> <path>                   # what changed there recently
```

- **`<ver>`** is the system's name (it uses the release its last triage recorded), or the
  `release:` value from the triage output. That way you read the code the machine actually
  runs, not today's `main`. `stable` means the newest release tag and `latest` means
  `origin/main`. If the tool warns that it fell back to `origin/main`, run
  `scripts/code.sh sync` once. If that doesn't help, say in the answer which version you read.
- **Searching a log line:** search the fixed words. Drop the parts that vary, like node names,
  numbers and IPs. `grep` names the enclosing function (`file=NN=function` lines), so a
  `show` of that range is usually the whole story.
- **Config keys:** find the default (`configs/default/` or the top-level `configs/*.yaml`),
  the site's own config (it's in the repo too, e.g. `configs/megenim_forest.yaml`, though the
  machine may have local edits), and the code that reads it.
- In the reply, cite what you read as `file:line`. It lets an engineer check your reasoning
  in seconds.

Reading the source is for understanding. It doesn't license a source edit as the fix. If
the only fix is a code change, that's an escalation (see the end).

When the source solves something the KB didn't have, offer to write it up as a new case in
`kb/cases/`, in the same shape as the others, and add its log lines to
`scripts/signatures.txt`. Next time, the triage will tag it.

## 5. At most a couple of targeted follow-ups

If the bundle narrows it to one case but a check in that case's file would settle it, run
that check with `tailscale ssh <host> '<command>'`. Aim for one or two, not a second
investigation. What counts as read-only:

- fine: `docker ps/inspect/logs`, `grep`, `ls`, `ss`, `ip route get`, `ip neigh`, `curl` GETs to
  the machine's own ports, `--print-config`, `docker exec <c> ls|which|cat <non-secret file>`
- never: `make up/down/restart/pull/rollback/init`, `docker restart/stop/rm`, editing or
  appending to any file, `keyboard_node`, installing anything. Those are fixes and belong in
  the reply.
- never print secrets: no `cat .env` and no unredacted site configs (camera passwords). The
  triage bundle already reads just the safe parts. If you need a value from `.env`,
  `grep '^KEY=' .env` for that one key.

The KB is blunt about improvised commands, and it's right: *if it is not written down, it
does not get run on a customer's system.* That applies to the fixes you recommend too. Stick
to the commands in the case file, adapted only by filling in real values (IPs, tags, paths)
from the triage output.

## 6. When you can't connect

Sites often have weak links or none at all. Don't wait on them. `run_triage.sh` pings the
system over the tailnet first, and if there's no reply within 5 seconds it exits with code
10 and an `UNREACHABLE:` line. Take that as your cue to switch to this section right away,
without retrying. (If it says Tailscale on *this* machine isn't running, that's a local
problem: tell the user to run `! sudo tailscale up`.)

The PM still needs an answer, so give the best one the KB and the source can support, and be
clear about what it rests on:

1. Use the version on the `fallback version for code lookups:` line. It's the release
   recorded at the system's last successful triage, with its date. With no record, it's the
   **newest release**, since deployed machines track `stable`. Not `main`, which may hold
   code no site runs yet. Pass the system name to `code.sh` and it picks the same version.
   Say in the answer which version you read and why: "last seen running v1.3.0-38 on Sep 23",
   or "no record, assumed the newest release v1.3.0". A recorded version can be out of date if
   the machine was updated since.
2. Match the PM's description against the case index and the cases' `symptoms:`. Use the
   source for the mechanism, and read the site's config from the repo.
3. Ask the PM for what they can see without you: the exact error text, a screenshot, what
   changed and when, or output someone on site can paste. Pasted output is as good as triage
   for the part it covers. Tell them the one command worth running on site if there is one,
   taken from a case's Checks section.
4. Reply in the usual shape, with a **Confidence** line that says it's **not verified on the
   machine** and names the one check that would confirm it once the connection is back.

If the link is weak rather than down, run the triage anyway. It sends one small script and
gets back about 200 lines, and a partial output is still saved and still useful. If it times
out, say what you got before it did.

## 7. When nothing matches

Read `kb/system-model.md`. It explains the mechanism, so you can reason about a fault nobody
has written up. Use step 4 for the specific line or key in front of you. It was written for a different harness, so translate its tool names:
`get_process_table` means the containers and launcher sections; `probe_endpoint` means sensor
paths; `get_asu_service_status` means the dumbo container plus the :8000 check;
`search_runbook`/`read_case` means grepping `kb/cases/`. There is no equivalent here for
`get_system_health` or `get_ecal_topology`. Don't claim you saw per-node health or bus
wiring.

The rules from that document that matter most when you're under time pressure:

- a route via `tailscale0` to a sensor subnet means the sensor can't be assessed until the
  route is cleared. Don't call the sensor dead, and don't call the network fine.
- a node that is `OFFLINE` in the UI but running with a long uptime is a transport problem,
  not a dead node.
- a section that errored gave you no information. It did not give you a negative result.
- if the evidence supports two stories, say both and name what would separate them.

`kb/glossary.md` helps when the PM's word doesn't match the system's. "The camera" might be
Meduza, not the PTZ, and "the system" usually means the C2 UI.

## 8. Reply to the PM

Short. The PM should be able to read it in under a minute and forward the "what to do" part
as-is. Use this shape:

```
**<system>: <one-line verdict in plain words>**

**What's wrong:** 1–2 sentences, no jargon a PM wouldn't know.
**Evidence:** 2–3 bullets quoting what the machine actually showed.
**What to do:**
1. <step, who does it, and the exact command if it needs one>
2. ...
**How to confirm it's fixed:** one line.
**Confidence:** high / medium / low, whether it was verified on the machine, and what you ruled out, in one line.
```

Example:

```
**axon-gotcha-3: the map is showing simulated data, not the real sensors**

**What's wrong:** the web gateway failed to load its sensor connector at startup and
quietly switched to demo mode, so the picture is fake (it's centred on Dallas).
**Evidence:**
- gateway log: `py_glue or protobuf not available: No module named 'py_glue'` (once, at 06:12 today)
- `/health` → `data_provider_available: false`; gateway config `mode: auto`
- image `IMAGE_TAG=latest`, updated this morning
**What to do:**
1. Field engineer rolls back to the last good image: `make rollback TAG=<previous tag>` then `make up`
2. Set `data_provider.mode: glue` in `gateway-config/config.yaml` so this fails loudly next time, then `make restart SERVICE=gateway`
3. Report the `latest` tag as broken to engineering
**How to confirm it's fixed:** `/health` shows `data_provider_available: true` and the map is centred on the site.
**Confidence:** high. Sensors and network weren't involved (all sensor IPs routed on the LAN).
```

Adjust the shape when it helps. If it's an escalation, say so in the verdict line and add the
ticket title as the customer experienced it (e.g. *"axon-gotcha-3: no detections after
network maintenance"*) plus the saved triage file path. If the fix is "wait for the network
team" or "someone on site checks a cable", say exactly that. Don't pad it with commands.

Don't speculate to the PM. They will repeat it to the customer, and a guess repeated back in
three days has turned into a fact. When you're not sure, the honest answer is "here's what
it isn't, and here's the one check that would tell us".

## Escalate after triage when

- two hypotheses have been ruled out and there's no third
- the evidence contradicts every case, or shows behaviour that should be impossible
- the fix would need a source edit, an image rebuild, or a command no case documents

`kb/escalation.md` has what to attach and how to title the Jira ticket (project GOT). The
saved triage file stands in for the "session trace" it mentions.
