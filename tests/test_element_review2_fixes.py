"""Regression tests for the second review round (element / window / desktop / compat)."""

from __future__ import annotations

import pytest

from pypilot import Desktop
from pypilot.backends.base import AccessibleNode
from pypilot.backends.mock import MockBackend, MockNode
from pypilot.element import Element
from pypilot.errors import ActionTimeout, BackendError, ElementNotFound, ImageNotFound, NotSupported, WindowNotFound
from pypilot.types import MouseButton


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += max(0.0, float(s))


@pytest.fixture
def ft():
    return FakeTime()


def make_desktop(be, ft):
    return Desktop(be, sleep=ft.sleep, clock=ft.clock, failsafe=False)


@pytest.fixture
def uia_like_focus(monkeypatch):
    """MockNode.focus() fails on disabled widgets, like UIA SetFocus (UIA_E_ELEMENTNOTENABLED)."""
    orig = MockNode.focus
    monkeypatch.setattr(MockNode, "focus", lambda self: orig(self) if self.widget.enabled else False)


# ----------------------------------------------------------- keystrokes vs focus
def test_set_text_on_disabled_field_never_types_into_the_previous_field(ft, uia_like_focus):
    be = MockBackend()
    w = be.add_window("Vendor payment", (100, 100, 500, 400))
    iban = w.add_edit("IBAN", (10, 10, 300, 30), value="DE89370400440532013000")
    amount = w.add_edit("Amount", (10, 60, 300, 30), value="", enabled=False)
    d = make_desktop(be, ft)
    d.find('edit "IBAN"').click()
    el = d.find('edit "Amount"')
    for action in (lambda: el.set_text("1250.00"), lambda: el.type("1250.00"), lambda: el.press("backspace")):
        with pytest.raises(ElementNotFound, match="disabled"):
            action()
    assert iban.value == "DE89370400440532013000" and amount.value == ""
    assert not [e for e in be.events if e[0] in ("key_down", "type")]


def test_type_refuses_when_focus_click_does_not_focus_the_element(ft, monkeypatch):
    be = MockBackend()
    w = be.add_window("Vendor payment", (100, 100, 500, 400))
    iban = w.add_edit("IBAN", (10, 10, 300, 30), value="DE89")
    amount = w.add_edit("Amount", (10, 60, 300, 30), value="")
    d = make_desktop(be, ft)
    d.find('edit "IBAN"').click()
    monkeypatch.setattr(MockNode, "focus", lambda self: False)
    # a click that does not move keyboard focus (custom control, focus stolen back...)
    real_down = be.mouse_down

    def down(button=MouseButton.LEFT):
        keep = be._focus
        real_down(button)
        be._focus = keep

    monkeypatch.setattr(be, "mouse_down", down)
    with pytest.raises(ElementNotFound, match="keyboard focus"):
        d.find('edit "Amount"').type("1250.00")
    assert iban.value == "DE89" and amount.value == ""


def test_focus_fallback_click_refuses_covered_element(ft, monkeypatch):
    be = MockBackend()
    main = be.add_window("Pay", (100, 100, 500, 400))
    amount = main.add_edit("Amount", (10, 10, 300, 30))
    chat = be.add_window("Chat", (90, 90, 400, 100))
    msg = chat.add_edit("Message", (0, 0, 400, 100))
    d = make_desktop(be, ft)
    el = d.find('edit "Amount"')
    be._raise(chat)
    monkeypatch.setattr(MockNode, "focus", lambda self: False)  # SetFocus refused (foreground lock)
    monkeypatch.setattr(be, "activate_window", lambda h: None)  # and the window cannot be raised
    with pytest.raises(ElementNotFound, match="covered"):
        el.type("1250.00\n")
    assert amount.value == "" and msg.value == ""


@pytest.mark.parametrize("action", ["double_click", "right_click", "hover", "scroll", "desktop_click_timed"])
def test_mouse_actions_refuse_covered_element(ft, monkeypatch, action):
    be = MockBackend()
    main = be.add_window("ALV list", (100, 100, 500, 400))
    row = main.add("listitem", "Invoice 4711", (10, 10, 300, 24))
    popup = be.add_window("Reminder", (90, 90, 350, 120))
    hits = []
    popup.add_button("Delete all reminders", (0, 0, 350, 120), on_click=lambda w: hits.append(1))
    d = make_desktop(be, ft)
    el = d.find('listitem "Invoice 4711"')
    be._raise(popup)
    monkeypatch.setattr(be, "activate_window", lambda h: None)
    be.clear_events()
    run = {
        "double_click": el.double_click,
        "right_click": el.right_click,
        "hover": el.hover,
        "scroll": lambda: el.scroll(-3),
        "desktop_click_timed": lambda: d.click(el, interval=0.01),
    }[action]
    with pytest.raises(ElementNotFound, match="covered"):
        run()
    assert hits == [] and row.clicks == 0
    assert not [e for e in be.events if e[0] in ("down", "move", "scroll")]


