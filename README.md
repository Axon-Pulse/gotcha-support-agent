# support-agent

Multi-agent troubleshooting assistant for gotcha30. Reads a gotcha30 checkout for its
node inventory; never imports from it.

## What each file is for

| Path | Purpose |
|---|---|
| `run.py` | CLI. Starts a session, drives the graph, shows approval prompts. |
| `graph.py` | The LangGraph state machine + the **only** code that writes to `kb/`. |
| `agents.py` | **Agent definitions and ordering.** Edit this to add/reorder agents. |
| `registry.py` | `@tool` decorator, autoloads `tools/`, refuses write tools. |
| `transport.py` | **The only code that runs a command**, and the only one that reveals a secret. |
| `inventory.py` | The system allowlist. **Public view** for the model, **private lookup** for transport. |
| `systems_inventory.example.yaml` | Template for the central registry. Holds env var *names*, never secrets. |
| `llm.py` | Anthropic calls: model, thinking, effort, prompt caching, token totals. |
| `conversation.py` | Turn routing (run vs follow-up), per-turn timing and token totals. |
| `env_file.py` | Loads `.env` into the environment. Never overrides an exported variable. |
| `state.py` | Graph state; `Finding` and `Scenario` shapes. |
| `tools/*.py` | **One file per diagnostic area.** Drop a new file here to add a tool. |
| `kb/system-model.md` | **How the system works.** Code-derived, always in the cached prompt, read-only. |
| `kb/cases/*.md` | **What has actually gone wrong.** One file per incident, retrieved by search. |
| `tests/` | Fixtures from a real faulty run + the safety assertions. |
| `traces/` | One JSONL per session or conversation; one line per turn. |
| `graph.db` | Checkpoints. Lets a pending approval survive a restart. |

## Operating it

    cp .env.example .env && $EDITOR .env      # ANTHROPIC_API_KEY, GOTCHA30_REPO
    .venv/bin/python run.py "the acoustic sensor shows no tracks"

