"""Regressions for the "core-correctness" review findings."""

from __future__ import annotations

import time

import numpy as np
import pytest
from PIL import Image

from pypilot import Desktop, failsafe
from pypilot import vision
from pypilot.backends.base import AccessibleNode
from pypilot.backends.mock import MockBackend
from pypilot.errors import ActionTimeout, ElementNotFound, FailSafeTriggered, PolicyViolation
from pypilot.keys import parse_hotkey
from pypilot.selectors import parse
from pypilot.types import Point
from pypilot.wait import retry_call, wait_until


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))


@pytest.fixture(autouse=True)
def _reset_abort():
    failsafe.reset_abort()
    yield
    failsafe.reset_abort()


def _two_buttons():
    be = MockBackend()
    w = be.add_window("W", (100, 100, 400, 300))
    a = w.add_button("Alpha", (10, 10, 80, 30))
    b = w.add_button("Beta", (10, 50, 80, 30))
    clk = FakeClock()
    d = Desktop(be, sleep=clk.sleep, clock=clk, default_timeout=0)
    return be, w, a, b, d


# ----------------------------------------------------------- element identity
class TestElementIdentity:
    def test_find_all_refresh_keeps_element(self):
        _, _, _, _, d = _two_buttons()
        els = d.find_all("button")
        assert [e.index for e in els] == [0, 1]
        assert els[1].refresh().name == "Beta"
        assert els[0].refresh().name == "Alpha"

    def test_wait_result_refresh_keeps_element(self):
        _, _, a, _, d = _two_buttons()
        a.enabled = False
        e = d.wait("button", state="enabled", timeout=0)
        assert e.name == "Beta"
        assert e.refresh().name == "Beta" and e.is_enabled

    def test_gone_tracks_the_specific_element(self):
        _, w, _, b, d = _two_buttons()
        beta = d.find_all("button")[1]
        alpha = d.find_all("button")[0]
        w.root.children.remove(b)
        beta.wait_until("gone", timeout=0)  # Alpha still matches "button", Beta is gone
        alpha.wait_until("exists", timeout=0)
        with pytest.raises(ActionTimeout):
            alpha.wait_until("gone", timeout=0)

    def test_index_identity_survives_earlier_sibling_removal(self):
        _, w, a, _, d = _two_buttons()
        beta = d.find_all("button")[1]
        w.root.children.remove(a)
        assert beta.refresh().name == "Beta"
        beta.wait_until("exists", timeout=0)

    def test_window_and_element_find_all_indices(self):
        _, _, _, _, d = _two_buttons()
        win = d.window("W")
        assert win.find_all("button")[1].refresh().name == "Beta"
        assert win.element.find_all("button")[1].refresh().name == "Beta"

    def test_find_result_prefers_own_node(self):
        _, w, _, _, d = _two_buttons()
        e = d.find('button "Beta"')
        assert e.index is None
        # a new earlier match does not change a find() element whose node still matches
        w.root.children.insert(0, w.root.children.pop())
        assert d.find("button").name == "Beta"
        assert e.refresh().name == "Beta"

    def test_refresh_missing_raises(self):
        _, w, _, b, d = _two_buttons()
        beta = d.find_all('button "Beta"')[0]
        w.root.children.remove(b)
        with pytest.raises(ElementNotFound):
            beta.refresh()


