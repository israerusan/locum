"""Tests for the PyAutoGUI compatibility layer (pypilot.compat.pyautogui) on the MockBackend."""

from __future__ import annotations

import importlib
import inspect
import sys

import numpy as np
import pytest
from PIL import Image

import pypilot.compat.pyautogui as pg
from pypilot.backends.mock import MockBackend
from pypilot.compat import install_pyautogui_shim, uninstall_pyautogui_shim
from pypilot.desktop import Desktop
from pypilot.errors import FailSafeTriggered, ImageNotFound, PypilotError


def noise(w: int, h: int, seed: int = 1) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8))


@pytest.fixture
def env():
    """Fresh mock desktop + reset module settings; PAUSE sleeps are recorded, not slept."""
    be = MockBackend(width=800, height=600)
    win = be.add_window("Editor", (100, 100, 400, 300), app="editor")
    btn = win.add_button("Go", (20, 220, 80, 30))
    edit = win.add_edit("Text", (20, 40, 200, 28))
    edit2 = win.add_edit("Other", (20, 90, 200, 28))
    d = Desktop(be, sleep=lambda s: None, pause=0.0, default_timeout=0.0)
    saved = {k: getattr(pg, k) for k in ("PAUSE", "FAILSAFE", "FAILSAFE_POINTS", "USE_IMAGE_NOT_FOUND_EXCEPTION", "_SLEEP")}
    pauses = []
    pg._SLEEP = pauses.append
    pg.PAUSE = 0.0
    pg.FAILSAFE = True
    pg.FAILSAFE_POINTS = [(0, 0)]
    pg.USE_IMAGE_NOT_FOUND_EXCEPTION = True
    old = pg.set_desktop(d)
    be.mouse_move(400, 300)
    be.clear_events()
    try:
        yield {"be": be, "d": d, "win": win, "btn": btn, "edit": edit, "edit2": edit2, "pauses": pauses}
    finally:
        pg.set_desktop(old)
        for k, v in saved.items():
            setattr(pg, k, v)


def moves(be):
    return [e for e in be.events if e[0] == "move"]


# ----------------------------------------------------------------- signatures
def test_signatures_match_pyautogui():
    def params(fn):
        return list(inspect.signature(fn).parameters)

    assert params(pg.moveTo) == ["x", "y", "duration", "tween", "logScreenshot", "_pause"]
    assert params(pg.click) == ["x", "y", "clicks", "interval", "button", "duration", "tween", "logScreenshot", "_pause"]
    assert params(pg.dragTo)[:5] == ["x", "y", "duration", "tween", "button"]
    assert params(pg.press) == ["keys", "presses", "interval", "logScreenshot", "_pause"]
    assert params(pg.write) == ["message", "interval", "logScreenshot", "_pause"]
    assert params(pg.scroll) == ["clicks", "x", "y", "logScreenshot", "_pause"]
    assert inspect.signature(pg.moveTo).parameters["tween"].default is pg.linear
    assert inspect.signature(pg.click).parameters["button"].default == "primary"
    assert pg.move is pg.moveRel and pg.drag is pg.dragRel and pg.typewrite is pg.write
    assert pg.FailSafeException is FailSafeTriggered
    assert pg.ImageNotFoundException is ImageNotFound
    assert issubclass(pg.FailSafeException, pg.PyAutoGUIException)
    for name in pg.__all__:
        assert hasattr(pg, name), name


# ---------------------------------------------------------------------- mouse
def test_screen_queries(env):
    assert pg.size() == (800, 600) and pg.size().width == 800
    assert pg.resolution() == pg.size()
    assert pg.position() == (400, 300)
    assert pg.position(x=5) == (5, 300)
    assert pg.onScreen(10, 10) and pg.onScreen((799, 599))
    assert not pg.onScreen(800, 10)


