"""Safety/input defects found in review 12 (2026-11-06), before 0.4.1.

* A bound scroll checked the requested point, not where the cursor really is
  (clicks were fixed in review 8; the scroll path was missed).
* ``fold_text`` kept the combining grapheme joiner and variation selectors:
  invisible marks whose combining class is 0, so "Del<U+034F>ete" was not "Delete".
"""

from __future__ import annotations

import pytest

from locum import Desktop, TargetLost, _target
from locum.backends.mock import MockBackend
from locum.errors import PolicyViolation
from locum.llm.safety import GuardedDesktop, Policy, fold_text


def _desktop(be):
    return Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)


# ============================================================ 1: bound scroll, live cursor
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
    b.add_button("Send", (10, 10, 100, 30))
    return be, _desktop(be)


def test_bound_scroll_checks_the_live_cursor_position():
    be, d = _two_windows()
    be.drift = (520, 20)  # the cursor ends up over the other window after the move
    with _target.bind(_target.for_window(d.window("Target"))):
        with pytest.raises(TargetLost):
            d.mouse.scroll(-3, at=(50, 20))
    assert not [e for e in be.events if e[0] == "scroll"]


def test_bound_scroll_over_the_target_still_scrolls():
    be, d = _two_windows()
    with _target.bind(_target.for_window(d.window("Target"))):
        d.mouse.scroll(-3, at=(50, 20))
    assert [e for e in be.events if e[0] == "scroll"] == [("scroll", 0, -3, 50, 20)]


# ============================================================ 2: invisible marks in fold_text
@pytest.mark.parametrize("mark", ["\u034f", "\ufe0f", "\ufe00", "\U000e0101"])
def test_fold_text_drops_invisible_marks_with_combining_class_zero(mark):
    assert fold_text(f"Del{mark}ete") == "Delete"
    assert fold_text(f"P{mark}ay now") == "Pay now"


def test_fold_text_still_drops_accents_and_keeps_visible_text():
    assert fold_text("\u00dcberweisen") == "Uberweisen"
    assert fold_text("R\u00e9glement & co") == "Reglement & co"


def test_tripwire_sees_through_a_combining_grapheme_joiner():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Bank", (0, 0, 400, 400), app="bank")
    btn = w.add_button("Del\u034fete", (10, 10, 100, 30))
    d = _desktop(be)
    g = GuardedDesktop(d, Policy.default())
    with pytest.raises(PolicyViolation):
        g.click(55, 25)
    assert btn.clicks == 0


# ============================================================ 3: a budget never keeps a button held
@pytest.mark.parametrize("limit", [{"max_actions": 2}, {"max_actions_per_minute": 2}])
@pytest.mark.parametrize("via", ["desktop", "mouse"])
def test_guarded_mouse_up_is_not_refused_by_the_action_budget(limit, via):
    be = MockBackend(width=1280, height=800)
    be.add_window("Doc", (0, 0, 400, 400), app="notepad")
    d = _desktop(be)
    g = GuardedDesktop(d, Policy.default(**limit))
    if via == "desktop":
        g.move(50, 50)
        g.mouse_down()
        g.mouse_up()  # the 3rd action: refused before the fix, the button stayed down
    else:
        g.mouse.move(50, 50)
        g.mouse.down()
        g.mouse.up()
    assert d.mouse.pressed == []
    assert [e[0] for e in be.events if e[0] in ("down", "up")] == ["down", "up"]
    with pytest.raises(PolicyViolation):
        g.move(60, 60)  # the budget still holds for everything else


def test_custom_pattern_with_a_mark_in_a_character_class_still_compiles():
    # Release gate: folding stripped the marks and left "[]", which raised re.error when the
    # Policy was built (0.4.0 accepted the pattern).
    from locum.llm.safety import _compile_folded

    pats = _compile_folded(["हटाए[ंँ]"])
    assert pats[0].search("हटाएं")
