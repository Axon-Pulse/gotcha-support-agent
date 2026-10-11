"""How a key reaches the SDK, and what the token counters add up to.

Two things are being protected here. The first is the precedence rule: a file is a
fallback, never an override, so nothing on disk can quietly outrank the credential an
operator exported — not .env, and not a key somebody typed into the console months ago.
The second is that the console remains incapable of running live, which is a property of
the ORDER in which console/server.py sets things up and would otherwise be silently lost
to an innocent-looking import reshuffle.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import env_file  # noqa: E402
import inventory  # noqa: E402
import llm  # noqa: E402
import secrets_store  # noqa: E402

_TOUCHED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
            "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_FEDERATION_RULE_ID",
            "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID",
            "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
            "GOTCHA30_REPO", "ENV_FILE")


@pytest.fixture(autouse=True)
def restore_environment():
    """Put os.environ back afterwards.

    Both things under test here write to os.environ on purpose — that is the mechanism —
    and monkeypatch cannot undo a write it did not make. Without this, one test's key
    becomes the next test's "already exported" and the precedence assertions pass or fail
    for the wrong reason.
    """
    before = {v: os.environ.get(v) for v in _TOUCHED}
    yield
    for v, was in before.items():
        if was is None:
            os.environ.pop(v, None)
        else:
            os.environ[v] = was


# ---------- .env loading ----------

def test_parses_values_comments_and_quotes():
    got = env_file.parse('\n'.join([
        "# a comment",
        "",
        "ANTHROPIC_API_KEY=sk-ant-plain",
        'QUOTED="  spaces kept  "',
        "SINGLE='single'",
        "lowercase=ignored",          # not a valid env var name by the shared rule
        "no_equals_sign",
        "HASH=value#not-a-comment",   # inline # belongs to the value
    ]))
    assert got == {"ANTHROPIC_API_KEY": "sk-ant-plain", "QUOTED": "  spaces kept  ",
                   "SINGLE": "single", "HASH": "value#not-a-comment"}


def test_missing_file_is_not_an_error(tmp_path):
    assert env_file.load(tmp_path / "nope.env") == {}


def test_an_exported_variable_is_never_overridden(tmp_path, monkeypatch):
    """The whole point of the precedence rule: the shell wins over the file."""
    p = tmp_path / ".env"
    p.write_text("ANTHROPIC_API_KEY=from-file\nGOTCHA30_REPO=/from/file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    monkeypatch.delenv("GOTCHA30_REPO", raising=False)

    applied = env_file.load(p)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell", "a stale file outranked the shell"
    assert os.environ["GOTCHA30_REPO"] == "/from/file", "the unset one should come from file"
    assert applied == {"GOTCHA30_REPO": "/from/file"}, "reported setting what it did not set"


def test_a_malformed_line_does_not_stop_startup(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("!!! not a line at all\nANTHROPIC_API_KEY=still-loaded\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert env_file.load(p) == {"ANTHROPIC_API_KEY": "still-loaded"}


def test_a_dotenv_cannot_talk_the_console_into_running_live(tmp_path):
    """Both halves of console/server.py's startup, asserted by actually importing it.

    Two independent things keep AGENT_MODE=live in a .env from reaching the browser
    console: env_file.load() never overrides, and the forced AGENT_MODE=mock runs before
    `import transport`, which reads MODE once at import and never again. The second is
    the fragile one — move that assignment below the import and this test goes red,
    which is the whole reason it exists.
    """
    envf = tmp_path / "live.env"
    envf.write_text("AGENT_MODE=live\nANTHROPIC_API_KEY=sk-from-env-file\n")
    # A CONTROLLED environment, not this process's. Inheriting os.environ made the test
    # depend on the developer's machine: any ANTHROPIC_API_KEY already exported — even
    # an empty one merged in from a real .env at import — outranks the file under test,
    # because not overriding is precisely the behaviour being asserted elsewhere.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "AGENT_"))}
    r = subprocess.run(
        [sys.executable, "-c",
         "import console.server, transport, os;"
         "print(transport.MODE, repr(os.environ.get('ANTHROPIC_API_KEY')))"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**env, "ENV_FILE": str(envf), "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    mode, key = r.stdout.split()
    key = key.strip("'\"")
    assert mode == "mock", "a .env talked the console into live mode"
    assert key == "sk-from-env-file", ".env was not loaded at all"


# ---------- the console's key field ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A console whose secret store and registry are this test's, not the operator's."""
    import console.server as srv
    monkeypatch.setattr(secrets_store, "PATH", tmp_path / "secrets.local.env")
    monkeypatch.setattr(inventory, "SYSTEMS", tmp_path / "systems_inventory.yaml")
    monkeypatch.setattr(srv, "TRACES", tmp_path / "traces")
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
              "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
              "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_IDENTITY_TOKEN",
              "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "noprofiles"))
    monkeypatch.setattr(env_file, "PATH", tmp_path / "absent.env")
    return TestClient(srv.app)


