"""Polish pass 3 (core): regression tests for the desktop/element/keyboard/input-lock fixes."""
from __future__ import annotations

import random
import threading
import time

import pytest

import locum
from locum import _inputlock
from locum.backends.mock import MockBackend
from locum.element import _common_prefix, _common_suffix, _repair_candidates, _typed_into
from locum.errors import BackendError, NotSupported, TargetLost
from locum.keyboard import Keyboard


# --------------------------------------------------- timed Desktop.click binding
def _popup_scene():
    be = MockBackend()
    w = be.add_window("App", rect=(100, 100, 400, 300))
    hits = []
    w.add_button("Save", (50, 50, 100, 30), on_click=lambda *a: hits.append("Save"))
    state = {"n": 0}
    orig = be.mouse_move

    def mm(x, y):
        orig(x, y)
        state["n"] += 1
        if state["n"] == 1:  # another app's pop-up appears over the target during the move
            pop = be.add_window("Popup", rect=(100, 100, 400, 300), app="other", pid=999)
            pop.add_button("Delete all", (0, 0, 400, 300), on_click=lambda *a: hits.append("Delete all"))

    be.mouse_move = mm
    return be, hits


@pytest.mark.parametrize("kw", [{"duration": 0.2}, {"humanize": True}, {"clicks": 2, "interval": 0.05}])
def test_timed_desktop_click_never_clicks_a_window_that_appears_over_the_element(kw):
    be, hits = _popup_scene()
    d = locum.Desktop(backend=be, sleep=lambda s: None, failsafe=False)
    with pytest.raises(TargetLost):
        d.click('button "Save"', **kw)
    assert hits == []


def test_timed_desktop_click_still_clicks_an_uncovered_element():
    be = MockBackend()
    hits = []
    be.add_window("App", rect=(100, 100, 400, 300)).add_button(
        "Save", (50, 50, 100, 30), on_click=lambda *a: hits.append("Save"))
    d = locum.Desktop(backend=be, sleep=lambda s: None, failsafe=False)
    d.click('button "Save"', duration=0.1)
    assert hits == ["Save"]


def test_drag_from_an_element_is_bound_to_its_window():
    be = MockBackend()
    w = be.add_window("App", rect=(100, 100, 400, 300))
    w.add_button("Item", (10, 10, 50, 20))
    d = locum.Desktop(backend=be, sleep=lambda s: None, failsafe=False)
    seen = []
    orig = locum.desktop._target.bind

    def spy(target):
        seen.append(target)
        return orig(target)

    locum.desktop._target.bind = spy
    try:
        d.drag('button "Item"', (300, 300), duration=0)
    finally:
        locum.desktop._target.bind = orig
    assert any(t is not None for t in seen)


# --------------------------------------------- disabled element: lock not held
def test_waiting_for_a_disabled_element_does_not_hold_the_input_lock():
    be = MockBackend()
    be.add_window("App").add_button("Post", (10, 10, 80, 24), enabled=False)
    d = locum.Desktop(backend=be, default_timeout=2, input_lock_timeout=0.5)
    el = d.find('button "Post"')
    errors = []

    def worker():
        try:
            el.click()
        except Exception as e:  # ElementNotFound: stays disabled
            errors.append(type(e).__name__)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.2)
    d.press("tab")  # DesktopBusy before the fix
    t.join()
    assert errors == ["ElementNotFound"]


# -------------------------------------------- clipboard restore keeps the error
def test_failed_clipboard_restore_does_not_replace_the_paste_error():
    be = MockBackend()
    be.set_clipboard("user data")
    orig_down = be.key_down

    def key_down(k):
        if k == "v":
            raise TargetLost("focus moved")
        orig_down(k)

    be.key_down = key_down

    def restore(saved):
        raise BackendError("OpenClipboard failed")

    be.restore_clipboard = restore
    kb = Keyboard(be, sleep=lambda s: None)
    with pytest.raises(TargetLost):
        kb.type("secret text", mode="clipboard")