Get the key from [console.anthropic.com](https://console.anthropic.com) → Settings → API
keys → Create key; it is shown once. `.env` is read at startup by `env_file.py` and
**never overrides a variable you exported** — a shell export, a systemd unit or a
container's environment always wins, so a stale `.env` cannot shadow the key you are
testing with. If you would rather not keep it in a file at all, `export
ANTHROPIC_API_KEY=sk-ant-…` or `ant auth login` both work, and so does typing it into the
console's Run tab (see below).

    .venv/bin/python run.py --resume <session_id>   # finish a pending approval
    AGENT_MODE=live .venv/bin/python run.py "..."   # real commands, not fixtures

`AGENT_MODE` defaults to `mock`: `get_*` tools read `tests/fixtures/` instead of touching
the system. `probe_endpoint` always runs live (it inspects local routing only).

## Where you manage things

**Add a tool** — new file in `tools/`:

```python
from registry import tool
import transport

@tool({"name": "get_x", "description": "What it shows, and what it means.",
       "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}})
def get_x() -> dict:
    return summarise(transport.run("x"))     # small dict, never a raw dump
```

Autoloaded. Then list `"get_x"` in an agent's `tools` in `agents.py`.

**Add knowledge** — a new incident is a `.md` in `kb/cases/`, normally written by the
approval gate rather than by hand. Mechanism that is true of the system regardless of any
incident belongs in `kb/system-model.md` instead.

**Add an agent** — an entry in `agents.py` (prompt + tool names), then put it in `ORDER`.

**Change ordering** — `ORDER` is the sequence, `REQUIRES` is a hard precondition the
supervisor cannot violate. `SUPERVISOR_PICKS = False` turns it into a fixed pipeline.

**Permissions** live in three places, all in code, all reviewable:

1. `transport.ALLOWED` — the commands that may ever run. Keys, not strings; no `shell=True`.
2. `inventory.py` — the systems that may be addressed. The model cannot name a host.
3. `registry.load_tools()` — refuses any module declaring `SIDE_EFFECT = "write"`.

## The approval gate

`synthesize` may *propose* a new case. `propose_scenario` then calls
`interrupt()`: the graph stops, state is checkpointed, and you get `[y / e = edit / N]`.
Only on `y` does `save` write to `kb/cases/`, in the same frontmatter shape the console
editor reads. The agent has no write tool, so it cannot add a
scenario on its own.

Same seam covers remediation later: give a `restart_node` tool `SIDE_EFFECT = "write"`
and it is refused at registration until an approval node is wired in front of it.

## Tests

    .venv/bin/python -m pytest tests/ -q

`tests/conftest.py` isolates every test from the registry at the repo root: those are
live operator data, so a test that read them would fail for reasons unrelated to the
code. Each test starts with no registry unless it points `SYSTEMS` at its own fixture.

The console UI has no build step and no browser in CI, so `tests/test_console_render.py`
runs `console/index.html`'s inline script under node against a DOM stub and calls every
render path — which catches a typo in a template literal before it blanks a tab. It is
skipped when node is not installed.

## Console

    .venv/bin/python -m uvicorn console.server:app --port 8765
    # then open http://localhost:8765

Tabs: **Run** *or* **Live with chat** depending on the answering mode (see below),
then **Approvals** (the queue — edit the markdown in place, then approve or reject),
**Traces**, **Knowledge** (a structured runbook editor), **Agents & permissions**
(which now ends with the execution graph and the flow editor), and
**Inventory & systems**.

The console sets `AGENT_MODE=mock` before importing anything, so it cannot touch the real
system. Live runs stay a deliberate command-line act.

### Two answering modes, chosen from the pill in the header

The pill that used to read `mode: mock` now selects **how a question is answered**:

    token mode   the graph pipeline. Spends API tokens. Tools read tests/fixtures/ —
                 the console forces AGENT_MODE=mock, so it cannot touch the bench.
    chat mode    a ticket queued to disk (bridge.py), picked up when somebody types
                 `go` in the Claude Code session. No API tokens, but its read-only
                 checks run against the REAL hardware.

**"mock" and "token" are not synonyms, and the pill no longer pretends otherwise.**
`mock` is about what the tools can *reach*; the answering mode is about *who answers*.
Collapsing them into one word would have dropped the safety-relevant half — chat mode
genuinely touches the bench — so the fixtures-versus-hardware fact is stated on each
option in the dropdown, where it is read at the moment of choosing, and the note under
the input box is rewritten per mode. A reassurance that is false in the mode you are in
is worse than no reassurance.

The two modes keep **separate threads**. A bridge ticket is not a conversation turn:
splicing them into one history would make the token counts and the routing read as if
they applied to both.

**The tab bar follows the mode.** Run and Live-with-chat are the same job done two ways,
so only the one that matches is offered — hidden rather than disabled, because a tab you
can click into and then cannot use is worse than one that is not there. Switching modes
while you are on the tab being hidden moves you to its counterpart instead of leaving
every tab deselected and the page blank; switching while you are on Traces, Approvals or
anything else leaves you where you are. Those tabs are never mode-specific — hiding the
queue or the history along with Run would strand them.

### The execution graph sits under Agents & permissions

It used to be its own tab, which meant the diagram was only ever seen by somebody who
remembered to go and look — and it sat stale against an edit until they did. It is the
shape those settings produce, so it is read beside them, at the foot of the page after
the agents, tools and permissions that determine it. The drag-and-drop flow editor comes
with it.

`loadGraph()` fetches, `paintGraph()` draws. The split matters because `renderPerms()`
rebuilds the whole panel — and so destroys the pane — on every card expand: a repaint
comes from cache, and only a save (which goes through `loadPerms()`) refetches. The
diagram now follows a reorder immediately instead of on the next tab switch.

### The open tab is in the URL

`#traces`, `#inv` and so on. A refresh reopens where you were instead of dropping you
back on Run, the tab is linkable, and browser back/forward work without extra code.
Kept in the hash rather than in storage deliberately: where you are stays visible and
explainable rather than becoming a hidden preference.

A hash naming a tab that does not exist, or one the current mode hides — `#run`
bookmarked in token mode and reopened in chat mode — falls back to the tab that mode
does offer, rather than stranding you on a section whose tab is not in the bar.

### Choosing the model

The model pill in the header expands to **the models this account can actually use**,
asked of the Models API (`client.models.list()`) rather than hard-coded. A curated list
goes stale the week a model ships, and an operator choosing from a stale list picks
something that 404s — the live call here returned `claude-opus-5-5`, which no list
written by hand would have had. `llm.FALLBACK_MODELS` is the fallback for no
credentials or no network, and the menu **says when it is showing that** instead of
looking authoritative.

The choice is persisted in `overrides.json` and written to `config_audit.jsonl` like
every other console edit — it changes what runs cost, so it should show up in a diff.
`llm.model()` reads it **per call**: the earlier module constant meant an edit could
leave the pill saying one thing while the process kept sending another. Precedence is
the console override, then `AGENT_MODEL`, then the code default.

An id is validated against the catalogue at the moment it is chosen. Accepting a typo
or a date-suffixed id silently would surface minutes later as an opaque 404 in the
middle of a diagnosis. **Switching invalidates the prompt cache** — caches are
model-scoped, so the next run pays full price for the system prompt and the inventory
once, and the menu says so.

### The Run tab is a conversation

A diagnosis is rarely one question. The Run tab keeps a thread, and **every message is
routed first** (`conversation.py`):

    run        a new or changed symptom. Full pipeline: supervisor, agents, tools, report.
    follow_up  a question about what was already found. No graph, NO TOOLS, one text call
               against evidence already in hand.

The two mistakes are not symmetric, and the router's prompt says so: a follow-up
misrouted as a run wastes a minute and a pipeline's tokens, while a new problem misrouted
as a follow-up answers it out of evidence gathered about something else — and reads as
confident. When the readings are close it routes to a run.

**Routing never raises.** It is an optimisation — it decides whether the expensive path
can be skipped — so a failure costs the three-second call, not the operator's turn. A
dropped connection on that call used to surface as `APITimeoutError` and destroy a
conversation the full pipeline would have answered; now it falls through to a run and
says so in the turn's reason. `llm.client()` also retries more than the SDK default,
because one dropped connection anywhere in a multi-minute run otherwise loses all of it.

**A conversation is not one long graph run.** Graph state is append-only, so feeding a
second question into the same LangGraph thread finds every agent already in `visited`,
leaves the supervisor with nothing eligible, and silently re-summarises the old evidence
against the new question — the same trap `slack_app.py` records as "ONE MESSAGE IS ONE
SESSION". So each `run` turn gets its **own fresh session**; earlier turns reach it as
context, never as state.

**The follow-up agent has no tools, and that is the safety property**, not an
optimisation. With no tools it cannot reach `transport.py`, so no follow-up can touch a
device however the message is phrased. It answers from the findings it is handed or says
it cannot.

**Mixed Hebrew and English is isolated, not merged.** Tickets arrive in Hebrew about a
system whose parts are named in English, and both get interpolated into one left-to-right
template. Left alone, the Unicode bidi algorithm resolves the whole line as a single
paragraph: a Hebrew sentence ending in `PID 291846.` puts the full stop at the wrong end,
and an English node name inside a Hebrew clause drags its neighbouring punctuation across.

The two cases need **opposite** treatment, and conflating them is how the bug comes back:

    the value IS the element     dir="auto" on the element itself
    (a question, an answer,      — resolves direction from the text AND aligns it
     one evidence item)

    the value is EMBEDDED        <bdi> around the value
    beside other text            — stops it reordering its neighbours
    (`what — why`)

`dir="auto"` **skips isolated descendants** when it looks for the first strong character,
so wrapping a whole paragraph in `<bdi>` makes it fall back to LTR — the Hebrew then
renders left-aligned with a ragged gap down the right, which is exactly the symptom that
looks like the isolation is missing. The chrome around the text (labels, badges, token
counts, the meta line) is pinned LTR so a Hebrew answer cannot flip it.

**A refused check asks for permission.** A tool that comes back "not allowlisted",
"unknown node" or "has no access block" was not *broken* — it was *not allowed to look*,
and that is a gap somebody can close. `graph.permission_gaps()` reads those refusals off
what the tools actually returned, and they are merged into `needs_permission` in the
report **whatever the model says**, because which permission would have helped is a fact
about the run and a model asked to remember it across a long transcript sometimes will
not. The model may add its own. The ask renders at the end of the feedback and is
deliberately **not** collapsed: a request nobody expands is a request nobody answers.

**What you read is the bottom line.** `bottom_line` is a required field of
`REPORT_SCHEMA` — required, because it is the only part most readers open, and a model
allowed to omit it will. Two or three sentences, still technical: node names, PIDs and
uptimes belong there, because the reader is an engineer. Everything else — root cause,
evidence, what could not be checked, next steps, the tool table, the per-agent prose —
is one collapsed line until you ask for it.

**Every turn is recorded as it finishes**, one JSON line per turn in
`traces/<conversation>.jsonl`, carrying the question, the answer or report, the findings,
which kind of turn it was and why it was routed that way, how long it took, what it spent,
and the running conversation total. A conversation still in progress is therefore already
durable — the trace is not written at the end, because there is no end until somebody
stops typing. A turn that *failed* is recorded too: it spent tokens and took time, and a
trace that drops it cannot be read back honestly.

### Inventory & systems

A visual editor for `systems_inventory.yaml`, and the last tab because it is reference
data rather than something you touch during a session.

A **system** groups everything on one platform, and it is not limited to sensors: each
**component** carries a `role` — `sensor`, `compute`, `laptop`, `network`, `power` or
`other` — alongside its address, port, scheme, hardware model, software version, domain
and SSH access. A compute box or a switch is addressable and diagnosable like anything
else, so they share the one namespace; `role` is what tells them apart. `components:` is
the current key and `sensors:` is still read as an alias, so older registries keep
loading and keep defaulting to `role: sensor`.

Clicking a system opens a **read-only summary** — one row per component, with the access
column showing `key`, `password` or `—`. Editing is an explicit **Edit** click, which
swaps in a dense 12-column grid that puts name/role/type/domain on one row and
address/port/scheme/model/version on the next, rather than a field per line.

An access block is validated on the way in: a port outside 1-65535, an invalid username,
or a key or password with no user to log in as is refused with the field named. Those
are exactly the shapes `credentials()` raises on at run time, so they are caught at the
keystroke instead. Validation is scoped to the system being saved — a half-filled block
saved earlier does not make the rest of the registry read-only.

Typing a password is allowed here — this is a human at localhost — but **the value never
enters the registry**. On save it goes to `secrets.local.env` (gitignored, mode 0600) and
only `password_env: NAME` is written to the YAML, with the variable name derived from the
sensor name if you do not supply one. Reopening the system shows the password again,
masked behind a reveal toggle. A variable already exported in the environment wins over
the stored file and is shown read-only, since the console cannot unset it.

Every write is validated by loading the result through `inventory.py` — the same code the
agent path uses — and **rolled back if it does not load**, so an inline secret, a
duplicate sensor name or a hostname where an address belongs is refused before it can
break a session. Deleting a system deliberately **keeps** its stored secret and names it
in the confirmation, rather than silently destroying a credential you may still need.

The tab shows the model-facing view beside the editor: the exact text that goes into the
cached system prompt, rebuilt from a field allowlist rather than by stripping fields.

### Editing configuration from the UI

**Agents & permissions** edits agent prompts, which tools each agent may call, the tool
descriptions the model reads, the order and its `requires` preconditions, and the allowed
command table. Changes land in `overrides.json`, layered over the code defaults in
`agents.py` / `transport.py` / `tools/`; clearing a field reverts to the default.

Two properties survive the UI being editable:

- **The agent still cannot widen its own permissions.** No tool can reach `config_store` —
  asserted by a test that greps every registered tool's source. Config is written by the
  console API, i.e. by a human at localhost.
- **Changes remain reviewable.** `overrides.json` is git-tracked so edits show in a diff,
  and every write is appended to `config_audit.jsonl` (visible under "View change log").

Edits are validated before they are saved: unknown tools, reserved node names, an agent
with no tools, duplicate or undefined agents in the order, `requires` cycles, a
prerequisite scheduled after its dependant, and non-list argv are all rejected with the
reason. Adding a shell binary (`bash`, `sh`, `env`, `xargs`…) to the command table is
*allowed* but prompts for confirmation first, because the argv-list protection does not
apply to a shell.

The **supervisor** appears in the agents list but is **read-only**: it has no prompt of
its own — one is assembled from the live eligibility set on every hop — and no tools. What
it may pick, and in what order, is changed in the **Graph** tab.

**Post-synthesis agents** (`customer_communicator`) are editable, but their tool list is
shown *disabled* rather than hidden. They run through `llm.run_text_agent`, which has no
tool loop at all, so a tool ticked there would be silently ignored rather than called —
and they run after the evidence phase, so a diagnostic call could not change the report
they are rewriting. Enabling it is a graph change, not a checkbox.

The addressable-systems list is no longer shown here; it lives in **Inventory & systems**.

### Knowledge

Two halves, split by lifecycle rather than by topic.

**`kb/system-model.md`** — how the system works: the three independent views and what each
is blind to, health vs launcher semantics, the probe verdicts and which cannot support a
conclusion, and the reasoning rules for a fault with no precedent. It is derived from the
code, **always in the cached system prompt**, and **read-only in the console** — a
generated document that people hand-edit is one that drifts. Tests pin its facts against
their source: every registered tool must be named in it, every `probe_endpoint` verdict
must appear, and the health timeout and process states must match gotcha30.

**`kb/cases/*.md`** — one file per fault actually diagnosed here. These are **not** in the
prompt; they are reached with `search_runbook`. That is the point: the history can grow
without growing every request, and without invalidating the cached prefix on every
approval. It is also what makes `search_runbook` load-bearing — when the whole KB was in
the prompt, searching it was re-fetching something the model could already see.

One file per case, not one growing file, for three reasons: `save` keeps writing one file
per approval with no append logic to corrupt; each case keeps its own topic frontmatter so
the structured editor works unchanged; and the lexical index ranks individual incidents
rather than returning the same single document for every query.

A match needs at least one distinctive token — an identifier or a long word. Everyday
words add to the score but cannot create a hit on their own, or a query about something
genuinely new would silently recall an unrelated case, which is the one outcome the split
exists to prevent. **No hits is a result**: it means reason from the system model.

The case editor is a structured form, not a markdown box. A field per part rather than one markdown box: **topics** (multi-select, any number),
**symptoms** (a dynamic list), then **title**, **context**, **root cause**, **checks** and
**fix** as separate sections. The backend splits the body on its headings and reassembles
it, so `graph._render` and the editor write the same shape and an agent-proposed case
opens in the same form as a hand-written one.

A heading the form has no field for — an `Evidence` section, say — is surfaced in an
**Other sections** box rather than dropped: an editor that silently deletes what it cannot
display is worse than one big textarea. The **checks** section keeps its leading indent,
because that four-space indent is what makes it a code block rather than prose.

Together they become YAML frontmatter plus a markdown body:

```markdown
---
topics:
  - network
  - radar
symptoms:
  - the radar stopped responding
  - ping to the sensor times out
---

# Tailscale subnet route hijacks the sensor LAN
...
```

**Topics are deliberately not called domains.** A routing domain in `graph.DOMAINS`
decides which *agent* may run; a topic decides which *past case* a ticket matches. Sharing
the word invited the reasonable expectation that adding a tag would summon an expert. A
case may carry several — the hijack case is both `network` and `radar`, so it is found
from either direction — and a new topic is created simply by using it. `domain:` and
`domains:` are still read, so cases written before the rename keep opening.

Opening a case parses the frontmatter back into the fields, and **Close** dismisses the
editor in place (as does clicking *edit* again on the open case). **Documents written
before the editor have no frontmatter and are not broken by it**: the whole file becomes
the body, the fields come back empty, and the editor says so. Frontmatter is only
emitted when there is something to record, so a plain document stays plain.

The frontmatter is read by the model too, which is the point — the topics and the
symptom list are what a ticket gets matched against. **Credentials are not just an API key.** The console gates the Run button on whether *any*
credential resolves — `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, an OAuth profile from
`ant auth login`, or workload identity federation — in the SDK's own precedence order. It
also calls out the two documented traps: an `ANTHROPIC_API_KEY` set to the empty string
still wins its precedence slot and authenticates with nothing, and setting both a key and
a token makes the SDK send both, which the API rejects.

**A key can be typed into the Run tab**, which is the same split the device passwords use:
the value goes to `secrets.local.env` (mode 0600, gitignored) and never into a git-tracked
file, and it comes back out masked rather than rendered. Two things are specific to it
being a *billing* credential. It is promoted into the environment **only when nothing else
resolves**, so it can never shadow a key you exported or a working OAuth profile — the
console is a fallback, not an override. And `llm.reset_client()` drops the memoized SDK
client on every write, so a key set at runtime takes effect without a restart; without
that, a process that started with no credential would keep failing at a key visibly
present on screen.

**This console has no authentication** — no login, no CORS policy, no auth check anywhere.
Its entire trust model is uvicorn's default bind to `127.0.0.1`. That is tolerable for a
device password, which still needs to reach the LAN to be worth anything; it is not
tolerable for an API key, which is spendable by anyone who reads it. Serve it on localhost
only, and never `--host 0.0.0.0`.

**Token counts.** `llm.py` keeps a per-run total in thread-local state — thread-local
because the console and the Slack gateway each give a session its own worker thread, and a
module-level counter would bill one operator's session for another's. It exists alongside
the per-agent numbers in the transcript because it catches what the transcript
structurally cannot: `ask_json()`'s routing and synthesis calls return only a tool input
and produce no transcript entry, so their tokens were previously spent and never counted.
The total is written into each trace line; the Run tab shows this session and a lifetime
figure summed across `traces/`. Traces written before this existed still count — the
rollup falls back to summing their transcripts, just without that routing overhead.

**The filename is derived, not typed.** Write the name however you like — "Radar
WebSocket keeps flapping!" — and the server slugifies it to `radar-websocket-keeps-flapping.md`,
previewing the result live as you type. Slugification happens **only on create**, because
that is the one moment the filename is chosen; every other route still resolves a name
strictly, since slugifying on read or delete would mean a request for one file could
quietly reach another. The result is re-validated against the same guard, so a traversal
attempt is confined (`../../etc/passwd` becomes `etc-passwd`) rather than followed.

The preview in the page and the rule on the server are separate implementations, so a
test runs both over the same inputs — if they drift, the preview starts lying about where
the case will land.

Editing a **case** takes effect immediately for retrieval — it is read at search time,
not at session start, and it does not touch the prompt cache. Editing the **system model**
reaches the next run rather than one already in flight, and invalidates the cached prefix,
so the first session afterwards pays full input price once. That asymmetry is the reason
for the split: the half that changes often is the half that is not cached.

### Graph

The **Graph** tab draws the execution graph: START → supervisor → agents (each looping
back) → synthesize → the approval gate → `save` or END. Dashed amber edges are `requires`
preconditions; green and red are the approve/reject branches. Agents defined but missing
from `order` are greyed out, since the supervisor can never pick them.

Below it is a **visual flow editor**, no library:

- **Pipeline** — agents are draggable chips in two lanes. Drag within the top lane to
  reorder; drag to the bottom shelf to take an agent out of the pipeline entirely, which
  also drops every prerequisite edge mentioning it (otherwise the save would be rejected
  for referring to an agent that can never run).
- **Prerequisites** — click an agent, then click one to its **left** to make that a
  prerequisite. Only the left side is offered because a prerequisite must run earlier,
  which is exactly what `config_store.validate_flow` enforces. Click an arrow to remove it.

Saving redraws the diagram. The validator still has the last word — but the editor no
longer lets you compose a save it would reject: a prerequisite pointing backwards after a
reorder is drawn **red**, named in a banner, and disables the save button until it is
fixed. A cycle or a duplicate is still caught server-side.

Both diagrams cap their rendered width, since an SVG with `width:100%` and a viewBox
narrower than its container is scaled *up* and renders visibly zoomed.


## Prerequisites: `requires`, `needs_context`, `scope_context`

`agents.py` declares three kinds of gate, all enforced in `graph._eligible()`:

- **`requires`** — agent B cannot run until agent A has run at all. Pure ordering.
- **`needs_context`** — agent B cannot run until a named fact has actually been
  *extracted from tool output*. `network` needs `probe_targets`, so if `triage` runs but
  every tool errors, `network` stays ineligible instead of probing the whole inventory on
  a guess.
- **`scope_context`** — same gate, different meaning when it is unmet. A missing
  `needs_context` key means *we could not find out*; a missing `scope_context` key means
  *there was nothing to find out*. `radar_deep_dive` scopes on `radar_targets`, so on an
  acoustic-only fault it is **not applicable** rather than blocked, and is left out of
  the report instead of appearing there as a check we failed to run.

Context is extracted by `graph._extract_context()` from the **structured tool payloads**,
never from the model's prose — an agent that narrates "the radar looks unreachable"
without a tool returning a node must not unlock the network agent.

A blocked agent's reason lands in `state["blocked"]`, reaches the Slack summary, and is
appended to the `synthesize` prompt as "Checks that could NOT be performed" with an
instruction to list them under `unknowns`. Without that the report reads as complete
while a gated agent never ran — the exact overconfidence the gates exist to prevent.
The one exception to the scope rule is a **blind run**: if nothing was observed at all,
an empty `radar_targets` is ignorance rather than the absence of a radar fault, so scope
misses are reported like any other gap.

### `requires` must never name a gated agent

`requires` means "has run", and a gated agent may never run. Writing
`requires: ["network"]` on a deep-dive deadlocks it permanently the first time `network`
is blocked on `probe_targets`. Domain experts therefore require `triage` only; sequencing
after the plumbing checks is done by position in `order`, and since every agent receives
the full findings digest, a deep-dive can read the network verdict and is told to defer
to an ambiguous one. `tests/test_domain_routing.py` asserts this generically, so the
guard fails the moment someone adds such a `requires`.

## Domain experts

Functional agents (`triage`, `knowledge`, `topology`, `network`) give breadth: which
nodes are suspect, and whether the fault is in shared plumbing. Domain experts give depth
on one hardware type, and are unlocked by `graph.DOMAINS` mapping a node's type to a
domain:

| expert | scope key | tools |
|---|---|---|
| `acoustic_deep_dive` | `acoustic_targets` | `get_asu_service_status`, `search_runbook` |
| `radar_deep_dive` | `radar_targets` | `get_radar_status`, `search_runbook` |
| `camera_deep_dive` | `camera_targets` | `get_camera_status`, `search_runbook` |
| `infrastructure_expert` | `infrastructure_targets` | `get_tower_status`, `search_runbook` |

**An expert gets its own domain tool and `search_runbook`, and nothing else.** In
particular it does not get `get_system_health`: triage already called it, and every agent
is handed the full findings digest, so the health rows are in front of the expert without
a second call. Holding the tool invites a redundant call; holding another domain's tool
invites a diagnosis of hardware it is not expert in. A test asserts the scoping, and
another asserts no expert's prompt names a tool it cannot call.

**The two type vocabularies are reversed word order**, so every model needs both
spellings. Taken from gotcha30: `configs/*.yaml` for the left column, the `node_type`
literals in the node sources for the right.

| domain | `inventory.py` (config) | health proto `node_type` |
|---|---|---|
| radar | `magos_radar` | `radar_magos` |
| radar | `elm2135_radar` | `radar_elm2135` |
| acoustic | `asu`, `python_asu` | `acoustic_asu` |
| camera | `meduza_optic` | `optic_meduza` |
| camera | `python_optic_ptz` | `optic_ptz` |
| camera | `python_optic_verification` | `optic_verification` |

So `DOMAINS` enumerates both spellings and matches on set membership; a substring rule
gets `magos_radar` and `radar_magos` right half the time. `optic_scanning_asu` is named
like an ASU but is emitted by `python/nodes/optic_scanning_node` — it is in the **camera**
domain, not acoustic. gotcha30 calls the family "optic" and the console calls it
"camera"; `inventory.DOMAIN_ALIASES` normalises that once at load, so there are never two
domain keys for one piece of hardware, one of which would match no agent. Either spelling is enough to
place a node, so a node that never started — no health message, therefore no proto type
— still routes correctly via the config. A test asserts the table stays in sync with
both sources, so a new node type fails a test rather than silently routing to nothing.

### The platform-fault signature

`infrastructure_expert` is the odd one out: **nothing publishes health for a mast**, so no
node ever reports that a tower is misaligned. Its trigger is structural instead — two or
more suspect components sharing one `system` in the registry, which is exactly the
discriminator `kb/tower.md` uses: an angular error shared across a tower is the tower's
yaw, an error on one sensor alone is that sensor's mount. One suspect is deliberately not
enough. Because the key comes from the registry's `system` field, it only fires once
`systems_inventory.yaml` groups components into platforms.

### Placeholder tools

`get_radar_status`, `get_camera_status` and `get_tower_status` are **declared but not
implemented**. They share `tools/_placeholder.py`, which enforces three things a stub
would otherwise get wrong:

- **No invented measurements.** A plausible number would be cited by the model, quoted by
  `synthesize` as evidence, and land in a report that is confidently wrong about hardware
  nobody looked at. They run one read-only liveness probe and return `implemented: false`.
- **No `nodes` key in the payload.** `graph._extract_context` reads `nodes` out of tool
  results to decide which agents may run, so a placeholder contributing there would gate
  real agents on placeholder output.
- **A failed probe is a finding, not a crash.** "The radar did not answer" belongs in the
  payload, not in a traceback.

Each one's description opens with `PLACEHOLDER — the <domain> check is NOT implemented
yet` and says what it *will* do, so the model calls it for reachability and reports that
the domain check could not be performed rather than inventing one. A cross-domain call is
refused: `get_radar_status` on a camera comes back with `refused`, not a reading.

All four experts ship **defined but absent from `DEFAULT_ORDER`**. `graph.build()` creates a
node for every agent in `agents.py`, so enabling one is an `order` edit in the console,
not a code change, and the Graph tab greys out anything the supervisor can never pick.

Their permitted commands live in `transport.DEFAULT_ALLOWED` — `radar_status` and
`tower_status` are an SSH `uptime`, `camera_status` is a `curl` that discards the body —
so the permission surface is reviewable in a diff now rather than being added under time
pressure later. They are code defaults, not `overrides.json` entries; `overrides.json`
stays for console edits layered on top.

Two details in `transport.run_on` that these commands forced:

- **Credentials are resolved only for a command that actually logs in.** An HTTP probe
  against a camera with no SSH access block must not be refused for lacking one.
- **`%{http_code}` is curl's placeholder, not ours.** The substitution pattern carries a
  negative lookbehind for `%`, or filling any curl command raises `unknown placeholder`.

## The central inventory

`inventory.py` is the only authority for names, addresses and credentials, and it keeps
two views of the registry apart:

- **Public** — `load()`, `names()`, `require()`, `as_prompt()`. Logical name, type,
  domain, site, description, endpoint. This is what reaches the model, the tool enums
  and the LangGraph state. It is built from a field **allowlist** (`_PUBLIC_FIELDS`), so
  a key nobody anticipated cannot ride along into a prompt.
- **Private** — `credentials(name)`. Resolved at call time, never cached, never returned
  into graph state. A test asserts `transport.py` is its **only** caller, so the
  credential path has one chokepoint the way command execution does.

Agents address systems by logical name (`"magos"`, `"camera_ptz_1"`). A name that does
not resolve is refused, so the model cannot reach anything not in the registry and
cannot name a host at all.

### The registry file

    cp systems_inventory.example.yaml systems_inventory.yaml && $EDITOR $_

Loaded if present (`SYSTEMS_INVENTORY` overrides the path), and it **overlays** the
gotcha30 deployment config on matching names — so a node the launcher runs can gain a
site and access details without being listed twice. Absent, nothing changes.

**The file may never contain a secret value.** It holds `password_env:` — the *name* of
an environment variable — or `key_file:`, a path. `inventory.py` walks the document
before anything else touches it and raises `InventoryError` naming the offending key if
it finds an inline `password`, `token`, `secret`, `api_key` and so on. A secret inside a
file that gets read into a model-facing process is a problem no later redaction fixes,
so it fails at startup instead. That refusal is also what makes the file safe to commit;
gitignore it anyway if mapping your estate is itself sensitive.

### Secrets that cannot be stringified by accident

`credentials()` returns any password wrapped in `inventory.Secret`, which renders as
`***` through `str()`, `repr()`, f-strings, `%s` and `json.dumps(default=str)` — that
last one being exactly the path `registry.call()` uses to serialise a tool payload, and
therefore the way a credential would otherwise reach graph state. `reveal()` is the one
deliberate way out, and it is called in exactly one place.

### Targeted execution

`transport.run_on(key, node)` runs an allowlisted command against one system:

```python
"remote_uptime": ("ssh", "-p", "{ssh_port}", "{ssh_user}@{host}", "uptime"),
```

The placeholders are filled from the inventory record for a **logical name** — they are
not free-form and cannot come from the model. An unknown placeholder is fatal rather
than passed through. `run_on` injects `ConnectTimeout` and `StrictHostKeyChecking`
itself rather than trusting the table, so a console edit cannot quietly drop them.

If a password is configured it goes into the child process **environment** (`SSHPASS`,
with `sshpass -e` as ssh's direct parent), never onto an argv where `ps` shows it to
every local user. With key auth instead, `BatchMode=yes` is set so a call fails rather
than hanging on a prompt. In mock mode `run_on` reads a fixture and never resolves a
credential at all — a test asserts this, because console sessions run in mock mode and
must not touch the estate.

### Driving routing from the registry

A system may declare `domain: radar` outright. `graph._extract_context` prefers that
over the `DOMAINS` type table, and emits `<domain>_targets` for any domain the registry
names — so a new hardware class becomes routable by editing the registry rather than
`graph.py`. The template's `camera_ptz_1` declares `domain: optic`, which produces
`optic_targets` today even though no optic expert exists yet.

## Customer communicator

`customer_communicator` runs between `synthesize` and the approval gate. It is a
*post-synthesis* agent: it gathers no evidence, takes no tools, and is not in the
supervisor pool — it rewrites the technical report as a client-facing message.

Its draft is not trusted. `graph._scan_for_leaks()` checks it for IP addresses, node
names, PIDs, paths, topic names, internal status fields, tool names and container names,
and `safe_to_send` is false if any are found **or** if the report escalates or is
low-confidence. "Do not leak internal identifiers" is the kind of instruction a model
follows 95% of the time, and 95% is not good enough for outbound customer mail.

## Slack

`slack_app.py` is an **adapter, not a node**. The graph knows nothing about Slack; a test
asserts `graph.py`, `agents.py` and `llm.py` never mention it.

    pip install slack-bolt
    export SLACK_BOT_TOKEN=xoxb-...        SLACK_APP_TOKEN=xapp-...
    export SLACK_ALLOWED_CHANNELS=C0...    SLACK_ADMIN_CHANNEL=C0...
    python slack_app.py

**Socket Mode**, so no inbound port is exposed — the app holds an outbound WebSocket and
the `xapp-` token authenticates it, which is why there is no request signature to verify.
Note it needs *outbound* egress to Slack; Socket Mode solves inbound firewall rules only.

### The audience is Tier 1, not the customer

Engineers get the **technical report**: root cause, confidence, evidence quoting real tool
output, the checks that could not run, which agents ran and which tools failed, and the
trace path. They do **not** get the `customer_communicator` draft — that agent exists to
strip node names, IPs, PIDs, paths, topic names and tool names, which is precisely what an
engineer needs to act. The draft is still produced; `/draft <session>` posts it, with its
`safe_to_send` state and any leak flags, for when someone is about to forward something to
a client.

A report from a mock-mode run says so, in the acknowledgement and again in the report. A
fixture answer must not be mistaken for a live diagnosis.

### One message is one session

Never resume a checkpoint for a new question. `findings` and `visited` are `operator.add`,
so a reused thread has every agent already visited: `_eligible()` returns nothing, the
supervisor goes straight to `synthesize`, and it re-summarises the **old** evidence against
the **new** question — silently. Each Slack message therefore gets a fresh `session_id`,
and a test asserts the only `Command(resume=...)` in the file is the approval path.

Every run writes `traces/<session_id>.jsonl` in the same shape the CLI and console use,
plus an `origin` block recording who asked, in which channel, and in which mode. An
interface with no audit trail is the wrong one to make primary.

### Admission is fail-closed

`SLACK_ALLOWED_CHANNELS` and `SLACK_ALLOWED_USERS` are checked **before the graph runs**.
With neither set the bot refuses everyone — workspace membership is not authorisation, any
member can DM a bot, and guests or Slack Connect users may be in the workspace. DMs are off
unless `SLACK_ALLOW_DMS=1` *and* the user is named. There is a per-user cooldown and a
concurrency cap, because a chat surface invites casual use and every run is several model
calls at `effort: high`.

Socket Mode still redelivers envelopes, so deduplication on `event_id` survived the port
from the Events API even though signature verification did not.

### Approval stays an admin decision

Writing to `kb/cases/` is admin business, so it defaults to **the console**. The interrupt
is announced in `SLACK_ADMIN_CHANNEL`, never in the engineer's thread. `/approve` works
only if `SLACK_APPROVERS` names people, and then only for those users and only in the admin
channel.

### Config edits reach the bot without a restart

`build()` reads the agent set at build time, so the gateway stats `overrides.json` on each
request and rebuilds when it changes. Without it, an admin reordering agents in the console
would see no effect in Slack until someone restarted the process — a silent seam, and
exactly the one this split creates.

Concurrent sessions share one SQLite checkpoint file, so the gateway enables WAL at
startup; without it the second simultaneous diagnosis meets `database is locked`.