# ----------------------------------------------------------- fail-safe on native actions
class TestNativeFailSafe:
    def _scene(self):
        be = MockBackend()
        w = be.add_window("W", (100, 100, 400, 300))
        hits = []
        w.add_button("Delete", (10, 10, 80, 30), on_click=lambda *_: hits.append(1))
        edit = w.add_edit("Name", (10, 50, 200, 28))
        w.add_checkbox("Opt", (10, 90, 80, 20))
        be.add_window("Cover", (90, 90, 500, 400))
        d = Desktop(be, sleep=lambda s: None, default_timeout=0)
        return be, d, hits, edit

    @pytest.mark.parametrize(
        "action",
        [
            lambda d: d.click('button "Delete"'),
            lambda d: d.find("button").invoke(),
            lambda d: d.find("button").click(),
            lambda d: d.find("edit").set_text("pwned"),
            lambda d: d.find("edit").focus(),
            lambda d: d.find("checkbox").toggle(),
            lambda d: d.find("checkbox").check(),
            lambda d: d.find("button").select(),
            lambda d: d.find("button").scroll_into_view(),
            lambda d: d.window("W").close(),
            lambda d: d.window("W").activate(),
            lambda d: d.window("W").minimize(),
            lambda d: d.window("W").move(5, 5),
        ],
    )
    def test_abort_blocks_native_actions(self, action):
        be, d, hits, edit = self._scene()
        failsafe.abort("kill hotkey")
        with pytest.raises(FailSafeTriggered):
            action(d)
        assert hits == [] and edit.value == ""
        assert [w.title for w in be.windows] == ["Cover", "W"]

    def test_corner_blocks_native_invoke(self):
        be, d, hits, _ = self._scene()
        be._pos = Point(0, 0)
        with pytest.raises(FailSafeTriggered):
            d.find("button").invoke()
        assert hits == []

    def test_native_actions_work_without_abort(self):
        _, d, hits, edit = self._scene()
        d.find("button").invoke()
        d.find("edit").set_text("ok")
        assert hits == [1] and edit.value == "ok"


# ----------------------------------------------------------- relative :has
class TestRelativeHas:
    @pytest.fixture
    def roots(self):
        be = MockBackend()
        w = be.add_window("Win", (100, 100, 400, 300))
        pane = w.add("pane", "P", (0, 0, 300, 200))
        inner = pane.add("group", "G", (0, 0, 200, 100))
        inner.add_button("OK", (10, 10, 80, 30))
        pane.add_button("Direct", (10, 150, 80, 30))
        return be.accessibility_roots()

    @pytest.mark.parametrize(
        "sel, expected",
        [
            ("window:has(> pane button)", ["Win"]),
            ("window:has(> pane > button)", ["Win"]),
            ("window:has(> pane > group > button)", ["Win"]),
            ('*:has(> pane button "OK")', ["Win"]),
            ("window:has(> pane)", ["Win"]),
            ("window:has(> button)", []),
            ("window:has(> group button)", []),  # group is not a direct child of window
            ("pane:has(> group > button)", ["P"]),
            ("window:has(> pane > button\"OK\")", []),  # OK is a grandchild of pane
        ],
    )
    def test_multi_step_relative(self, roots, sel, expected):
        assert [n.name for n in parse(sel).find_all(roots)] == expected


# ----------------------------------------------------------- off-screen click
class TestOffScreenClick:
    def test_auto_click_off_screen_uses_invoke(self):
        be = MockBackend()
        w = be.add_window("W", (1700, 100, 400, 300))
        hits = []
        w.add_button("Other", (100, 10, 140, 30), on_click=lambda *_: hits.append("Other"))
        w.add_button("Target", (250, 10, 80, 30), on_click=lambda *_: hits.append("Target"))
        d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
        d.click('button "Target"')
        assert hits == ["Target"]
        assert not [e for e in be.events if e[0] == "down"]

    def test_mouse_click_off_screen_raises(self):
        be = MockBackend()
        w = be.add_window("W", (1700, 100, 400, 300))
        hits = []
        w.add_button("Other", (100, 10, 140, 30), on_click=lambda *_: hits.append("Other"))
        w.add_button("Target", (250, 10, 80, 30))
        d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
        with pytest.raises(ElementNotFound):
            d.find('button "Target"').click(native=False)
        assert hits == []

    def test_partially_visible_clicks_visible_part(self):
        be = MockBackend()
        w = be.add_window("W", (1700, 100, 400, 300))
        hits = []
        w.add_button("Half", (180, 10, 80, 30), on_click=lambda *_: hits.append("Half"))  # 1880..1960
        d = Desktop(be, sleep=lambda s: None, default_timeout=0, failsafe=False)
        d.find('button "Half"').click(native=False)
        downs = [e for e in be.events if e[0] == "down"]
        assert downs and downs[-1][2] == 1900  # center of 1880..1920
        assert hits == ["Half"]


