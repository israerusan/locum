"""Real Windows desktop: another window takes the focus between finding the target and
typing, or in the middle of typing. Nothing may reach that window.

These send real keystrokes and clicks, so they only run with ``LOCUM_TEST_DESKTOP=1``
(the CI job ``windows-native-selftest`` sets it on a throwaway runner desktop). Never
run them on a desktop someone is using.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("LOCUM_TEST_DESKTOP") != "1",
    reason="real-desktop input test: set LOCUM_TEST_DESKTOP=1 on a dedicated Windows desktop",
)

HELPER = Path(__file__).parent / "helpers" / "textbox.ps1"


def _spawn(tmp_path, title, x, y, *extra):
    """A WinForms window with one native text box; its text is mirrored to a file."""
    out = tmp_path / f"{title}.txt"
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HELPER),
                             "-Title", title, "-Out", str(out), "-X", str(x), "-Y", str(y), *extra])
    return proc, out


def _read(path, settle=0.4):
    time.sleep(settle)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


@pytest.fixture
def two_windows(tmp_path):
    from locum import Desktop

    tag = f"{os.getpid()}-{int(time.time() * 1000) % 100000}"
    target_title, thief_title = f"locum-target-{tag}", f"locum-thief-{tag}"
    procs = []
    p1, target_out = _spawn(tmp_path, target_title, 80, 80)
    p2, thief_out = _spawn(tmp_path, thief_title, 600, 80)
    procs += [p1, p2]
    d = Desktop(failsafe=False)
    try:
        target = d.window(target_title, timeout=20)
        thief = d.window(thief_title, timeout=20)
        yield d, target, thief, target_out, thief_out, procs, tmp_path
    finally:
        try:
            d.keyboard.release_all()
        except Exception:
            pass
        for p in procs:
            p.kill()
        d.close()


def _steal_after(backend, n, thief, method="key_down"):
    orig = getattr(backend, method)
    count = {"n": 0}

    def wrapped(*a, **k):
        res = orig(*a, **k)
        count["n"] += 1
        if count["n"] == n:
            thief.activate()
            time.sleep(0.2)
        return res

    setattr(backend, method, wrapped)
    return lambda: setattr(backend, method, orig)


def test_focus_stolen_in_the_middle_of_element_typing(two_windows):
    from locum import TargetLost

    d, target, thief, target_out, thief_out, *_ = two_windows
    box = target.find("edit", timeout=10)
    box.focus()
    undo = _steal_after(d.backend, 6, thief)  # a few keys in, the other window takes the foreground
    try:
        with pytest.raises(TargetLost):
            box.type("secret-text-for-target\n", mode="keys", verify=False)
    finally:
        undo()
    assert _read(thief_out) == ""  # not one keystroke, and no Enter, reached the other window


def test_focus_stolen_between_discovery_and_cli_type(two_windows):
    from locum.cli import main

    d, target, thief, target_out, thief_out, *_ = two_windows
    target.find("edit", timeout=10)  # discovery
    thief.activate()  # ... then something else takes the foreground
    time.sleep(0.3)
    out, err = io.StringIO(), io.StringIO()
    code = main(["type", "hello target", "--window", target.title], desktop=d, stdout=out, stderr=err)
    # --window brings the target back and types only while it has the focus.
    assert code == 0, err.getvalue()
    assert _read(thief_out) == ""
    assert "hello target" in _read(target_out)


def test_focus_stolen_during_cli_type_stops_with_exit_1(two_windows):
    from locum.cli import main

    d, target, thief, target_out, thief_out, *_ = two_windows
    undo = _steal_after(d.backend, 4, thief)
    try:
        out, err = io.StringIO(), io.StringIO()
        code = main(["--json", "type", "abcdefghijkl\\n", "-e", "--mode", "keys", "--window", target.title],
                    desktop=d, stdout=out, stderr=err)
    finally:
        undo()
    assert code == 1, err.getvalue()
    assert '"TargetLost"' in out.getvalue()
    assert _read(thief_out) == ""


def test_unscoped_cli_type_is_refused(two_windows):
    from locum.cli import main

    d, target, thief, target_out, thief_out, *_ = two_windows
    thief.activate()
    code = main(["type", "should not be typed"], desktop=d, stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 2
    assert _read(thief_out) == ""


def test_click_on_a_point_covered_by_a_topmost_window_is_refused(two_windows):
    from locum.cli import main

    d, target, thief, target_out, thief_out, procs, tmp_path = two_windows
    r = target.rect
    cover = target.title.replace("locum-target-", "locum-cover-")  # must not match --window target.title
    p, cover_out = _spawn(tmp_path, cover, r.left + 20, r.top + 40, "-Topmost")
    procs.append(p)
    d.window(cover, timeout=20)
    time.sleep(0.5)
    cx, cy = r.left + 120, r.top + 120  # inside the target, under the topmost cover
    code = main(["click", f"{cx},{cy}", "--window", target.title], desktop=d,
                stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 1


def test_lone_tool_type_is_bound_to_the_window_the_policy_judged(two_windows):
    from locum.llm.safety import Policy
    from locum.llm.tools import DesktopToolkit

    d, target, thief, target_out, thief_out, *_ = two_windows
    target.activate()
    time.sleep(0.3)
    kit = DesktopToolkit(d, Policy())
    undo = _steal_after(d.backend, 1, thief, "type_text")
    try:
        r = kit.call("type_text", {"text": "first part\nsecond part"})
    finally:
        undo()
    assert r.is_error and "TargetLost" in r.text
    assert _read(thief_out) == ""