def test_clipboard_restore_failure_after_a_successful_paste_still_raises():
    be = MockBackend()
    be.set_clipboard("user data")

    def restore(saved):
        raise BackendError("OpenClipboard failed")

    be.restore_clipboard = restore
    kb = Keyboard(be, sleep=lambda s: None)
    with pytest.raises(BackendError):
        kb.type("hello", mode="clipboard")


# ------------------------------------------------ refresh surfaces fatal errors
def test_refresh_with_timeout_raises_fatal_errors_at_once():
    be = MockBackend()
    be.add_window("App").add_button("OK", (10, 10, 50, 20))
    d = locum.Desktop(backend=be)
    el = d.find('button "OK"')

    def boom():
        raise NotSupported("accessibility permission revoked")

    be.accessibility_roots = boom
    t0 = time.monotonic()
    with pytest.raises(NotSupported):
        el.refresh(timeout=2)
    assert time.monotonic() - t0 < 1


# ------------------------------------------------------- typing read-back math
def _ref_prefix(a, b):
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def _ref_suffix(a, b, limit):
    i = 0
    while i < min(len(a), len(b), limit) and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _ref_typed_into(before, after, text):
    if len(after) < len(text):
        return False
    p, s = _ref_prefix(before, after), _ref_suffix(before, after, len(before))
    for a in range(min(p, len(after) - len(text)) + 1):
        k = len(after) - a - len(text)
        if 0 <= k <= s and a + k <= len(before) and after[a:a + len(text)] == text:
            return True
    return False


def _ref_candidates(before, after, text):
    p, s = _ref_prefix(before, after), _ref_suffix(before, after, len(before))
    lo, hi = max(0, len(before) - s), min(p, len(before))
    return {before[:k] + text + before[k:] for k in range(lo, hi + 1)}


def test_fast_prefix_suffix_typed_into_and_candidates_match_the_reference():
    rng = random.Random(7)
    for _ in range(4000):
        alpha = rng.choice(["ab", "a", "abc", "xyz é"])
        before = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 12)))
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 4)))
        k = rng.randint(0, len(before))
        if rng.random() < 0.5:
            after = before[:k] + text + before[k:]
        else:
            after = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 14)))
        assert _common_prefix(before, after) == _ref_prefix(before, after)
        lim = rng.randint(0, 15)
        assert _common_suffix(before, after, lim) == _ref_suffix(before, after, lim)
        assert _typed_into(before, after, text) == _ref_typed_into(before, after, text)
        ref = _ref_candidates(before, after, text)
        got = _repair_candidates(before, after, text)
        assert got <= ref
        assert (len(got), len(ref) > 1) in ((0, False), (1, False), (2, True)) and (len(got) == 0) == (len(ref) == 0)


def test_repair_candidates_are_bounded_when_every_keystroke_was_dropped():
    before = "x" * 50_000 + "y"
    t0 = time.monotonic()
    got = _repair_candidates(before, before, "abc")
    assert len(got) == 2
    assert time.monotonic() - t0 < 1.0  # was quadratic: gigabytes at this size


# ----------------------------------------------------------- placeholder cache
def test_placeholder_is_read_once_per_node():
    be = MockBackend()
    w = be.add_window("App")
    w.add_edit("Search", (10, 10, 100, 20), value="hello")
    d = locum.Desktop(backend=be)
    el = d.find('edit "Search"')
    calls = []
    node = el._node
    orig = node.attributes

    def counting():
        calls.append(1)
        return orig()

    node.attributes = counting
    for _ in range(10):
        assert el.text == "hello"
    assert len(calls) == 1


# ------------------------------------------------------------ input-lock owner
def test_owner_file_sits_next_to_the_per_user_display_lock_file(tmp_path):
    class FakeLock:
        name = "locum-desktop"
        path = str(tmp_path / "locum-desktop-1000-_0.lock")

    assert _inputlock._owner_path(FakeLock()) == str(tmp_path / "locum-desktop-1000-_0.owner")
    _inputlock._write_owner(FakeLock())
    assert "pid" in (tmp_path / "locum-desktop-1000-_0.owner").read_text(encoding="utf-8")
