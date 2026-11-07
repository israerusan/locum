"""Off-screen window capture: ``Window.screenshot(mode=..., restore_minimized=...)``,
``Backend.capture_window`` and its routing through observe / the CLI.

Nothing here touches the real desktop: the routing runs against
:class:`MockBackend`; the Windows (PrintWindow), macOS (CGWindowListCreateImage)
and X11 (Composite pixmap) code paths run against fake native APIs.
"""

from __future__ import annotations

import ctypes
import io
import json
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from PIL import Image

from locum.backends.base import CAP_WINDOW_CAPTURE, is_blank_image
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import NotSupported, WindowNotFound
from locum.llm.observe import observe
from locum.types import Rect

GREEN, RED, BLUE = (10, 200, 10), (200, 10, 10), (10, 10, 200)


def _noop(_s: float) -> None:
    pass


@pytest.fixture
def scene():
    """A green "Target" window with a red "Cover" window in front of part of it."""
    be = MockBackend(width=800, height=600)
    target = be.add_window("Target", (100, 100, 300, 200), fill=GREEN)
    target.add_button("Hidden", (60, 60, 80, 30))  # screen (160, 160): under the cover
    be.add_window("Cover", (150, 150, 120, 120), fill=RED)
    d = Desktop(be, sleep=_noop, default_timeout=0.2, poll_interval=0.01)
    return be, d, target


def _events(be: MockBackend, kind: str) -> List[tuple]:
    return [e for e in be.events if e[0] == kind]


# ============================================================ blank detection
def test_is_blank_image():
    assert is_blank_image(Image.new("RGB", (5, 5), (0, 0, 0)))
    assert is_blank_image(Image.new("RGB", (5, 5), (255, 255, 255)))
    assert is_blank_image(Image.new("L", (3, 3), 7))
    img = Image.new("RGB", (5, 5), (0, 0, 0))
    img.putpixel((4, 4), (0, 0, 1))
    assert not is_blank_image(img)


def test_base_backend_capture_window_is_not_supported():
    from locum.backends.base import Backend

    class Bare(MockBackend):
        capture_window = Backend.capture_window

    with pytest.raises(NotSupported, match="cannot capture"):
        Bare().capture_window(1)


# ============================================================ routing (mock)
def test_covered_window_auto_uses_window_capture(scene):
    be, d, _ = scene
    w = d.window("Target")
    assert w.is_covered() and w.is_on_screen()
    be.clear_events()
    img = w.screenshot()
    assert img.size == (300, 200)
    assert img.getpixel((100, 100)) == GREEN  # the window's own content, not the cover
    assert _events(be, "capture_window") and not _events(be, "screenshot")
    assert w.screenshot(mode="screen").getpixel((100, 100)) == RED  # the old behaviour on request
    assert w.screenshot(mode="window").getpixel((100, 100)) == GREEN


def test_window_in_plain_view_uses_the_screen_grab(scene):
    be, d, _ = scene
    w = d.window("Cover")
    assert not w.is_covered()
    be.clear_events()
    img = w.screenshot()
    assert img.size == (120, 120) and img.getpixel((60, 60)) == RED
    assert _events(be, "screenshot") and not _events(be, "capture_window")


def test_partly_off_screen_window_is_captured_whole():
    be = MockBackend(width=400, height=300)
    be.add_window("Edge", (300, 200, 200, 200), fill=BLUE)
    d = Desktop(be, sleep=_noop, default_timeout=0.2)
    w = d.window("Edge")
    assert not w.is_on_screen()
    img = w.screenshot()
    assert img.size == (200, 200) and img.getpixel((150, 150)) == BLUE
    assert w.screenshot(mode="screen").size == (100, 100)


def test_minimized_window_needs_restore_minimized(scene):
    be, d, target = scene
    w = d.window("Target")
    w.minimize()
    active_before = be.active_window()
    with pytest.raises(NotSupported, match="minimized"):
        w.screenshot()
    with pytest.raises(NotSupported, match="minimized"):
        w.screenshot(mode="window")
    with pytest.raises(NotSupported, match="minimized"):
        w.screenshot(mode="screen")
    be.clear_events()
    img = w.screenshot(restore_minimized=True)
    assert img.size == (300, 200) and img.getpixel((100, 100)) == GREEN
    kinds = [e[0] for e in be.events]
    assert kinds.index("restore_noactivate") < kinds.index("minimize_noactivate")
    assert target.minimized and w.refresh().is_minimized  # minimized again
    assert be.active_window() == active_before  # no focus change
    assert "activate" not in kinds