def test_move_to_and_rel(env):
    be = env["be"]
    pg.moveTo(100, 200)
    assert pg.position() == (100, 200)
    pg.moveTo(None, 250)
    assert pg.position() == (100, 250)
    pg.moveTo(x=150)
    assert pg.position() == (150, 250)
    pg.moveTo((30, 40))
    assert pg.position() == (30, 40)
    pg.moveTo((10, 10, 20, 30))  # 4-tuple -> center
    assert pg.position() == (20, 25)
    pg.moveRel(5, -5)
    assert pg.position() == (25, 20)
    pg.move(None, 10)
    assert pg.position() == (25, 30)
    pg.moveRel((1, 1))
    assert pg.position() == (26, 31)
    be.clear_events()
    pg.moveTo(500, 400, duration=0.5, tween=pg.easeInOutQuad)
    assert len(moves(be)) > 10 and pg.position() == (500, 400)
    be.clear_events()
    pg.moveTo(300, 300, duration=0.05)  # below MINIMUM_DURATION: instant
    assert len(moves(be)) == 1


def test_click_variants(env):
    be, btn = env["be"], env["btn"]
    c = (100 + 20 + 40, 100 + 220 + 15)
    pg.click(*c)
    assert btn.clicks == 1
    pg.click(c, clicks=2, interval=0.01)
    assert btn.clicks == 3
    pg.click(button=pg.PRIMARY)
    assert btn.clicks == 4
    pg.leftClick(*c)
    pg.doubleClick(*c)
    assert btn.clicks == 7
    pg.tripleClick(*c)
    assert btn.clicks == 10
    be.clear_events()
    pg.rightClick(10, 10)
    pg.middleClick(10, 10)
    pg.click(10, 10, button=3)
    pg.click(10, 10, button="secondary")
    assert [e[1] for e in be.events if e[0] == "down"] == ["right", "middle", "right", "right"]
    with pytest.raises(pg.PyAutoGUIException):
        pg.click(10, 10, button="sideways")
    pg.click(10, 10, duration=0.3)
    assert pg.position() == (10, 10)


def test_mouse_down_up_drag_scroll(env):
    be = env["be"]
    pg.mouseDown(50, 60)
    assert be.pressed_buttons and pg.position() == (50, 60)
    pg.mouseUp(button="left")
    assert not be.pressed_buttons
    pg.mouseDown(button="right")
    pg.mouseUp(button="right")
    be.clear_events()
    pg.moveTo(10, 10)
    pg.dragTo(200, 150, duration=0.2, button="left")
    kinds = [e[0] for e in be.events]
    assert kinds.index("down") < kinds.index("up")
    assert be.events[-1] == ("up", "left", 200, 150)
    pg.dragRel(-50, 10)
    assert pg.position() == (150, 160)
    be.clear_events()
    pg.dragTo(170, 170, mouseDownUp=False)
    assert "down" not in [e[0] for e in be.events]
    be.clear_events()
    pg.scroll(5)
    pg.scroll(-3, x=300, y=200)
    pg.hscroll(2)
    pg.vscroll(1)
    scrolls = [e for e in be.events if e[0] == "scroll"]
    assert scrolls == [("scroll", 0, 5, 170, 170), ("scroll", 0, -3, 300, 200), ("scroll", 2, 0, 300, 200), ("scroll", 0, 1, 300, 200)]


# ------------------------------------------------------------ PAUSE / FAILSAFE
def test_pause_applied_after_each_call(env):
    pauses = env["pauses"]
    pg.PAUSE = 0.25
    pg.moveTo(100, 100)
    pg.click()
    pg.press("a")
    pg.hotkey("ctrl", "a")
    pg.write("x")
    assert pauses == [0.25] * 5
    pg.moveTo(120, 120, _pause=False)
    pg.hotkey("shift", "a", _pause=False)
    pg.click(_pause=False)
    assert len(pauses) == 5
    pg.size()
    pg.position()
    pg.screenshot()
    assert len(pauses) == 5  # queries do not pause
    with pg.hold("shift"):
        pass
    assert len(pauses) == 6


