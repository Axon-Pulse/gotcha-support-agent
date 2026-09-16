"""Shared shape for domain tools that are DECLARED but not yet IMPLEMENTED.

A placeholder that invents plausible numbers is worse than no tool at all: the model
would cite them, `synthesize` would quote them as evidence, and the report would be
confidently wrong about hardware nobody looked at. So these return no measurements.
They run the one read-only liveness probe their domain has a permitted command for and
say plainly that the domain query does not exist yet.

Three properties matter, and each is asserted by a test:

- The payload carries NO `nodes` key. `graph._extract_context` reads `nodes` out of tool
  results to decide which agents may run, so a placeholder contributing there would gate
  real agents on invented evidence.
- `implemented: false` is in every payload, so the result cannot be read as a clean bill
  of health for the unit.
- A probe failure is captured into the payload rather than raised. "The radar did not
  answer" is a finding; it should not look like a broken tool.

This module registers no tools of its own — it is imported by the ones in radar.py,
camera.py and tower.py.
"""
import inventory
import transport


def probe(node: str, *, domain: str, command: str, planned: str) -> dict:
    """Liveness-probe one node and return an explicitly unimplemented payload."""
    rec = inventory.require(node)          # refuses anything not in the inventory
    declared = rec.get("domain")
    if declared and declared != domain:
        return {
            "implemented": False,
            "refused": (f"{node!r} is in the {declared!r} domain; this tool covers "
                        f"{domain!r}. Use the {declared} domain's tool instead."),
            "node": node, "type": rec.get("type"), "declared_domain": declared,
        }

    out: dict = {
        "implemented": False,
        "node": node,
        "type": rec.get("type"),
        "declared_domain": declared,
        "hardware": rec.get("hardware"),
        "software_version": rec.get("software_version"),
        "planned": planned,
        "note": ("This is a liveness probe only. It says whether the unit answers, and "
                 "nothing about whether it is working correctly. Do not report a "
                 "domain diagnosis on this result — say the domain check does not "
                 "exist yet and what you would need."),
    }
    try:
        raw = transport.run_on(command, node).strip()
        out["probe"] = {"command_key": command, "ok": True, "output": raw[:300]}
    except Exception as e:  # noqa: BLE001 - a failed probe is signal, not a crash
        out["probe"] = {"command_key": command, "ok": False,
                        "error": f"{type(e).__name__}: {e}"}
    return out


def schema(name: str, domain: str, will: str) -> dict:
    """Tool schema with the placeholder status stated first, where the model reads it."""
    return {
        "name": name,
        "description": (
            f"PLACEHOLDER — the {domain} domain check is NOT implemented yet. Today this "
            f"only probes whether the named unit answers at all; it returns no {domain} "
            f"measurements of any kind. When implemented it will {will} Call it to "
            f"establish reachability, then say plainly that the {domain} diagnosis could "
            f"not be performed — never present its output as a {domain} finding."),
        "input_schema": {
            "type": "object",
            "properties": {"node": {"type": "string", "enum": inventory.names(),
                                    "description": "Logical name from the inventory."}},
            "required": ["node"], "additionalProperties": False,
        },
    }
