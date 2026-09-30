# When to stop and escalate

The bot reads this and uses it whenever it cannot answer. It is also the honest answer for a
human: knowing when to stop is a skill, and guessing at a customer is worse than escalating.

## Escalate immediately, without troubleshooting

- **Anything safety-related.** An effector that fired unexpectedly, or did not fire when it
  should have. Do not debug this over the phone.
- **Suspected data loss** — recordings or events that mattered and are gone.
- **A security concern** — unexpected access, credentials in the wrong place, a machine
  reachable from somewhere it should not be.
- **The customer is asking for a change, not a fix.** New sensors, new site geometry, changed
  thresholds. That is a project, not a support call.

## Escalate after triage

Escalate once you can say **what you ruled out**, not just what you saw:

- Two hypotheses tried and disproven, and no third.
- The evidence contradicts every playbook — a log message nobody recognises, or behaviour that
  should be impossible.
- The fix would mean editing source, rebuilding an image, or running a command not written
  down anywhere.
- The machine is in a state no playbook describes.

**Stop and escalate rather than improvise a command.** A plausible-looking command invented on
a call is how a diagnosis becomes an outage. If it is not written down, it does not get run on
a customer's system.

## What to attach

Always:

1. **The session trace** — `traces/<session_id>.jsonl`, the single most useful artefact.
   It carries the findings, the transcript and the report, so it shows what was actually
   observed rather than what someone remembers observing. A trace from a **mock-mode** run
   says so; say so too, because a fixture answer is not a diagnosis.
2. **Which system**, by its real name (`axon-gotcha-3`), not "the customer in the north".
3. **What the customer actually said**, in their words, before anyone interpreted it.
4. **When it started**, and what changed around then — an update, a power cut, a network
   change, weather. "Nothing changed" is usually wrong; ask what happened that day.
5. **What you ruled out**, with the evidence. This is the part that saves the next person an
   hour.

If there is no trace (site offline, no access, diagnosed by hand), say so explicitly and
attach what you ran instead:

```
make status
make logs SERVICE=gotcha30 2>&1 | tail -200
make logs SERVICE=gateway  2>&1 | tail -200
grep IMAGE_TAG .env
docker compose exec -T gotcha30 ./build/bin/system_launcher -c configs/<site>/full_system.yaml --print-config
```

## Never attach

- Raw `.env` contents — it holds tokens.
- Site config files unredacted — they carry camera credentials.
- Anything from a customer's network you were not asked to collect.

A trace is already redacted. Hand-collected output is not — check it before sending.

## Raising it

Jira project **GOT** at `https://axon-pulse.atlassian.net`.

A useful ticket opens with the customer's symptom, then the system name and image tag, then
what was ruled out, then the bundle. Put the symptom first: whoever picks it up is matching
against things they have seen, and they can only do that from the symptom.

Title it as the customer experienced it — *"axon-gotcha-3: no detections after network
maintenance"* — not as your current best theory. Theories in titles outlive the evidence for
them and send the next person down the same wrong path.

## Telling the customer

Say what is known, what is not, and what happens next. A customer who is told "we have ruled
out the network and the sensors, we are escalating to engineering, you will hear by tomorrow"
is far better served than one told "we're looking into it".

Do not speculate about cause to a customer. A guess repeated back to you in three days has
become a fact.
