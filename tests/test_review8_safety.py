"""Policy/target-binding defects found in review 8 (2026-11-06).

* GuardedElement.press/type sent keys through the unbound desktop path (another
  window could receive them) and clicked the element to focus it before any check.
* The keyboard tripwire judged each call alone: ``hold("ctrl")`` + ``press("s")``.
* Clicks checked the requested point, not the live cursor; drags never checked the drop.
* A flow's ``exempt: ["*"]`` switched the click tripwire off.
* ``fold_text`` kept soft hyphens, zero-width characters and ``&`` mnemonics.
* Unicode typing injected a whole run of text after a single target check.
"""

from __future__ import annotations

import pytest

from locum import Desktop, TargetLost, _target
from locum.backends.mock import MockBackend, MockNode
from locum.element import Element
from locum.errors import PolicyViolation, WorkflowError
from locum.llm.safety import GuardedDesktop, Policy, fold_text
from locum.rpa.flow import flow_exempt_rules, run_flow


class FocusBackend(MockBackend):
    def focused_element(self):
        return self._node_with_parents(self._focus)


def _desktop(be):
    return Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)


def _keys(be):
    return [e for e in be.events if e[0].startswith("key")]


# ============================================================ 1: bound element input
def test_guarded_element_press_keys_do_not_reach_a_window_that_took_the_focus(monkeypatch):
    be = FocusBackend(width=1280, height=800)
    a = be.add_window("Target", (0, 0, 400, 400), app="a")
    a.add_edit("Field", (10, 10, 200, 30))
    b = be.add_window("Chat", (500, 0, 400, 400), app="chat")
    chat = b.add_edit("Message", (10, 10, 200, 30))
    d = _desktop(be)
    orig = Element._ensure_focus

    def steal(self, *args, **kw):
        orig(self, *args, **kw)
        be._raise(b)
        be._focus = chat  # a chat pop-up takes the foreground right after focusing

    monkeypatch.setattr(Element, "_ensure_focus", steal)
    el = GuardedDesktop(d, Policy.default()).find('edit "Field"')
    with pytest.raises(TargetLost):
        el.press("x", "y", "z")
    assert (chat.value or "") == ""


def test_guarded_element_type_after_tab_does_not_reach_a_window_that_took_the_focus():
    class B(FocusBackend):
        def _focus_next(self, w):  # the Tab lands while a chat pop-up grabs the foreground
            self._raise(self.chatwin)
            self._focus = self.chat

    be = B(width=1280, height=800)
    a = be.add_window("Target", (0, 0, 400, 400), app="a")
    a.add_edit("Field", (10, 10, 200, 30))
    a.add_edit("Next", (10, 60, 200, 30))
    be.chatwin = be.add_window("Chat", (500, 0, 400, 400), app="chat")
    be.chat = be.chatwin.add_edit("Message", (10, 10, 200, 30))
    d = _desktop(be)
    d.window("Target").activate()
    with pytest.raises(TargetLost):
        GuardedDesktop(d, Policy.default()).find('edit "Field"').type("abc\tsecret", verify=False)
    assert (be.chat.value or "") == ""


def test_guarded_element_press_and_type_still_work():
    be = FocusBackend(width=1280, height=800)
    a = be.add_window("Target", (0, 0, 400, 400), app="a")
    field = a.add_edit("Field", (10, 10, 200, 30))
    nxt = a.add_edit("Next", (10, 60, 200, 30))
    d = _desktop(be)
    g = GuardedDesktop(d, Policy.default())
    g.find('edit "Field"').type("abc\tdef", verify=False)
    assert field.value == "abc" and nxt.value == "def"
    g.find('edit "Field"').press("x", presses=2)
    assert field.value == "abcxx"


# ============================================================ 2: no click before the gate
def test_guarded_element_press_does_not_click_a_blocked_element_to_focus_it(monkeypatch):
    be = FocusBackend(width=1280, height=800)
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    danger = win.add_button("Delete account", (120, 340, 120, 30))
    d = _desktop(be)
    monkeypatch.setattr(MockNode, "focus", lambda self: False)  # native focus fails: a click would focus
    g = GuardedDesktop(d, Policy.default())
    with pytest.raises(PolicyViolation, match="Delete"):
        g.find('button "Delete account"').press("escape")
    assert danger.clicks == 0
    assert not _keys(be)


