"""The central registry: what the model may see, and what only transport may resolve.

The property under test is not "we redact credentials" but "the model-facing view is
built from an allowlist and never contains them in the first place", plus "the one
place that resolves a secret is transport.py".
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inventory  # noqa: E402
import transport  # noqa: E402
import graph as G  # noqa: E402
from registry import REGISTRY, call, load_tools  # noqa: E402

load_tools()
FIXTURES = ROOT / "tests" / "fixtures"
PW = "correct-horse-battery-staple"


def _clear():
    inventory.load.cache_clear()
    inventory._systems_doc.cache_clear()
    inventory._access.cache_clear()


@pytest.fixture
def registry(monkeypatch):
    """Point inventory at a test registry, with the caches dropped either side."""
    def use(filename: str):
        monkeypatch.setattr(inventory, "SYSTEMS", FIXTURES / filename)
        _clear()
        return inventory
    _clear()
    yield use
    _clear()


@pytest.fixture
def estate(registry, monkeypatch):
    monkeypatch.setenv("TEST_MAGOS_SSH_PW", PW)
    return registry("systems_inventory.yaml")


# ---------- 1. Secret cannot be stringified by accident ----------

def test_secret_redacts_through_every_path():
    s = inventory.Secret(PW, "env:TEST_MAGOS_SSH_PW")
    assert str(s) == "***"
    assert PW not in repr(s)
    assert PW not in f"{s}"
    assert PW not in "%s" % s
    assert PW not in "{}".format(s)
    # registry.call() serialises tool payloads with default=str — the path a secret
    # would take into a tool result, and from there into graph state.
    assert PW not in json.dumps({"password": s}, default=str)
    assert s.reveal() == PW, "the deliberate way out still works"


def test_secret_survives_being_logged(caplog):
    import logging
    logging.getLogger("t").warning("creds=%s %r", inventory.Secret(PW, "env:X"),
                                   inventory.Secret(PW, "env:X"))
    assert PW not in caplog.text


# ---------- 2. an inline secret is a load error, not a redaction problem ----------

def test_inline_secret_is_refused_with_the_offending_key(registry):
    registry("systems_inventory_insecure.yaml")
    with pytest.raises(inventory.InventoryError) as e:
        inventory.load()
    assert "password" in str(e.value)
    assert "password_env" in str(e.value), "the error must say how to fix it"


def test_a_clean_registry_loads(estate):
    assert "camera_ptz_1" in inventory.names()


# ---------- 3. the public view is an allowlist ----------

def test_public_record_carries_no_access_material(estate):
    rec = inventory.require("magos")
    assert set(rec) == set(inventory._PUBLIC_FIELDS)
    blob = json.dumps(inventory.load(), default=str)
    for forbidden in ("access", "ssh", "password_env", "key_file", "TEST_MAGOS_SSH_PW",
                      ".ssh/gotcha_magos", PW):
        assert forbidden not in blob, f"{forbidden!r} reached the public inventory"


def test_the_model_prompt_carries_no_access_material(estate):
    text = inventory.as_prompt()
    for forbidden in ("password", "key_file", "TEST_MAGOS_SSH_PW", PW, "gotcha_magos"):
        assert forbidden not in text
    assert "magos" in text and "Magos AR-300" in text, "useful context still gets through"


def test_no_tool_output_contains_a_credential(estate):
    for name, t in REGISTRY.items():
        if t["schema"]["input_schema"].get("required"):
            continue
        assert PW not in json.dumps(call(name, {}), default=str), f"{name} leaked"


# ---------- 4. the private lookup ----------

def test_credentials_resolve_from_the_environment_at_call_time(estate):
    c = inventory.credentials("magos")
    assert c["host"] == "192.168.40.60" and c["user"] == "magos" and c["port"] == 22
    assert isinstance(c["password"], inventory.Secret)
    assert c["password"].reveal() == PW
    assert c["key_file"].endswith(".ssh/gotcha_magos") and "~" not in c["key_file"]


def test_an_unset_env_var_yields_no_password_rather_than_a_crash(estate, monkeypatch):
    monkeypatch.delenv("TEST_MAGOS_SSH_PW")
    _clear()
    assert inventory.credentials("magos")["password"] is None


def test_credentials_refuse_an_unknown_name(estate):
    with pytest.raises(KeyError):
        inventory.credentials("not_a_system")


def test_a_system_with_no_access_block_has_no_credentials(estate):
    assert not inventory.has_credentials("asu")
    with pytest.raises(KeyError, match="nothing to log into"):
        inventory.credentials("asu")


def _calls_credentials(source: str) -> bool:
    """Real calls only. An AST walk cannot be fooled by prose in a docstring, and
    cannot be evaded by importing the name under an alias."""
    import ast
    tree = ast.parse(source)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "inventory":
            if any(a.name == "credentials" for a in n.names):
                return True
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == "credentials"
                    and isinstance(f.value, ast.Name) and f.value.id == "inventory"):
                return True
    return False


def test_credentials_has_exactly_one_caller():
    """The credential path has one chokepoint, like command execution does."""
    callers = sorted(f.name for f in
                     [*ROOT.glob("*.py"), *(ROOT / "tools").glob("*.py"),
                      *(ROOT / "console").glob("*.py")]
                     if f.name != "inventory.py" and _calls_credentials(f.read_text()))
    assert callers == ["transport.py"], f"credentials resolved outside transport: {callers}"


# ---------- 5. run_on: the secret goes to the environment, never an argv ----------

def _live(monkeypatch):
    seen = {}

    class Result:
        stdout = "ok"

    monkeypatch.setattr(transport, "MODE", "live")
    monkeypatch.setattr(transport.subprocess, "run",
                        lambda argv, **kw: (seen.update(argv=argv, env=kw.get("env"))
                                            or Result()))
    return seen


def test_password_reaches_the_child_env_and_never_the_argv(estate, monkeypatch):
    seen = _live(monkeypatch)
    transport.run_on("remote_uptime", "magos")
    flat = " ".join(seen["argv"])
    assert PW not in flat, "`ps` would show this to every local user"
    assert seen["env"]["SSHPASS"] == PW
    assert seen["argv"][:2] == ["timeout", "15"], "the timeout bounds the whole call"
    i = seen["argv"].index("sshpass")
    assert seen["argv"][i + 1] == "-e" and seen["argv"][i + 2] == "ssh", \
        "sshpass must be ssh's direct parent to own the pty"
    assert "magos@192.168.40.60" in flat and "uptime" in flat


def test_key_auth_uses_batchmode_and_the_identity_file(registry, monkeypatch):
    registry("systems_inventory_keyed.yaml")
    seen = _live(monkeypatch)
    transport.run_on("remote_uptime", "magos")
    flat = " ".join(seen["argv"])
    assert "SSHPASS" not in seen["env"]
    assert "sshpass" not in flat
    assert "BatchMode=yes" in flat, "must fail rather than hang on a password prompt"
    assert "-i" in seen["argv"] and any(a.endswith("gotcha_magos") for a in seen["argv"])
    assert "-p 2222" in flat, "the per-system port is honoured"


def test_hardening_options_are_injected_not_taken_from_the_table(estate, monkeypatch):
    seen = _live(monkeypatch)
    transport.run_on("remote_uptime", "magos")
    flat = " ".join(seen["argv"])
    assert "ConnectTimeout=5" in flat and "StrictHostKeyChecking=accept-new" in flat
    assert "ConnectTimeout" not in " ".join(transport.DEFAULT_ALLOWED["remote_uptime"])


def test_run_on_refuses_an_unlisted_command_and_an_unknown_node(estate, monkeypatch):
    _live(monkeypatch)
    with pytest.raises(KeyError, match="not allowlisted"):
        transport.run_on("rm_rf", "magos")
    with pytest.raises(KeyError):
        transport.run_on("remote_uptime", "not_a_system")


def test_an_unknown_placeholder_is_fatal(estate, monkeypatch):
    _live(monkeypatch)
    monkeypatch.setitem(transport.DEFAULT_ALLOWED, "bad", ("ssh", "{shell_command}"))
    with pytest.raises(KeyError, match="unknown placeholder"):
        transport.run_on("bad", "magos")


def test_mock_mode_never_resolves_a_credential(estate, monkeypatch):
    """Console sessions run in mock mode; they must not touch the estate at all."""
    monkeypatch.setattr(transport, "MODE", "mock")
    monkeypatch.setattr(inventory, "credentials",
                        lambda n: pytest.fail("mock mode resolved a credential"))
    assert "load average" in transport.run_on("remote_uptime", "magos")


# ---------- 6. the registry drives routing ----------

def test_optic_is_normalised_to_the_camera_domain(estate):
    """gotcha30 says "optic", the console says "camera". Two keys would mean one of
    them matches no agent, so the alias is resolved once at load."""
    assert inventory.require("camera_ptz_1")["domain"] == "camera"
    ctx = G._extract_context([{"agent": "triage", "tool": "get_system_health", "ok": True,
                               "data": {"nodes": [{"node": "camera_ptz_1",
                                                   "type": "optic_ptz",
                                                   "status": "CRITICAL"}]}}])
    assert ctx["camera_targets"] == ["camera_ptz_1"]
    assert "optic_targets" not in ctx
    assert "radar_targets" not in ctx and "acoustic_targets" not in ctx


def test_a_domain_unknown_to_the_code_still_routes(estate, registry, monkeypatch, tmp_path):
    """The registry alone must be able to make new hardware routable."""
    f = tmp_path / "power.yaml"
    f.write_text("version: 1\nsystems:\n  rack1:\n    components:\n      ups1:\n"
                 "        type: eaton_ups\n        role: power\n        domain: power\n"
                 "        address: 192.168.40.90\n")
    monkeypatch.setattr(inventory, "SYSTEMS", f)
    _clear()
    assert "power" not in G.DOMAINS
    ctx = G._extract_context([{"agent": "triage", "tool": "get_system_health", "ok": True,
                               "data": {"nodes": [{"node": "ups1", "status": "CRITICAL"}]}}])
    assert ctx["power_targets"] == ["ups1"]


def test_declared_domain_and_type_spelling_agree_for_known_hardware(estate):
    ctx = G._extract_context([{"agent": "triage", "tool": "get_system_health", "ok": True,
                               "data": {"nodes": [
                                   {"node": "magos", "type": "radar_magos", "status": "DEGRADED"},
                                   {"node": "asu", "type": "acoustic_asu", "status": "CRITICAL"}]}}])
    assert ctx["radar_targets"] == ["magos"]
    assert ctx["acoustic_targets"] == ["asu"]
