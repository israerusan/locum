"""Review 12: Windows backend / Window regressions.

Nothing here touches the real desktop: every Win32 / UIA call goes to a fake.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Dict

import pytest

import locum.backends.windows._win32api as w32
from locum.backends.windows import WindowsBackend
from locum.backends.windows.uia import UIANode
from locum.desktop import Desktop
from locum.types import Rect
from locum.window import Window

from test_review8_windows import PresetDpiApi, _minimized_window, _signed
from test_windows_backend import FakeApi, FakeWin

PMV2 = w32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


# ============================================================ 1. UIA bounds and the DPI scope
class PerThreadDpiApi(PresetDpiApi):
    """Preset process awareness; remembers each thread's awareness context."""

    def __init__(self) -> None:
        super().__init__()
        self.ctx: Dict[int, int] = {}

    def SetThreadDpiAwarenessContext(self, ctx):
        tid = threading.get_ident()
        old = self.ctx.get(tid, 0x11)  # unaware
        self.ctx[tid] = _signed(ctx)
        return old

    def is_pmv2(self) -> bool:
        return self.ctx.get(threading.get_ident(), 0x11) == PMV2


class _Element:
    """UIA converts BoundingRectangle to the DPI awareness of the reading thread."""

    def __init__(self, api: PerThreadDpiApi) -> None:
        self.api = api

    @property
    def CurrentBoundingRectangle(self):
        if self.api.is_pmv2():
            return SimpleNamespace(left=250, top=125, right=1250, bottom=750)
        return SimpleNamespace(left=200, top=100, right=1000, bottom=600)  # logical at 125 %


def test_uia_bounds_read_on_another_thread_are_physical_like_the_backend():
    api = PerThreadDpiApi()
    be = WindowsBackend(_api=api, sleep=lambda s: None)
    assert be.dpi_mode == "thread_per_monitor_v2"
    be._uia_ctx = SimpleNamespace(app_name=None)  # stands in for _UIAContext
    node = UIANode(_Element(api), be.uia)
    got = []

    def worker():  # e.g. an MCP / thread-pool worker: its own (unaware) awareness
        got.append(node.bounds)
        got.append(api.ctx.get(threading.get_ident(), 0x11))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert got[0] == Rect.from_ltrb(250, 125, 1250, 750)  # same space as clicks / window rects
    assert got[1] == 0x11  # the worker's awareness is put back


# ============================================================ 2. move/resize argument checks
def test_invalid_resize_of_a_minimized_window_does_not_restore_it():
    api, w = _minimized_window()
    with pytest.raises(ValueError):
        w.resize(0, 480)
    with pytest.raises((TypeError, ValueError)):
        w.move("left", 10)
    assert api.wins[0x100].iconic  # still minimized: nothing was changed
    assert not [c for c in api.calls if c[0] == "ShowWindow"]


# ============================================================ 3. reused window handle
def test_window_whose_handle_was_reused_by_another_process_is_gone():
    api = FakeApi()
    api.add(FakeWin(0x100, "Untitled - Notepad", Rect(100, 100, 800, 600), pid=100))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    d = Desktop(be, sleep=lambda s: None, default_timeout=0.05, poll_interval=0.01)
    w = Window(be.window_info(0x100), d)
    assert w.exists
    # Notepad closes; Windows hands the same HWND to a new window of another process.
    api.wins[0x100] = FakeWin(0x100, "Untitled - Paint", Rect(0, 0, 300, 300), pid=4242)
    assert not w.exists
    assert w.wait_closed(timeout=0.05)