def test_guarded_element_press_dry_run_sends_nothing(monkeypatch):
    be = FocusBackend(width=1280, height=800)
    win = be.add_window("W", (0, 0, 400, 400), app="w")
    field = win.add_edit("Field", (10, 10, 200, 30))
    d = _desktop(be)
    monkeypatch.setattr(MockNode, "focus", lambda self: False)
    g = GuardedDesktop(d, Policy.default(dry_run=True))
    g.find('edit "Field"').press("x")
    assert field.clicks == 0 and not _keys(be)


# ============================================================ 3: held modifiers
def _form():
    be = FocusBackend(width=1280, height=800)
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount")
    d = _desktop(be)
    g = GuardedDesktop(d, Policy.default())
    g.find('edit "Amount"').focus()
    be.events.clear()
    return be, d, g


def test_hold_ctrl_then_press_s_is_ctrl_s():
    be, d, g = _form()
    with pytest.raises(PolicyViolation, match="ctrl"):
        with g.hold("ctrl"):
            g.press("s")
    assert ("key_down", "s") not in be.events


def test_key_down_ctrl_then_press_enter_is_ctrl_enter():
    be, d, g = _form()
    g.key_down("ctrl")
    try:
        with pytest.raises(PolicyViolation):
            g.press("enter")
        with pytest.raises(PolicyViolation):
            g.hotkey("s")
        with pytest.raises(PolicyViolation):
            g.type("s")
    finally:
        d.key_up("ctrl")
    assert ("key_down", "enter") not in be.events and ("key_down", "s") not in be.events
    g.press("s")  # released: a plain character again
    assert ("key_down", "s") in be.events


@pytest.mark.parametrize("call", [
    lambda g: g.key_down("num_enter"),
    lambda g: g.key_down("kp_enter"),
    lambda g: g.key_down("return"),
    lambda g: g.hold("enter").__enter__(),
])
def test_key_down_and_hold_judge_every_enter_alias(call):
    be = FocusBackend(width=1280, height=800)
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount")
    win.add_button("Pay now", (320, 300, 100, 30), attrs={"default": True})
    d = _desktop(be)
    g = GuardedDesktop(d, Policy.default())
    g.find('edit "Amount"').focus()
    be.events.clear()
    with pytest.raises(PolicyViolation, match="Pay"):
        call(g)
    assert not _keys(be)


# ============================================================ 4: live pointer / drop target
class Drift(MockBackend):
    """The user nudges the mouse right after locum's last move."""

    drift = None

    def mouse_move(self, x, y):
        super().mouse_move(x, y)
        if self.drift:
            super().mouse_move(*self.drift)


def _two_windows():
    be = Drift(width=1280, height=800)
    a = be.add_window("Target", (0, 0, 400, 400), app="a")
    a.add_button("Safe", (10, 10, 100, 30))
    b = be.add_window("Other", (500, 0, 400, 400), app="b")
    danger = b.add_button("Send", (10, 10, 100, 30))
    d = _desktop(be)
    return be, d, danger


def test_bound_click_checks_the_live_cursor_position():
    be, d, danger = _two_windows()
    be.drift = (520, 20)
    with _target.bind(_target.for_window(d.window("Target"))):
        with pytest.raises(TargetLost):
            d.mouse.click(50, 20)
    assert danger.clicks == 0
    assert not [e for e in be.events if e[0] == "down"]


def test_bound_drag_does_not_drop_on_a_window_that_appeared_over_the_drop_point():
    be, d, danger = _two_windows()
    orig = be.mouse_down

    def down(*a, **k):
        orig(*a, **k)
        be.add_window("Pop-up", (150, 150, 200, 200), app="popup")  # covers the drop point mid-drag

    be.mouse_down = down
    with _target.bind(_target.for_window(d.window("Target"))):
        with pytest.raises(TargetLost):
            d.mouse.drag((50, 20), (200, 200), duration=0, hold=0)
    ups = [i for i, e in enumerate(be.events) if e[0] == "up"]
    assert ups, "the button must be released"
    moves = [e for e in be.events[:ups[-1]] if e[0] == "move"]
    assert tuple(moves[-1][1:3]) == (50, 20)  # carried back to the start before release


