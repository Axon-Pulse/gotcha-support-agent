"""Attachments in the Diagnose box, run under node against a DOM stub."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "attach_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed; attachment check skipped")


def test_attachments_ui():
    r = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    sys.stdout.write(r.stdout)
    assert r.returncode == 0, f"attachment checks failed:\n{r.stdout}\n{r.stderr}"
