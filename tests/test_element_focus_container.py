"""A focused *container* (window, pane, web document) is not proof that a field inside it has focus.

UI Automation's GetFocusedElement and macOS's AXFocusedUIElement report the
window or web document when nothing inside it is focused. Treating that
ancestor as proof before focusing the target would send keystrokes to the
container (single-key page shortcuts) instead of the field. The ancestor rule
(a grid that owns its cells' keyboard input) only applies after a click has
physically targeted the element.
"""

from __future__ import annotations

import pytest

from pypilot import Desktop
from pypilot.backends.mock import MockBackend, MockNode
from pypilot.errors import ElementNotFound
from pypilot.types import MouseButton


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += max(0.0, float(s))


class ContainerFocusBackend(MockBackend):
    """Reports the active window as focused when no widget inside it has focus (like UIA)."""

    calls = 0

    def focused_element(self):
        self.calls += 1
        if self._focus is not None:
            return self._node_with_parents(self._focus)
        return self._node_with_parents(self.windows[0].root) if self.windows else None


@pytest.fixture
def ft():
    return FakeTime()


def make(ft, cls=ContainerFocusBackend):
    be = cls()
    w = be.add_window("Vendor payment", (100, 100, 500, 400))
    iban = w.add_edit("IBAN", (10, 10, 300, 30), value="")
    d = Desktop(be, sleep=ft.sleep, clock=ft.clock, failsafe=False)
    be._focus = None  # the window is active, nothing inside it is focused
    return d, be, w, iban


def keystrokes(be):
    return [e for e in be.events if e[0] in ("key_down", "type")]


def mouse_downs(be):
    return [e for e in be.events if e[0] == "down"]


def test_focused_window_is_not_proof_before_focusing(ft):
    d, be, w, iban = make(ft)
    el = d.find('edit "IBAN"')
    assert el._has_focus(allow_ancestor=False) is False
    d.find('edit "IBAN"').press("a")
    assert be._focus is iban  # focus was actually moved to the field...
    assert iban.value == "a"  # ...so the key landed there, not in the window


def test_type_into_field_inside_focused_window(ft):
    d, be, w, iban = make(ft)
    d.find('edit "IBAN"').type("DE89", verify=False)
    assert be._focus is iban and iban.value == "DE89"


def test_native_focus_noop_with_focused_container_falls_back_to_click(ft, monkeypatch):
    """Native focus "succeeds" but focus stays on the window: the click must do the job."""
    d, be, w, iban = make(ft)
    monkeypatch.setattr(MockNode, "focus", lambda self: True)
    be.clear_events()
    d.find('edit "IBAN"').press("a")
    assert mouse_downs(be)  # not accepted on the ancestor rule after native focus alone
    assert be._focus is iban and iban.value == "a"


def test_container_still_focused_after_click_counts_as_owner(ft, monkeypatch):
    """After a click on the element, a focused ancestor (grid owning its cells) is accepted, as before."""
    d, be, w, iban = make(ft)
    monkeypatch.setattr(MockNode, "focus", lambda self: False)
    real_down = be.mouse_down

    def down(button=MouseButton.LEFT):
        real_down(button)
        be._focus = None  # the app keeps reporting the container as focused

    monkeypatch.setattr(be, "mouse_down", down)
    be.clear_events()
    d.find('edit "IBAN"').press("a")
    assert mouse_downs(be) and keystrokes(be)


def test_container_focus_without_any_move_is_refused(ft, monkeypatch):
    """Native focus is a no-op and the click cannot be verified on the element either: refuse."""
    d, be, w, iban = make(ft)
    other = w.add_edit("Amount", (10, 60, 300, 30), value="")
    be._focus = other
    monkeypatch.setattr(MockNode, "focus", lambda self: True)
    real_down = be.mouse_down

    def down(button=MouseButton.LEFT):
        real_down(button)
        be._focus = other

    monkeypatch.setattr(be, "mouse_down", down)
    be.clear_events()
    with pytest.raises(ElementNotFound, match="keyboard focus"):
        d.find('edit "IBAN"').press("a")
    assert keystrokes(be) == [] and other.value == ""


def test_focused_state_skips_focused_element_lookup(ft):
    """The pre-check reads only the element's own state; no tree walk when it is already focused."""
    d, be, w, iban = make(ft)
    be._set_focus(iban)
    el = d.find('edit "IBAN"')
    be.calls = 0
    el.press("a")
    assert be.calls == 0 and iban.value == "a"


def test_focused_state_after_native_focus_needs_no_second_lookup(ft):
    """After native focus, the element's own "focused" state is proof: no second tree walk."""
    d, be, w, iban = make(ft)
    el = d.find('edit "IBAN"')
    be.calls = 0
    el.press("a")
    assert be.calls == 1 and iban.value == "a"  # only the pre-check consulted focused_element()


def test_focus_on_descendant_is_not_refocused(ft, monkeypatch):
    """A container whose child already has focus keeps it (no focus() call that would steal it)."""
    d, be, w, iban = make(ft)
    be._set_focus(iban)
    from pypilot.element import Element

    win = Element(MockNode(be, w.root), d)
    called = []
    monkeypatch.setattr(MockNode, "focus", lambda self: called.append(self) or True)
    win.press("a")
    assert called == [] and be._focus is iban and iban.value == "a"
