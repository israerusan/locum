"""Review 9: regressions of 5cc796f in the Java bridge and the Windows backend."""

from __future__ import annotations

import pytest

from locum.bridges import java

import test_bridge_java as t


@pytest.fixture
def fake(monkeypatch):
    dll = t.FakeDll(t.make_tree())
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    return dll


def _rows(n, tag=""):
    return [t.Obj(f"Row {tag}{i}", "label", "enabled,visible,showing", (0, 0, 1, 1)) for i in range(n)]


# ---------------------------------------------------------------------- Java: held elements
def test_held_element_survives_a_rebuilt_top_of_its_panel(fake):
    panel = fake.roots[t.HWND].kids[0]
    panel.kids[:0] = _rows(9)
    with t.bridge() as jab:
        win = jab.from_hwnd(t.HWND)
        save = win.find('button "Save"')
        panel.kids[:9] = _rows(9, "new ")  # 9 new objects above "Save"; "Save" itself is unchanged
        win.node.refresh()
        assert win.exists('button "Save"')
        assert save.name == "Save" and not save.node._released
        assert save.node is win.find('button "Save"').node  # still matched, not a second node
        save.click(native=True)
    assert panel.kids[13].done


def test_held_element_shifted_beyond_the_match_window_stays_valid(fake):
    # Rows inserted above "Save" shift it further than the match window: the refresh cannot match
    # it and makes a new node, but the held element still points at a live object.
    panel = fake.roots[t.HWND].kids[0]
    jab = t.bridge()
    win = jab.from_hwnd(t.HWND)
    save = win.find('button "Save"')
    panel.kids[:0] = _rows(java._MATCH_WINDOW + 6)
    win.node.refresh()
    assert win.exists('button "Save"')
    assert save.name == "Save" and not save.node._released
    save.click(native=True)
    assert [o for o in panel.kids if o.name == "Save"][0].done
    jab.close()
    assert fake.refs == {} and jab.live_references == 0  # nothing leaks past close


# ---------------------------------------------------------------------- Windows: scoped thread DPI
import threading  # noqa: E402

import locum.backends.windows._win32api as w32  # noqa: E402
from locum.backends.windows import WindowsBackend  # noqa: E402
from locum.types import Rect  # noqa: E402

from test_review8_windows import LogicalThreadApi, _signed  # noqa: E402
from test_windows_backend import FakeApi, FakeWin  # noqa: E402

PMV2 = w32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
UNAWARE = 0x11


class PresetPerThreadApi(FakeApi):
    """Process awareness preset elsewhere; every thread has its own awareness context."""

    def __init__(self) -> None:
        super().__init__()
        self._ctx = {}
        self.switches = []

    def SetProcessDpiAwarenessContext(self, ctx):
        self.last_error = w32.ERROR_ACCESS_DENIED
        return 0

    def GetThreadDpiAwarenessContext(self):
        return self._ctx.get(threading.get_ident(), UNAWARE)

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        return w32.DPI_AWARENESS_PER_MONITOR_AWARE if _signed(ctx) in (-3, -4) else 0

    def SetThreadDpiAwarenessContext(self, ctx):
        t = threading.get_ident()
        old = self._ctx.get(t, UNAWARE)
        self._ctx[t] = _signed(ctx)
        self.switches.append((t, _signed(ctx)))
        return old


def _in_thread(fn):
    out = {}

    def run():
        out["tid"] = threading.get_ident()
        try:
            out["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc

    th = threading.Thread(target=run)
    th.start()
    th.join()
    return out


def test_a_calling_threads_dpi_awareness_is_restored_after_each_call():
    api = PresetPerThreadApi()
    api.add(FakeWin(0x100, "Notepad", Rect(100, 100, 400, 300)))
    be = WindowsBackend(_api=api, sleep=lambda s: None)
    assert be.dpi_mode == "thread_per_monitor_v2"

    def gui_thread():
        seen = []
        orig = api.DwmGetWindowAttribute

        def spy(*a):
            seen.append(api.GetThreadDpiAwarenessContext())
            return orig(*a)

        api.DwmGetWindowAttribute = spy
        try:
            assert [w.handle for w in be.list_windows()] == [0x100]
            be.window_info(0x100)  # nests _check / _frame_rect: one switch, not several
        finally:
            del api.DwmGetWindowAttribute
        return seen, api.GetThreadDpiAwarenessContext()

    out = _in_thread(gui_thread)
    seen, after = out["result"]
    assert seen and set(seen) == {PMV2}  # coordinates were read per-monitor aware
    assert after == UNAWARE  # the host thread keeps its own awareness for windows it creates
    mine = [c for t, c in api.switches if t == out["tid"]]
    assert mine == [PMV2, UNAWARE, PMV2, UNAWARE]


def test_thread_dpi_awareness_is_restored_when_the_call_raises():
    api = PresetPerThreadApi()
    be = WindowsBackend(_api=api, sleep=lambda s: None)

    def boom():
        try:
            be.window_info(0xDEAD)  # no such window
        except Exception:  # noqa: BLE001
            pass
        be.close()
        try:
            be.virtual_screen()  # closed: raises inside the scope
        except Exception:  # noqa: BLE001
            pass
        return api.GetThreadDpiAwarenessContext()

    assert _in_thread(boom)["result"] == UNAWARE


def test_list_windows_asks_the_thread_awareness_once():
    api = LogicalThreadApi()
    for i in range(5):
        api.add(FakeWin(0x100 + i, f"Win {i}", Rect(0, 0, 10, 10)))
    be = WindowsBackend(_api=api, dpi_aware=False, sleep=lambda s: None)
    n = []
    orig = api.GetThreadDpiAwarenessContext
    api.GetThreadDpiAwarenessContext = lambda: n.append(1) or orig()
    assert len(be.list_windows()) == 5
    assert len(n) == 1
    # A per-monitor aware thread pays no thread switch at all.
    api.ctx = PMV2
    del api.calls[:]
    switches = []
    orig_set = api.SetThreadDpiAwarenessContext
    api.SetThreadDpiAwarenessContext = lambda c: switches.append(1) or orig_set(c)
    assert len(be.list_windows()) == 5
    assert switches == []
