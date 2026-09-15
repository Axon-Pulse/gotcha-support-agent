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
| `transport.py` | **The only code that runs a command.** Allowlist + mock/live switch. |
| `inventory.py` | The node allowlist, built from a gotcha30 config. Drops credentials. |
| `llm.py` | Anthropic calls: model, thinking, effort, prompt caching. |
| `state.py` | Graph state; `Finding` and `Scenario` shapes. |
| `tools/*.py` | **One file per diagnostic area.** Drop a new file here to add a tool. |
| `kb/*.md` | **Knowledge.** Plain markdown, loaded into the cached system prompt. |
| `tests/` | Fixtures from a real faulty run + the safety assertions. |
| `traces/` | One JSONL per session: findings, transcript, report. |
| `graph.db` | Checkpoints. Lets a pending approval survive a restart. |

## Operating it

    cp .env.example .env && $EDITOR .env      # ANTHROPIC_API_KEY, GOTCHA30_REPO
    .venv/bin/python run.py "the acoustic sensor shows no tracks"

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

**Add knowledge** — drop a `.md` in `kb/`. Loaded at startup into the cached prefix.

**Add an agent** — an entry in `agents.py` (prompt + tool names), then put it in `ORDER`.

**Change ordering** — `ORDER` is the sequence, `REQUIRES` is a hard precondition the
supervisor cannot violate. `SUPERVISOR_PICKS = False` turns it into a fixed pipeline.

**Permissions** live in three places, all in code, all reviewable:

1. `transport.ALLOWED` — the commands that may ever run. Keys, not strings; no `shell=True`.
2. `inventory.py` — the nodes that may be addressed. The model cannot name a host.
3. `registry.load_tools()` — refuses any module declaring `SIDE_EFFECT = "write"`.

## The approval gate

`synthesize` may *propose* a new runbook entry. `propose_scenario` then calls
`interrupt()`: the graph stops, state is checkpointed, and you get `[y / e = edit / N]`.
Only on `y` does `save` write to `kb/`. The agent has no write tool, so it cannot add a
scenario on its own.

Same seam covers remediation later: give a `restart_node` tool `SIDE_EFFECT = "write"`
and it is refused at registration until an approval node is wired in front of it.

## Tests

    .venv/bin/python -m pytest tests/ -q

## Console

    .venv/bin/python -m uvicorn console.server:app --port 8765
    # then open http://localhost:8765

Six tabs: **Run** (mock sessions; watch each agent fire, with per-agent cache and token
counts), **Approvals** (the queue — edit the markdown in place, then approve or reject),
**Traces**, **Knowledge** (edit runbook documents and create new ones), **Graph** (the
diagram plus the flow editor), and **Tools & permissions**.

The console sets `AGENT_MODE=mock` before importing anything, so it cannot touch the real
system. Live runs stay a deliberate command-line act.

### Editing configuration from the UI

**Tools & permissions** edits agent prompts, which tools each agent may call, the tool
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

The **addressable node list stays read-only**: it is derived from the gotcha30 config
named by `GOTCHA30_CONFIG`, with credential keys dropped during parsing. Point the agent
at another deployment by changing that env var, not by typing hosts into a box.

### Knowledge

Documents are editable in place and new ones start from the Symptoms / Root cause /
Checks / Fix skeleton the seeded runbook entries use. Names are kebab-case and every
write is resolved against `kb/` before it lands, so a name cannot escape the directory.

Because the whole of `kb/` is concatenated into the cached system prompt at session
start, an edit reaches the **next** run, not one already in flight — and it invalidates
the prompt cache, so the first session after an edit pays full input price once.

### Graph

The **Graph** tab draws the execution graph: START → supervisor → agents (each looping
back) → synthesize → the approval gate → `save` or END. Dashed amber edges are `requires`
preconditions; green and red are the approve/reject branches. Agents defined but missing
from `order` are greyed out, since the supervisor can never pick them. LangGraph's own
mermaid export is included below the diagram. The **flow editor** lives here too — editing
the order or the `requires` preconditions redraws the diagram immediately.


## Prerequisites: `requires` vs `needs_context`

`agents.py` declares two kinds of gate, both enforced in `graph._eligible()`:

- **`requires`** — agent B cannot run until agent A has run at all. Pure ordering.
- **`needs_context`** — agent B cannot run until a named fact has actually been
  *extracted from tool output*. `network` needs `probe_targets`, so if `triage` runs but
  every tool errors, `network` stays ineligible instead of probing the whole inventory on
  a guess. The reason lands in `state["blocked"]` and reaches the report.

Context is extracted by `graph._extract_context()` from the **structured tool payloads**,
never from the model's prose — an agent that narrates "the radar looks unreachable"
without a tool returning a node must not unlock the network agent.

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

    pip install slack-sdk
    export SLACK_BOT_TOKEN=xoxb-... SLACK_SIGNING_SECRET=... SLACK_INTERNAL_CHANNEL=C0...
    uvicorn slack_app:app --port 3000

Four things it handles that matter in production: the 3-second ACK (diagnosis runs in a
background thread, Slack gets 200 immediately); deduplication on `event_id`, since Slack
resends on timeout; routing the **approval interrupt to a staff channel, never the
customer thread**; and holding a leaked or escalating draft for review instead of sending
it, with the customer getting a neutral holding reply.
