import os
import shutil
import subprocess
import time

import pytest

from locum.backends.mock import MockBackend


def real_browser_allowed() -> bool:
    """Tests that start a real (headless) Edge/Chrome run on CI and with LOCUM_TEST_BROWSER=1.

    On a developer machine they are skipped by default: they start browser processes
    (in a temporary profile) on the desktop of whoever runs the suite.
    """
    flag = os.environ.get("LOCUM_TEST_BROWSER", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(flag) or bool(os.environ.get("CI"))


def skip_unless_real_browser_allowed() -> None:
    if not real_browser_allowed():
        pytest.skip("starts a real browser: set LOCUM_TEST_BROWSER=1 to run (on by default on CI)")


@pytest.fixture
def mock_backend():
    return MockBackend(width=1280, height=800)


@pytest.fixture(scope="session")
def xvfb_display():
    """Start a private Xvfb server (or reuse $DISPLAY). Skips if neither is available."""
    if os.environ.get("LOCUM_TEST_DISPLAY"):
        yield os.environ["LOCUM_TEST_DISPLAY"]
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
