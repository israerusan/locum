from __future__ import annotations

import pytest

from pyoilot.backends.base import CAP_CLIPBOARD, CAP_UNICODE_TYPING
from pyoilot.backends.mock import MockBackend
from pyoilot.errors import FailSafeTriggered, NotSupported
from pyoilot.failsafe import FailSafe, abort, reset_abort
from pyoilot.keyboard import Keyboard


class NoUnicodeBackend(MockBackend):
    @property
    def capabilities(self):
        return super().capabilities - {CAP_UNICODE_TYPING}

    def type_text(self, text):
        raise NotSupported("no unicode")


class BareBackend(NoUnicodeBackend):
    @property
    def capabilities(self):
        return super().capabilities - {CAP_CLIPBOARD}

    def get_clipboard(self):
        raise NotSupported("no clipboard")

    def set_clipboard(self, text):
        raise NotSupported("no clipboard")


class LyingBackend(NoUnicodeBackend):
    """Advertises unicode typing but cannot do it."""

    @property
    def capabilities(self):
        return MockBackend.capabilities.fget(self)


@pytest.fixture(autouse=True)
def _clean_abort():
    reset_abort()
    yield
    reset_abort()


def setup(backend_cls=MockBackend):
    be = backend_cls(width=1280, height=800)
    win = be.add_window("Form", (100, 100, 400, 300))
    edit = win.add_edit("Name", (10, 50, 200, 28))
    be._set_focus(edit)
    kb = Keyboard(be, FailSafe(be), sleep=lambda s: None)
    return be, edit, kb


def keyev(be):
    return [e for e in be.events if e[0] in ("key_down", "key_up")]


def test_down_up_aliases():
    be, _, kb = setup()
    kb.down("Control")
    assert be.pressed_keys == {"ctrl"} and kb.pressed == ["ctrl"]
    kb.up("CTRL")
    assert be.pressed_keys == set() and kb.pressed == []
    with pytest.raises(ValueError):
        kb.down("notakey")


def test_press_sequence_and_repeats():
    be, edit, kb = setup()
    kb.press("a", "b", presses=2)
    assert edit.value == "abab"
    be.clear_events()
    kb.press("Return")
    assert keyev(be) == [("key_down", "enter"), ("key_up", "enter")]


def test_press_adds_shift_and_combos():
    be, edit, kb = setup()
    kb.press("A", "!")
    assert edit.value == "A!"
    assert keyev(be)[:4] == [("key_down", "shift"), ("key_down", "a"), ("key_up", "a"), ("key_up", "shift")]
    kb.press("ctrl+a", "backspace")
    assert edit.value == ""
    assert not be.pressed_keys


def test_press_interval():
    be = MockBackend()
    sleeps = []
    kb = Keyboard(be, sleep=sleeps.append)
    kb.press("tab", "tab", "enter", interval=0.2)
    assert sleeps == [0.2, 0.2]


def test_hotkey_order_and_forms():
    be, _, kb = setup()
    kb.hotkey("ctrl+shift+s")
    assert keyev(be) == [
        ("key_down", "ctrl"), ("key_down", "shift"), ("key_down", "s"),
        ("key_up", "s"), ("key_up", "shift"), ("key_up", "ctrl"),
    ]
    be.clear_events()
    kb.hotkey("ctrl", "c")
    assert keyev(be) == [("key_down", "ctrl"), ("key_down", "c"), ("key_up", "c"), ("key_up", "ctrl")]
    be.clear_events()
    kb.hotkey(["alt", "f4"])
    assert [e[1] for e in keyev(be)] == ["alt", "f4", "f4", "alt"]
    with pytest.raises(ValueError):
        kb.hotkey()


