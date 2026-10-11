"""Isolate every test from the operator's live configuration.

tests/ must not depend on anything a person edits at runtime — systems_inventory.yaml,
secrets.local.env, overrides.json, .env, or the gotcha30 deployment config. Those are
live operator data: deleting an agent in the console, renaming the config on the bench,
or finally putting an API key in .env is a legitimate thing to do and must not turn the
suite red. So each test starts from the code defaults, and a test that wants an override
writes one itself.

Any test may still point SYSTEMS or config_store.PATH at its own fixture — monkeypatch in
a requested fixture runs after an autouse one, so the local setting wins.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store  # noqa: E402
import env_file  # noqa: E402
import inventory  # noqa: E402
import secrets_store  # noqa: E402

# AT MODULE LEVEL, not in a fixture, and this is the whole point: console/server.py,
# run.py and slack_app.py call env_file.load() at IMPORT, so by the time any fixture
# runs the operator's .env has already been merged into os.environ — permanently, for
# the rest of the session, where no monkeypatch can see or undo it. conftest is imported
# before any of them, so pointing PATH at a file that does not exist is the only place
# the load can still be made a no-op.
#
# Found the hard way: a developer created a .env holding an empty ANTHROPIC_API_KEY and
# a subprocess test three files away started failing, because "" was now exported into
# every test and env_file deliberately never overrides what is already set.
env_file.PATH = ROOT / "tests" / "fixtures" / "no-such.env"


def _clear() -> None:
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()


@pytest.fixture(autouse=True)
def isolate_operator_state(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("operator")
    monkeypatch.setattr(inventory, "SYSTEMS", d / "systems_inventory.yaml")
    # The gotcha30 deployment config is operator data too — a sibling repo whose config
    # they rename between bench runs. Pin node identity to a fixture so the suite does
    # not go red because a file outside this repo moved.
    monkeypatch.setattr(inventory, "REPO", ROOT / "tests" / "fixtures")
    monkeypatch.setattr(inventory, "CONFIG", "gotcha30_config.yaml")
    monkeypatch.setattr(secrets_store, "PATH", d / "secrets.local.env")
    # Agents, order, tool descriptions and the command table all live here; without this
    # an agent deleted in the console fails every test that reads the effective config.
    monkeypatch.setattr(config_store, "PATH", d / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", d / "config_audit.jsonl")
    _clear()
    yield
    _clear()
