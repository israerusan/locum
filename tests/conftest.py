import os
import shutil
import subprocess
import time

import pytest

from pyoilot.backends.mock import MockBackend


@pytest.fixture
def mock_backend():
    return MockBackend(width=1280, height=800)


@pytest.fixture(scope="session")
def xvfb_display():
    """Start a private Xvfb server (or reuse $DISPLAY). Skips if neither is available."""
    if os.environ.get("PYOILOT_TEST_DISPLAY"):
        yield os.environ["PYOILOT_TEST_DISPLAY"]
        return
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        if os.environ.get("DISPLAY"):
            yield os.environ["DISPLAY"]
            return
        pytest.skip("No X display / Xvfb available")
    display = ":97"
    proc = subprocess.Popen([xvfb, display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    if proc.poll() is not None:
        pytest.skip("Xvfb failed to start")
    try:
        yield display
    finally:
        proc.terminate()
        proc.wait(timeout=5)