def test_hotkey_releases_in_finally_on_error():
    class Exploding(MockBackend):
        def key_down(self, key):
            if key == "s":
                raise RuntimeError("boom")
            super().key_down(key)

    be = Exploding()
    kb = Keyboard(be, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        kb.hotkey("ctrl+shift+s")
    assert be.pressed_keys == set()
    assert keyev(be)[-2:] == [("key_up", "shift"), ("key_up", "ctrl")]
    assert kb.pressed == []


def test_hotkey_releases_on_failsafe_during_interval():
    be = MockBackend()
    n = {"i": 0}

    def sleep(s):
        n["i"] += 1
        raise FailSafeTriggered("stop")  # simulate abort while holding

    kb = Keyboard(be, sleep=sleep)
    with pytest.raises(FailSafeTriggered):
        kb.hotkey("ctrl", "alt", "delete", interval=0.1)
    assert be.pressed_keys == set()


def test_hotkey_copy_paste_on_widget():
    be, edit, kb = setup()
    edit.value = "hello"
    kb.hotkey("mod+a")
    kb.hotkey("mod+c")
    assert be.clipboard == "hello"
    kb.hotkey("ctrl+v")
    assert edit.value == "hello"


def test_hold_context_manager():
    be, _, kb = setup()
    with kb.hold("shift", "ctrl") as held:
        assert held == ["shift", "ctrl"]
        assert be.pressed_keys == {"shift", "ctrl"}
        kb.hotkey("ctrl", "x")  # ctrl already held: must stay held afterwards
        assert "ctrl" in be.pressed_keys
    assert be.pressed_keys == set()
    with pytest.raises(KeyError):
        with kb.hold("alt"):
            raise KeyError("x")
    assert be.pressed_keys == set()


def test_release_all():
    be, _, kb = setup()
    kb.down("ctrl")
    kb.down("shift")
    kb.down("a")
    assert kb.release_all() == ["a", "shift", "ctrl"]
    assert be.pressed_keys == set()
    assert kb.release_all() == []


def test_type_unicode_mode():
    be, edit, kb = setup()
    kb.type("héllo wörld ✓", mode="unicode")
    assert edit.value == "héllo wörld ✓"
    assert ("type", "héllo wörld ✓") in be.events


def test_type_unicode_newlines_become_keys():
    be, edit, kb = setup()
    edit.attrs["multiline"] = True
    kb.type("a\nb\tc", mode="unicode")
    assert edit.value == "a\nbc"  # tab cycles focus back to the only focusable widget
    kinds = [e for e in be.events if e[0] in ("type", "key_down")]
    assert kinds == [("type", "a"), ("key_down", "enter"), ("type", "b"), ("key_down", "tab"), ("type", "c")]


def test_type_unicode_interval_per_char():
    be = MockBackend()
    sleeps = []
    kb = Keyboard(be, sleep=sleeps.append)
    kb.type("abc", mode="unicode", interval=0.05)
    assert [e for e in be.events if e[0] == "type"] == [("type", "a"), ("type", "b"), ("type", "c")]
    assert sleeps == [0.05, 0.05]


def test_type_keys_mode_shift_handling():
    be, edit, kb = setup()
    text = 'Hello, World! ~`@#$%^&*()_+{}|:"<>? 123'
    kb.type(text, mode="keys")
    assert edit.value == text
    assert not any(e[0] == "type" for e in be.events)
    downs = [e[1] for e in be.events if e[0] == "key_down"]
    assert "H" not in downs and "h" in downs and "!" not in downs and "1" in downs
    assert be.pressed_keys == set()


def test_type_keys_mode_newline_tab():
    be, edit, kb = setup()
    edit.attrs["multiline"] = True
    kb.type("x\r\ny", mode="keys")
    assert edit.value == "x\ny"
    kb.type("\t", mode="keys")
    assert ("key_down", "tab") in be.events


def test_type_keys_mode_respects_held_shift():
    be, edit, kb = setup()
    with kb.hold("shift"):
        kb.type("A", mode="keys")
        assert "shift" in be.pressed_keys  # not released by the shifted tap
    assert edit.value == "A"


def test_type_clipboard_mode_restores_clipboard():
    be, edit, kb = setup()
    be.clipboard = "precious"
    kb.type("pasted ✓ text", mode="clipboard")
    assert edit.value == "pasted ✓ text"
    assert be.clipboard == "precious"
    keys = [e for e in keyev(be)]
    assert keys == [("key_down", "ctrl"), ("key_down", "v"), ("key_up", "v"), ("key_up", "ctrl")] or \
        keys == [("key_down", "cmd"), ("key_down", "v"), ("key_up", "v"), ("key_up", "cmd")]


def test_type_clipboard_restores_even_on_error():
    class Boom(MockBackend):
        def key_down(self, key):
            if key == "v":
                raise RuntimeError("paste failed")
            super().key_down(key)

    be = Boom()
    be.clipboard = "keep me"
    kb = Keyboard(be, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        kb.type("xyz", mode="clipboard")
    assert be.clipboard == "keep me"
    assert be.pressed_keys == set()


def test_type_clipboard_settle_sleep():
    be = MockBackend()
    sleeps = []
    kb = Keyboard(be, sleep=sleeps.append, clipboard_settle=0.07)
    kb.type("x", mode="clipboard")
    assert sleeps == [0.07]


def test_auto_prefers_unicode():
    be, edit, kb = setup()
    kb.type("Ünïcode")
    assert ("type", "Ünïcode") in be.events
    assert edit.value == "Ünïcode"


def test_auto_without_unicode_mixes_keys_and_clipboard():
    be, edit, kb = setup(NoUnicodeBackend)
    be.clipboard = "orig"
    kb.type("Price: 5€ ok")
    assert edit.value == "Price: 5€ ok"
    assert be.clipboard == "orig"
    sets = [e for e in be.events if e[0] == "set_clipboard"]
    assert sets == [("set_clipboard", "€"), ("set_clipboard", "orig")]


def test_auto_without_clipboard_uses_keys_only():
    be, edit, kb = setup(BareBackend)
    kb.type("abc")
    assert edit.value == "abc"


def test_auto_falls_back_when_unicode_unexpectedly_unsupported():
    be, edit, kb = setup(LyingBackend)
    assert be.has(CAP_UNICODE_TYPING)
    kb.type("Hi!")
    assert edit.value == "Hi!"


def test_explicit_unicode_mode_raises_when_unsupported():
    be, _, kb = setup(BareBackend)
    with pytest.raises(NotSupported):
        kb.type("x", mode="unicode")


def test_type_validation():
    be, edit, kb = setup()
    with pytest.raises(ValueError):
        kb.type("x", mode="telepathy")
    with pytest.raises(TypeError):
        kb.type(123)
    kb.type("")
    assert be.events == []


def test_failsafe_stops_typing_midway():
    be, edit, kb = setup()
    n = {"i": 0}

    def sleep(s):
        n["i"] += 1
        if n["i"] == 3:
            abort("user")

    kb2 = Keyboard(be, FailSafe(be), sleep=sleep)
    with pytest.raises(FailSafeTriggered):
        kb2.type("abcdefgh", mode="keys", interval=0.01)
    assert edit.value == "abc"
    assert be.pressed_keys == set()


def test_failsafe_blocks_press_but_not_up():
    be, _, kb = setup()
    kb.down("ctrl")
    be.mouse_move(0, 0)
    with pytest.raises(FailSafeTriggered):
        kb.press("a")
    with pytest.raises(FailSafeTriggered):
        kb.hotkey("ctrl+c")
    with pytest.raises(FailSafeTriggered):
        kb.type("x")
    kb.up("ctrl")
    assert be.pressed_keys == set()


def test_pause():
    be = MockBackend()
    sleeps = []
    kb = Keyboard(be, pause=0.1, sleep=sleeps.append)
    kb.press("a")
    kb.hotkey("ctrl+c")
    kb.type("hi")
    assert sleeps == [0.1, 0.1, 0.1]
