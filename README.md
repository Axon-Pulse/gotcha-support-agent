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

Five tabs: **Run** (mock sessions; watch each agent fire, with per-agent cache and token
counts), **Approvals** (the queue — edit the markdown in place, then approve or reject),
**Traces**, **Knowledge**, and **Tools & permissions**.

The console sets `AGENT_MODE=mock` before importing anything, so it cannot touch the real
system. Live runs stay a deliberate command-line act.

The permission surface is shown **read-only** on purpose. Widening it should be a code
change that appears in a diff and a test run, not a button.
