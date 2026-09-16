"""Isolate every test from the operator's live configuration.

tests/ must not depend on anything a person edits at runtime — systems_inventory.yaml,
secrets.local.env, or overrides.json. Those are live operator data: deleting an agent in
the console is a legitimate thing to do, and it must not turn the suite red. So each test
starts from the code defaults, and a test that wants an override writes one itself.

Any test may still point SYSTEMS or config_store.PATH at its own fixture — monkeypatch in
a requested fixture runs after an autouse one, so the local setting wins.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store  # noqa: E402
import inventory  # noqa: E402
import secrets_store  # noqa: E402


def _clear() -> None:
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()


@pytest.fixture(autouse=True)
def isolate_operator_state(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("operator")
    monkeypatch.setattr(inventory, "SYSTEMS", d / "systems_inventory.yaml")
    monkeypatch.setattr(secrets_store, "PATH", d / "secrets.local.env")
    # Agents, order, tool descriptions and the command table all live here; without this
    # an agent deleted in the console fails every test that reads the effective config.
    monkeypatch.setattr(config_store, "PATH", d / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", d / "config_audit.jsonl")
    _clear()
    yield
    _clear()
