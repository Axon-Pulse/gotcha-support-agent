"""Run every console render path against a DOM stub.

The console UI is inline JavaScript with no build step and no browser in CI, so a typo
in a template literal would only show up as a blank tab. This executes the real script
from index.html under node with a minimal document stub and calls each render function,
which catches runtime errors and the pure-logic bugs (reordering, prerequisite
validation, symptom lists) without needing a browser.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "console_render_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed; console render check skipped")


def test_every_console_render_path_runs():
    r = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    assert r.returncode == 0, f"render paths failed:\n{r.stdout}\n{r.stderr}"
    assert "all " in r.stdout and "render paths ok" in r.stdout
