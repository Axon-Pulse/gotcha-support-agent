"""Command transport: the only place that runs anything.

Commands are KEYS into a frozen table, never strings built from model input.
No shell=True, ever — ticket text and log lines are attacker-influenced input.

Mock vs live is resolved once, here. Tool code is mode-blind, so a fixture run
exercises byte-identical parser paths to a live run.
"""
import os
import subprocess
from pathlib import Path

import config_store

DEFAULT_ALLOWED: dict[str, tuple[str, ...]] = {
    "topology": ("ecal_mon_cli", "-l"),
    "health":   ("ecal_mon_cli", "--proto", "/system/health", "-c", "40"),
    "launcher": ("ecal_mon_cli", "--proto", "/launcher/status", "-c", "2"),
}



def allowed() -> dict[str, list]:
    """Effective command table: code defaults, overlaid with console edits."""
    return {k: list(v) for k, v in
            config_store.get("allowed_commands", DEFAULT_ALLOWED).items()}


# Which fixture file backs each command in mock mode.
FIXTURES = {
    "topology": "topics_faulty.txt",
    "health":   "health_faulty.txt",
    "launcher": "launcher_status.txt",
}

MODE = os.environ.get("AGENT_MODE", "mock")
FIXTURE_DIR = Path(os.environ.get("FIXTURE_DIR", "tests/fixtures"))


def run(key: str, timeout: int = 10) -> str:
    """Run an allowlisted command (live) or read its fixture (mock)."""
    table = allowed()
    if key not in table:
        raise KeyError(f"command {key!r} is not allowlisted: {sorted(table)}")
    if MODE == "mock":
        path = FIXTURE_DIR / FIXTURES.get(key, f"{key}.txt")
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {key!r} at {path}")
        return path.read_text()
    return subprocess.run(
        ["timeout", str(timeout), *table[key]],
        capture_output=True, text=True, timeout=timeout + 5,
    ).stdout


def run_argv(argv: list[str], timeout: int = 10) -> str:
    """Run a validated argv list. Callers MUST have validated every element.

    Used only by net.py, whose arguments are parsed as ipaddress/int before arrival.
    """
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list of str")
    return subprocess.run(
        ["timeout", str(timeout), *argv],
        capture_output=True, text=True, timeout=timeout + 5,
    ).stdout
