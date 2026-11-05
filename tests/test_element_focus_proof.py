"""Element.type / press / set_text never send keystrokes without affirmative proof of focus.

"The backend cannot tell" (no states, no focused element, or a read that
raised) used to be accepted as success, so keystrokes could land in whichever
control already had focus. It is now refused unless the caller opts out with
``require_focus=False`` (per call) or ``desktop.require_focus = False``.
"""

from __future__ import annotations

import pytest

from pypilot import Desktop
from pypilot.backends.mock import MockBackend, MockNode
from pypilot.element import Element
from pypilot.errors import ElementNotFound
from pypilot.types import MouseButton


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += max(0.0, float(s))


class BlindBackend(MockBackend):
    """A backend that cannot report the focused element."""

    def focused_element(self):
        return None


class GarblingBlindBackend(BlindBackend):
    """Cannot report focus and garbles typed text (every char becomes the last one)."""

    def type_text(self, text: str) -> None:
        if len(text) > 1:
            text = text[-1] * len(text)
        super().type_text(text)


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def no_states(monkeypatch):
    """Nodes report no states at all (an accessibility API without a focus flag)."""
    monkeypatch.setattr(MockNode, "states", property(lambda self: frozenset()))


@pytest.fixture
def no_focus_move(monkeypatch):
    """Native focus "succeeds" but does nothing; clicks do not move focus either."""
    monkeypatch.setattr(MockNode, "focus", lambda self: True)

    def patch(be):
        real_down = be.mouse_down

        def down(button=MouseButton.LEFT):
            keep = be._focus
            real_down(button)
            be._focus = keep

        monkeypatch.setattr(be, "mouse_down", down)

    return patch


def make(be_cls, ft):
    be = be_cls()
    w = be.add_window("Vendor payment", (100, 100, 500, 400))
    iban = w.add_edit("IBAN", (10, 10, 300, 30), value="DE89")
    amount = w.add_edit("Amount", (10, 60, 300, 30), value="")
    d = Desktop(be, sleep=ft.sleep, clock=ft.clock, failsafe=False)
    return d, be, iban, amount


def keystrokes(be):
    return [e for e in be.events if e[0] in ("key_down", "type")]


def mouse_downs(be):
    return [e for e in be.events if e[0] == "down"]


# ------------------------------------------------------------- refusal: unknown
def test_type_refuses_when_focus_cannot_be_told(ft, no_states):
    d, be, iban, amount = make(BlindBackend, ft)
    be._set_focus(iban)  # keystrokes would really land in IBAN
    be.clear_events()
    el = d.find('edit "Amount"')
    for action in (lambda: el.type("1250.00"), lambda: el.press("backspace"),
                   lambda: el.set_text("1250.00", native=False)):
        with pytest.raises(ElementNotFound, match="could not confirm.*require_focus=False"):
            action()
    assert iban.value == "DE89" and amount.value == ""
    assert keystrokes(be) == []
    assert mouse_downs(be) == []  # a click cannot produce proof either: not attempted


def test_unknown_after_focus_click_is_refused(ft, no_states, monkeypatch):
    d, be, iban, amount = make(BlindBackend, ft)
    be._set_focus(iban)
    monkeypatch.setattr(MockNode, "focus", lambda self: False)  # native focus refused -> click path
    be.clear_events()
    with pytest.raises(ElementNotFound, match="could not confirm"):
        d.find('edit "Amount"').type("1250.00")
    assert mouse_downs(be)  # the click was tried...
    assert keystrokes(be) == []  # ...but nothing was typed without proof
    assert iban.value == "DE89"


def test_state_read_errors_are_not_evidence(ft, monkeypatch):
    d, be, iban, amount = make(MockBackend, ft)
    be._set_focus(iban)
    el = d.find('edit "Amount"')

    def boom(self):
        raise RuntimeError("COM call failed")

    monkeypatch.setattr(MockNode, "states", property(boom))
    monkeypatch.setattr(be, "focused_element", lambda: (_ for _ in ()).throw(RuntimeError("COM")))
    assert el._read_states() is None
    assert el._has_focus() is None  # failed reads: neither "focused" nor "not focused"
    be.clear_events()
    with pytest.raises(ElementNotFound, match="could not confirm"):
        el.type("1250.00")
    assert keystrokes(be) == [] and iban.value == "DE89"


def test_focused_element_error_with_unfocused_states_is_unknown_not_false(ft, monkeypatch):
    d, be, iban, amount = make(MockBackend, ft)
    el = d.find('edit "Amount"')
    monkeypatch.setattr(be, "focused_element", lambda: (_ for _ in ()).throw(RuntimeError("COM")))
    assert "focused" not in el.states and el.states  # states readable, no focus flag
    assert el._has_focus() is None


def test_states_error_does_not_hide_proof_from_focused_element(ft, monkeypatch):
    d, be, iban, amount = make(MockBackend, ft)
    be._set_focus(amount)
    el = d.find('edit "Amount"')

    def boom(self):
        raise RuntimeError("COM call failed")

    monkeypatch.setattr(MockNode, "states", property(boom))
    assert el._has_focus() is True