def test_double_click_raises_covering_window_then_clicks_element(ft):
    be = MockBackend()
    main = be.add_window("ALV list", (100, 100, 500, 400))
    row = main.add("listitem", "Invoice 4711", (10, 10, 300, 24))
    popup = be.add_window("Reminder", (90, 90, 350, 120))
    hits = []
    popup.add_button("Delete all reminders", (0, 0, 350, 120), on_click=lambda w: hits.append(1))
    d = make_desktop(be, ft)
    el = d.find('listitem "Invoice 4711"')
    be._raise(popup)
    el.double_click()
    assert hits == [] and row.clicks == 2


def test_auto_click_on_hidden_element_does_not_click_what_is_drawn_there(ft):
    be = MockBackend()
    w = be.add_window("Payment run", (100, 100, 500, 400))
    fired = []
    w.add_button("Post payment run", (10, 300, 200, 40), on_click=lambda x: fired.append(1))
    grid = w.add("table", "Vendors", (10, 10, 300, 200))
    row = grid.add("dataitem", "Vendor 4711", (10, 290, 300, 60))  # virtual bounds of a scrolled-out row

    class OffscreenNode(MockNode):
        @property
        def states(self):
            return (super().states - {"visible"}) | {"offscreen"}

    d = make_desktop(be, ft)
    with pytest.raises(ElementNotFound, match="not visible"):
        Element(OffscreenNode(be, row), d).click()
    assert fired == [] and not [e for e in be.events if e[0] == "down"]


# --------------------------------------------------------------------- set_text
def test_set_text_append_with_normalising_app_appends_once(ft):
    be = MockBackend()
    w = be.add_window("SAP", (100, 100, 500, 400))

    def upper(x):
        x.value = x.value.upper()

    e = w.add_edit("Reference", (10, 10, 300, 30), value="INV", on_change=upper)
    d = make_desktop(be, ft)
    d.find('edit "Reference"').set_text("-42a", clear=False)
    assert e.value == "INV-42A"


# ----------------------------------------------------------------- check/uncheck
class TriState(AccessibleNode):
    """A UIA-like tri-state checkbox: Toggle() cycles off -> on -> mixed -> off."""

    role = "checkbox"
    name = "tri"
    bounds = None

    def __init__(self, state: str, cycle=("off", "on", "mixed")) -> None:
        self.st = state
        self.cycle = cycle
        self.toggles = 0

    def children(self):
        return []

    @property
    def states(self):
        extra = {"on": {"checked"}, "mixed": {"mixed"}}.get(self.st, set())
        return frozenset({"enabled", "checkable"} | extra)

    def toggle(self):
        self.toggles += 1
        if self.cycle:
            self.st = self.cycle[(self.cycle.index(self.st) + 1) % len(self.cycle)]
        return True


@pytest.mark.parametrize("start,method,end", [
    ("mixed", "uncheck", "off"), ("on", "uncheck", "off"), ("mixed", "check", "on"),
    ("off", "check", "on"), ("off", "uncheck", "off"), ("on", "check", "on"),
])
def test_check_uncheck_reach_the_state_on_tri_state_boxes(ft, start, method, end):
    n = TriState(start)
    getattr(Element(n, make_desktop(MockBackend(), ft)), method)()
    assert n.st == end


def test_check_raises_when_toggle_has_no_effect(ft):
    n = TriState("off", cycle=())  # disabled: toggling does nothing
    with pytest.raises(ActionTimeout, match="checked"):
        Element(n, make_desktop(MockBackend(), ft)).check()
    assert n.toggles == 1  # never toggled blindly again and again


# ------------------------------------------------------------------ scoped find
def test_scoped_find_propagates_not_supported_immediately(ft):
    class NoA11y(MockBackend):
        broken = False

        def accessibility_roots(self):
            if self.broken:
                raise NotSupported("AT-SPI accessibility is unavailable")
            return super().accessibility_roots()

    be = NoA11y()
    be.add_window("Invoice").add_button("OK", (0, 0, 50, 20))
    d = make_desktop(be, ft)
    win = d.window("Invoice")
    win.element  # resolve the window element before the backend breaks
    be.broken = True
    with pytest.raises(NotSupported):
        win.find("button", timeout=10)
    with pytest.raises(NotSupported):
        d.find("button", root=win, timeout=10)
    assert ft.now == 0


def test_element_find_propagates_not_supported_immediately(ft, monkeypatch):
    from pypilot import selectors

    be = MockBackend()
    w = be.add_window("Invoice")
    d = make_desktop(be, ft)
    el = Element(MockNode(be, w.root), d)
    cls = type(selectors.parse("button"))

    def unsupported(self, *a, **kw):
        raise NotSupported("this backend cannot evaluate the selector")

    monkeypatch.setattr(cls, "find_first", unsupported)
    with pytest.raises(NotSupported):
        el.find("button", timeout=10)
    assert ft.now == 0


