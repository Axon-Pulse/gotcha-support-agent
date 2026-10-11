"""Launcher / process truth from /launcher/status.

This is NOT redundant with get_system_health. A node that failed to spawn never
publishes health, so it is invisible to every health-based view — only the launcher
knows it should exist. `config_path` also catches "you are debugging the wrong config",
which is otherwise unknowable.
"""
import re

import transport
from registry import tool

_SECRET = re.compile(r"(--(?:password|passwd|secret|token|api[-_]?key|username|user)[= ])\S+", re.I)
_USERINFO = re.compile(r"(https?://)[^/@\s]+@")


def _redact(cmd: str) -> str:
    """The launcher builds command lines containing plaintext passwords (x-cli flags)."""
    return _USERINFO.sub(r"\1***@", _SECRET.sub(r"\1***", cmd))


@tool({
    "name": "get_process_table",
    "description": (
        "Launcher view: which nodes the config says should run, their process state, PID, "
        "exit code and uptime. Shows nodes that NEVER STARTED — these publish no health "
        "and are invisible to get_system_health. Also reports config_path, which catches "
        "the case where the wrong config is loaded."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
})
def get_process_table() -> dict:
    text = transport.run("launcher")
    top, nodes, cur, depth = {}, [], {}, 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("{"):
            depth += 1
            if depth == 1:
                cur = {}
            continue
        if line == "}":
            depth -= 1
            if depth == 0 and cur:
                nodes.append(cur)
                cur = {}
            continue
        if ":" not in line or line.startswith("[eCAL]"):
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        val = v.strip('"') if v.startswith('"') else v
        if not v.startswith('"'):
            try:
                val = int(v)
            except ValueError:
                val = v
        (cur if depth else top)[k] = val

    rows = [{"name": n.get("name"), "state": n.get("state"), "pid": n.get("pid"),
             "exit_code": n.get("exit_code"),
             "uptime_s": round((n.get("uptime_ms") or 0) / 1000),
             "cmd": _redact(str(n.get("command", "")))[:200]} for n in nodes]
    return {
        "launcher_state": top.get("launcher_state"),
        "session_title": top.get("session_title"),
        "config_path": top.get("config_path"),
        "total": top.get("total_nodes"), "running": top.get("running_nodes"),
        "stopped": top.get("stopped_nodes"), "failed": top.get("failed_nodes"),
        "nodes": rows,
        "never_started": [r["name"] for r in rows if r["state"] in
                          ("PROC_NOT_STARTED", "PROC_FAILED_TO_START")],
        "exited_with_error": [{"name": r["name"], "exit_code": r["exit_code"]}
                              for r in rows if r["exit_code"]],
    }