# ----------------------------------------------------------- waits never swallow stop requests
class TestWaitFatal:
    def test_desktop_wait_until_propagates_failsafe(self):
        be = MockBackend()
        clk = FakeClock()
        d = Desktop(be, sleep=clk.sleep, clock=clk)
        be._pos = Point(0, 0)
        with pytest.raises(FailSafeTriggered):
            d.wait_until(lambda: d.click(500, 500) and False, timeout=5)
        assert not [e for e in be.events if e[0] == "down"]

    def test_wait_until_propagates_policy_violation(self):
        calls = []

        def pred():
            calls.append(1)
            raise PolicyViolation("blocked")

        with pytest.raises(PolicyViolation):
            wait_until(pred, timeout=5, sleep=lambda s: None)
        assert calls == [1]

    def test_wait_until_explicit_swallow_still_propagates(self):
        def pred():
            raise FailSafeTriggered("stop")

        with pytest.raises(FailSafeTriggered):
            wait_until(pred, timeout=1, ignore_exceptions=(Exception,), sleep=lambda s: None)

    def test_retry_call_does_not_retry_failsafe(self):
        calls = []

        def fn():
            calls.append(1)
            raise FailSafeTriggered("stop")

        with pytest.raises(FailSafeTriggered):
            retry_call(fn, attempts=5, delay=0, sleep=lambda s: None)
        assert calls == [1]

    def test_element_wait_until_callable_propagates(self):
        _, _, _, _, d = _two_buttons()
        failsafe.abort("stop")
        el = d.find_all("button")[0]
        with pytest.raises(FailSafeTriggered):
            el.wait_until(lambda e: e.click(), timeout=5)

    def test_ordinary_errors_still_swallowed(self):
        n = []

        def pred():
            n.append(1)
            if len(n) < 3:
                raise RuntimeError("transient")
            return "ok"

        assert wait_until(pred, timeout=5, interval=0, sleep=lambda s: None) == "ok"


# ----------------------------------------------------------- hotkey parsing
class TestHotkeyNames:
    @pytest.mark.parametrize(
        "combo, keys",
        [
            ("Page Down", ["pagedown"]),
            ("page up", ["pageup"]),
            ("page-up", ["pageup"]),
            ("caps-lock", ["capslock"]),
            ("num-lock", ["numlock"]),
            ("print-screen", ["printscreen"]),
            ("ctrl+ ", ["ctrl", "space"]),
            ("ctrl++", ["ctrl", "+"]),
            ("ctrl + +", ["ctrl", "+"]),
            ("ctrl+Page Down", ["ctrl", "pagedown"]),
            ("Ctrl-Alt-Del", ["ctrl", "alt", "delete"]),
            ("ctrl alt del", ["ctrl", "alt", "delete"]),
            ("cmd-r", ["cmd", "r"]),
            ("shift-tab", ["shift", "tab"]),
            ("-", ["-"]),
            (" ", ["space"]),
        ],
    )
    def test_parse(self, combo, keys):
        assert parse_hotkey(combo) == keys

    def test_unknown_still_raises(self):
        with pytest.raises(ValueError):
            parse_hotkey("foo bar")
        with pytest.raises(ValueError):
            parse_hotkey("page-sideways")

    def test_keyboard_press_multiword(self):
        from pypilot.keyboard import Keyboard

        be = MockBackend()
        Keyboard(be, sleep=lambda s: None).press("Page Down")
        assert [e for e in be.events if e[0] == "key_down"] == [("key_down", "pagedown")]


