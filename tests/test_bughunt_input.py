"""Regression tests for bug-hunt findings in vision tiling, auto typing fallback and key names."""

from __future__ import annotations

import numpy as np
import pytest

from locum import vision
from locum.backends.base import CAP_UNICODE_TYPING
from locum.backends.mock import MockBackend
from locum.errors import NotSupported
from locum.failsafe import FailSafe, reset_abort
from locum.keyboard import Keyboard
from locum.keys import normalize_key, parse_hotkey


@pytest.fixture(autouse=True)
def _clean_abort():
    reset_abort()
    yield
    reset_abort()


# ----------------------------------------------------------------- vision tiles
def _ref_max_tile_mean(m: np.ndarray, tile: int) -> float:
    """Brute force: exact tile x tile windows, stride tile//2, clipped, / full area."""
    h, w = m.shape
    th, tw = min(tile, h), min(tile, w)
    sy, sx = max(1, th // 2), max(1, tw // 2)
    ys = [0] + list(range(sy, max(1, h - th + sy), sy))
    xs = [0] + list(range(sx, max(1, w - tw + sx), sx))
    best = 0.0
    for y in sorted(set(ys)):
        for x in sorted(set(xs)):
            best = max(best, float(m[y : y + th, x : x + tw].sum()))
    return best / (th * tw)


@pytest.mark.parametrize("tile", [5, 9, 65])
def test_odd_tile_fully_changed_is_one(tile):
    a = np.zeros((200, 200, 3), np.uint8)
    b = np.full((200, 200, 3), 255, np.uint8)
    assert vision.changed_fraction(a, b, tile=tile) == pytest.approx(1.0)
    assert vision.image_diff(a, b, tile=tile) == pytest.approx(1.0)


def test_odd_tile_local_block_measured_over_tile_area():
    a = np.zeros((200, 200, 3), np.uint8)
    c = a.copy()
    c[0:9, 0:9] = 255
    assert vision.changed_fraction(a, c, tile=9) == pytest.approx(1.0)
    c2 = a.copy()
    c2[0:3, 0:3] = 255
    assert vision.changed_fraction(a, c2, tile=9) == pytest.approx(9 / 81)


@pytest.mark.parametrize("shape", [(37, 53), (64, 64), (200, 130), (7, 300)])
@pytest.mark.parametrize("tile", [1, 2, 3, 5, 8, 9, 16, 64, 65, 400])
def test_tile_mean_matches_bruteforce(shape, tile):
    rng = np.random.default_rng(tile * 1000 + shape[0])
    m = (rng.random(shape) < 0.2).astype(np.int32) * rng.integers(1, 765, shape, dtype=np.int32)
    got = vision._max_tile_mean(m, tile)
    assert got == pytest.approx(_ref_max_tile_mean(m, tile))
    assert got <= m.max() + 1e-9


# ------------------------------------------------------------ auto type fallback
class LyingBackend(MockBackend):
    """Advertises unicode typing but cannot do it."""

    def type_text(self, text):
        raise NotSupported("no unicode")


def _setup():
    be = LyingBackend(width=1280, height=800)
    win = be.add_window("Form", (100, 100, 400, 300))
    edit = win.add_edit("Name", (10, 50, 200, 28))
    be._set_focus(edit)
    return be, edit, Keyboard(be, FailSafe(be), sleep=lambda s: None)


def _downs(be, key):
    return sum(1 for e in be.events if e[0] == "key_down" and e[1] == key)


@pytest.mark.parametrize("prefix,key", [("\t", "tab"), ("\n", "enter"), ("\n\t", "tab")])
def test_auto_fallback_with_leading_newline_or_tab(prefix, key):
    be, edit, kb = _setup()
    assert be.has(CAP_UNICODE_TYPING)
    kb.type(prefix + "Hi!")
    assert edit.value.endswith("Hi!")
    # leading keys pressed once each, not repeated by the fallback
    assert _downs(be, key) == prefix.count("\t" if key == "tab" else "\n")


def test_auto_only_keys_on_lying_backend():
    be, edit, kb = _setup()
    kb.type("\t\t")
    assert _downs(be, "tab") == 2


def test_auto_unicode_with_leading_tab_still_uses_unicode():
    be = MockBackend(width=1280, height=800)
    win = be.add_window("Form", (100, 100, 400, 300))
    edit = win.add_edit("Name", (10, 50, 200, 28))
    be._set_focus(edit)
    kb = Keyboard(be, FailSafe(be), sleep=lambda s: None)
    kb.type("\tHé")
    assert edit.value.endswith("Hé")
    assert _downs(be, "tab") == 1


# ------------------------------------------------------------- spaced key names
@pytest.mark.parametrize(
    "name,want",
    [
        ("Browser Back", "browser_back"),
        ("Browser Refresh", "browser_refresh"),
        ("Num Add", "num_add"),
        ("Num Enter", "num_enter"),
        ("Num Decimal", "num_decimal"),
        ("KP Enter", "num_enter"),
        ("num  add", "num_add"),
        ("Page Down", "pagedown"),
        ("Num Lock", "numlock"),
        ("Print Screen", "printscreen"),
    ],
)
def test_spaced_multiword_key_names(name, want):
    assert normalize_key(name) == want
    assert parse_hotkey(name) == [want]


def test_spaced_names_in_combo_and_unknown_still_rejected():
    assert parse_hotkey("ctrl+Num Add") == ["ctrl", "num_add"]
    with pytest.raises(ValueError):
        normalize_key("Browser Teleport")