def test_failsafe(env):
    be = env["be"]
    pg.FAILSAFE = False
    pg.moveTo(0, 0)  # allowed while disabled
    pg.FAILSAFE = True
    with pytest.raises(pg.FailSafeException):
        pg.click()
    with pytest.raises(pg.FailSafeException):
        pg.press("a")
    with pytest.raises(FailSafeTriggered):
        pg.failSafeCheck()
    pg.FAILSAFE = False
    pg.click()
    pg.FAILSAFE = True
    pg.FAILSAFE_POINTS = [(5, 5)]
    pg.click()  # (0, 0) is no longer a trigger point
    pg.moveTo(5, 5, _pause=False)
    with pytest.raises(pg.FailSafeException):
        pg.moveTo(100, 100)
    assert be.mouse_position() == (5, 5)


# ------------------------------------------------------------------- keyboard
def test_write_press_keys_hold(env):
    be, edit = env["be"], env["edit"]
    pg.click(160, 154)  # focus the "Text" edit
    pg.write("Hello, World!")
    assert edit.value == "Hello, World!"
    pg.typewrite(["backspace", "backspace", "!"], interval=0.01)
    assert edit.value == "Hello, Worl!"
    pg.press("backspace", presses=3, interval=0.01)
    assert edit.value == "Hello, Wo"
    pg.press(["a", "B"])
    assert edit.value == "Hello, WoaB"
    pg.press("definitely-not-a-key")  # ignored, like PyAutoGUI
    pg.press(["kana", "x"])  # unsupported key skipped, rest pressed
    assert edit.value == "Hello, WoaBx"
    pg.keyDown("shift")
    pg.press("y")
    pg.keyUp("shift")
    assert edit.value == "Hello, WoaBxY"
    with pg.hold(["shift"]):
        pg.press("z")
    assert edit.value == "Hello, WoaBxYZ"
    assert not be.pressed_keys
    pg.keyDown("nonsense")
    pg.keyUp("nonsense")
    pg.hotkey("ctrl", "a")
    pg.press("del")
    assert edit.value == ""


def test_press_enter_activates_button_and_tab_navigation(env):
    btn, edit2 = env["btn"], env["edit2"]
    pg.click(160, 154)
    pg.press("tab")
    assert env["be"].focused_widget is edit2
    pg.press("tab")
    pg.press("enter")
    assert btn.clicks == 1
    pg.press("Return")
    assert btn.clicks == 2


def test_hotkey_copy_paste_via_clipboard(env):
    _edit, edit2 = env["edit"], env["edit2"]
    pg.click(160, 154)
    pg.write("clip me")
    pg.hotkey("ctrl", "c")
    assert env["d"].clipboard_get() == "clip me"
    pg.click(160, 204)
    pg.hotkey("ctrl", "v", interval=0.01)
    assert edit2.value == "clip me"
    pg.hotkey(["ctrl", "a"])
    pg.press("backspace")
    assert edit2.value == ""
    with pytest.raises(TypeError):
        pg.hotkey("ctrl", "c", bogus=1)


def test_key_names():
    assert pg.isValidKey("enter") and pg.isValidKey("Enter") and pg.isValidKey("a")
    assert not pg.isValidKey("nope")
    assert pg.KEY_NAMES is pg.KEYBOARD_KEYS and "f24" in pg.KEYBOARD_KEYS
    assert pg.isShiftCharacter("A") and pg.isShiftCharacter("!") and not pg.isShiftCharacter("a")
    assert pg._key("browserback") == "browser_back"
    assert pg._key("prntscrn") == "printscreen"
    assert pg._key("win") == "cmd" and pg._key("command") == "cmd"
    assert pg._key("kana") is None


# --------------------------------------------------------------- screen/locate
def test_screenshot_pixel(env, tmp_path):
    be = env["be"]
    be.paint(Image.new("RGB", (3, 3), (1, 2, 3)), (700, 500))
    img = pg.screenshot()
    assert img.size == (800, 600)
    assert pg.screenshot(region=(700, 500, 3, 3)).getpixel((1, 1)) == (1, 2, 3)
    out = tmp_path / "s.png"
    pg.screenshot(str(out), region=(0, 0, 10, 10))
    assert Image.open(out).size == (10, 10)
    assert pg.screenshot(allScreens=True).size == (800, 600)
    assert pg.pixel(701, 501) == (1, 2, 3)
    assert pg.pixel((701, 501)) == (1, 2, 3)
    assert pg.pixelMatchesColor(701, 501, (1, 2, 3))
    assert pg.pixelMatchesColor(701, 501, (2, 3, 4), tolerance=1)
    assert pg.pixelMatchesColor(701, 501, (1, 2, 3, 255))
    assert not pg.pixelMatchesColor(701, 501, (9, 9, 9))
    with pytest.raises(pg.PyAutoGUIException):
        pg.pixelMatchesColor(701, 501, (1, 2))