# ------------------------------------------------------ refusal: focus elsewhere
def test_type_refuses_when_another_control_keeps_focus(ft, no_focus_move):
    d, be, iban, amount = make(MockBackend, ft)
    no_focus_move(be)
    be._set_focus(iban)
    be.clear_events()
    el = d.find('edit "Amount"')
    with pytest.raises(ElementNotFound, match="another control has it"):
        el.type("1250.00")
    with pytest.raises(ElementNotFound, match="another control has it"):
        el.press("enter", require_focus=False)  # opting out never accepts a *known* wrong target
    assert iban.value == "DE89" and amount.value == ""
    assert keystrokes(be) == []


def test_focus_elsewhere_even_without_states_is_refused(ft, no_states, no_focus_move):
    d, be, iban, amount = make(MockBackend, ft)  # focused_element() works, states do not
    no_focus_move(be)
    be._set_focus(iban)
    el = d.find('edit "Amount"')
    assert el._has_focus() is False
    with pytest.raises(ElementNotFound, match="another control"):
        el.type("x", require_focus=False)
    assert iban.value == "DE89" and amount.value == ""


# ----------------------------------------------------------------- success
def test_type_succeeds_when_focus_is_proven(ft):
    d, be, iban, amount = make(MockBackend, ft)
    be._set_focus(iban)
    el = d.find('edit "Amount"')
    el.type("1250.00")
    assert amount.value == "1250.00" and iban.value == "DE89"
    el.press("backspace")
    assert amount.value == "1250.0"
    el.set_text("99", native=False)
    assert amount.value == "99"


def test_focus_proven_by_focused_element_without_states(ft, no_states):
    d, be, iban, amount = make(MockBackend, ft)  # no focused flag, but focused_element() reports it
    el = d.find('edit "Amount"')
    el.type("42")
    assert amount.value == "42"


def test_focus_on_descendant_counts_as_proof(ft, no_states, monkeypatch):
    d, be, iban, amount = make(MockBackend, ft)
    be._set_focus(amount)
    # the backend reports the focused leaf with its parent chain (like UIA)
    monkeypatch.setattr(be, "focused_element", lambda: be._node_with_parents(be._focus))
    win = Element(MockNode(be, amount.window.root), d)
    assert win._has_focus() is True
    win.press("a")
    assert amount.value == "a"


# ------------------------------------------------------------------- opt-out
def test_per_call_opt_out_types_when_focus_cannot_be_told(ft, no_states):
    d, be, iban, amount = make(BlindBackend, ft)
    el = d.find('edit "Amount"')
    el.type("1250.00", require_focus=False)
    assert amount.value == "1250.00"
    el.press("backspace", require_focus=False)
    assert amount.value == "1250.0"
    el.set_text("7", native=False, require_focus=False)
    assert amount.value == "7"


def test_desktop_level_opt_out_and_per_call_override(ft, no_states):
    d, be, iban, amount = make(BlindBackend, ft)
    el = d.find('edit "Amount"')
    d.require_focus = False
    el.type("12")
    assert amount.value == "12"
    with pytest.raises(ElementNotFound, match="could not confirm"):
        el.type("3", require_focus=True)  # the per-call keyword wins
    assert amount.value == "12"


def test_desktop_without_require_focus_attribute_defaults_to_required(ft, no_states):
    d, be, iban, amount = make(BlindBackend, ft)

    class Minimal:
        backend, mouse, keyboard, default_timeout = d.backend, d.mouse, d.keyboard, 0.05
        sleep = staticmethod(ft.sleep)

    el = Element(d.find('edit "Amount"').node, Minimal())
    with pytest.raises(ElementNotFound, match="could not confirm"):
        el.type("1")
    assert amount.value == ""


# ------------------------------------------------- composes with verify/repair
def test_repair_after_garbled_typing_with_opt_out(ft, no_states):
    d, be, iban, amount = make(GarblingBlindBackend, ft)
    el = d.find('edit "Amount"')
    el.type("café", require_focus=False)
    assert amount.value == "café"  # repaired natively (set-value needs no focus)


def test_repair_typing_path_inherits_the_opt_out(ft, no_states, monkeypatch):
    d, be, iban, amount = make(GarblingBlindBackend, ft)
    monkeypatch.setattr(MockNode, "set_value", lambda self, text: False)  # repair must paste
    el = d.find('edit "Amount"')
    el.type("café", require_focus=False)
    assert amount.value == "café"


def test_repair_with_proven_focus(ft, monkeypatch):
    class Garbling(MockBackend):
        def type_text(self, text: str) -> None:
            if len(text) > 1:
                text = text[-1] * len(text)
            super().type_text(text)

    d, be, iban, amount = make(Garbling, ft)
    monkeypatch.setattr(MockNode, "set_value", lambda self, text: False)
    d.find('edit "Amount"').type("café")
    assert amount.value == "café" and iban.value == "DE89"


def test_native_set_text_needs_no_focus_proof(ft, no_states):
    d, be, iban, amount = make(BlindBackend, ft)
    d.find('edit "Amount"').set_text("1250.00")
    assert amount.value == "1250.00"
    assert keystrokes(be) == []
