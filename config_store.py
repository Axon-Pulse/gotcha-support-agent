"""Runtime overrides layered on top of the code defaults.

The console writes here; the code defaults in agents.py / transport.py / tools/ stay
untouched. Only keys actually present in overrides.json override anything, so deleting
a key reverts to the code default.

Two properties are preserved deliberately:

1. The AGENT cannot write this file. No tool touches it; the console API does, and the
   console is a human at localhost. The invariant is "the agent cannot widen its own
   permissions", not "permissions never change".
2. overrides.json is git-tracked and every write is appended to config_audit.jsonl, so
   a change still shows up in a diff and in an audit trail.
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "overrides.json"
AUDIT = ROOT / "config_audit.jsonl"

KEYS = ("agents", "post_agents", "order", "requires", "supervisor_picks",
        "tool_descriptions", "allowed_commands")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ConfigError(ValueError):
    """A rejected edit. The message is shown to the operator."""


def load() -> dict[str, Any]:
    if not PATH.exists():
        return {}
    try:
        data = json.loads(PATH.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"overrides.json is not valid JSON: {e}") from e
    return {k: v for k, v in data.items() if k in KEYS}


def _write(data: dict, who: str, what: str) -> None:
    PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "by": who, "change": what,
                            "snapshot": data}, sort_keys=True) + "\n")


def get(key: str, default: Any) -> Any:
    """Effective value for one key: override if present, else the code default."""
    return load().get(key, default)


def put(key: str, value: Any, who: str = "console", note: str = "") -> dict:
    if key not in KEYS:
        raise ConfigError(f"unknown config key {key!r}")
    data = load()
    data[key] = value
    _write(data, who, note or f"set {key}")
    return data


def clear(key: str | None = None, who: str = "console") -> dict:
    """Drop one override (or all), reverting to the code defaults."""
    data = {} if key is None else {k: v for k, v in load().items() if k != key}
    _write(data, who, f"reset {key or 'all'}")
    return data


# ---------- validation ----------

def validate_agents(agents: dict, known_tools: set[str]) -> None:
    if not isinstance(agents, dict) or not agents:
        raise ConfigError("at least one agent is required")
    for name, spec in agents.items():
        if not _NAME.match(name):
            raise ConfigError(f"agent name {name!r} must be lowercase alphanumeric/underscore")
        if name in {"supervisor", "synthesize", "propose_scenario", "save", "start", "end"}:
            raise ConfigError(f"{name!r} is a reserved graph node name")
        if not isinstance(spec, dict):
            raise ConfigError(f"agent {name!r} must be an object")
        if not str(spec.get("prompt", "")).strip():
            raise ConfigError(f"agent {name!r} needs a prompt")
        tools = spec.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ConfigError(f"agent {name!r}: tools must be a list of names")
        unknown = [t for t in tools if t not in known_tools]
        if unknown:
            raise ConfigError(f"agent {name!r} references unregistered tools: {unknown}")
        if not tools:
            raise ConfigError(f"agent {name!r} has no tools and could not gather evidence")


def validate_flow(order: list, requires: dict, agent_names: set[str]) -> None:
    if not isinstance(order, list) or not order:
        raise ConfigError("order must list at least one agent")
    unknown = [a for a in order if a not in agent_names]
    if unknown:
        raise ConfigError(f"order references undefined agents: {unknown}")
    if len(set(order)) != len(order):
        raise ConfigError("order contains duplicates")
    for node, deps in (requires or {}).items():
        if node not in agent_names:
            raise ConfigError(f"requires references undefined agent {node!r}")
        if not isinstance(deps, list):
            raise ConfigError(f"requires[{node!r}] must be a list")
        for d in deps:
            if d not in agent_names:
                raise ConfigError(f"requires[{node!r}] references undefined agent {d!r}")
            if d == node:
                raise ConfigError(f"agent {node!r} cannot require itself")
    # Cycles would deadlock the supervisor: nothing would ever become eligible.
    seen, stack = set(), set()

    def walk(n: str) -> None:
        if n in stack:
            raise ConfigError(f"requires has a cycle through {n!r}")
        if n in seen:
            return
        stack.add(n)
        for d in (requires or {}).get(n, []):
            walk(d)
        stack.discard(n)
        seen.add(n)

    for n in agent_names:
        walk(n)
    # An agent whose prerequisites are not in order can never run.
    pos = {a: i for i, a in enumerate(order)}
    for node, deps in (requires or {}).items():
        if node not in pos:
            continue
        for d in deps:
            if d not in pos:
                raise ConfigError(f"{node!r} requires {d!r}, which is not in order")
            if pos[d] > pos[node]:
                raise ConfigError(f"{node!r} requires {d!r} but runs before it in order")


_SHELLISH = {"sh", "bash", "zsh", "dash", "ksh", "csh", "fish", "env", "eval", "xargs"}


def validate_commands(cmds: dict) -> list[str]:
    """Structural checks. Returns warnings for the operator, does not block."""
    if not isinstance(cmds, dict):
        raise ConfigError("allowed_commands must be an object")
    warnings = []
    for key, argv in cmds.items():
        if not _NAME.match(key):
            raise ConfigError(f"command key {key!r} must be lowercase alphanumeric/underscore")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ConfigError(f"command {key!r} must be a non-empty list of strings")
        if any(not a.strip() for a in argv):
            raise ConfigError(f"command {key!r} has an empty argument")
        binary = os.path.basename(argv[0])
        if binary in _SHELLISH:
            warnings.append(
                f"{key!r} runs {binary!r}, which can execute arbitrary commands — "
                f"the argv-list protection does not apply to a shell.")
        if any(c in " ".join(argv) for c in ";|&`$(){}<>"):
            warnings.append(
                f"{key!r} contains shell metacharacters. They are NOT interpreted "
                f"(no shell is used), so they will be passed through literally.")
    return warnings


def validate_post_agents(post: dict) -> None:
    """Post-synthesis agents transform text; they take no tools by design."""
    if not isinstance(post, dict):
        raise ConfigError("post_agents must be an object")
    for name, spec in post.items():
        if not _NAME.match(name):
            raise ConfigError(f"post agent name {name!r} must be lowercase alphanumeric")
        if not str((spec or {}).get("prompt", "")).strip():
            raise ConfigError(f"post agent {name!r} needs a prompt")
        if (spec or {}).get("tools"):
            raise ConfigError(
                f"post agent {name!r} cannot have tools — it runs after the evidence "
                f"phase and only transforms the report")
