"""The three guarantees, asserted rather than assumed."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True)


def test_write_tool_is_refused():
    tmp = ROOT / "tools" / "_tmp_writer.py"
    tmp.write_text('SIDE_EFFECT = "write"\n'
                   'from registry import tool\n'
                   '@tool({"name":"wipe","description":"x",'
                   '"input_schema":{"type":"object","properties":{}}})\n'
                   'def wipe(): return {}\n')
    try:
        r = run("from registry import load_tools; load_tools()")
        assert "WriteToolRefused" in r.stderr, r.stderr
    finally:
        tmp.unlink()


def test_model_cannot_name_a_host():
    r = run("import tools.net as n; print(n.probe_endpoint('evil-box'))")
    assert "KeyError" in r.stderr or "unknown node" in r.stderr


def test_transport_refuses_unlisted_command():
    r = run("import transport; transport.run('rm -rf /')")
    assert "not allowlisted" in r.stderr


def test_no_credentials_in_inventory():
    import inventory
    blob = json.dumps(inventory.load()).lower()
    for bad in ("password", "secret", "token", "username"):
        assert bad not in blob


def test_tool_outputs_stay_small():
    from registry import call, load_tools
    load_tools()
    for name in ("get_ecal_topology", "get_system_health"):
        size = len(json.dumps(call(name, {}), default=str))
        assert size < 20_000, f"{name} returned {size} bytes"


def test_save_is_the_only_writer():
    src = (ROOT / "graph.py").read_text()
    writers = [l for l in src.splitlines() if "write_text" in l]
    assert len(writers) == 1, writers
    for f in (ROOT / "tools").glob("*.py"):
        assert "write_text" not in f.read_text(), f