KEY = "sk-ant-api03-0123456789abcdefghijZQ4A"


def test_a_key_set_in_the_console_round_trips(client, tmp_path):
    import console.server as srv

    assert client.get("/api/credentials").json()["ready"] is False

    got = client.put("/api/credentials", json={"key": KEY}).json()
    assert got["ready"] is True
    assert got["source"] == "secrets.local.env"
    assert got["stored"] is True

    stored = tmp_path / "secrets.local.env"
    assert KEY in stored.read_text(), "the key never reached the secret store"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600, "a billing credential left readable"
    assert os.environ["ANTHROPIC_API_KEY"] == KEY, "stored but never promoted; the SDK "\
                                                   "reads os.environ and nothing else"

    # The masked form identifies the key without being the key.
    assert got["masked"] and KEY not in got["masked"] and got["masked"].endswith("ZQ4A")
    assert KEY not in json.dumps(got), "the response handed the raw key back to the browser"

    assert client.delete("/api/credentials").json()["ready"] is False
    assert KEY not in stored.read_text()
    assert "ANTHROPIC_API_KEY" not in os.environ, "cleared on disk but left in the process"
    assert srv  # keeps the import meaningful for the reader


def test_the_key_never_lands_in_the_registry(client, tmp_path):
    client.put("/api/credentials", json={"key": KEY})
    registry = tmp_path / "systems_inventory.yaml"
    if registry.exists():
        assert KEY not in registry.read_text()


def test_storing_a_key_rebuilds_the_sdk_client(client):
    """Without this the process keeps the client it built before the key existed."""
    llm._client = object()
    client.put("/api/credentials", json={"key": KEY})
    assert llm._client is None, "a key was stored but the memoized client survived"

    llm._client = object()
    client.delete("/api/credentials")
    assert llm._client is None, "the key was cleared but the memoized client survived"


def test_clearing_a_console_key_falls_back_to_the_env_file(tmp_path, monkeypatch):
    """Clearing must not take a working .env key down with it.

    Found by running the console: after Clear, a machine with a perfectly good .env
    reported "no credentials" and only a restart brought it back.
    """
    import console.server as srv
    monkeypatch.setattr(secrets_store, "PATH", tmp_path / "secrets.local.env")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "noprofiles"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    envf = tmp_path / ".env"
    envf.write_text("ANTHROPIC_API_KEY=sk-ant-from-the-env-file\n")
    monkeypatch.setattr(env_file, "PATH", envf)
    env_file.load()
    c = TestClient(srv.app)

    c.put("/api/credentials", json={"key": KEY})
    assert os.environ["ANTHROPIC_API_KEY"] == KEY, "the typed key should be the one in use"

    got = c.delete("/api/credentials").json()

    assert got["ready"] is True, "clearing the typed key killed a working .env credential"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-the-env-file"
    assert got["stored"] is False


def test_clearing_never_unsets_a_key_the_operator_exported(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(secrets_store, "PATH", tmp_path / "secrets.local.env")
    monkeypatch.setattr(env_file, "PATH", tmp_path / "absent.env")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "noprofiles"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported-by-the-operator")
    secrets_store.put("ANTHROPIC_API_KEY", KEY)          # stored, but outranked
    monkeypatch.setattr(srv, "_promoted", False)

    TestClient(srv.app).delete("/api/credentials")

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-exported-by-the-operator", \
        "the console removed a credential that was not its own"


def test_a_stored_key_rescues_the_empty_string_trap(client, monkeypatch):
    """An empty ANTHROPIC_API_KEY is never deliberate, so the stored key may replace it.

    Set to "", the variable still wins its precedence slot and authenticates with
    nothing — the trap the console has always warned about. A stored key is treated as
    "nothing else resolves" here and overwrites it, which is the one case where the
    console outranks an environment variable. Pinned because it is a judgement call.
    """
    import console.server as srv
    secrets_store.put("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    got = srv._credentials()

    assert got["ready"] is True and got["source"] == "secrets.local.env"
    assert os.environ["ANTHROPIC_API_KEY"] == KEY


def test_a_whitespace_damaged_key_is_refused(client):
    r = client.put("/api/credentials", json={"key": "sk-ant-api03-half key"})
    assert r.status_code == 400 and "whitespace" in r.json()["detail"]
    r = client.put("/api/credentials", json={"key": "   "})
    assert r.status_code == 400


def test_a_stored_key_never_shadows_an_exported_one(client, monkeypatch, tmp_path):
    """The stored key is a last resort, not an override."""
    import console.server as srv
    secrets_store.put("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported-by-the-operator")

    got = srv._credentials()

    assert got["source"] == "ANTHROPIC_API_KEY", "the console's stored key won, wrongly"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-exported-by-the-operator"
    assert got["stored"] is True, "it should still report that a key is on disk"


def test_a_stored_key_never_shadows_an_oauth_profile(client, tmp_path, monkeypatch):
    import console.server as srv
    creds = tmp_path / "profiles" / "credentials"
    creds.mkdir(parents=True)
    (creds / "default.json").write_text("{}")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "profiles"))
    secrets_store.put("ANTHROPIC_API_KEY", KEY)

    got = srv._credentials()

    assert got["source"] == "OAuth profile (default)"
    assert "ANTHROPIC_API_KEY" not in os.environ, "promoted over a working profile"