def test_restore_minimized_honours_the_failsafe(scene):
    from locum import failsafe
    from locum.errors import FailSafeTriggered

    be, d, _ = scene
    w = d.window("Target")
    w.minimize()
    failsafe.abort("test")
    try:
        with pytest.raises(FailSafeTriggered):
            w.screenshot(restore_minimized=True)
    finally:
        failsafe.reset_abort()
    assert not _events(be, "restore_noactivate")


def test_blank_capture_falls_back_to_screen_in_auto_and_raises_in_window_mode(scene):
    be, d, target = scene
    target.capture_blank = True  # a GPU / UWP surface PrintWindow cannot read
    w = d.window("Target")
    assert w.screenshot().getpixel((100, 100)) == RED  # auto: back to the screen grab
    with pytest.raises(NotSupported, match="blank"):
        w.screenshot(mode="window")
    w.minimize()
    with pytest.raises(NotSupported):
        w.screenshot(restore_minimized=True)  # still blank when shown


def test_backend_without_window_capture(scene):
    be, d, _ = scene
    be.window_capture = False
    assert CAP_WINDOW_CAPTURE not in be.capabilities
    w = d.window("Target")
    assert w.screenshot().getpixel((100, 100)) == RED  # compatible: screen grab
    with pytest.raises(NotSupported):
        w.screenshot(mode="window")
    w.minimize()
    with pytest.raises(NotSupported, match="minimized"):
        w.screenshot(restore_minimized=True)


def test_screenshot_mode_validation_and_vanished_window(scene):
    be, d, target = scene
    w = d.window("Target")
    with pytest.raises(ValueError, match="mode"):
        w.screenshot(mode="bogus")
    be.remove_window(target)
    with pytest.raises(WindowNotFound):
        w.screenshot()


# ============================================================ observe
def test_observe_covered_window_shows_its_own_content(scene, monkeypatch):
    import locum.vision as vision

    be, d, _ = scene
    boxes: List[Any] = []
    real = vision.annotate

    def spy(img, bxs, **kw):
        boxes.extend(bxs)
        return real(img, bxs, **kw)

    monkeypatch.setattr(vision, "annotate", spy)
    obs = observe(d, window="Target", screenshot=True, max_long_edge=10000)
    assert obs.screenshot.size == (300, 200) and obs.scale == 1.0
    assert obs.screenshot.getpixel((100, 100)) == GREEN
    assert any("captured off-screen" in n for n in obs.notes)
    assert not any("blanked" in n for n in obs.notes)
    hidden = next(e for e in obs.elements if e.name == "Hidden")
    assert hidden.occluded  # clicks still go through the native action
    assert any(label == str(hidden.id) for _, label in boxes)  # but it is drawn: it is visible
    assert "Cover" not in obs.text()


def test_observe_falls_back_to_blanking(scene):
    be, d, target = scene
    target.capture_blank = True
    obs = observe(d, window="Target", screenshot=True, max_long_edge=10000)
    assert obs.screenshot.getpixel((100, 100)) == (96, 96, 96)
    assert any("blanked" in n for n in obs.notes)


def test_observe_uncovered_window_unchanged(scene):
    be, d, _ = scene
    be.clear_events()
    obs = observe(d, window="Cover", screenshot=True, max_long_edge=10000)
    assert not _events(be, "capture_window")
    assert not any("off-screen" in n for n in obs.notes)


