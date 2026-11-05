"""Tests for the macOS backend.

The real backend needs macOS + pyobjc, so on other platforms we test:
* that the module imports and the constructor raises BackendUnavailable;
* the pure helpers (keycodes, AX roles, point/pixel layout, click state, UTF-16 chunking);
* the backend logic itself, wired to fake Quartz / AppKit / AX modules via
  ``MacOSBackend._from_parts`` (verifies event types, flags, click counts, conversions).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from locum.backends import create_backend
from locum.backends import macos
from locum.backends.base import CAP_ACCESSIBILITY, CAP_CLIPBOARD, CAP_UNICODE_TYPING
from locum.backends.macos import (
    AXNode,
    ClickTracker,
    DisplayInfo,
    DisplayLayout,
    MacOSBackend,
    capture_plan,
    chunk_utf16,
    map_ax_role,
    match_window_record,
    resolve_key,
    split_typing_segments,
    utf16_len,
)
from locum.errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound
from locum.keys import KEYS, MODIFIERS
from locum.types import MouseButton, Point, Rect


# =============================================================================
# import / availability
# =============================================================================


@pytest.mark.skipif(sys.platform == "darwin", reason="checks the non-macOS behaviour")
def test_constructor_unavailable_off_macos():
    with pytest.raises(BackendUnavailable):
        MacOSBackend()
    with pytest.raises(BackendUnavailable):
        create_backend("macos")


def test_constructor_reports_missing_pyobjc(monkeypatch):
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", None)  # makes "import Quartz" raise ImportError
    with pytest.raises(BackendUnavailable, match="pip install pyobjc"):
        MacOSBackend()


# =============================================================================
# keyboard mapping
# =============================================================================


def test_every_canonical_key_is_mapped():
    for key in sorted(KEYS):
        if key in macos.UNSUPPORTED_KEYS:
            with pytest.raises(NotSupported):
                resolve_key(key)
            continue
        spec = resolve_key(key)
        assert spec.kind in ("key", "modifier", "media", "unicode"), key
        assert isinstance(spec.keycode, int) and 0 <= spec.keycode <= 0x7F, key
        if key in MODIFIERS:
            assert spec.kind == "modifier" and spec.flags, key
    assert macos.UNSUPPORTED_KEYS == {"f21", "f22", "f23", "f24"}


def test_keycodes_are_correct_for_well_known_keys():
    assert resolve_key("a").keycode == 0x00
    assert resolve_key("enter").keycode == 0x24
    assert resolve_key("escape").keycode == 0x35
    assert resolve_key("backspace").keycode == 0x33
    assert resolve_key("delete").keycode == 0x75
    assert resolve_key("cmd").keycode == 0x37
    assert resolve_key("f1").keycode == 0x7A
    assert resolve_key("f20").keycode == 0x5A
    assert resolve_key("num5").keycode == 0x57
    assert resolve_key("left").keycode == 0x7B


def test_letter_and_digit_keycodes_are_unique():
    codes = [resolve_key(c).keycode for c in "abcdefghijklmnopqrstuvwxyz0123456789"]
    assert len(set(codes)) == len(codes)


def test_modifier_flags():
    assert resolve_key("cmd").flags & macos.FLAG_COMMAND
    assert resolve_key("cmd_r").flags & macos.FLAG_COMMAND
    assert resolve_key("shift").flags & macos.FLAG_SHIFT
    assert resolve_key("ctrl").flags & macos.FLAG_CONTROL
    assert resolve_key("alt").flags & macos.FLAG_ALTERNATE
    assert resolve_key("altgr").flags & macos.FLAG_ALTERNATE
    assert resolve_key("fn").flags == macos.FLAG_SECONDARY_FN
    # left/right are distinguishable through device-dependent bits
    assert resolve_key("shift").flags != resolve_key("shift_r").flags


def test_shifted_and_unicode_characters():
    a, big_a = resolve_key("a"), resolve_key("A")
    assert big_a.keycode == a.keycode and big_a.flags & macos.FLAG_SHIFT and not a.flags
    bang = resolve_key("!")
    assert bang.keycode == resolve_key("1").keycode and bang.flags & macos.FLAG_SHIFT
    assert resolve_key("?").keycode == resolve_key("/").keycode
    e = resolve_key("é")
    assert e.kind == "unicode" and e.char == "é"
    assert resolve_key("\n").keycode == 0x24
    assert resolve_key(" ").keycode == 0x31


def test_special_key_kinds_and_flags():
    assert resolve_key("volumeup").kind == "media"
    assert resolve_key("playpause").keycode == 16
    back = resolve_key("browser_back")
    assert back.kind == "key" and back.flags & macos.FLAG_COMMAND and back.keycode == resolve_key("[").keycode
    assert resolve_key("up").flags & macos.FLAG_SECONDARY_FN
    assert resolve_key("up").flags & macos.FLAG_NUMERIC_PAD
    assert resolve_key("num1").flags & macos.FLAG_NUMERIC_PAD
    assert resolve_key("home").flags & macos.FLAG_SECONDARY_FN


def test_resolve_key_aliases_and_errors():
    assert resolve_key("Return").keycode == 0x24
    with pytest.raises(ValueError):
        resolve_key("definitelynotakey")
    with pytest.raises(ValueError):
        resolve_key("")


# =============================================================================
# UTF-16 chunking / segments
# =============================================================================


def test_utf16_len():
    assert utf16_len("abc") == 3
    assert utf16_len("😀") == 2
    assert utf16_len("a😀b") == 4


def test_chunk_utf16_basic_and_limits():
    text = "x" * 45
    chunks = chunk_utf16(text, 20)
    assert [len(c) for c in chunks] == [20, 20, 5]
    assert "".join(chunks) == text
    assert chunk_utf16("") == []
    with pytest.raises(ValueError):
        chunk_utf16("abc", 1)


def test_chunk_utf16_never_splits_surrogate_pairs():
    text = "a" * 19 + "😀" + "b" * 3 + "😀" * 12
    chunks = chunk_utf16(text, 20)
    assert "".join(chunks) == text
    for c in chunks:
        assert utf16_len(c) <= 20
    assert chunks[0] == "a" * 19  # emoji did not fit -> pushed to next chunk
    assert chunks[1].startswith("😀")


def test_split_typing_segments():
    segs = split_typing_segments("ab\r\ncd\tx\ry")
    assert segs == [("text", "ab"), ("key", "enter"), ("text", "cd"), ("key", "tab"),
                    ("text", "x"), ("key", "enter"), ("text", "y")]
    long = split_typing_segments("z" * 25)
    assert long == [("text", "z" * 20), ("text", "z" * 5)]
    assert split_typing_segments("\n\n") == [("key", "enter"), ("key", "enter")]


# =============================================================================
# click tracking
# =============================================================================


def test_click_tracker_counts_multi_clicks():
    t = ClickTracker(interval=0.5, max_distance=4)
    assert t.press("left", 10, 10, now=0.0) == 1
    assert t.release("left") == 1
    assert t.press("left", 11, 10, now=0.2) == 2
    assert t.release("left") == 2
    assert t.press("left", 12, 11, now=0.4) == 3
    assert t.release("left") == 3
    # too slow -> restart
    assert t.press("left", 12, 11, now=1.5) == 1
    # moved too far -> restart
    assert t.press("left", 40, 40, now=1.6) == 1
    # different button -> restart
    assert t.press("right", 40, 40, now=1.7) == 1
    assert t.press("left", 40, 40, now=1.8) == 1
    t.reset()
    assert t.press("left", 40, 40, now=1.9) == 1


def test_click_tracker_release_without_press_and_clock():
    now = [0.0]
    t = ClickTracker(clock=lambda: now[0])
    assert t.release("left") == 1
    assert t.press("left", 0, 0) == 1
    now[0] = 0.3
    assert t.press("left", 0, 0) == 2
    now[0] = 0.2  # clock going backwards never continues a sequence
    assert t.press("left", 0, 0) == 1


# =============================================================================
# display layout: points <-> pixels
# =============================================================================


def _d(did, x, y, w, h, scale, primary=False, **kw):
    return DisplayInfo(did, x, y, w, h, scale=scale, is_primary=primary, **kw)


def test_single_retina_display():
    lay = DisplayLayout([_d(1, 0, 0, 1440, 900, 2.0, True)])
    assert lay.virtual_pixel_rect == Rect(0, 0, 2880, 1800)
    assert lay.point_to_pixel(100, 50) == Point(200, 100)
    assert lay.pixel_to_point(201, 101) == (100.5, 50.5)
    mons = lay.monitors()
    assert len(mons) == 1 and mons[0].scale == 2.0 and mons[0].is_primary
    assert lay.points_rect_to_pixels(10, 20, 30, 40) == Rect(20, 40, 60, 80)
    assert lay.pixels_rect_to_points(Rect(20, 40, 60, 80)) == (10, 20, 30, 40)


def test_uniform_scale_is_points_times_scale():
    lay = DisplayLayout([
        _d(2, 1440, -200, 1920, 1080, 2.0),
        _d(1, 0, 0, 1440, 900, 2.0, True),
        _d(3, -1280, 100, 1280, 800, 2.0),
        _d(4, 0, 900, 1440, 900, 2.0),
    ])
    assert lay.displays[0].display_id == 1  # primary first
    for d in lay.displays:
        assert d.pixel_rect == Rect(int(d.x * 2), int(d.y * 2), int(d.width * 2), int(d.height * 2))
    for pt in [(5, 5), (1500, -100), (-1000, 500), (700, 1200)]:
        px = lay.point_to_pixel(*pt)
        assert px == Point(pt[0] * 2, pt[1] * 2)
        assert lay.pixel_to_point(*px) == pt


def test_mixed_scales_keep_edges_and_native_resolution():
    # 2x laptop primary, 1x monitor to its right (offset 100pt down), 1x monitor on the left,
    # 2x display above.
    primary = _d(1, 0, 0, 1440, 900, 2.0, True)
    right = _d(2, 1440, 100, 1920, 1080, 1.0)
    left = _d(3, -1024, 0, 1024, 768, 1.0)
    above = _d(4, 200, -600, 1000, 600, 2.0)
    lay = DisplayLayout([right, left, above, primary])
    by_id = {d.display_id: d for d in lay.displays}
    assert by_id[1].pixel_rect == Rect(0, 0, 2880, 1800)
    assert by_id[2].pixel_rect == Rect(2880, 200, 1920, 1080)  # starts at primary's right pixel edge
    assert by_id[3].pixel_rect == Rect(-1024, 0, 1024, 768)  # ends at primary's left pixel edge
    assert by_id[4].pixel_rect == Rect(400, -1200, 2000, 1200)
    # no two displays overlap in pixel space
    ds = lay.displays
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            assert ds[i].pixel_rect.intersect(ds[j].pixel_rect) is None
    # conversions go through the containing display
    assert lay.point_to_pixel(1440, 100) == Point(2880, 200)
    assert lay.point_to_pixel(1500, 150) == Point(2940, 250)
    assert lay.point_to_pixel(1439, 0) == Point(2878, 0)
    assert lay.point_to_pixel(-1, 10) == Point(-1, 10)
    assert lay.point_to_pixel(300, -1) == Point(600, -2)
    # round trip for many points on every display
    for d in ds:
        for fx in (0.0, 0.25, 0.5, 0.99):
            for fy in (0.0, 0.5, 0.99):
                x, y = round(d.x + d.width * fx), round(d.y + d.height * fy)
                px = lay.point_to_pixel(x, y)
                assert lay.display_at_pixels(*px).display_id == d.display_id
                bx, by = lay.pixel_to_point(*px)
                assert abs(bx - x) < 1e-9 and abs(by - y) < 1e-9
    assert lay.virtual_pixel_rect == Rect.from_ltrb(-1024, -1200, 2880 + 1920, 1800)
    # monitors: index 0 is the primary
    mons = lay.monitors()
    assert mons[0].is_primary and mons[0].rect == Rect(0, 0, 2880, 1800)
    assert sum(m.is_primary for m in mons) == 1


def test_rects_use_the_display_containing_their_center():
    lay = DisplayLayout([_d(1, 0, 0, 1440, 900, 2.0, True), _d(2, 1440, 0, 1920, 1080, 1.0)])
    # window mostly on the 1x display
    assert lay.points_rect_to_pixels(1400, 10, 400, 300) == Rect(2840, 10, 400, 300)
    # window on the retina display
    assert lay.points_rect_to_pixels(100, 10, 400, 300) == Rect(200, 20, 800, 600)
    x, y, w, h = lay.pixels_rect_to_points(Rect(2840, 10, 400, 300))
    assert (x, y, w, h) == (1400, 10, 400, 300)


def test_points_outside_every_display_use_nearest():
    lay = DisplayLayout([_d(1, 0, 0, 1440, 900, 2.0, True), _d(2, 1440, 0, 1920, 1080, 1.0)])
    assert lay.display_at_points(5000, 10).display_id == 2
    assert lay.display_at_points(-50, 10).display_id == 1
    assert lay.display_at_pixels(-50, 10).display_id == 1
    assert lay.point_to_pixel(-10, 0) == Point(-20, 0)


def test_disconnected_display_is_placed_relative_to_nearest():
    lay = DisplayLayout([_d(1, 0, 0, 1000, 800, 2.0, True), _d(2, 1100, 0, 800, 600, 1.0)])
    d2 = lay.displays[1]
    assert (d2.pixel_left, d2.pixel_top) == (2200, 0)
    assert lay.pixel_to_point(2200, 0) == (1100, 0)


def test_primary_inferred_and_empty_layout():
    lay = DisplayLayout([_d(2, 1000, 0, 500, 500, 1.0), _d(1, 0, 0, 1000, 800, 1.0)])
    assert lay.primary.display_id == 1
    with pytest.raises(BackendError):
        DisplayLayout([])


def _assert_layout_consistent(lay):
    ds = lay.displays
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            assert ds[i].pixel_rect.intersect(ds[j].pixel_rect) is None, (ds[i], ds[j])
    for d in ds:
        for fx in (0.0, 0.3, 0.7):
            for fy in (0.0, 0.5):
                x, y = round(d.x + d.width * fx), round(d.y + d.height * fy)
                px = lay.point_to_pixel(x, y)
                assert lay.display_at_pixels(*px).display_id == d.display_id
                bx, by = lay.pixel_to_point(*px)
                tol = 0.5 / d.scale + 1e-9  # point_to_pixel rounds to whole pixels
                assert abs(bx - x) <= tol and abs(by - y) <= tol
        # last point on the display
        x, y = d.x + d.width - 1, d.y + d.height - 1
        assert lay.display_at_pixels(*lay.point_to_pixel(x, y)).display_id == d.display_id


def test_mixed_scale_chain_does_not_overlap_primary():
    # Retina laptop, 1x monitor below, 1x monitor to the right of that, 1.5x monitor
    # above the third: naive edge placement put display 4 on top of the primary.
    lay = DisplayLayout([_d(1, 0, 0, 1440, 900, 2.0, True), _d(2, 254, 900, 2560, 1440, 1.0),
                         _d(3, 2814, 116, 1512, 982, 1.0), _d(4, 1548, -964, 1920, 1080, 1.5)])
    _assert_layout_consistent(lay)
    px = lay.point_to_pixel(2000, 100)
    assert lay.display_at_pixels(*px).display_id == 4
    assert lay.pixel_to_point(*px) == (2000, 100)
    by_id = {d.display_id: d for d in lay.displays}
    # display 4 still touches display 3 (its edge neighbour) in pixel space
    r3, r4 = by_id[3].pixel_rect, by_id[4].pixel_rect
    assert r4.bottom == r3.top and min(r3.right, r4.right) > max(r3.left, r4.left)


def test_random_mixed_scale_layouts_are_disjoint_and_round_trip():
    import random

    rng = random.Random(1234)
    sizes = [(1920, 1080), (2560, 1440), (1280, 800), (1512, 982), (1000, 700)]
    for _ in range(600):
        rects = [(0, 0, 1440, 900)]
        ds = [_d(1, 0, 0, 1440, 900, 2.0, True)]
        for k in range(rng.randint(1, 4)):
            w, h = rng.choice(sizes)
            s = rng.choice([1.0, 1.5, 2.0, 3.0])
            for _attempt in range(50):
                bx, by, bw, bh = rng.choice(rects)
                side = rng.choice("LRTB")
                if side == "R":
                    x, y = bx + bw, by + rng.randint(-h + 1, bh - 1)
                elif side == "L":
                    x, y = bx - w, by + rng.randint(-h + 1, bh - 1)
                elif side == "B":
                    x, y = bx + rng.randint(-w + 1, bw - 1), by + bh
                else:
                    x, y = bx + rng.randint(-w + 1, bw - 1), by - h
                if all(Rect(x, y, w, h).intersect(Rect(*r)) is None for r in rects):
                    rects.append((x, y, w, h))
                    ds.append(_d(k + 2, x, y, w, h, s))
                    break
        rng.shuffle(ds)
        _assert_layout_consistent(DisplayLayout(ds))


def test_disconnected_display_overlap_is_pushed_out():
    # a 1x display far right of a 3x primary would land inside the primary's pixels
    lay = DisplayLayout([_d(1, 0, 0, 1000, 800, 3.0, True), _d(2, 1500, 0, 800, 600, 1.0)])
    _assert_layout_consistent(lay)
    assert lay.displays[1].pixel_left >= 3000


def test_capture_plan_retina_fractional_region():
    d = _d(1, 0, 0, 1440, 900, 2.0, True)
    pts, expected, crop = capture_plan(d, Rect(101, 51, 10, 10))
    assert pts == (50, 25, 6, 6)  # enclosing points rect
    assert expected == (12, 12)
    assert crop == (1, 1, 11, 11)


def test_capture_plan_offset_display_and_clamping():
    lay = DisplayLayout([_d(1, 0, 0, 1440, 900, 2.0, True), _d(2, 1440, 100, 1920, 1080, 1.0)])
    d2 = lay.displays[1]
    region = Rect(2880 + 10, 200 + 20, 30, 40)
    pts, expected, crop = capture_plan(d2, region)
    assert pts == (1450, 120, 30, 40)
    assert expected == (30, 40)
    assert crop == (0, 0, 30, 40)
    d1 = lay.displays[0]
    pts, expected, crop = capture_plan(d1, Rect(2870, 1790, 10, 10))
    assert pts == (1435, 895, 5, 5) and expected == (10, 10) and crop == (0, 0, 10, 10)


# =============================================================================
# AX role mapping
# =============================================================================


def test_map_ax_role():
    assert map_ax_role("AXButton") == "button"
    assert map_ax_role("AXTextField") == "edit"
    assert map_ax_role("AXTextField", "AXSecureTextField") == "edit"
    assert map_ax_role("AXTextArea") == "edit"
    assert map_ax_role("AXStaticText") == "text"
    assert map_ax_role("AXWindow", "AXStandardWindow") == "window"
    assert map_ax_role("AXWindow", "AXDialog") == "dialog"
    assert map_ax_role("AXSheet") == "dialog"
    assert map_ax_role("AXCheckBox") == "checkbox"
    assert map_ax_role("AXCheckBox", "AXToggle") == "togglebutton"
    assert map_ax_role("AXCheckBox", "AXSwitch") == "checkbox"
    assert map_ax_role("AXRadioButton", "AXTabButton") == "tab"
    assert map_ax_role("AXRow", "AXOutlineRow") == "treeitem"
    assert map_ax_role("AXRow", "AXTableRow") == "row"
    assert map_ax_role("AXPopUpButton") == "combobox"
    assert map_ax_role("AXMenuItem") == "menuitem"
    assert map_ax_role("AXMenuBarItem") == "menuitem"
    assert map_ax_role("AXTabGroup") == "tablist"
    assert map_ax_role("AXWebArea") == "document"
    assert map_ax_role("AXOutline") == "tree"
    assert map_ax_role("AXScrollArea") == "pane"
    assert map_ax_role("AXApplication") == "application"
    assert map_ax_role("AXFancyNewThing") == "fancynewthing"
    assert map_ax_role("") == "unknown"
    assert map_ax_role(None) == "unknown"
    # dialog subrole on a non-window element does not turn it into a dialog
    assert map_ax_role("AXButton", "AXDialog") == "button"


def test_all_mapped_roles_are_portable():
    from locum.backends.base import ROLES

    for value in list(macos._AX_ROLE_MAP.values()) + list(macos._AX_SUBROLE_MAP.values()):
        assert value in ROLES, value


# =============================================================================
# window record matching
# =============================================================================


def _rec(num, pid, x, y, w, h, title="", layer=0, owner="App"):
    return {"kCGWindowNumber": num, "kCGWindowOwnerPID": pid, "kCGWindowLayer": layer,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
            "kCGWindowName": title, "kCGWindowOwnerName": owner, "kCGWindowAlpha": 1.0}


def test_match_window_record():
    recs = [
        _rec(10, 1, 0, 0, 500, 400, "Doc A"),
        _rec(11, 1, 0, 0, 500, 400, "Doc B"),
        _rec(12, 1, 600, 0, 300, 300, "Other"),
        _rec(13, 2, 600, 0, 300, 300, "Foreign"),
        _rec(14, 1, 0, 0, 1440, 25, "Menu", layer=24),
    ]
    assert match_window_record(recs, 1, (600, 0, 300, 300)) == 12
    assert match_window_record(recs, 1, (601, 1, 299, 300)) == 12  # tolerance
    assert match_window_record(recs, 1, (0, 0, 500, 400), "Doc B") == 11
    assert match_window_record(recs, 1, (9, 9, 9, 9), "Doc A") == 10
    assert match_window_record(recs, 2, (9, 9, 9, 9)) == 13  # the pid's only window
    assert match_window_record(recs, 1, (9, 9, 9, 9)) is None
    assert match_window_record(recs, 3, (0, 0, 500, 400)) is None


# =============================================================================
# backend logic against fake native modules
# =============================================================================


class FakeEvent:
    def __init__(self, kind: str, **kw: Any):
        self.kind = kind
        self.fields: Dict[int, int] = {}
        self.flags: Optional[int] = None
        self.text: Optional[str] = None
        self.text_len: Optional[int] = None
        self.__dict__.update(kw)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeEvent {self.__dict__}>"


class FakeQuartz:
    def __init__(self) -> None:
        self.posted: List[FakeEvent] = []
        self.pos = (0.0, 0.0)
        self.windows: List[Dict[str, Any]] = []
        self.screen_capture_ok = True

    def CGEventSourceCreate(self, state):
        return "source"

    def CGEventCreateMouseEvent(self, src, etype, pt, button):
        return FakeEvent("mouse", etype=etype, pt=tuple(pt), button=button)

    def CGEventSetIntegerValueField(self, ev, field, value):
        ev.fields[field] = value

    def CGEventSetFlags(self, ev, flags):
        ev.flags = flags

    def CGEventSetType(self, ev, etype):
        ev.etype = etype

    def CGEventPost(self, tap, ev):
        assert tap == macos.kCGHIDEventTap
        self.posted.append(ev)
        if ev.kind == "mouse":
            self.pos = ev.pt

    def CGEventCreate(self, src):
        return None

    def CGEventGetLocation(self, ev):
        return SimpleNamespace(x=self.pos[0], y=self.pos[1])

    def CGEventCreateScrollWheelEvent(self, src, units, count, w1, w2):
        return FakeEvent("scroll", etype=macos.kCGEventScrollWheel, units=units, count=count, w1=w1, w2=w2)

    def CGEventCreateKeyboardEvent(self, src, code, down):
        return FakeEvent("key", etype=macos.kCGEventKeyDown if down else macos.kCGEventKeyUp,
                         code=code, down=down)

    def CGEventKeyboardSetUnicodeString(self, ev, n, text):
        ev.text, ev.text_len = text, n

    def CGWindowListCopyWindowInfo(self, options, relative):
        if options == macos.kCGWindowListOptionIncludingWindow:
            return [w for w in self.windows if w["kCGWindowNumber"] == relative]
        return list(self.windows)

    def CGPreflightScreenCaptureAccess(self):
        return self.screen_capture_ok


class FakePasteboard:
    def __init__(self) -> None:
        self.value: Optional[str] = None

    def stringForType_(self, t):
        return self.value

    def clearContents(self):
        self.value = None

    def setString_forType_(self, s, t):
        self.value = s
        return True


class FakeRunningApp:
    def __init__(self, pid, name):
        self.pid, self.name, self.activated = pid, name, 0

    def processIdentifier(self):
        return self.pid

    def localizedName(self):
        return self.name

    def activationPolicy(self):
        return 0

    def isTerminated(self):
        return False

    def activateWithOptions_(self, opts):
        self.activated += 1
        return True


class FakeAppKit:
    NSPasteboardTypeString = "public.utf8-plain-text"

    def __init__(self, apps):
        self.apps = {a.pid: a for a in apps}
        self.front = apps[0] if apps else None
        pb = FakePasteboard()
        self.NSPasteboard = SimpleNamespace(generalPasteboard=lambda: pb)
        self.pasteboard = pb
        ws = SimpleNamespace(frontmostApplication=lambda: self.front,
                             runningApplications=lambda: list(self.apps.values()))
        self.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: ws)
        self.NSRunningApplication = SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: self.apps.get(pid))


class FakeEl:
    def __init__(self, pid=100, actions=(), settable=(), **attrs):
        self.pid = pid
        self.attrs = dict(attrs)
        self.actions = list(actions)
        self.settable = set(settable)
        self.performed: List[str] = []


class FakeAX:
    def __init__(self) -> None:
        self.trusted = True
        self.system_wide = FakeEl()
        self.apps: Dict[int, FakeEl] = {}
        self.at: Optional[FakeEl] = None
        self.last_at = None

    def is_trusted(self):
        return self.trusted

    def prompt_trust(self):
        return self.trusted

    def application(self, pid):
        return self.apps[pid]

    def element_at(self, x, y):
        self.last_at = (x, y)
        return self.at

    def pid(self, el):
        return el.pid

    def same(self, a, b):
        return a is b

    def attr(self, el, name, default=None):
        return el.attrs.get(name, default)

    def attrs(self, el, names):
        return {n: el.attrs[n] for n in names if n in el.attrs}

    def attr_names(self, el):
        return list(el.attrs)

    def settable(self, el, name):
        return name in el.settable

    def set_attr(self, el, name, value):
        if name in el.settable:
            el.attrs[name] = value
            return True
        return False

    def set_point(self, el, name, x, y):
        return self.set_attr(el, name, (x, y))

    def set_size(self, el, name, w, h):
        return self.set_attr(el, name, (w, h))

    def action_names(self, el):
        return list(el.actions)

    def perform(self, el, action):
        if action in el.actions:
            el.performed.append(action)
            return True
        return False


def make_backend(ax=None, apps=None, layout=None, clock=None):
    q = FakeQuartz()
    appkit = FakeAppKit(apps if apps is not None else [FakeRunningApp(100, "TextEdit")])
    if layout is None:
        layout = DisplayLayout([
            _d(1, 0, 0, 1440, 900, 2.0, True, work_area=(0, 25, 1440, 875)),
            _d(2, 1440, 0, 1920, 1080, 1.0, work_area=(1440, 0, 1920, 1080)),
        ])
    kw = {}
    if clock is not None:
        kw["clock"] = clock
    be = MacOSBackend._from_parts(q, appkit, ax, layout=layout, **kw)
    return be, q, appkit


def test_from_parts_capabilities():
    be, _, _ = make_backend(ax=FakeAX())
    assert CAP_UNICODE_TYPING in be.capabilities
    assert CAP_CLIPBOARD in be.capabilities
    assert CAP_ACCESSIBILITY in be.capabilities
    be2 = MacOSBackend._from_parts(FakeQuartz(), None, None, layout=DisplayLayout([_d(1, 0, 0, 10, 10, 1.0)]))
    assert CAP_ACCESSIBILITY not in be2.capabilities and CAP_CLIPBOARD not in be2.capabilities
    assert be.monitors()[1].rect == Rect(2880, 0, 1920, 1080)
    assert be.virtual_screen() == Rect(0, 0, 4800, 1800)


def test_mouse_move_converts_pixels_to_points():
    be, q, _ = make_backend()
    be.mouse_move(200, 100)
    ev = q.posted[-1]
    assert ev.etype == macos.kCGEventMouseMoved and ev.pt == (100.0, 50.0)
    assert be.mouse_position() == Point(200, 100)
    be.mouse_move(2880 + 100, 30)  # on the 1x display
    assert q.posted[-1].pt == (1540.0, 30.0)
    assert be.mouse_position() == Point(2980, 30)
    assert be.to_points(2980, 30) == (1540.0, 30.0)
    assert be.to_pixels(1540, 30) == Point(2980, 30)


def test_drag_uses_dragged_events_and_buttons():
    be, q, _ = make_backend()
    be.mouse_move(10, 10)
    be.mouse_down(MouseButton.LEFT)
    assert q.posted[-1].etype == macos.kCGEventLeftMouseDown
    be.mouse_move(50, 50)
    assert q.posted[-1].etype == macos.kCGEventLeftMouseDragged
    assert q.posted[-1].pt == (25.0, 25.0)
    assert q.posted[-1].fields[macos.kCGMouseEventDeltaX] == 20
    be.mouse_up(MouseButton.LEFT)
    assert q.posted[-1].etype == macos.kCGEventLeftMouseUp
    be.mouse_move(60, 60)
    assert q.posted[-1].etype == macos.kCGEventMouseMoved
    be.mouse_down("right")
    be.mouse_move(70, 70)
    assert q.posted[-1].etype == macos.kCGEventRightMouseDragged
    be.mouse_up("right")
    be.mouse_down("middle")
    ev = q.posted[-1]
    assert ev.etype == macos.kCGEventOtherMouseDown and ev.fields[macos.kCGMouseEventButtonNumber] == 2
    be.mouse_move(80, 80)
    assert q.posted[-1].etype == macos.kCGEventOtherMouseDragged
    be.mouse_up("middle")
    be.mouse_down("x2")
    assert q.posted[-1].fields[macos.kCGMouseEventButtonNumber] == 4
    be.mouse_up("x2")
    assert be._held_buttons == []


def test_click_state_written_into_events():
    now = [0.0]
    be, q, _ = make_backend(clock=lambda: now[0])
    be.mouse_move(100, 100)
    states = []
    for i in range(3):
        now[0] = i * 0.1
        be.mouse_down()
        states.append(q.posted[-1].fields[macos.kCGMouseEventClickState])
        be.mouse_up()
        states.append(q.posted[-1].fields[macos.kCGMouseEventClickState])
    assert states == [1, 1, 2, 2, 3, 3]
    now[0] = 5.0
    be.mouse_down()
    assert q.posted[-1].fields[macos.kCGMouseEventClickState] == 1
    be.mouse_up()


def test_scroll_line_units_and_directions():
    be, q, _ = make_backend()
    be.scroll(dy=3)
    evs = [e for e in q.posted if e.kind == "scroll"]
    assert len(evs) == 3 and all(e.w1 == 1 and e.w2 == 0 for e in evs)
    assert all(e.units == macos.kCGScrollEventUnitLine for e in evs)
    q.posted.clear()
    be.scroll(dy=-1, dx=2)
    evs = [(e.w1, e.w2) for e in q.posted]
    assert evs == [(-1, 0), (0, -1), (0, -1)]  # dx>0 = right = negative wheel2
    q.posted.clear()
    be.scroll(dx=-1)
    assert [(e.w1, e.w2) for e in q.posted] == [(0, 1)]
    q.posted.clear()
    be.scroll()
    assert q.posted == []


def test_hotkey_flags_and_modifier_events():
    be, q, _ = make_backend()
    be.key_down("cmd")
    ev = q.posted[-1]
    assert ev.etype == macos.kCGEventFlagsChanged and ev.code == 0x37 and ev.flags & macos.FLAG_COMMAND
    be.key_down("shift")
    be.key_down("s")
    ev = q.posted[-1]
    assert ev.etype == macos.kCGEventKeyDown and ev.code == 0x01
    assert ev.flags & macos.FLAG_COMMAND and ev.flags & macos.FLAG_SHIFT
    be.key_up("s")
    assert q.posted[-1].etype == macos.kCGEventKeyUp and q.posted[-1].flags & macos.FLAG_COMMAND
    be.key_up("shift")
    ev = q.posted[-1]
    assert ev.etype == macos.kCGEventFlagsChanged and not ev.flags & macos.FLAG_SHIFT
    assert ev.flags & macos.FLAG_COMMAND
    # mouse events carry held modifiers too (cmd+click)
    be.mouse_down()
    assert q.posted[-1].flags & macos.FLAG_COMMAND
    be.mouse_up()
    be.key_up("cmd")
    assert q.posted[-1].flags == 0
    be.key_down("a")
    assert q.posted[-1].flags == 0


def test_shifted_char_and_unicode_key():
    be, q, _ = make_backend()
    be.key_down("A")
    assert q.posted[-1].code == 0x00 and q.posted[-1].flags & macos.FLAG_SHIFT
    be.key_up("A")
    assert q.posted[-1].flags & macos.FLAG_SHIFT
    be.key_down("ü")
    assert q.posted[-1].text == "ü" and q.posted[-1].text_len == 1
    be.key_up("ü")
    with pytest.raises(NotSupported):
        be.key_down("f24")


def test_type_text_chunks_and_newlines():
    be, q, _ = make_backend()
    be.key_down("cmd")  # held modifiers must not leak into typed text
    q.posted.clear()
    be.type_text("héllo 😀" * 4 + "\nok")
    texts = [e.text for e in q.posted if e.down and e.text]
    assert "".join(texts) == "héllo 😀" * 4 + "ok"
    assert all(utf16_len(t) <= 20 for t in texts)
    assert all(e.flags == 0 for e in q.posted)
    enters = [e for e in q.posted if e.code == 0x24]
    assert len(enters) == 2 and enters[0].down and not enters[1].down
    for e in q.posted:
        if e.text:
            assert e.text_len == utf16_len(e.text)


# --- keyboard layout awareness ------------------------------------------------

# French AZERTY (subset): physical ANSI positions -> characters
_AZERTY = {
    0x0C: ("a", "A"), 0x0D: ("z", "Z"), 0x00: ("q", "Q"), 0x06: ("w", "W"),
    0x01: ("s", "S"), 0x10: ("y", "Y"), 0x2E: (",", "?"), 0x12: ("&", "1"),
    0x13: ("é", "2"), 0x31: (" ", " "), 0x21: (None, None),  # dead key (^ / ¨)
    0x53: ("1", "1"),  # keypad 1 also types "1": the main row must win
}
_RUSSIAN = {0x00: ("ф", "Ф"), 0x0C: ("й", "Й"), 0x06: ("я", "Я")}
_RUSSIAN_CMD = {0x00: "a", 0x0C: "q", 0x06: "z"}


def _translator(table, cmd_table=None):
    def translate(code, state):
        if state == macos.UC_MOD_CMD:
            if cmd_table is not None:
                return cmd_table.get(code)
            return table.get(code, (None, None))[0]
        pair = table.get(code)
        if pair is None:
            return None
        return pair[1] if state == macos.UC_MOD_SHIFT else pair[0]
    return translate


class FakeLayouts:
    def __init__(self, table, cmd_table=None):
        self.set(table, cmd_table)
        self.calls = 0

    def set(self, table, cmd_table=None):
        self.fp = repr((sorted(table.items()), cmd_table)).encode()
        self.map = macos.KeyboardLayoutMap.from_translator(_translator(table, cmd_table))

    def current(self, known):
        self.calls += 1
        if known == self.fp:
            return self.fp, None
        return self.fp, self.map


def test_keyboard_layout_map_from_translator():
    km = macos.KeyboardLayoutMap.from_translator(_translator(_AZERTY))
    assert km.lookup("a") == (0x0C, False)
    assert km.lookup("A") == (0x0C, True)
    assert km.lookup("1") == (0x12, True)  # shifted main-row key beats the keypad
    assert km.lookup("&") == (0x12, False)
    assert km.lookup("é") == (0x13, False)
    assert km.lookup("^") is None and km.lookup("[") is None
    us = macos.KeyboardLayoutMap.us_ansi()
    assert us.lookup("a") == (0x00, False) and us.lookup("A") == (0x00, True)
    assert us.lookup("!") == (0x12, True)


def test_resolve_key_uses_layout():
    km = macos.KeyboardLayoutMap.from_translator(_translator(_AZERTY))
    assert resolve_key("a", km).keycode == 0x0C
    assert resolve_key("z", km).keycode == 0x0D
    spec = resolve_key("Q", km)
    assert spec.keycode == 0x00 and spec.flags & macos.FLAG_SHIFT
    assert resolve_key("ß", km).kind == "unicode"
    assert resolve_key("enter", km).keycode == 0x24
    # shortcut char not typeable on the layout -> US position fallback
    assert resolve_key("browser_back", km).keycode == 0x21
    # without a map: the static ANSI table (unchanged behaviour)
    assert resolve_key("a").keycode == 0x00


def test_azerty_hotkeys_use_layout_keycodes():
    layouts = FakeLayouts(_AZERTY)
    q = FakeQuartz()
    be = MacOSBackend._from_parts(q, FakeAppKit([FakeRunningApp(100, "TextEdit")]), None,
                                  layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                  keyboard_layouts=layouts)
    # cmd+a (select all) must not become cmd+q (quit), cmd+z (undo) not cmd+w (close)
    be.key_down("cmd")
    be.key_down("a")
    assert q.posted[-1].code == 0x0C and q.posted[-1].flags & macos.FLAG_COMMAND
    be.key_up("a")
    assert q.posted[-1].code == 0x0C
    be.key_down("z")
    assert q.posted[-1].code == 0x0D
    be.key_up("z")
    be.key_up("cmd")
    # a character the layout lacks falls back to unicode injection
    be.key_down("ß")
    assert q.posted[-1].text == "ß"
    be.key_up("ß")
    # typed text carries the layout keycode of the first char
    q.posted.clear()
    be.type_text("a")
    assert q.posted[0].code == 0x0C and q.posted[0].text == "a"


def test_keyboard_layout_change_is_picked_up():
    layouts = FakeLayouts(_AZERTY)
    now = [0.0]
    q = FakeQuartz()
    be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                  keyboard_layouts=layouts, clock=lambda: now[0])
    be.key_down("a")
    be.key_up("a")
    assert q.posted[-1].code == 0x0C
    calls = layouts.calls
    be.key_down("a")
    be.key_up("a")
    assert layouts.calls == calls  # throttled
    layouts.set({0x00: ("a", "A")})  # switch to a US-like layout
    now[0] += 1.0
    be.key_down("a")
    assert q.posted[-1].code == 0x00
    be.key_up("a")


def test_russian_layout_uses_command_table_for_shortcuts():
    layouts = FakeLayouts(_RUSSIAN, _RUSSIAN_CMD)
    q = FakeQuartz()
    be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                  keyboard_layouts=layouts)
    be.key_down("cmd")
    be.key_down("q")
    assert q.posted[-1].code == 0x0C and q.posted[-1].text is None
    be.key_up("q")
    be.key_up("cmd")
    # without Command, a Latin letter is not on the layout: inject it as unicode
    be.key_down("q")
    assert q.posted[-1].text == "q"
    be.key_up("q")
    be.key_down("ф")
    assert q.posted[-1].code == 0x00


def test_keyboard_layout_provider_errors_and_threads():
    import threading as _t

    class Broken:
        def current(self, known):
            raise OSError("TIS unavailable")

    q = FakeQuartz()
    be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                  keyboard_layouts=Broken())
    be.key_down("a")
    assert q.posted[-1].code == 0x00  # US-ANSI fallback
    be.key_up("a")
    layouts = FakeLayouts(_AZERTY)
    be2 = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                   keyboard_layouts=layouts)
    seen = []
    th = _t.Thread(target=lambda: seen.append(be2._keyboard_map()))
    th.start()
    th.join()
    assert layouts.calls == 0  # TIS is never touched off the main thread
    assert seen[0].lookup("a") == (0x00, False)


def test_release_all_and_close():
    be, q, _ = make_backend()
    be.key_down("ctrl")
    be.key_down("x")
    be.mouse_down()
    be.close()
    assert be._held_buttons == [] and be._held_mods == {} and be._pressed_keys == {}
    ups = [e for e in q.posted if e.kind == "key" and not e.down]
    assert {e.code for e in ups} == {0x3B, 0x07}
    be.close()  # idempotent


def test_permission_check_raises_helpful_error():
    ax = FakeAX()
    ax.trusted = False
    be, q, _ = make_backend(ax=ax)
    be._check_permissions = True
    with pytest.raises(BackendError, match="Accessibility"):
        be.mouse_move(1, 1)
    with pytest.raises(BackendError, match="Privacy & Security"):
        be.key_down("a")
    with pytest.raises(BackendError):
        be.accessibility_roots()
    assert q.posted == []
    ax.trusted = True
    be.mouse_move(1, 1)
    assert q.posted
    assert be.permissions() == {"accessibility": True, "screen_recording": True}


def test_clipboard_roundtrip():
    be, _, appkit = make_backend()
    assert be.get_clipboard() == ""
    be.set_clipboard("héllo")
    assert be.get_clipboard() == "héllo"
    be2 = MacOSBackend._from_parts(FakeQuartz(), None, None, layout=DisplayLayout([_d(1, 0, 0, 10, 10, 1.0)]))
    with pytest.raises(NotSupported):
        be2.get_clipboard()


def test_list_windows_filters_and_converts():
    apps = [FakeRunningApp(100, "TextEdit"), FakeRunningApp(200, "Finder")]
    be, q, appkit = make_backend(apps=apps)
    q.windows = [
        _rec(1, 200, 0, 0, 1440, 25, "Menubar", layer=25, owner="Finder"),
        _rec(2, 100, 100, 50, 400, 300, "Untitled", owner="TextEdit"),
        _rec(3, 100, 1500, 100, 300, 200, "Second", owner="TextEdit"),
        _rec(4, 200, 0, 25, 1440, 875, "Home", owner="Finder"),
        _rec(5, 200, 10, 10, 1, 1, "", owner="Finder"),
    ]
    wins = be.list_windows()
    assert [w.handle for w in wins] == [2, 3, 4]
    assert wins[0].rect == Rect(200, 100, 800, 600)  # retina display
    assert wins[1].rect == Rect(2940, 100, 300, 200)  # 1x display
    assert wins[0].is_active and not wins[1].is_active and not wins[2].is_active
    assert wins[0].app == "TextEdit" and wins[0].pid == 100
    assert wins[2].is_maximized and not wins[0].is_maximized
    assert be.active_window().handle == 2


def test_window_titles_fall_back_to_ax():
    ax = FakeAX()
    ax.apps[100] = FakeEl(AXWindows=[FakeEl(AXPosition=(100.0, 50.0), AXSize=(400.0, 300.0), AXTitle="From AX")])
    be, q, _ = make_backend(ax=ax)
    q.windows = [_rec(2, 100, 100, 50, 400, 300, "", owner="TextEdit")]
    assert be.list_windows()[0].title == "From AX"


def _window_setup():
    ax = FakeAX()
    win = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Doc", AXPosition=(100.0, 50.0), AXSize=(400.0, 300.0),
                 AXMinimized=True, actions=["AXRaise"],
                 settable={"AXPosition", "AXSize", "AXMinimized", "AXMain", "AXFocused", "AXFullScreen"})
    win.attrs["AXCloseButton"] = FakeEl(actions=["AXPress"])
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[win])
    be, q, appkit = make_backend(ax=ax)
    q.windows = [_rec(7, 100, 100, 50, 400, 300, "Doc", owner="TextEdit")]
    return be, q, appkit, ax, win


def test_window_actions_via_ax():
    be, q, appkit, ax, win = _window_setup()
    be.activate_window(7)
    assert appkit.apps[100].activated == 1
    assert win.attrs["AXMinimized"] is False and "AXRaise" in win.performed and win.attrs["AXMain"] is True

    be.move_window(7, Rect(2880 + 60, 20, 500, 400))  # onto the 1x display
    assert win.attrs["AXPosition"] == (1500.0, 20.0) and win.attrs["AXSize"] == (500.0, 400.0)
    be.move_window(7, Rect(200, 100, 800, 600))  # retina: halve
    assert win.attrs["AXPosition"] == (100.0, 50.0) and win.attrs["AXSize"] == (400.0, 300.0)

    be.set_window_state(7, "maximized")
    assert win.attrs["AXPosition"] == (0, 25) and win.attrs["AXSize"] == (1440, 875)
    q.windows = [_rec(7, 100, 0, 25, 1440, 875, "Doc", owner="TextEdit")]
    be.set_window_state(7, "normal")
    assert win.attrs["AXPosition"] == (100.0, 50.0) and win.attrs["AXSize"] == (400.0, 300.0)
    q.windows = [_rec(7, 100, 100, 50, 400, 300, "Doc", owner="TextEdit")]
    be.set_window_state(7, "minimized")
    assert win.attrs["AXMinimized"] is True
    with pytest.raises(ValueError):
        be.set_window_state(7, "sideways")
    be.close_window(7)
    assert win.attrs["AXCloseButton"].performed == ["AXPress"]
    with pytest.raises(WindowNotFound):
        be.close_window(999)
    with pytest.raises(WindowNotFound):
        be.activate_window("nope")


def test_move_window_refused_raises():
    be, q, appkit, ax, win = _window_setup()
    win.settable.discard("AXSize")
    with pytest.raises(BackendError):
        be.move_window(7, Rect(0, 0, 100, 100))


def _tree_setup():
    ax = FakeAX()
    ok = FakeEl(pid=100, AXRole="AXButton", AXTitle="OK", AXIdentifier="okBtn", AXEnabled=True,
                AXPosition=(110.0, 60.0), AXSize=(80.0, 30.0), actions=["AXPress"])
    label = FakeEl(pid=100, AXRole="AXStaticText", AXValue="Name:")
    field = FakeEl(pid=100, AXRole="AXTextField", AXValue="abc", AXEnabled=True, AXFocused=False,
                   AXTitleUIElement=label, AXPosition=(110.0, 100.0), AXSize=(200.0, 24.0),
                   settable={"AXValue", "AXFocused"})
    check = FakeEl(pid=100, AXRole="AXCheckBox", AXTitle="Remember", AXValue=1, actions=["AXPress"],
                   AXPosition=(110.0, 140.0), AXSize=(100.0, 20.0))
    disabled = FakeEl(pid=100, AXRole="AXButton", AXDescription="Trash", AXEnabled=False,
                      AXPosition=(5000.0, 5000.0), AXSize=(10.0, 10.0))
    outline_row = FakeEl(pid=100, AXRole="AXRow", AXSubrole="AXOutlineRow", AXDisclosing=False,
                         settable={"AXDisclosing", "AXSelected"})
    win = FakeEl(pid=100, AXRole="AXWindow", AXSubrole="AXStandardWindow", AXTitle="Doc",
                 AXPosition=(100.0, 50.0), AXSize=(400.0, 300.0),
                 AXChildren=[ok, field, check, disabled, outline_row], actions=["AXRaise"],
                 settable={"AXMain", "AXFocused"})
    for c in (ok, field, check, disabled, outline_row):
        c.attrs["AXParent"] = win
        c.attrs["AXWindow"] = win
    app = FakeEl(pid=100, AXRole="AXApplication", AXTitle="TextEdit", AXWindows=[win], AXChildren=[win])
    win.attrs["AXParent"] = app
    ax.apps[100] = app
    be, q, appkit = make_backend(ax=ax)
    q.windows = [_rec(7, 100, 100, 50, 400, 300, "Doc", owner="TextEdit")]
    return be, q, appkit, ax, dict(app=app, win=win, ok=ok, field=field, check=check, disabled=disabled,
                                   row=outline_row)


def test_accessibility_roots_and_node_properties():
    be, q, appkit, ax, els = _tree_setup()
    roots = be.accessibility_roots()
    assert len(roots) == 1
    root = roots[0]
    assert isinstance(root, AXNode)
    assert root.role == "window" and root.name == "Doc" and root.handle == 7
    assert root.bounds == Rect(200, 100, 800, 600)
    assert root.attributes()["app"] == "TextEdit"
    assert root.parent.role == "application"
    kids = root.children()
    ok, field, check, disabled, row = kids
    assert ok.role == "button" and ok.name == "OK" and ok.automation_id == "okBtn"
    assert ok.bounds == Rect(220, 120, 160, 60)
    assert ok.handle == 7 and ok.pid == 100 and ok.app == "TextEdit"
    assert ok.parent.same_as(root)
    assert {"enabled", "visible"} <= ok.states
    assert field.role == "edit" and field.name == "Name:" and field.value == "abc"
    assert "editable" in field.states
    assert check.role == "checkbox" and "checked" in check.states and check.value == "1"
    assert disabled.name == "Trash" and "disabled" in disabled.states and "offscreen" in disabled.states
    assert not disabled.is_enabled
    assert row.role == "treeitem" and "collapsed" in row.states
    assert ok.class_name == "AXButton" and ok.native_role == "AXButton"
    assert "invoke" in ok.actions()
    assert {"set_value", "focus"} <= set(field.actions())
    assert "toggle" in check.actions()


def test_node_actions():
    be, q, appkit, ax, els = _tree_setup()
    root = be.accessibility_roots()[0]
    ok, field, check, disabled, row = root.children()
    assert ok.invoke() and els["ok"].performed == ["AXPress"]
    assert not disabled.invoke()
    assert field.set_value("new") and field.value == "new"
    assert field.focus() and "focused" in field.states
    assert check.toggle() and els["check"].performed == ["AXPress"]
    assert not ok.toggle()
    assert row.expand() and "expanded" in row.states
    assert row.collapse() and "collapsed" in row.states
    assert row.select() and "selected" in row.states
    assert not ok.scroll_into_view()
    assert root.focus()
    assert appkit.apps[100].activated == 1 and "AXRaise" in els["win"].performed


def test_element_at_and_focused():
    be, q, appkit, ax, els = _tree_setup()
    ax.at = els["ok"]
    node = be.element_at(300, 150)  # pixels -> points on the retina display
    assert ax.last_at == (150.0, 75.0)
    assert node.name == "OK" and node.app == "TextEdit" and node.handle == 7
    ax.at = None
    assert be.element_at(0, 0) is None
    ax.system_wide.attrs["AXFocusedUIElement"] = els["field"]
    assert be.focused_element().role == "edit"
    del ax.system_wide.attrs["AXFocusedUIElement"]
    ax.apps[100].attrs["AXFocusedUIElement"] = els["check"]
    assert be.focused_element().name == "Remember"


def test_selectors_work_on_ax_tree():
    from locum.selectors import parse

    be, q, appkit, ax, els = _tree_setup()
    roots = be.accessibility_roots()
    found = parse('button "OK"').find_first(roots)
    assert found is not None and found.automation_id == "okBtn"
    assert parse("#okBtn").find_first(roots).name == "OK"
    assert parse("checkbox:checked").find_first(roots).name == "Remember"
    assert parse("button:disabled").find_first(roots).name == "Trash"


def test_screenshot_stitches_displays(monkeypatch):
    from PIL import Image

    be, q, _ = make_backend()
    grabs = []

    def fake_grab(x, y, w, h):
        grabs.append((x, y, w, h))
        scale = 2 if x < 1440 else 1
        color = (255, 0, 0) if x < 1440 else (0, 0, 255)
        return Image.new("RGB", (w * scale, h * scale), color)

    monkeypatch.setattr(be, "_grab_points", fake_grab)
    img = be.screenshot(Rect(2870, 10, 20, 10))  # straddles both displays
    assert img.size == (20, 10)
    assert img.getpixel((0, 0)) == (255, 0, 0) and img.getpixel((9, 5)) == (255, 0, 0)
    assert img.getpixel((10, 0)) == (0, 0, 255) and img.getpixel((19, 9)) == (0, 0, 255)
    assert grabs == [(1435, 5, 5, 5), (1440, 10, 10, 10)]
    full = be.screenshot()
    assert full.size == (4800, 1800)
    assert full.getpixel((4000, 1500)) == (0, 0, 0)  # gap below the shorter 1x display
    with pytest.raises(ValueError):
        be.screenshot(Rect(0, 0, 0, 5))


def test_screenshot_resizes_unexpected_grab_size(monkeypatch):
    from PIL import Image

    be, q, _ = make_backend()
    monkeypatch.setattr(be, "_grab_points", lambda x, y, w, h: Image.new("RGB", (w, h), (9, 9, 9)))
    img = be.screenshot(Rect(0, 0, 40, 20))
    assert img.size == (40, 20) and img.getpixel((39, 19)) == (9, 9, 9)


def test_screen_recording_warning(monkeypatch):
    from PIL import Image

    be, q, _ = make_backend()
    q.screen_capture_ok = False
    monkeypatch.setattr(be, "_grab_points", lambda x, y, w, h: Image.new("RGB", (w * 2, h * 2)))
    with pytest.warns(RuntimeWarning, match="Screen Recording"):
        be.screenshot(Rect(0, 0, 4, 4))


def test_launch_uses_open_for_app_names(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(macos.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(macos.shutil, "which", lambda name: None if name == "Safari" else "/bin/" + name)
    be, _, _ = make_backend()
    be.launch("Safari")
    be.launch("/Applications/Calculator.app")
    assert calls == [["open", "-a", "Safari"], ["open", "-a", "/Applications/Calculator.app"]]


# =============================================================================
# review regressions: live front-most app, window pairing, minimized windows,
# activation, zoom, launch, keyboard layout off the main thread, modified keys
# =============================================================================


def test_frontmost_app_is_read_live_not_from_stale_appkit():
    from locum import Desktop
    from locum.llm.safety import Policy
    from locum.llm.tools import DesktopToolkit

    claude, term = FakeRunningApp(100, "Claude"), FakeRunningApp(200, "Terminal")
    be, q, appkit = make_backend(apps=[claude, term])
    appkit.front = claude  # NSWorkspace snapshot: never refreshed without a run loop
    q.windows = [_rec(2, 200, 100, 50, 600, 400, "zsh", owner="Terminal"),
                 _rec(1, 100, 0, 25, 1440, 875, "Claude", owner="Claude")]
    assert be.active_window().app == "Terminal"
    tk = DesktopToolkit(Desktop(be, failsafe=False), policy=Policy.strict())
    res = tk.registry.call("type_text", {"text": "echo pwned"})
    assert res.is_error and "Terminal" in res.text
    assert not [e for e in q.posted if e.kind == "key"]
    # the system-wide AX focused application wins over everything else
    ax = FakeAX()
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[])
    ax.apps[200] = FakeEl(pid=200, AXRole="AXApplication", AXWindows=[])
    be2, q2, appkit2 = make_backend(ax=ax, apps=[claude, term])
    q2.windows = list(q.windows)
    ax.system_wide.attrs["AXFocusedApplication"] = ax.apps[100]
    assert be2._frontmost_pid() == 100
    # with a serviceable run loop on the main thread, AppKit is refreshed and trusted
    be3, q3, appkit3 = make_backend(apps=[claude, term])
    q3.windows = list(q.windows)
    pumped = []

    def run_in_mode(mode, seconds, once):
        pumped.append(mode)
        appkit3.front = claude
        return 3  # kCFRunLoopRunTimedOut

    appkit3.CFRunLoopRunInMode, appkit3.kCFRunLoopDefaultMode = run_in_mode, "kCFRunLoopDefaultMode"
    appkit3.front = term
    assert be3._frontmost_pid() == 100 and pumped


def test_apps_launched_after_start_get_accessibility_roots():
    ax = FakeAX()
    old_win = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Old", AXPosition=(0.0, 25.0), AXSize=(400.0, 300.0))
    new_win = FakeEl(pid=300, AXRole="AXWindow", AXTitle="Ledger", AXPosition=(500.0, 25.0), AXSize=(400.0, 300.0))
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[old_win])
    ax.apps[300] = FakeEl(pid=300, AXRole="AXApplication", AXWindows=[new_win])
    old, new = FakeRunningApp(100, "TextEdit"), FakeRunningApp(300, "Microsoft Excel")
    be, q, appkit = make_backend(ax=ax, apps=[old, new])
    snapshot = SimpleNamespace(frontmostApplication=lambda: old, runningApplications=lambda: [old])
    appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: snapshot)  # stale: Excel missing
    q.windows = [_rec(5, 300, 500, 25, 400, 300, "Ledger", owner="Microsoft Excel"),
                 _rec(4, 100, 0, 25, 400, 300, "Old", owner="TextEdit")]
    roots = be.accessibility_roots()
    assert [(r.name, r.handle, r.app) for r in roots] == [("Ledger", 5, "Microsoft Excel"), ("Old", 4, "TextEdit")]


def test_pair_ax_windows_is_one_to_one():
    pair = macos.pair_ax_windows
    recs = [_rec(11, 1, 0, 25, 1440, 875, "Payments.xlsx"), _rec(12, 1, 0, 25, 1440, 875, "Budget.xlsx")]
    full = (0.0, 25.0, 1440.0, 875.0)
    assert pair(recs, [(full, "Payments.xlsx", None), (full, "Budget.xlsx", None)]) == {0: 11, 1: 12}
    assert pair(recs, [(full, "Budget.xlsx", None), (full, "Payments.xlsx", None)]) == {0: 12, 1: 11}
    untitled = [_rec(11, 1, 0, 25, 1440, 875), _rec(12, 1, 0, 25, 1440, 875)]
    assert pair(untitled, [(full, "Payments.xlsx", None), (full, "Budget.xlsx", None)]) == {}  # ambiguous
    assert pair(untitled, [(full, "Payments.xlsx", 12), (full, "Budget.xlsx", None)]) == {0: 12, 1: 11}
    # a panel without an AX counterpart is never given the document window
    panel = [_rec(11, 1, 0, 25, 1440, 875, "Payments.xlsx"), _rec(12, 1, 900, 300, 250, 400, "Formula Builder")]
    assert pair(panel, [(full, "Payments.xlsx", None)]) == {0: 11}
    assert pair(panel[1:], [(full, "Payments.xlsx", None)]) == {}
    # a single window that is moving (frame mismatch) still pairs when titles do not conflict
    assert pair([_rec(11, 1, 10, 25, 1440, 875)], [(full, "Payments.xlsx", None)]) == {0: 11}


def _two_workbooks(window_ids=False):
    ax = FakeAX()
    pay = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Payments.xlsx", AXPosition=(0.0, 25.0),
                 AXSize=(1440.0, 875.0), actions=["AXRaise"], settable={"AXMain", "AXMinimized"})
    bud = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Budget.xlsx", AXPosition=(0.0, 25.0),
                 AXSize=(1440.0, 875.0), actions=["AXRaise"], settable={"AXMain", "AXMinimized"})
    for el in (pay, bud):
        el.attrs["AXCloseButton"] = FakeEl(actions=["AXPress"])
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[pay, bud])
    if window_ids:
        ids = {id(pay): 11, id(bud): 12}
        ax.window_id = lambda el: ids.get(id(el))
    be, q, appkit = make_backend(ax=ax, apps=[FakeRunningApp(100, "Microsoft Excel")])
    return be, q, ax, pay, bud


def test_windows_with_the_same_frame_are_told_apart():
    be, q, ax, pay, bud = _two_workbooks()
    q.windows = [_rec(11, 100, 0, 25, 1440, 875, "Payments.xlsx", owner="Microsoft Excel"),
                 _rec(12, 100, 0, 25, 1440, 875, "Budget.xlsx", owner="Microsoft Excel")]
    be.close_window(12)
    assert bud.attrs["AXCloseButton"].performed == ["AXPress"] and not pay.attrs["AXCloseButton"].performed
    be.set_window_state(12, "minimized")
    assert bud.attrs.get("AXMinimized") is True and not pay.attrs.get("AXMinimized")
    # without Screen Recording (no CG titles) the pairing is ambiguous: never guess
    q.windows = [_rec(11, 100, 0, 25, 1440, 875, "", owner="Microsoft Excel"),
                 _rec(12, 100, 0, 25, 1440, 875, "", owner="Microsoft Excel")]
    bud.attrs["AXMinimized"] = False
    assert [w.title for w in be.list_windows()] == ["", ""]
    assert [r.handle for r in be.accessibility_roots()] == [None, None]
    with pytest.raises(WindowNotFound):
        be.close_window(11)
    assert not pay.attrs["AXCloseButton"].performed
    # with exact window ids (_AXUIElementGetWindow) every window is resolved
    be, q, ax, pay, bud = _two_workbooks(window_ids=True)
    q.windows = [_rec(11, 100, 0, 25, 1440, 875, "", owner="Microsoft Excel"),
                 _rec(12, 100, 0, 25, 1440, 875, "", owner="Microsoft Excel")]
    assert [(w.handle, w.title) for w in be.list_windows()] == [(11, "Payments.xlsx"), (12, "Budget.xlsx")]
    assert sorted((r.name, r.handle) for r in be.accessibility_roots()) == [("Budget.xlsx", 12), ("Payments.xlsx", 11)]
    be.close_window(12)
    assert bud.attrs["AXCloseButton"].performed and not pay.attrs["AXCloseButton"].performed


def test_window_without_ax_counterpart_is_not_resolved_to_the_only_ax_window():
    ax = FakeAX()
    main = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Payments.xlsx", AXPosition=(0.0, 25.0), AXSize=(1440.0, 875.0))
    main.attrs["AXCloseButton"] = FakeEl(actions=["AXPress"])
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[main])
    be, q, _ = make_backend(ax=ax, apps=[FakeRunningApp(100, "Microsoft Excel")])
    q.windows = [_rec(11, 100, 0, 25, 1440, 875, "Payments.xlsx", owner="Microsoft Excel"),
                 _rec(12, 100, 900, 300, 250, 400, "Formula Builder", owner="Microsoft Excel")]
    with pytest.raises(WindowNotFound):
        be.close_window(12)
    assert main.attrs["AXCloseButton"].performed == []
    be.close_window(11)
    assert main.attrs["AXCloseButton"].performed == ["AXPress"]


def test_minimized_windows_are_listed():
    from locum import Desktop
    from locum.errors import ActionTimeout

    ax = FakeAX()
    win = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Budget.xlsx", AXPosition=(100.0, 50.0), AXSize=(400.0, 300.0),
                 AXMinimized=False, actions=["AXRaise"], settable={"AXMain", "AXMinimized"})
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[win])
    be, q, appkit = make_backend(ax=ax, apps=[FakeRunningApp(100, "Microsoft Excel")])
    q.windows = [_rec(7, 100, 100, 50, 400, 300, "Budget.xlsx", owner="Microsoft Excel"),
                 _rec(8, 100, 0, 0, 300, 200, "", owner="Microsoft Excel")]  # hidden helper window
    orig = q.CGWindowListCopyWindowInfo

    def real_quartz(options, relative):  # on-screen lists omit minimized windows
        out = orig(options, relative)
        if options & macos.kCGWindowListOptionOnScreenOnly:
            out = [r for r in out if r["kCGWindowNumber"] != 8
                   and not (r["kCGWindowNumber"] == 7 and win.attrs.get("AXMinimized"))]
        return out

    q.CGWindowListCopyWindowInfo = real_quartz
    d = Desktop(be, failsafe=False)
    w = d.window("Budget.xlsx")
    w.minimize()
    infos = be.list_windows()
    assert [(i.handle, i.is_minimized, i.is_visible) for i in infos] == [(7, True, False)]
    assert w.exists and w.is_minimized
    with pytest.raises(ActionTimeout):
        w.wait_closed(timeout=0.05)
    d.window("Budget.xlsx", timeout=0.1).restore()
    assert win.attrs["AXMinimized"] is False
    assert [(i.handle, i.is_minimized) for i in be.list_windows()] == [(7, False)]


def test_activate_window_must_make_the_app_frontmost(monkeypatch):
    monkeypatch.setattr(MacOSBackend, "ACTIVATE_TIMEOUT", 0.05)
    be, q, appkit, ax, win = _window_setup()
    ax.apps[300] = FakeEl(pid=300, AXRole="AXApplication", AXWindows=[])
    ax.system_wide.attrs["AXFocusedApplication"] = ax.apps[300]  # the bank portal has focus
    appkit.apps[100].activateWithOptions_ = lambda opts: False   # activation declined
    with pytest.raises(BackendError, match="pid 100"):
        be.activate_window(7)
    # AXFrontmost works where activateWithOptions_ is declined
    app = ax.apps[100]
    app.settable.add("AXFrontmost")
    orig_set = ax.set_attr

    def set_attr(el, name, value):
        ok = orig_set(el, name, value)
        if ok and el is app and name == "AXFrontmost":
            ax.system_wide.attrs["AXFocusedApplication"] = app
        return ok

    ax.set_attr = set_attr
    be.activate_window(7)
    assert app.attrs["AXFrontmost"] is True
    # activation "succeeds" but another app keeps keyboard focus: raise
    ax.system_wide.attrs["AXFocusedApplication"] = ax.apps[300]
    app.settable.discard("AXFrontmost")
    appkit.apps[100].activateWithOptions_ = lambda opts: True
    with pytest.raises(BackendError, match="front-most"):
        be.activate_window(7)


def test_restore_unzooms_a_window_maximized_outside_locum():
    class ZoomAX(FakeAX):
        def perform(self, el, action):
            ok = super().perform(el, action)
            if ok and action == "AXZoomWindow":
                el.owner.attrs.update(AXPosition=(100.0, 50.0), AXSize=(400.0, 300.0))
            return ok

    ax = ZoomAX()
    win = FakeEl(pid=100, AXRole="AXWindow", AXTitle="Ledger", AXPosition=(0.0, 25.0), AXSize=(1440.0, 875.0),
                 settable={"AXPosition", "AXSize", "AXMinimized", "AXMain"})
    button = FakeEl(AXSubrole="AXFullScreenButton", actions=["AXPress", "AXZoomWindow"])
    button.owner = win
    win.attrs["AXZoomButton"] = button
    ax.apps[100] = FakeEl(pid=100, AXRole="AXApplication", AXWindows=[win])
    be, q, _ = make_backend(ax=ax)
    q.windows = [_rec(7, 100, 0, 25, 1440, 875, "Ledger", owner="TextEdit")]
    assert be.list_windows()[0].is_maximized
    be.set_window_state(7, "normal")
    assert button.performed == ["AXZoomWindow"]  # never AXPress: that would enter full screen
    assert win.attrs["AXSize"] == (400.0, 300.0)
    # a full-screen button without a zoom action: refuse instead of reporting success
    win.attrs.update(AXPosition=(0.0, 25.0), AXSize=(1440.0, 875.0))
    button.actions = ["AXPress"]
    button.performed.clear()
    with pytest.raises(BackendError, match="restore"):
        be.set_window_state(7, "normal")
    assert button.performed == []


def test_launch_opens_app_names_with_spaces(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(macos.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(macos.shutil, "which", lambda name: "/bin/ls" if name == "ls" else None)
    be, _, _ = make_backend()
    for cmd in ("Microsoft Excel", " SAP GUI for Java ", "ls -la", "'My Tool' --x"):
        be.launch(cmd)
    assert calls == [["open", "-a", "Microsoft Excel"], ["open", "-a", "SAP GUI for Java"],
                     ["ls", "-la"], ["My Tool", "--x"]]


def test_policy_judges_multi_word_app_names_as_a_whole():
    from locum.errors import PolicyViolation
    from locum.llm.safety import Policy

    with pytest.raises(PolicyViolation, match="Google Chrome"):
        Policy(blocked_apps=["google chrome"]).check("launch_app", {"command": "Google Chrome"}, context={})
    Policy(allowed_apps=["Microsoft Excel"]).check("launch_app", {"command": "Microsoft Excel"}, context={})
    with pytest.raises(PolicyViolation):
        Policy(allowed_apps=["Microsoft Excel"]).check("launch_app", {"command": "Google Chrome"}, context={})


def test_keyboard_layout_unknown_off_main_thread_never_guesses_shortcuts():
    import threading as _t

    q = FakeQuartz()
    errors: List[BaseException] = []
    caught: List[Any] = []

    def worker():
        import warnings as _w

        be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                      keyboard_layouts=FakeLayouts(_AZERTY))
        be._keyboard_map()
        be.key_down("cmd")
        try:
            be.key_down("a")  # US "a" is "q" on AZERTY: Cmd+Q would quit the app
        except BackendError as exc:
            errors.append(exc)
        be.key_up("cmd")
        be.key_down("enter")  # layout independent keys still work
        be.key_up("enter")
        with _w.catch_warnings(record=True) as rec:
            _w.simplefilter("always")
            be.key_down("a")
            be.key_up("a")
        caught.extend(rec)

    th = _t.Thread(target=worker)
    th.start()
    th.join()
    assert len(errors) == 1 and "main thread" in str(errors[0])
    downs = [e for e in q.posted if e.kind == "key" and e.etype == macos.kCGEventKeyDown]
    assert all(not (e.flags & macos.FLAG_COMMAND) for e in downs)
    assert [(e.code, e.text) for e in downs] == [(0x24, None), (0, "a")]  # "a" injected as text
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    # once read on the main thread, other threads use the real layout
    layouts = FakeLayouts(_AZERTY)
    be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                  keyboard_layouts=layouts)
    be._keyboard_map()
    q.posted.clear()

    def shortcut():
        be.key_down("cmd")
        be.key_down("a")
        be.key_up("a")
        be.key_up("cmd")

    th = _t.Thread(target=shortcut)
    th.start()
    th.join()
    assert [e.code for e in q.posted if e.kind == "key" and e.etype == macos.kCGEventKeyDown] == [0x0C]


def test_modified_keys_never_fall_back_to_keycode_zero():
    def press(layouts, mod, key):
        q = FakeQuartz()
        be = MacOSBackend._from_parts(q, None, None, layout=DisplayLayout([_d(1, 0, 0, 100, 100, 1.0, True)]),
                                      keyboard_layouts=layouts)
        be.key_down(mod)
        try:
            be.key_down(key)
            be.key_up(key)
        finally:
            be.key_up(mod)
        return [e for e in q.posted if e.etype == macos.kCGEventKeyDown][0]

    ev = press(FakeLayouts(_AZERTY), "cmd", "[")  # needs Option on AZERTY: US position, not 0x00 ("q")
    assert ev.code == 0x21 and ev.text is None
    for mod in ("ctrl", "alt", "cmd"):  # Russian: Latin shortcuts use the Command table
        ev = press(FakeLayouts(_RUSSIAN, _RUSSIAN_CMD), mod, "a")
        assert ev.code == 0x00 and ev.text is None
    ev = press(FakeLayouts(_RUSSIAN, _RUSSIAN_CMD), "ctrl", "c")  # not on the layout: US "c"
    assert ev.code == 0x08 and ev.text is None
    with pytest.raises(NotSupported):
        press(FakeLayouts(_AZERTY), "cmd", "ß")
    assert resolve_key("ß", macos.KeyboardLayoutMap.us_ansi()).kind == "unicode"  # plain typing unchanged


def test_pair_ax_windows_unique_frame_beats_title_mismatch():
    # CG and AX titles may differ (e.g. Java apps); a frame unique on both sides still pairs
    recs = [_rec(21, 1, 0, 25, 800, 600, "SAP Easy Access"), _rec(22, 1, 900, 25, 400, 300, "Log")]
    wins = [((0.0, 25.0, 800.0, 600.0), "SAP Easy Access - User menu", None)]
    assert macos.pair_ax_windows(recs, wins) == {0: 21}


# =============================================================================
# first live run on a real Mac: async cursor, minimized windows, launch names,
# CFRange values, full-resolution capture, clipboard snapshot, caps lock, ids
# =============================================================================


class AsyncCursorQuartz(FakeQuartz):
    """Like the WindowServer: a posted move is applied later, not by CGEventPost."""

    def CGEventPost(self, tap, ev):
        self.posted.append(ev)


def test_click_right_after_move_uses_the_posted_point():
    be, q, _ = make_backend()
    async_q = AsyncCursorQuartz()
    be._q = async_q
    be.mouse_move(200, 100)
    assert async_q.pos == (0.0, 0.0)  # the real cursor has not caught up yet
    assert be.mouse_position() == Point(200, 100)
    be.mouse_down(MouseButton.LEFT)
    be.mouse_up(MouseButton.LEFT)
    assert [e.pt for e in async_q.posted] == [(100.0, 50.0)] * 3


def test_cursor_is_read_back_once_the_posted_point_is_stale(monkeypatch):
    be, q, _ = make_backend()
    be.mouse_move(200, 100)
    q.pos = (300.0, 200.0)  # the user moved the mouse afterwards
    monkeypatch.setattr(be, "CURSOR_SETTLE", 0.0)
    assert be.mouse_position() == Point(600, 400)


def test_minimized_window_is_found_by_id():
    be, q, appkit, ax, win = _window_setup()
    # kCGWindowListOptionIncludingWindow omits minimized windows; describing the id does not
    q.CGWindowListCopyWindowInfo = lambda options, relative: (
        [] if options == macos.kCGWindowListOptionIncludingWindow else list(q.windows))
    q.CGWindowListCreateDescriptionFromArray = lambda ids: [w for w in q.windows if w["kCGWindowNumber"] in ids]
    be.set_window_state(7, "normal")
    assert win.attrs["AXMinimized"] is False
    be.close_window(7)
    assert win.attrs["AXCloseButton"].performed == ["AXPress"]
    with pytest.raises(WindowNotFound):
        be.close_window(999)


def test_launch_prefers_installed_apps_over_case_insensitive_commands(monkeypatch):
    from locum.backends.macos import windows_mixin

    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append(args)

    monkeypatch.setattr(macos.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/" + name.lower())  # APFS: which("Mail")
    apps = {"Mail", "Script Editor"}
    monkeypatch.setattr(windows_mixin, "_find_app_bundle",
                        lambda name: f"/System/Applications/{name}.app" if name in apps else None)
    be, _, _ = make_backend()
    be.launch("Mail")
    be.launch("Script Editor")
    be.launch("mail")  # lower case: the command
    be.launch("Say hello")  # no such app: the command
    assert calls == [["open", "-a", "Mail"], ["open", "-a", "Script Editor"], ["mail"], ["Say", "hello"]]


def test_cfrange_values_are_unpacked_from_plain_tuples():
    class AXValueRef:
        pass

    mod = SimpleNamespace(
        AXUIElementCreateSystemWide=lambda: object(),
        AXUIElementSetMessagingTimeout=lambda el, t: 0,
        AXValueGetType=lambda v: macos.kAXValueCFRangeType,
        AXValueGetValue=lambda v, typ, out: (True, (2, 5)),  # pyobjc's CFRange shape
    )
    assert macos._AXApi(mod)._convert(AXValueRef()) == (2, 5)


def test_screenshot_captures_full_resolution_before_mss(monkeypatch):
    from PIL import Image

    be, q, _ = make_backend()
    monkeypatch.setattr(be, "_grab_points_cg", lambda x, y, w, h: Image.new("RGB", (w * 2, h * 2), (1, 2, 3)))
    monkeypatch.setattr(be, "_mss", lambda: pytest.fail("mss captures one pixel per point on macOS"))
    assert be._grab_points(0, 0, 10, 5).size == (20, 10)

    def no_quartz(x, y, w, h):
        raise BackendError("no CGWindowListCreateImage")

    shot = SimpleNamespace(size=(10, 5), bgra=b"\x03\x02\x01\x00" * 50)
    monkeypatch.setattr(be, "_grab_points_cg", no_quartz)
    monkeypatch.setattr(be, "_mss", lambda: SimpleNamespace(grab=lambda region: shot))
    img = be._grab_points(0, 0, 10, 5)
    assert img.size == (10, 5) and img.getpixel((0, 0)) == (1, 2, 3)


class FakePasteboardItem:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def init(self):
        return self

    def types(self):
        return list(self.data)

    def dataForType_(self, t):
        return self.data.get(t)

    def setData_forType_(self, data, t):
        self.data[t] = data
        return True


def test_clipboard_snapshot_keeps_every_item_and_type():
    be, _, appkit = make_backend()
    pb = appkit.pasteboard
    pb.items = [FakePasteboardItem({"public.png": b"\x89PNG", "public.utf8-plain-text": b"pic"}),
                FakePasteboardItem({"public.file-url": b"file:///tmp/a.txt"})]
    pb.pasteboardItems = lambda: pb.items
    written = []
    pb.writeObjects_ = lambda objs: written.extend(objs) or True
    appkit.NSPasteboardItem = SimpleNamespace(alloc=lambda: FakePasteboardItem())
    appkit.NSData = SimpleNamespace(dataWithBytes_length_=lambda data, n: bytes(data[:n]))

    saved = be.save_clipboard()
    be.set_clipboard("typed text")
    be.restore_clipboard(saved)
    assert [w.data for w in written] == [{"public.png": b"\x89PNG", "public.utf8-plain-text": b"pic"},
                                         {"public.file-url": b"file:///tmp/a.txt"}]
    be.restore_clipboard("plain")  # a text snapshot still works
    assert be.get_clipboard() == "plain"


def test_capslock_toggles_the_lock_state(monkeypatch):
    from locum.backends.macos import input_mixin

    toggles = []
    monkeypatch.setattr(input_mixin, "toggle_caps_lock", lambda: toggles.append(1) or True)
    be, q, _ = make_backend()
    be.key_down("capslock")
    be.key_up("capslock")
    assert toggles == [1] and q.posted == []  # a synthetic key event would not change the state
    monkeypatch.setattr(input_mixin, "toggle_caps_lock", lambda: False)
    be.key_down("capslock")
    be.key_up("capslock")
    assert len(q.posted) == 2  # no IOKit: the key event is still sent


def test_generated_interface_builder_ids_are_not_automation_ids():
    be, q, appkit, ax, els = _tree_setup()
    els["ok"].attrs["AXIdentifier"] = "_NS:136"
    els["field"].attrs["AXIdentifier"] = "_NS:abc"  # not the generated pattern
    root = be.accessibility_roots()[0]
    ok, field = root.children()[:2]
    assert ok.automation_id == "" and field.automation_id == "_NS:abc"


# =============================================================================
# adversarial review of d47d6a1: clipboard snapshot, cursor cache, app arguments
# =============================================================================


def _snapshot_setup(items):
    be, _, appkit = make_backend()
    pb = appkit.pasteboard
    pb.items = items
    pb.pasteboardItems = lambda: pb.items
    pb.written = []
    pb.writeObjects_ = lambda objs: pb.written.extend(objs) or True
    appkit.NSPasteboardItem = SimpleNamespace(alloc=lambda: FakePasteboardItem())
    appkit.NSData = SimpleNamespace(dataWithBytes_length_=lambda data, n: bytes(data[:n]))
    return be, pb, appkit


def test_snapshot_refuses_unavailable_data_instead_of_saving_a_partial_copy():
    be, pb, _ = _snapshot_setup([FakePasteboardItem({"public.png": None, "public.utf8-plain-text": b"x"})])
    with pytest.raises(BackendError, match="unavailable"):
        be.save_clipboard()


def test_snapshot_refuses_an_oversized_clipboard(monkeypatch):
    be, pb, _ = _snapshot_setup([FakePasteboardItem({"public.mpeg-4": b"\0" * 2048})])
    monkeypatch.setattr(type(be), "CLIPBOARD_SNAPSHOT_MAX", 1024)
    with pytest.raises(BackendError, match="more than"):
        be.save_clipboard()


def test_snapshot_retries_then_refuses_a_clipboard_that_keeps_changing():
    be, pb, _ = _snapshot_setup([FakePasteboardItem({"public.utf8-plain-text": b"a"})])
    counter = iter(range(100))
    pb.changeCount = lambda: next(counter)  # changes between every read
    with pytest.raises(BackendError, match="kept changing"):
        be.save_clipboard()
    stable = iter([5, 5])
    pb.changeCount = lambda: next(stable)
    assert be.save_clipboard() == {"items": [[("public.utf8-plain-text", b"a")]]}


def test_clipboard_mode_typing_never_overwrites_a_clipboard_it_cannot_save():
    """A failed snapshot must stop clipboard typing *before* the paste replaces the user's data."""
    from locum.keyboard import Keyboard

    be, pb, _ = _snapshot_setup([FakePasteboardItem({"public.png": None})])
    pb.value = "user data"
    kb = Keyboard(be, sleep=lambda s: None)
    with pytest.raises(BackendError):
        kb.type("pasted", mode="clipboard")
    assert pb.value == "user data"


