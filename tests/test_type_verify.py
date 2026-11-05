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