# ============================================================ CLI
def _run(argv, desktop):
    from locum.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_screenshot_window(scene, tmp_path):
    from locum.cli import EXIT_OK, EXIT_USAGE

    be, d, _ = scene
    out = tmp_path / "t.png"
    code, text, _ = _run(["--json", "screenshot", "--window", "Target", "-o", str(out)], d)
    assert code == EXIT_OK
    data = json.loads(text)
    assert data["capture"] == "window" and (data["width"], data["height"]) == (300, 200)
    assert data["capture_reason"] == "covered" and data["clipped"] is False
    assert Image.open(out).convert("RGB").getpixel((100, 100)) == GREEN
    code, text, err = _run(["screenshot", "--window", "Target", "-o", str(out)], d)
    assert code == EXIT_OK and text.startswith("saved 300x200 screenshot")
    # A good capture never reads like a failure: it says why the window's own content was used.
    assert "off-screen" not in text and "another window covers part of it" in err
    code, text, _ = _run(["--json", "screenshot", "--window", "Target", "--mode", "screen", "-o", str(out)], d)
    data = json.loads(text)
    assert code == EXIT_OK and data["capture"] == "screen" and data["capture_reason"] == "requested"
    assert Image.open(out).convert("RGB").getpixel((100, 100)) == RED
    d.window("Target").minimize()
    code, text, err = _run(["screenshot", "--window", "Target", "--restore-minimized", "-o", str(out)], d)
    assert code == EXIT_OK and "it is minimized: captured the window's own content" in err
    assert _run(["screenshot", "--window", "Target", "--region", "0,0,5,5"], d)[0] == EXIT_USAGE
    assert _run(["screenshot", "--mode", "window"], d)[0] == EXIT_USAGE


# ============================================================ Windows: PrintWindow (fake Win32)
from locum.backends.windows import WindowsBackend  # noqa: E402
from locum.backends.windows import _win32api as w32  # noqa: E402


def _deref(arg: Any) -> Any:
    return getattr(arg, "_obj", arg)


def _bgrx(w: int, h: int, color_at) -> bytes:
    out = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            r, g, b = color_at(x, y)
            i = (y * w + x) * 4
            out[i:i + 4] = bytes((b, g, r, 0xEE))  # garbage alpha, like PrintWindow leaves
    return bytes(out)


