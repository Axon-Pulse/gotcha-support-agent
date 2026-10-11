"""The Trace tab's JavaScript, run under node against a DOM stub."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "trace_page_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed; trace page check skipped")


def test_trace_page():
    r = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    assert r.returncode == 0, f"trace page checks failed:\n{r.stdout}\n{r.stderr}"