def test_locate_on_screen(env, tmp_path):
    be = env["be"]
    patch = noise(24, 16)
    be.paint(patch, (600, 450))
    path = tmp_path / "needle.png"
    patch.save(path)
    box = pg.locateOnScreen(str(path))
    assert box == (600, 450, 24, 16)
    assert (box.left, box.top, box.width, box.height) == (600, 450, 24, 16)
    assert type(box).__name__ == "Box"
    assert pg.locateOnScreen(patch, region=(500, 400, 200, 100), grayscale=True) == (600, 450, 24, 16)
    pt = pg.locateCenterOnScreen(str(path))
    assert pt == (612, 458) and pt.x == 612
    assert pg.center(box) == (612, 458)
    assert pg.center((0, 0, 5, 5)) == (2, 2)
    other = noise(24, 16, seed=2)
    with pytest.raises(pg.ImageNotFoundException):
        pg.locateOnScreen(other)
    with pytest.raises(pg.ImageNotFoundException):
        pg.locateCenterOnScreen(other)
    pg.USE_IMAGE_NOT_FOUND_EXCEPTION = False
    assert pg.locateOnScreen(other) is None
    assert pg.locateCenterOnScreen(other) is None
    pg.useImageNotFoundException()
    assert pg.USE_IMAGE_NOT_FOUND_EXCEPTION is True
    pg.useImageNotFoundException(False)
    assert pg.USE_IMAGE_NOT_FOUND_EXCEPTION is False


def test_locate_all_and_haystack_locate(env):
    be = env["be"]
    patch = noise(12, 12, seed=4)
    be.paint(patch, (300, 400))
    be.paint(patch, (50, 60))
    boxes = list(pg.locateAllOnScreen(patch))
    assert boxes == [(50, 60, 12, 12), (300, 400, 12, 12)]  # reading order
    assert len(list(pg.locateAllOnScreen(patch, limit=1))) == 1
    assert list(pg.locateAllOnScreen(noise(12, 12, seed=8))) == []
    hay = pg.screenshot()
    assert pg.locate(patch, hay) == (50, 60, 12, 12)
    assert list(pg.locateAll(patch, hay)) == boxes
    with pytest.raises(pg.ImageNotFoundException):
        pg.locate(noise(12, 12, seed=8), hay)


def test_click_on_image_filename(env, tmp_path):
    be = env["be"]
    patch = noise(20, 20, seed=6)
    be.paint(patch, (650, 50))
    path = tmp_path / "target.png"
    patch.save(path)
    be.clear_events()
    pg.click(str(path))
    assert ("down", "left", 660, 60) in be.events
    pg.moveTo(path)
    assert pg.position() == (660, 60)
    with pytest.raises(pg.ImageNotFoundException):
        pg.click(noise(20, 20, seed=7))
    pg.USE_IMAGE_NOT_FOUND_EXCEPTION = False
    with pytest.raises(pg.ImageNotFoundException):  # moving to "nowhere" still cannot succeed
        pg.moveTo(noise(20, 20, seed=7))