def test_restore_checks_every_type_before_touching_the_pasteboard():
    be, pb, appkit = _snapshot_setup([])

    class Refusing(FakePasteboardItem):
        def setData_forType_(self, data, t):
            return t != "com.example.private"

    appkit.NSPasteboardItem = SimpleNamespace(alloc=lambda: Refusing())
    pb.value = "still here"
    with pytest.raises(BackendError, match="com.example.private"):
        be.restore_clipboard({"items": [[("public.utf8-plain-text", b"t"), ("com.example.private", b"p")]]})
    assert pb.value == "still here" and pb.written == []  # nothing was cleared or written


def test_cached_cursor_yields_to_a_real_pointer_move():
    """The posted point bridges WindowServer lag only while the cursor still reads its old spot."""
    be, q, _ = make_backend()
    async_q = AsyncCursorQuartz()
    be._q = async_q
    be.mouse_move(200, 100)  # posted, not yet applied: cursor still reads (0, 0)
    assert be.mouse_position() == Point(200, 100)
    async_q.pos = (300.0, 200.0)  # the user moves the pointer elsewhere within 250 ms
    assert be.mouse_position() == Point(600, 400)
    be.mouse_down(MouseButton.LEFT)
    assert async_q.posted[-1].pt == (300.0, 200.0)  # the click goes where the pointer really is


def test_launch_splits_app_name_from_arguments_and_documents(monkeypatch):
    from locum.backends.macos import windows_mixin

    calls = []
    monkeypatch.setattr(macos.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(macos.shutil, "which", lambda name: None)
    apps = {"TextEdit", "Preview", "Microsoft Excel"}
    monkeypatch.setattr(windows_mixin, "_find_app_bundle",
                        lambda name: f"/Applications/{name}.app" if name in apps else None)
    be, _, _ = make_backend()
    be.launch("TextEdit --args -ApplePersistenceIgnoreState YES")
    be.launch("Preview /tmp/report.pdf")
    be.launch("Microsoft Excel -n")
    be.launch("Microsoft Excel")
    be.launch("Setapp Tool")  # not in the usual folders: Launch Services resolves it
    assert calls == [
        ["open", "-a", "TextEdit", "--args", "-ApplePersistenceIgnoreState", "YES"],
        ["open", "-a", "Preview", "/tmp/report.pdf"],
        ["open", "-a", "Microsoft Excel", "--args", "-n"],
        ["open", "-a", "Microsoft Excel"],
        ["open", "-a", "Setapp Tool"],
    ]