# ----------------------------------------------------------- vision NMS exactness
class TestLocateAllNms:
    def test_weaker_peak_in_suppressed_block_survives(self):
        score = np.zeros((1, 20))
        score[0, 0], score[0, 1], score[0, 6] = 0.95, 0.96, 1.0
        bh, bw = vision._pool_block(10, 10, 0.3)
        assert (bh, bw) != (1, 1)
        ys, xs, vals = vision._candidates(score, 0.9, bh, bw)
        kept = vision._nms(xs, ys, np.full(xs.size, 10), np.full(xs.size, 10), vals, 0.3, 100)
        assert sorted(int(xs[i]) for i in kept) == [0, 6]

    def test_pooling_kicks_in_above_cap(self, monkeypatch):
        monkeypatch.setattr(vision, "_MAX_CANDIDATES", 10)
        score = np.ones((10, 10))
        ys, xs, vals = vision._candidates(score, 0.5, 2, 2)
        assert vals.size == 10  # 25 pooled peaks capped to 10
        ys, xs, vals = vision._candidates(score, 0.5, 1, 1)
        assert vals.size == 10


# ----------------------------------------------------------- invalid vision input fails fast
class TestVisionInputErrors:
    def _desktop(self):
        clk = FakeClock()
        return Desktop(MockBackend(), sleep=clk.sleep, clock=clk, default_timeout=5), clk

    def test_transparent_needle(self):
        d, clk = self._desktop()
        with pytest.raises(ValueError):
            d.wait_image(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
        assert clk.t == 0.0

    def test_bad_scales(self):
        d, clk = self._desktop()
        with pytest.raises(ValueError):
            d.wait_image(Image.new("RGB", (4, 4)), scales=(0,))
        with pytest.raises(ValueError):
            d.click_image(Image.new("RGB", (4, 4)), scales=(-1,))
        assert clk.t == 0.0

    def test_not_found_still_times_out(self):
        from pypilot.errors import ImageNotFound

        d, clk = self._desktop()
        with pytest.raises(ImageNotFound):
            d.wait_image(Image.new("RGB", (8, 8), (1, 250, 3)), timeout=1)
        assert clk.t >= 1.0


# ----------------------------------------------------------- injected clock in scoped finds
class TestScopedFindClock:
    def test_scoped_find_uses_desktop_clock(self):
        be = MockBackend()
        be.add_window("W")
        clk = FakeClock()
        d = Desktop(be, sleep=clk.sleep, clock=clk)
        start = time.monotonic()
        with pytest.raises(ElementNotFound):
            d.window("W").find("button", timeout=30)
        with pytest.raises(ElementNotFound):
            d.window("W").element.find("button", timeout=30)
        with pytest.raises(ActionTimeout):
            d.window("W").element.wait_until(lambda e: False, timeout=30)
        assert time.monotonic() - start < 2.0
        assert clk.t >= 90


# ----------------------------------------------------------- app attribute fallback (contract)
class _PidOnlyNode(AccessibleNode):
    def __init__(self, role, name, attrs, kids=()):
        self._r, self._n, self._a, self._k, self._p = role, name, attrs, list(kids), None
        for k in self._k:
            k._p = self

    role = property(lambda s: s._r)
    name = property(lambda s: s._n)
    bounds = property(lambda s: None)
    pid = property(lambda s: 1234)

    def children(self):
        return self._k

    @property
    def parent(self):
        return self._p

    def attributes(self):
        return dict(self._a)


def test_app_selector_matches_backend_app_attribute():
    ok = _PidOnlyNode("button", "OK", {"app": "EXCEL.EXE"})
    win = _PidOnlyNode("window", "Book1 - Excel", {"app": "EXCEL.EXE"}, [ok])
    assert parse("<wnd app='excel.exe' title='*Book1*' /><ctrl name='OK' role='push button' />").find_first([win]) is ok
    assert parse('window[app="excel"] button').find_first([win]) is ok
