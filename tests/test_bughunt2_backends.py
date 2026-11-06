"""Regression tests for round-2 bug-hunt fixes in the backends (mock, macOS, X11, Windows process).

All tests use fakes / the mock backend: no real input, windows or app launches.
"""

from types import SimpleNamespace

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.types import Rect, WindowInfo


# ---- Windows: shell_launch_target with an unquoted spaced path, implied .exe, no args


def test_shell_launch_target_unquoted_spaced_path_without_extension_or_args(tmp_path):
    from locum.backends.windows import process as P

    d = tmp_path / "dir with space"
    d.mkdir()
    (d / "erp.exe").write_text("x")
    stem = str(d / "erp")
    assert P.shell_launch_target(stem) is None  # CreateProcess would start erp.exe
    assert P.shell_launch_target(f"{stem} --flag") is None
    assert P.shell_launch_target(f'"{stem}"') is None
    # a non-program file with spaces still goes to ShellExecuteEx as a whole
    (d / "Q3 report.xlsx").write_text("x")
    doc = str(d / "Q3 report.xlsx")
    assert P.shell_launch_target(doc) == (doc, "")


def test_shell_launch_target_unknown_bare_name_still_shell():
    from locum.backends.windows import process as P

    assert P.shell_launch_target("locum-no-such-app-xyz /e") == ("locum-no-such-app-xyz", "/e")
    assert P.shell_launch_target("locum-no-such-app-xyz") == ("locum-no-such-app-xyz", "")


# ---- X11: AT-SPI window resolver never matches another process's window


def _wi(h, title, pid, rect):
    return WindowInfo(handle=h, title=title, rect=Rect(*rect), app="a", pid=pid, is_active=False,
                      is_minimized=False, is_maximized=False, is_visible=True, class_name="")


def _resolver(wins):
    from locum.backends.x11 import X11Backend

    be = object.__new__(X11Backend)
    be.list_windows = lambda: wins
    return be._make_resolver()


def test_x11_resolver_title_fallback_respects_pid():
    wins = [
        _wi(0x100, "Untitled", 999, (0, 0, 800, 600)),
        _wi(0x200, "Report.odt", 1234, (900, 0, 600, 400)),
        _wi(0x300, "Notes.odt", 1234, (900, 500, 600, 400)),
    ]
    resolve = _resolver(wins)
    node = SimpleNamespace(role="window", pid=1234, name="Untitled", bounds=None)
    assert resolve(node) is None


def test_x11_resolver_iou_fallback_respects_pid():
    resolve = _resolver([_wi(0x100, "Untitled", 999, (0, 0, 800, 600))])
    node = SimpleNamespace(role="window", pid=1234, name="Dialog", bounds=Rect(0, 0, 800, 600))
    assert resolve(node) is None


def test_x11_resolver_still_matches_own_and_pidless_windows():
    wins = [
        _wi(0x100, "Untitled", 999, (0, 0, 800, 600)),
        _wi(0x200, "Report.odt", 1234, (900, 0, 600, 400)),
    ]
    resolve = _resolver(wins)
    assert resolve(SimpleNamespace(role="window", pid=1234, name="Report.odt", bounds=None)) == 0x200
    # pid unknown on the X side: still a candidate
    resolve = _resolver([_wi(0x400, "Dialog", 0, (0, 0, 800, 600))])
    assert resolve(SimpleNamespace(role="window", pid=1234, name="Dialog", bounds=None)) == 0x400
    # unknown AT-SPI pid: any window may match
    resolve = _resolver([_wi(0x100, "Untitled", 999, (0, 0, 800, 600))])
    assert resolve(SimpleNamespace(role="window", pid=0, name="Untitled", bounds=None)) == 0x100


# ---- macOS: 'normal' does not snap a user-unmaximized window back


def test_macos_restore_after_user_unmaximize_is_noop():
    from test_macos_backend import _rec, _window_setup

    be, q, appkit, ax, win = _window_setup()
    win.attrs["AXMinimized"] = False
    be.set_window_state(7, "maximized")
    win.attrs["AXPosition"] = (300.0, 200.0)
    win.attrs["AXSize"] = (500.0, 400.0)
    q.windows = [_rec(7, 100, 300, 200, 500, 400, "Doc", owner="TextEdit")]
    be.set_window_state(7, "normal")
    assert win.attrs["AXPosition"] == (300.0, 200.0)
    assert win.attrs["AXSize"] == (500.0, 400.0)
    assert 7 not in be._restore_rects


def test_macos_restore_while_still_maximized_uses_saved_rect():
    from test_macos_backend import _window_setup

    be, q, appkit, ax, win = _window_setup()
    win.attrs["AXMinimized"] = False
    before = (win.attrs["AXPosition"], win.attrs["AXSize"])
    be.set_window_state(7, "maximized")
    assert (win.attrs["AXPosition"], win.attrs["AXSize"]) != before
    be.set_window_state(7, "normal")
    assert tuple(win.attrs["AXPosition"]) == tuple(before[0])
    assert tuple(win.attrs["AXSize"]) == tuple(before[1])


# ---- Mock: space and keypad character keys type into edits


@pytest.fixture
def form():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    e = w.add_edit("Name", (20, 40, 300, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    d.click(e.screen_rect.center.x, e.screen_rect.center.y)
    return d, e


def test_mock_space_key_types_space(form):
    d, e = form
    d.press("a", "space", "b")
    assert e.value == "a b"


def test_mock_keypad_keys_type_characters(form):
    d, e = form
    d.press("num1", "num_add", "num2", "num_multiply", "num3", "num_subtract", "num4",
            "num_divide", "num5", "num_decimal", "num0")
    assert e.value == "1+2*3-4/5.0"


def test_mock_space_still_activates_button():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    hits = []
    b = w.add_button("OK", (20, 40, 80, 30), on_click=lambda _w: hits.append(1))
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    d.click(b.screen_rect.center.x, b.screen_rect.center.y)
    hits.clear()
    d.press("space")
    assert hits == [1]