# ------------------------------------------------------------------- windows
def test_window_functions(env):
    be = env["be"]
    be.add_window("Terminal", (500, 300, 200, 150), app="term", activate=True)
    assert pg.getAllTitles() == ["Terminal", "Editor"]
    assert pg.getActiveWindowTitle() == "Terminal"
    eds = pg.getWindowsWithTitle("EDIT")
    assert len(eds) == 1
    w = eds[0]
    assert (w.left, w.top, w.width, w.height) == (100, 100, 400, 300)
    assert w.box == (100, 100, 400, 300) and w.size == (400, 300) and w.topleft == (100, 100)
    assert w.right == 500 and w.bottom == 400 and w.center == (300, 250)
    assert not w.isActive
    w.activate()
    assert w.isActive and pg.getActiveWindow() == w
    w.moveTo(10, 20)
    assert (w.left, w.top) == (10, 20)
    w.moveRel(5, 5)
    w.resizeTo(300, 200)
    w.resizeRel(10, 10)
    assert w.box == (15, 25, 310, 210)
    w.minimize()
    assert w.isMinimized
    w.restore()
    w.maximize()
    assert w.isMaximized
    assert [x.title for x in pg.getWindowsAt(600, 350)] == ["Editor", "Terminal"]
    assert "Editor" in repr(w)
    assert len(pg.getAllWindows()) == 2
    t = pg.getWindowsWithTitle("terminal")[0]
    t.close()
    assert pg.getAllTitles() == ["Editor"]
    assert w.window.title == "Editor"


def test_locate_on_window(env):
    be = env["be"]
    patch = noise(10, 10, seed=9)
    be.paint(patch, (150, 300))
    assert pg.locateOnWindow(patch, "edit") == (150, 300, 10, 10)
    with pytest.raises(pg.PyAutoGUIException):
        pg.locateOnWindow(patch, "nonexistent window")


# --------------------------------------------------------------------- tweens
TWEENS = [
    "linear", "easeInQuad", "easeOutQuad", "easeInOutQuad", "easeInCubic", "easeOutCubic", "easeInOutCubic",
    "easeInQuart", "easeOutQuart", "easeInOutQuart", "easeInQuint", "easeOutQuint", "easeInOutQuint",
    "easeInSine", "easeOutSine", "easeInOutSine", "easeInExpo", "easeOutExpo", "easeInOutExpo",
    "easeInCirc", "easeOutCirc", "easeInOutCirc", "easeInElastic", "easeOutElastic", "easeInOutElastic",
    "easeInBack", "easeOutBack", "easeInOutBack", "easeInBounce", "easeOutBounce", "easeInOutBounce",
]


@pytest.mark.parametrize("name", TWEENS)
def test_tween_endpoints(name):
    fn = getattr(pg, name)
    assert fn(0.0) == pytest.approx(0.0, abs=1e-9)
    assert fn(1.0) == pytest.approx(1.0, abs=1e-9)
    if name.startswith("easeInOut") or name == "linear":
        assert fn(0.5) == pytest.approx(0.5, abs=1e-9)


def test_get_point_on_line():
    assert pg.getPointOnLine(0, 0, 10, 20, 0.5) == (5, 10)


# ---------------------------------------------------------------------- shim
def test_install_shim():
    name = "pyautogui_pypilot_test_shim"
    assert install_pyautogui_shim(name) is pg
    assert importlib.import_module(name) is pg
    assert uninstall_pyautogui_shim(name) is True
    assert name not in sys.modules
    assert uninstall_pyautogui_shim(name) is False
    other = type(sys)("placeholder")
    sys.modules[name] = other
    try:
        assert install_pyautogui_shim(name) is other  # never clobbers a real module by default
        assert install_pyautogui_shim(name, force=True) is pg
    finally:
        sys.modules.pop(name, None)


def test_default_desktop_is_created_lazily(monkeypatch):
    from pypilot import backends

    old = pg.set_desktop(None)
    old_backend = backends._default
    monkeypatch.setenv("PYPILOT_BACKEND", "mock")
    backends.set_backend(None)
    try:
        d = pg.get_desktop()
        assert isinstance(d.backend, MockBackend)
        assert d.pause == 0.0 and d.humanize is None
        assert pg.get_desktop() is d
        assert isinstance(pg.position(), tuple)
    finally:
        pg.set_desktop(old)
        backends.set_backend(old_backend)


def test_errors_are_pypilot_errors():
    assert issubclass(pg.ImageNotFoundException, PypilotError)
