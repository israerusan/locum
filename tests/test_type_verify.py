"""Element.type read-back verification and repair of garbled keystrokes.

Reproduces what was seen on a real Windows 11 machine: Notepad, when busy,
repeats a later character in place of earlier ones
("café €1.234,56" -> "éééé 444444,").
"""

from __future__ import annotations

import pytest

from _stub_desktop import StubDesktop
from pypilot.backends.mock import MockBackend
from pypilot.element import _repair_candidates, _typed_into
from pypilot.errors import ActionTimeout


class GarblingBackend(MockBackend):
    """Types every character as the last one of the string (stale VK_PACKET style)."""

    garble = True

    def type_text(self, text: str) -> None:
        if self.garble and len(text) > 1:
            text = text[-1] * len(text)
        super().type_text(text)


@pytest.fixture
def env():
    be = GarblingBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Editor", (100, 100, 600, 400), app="notepad.exe")
    field = win.add_edit("Text editor", (10, 40, 560, 300), automation_id="ed")
    return d, be, field


# ----------------------------------------------------------------- helpers
@pytest.mark.parametrize("before,after,text", [
    ("", "abc", "abc"),
    ("hello", "hello world", " world"),       # appended
    ("held", "hello world", "lo worl"),       # inserted in the middle
    ("hello", "XY", "XY"),                    # replaced a full selection
    ("one two three", "one 2 three", "2"),   # replaced a partial selection
    ("ab", "abb", "b"),                       # ambiguous position, but consistent
])
def test_typed_into_accepts_correct_results(before, after, text):
    assert _typed_into(before, after, text)


@pytest.mark.parametrize("before,after,text", [
    ("", "éééé", "café"),
    ("x", "x44444", "€1.234"),
    ("hello", "hello", " world"),             # nothing arrived
    ("hello", "hello wor", " world"),         # truncated
])
def test_typed_into_rejects_garbled_results(before, after, text):
    assert not _typed_into(before, after, text)


def test_repair_candidates():
    assert _repair_candidates("", "éééé", "café") == {"café"}
    assert _repair_candidates("Hi ", "Hi ééé", "abc") == {"Hi abc"}
    assert _repair_candidates("Hi there", "Hi XXXthere", "abc") == {"Hi abcthere"}
    # the garbage may sit either side of the space: ambiguous, never guessed
    assert _repair_candidates("Hi there", "Hi XXX there", "abc") == {"Hiabc there", "Hi abcthere"}
    # ambiguous: the garbage "b" could have gone before or after the existing "b"
    assert _repair_candidates("ab", "abb", "Z") == {"aZb", "abZ"}
    # not a plain insertion (a selection was overwritten): no safe repair
    assert _repair_candidates("hello", "hXXo", "abc") == set()


# ----------------------------------------------------------- Element.type
def test_type_repairs_garbled_keystrokes_in_empty_field(env):
    d, be, field = env
    e = d.find("#ed", timeout=0)
    e.type("café €1.234")
    assert field.value == "café €1.234"
    assert any(ev[0] == "a11y_set_value" for ev in be.events)  # repaired natively


def test_type_repairs_after_existing_text(env):
    d, be, field = env
    field.value = "Total: "
    e = d.find("#ed", timeout=0)
    e.type("€42")
    assert field.value == "Total: €42"


def test_type_without_garbling_does_not_repair(env):
    d, be, field = env
    be.garble = False
    e = d.find("#ed", timeout=0)
    e.type("plain ✓")
    assert field.value == "plain ✓"
    assert not any(ev[0] == "a11y_set_value" for ev in be.events)


def test_type_ambiguous_garbling_raises_instead_of_guessing(env):
    d, be, field = env
    field.value = "ab"
    e = d.find("#ed", timeout=0)
    with pytest.raises(ActionTimeout, match="ambiguous"):
        e.type("Zb")  # garbled to "bb" -> "abbb": the insertion point cannot be told
    assert field.value == "abbb"  # left as the app has it, not overwritten with a guess


def test_type_verify_false_skips_readback(env):
    d, be, field = env
    e = d.find("#ed", timeout=0)
    e.type("café", verify=False)
    assert field.value == "éééé"


def test_type_with_tab_or_enter_is_not_verified_or_repaired(env):
    d, be, field = env
    be.garble = False
    e = d.find("#ed", timeout=0)
    e.type("a\tb")  # Tab is a key press (moves focus); never rewritten into a literal tab
    assert "\t" not in field.value
    assert not any(ev[0] == "a11y_set_value" for ev in be.events)


# ------------------------------------------------- reformatting is not "garbling"
class CompletingBackend(MockBackend):
    """An address-bar-like field that autocompletes, or a phone mask that formats digits."""

    behaviour = "complete"

    def type_text(self, text: str) -> None:
        if self.behaviour == "complete" and text == "goo":
            text = "google.com"
        elif self.behaviour == "mask" and text == "5551234":
            text = "(555) 123-4"
        elif self.behaviour == "upper":
            text = text.upper()
        super().type_text(text)


@pytest.fixture
def completing():
    be = CompletingBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Browser", (100, 100, 600, 400), app="browser.exe")
    field = win.add_edit("Address", (10, 40, 560, 28), automation_id="url")
    return d, be, field


@pytest.mark.parametrize("behaviour,typed,shown", [
    ("complete", "goo", "google.com"),
    ("mask", "5551234", "(555) 123-4"),
    ("upper", "abc", "ABC"),
])
def test_type_accepts_autocomplete_masks_and_case(completing, behaviour, typed, shown):
    d, be, field = completing
    be.behaviour = behaviour
    d.find("#url", timeout=0).type(typed)
    assert field.value == shown  # left as the app made it, not "repaired" back
    assert not any(ev[0] == "a11y_set_value" for ev in be.events)


def test_looks_reformatted():
    from pypilot.element import _looks_reformatted

    assert _looks_reformatted("", "google.com", "goo")
    assert _looks_reformatted("x ", "x (555) 123-4", "5551234")
    assert not _looks_reformatted("", "éééé", "café")
    assert not _looks_reformatted("caf", "cafééé", "éé€")  # garbled after existing text


# -------------------------------------------------------------- Desktop.type
@pytest.fixture
def real():
    from pypilot import Desktop

    be = GarblingBackend(width=1280, height=800)
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    win = be.add_window("Editor", (100, 100, 600, 400), app="notepad.exe")
    field = win.add_edit("Text editor", (10, 40, 560, 300), automation_id="ed")
    return d, be, field


def _focus(d, sel):
    e = d.find(sel, timeout=0)
    e.focus()
    assert e.is_focused
    return e


def test_desktop_type_repairs_the_focused_text_field(real):
    d, be, field = real
    _focus(d, "#ed")
    d.type("café €1.234")
    assert field.value == "café €1.234"


def test_desktop_type_verify_false_is_raw(real):
    d, be, field = real
    _focus(d, "#ed")
    d.type("café", verify=False)
    assert field.value == "éééé"


def test_desktop_type_without_a_focused_text_field_types_unverified(real):
    d, be, field = real
    be.garble = False
    d.type("abc")  # nothing focused: plain keystrokes, no error
    assert not any(ev[0] == "a11y_set_value" for ev in be.events)