def test_bound_drag_where_the_cursor_drifted_does_not_drop():
    be, d, danger = _two_windows()
    orig = be.mouse_down

    def down(*a, **k):
        orig(*a, **k)
        be.drift = (520, 20)  # the user grabs the mouse once the button is down

    be.mouse_down = down
    with _target.bind(_target.for_window(d.window("Target"))):
        with pytest.raises(TargetLost):
            d.mouse.drag((50, 20), (200, 200), duration=0, hold=0)


def test_bound_drag_within_the_window_or_to_the_approved_window_still_works():
    be, d, _ = _two_windows()
    with _target.bind(_target.for_window(d.window("Target"))):
        d.mouse.drag((50, 20), (200, 200), duration=0, hold=0)
    assert be._pos == (200, 200)
    with _target.bind(_target.for_window(d.window("Target"))):
        d.mouse.drag((50, 20), (520, 20), duration=0, hold=0)  # cross-window drop, window unchanged
    assert be._pos == (520, 20)


# ============================================================ 5: flow exempt rules
@pytest.mark.parametrize("rule", ["*", "button", "window", "button:nth(1)", 'button[name%="*"]',
                                  'button[name~=".*"]', "button[name]", 'button[name!="x"]',
                                  {"selector": "*", "window": "Bank"}, "launch=*"])
def test_flow_exempt_rejects_rules_that_name_no_target(rule):
    with pytest.raises(ValueError, match="too broad"):
        flow_exempt_rules({"exempt": [rule]})


@pytest.mark.parametrize("rule", ['button "Delete row"', "button#delete", 'button[name^="Delete"]',
                                  'window "Bank" button "Transfer all funds"', "keys=ctrl+s",
                                  'button:has-text("Delete")', "launch=notepad.exe"])
def test_flow_exempt_accepts_specific_rules(rule):
    assert len(flow_exempt_rules({"exempt": [rule]})) == 1


def test_flow_exempt_star_cannot_turn_the_tripwire_off():
    be = FocusBackend(width=1280, height=800)
    win = be.add_window("Bank", (100, 100, 500, 400), app="bank")
    pay = win.add_button("Transfer all funds", (20, 340, 160, 30))
    d = _desktop(be)
    with pytest.raises(WorkflowError, match="too broad"):
        run_flow({"exempt": ["*"], "steps": [{"click": 'button "Transfer all funds"'}]}, d, policy=Policy.default())
    assert pay.clicks == 0


# ============================================================ 6: fold_text
@pytest.mark.parametrize("label", ["De&lete", "Del­ete", "De​lete", "Dele‍te", "‎Delete",
                                   "&Delete account"])
def test_tripwire_sees_through_invisible_characters_and_mnemonics(label):
    assert "delete" in fold_text(label).lower()
    assert Policy.default().confirmation_reason("click", {}, {"target": label}) is not None


def test_fold_text_keeps_literal_ampersands():
    assert fold_text("Save && Exit") == "Save & Exit"
    assert fold_text("Save & Exit") == "Save & Exit"


# ============================================================ 7: unicode typing in chunks
def test_unicode_typing_checks_the_target_between_chunks():
    be = MockBackend(width=1200, height=800)
    chat = be.add_window("Chat", (600, 0, 500, 400), app="chat")
    box = chat.add_edit("Message", (620, 300, 400, 30))
    note = be.add_window("Notepad", (0, 0, 500, 400), app="notepad")
    note.add_edit("Text editor", (20, 40, 400, 300))
    d = _desktop(be)
    el = d.find('edit "Text editor"')
    orig = be.type_text
    calls = []

    def wrapped(text):
        orig(text)
        calls.append(text)
        if len(calls) == 1:
            be.activate_window(chat.handle)  # a chat takes the foreground mid-text

    be.type_text = wrapped
    with pytest.raises(TargetLost):
        el.type("x" * 200, mode="unicode", verify=False)
    assert len(calls) == 1 and len(calls[0]) <= 32
    assert (box.value or "") == ""


def test_unicode_typing_sends_long_text_whole():
    be = MockBackend(width=1200, height=800)
    note = be.add_window("Notepad", (0, 0, 500, 400), app="notepad")
    editor = note.add_edit("Text editor", (20, 40, 400, 300))
    d = _desktop(be)
    text = "".join(chr(ord("a") + i % 26) for i in range(100)) + "é"
    d.find('edit "Text editor"').type(text, mode="unicode")
    assert editor.value == text
    assert all(len(e[1]) <= 32 for e in be.events if e[0] == "type")