# ---------- token totals ----------

class _Resp:
    """The shape llm._account() reads off a Message."""
    def __init__(self, i, o, c=0):
        self.usage = type("U", (), {"input_tokens": i, "output_tokens": o,
                                    "cache_read_input_tokens": c})()


def test_totals_accumulate_and_reset():
    llm.reset_usage()
    assert llm.totals() == {"input": 0, "output": 0, "cache_read": 0}
    llm._account(_Resp(100, 10, 900))
    one = llm._account(_Resp(50, 5, 100))
    assert one == {"input": 50, "output": 5, "cache_read": 100}, "single-call figure wrong"
    assert llm.totals() == {"input": 150, "output": 15, "cache_read": 1000}
    llm.reset_usage()
    assert llm.totals() == {"input": 0, "output": 0, "cache_read": 0}


def test_totals_are_per_thread():
    """Two console sessions run concurrently; neither may be billed for the other."""
    import threading
    llm.reset_usage()
    llm._account(_Resp(100, 10))
    other = {}

    def worker():
        llm.reset_usage()
        llm._account(_Resp(7, 7))
        other.update(llm.totals())

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other["input"] == 7
    assert llm.totals()["input"] == 100, "another thread's session changed this one's total"


def test_routing_and_synthesis_calls_are_counted(monkeypatch):
    """ask_json returns only the tool input, so its usage has to reach the total here."""
    class _Block:
        type = "tool_use"
        input = {"next": "network"}

    resp = _Resp(400, 40, 4000)
    resp.content = [_Block()]
    monkeypatch.setattr(llm, "client", lambda: type(
        "C", (), {"messages": type("M", (), {"create": staticmethod(lambda **kw: resp)})()})())

    llm.reset_usage()
    assert llm.ask_json(prompt="p", schema={}) == {"next": "network"}
    assert llm.totals() == {"input": 400, "output": 40, "cache_read": 4000}, \
        "the router's tokens were spent but never counted"


def test_lifetime_usage_sums_traces_in_both_formats(client, tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir()
    # Written since usage was recorded: one authoritative session total.
    (traces / "aaa.jsonl").write_text(json.dumps({
        "usage": {"input": 1000, "output": 100, "cache_read": 5000}}) + "\n")
    # Written before it: only per-agent numbers in the transcript.
    (traces / "bbb.jsonl").write_text(json.dumps({
        "transcript": [{"agent": "triage", "usage": {"input": 20, "output": 2, "cache_read": 7}},
                       {"agent": "network", "usage": {"input": 5, "output": 1}},
                       {"agent": "no_usage_key"}]}) + "\n")

    # Older still: a trace from before the transcript existed, carrying no token data.
    (traces / "ccc.jsonl").write_text(json.dumps({"report": {}, "commands": []}) + "\n")

    got = client.get("/api/usage").json()

    assert got["lifetime"] == {"input": 1025, "output": 103, "cache_read": 5007}, \
        "an older trace stopped counting when the format changed"
    assert got["sessions"] == 2, "a trace with no token data was counted, which reads " \
                                 "in the UI as a broken counter rather than as history"


def test_lifetime_usage_survives_a_corrupt_trace(client, tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "bad.jsonl").write_text("{not json\n" + json.dumps(
        {"usage": {"input": 3, "output": 1, "cache_read": 0}}) + "\n")
    assert client.get("/api/usage").json()["lifetime"]["input"] == 3