def test_scoped_find_still_retries_transient_errors(ft):
    calls = []

    class Flaky(MockNode):
        def children(self):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("COM busy")
            return super().children()

    be = MockBackend()
    w = be.add_window("Invoice")
    w.add_button("OK", (0, 0, 50, 20))
    d = make_desktop(be, ft)
    assert Element(Flaky(be, w.root), d).find("button", timeout=5).name == "OK"


# ----------------------------------------------------------------------- launch
def test_launch_wait_window_ignores_windows_that_were_already_open(ft):
    be = MockBackend()
    be.add_window("Q3 close.xlsx - Excel", (0, 0, 400, 300), app="excel")
    d = make_desktop(be, ft)

    def spawn():
        if ft.now >= 0.3 and len(be.windows) == 1:
            be.add_window("Book2 - Excel", (10, 10, 400, 300), app="excel", activate=False)

    real_sleep = ft.sleep
    d.sleep = lambda s: (real_sleep(s), spawn())
    w = d.launch(["excel.exe", "/x"], wait_window="Excel", timeout=5)
    assert w.title == "Book2 - Excel"


def test_launch_wait_window_prefers_launched_process_and_falls_back_after_handover(ft):
    class Proc:
        def __init__(self, pid, rc):
            self.pid, self.rc = pid, rc

        def poll(self):
            return self.rc

    be = MockBackend()
    be.add_window("SAP Logon 770", (0, 0, 400, 300), app="saplogon", pid=1)
    d = make_desktop(be, ft)
    # single-instance app: the new process exits and the running instance shows its window
    be.launch = lambda cmd, **kw: Proc(99, 0)
    assert d.launch("saplogon.exe", wait_window="SAP Logon", timeout=2).title == "SAP Logon 770"
    assert ft.now == pytest.approx(2, abs=0.2)
    # still running and no new window: never hand back the old window
    be.launch = lambda cmd, **kw: Proc(99, None)
    with pytest.raises(WindowNotFound, match="new_window=False"):
        d.launch("saplogon.exe", wait_window="SAP Logon", timeout=1)
    assert d.launch("saplogon.exe", wait_window="SAP Logon", timeout=1, new_window=False).title == "SAP Logon 770"
    # a new window of the launched process wins over another new one
    def spawn():
        if len(be.windows) == 1:
            be.add_window("SAP Logon - other", (0, 0, 10, 10), pid=5, activate=False)
            be.add_window("SAP Logon - mine", (0, 0, 10, 10), pid=99, activate=False)

    be.launch = lambda cmd, **kw: (spawn(), Proc(99, None))[1]
    assert d.launch("saplogon.exe", wait_window="SAP Logon", timeout=1).title == "SAP Logon - mine"


# ------------------------------------------------------------------ close/mouse
def test_close_releases_held_mouse_buttons(ft):
    be = MockBackend()
    be.add_window("x")
    with pytest.raises(RuntimeError):
        with make_desktop(be, ft) as d:
            d.keyboard.down("shift")
            d.mouse_down()
            d.move(50, 50)
            raise RuntimeError("boom")
    assert be.pressed_buttons == set()
    assert d.mouse.pressed == []


def test_mouse_release_all_tracks_down_up(ft):
    be = MockBackend()
    d = make_desktop(be, ft)
    d.mouse.down("left")
    d.mouse.down("right")
    d.mouse.up("right")
    assert d.mouse.pressed == [MouseButton.LEFT]
    assert d.mouse.release_all() == [MouseButton.LEFT]
    assert be.pressed_buttons == set() and d.mouse.pressed == []


# ----------------------------------------------------------------------- locate
def test_locate_reports_capture_failure_instead_of_not_found(ft):
    from PIL import Image

    class Locked(MockBackend):
        locked = True

        def screenshot(self, region=None):
            if self.locked:
                raise BackendError("BitBlt failed: the workstation is locked")
            return super().screenshot(region)

    be = Locked()
    d = make_desktop(be, ft)
    needle = Image.new("RGB", (20, 20), (1, 2, 3))
    with pytest.raises(BackendError, match="locked"):
        d.locate(needle)
    with pytest.raises(BackendError, match="locked"):
        d.wait_image(needle, timeout=1)
    be.locked = False
    assert d.locate(needle) is None
    with pytest.raises(ImageNotFound):
        d.wait_image(needle, timeout=0)


# ----------------------------------------------------------------------- compat
def test_compat_caret_key_types_a_caret(ft):
    import pypilot.compat.pyautogui as pg

    be = MockBackend()
    w = be.add_window("E")
    e = w.add_edit("x", (10, 10, 100, 30))
    d = make_desktop(be, ft)
    d.find('edit "x"').click()
    pg.set_desktop(d)
    try:
        pg.press("^")
        pg.write(["2", "^", "3"])
        pg.keyDown("^")
        pg.keyUp("^")
    finally:
        pg.set_desktop(None)
    assert e.value == "^2^3^"
    assert "ctrl" not in {ev[1] for ev in be.events if ev[0] == "key_down"}
    assert pg._key("+") == "+" and pg._key("^") == "^" and pg._key(" ") == "space"