class FakeCaptureApi:
    """The Win32 subset capture_window uses, with GDI objects backed by real buffers."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.frame = Rect(100, 100, 40, 30)
        self.border = (7, 0, 7, 7)  # invisible DWM borders l, t, r, b
        self.iconic = False
        self.parked = False
        self.alive = True
        self.awareness = 2  # per-monitor aware
        self.monitor_dpi = 96
        self.painters: Dict[int, Any] = {}  # flags -> fn(w, h) -> bytes | None (failure)
        self.bitmaps: Dict[int, Any] = {}
        self.selected: Dict[int, int] = {}
        self.live = {"dc": 0, "bmp": 0, "screen_dc": 0}
        self._next = 0x5000
        self.thread_ctx = 0x11

    # -- generic
    def get_last_error(self) -> int:
        return 0

    @staticmethod
    def format_error(code: int) -> str:
        return f"fake error {code}"

    def _id(self) -> int:
        self._next += 1
        return self._next

    # -- windows
    def IsWindow(self, hwnd):
        return int(self.alive and hwnd == 0x100)

    def IsIconic(self, hwnd):
        return int(self.iconic)

    @property
    def outer(self) -> Rect:
        if self.iconic and self.parked:
            return Rect(-32000, -32000, 160, 28)
        l, t, r, b = self.border
        f = self.frame
        return Rect.from_ltrb(f.left - l, f.top - t, f.right + r, f.bottom + b)

    def GetWindowRect(self, hwnd, rc):
        o, r = self.outer, _deref(rc)
        r.left, r.top, r.right, r.bottom = o.left, o.top, o.right, o.bottom
        return 1

    def DwmGetWindowAttribute(self, hwnd, attr, out, size):
        obj = _deref(out)
        if attr == w32.DWMWA_EXTENDED_FRAME_BOUNDS:
            f = Rect(-32000, -32000, 160, 28) if self.iconic and self.parked else self.frame
            obj.left, obj.top, obj.right, obj.bottom = f.left, f.top, f.right, f.bottom
            return 0
        obj.value = 0
        return 0

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", cmd))
        if cmd == w32.SW_SHOWNOACTIVATE:
            self.iconic = False
        elif cmd in (w32.SW_SHOWMINNOACTIVE, w32.SW_MINIMIZE):
            self.iconic = True
        return 1

    def SetThreadDpiAwarenessContext(self, ctx):
        self.calls.append(("SetThreadDpiAwarenessContext", ctx.value))
        old, self.thread_ctx = self.thread_ctx, ctx.value
        return old

    def GetWindowDpiAwarenessContext(self, hwnd):
        return 0x22

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        assert ctx == 0x22
        return self.awareness

    def MonitorFromWindow(self, hwnd, flags):
        assert flags == w32.MONITOR_DEFAULTTONEAREST
        return 0x1

    def GetDpiForMonitor(self, hmon, kind, px, py):
        _deref(px).value = self.monitor_dpi
        _deref(py).value = self.monitor_dpi
        return 0

    # -- GDI
    def GetDC(self, hwnd):
        assert hwnd is None
        self.live["screen_dc"] += 1
        return 0x77

    def ReleaseDC(self, hwnd, hdc):
        self.live["screen_dc"] -= 1
        return 1

    def CreateCompatibleDC(self, hdc):
        self.live["dc"] += 1
        return self._id()

    def DeleteDC(self, hdc):
        self.live["dc"] -= 1
        return 1

    def CreateDIBSection(self, hdc, pbmi, usage, ppv, section, offset):
        bmi = _deref(pbmi)
        hdr = bmi.bmiHeader
        assert usage == w32.DIB_RGB_COLORS and hdr.biBitCount == 32 and hdr.biHeight < 0
        w, h = hdr.biWidth, -hdr.biHeight
        buf = ctypes.create_string_buffer(w * h * 4)
        hbm = self._id()
        self.bitmaps[hbm] = (buf, w, h)
        _deref(ppv).value = ctypes.addressof(buf)
        self.live["bmp"] += 1
        return hbm

    def SelectObject(self, hdc, obj):
        old = self.selected.get(hdc, 0x9999)
        self.selected[hdc] = obj
        return old

    def DeleteObject(self, obj):
        self.live["bmp"] -= 1
        self.bitmaps.pop(obj, None)
        return 1

    def GdiFlush(self):
        self.calls.append(("GdiFlush",))
        return 1

    def PrintWindow(self, hwnd, hdc, flags):
        self.calls.append(("PrintWindow", flags))
        buf, w, h = self.bitmaps[self.selected[hdc]]
        painter = self.painters.get(flags)
        data = painter(w, h) if painter is not None else None
        if data is None:
            return 0
        ctypes.memmove(buf, data, len(data))
        return 1


def _frame_painter(api: FakeCaptureApi, inside=GREEN, border=RED):
    """Paints the visible frame ``inside`` (with a blue top-left pixel) and the invisible borders ``border``."""
    l, t, _, _ = api.border

    def paint(w, h):
        fw, fh = api.frame.width, api.frame.height

        def color(x, y):
            fx, fy = x - l, y - t
            if 0 <= fx < fw and 0 <= fy < fh:
                return BLUE if (fx, fy) == (0, 0) else inside
            return border

        return _bgrx(w, h, color)

    return paint


def _black(w, h):
    return bytes(w * h * 4)


@pytest.fixture
def wapi():
    return FakeCaptureApi()


@pytest.fixture
def wbe(wapi):
    sleeps: List[float] = []
    be = WindowsBackend(_api=wapi, dpi_aware=False, accessibility=False, sleep=sleeps.append)
    be.sleeps = sleeps
    return be


def _flags(api: FakeCaptureApi) -> List[int]:
    return [c[1] for c in api.calls if c[0] == "PrintWindow"]


def test_win_capability_follows_printwindow(wapi):
    be = WindowsBackend(_api=wapi, dpi_aware=False, accessibility=False)
    assert CAP_WINDOW_CAPTURE in be.capabilities

    class NoPrintWindow(FakeCaptureApi):
        PrintWindow = None  # an optional Win32 function the API table could not bind

    be2 = WindowsBackend(_api=NoPrintWindow(), dpi_aware=False, accessibility=False)
    assert CAP_WINDOW_CAPTURE not in be2.capabilities
    with pytest.raises(NotSupported, match="PrintWindow"):
        be2.capture_window(0x100)


def test_win_capture_crops_to_the_dwm_frame_and_converts_bgrx(wbe, wapi):
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(wapi)
    img = wbe.capture_window(0x100)
    assert img.mode == "RGB" and img.size == (40, 30)
    assert img.getpixel((0, 0)) == BLUE and img.getpixel((39, 29)) == GREEN
    assert _flags(wapi) == [w32.PW_RENDERFULLCONTENT]  # the DWM path worked: no classic print
    assert ("GdiFlush",) in wapi.calls
    assert wapi.live == {"dc": 0, "bmp": 0, "screen_dc": 0}  # every GDI object released


def test_win_capture_switches_the_thread_to_per_monitor_dpi_and_back(wbe, wapi):
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(wapi)
    wbe.capture_window(0x100)
    ctx = [c[1] for c in wapi.calls if c[0] == "SetThreadDpiAwarenessContext"]
    assert ctx == [ctypes.c_void_p(w32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).value, 0x11]
    assert wapi.thread_ctx == 0x11


def test_win_blank_full_content_falls_back_to_classic_print(wbe, wapi):
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _black  # a GPU surface DWM could not read
    wapi.painters[0] = _frame_painter(wapi)
    img = wbe.capture_window(0x100)
    assert img.getpixel((5, 5)) == GREEN
    assert _flags(wapi) == [w32.PW_RENDERFULLCONTENT, 0]


def test_win_blank_or_failed_capture_raises_not_supported(wbe, wapi):
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _black
    wapi.painters[0] = _black
    with pytest.raises(NotSupported, match="blank"):
        wbe.capture_window(0x100)
    wapi.painters.clear()  # PrintWindow returns FALSE
    with pytest.raises(NotSupported, match="failed"):
        wbe.capture_window(0x100)
    assert wapi.live == {"dc": 0, "bmp": 0, "screen_dc": 0}


def test_win_classic_print_of_dpi_unaware_window_is_scaled_up(wbe, wapi):
    wapi.awareness = w32.DPI_AWARENESS_UNAWARE
    wapi.monitor_dpi = 144  # 150 %
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _black

    def logical(w, h):  # an unaware window paints at its logical size, top-left
        lw, lh = round(w / 1.5), round(h / 1.5)
        return _bgrx(w, h, lambda x, y: (BLUE if (x, y) == (10, 2) else GREEN) if x < lw and y < lh else (0, 0, 0))

    wapi.painters[0] = logical
    img = wbe.capture_window(0x100)
    assert img.size == (40, 30)
    assert img.getpixel((38, 28)) == GREEN  # would be black without the upscale
    wapi.awareness = 2  # an aware window is left alone
    img = wbe.capture_window(0x100)
    assert img.getpixel((38, 28)) == (0, 0, 0)


def test_win_minimized_without_restore_raises_and_touches_nothing(wbe, wapi):
    wapi.iconic = wapi.parked = True
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(wapi)
    with pytest.raises(NotSupported, match="restore_minimized=True"):
        wbe.capture_window(0x100)
    assert not [c for c in wapi.calls if c[0] in ("ShowWindow", "PrintWindow")]


def test_win_minimized_restore_shows_without_activation_and_reminimizes(wbe, wapi):
    wapi.iconic = wapi.parked = True
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(wapi)
    img = wbe.capture_window(0x100, restore_minimized=True)
    assert img.size == (40, 30) and img.getpixel((5, 5)) == GREEN
    shows = [c[1] for c in wapi.calls if c[0] == "ShowWindow"]
    assert shows == [w32.SW_SHOWNOACTIVATE, w32.SW_SHOWMINNOACTIVE]
    assert wapi.iconic  # minimized again
    assert wbe.sleeps == [wbe.RESTORE_SETTLE]
    order = [c[0] for c in wapi.calls if c[0] in ("ShowWindow", "PrintWindow")]
    assert order == ["ShowWindow", "PrintWindow", "ShowWindow"]


def test_win_minimized_restore_reminimizes_even_when_capture_fails(wbe, wapi):
    wapi.iconic = wapi.parked = True
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _black
    wapi.painters[0] = _black
    with pytest.raises(NotSupported, match="blank"):
        wbe.capture_window(0x100, restore_minimized=True)
    assert wapi.iconic
    assert [c[1] for c in wapi.calls if c[0] == "ShowWindow"] == [w32.SW_SHOWNOACTIVATE, w32.SW_SHOWMINNOACTIVE]


def test_win_minimized_but_not_parked_tries_a_capture_first(wbe, wapi):
    wapi.iconic, wapi.parked = True, False  # a shell that keeps the window's rect
    wapi.frame = Rect(100, 100, 240, 60)  # bigger than a caption-size placeholder
    wapi.painters[w32.PW_RENDERFULLCONTENT] = _frame_painter(wapi)
    img = wbe.capture_window(0x100, restore_minimized=True)
    assert img.getpixel((5, 5)) == GREEN
    assert not [c for c in wapi.calls if c[0] == "ShowWindow"]


def test_win_invalid_handle(wbe, wapi):
    with pytest.raises(WindowNotFound):
        wbe.capture_window(0x999)


# ============================================================ macOS: CGWindowListCreateImage (fake Quartz)
def _mac_backend():
    from test_macos_backend import _rec, make_backend

    be, q, _ = make_backend()
    q.windows = [_rec(42, 100, 10, 20, 100, 50, "Doc", owner="TextEdit")]
    q.windows[0]["kCGWindowIsOnscreen"] = True
    q.CGRectNull = "CGRectNull"
    q.calls = []
    q.image_size = (200, 100)
    q.image_color = (1, 2, 3)

    def create(area, option, num, flags):
        q.calls.append((area, option, num, flags))
        if q.image_size is None:
            return None
        return SimpleNamespace(size=q.image_size)

    def data(img):  # BGRA, first pixel blue (a real window is never one flat color)
        w, h = img.size
        px = bytes((q.image_color[2], q.image_color[1], q.image_color[0], 0xFF))
        return (bytes((200, 10, 10, 0xFF)) if any(q.image_color) else px) + px * (w * h - 1)

    q.CGWindowListCreateImage = create
    q.CGImageGetWidth = lambda img: img.size[0]
    q.CGImageGetHeight = lambda img: img.size[1]
    q.CGImageGetBytesPerRow = lambda img: img.size[0] * 4
    q.CGImageGetDataProvider = lambda img: img
    q.CGDataProviderCopyData = data
    return be, q


def test_mac_capture_window_uses_the_window_id():
    from locum.backends import macos

    be, q = _mac_backend()
    assert CAP_WINDOW_CAPTURE in be.capabilities
    img = be.capture_window(42)
    assert img.size == (200, 100) and img.getpixel((5, 5)) == (1, 2, 3)  # 2x display: points -> pixels
    area, option, num, flags = q.calls[-1]
    assert area == "CGRectNull" and num == 42
    assert option == macos.kCGWindowListOptionIncludingWindow
    assert flags == macos.kCGWindowImageBoundsIgnoreFraming | macos.kCGWindowImageBestResolution


def test_mac_capture_window_rescales_to_the_pixel_rect():
    be, q = _mac_backend()
    q.image_size = (100, 50)  # nominal resolution came back
    assert be.capture_window(42).size == (200, 100)


def test_mac_capture_window_blank_or_missing_raises():
    be, q = _mac_backend()
    q.image_size = None
    with pytest.raises(NotSupported, match="blank"):
        be.capture_window(42)
    q.image_size = (200, 100)
    q.image_color = (0, 0, 0)
    with pytest.raises(NotSupported):
        be.capture_window(42)
    del q.windows[0]["kCGWindowIsOnscreen"]  # minimized / another Space
    with pytest.raises(NotSupported, match="minimized"):
        be.capture_window(42, restore_minimized=True)
    with pytest.raises(WindowNotFound):
        be.capture_window(4242)


# ============================================================ X11: Composite pixmap (fake Xlib)
class FakeXError(Exception):
    pass


class FakeXWin:
    def __init__(self, xid, geom, parent=None, mapped=True):
        self.id = xid
        self.geom = geom  # root-relative Rect (no X border)
        self.parent = parent
        self.mapped = mapped
        self.pixmap_ok = True
        self.paint = None

    def query_tree(self):
        return SimpleNamespace(parent=self.parent if self.parent is not None else 0)

    def get_geometry(self):
        return SimpleNamespace(width=self.geom.width, height=self.geom.height, border_width=0)

    def get_attributes(self):
        return SimpleNamespace(map_state=2 if self.mapped else 0)

    def composite_name_window_pixmap(self):
        win = self

        class Pix:
            freed = False

            def get_image(self, x, y, w, h, fmt, mask):
                if not win.pixmap_ok:
                    raise FakeXError("BadMatch")
                return SimpleNamespace(depth=24, data=win.paint(w, h))

            def free(self):
                Pix.freed = True

        return Pix()


def _x11_backend(composite=True, cm=True):
    from locum.backends.x11 import X11Backend

    be = object.__new__(X11Backend)
    root = FakeXWin(1, Rect(0, 0, 1920, 1080))
    frame = FakeXWin(0x200, Rect(90, 90, 124, 142), parent=root)  # WM frame with a 10px shadow
    client = FakeXWin(0x300, Rect(102, 120, 100, 100), parent=frame)
    be._state = SimpleNamespace(closed=False, desynced=False)
    be._lock = threading.RLock()
    be._xl = SimpleNamespace(X=SimpleNamespace(IsViewable=2, ZPixmap=2, MSBFirst=1), error=SimpleNamespace(XError=FakeXError))
    be._root = root
    be._root.translate_coords = lambda win, x, y: SimpleNamespace(x=win.geom.left + x, y=win.geom.top + y)
    be._d = SimpleNamespace(
        has_extension=lambda name: composite and name == "Composite",
        get_default_screen=lambda: 0,
        intern_atom=lambda name: 77 if name == "_NET_WM_CM_S0" else 0,
        get_selection_owner=lambda atom: SimpleNamespace(id=0x500 if (atom == 77 and cm) else 0),
        display=SimpleNamespace(info=SimpleNamespace(pixmap_formats=[SimpleNamespace(depth=24, bits_per_pixel=32)],
                                                     image_byte_order=0)),
    )
    be._handle = lambda h: client
    be._prop_list = lambda win, name: [2, 2, 20, 2] if name == "_NET_FRAME_EXTENTS" else []
    want = Rect(100, 100, 104, 122)  # client + frame extents (l, r, t, b)

    def paint(w, h):
        def color(x, y):
            if (90 + x, 90 + y) == (100, 100):
                return BLUE  # top-left of the wanted rect
            return GREEN if want.contains((90 + x, 90 + y)) else RED

        return _bgrx(w, h, color)

    frame.paint = paint
    return be, frame, client


def _x11_capture(be, handle=0x300, **kw):
    from locum.backends.x11 import X11Backend

    return X11Backend.capture_window.__wrapped__(be, handle, **kw)  # without the python-xlib error wrapper


def test_x11_capture_window_crops_the_frame_pixmap():
    be, frame, client = _x11_backend()
    assert CAP_WINDOW_CAPTURE in type(be).capabilities.fget(SimpleNamespace(
        _xtest=False, _clip_cmds=None, _clip_mode="none", _atspi=lambda: None, _has_composite=be._has_composite))
    img = _x11_capture(be)
    assert img.size == (104, 122)
    assert img.getpixel((0, 0)) == BLUE and img.getpixel((1, 1)) == GREEN and img.getpixel((103, 121)) == GREEN


def test_x11_capture_window_not_supported_cases():
    be, frame, client = _x11_backend()
    frame.pixmap_ok = False  # not redirected: no compositing manager
    with pytest.raises(NotSupported, match="compositing manager"):
        _x11_capture(be)
    frame.pixmap_ok = True
    client.mapped = False  # minimized
    with pytest.raises(NotSupported, match="not mapped"):
        _x11_capture(be, restore_minimized=True)
    be2, _, _ = _x11_backend(composite=False)
    with pytest.raises(NotSupported, match="Composite"):
        _x11_capture(be2)


def test_x11_window_capture_needs_a_running_compositing_manager_not_just_the_extension():
    # Xvfb has the Composite extension but, without a compositing manager, no window
    # pixmaps: claiming the capability there made every capture fail (real-desktop CI).
    assert _x11_backend()[0]._has_composite() is True
    assert _x11_backend(cm=False)[0]._has_composite() is False
    assert _x11_backend(composite=False)[0]._has_composite() is False
