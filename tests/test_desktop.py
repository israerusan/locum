"""Tests for the Desktop facade, end-to-end on the MockBackend."""

from __future__ import annotations


import numpy as np
import pytest
from PIL import Image

from pypilot import failsafe as fs_mod
from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.element import Element
from pypilot.errors import (
    ActionTimeout,
    ElementNotFound,
    FailSafeTriggered,
    ImageNotFound,
    NotSupported,
    SelectorSyntaxError,
    WindowNotFound,
)
from pypilot.motion import Humanize
from pypilot.ocr import OCRWord
from pypilot.types import Match, Point, Rect
from pypilot.window import Window


class FakeTime:
    """A clock that only advances when slept on; optional hooks run on each sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = []
        self.hooks = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s
        for h in list(self.hooks):
            h()


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def be():
    return MockBackend(width=1280, height=800)


@pytest.fixture
def d(be, ft):
    desk = Desktop(be, sleep=ft.sleep, clock=ft.clock, default_timeout=2.0)
    yield desk
    fs_mod.reset_abort()


@pytest.fixture
def app(be):
    w = be.add_window("Invoice Editor", (100, 100, 500, 400), app="erp")
    ok = w.add_button("OK", (20, 340, 80, 30), automation_id="okBtn")
    cancel = w.add_button("Cancel", (120, 340, 80, 30))
    name = w.add_edit("Name", (20, 50, 200, 28))
    notes = w.add_edit("Notes", (20, 100, 200, 28))
    chk = w.add_checkbox("Paid", (20, 150, 120, 24))
    return {"win": w, "ok": ok, "cancel": cancel, "name": name, "notes": notes, "chk": chk}


def noise(w: int, h: int, seed: int = 1) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8), "RGB")


# --------------------------------------------------------------- construction
def test_construction_options(be):
    d = Desktop(be, humanize=True, pause=0.2, failsafe=False, typing_mode="keys")
    assert d.backend is be
    assert isinstance(d.humanize, Humanize)
    assert d.mouse.humanize is d.humanize
    assert d.mouse.pause == d.keyboard.pause == 0.2
    assert d.failsafe.enabled is False
    assert d.typing_mode == "keys"
    d.pause = 0.5
    assert d.mouse.pause == d.keyboard.pause == d.pause == 0.5
    h = Humanize(seed=3)
    assert Desktop(be, humanize=h).humanize is h
    assert Desktop(be).humanize is None


def test_construction_validation(be):
    with pytest.raises(ValueError):
        Desktop(be, typing_mode="telepathy")
    with pytest.raises(ValueError):
        Desktop(be, default_timeout=-1)
    with pytest.raises(TypeError):
        Desktop(be, humanize="yes")


def test_backend_by_name_is_owned_and_closed():
    d = Desktop("mock")
    assert isinstance(d.backend, MockBackend)
    assert d._owns_backend
    d.close()
    d.close()  # idempotent


def test_context_manager_releases_keys(be, ft):
    with Desktop(be, sleep=ft.sleep, clock=ft.clock) as d:
        d.key_down("shift")
        assert "shift" in be.pressed_keys
    assert "shift" not in be.pressed_keys


def test_repr_and_capabilities(d):
    assert "mock" in repr(d)
    assert d.has("mouse")
    assert "accessibility" in d.capabilities


# ------------------------------------------------------------ resolve_point
def test_resolve_point_basic_forms(d, be):
    be.mouse_move(50, 60)
    assert d.resolve_point() == Point(50, 60)
    assert d.resolve_point(None, 99) == Point(50, 99)
    assert d.resolve_point(10, 20) == Point(10, 20)
    assert d.resolve_point(10.6, 20.2) == Point(11, 20)
    assert d.resolve_point(7) == Point(7, 60)  # lone x keeps current y
    assert d.resolve_point((3, 4)) == Point(3, 4)
    assert d.resolve_point([3, 4]) == Point(3, 4)
    assert d.resolve_point(Point(5, 6)) == Point(5, 6)
    assert d.resolve_point({"x": 8, "y": 9}) == Point(8, 9)
    assert d.resolve_point(Rect(0, 0, 10, 20)) == Point(5, 10)
    assert d.resolve_point(Match(Rect(10, 10, 4, 4), 0.99)) == Point(12, 12)
    assert d.resolve_point("120, 340") == Point(120, 340)
    with pytest.raises(TypeError):
        d.resolve_point((1, 2), 3)
    with pytest.raises(TypeError):
        d.resolve_point(True)


def test_resolve_point_element_window_selector(d, app):
    ok_center = app["ok"].screen_rect.center
    assert d.resolve_point('button "OK"') == ok_center
    assert d.resolve_point("#okBtn") == ok_center
    el = d.find("#okBtn")
    assert d.resolve_point(el) == ok_center
    assert d.resolve_point(el.node) == ok_center
    win = d.window("Invoice")
    assert d.resolve_point(win) == Rect(100, 100, 500, 400).center


def test_resolve_point_missing_selector_raises(d, app, ft):
    with pytest.raises(ElementNotFound):
        d.resolve_point('button "Nope"')
    assert ft.now == pytest.approx(2.0)  # waited the default timeout


def test_resolve_point_images(d, be, tmp_path):
    patch = noise(30, 20)
    be.paint(patch, (400, 300))
    p = tmp_path / "patch.png"
    patch.save(p)
    assert d.resolve_point(p) == Point(415, 310)
    assert d.resolve_point(str(p)) == Point(415, 310)
    assert d.resolve_point(patch) == Point(415, 310)
    assert d.resolve_point(np.asarray(patch)) == Point(415, 310)
    d.default_timeout = 0.2  # keep the failing (polling) lookup cheap
    with pytest.raises(ImageNotFound):
        d.resolve_point(noise(30, 20, seed=99))


# --------------------------------------------------------------------- mouse
def test_click_selector_clicks_button(d, app, be):
    d.click('button "OK"')
    assert app["ok"].clicks == 1
    d.click("#okBtn", clicks=2)
    assert app["ok"].clicks == 3
    assert app["cancel"].clicks == 0
    downs = [e for e in be.events if e[0] == "down"]
    assert len(downs) == 3


def test_click_returns_point_and_offset(d, app, be):
    c = app["ok"].screen_rect.center
    assert d.click('button "OK"', offset=(5, 2)) == Point(c.x + 5, c.y + 2)
    assert be.mouse_position() == Point(c.x + 5, c.y + 2)


def test_click_coordinates_and_current_position(d, be):
    assert d.click(200, 150) == Point(200, 150)
    assert ("down", "left", 200, 150) in be.events
    be.clear_events()
    d.click()
    assert [e[0] for e in be.events] == ["down", "up"]
    d.click(offset=(10, 0))
    assert be.mouse_position() == Point(210, 150)


def test_click_with_timing_options_uses_mouse(d, app, be):
    d.click('button "Cancel"', interval=0.01, duration=0.1)
    assert app["cancel"].clicks == 1
    assert len([e for e in be.events if e[0] == "move"]) > 1  # tweened move


def test_click_covered_element_falls_back_to_invoke(d, app, be):
    cover = be.add_window("Popup", (90, 420, 300, 100))  # covers the OK button
    assert be.window_at(*app["ok"].screen_rect.center) is cover
    d.click('window "Invoice Editor" button "OK"')
    assert app["ok"].clicks == 1
    assert any(e[0] == "a11y_invoke" for e in be.events)


def test_double_right_middle_triple(d, be):
    d.double_click(100, 100)
    d.right_click(100, 100)
    d.middle_click(100, 100)
    d.triple_click(100, 100)
    downs = [e[1] for e in be.events if e[0] == "down"]
    assert downs == ["left", "left", "right", "middle", "left", "left", "left"]


def test_move_drag_scroll_mouse_buttons(d, be, app):
    assert d.move(300, 200) == Point(300, 200)
    assert d.position() == Point(300, 200)
    d.move_rel(10, -5)
    assert d.position() == Point(310, 195)
    d.move('button "OK"')
    assert d.position() == app["ok"].screen_rect.center
    be.clear_events()
    d.drag((10, 10), (60, 80), duration=0.1)
    kinds = [e[0] for e in be.events]
    assert kinds[0] == "move" and "down" in kinds and kinds[-1] == "up"
    assert be.mouse_position() == Point(60, 80)
    d.scroll(3, at=(400, 400))
    assert be.events[-1] == ("scroll", 0, 3, 400, 400)
    d.scroll(-2, 1)
    assert be.events[-1][:3] == ("scroll", 1, -2)
    d.mouse_down("right")
    assert be.pressed_buttons
    d.mouse_up("right")
    assert not be.pressed_buttons


def test_humanized_move_is_deterministic(be, ft):
    d = Desktop(be, humanize=Humanize(seed=7), sleep=ft.sleep, clock=ft.clock)
    d.move(900, 600)
    assert d.position() == Point(900, 600)
    assert len([e for e in be.events if e[0] == "move"]) > 5


def test_failsafe_corner_and_abort(d, be):
    be.mouse_move(0, 0)
    with pytest.raises(FailSafeTriggered):
        d.click(100, 100)
    d.failsafe.enabled = False
    d.click(100, 100)
    fs_mod.abort("stop it")
    try:
        with pytest.raises(FailSafeTriggered, match="stop it"):
            d.sleep(0.1)
        with pytest.raises(FailSafeTriggered):
            d.find("button", timeout=1)
    finally:
        fs_mod.reset_abort()


# ------------------------------------------------------------------ keyboard
def test_type_into_clicked_edit(d, app):
    d.click('edit "Name"')
    d.type("Ada Lovelace")
    assert app["name"].value == "Ada Lovelace"
    d.type("!", mode="keys")
    assert app["name"].value == "Ada Lovelace!"


def test_press_hotkey_hold_keys(d, app, be):
    d.click('edit "Name"')
    d.type("abc")
    d.hotkey("ctrl+a")
    d.press("backspace")
    assert app["name"].value == ""
    with d.hold("shift"):
        d.press("x")
    assert app["name"].value == "X"
    assert not be.pressed_keys
    d.key_down("ctrl")
    d.key_up("ctrl")
    assert not be.pressed_keys
    d.press("tab", presses=2)  # Name -> Notes -> Paid
    assert be.focused_widget is app["chk"]


def test_hotkey_copy_paste_through_clipboard(d, app, be):
    d.find('edit "Name"').set_text("copied text")
    d.click('edit "Name"')
    d.hotkey("ctrl", "c")
    assert d.clipboard_get() == "copied text"
    d.click('edit "Notes"')
    d.hotkey("ctrl+v")
    assert app["notes"].value == "copied text"
    d.clipboard_set("other")
    assert be.clipboard == "other"
    with pytest.raises(TypeError):
        d.clipboard_set(5)


# ---------------------------------------------------------------- elements
def test_find_returns_element_with_selector(d, app):
    el = d.find('button "OK"')
    assert isinstance(el, Element)
    assert el.name == "OK" and el.role == "button"
    assert el.selector == 'button "OK"'
    assert el.desktop is d
    el2 = d.find({"role": "edit", "name": "Notes"})
    assert el2.name == "Notes"
    assert isinstance(el2.selector, str)


def test_find_set_text_on_edit(d, app):
    d.find('edit "Name"').set_text("Grace")
    assert app["name"].value == "Grace"
    d.find('edit "Notes"').set_text("typed", native=False)
    assert app["notes"].value == "typed"


def test_find_waits_until_element_appears(d, app, ft):
    win = app["win"]

    def appear():
        if ft.now >= 0.5 and win.find("Later") is None:
            win.add_button("Later", (300, 10, 60, 25))

    ft.hooks.append(appear)
    el = d.find('button "Later"', timeout=5)
    assert el.name == "Later"
    assert 0.5 <= ft.now < 1.0


def test_find_timeout_zero_is_single_check(d, app, ft):
    with pytest.raises(ElementNotFound):
        d.find('button "Missing"', timeout=0)
    assert ft.now == 0.0


def test_find_not_found_message_has_hint(d, app):
    with pytest.raises(ElementNotFound) as ei:
        d.find('button "Canel"', timeout=0.3)
    msg = str(ei.value)
    assert "Canel" in msg and "0.3s" in msg
    assert 'did you mean' in msg and "Cancel" in msg
    with pytest.raises(ElementNotFound) as ei:
        d.find("buton", timeout=0)
    assert "button" in str(ei.value)


def test_find_not_found_empty_tree(d):
    with pytest.raises(ElementNotFound, match="empty"):
        d.find("button", timeout=0)


def test_find_syntax_error_is_immediate(d, app, ft):
    with pytest.raises(SelectorSyntaxError):
        d.find("button[name=", timeout=5)
    assert ft.now == 0.0


def test_find_not_supported_is_not_swallowed(ft):
    class NoA11y(MockBackend):
        def accessibility_roots(self):
            raise NotSupported("no tree")

    d = Desktop(NoA11y(), sleep=ft.sleep, clock=ft.clock)
    with pytest.raises(NotSupported):
        d.find("button", timeout=5)
    assert ft.now == 0.0


def test_find_transient_errors_are_retried(ft, be, app):
    calls = {"n": 0}
    orig = be.accessibility_roots

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("COM hiccup")
        return orig()

    be.accessibility_roots = flaky
    d = Desktop(be, sleep=ft.sleep, clock=ft.clock)
    assert d.find('button "OK"', timeout=2).name == "OK"
    assert calls["n"] == 3


def test_find_with_root_and_find_all_exists(d, app, be):
    other = be.add_window("Other", (700, 100, 300, 200), activate=False)
    other.add_button("OK", (10, 10, 60, 25))
    win = d.window("Invoice Editor")
    el = d.find('button "OK"', root=win)
    assert el.bounds == app["ok"].screen_rect
    assert len(d.find_all('button "OK"')) == 2
    assert len(d.find_all("button", root=win)) == 2
    assert len(d.find_all("edit", root=win.element)) == 2
    assert d.exists('button "Cancel"')
    assert not d.exists('button "Nope"')
    assert d.exists('button "OK"', root=d.window("Other"))
    with pytest.raises(TypeError):
        d.find("button", root="Other")


def test_wait_states(d, app, ft):
    assert d.wait('button "OK"').name == "OK"
    app["ok"].enabled = False
    with pytest.raises(ActionTimeout):
        d.wait('button "OK"', state="enabled", timeout=0.5)

    def enable():
        if ft.now >= 0.3:
            app["ok"].enabled = True

    ft.hooks.append(enable)
    assert d.wait('button "OK"', state="enabled", timeout=2).is_enabled
    with pytest.raises(ElementNotFound):
        d.wait('button "Ghost"', timeout=0.2)
    assert d.wait('checkbox "Paid"', state=lambda e: not e.is_checked, timeout=0).name == "Paid"
    with pytest.raises(ValueError):
        d.wait("button", state="sparkly")


def test_wait_gone(d, app, be, ft):
    def vanish():
        if ft.now >= 0.4:
            app["cancel"].visible = False

    ft.hooks.append(vanish)
    assert d.wait('button "Cancel"', state="gone", timeout=3) is None
    assert 0.4 <= ft.now < 1.0
    with pytest.raises(ActionTimeout):
        d.wait('button "OK"', state="gone", timeout=0.2)


def test_element_at_focused_tree_roots(d, app):
    c = app["name"].screen_rect.center
    el = d.element_at(c.x, c.y)
    assert el is not None and el.name == "Name"
    assert d.element_at(c).name == "Name"
    assert d.element_at(5, 5) is None
    assert d.focused() is None
    d.click('edit "Notes"')
    assert d.focused().name == "Notes"
    text = d.tree()
    assert 'button "OK"' in text and "Invoice Editor" in text
    assert 'edit "Name"' in d.tree(window="Invoice", interactive_only=True)
    assert 'edit "Name"' in d.tree(window=d.window("Invoice"))
    assert len(d.roots()) == 1


# ------------------------------------------------------------------- windows
def test_windows_and_activation(d, be, app):
    w2 = be.add_window("Invoice Editor - copy", (600, 100, 300, 300), app="erp")
    assert be._active is w2
    titles = [w.title for w in d.windows("invoice")]
    assert titles == ["Invoice Editor - copy", "Invoice Editor"]
    win = d.window("Invoice Editor")  # exact title wins over z-order
    assert win.title == "Invoice Editor"
    assert not win.is_active
    win.activate()
    assert win.is_active
    assert be._active is app["win"]
    assert d.active_window() == win
    assert d.window().title == "Invoice Editor"  # no criteria -> active
    assert [w.title for w in d.windows(app="ERP.exe")] == ["Invoice Editor", "Invoice Editor - copy"]
    assert isinstance(win, Window)


def test_window_not_found_lists_open_windows(d, app, ft):
    with pytest.raises(WindowNotFound) as ei:
        d.window("Calculator", timeout=0.5)
    assert "Invoice Editor" in str(ei.value) and "Calculator" in str(ei.value)
    assert ft.now == pytest.approx(0.5)


def test_window_waits_for_appearance_and_launch(d, be, ft):
    def spawn():
        if ft.now >= 0.3 and not be.windows:
            be.add_window("Notepad", (0, 0, 300, 200), app="notepad")

    ft.hooks.append(spawn)
    w = d.launch("notepad.exe", wait_window="Notepad", timeout=3)
    assert isinstance(w, Window) and w.title == "Notepad"
    assert be.launched == ["notepad.exe"]
    assert d.launch(["x", "y"]) is None  # mock returns no process
    assert d.active_window().title == "Notepad"


# -------------------------------------------------------------- screen/vision
def test_screenshot_region_monitor_and_save(d, app, tmp_path):
    img = d.screenshot()
    assert img.size == (1280, 800)
    assert d.screenshot((10, 20, 30, 40)).size == (30, 40)
    el = d.find('button "OK"')
    assert d.screenshot(el).size == (80, 30)
    assert d.screenshot(d.window("Invoice")).size == (500, 400)
    out = tmp_path / "sub" / "shot.png"
    d.screenshot(path=out)
    assert out.exists()
    out2 = tmp_path / "noext"
    d.screenshot((0, 0, 5, 5), path=out2)
    assert Image.open(out2).size == (5, 5)
    assert d.size() == (1280, 800)
    assert len(d.monitors()) == 1
    assert d.virtual_rect() == Rect(0, 0, 1280, 800)
    assert d.on_screen(5, 5) and not d.on_screen(5000, 5)


def test_pixel(d, be):
    be.paint(Image.new("RGB", (4, 4), (12, 34, 56)), (500, 500))
    assert d.pixel(501, 501) == (12, 34, 56)
    assert d.pixel((501, 501)) == (12, 34, 56)
    assert d.pixel_matches(501, 501, (10, 34, 56), tolerance=2)
    assert not d.pixel_matches(501, 501, (10, 34, 56))


def test_locate_painted_image(d, be, tmp_path):
    patch = noise(32, 24)
    be.paint(patch, (640, 480))
    m = d.locate(patch)
    assert m is not None and m.rect == Rect(640, 480, 32, 24)
    assert m.confidence > 0.99
    path = tmp_path / "needle.png"
    patch.save(path)
    assert d.locate(str(path)).rect == Rect(640, 480, 32, 24)
    assert d.locate(path, region=(600, 400, 200, 200)).rect == Rect(640, 480, 32, 24)
    assert d.locate(path, region=(0, 0, 100, 100)) is None
    assert d.locate(noise(32, 24, seed=5)) is None


def test_locate_polls_until_timeout(d, be, ft):
    patch = noise(16, 16, seed=3)

    def show():
        if ft.now >= 0.5 and not be.overlays:
            be.paint(patch, (100, 700))

    ft.hooks.append(show)
    assert d.locate(patch) is None  # timeout=0 -> single check
    assert ft.now == 0
    m = d.locate(patch, timeout=2)
    assert m is not None and m.rect.left == 100
    assert 0.5 <= ft.now < 1.0


def test_locate_all_wait_image_click_image(d, be, ft):
    patch = noise(20, 20, seed=11)
    be.paint(patch, (100, 100))
    be.paint(patch, (300, 500))
    found = d.locate_all(patch)
    assert sorted((m.rect.left, m.rect.top) for m in found) == [(100, 100), (300, 500)]
    assert len(d.locate_all(patch, region=(0, 0, 200, 200))) == 1
    m = d.wait_image(patch)
    assert m.rect.width == 20
    be.clear_events()
    m = d.click_image(patch, region=(250, 450, 100, 100))
    assert m.rect.left == 300
    assert ("down", "left", 310, 510) in be.events
    with pytest.raises(ImageNotFound, match="within 0.2s"):
        d.wait_image(noise(20, 20, seed=12), timeout=0.2)
    assert ft.now == pytest.approx(0.2)
    d.click(patch)  # image targets go through resolve_point
    assert be.mouse_position() in (Point(110, 110), Point(310, 510))


class FakeOCR:
    def __init__(self, words):
        self.words = words
        self.calls = 0

    def read(self, image):
        self.calls += 1
        return list(self.words)


def test_find_text_click_text_read_text(d, be, ft):
    engine = FakeOCR([OCRWord("Submit", Rect(10, 20, 50, 14), 0.95), OCRWord("order", Rect(64, 20, 40, 14), 0.9)])
    m = d.find_text("Submit", engine=engine)
    assert m is not None and m.rect == Rect(10, 20, 50, 14)
    m = d.find_text("order", region=(100, 100, 300, 300), engine=engine)
    assert m.rect == Rect(164, 120, 40, 14)  # offset by the region origin
    assert d.find_text("absent", engine=engine) is None
    assert d.read_text(engine=engine) == "Submit order"
    be.clear_events()
    d.click_text("Submit order", engine=engine)
    assert be.events[-2][0] == "down"
    with pytest.raises(ImageNotFound):
        d.click_text("nothing", timeout=0.3, engine=engine)
    before = engine.calls
    d.find_text("absent", timeout=0.5, engine=engine)
    assert engine.calls - before > 1  # polled


def test_wait_for_change_and_stable(d, be, ft):
    def change():
        if ft.now >= 0.4 and not be.overlays:
            be.paint(Image.new("RGB", (50, 50), (255, 0, 0)), (0, 0))

    ft.hooks.append(change)
    assert d.wait_for_change((0, 0, 60, 60), timeout=2, interval=0.1)
    assert d.wait_until_stable((0, 0, 60, 60), stable_for=0.3, timeout=2)


def test_desktop_wait_until_and_sleep(d, ft):
    box = {"n": 0}

    def pred():
        box["n"] += 1
        return box["n"] if box["n"] >= 3 else 0

    assert d.wait_until(pred, timeout=5, interval=0.25) == 3
    assert ft.now == pytest.approx(0.5)
    with pytest.raises(ActionTimeout):
        d.wait_until(lambda: False, timeout=0.2)
    d.sleep(1.5)
    assert ft.slept[-1] == 1.5
    d.sleep(0)
    assert ft.slept[-1] == 1.5
