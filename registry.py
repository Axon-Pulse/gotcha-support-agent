"""Tool registry.

Adding a tool: drop a module in tools/ and decorate a function with @tool({...}).
It is discovered automatically by load_tools().

A module may declare SIDE_EFFECT = "write" to mark its tools as mutating. Registration
of those is REFUSED unless allow_writes=True, so the agent cannot be handed a write tool
by accident. Writing to the knowledge base happens only in the graph's `save` node,
downstream of a human approval.
"""
import importlib
import json
import logging
import pkgutil
import sys

log = logging.getLogger(__name__)

REGISTRY: dict[str, dict] = {}
_MAX_RESULT_BYTES = 20_000


class WriteToolRefused(RuntimeError):
    """A module marked SIDE_EFFECT='write' tried to register while writes are disallowed."""


def tool(schema: dict):
    """Register a function as a model-callable tool."""
    def deco(fn):
        mod = sys.modules[fn.__module__]
        side_effect = getattr(mod, "SIDE_EFFECT", "none")
        REGISTRY[schema["name"]] = {
            "schema": schema, "fn": fn, "side_effect": side_effect,
        }
        return fn
    return deco


def load_tools(allow_writes: bool = False) -> dict[str, dict]:
    """Import every module in tools/ and return the registry."""
    import tools
    for m in pkgutil.iter_modules(tools.__path__):
        importlib.import_module(f"tools.{m.name}")
    offenders = [n for n, t in REGISTRY.items() if t["side_effect"] != "none"]
    if offenders and not allow_writes:
        raise WriteToolRefused(
            f"tools declare SIDE_EFFECT != 'none' and no approval node is wired: {offenders}"
        )
    return REGISTRY


def schemas(names: list[str] | None = None) -> list[dict]:
    return [t["schema"] for n, t in REGISTRY.items() if names is None or n in names]


def call(name: str, args: dict) -> dict:
    """Invoke a tool. Never raises — a failure is diagnostic signal, not a crash."""
    if name not in REGISTRY:
        return {"ok": False, "error": "unknown_tool", "valid": sorted(REGISTRY)}
    try:
        data = REGISTRY[name]["fn"](**args)
        payload = {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001 - surfaced to the model, not swallowed
        log.warning("tool %s failed: %s", name, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    raw = json.dumps(payload, default=str)
    if len(raw) > _MAX_RESULT_BYTES:
        # A tool that returns this much has a parser bug. Say so rather than
        # silently truncating structured data into something misleading.
        log.error("tool %s returned %d bytes; parser needs tightening", name, len(raw))
        return {"ok": False, "error": "result_too_large",
                "bytes": len(raw), "limit": _MAX_RESULT_BYTES,
                "hint": "tool must summarise, not dump"}
    return payload
