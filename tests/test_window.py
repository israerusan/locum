from __future__ import annotations

import re

import pytest

from _stub_desktop import StubDesktop
from locum.backends.mock import MockBackend
from locum.element import Element
from locum.errors import ActionTimeout, ElementNotFound, NotSupported, WindowNotFound
from locum.types import Rect, WindowInfo
from locum.window import Window, match_window


def info(title="Untitled - Notepad", app="C:\\Windows\\notepad.exe", cls="Notepad", pid=10, handle=1):
    return WindowInfo(handle=handle, title=title, rect=Rect(0, 0, 100, 100), app=app, pid=pid, class_name=cls)


# ---------------------------------------------------------------- match_window
def test_match_window_title_rules():
    i = info()
    assert match_window(i)
    assert match_window(i, "Untitled - Notepad")  # exact
    assert match_window(i, "notepad")  # case-insensitive substring
    assert match_window(i, "*NOTEPAD")  # glob, case-insensitive
    assert match_window(i, "Untitled - ?otepad")
    assert not match_window(i, "*Word")
    assert match_window(i, re.compile(r"^Untitled"))
    assert not match_window(i, re.compile(r"^untitled"))
    assert match_window(i, re.compile(r"^untitled", re.I))
    assert not match_window(i, "Wordpad")
    star = info(title="*Untitled - Notepad")
    assert match_window(star, "*Untitled - Notepad")  # exact wins over glob
    assert match_window(star, "*untitled")  # glob fails but literal substring still matches
    assert match_window(i, "")  # empty string = substring of anything


def test_match_window_app_class_pid():
    i = info()
    assert match_window(i, app="notepad")
    assert match_window(i, app="NOTEPAD.EXE")
    assert match_window(i, app="note*")
    assert not match_window(i, app="note")
    assert match_window(i, app=re.compile("notepad"))
    assert match_window(i, class_name="Notepad")
    assert match_window(i, class_name="Note*")
    assert not match_window(i, class_name="notepad2")
    assert match_window(i, pid=10)
    assert not match_window(i, pid=11)
    assert match_window(i, "Notepad", app="notepad", class_name="Notepad", pid=10)
    assert not match_window(i, "Notepad", app="calc")
    unix = info(app="/usr/bin/gedit")
    assert match_window(unix, app="gedit")


# ---------------------------------------------------------------------- Window
@pytest.fixture
def env():
    be = MockBackend(width=1280, height=800)
    d = StubDesktop(be)
    back = be.add_window("Back", (50, 50, 300, 200), app="back.exe", pid=1)
    front = be.add_window("Editor - doc.txt", (200, 150, 600, 400), app="editor.exe", pid=2)
    front.add_edit("Body", (10, 40, 580, 300), automation_id="body")
    front.add_button("Save", (10, 350, 80, 30))
    back.add_button("Save", (10, 10, 80, 30))
    return d, be, back, front


def window(d, title):
    return d.window(title)


def test_properties_and_repr(env):
    d, be, back, front = env
    w = window(d, "Editor")
    assert isinstance(w, Window)
    assert w.title == "Editor - doc.txt" and w.app == "editor.exe" and w.pid == 2
    assert w.handle == front.handle
    assert w.rect == Rect(200, 150, 600, 400)
    assert w.is_active and not w.is_minimized and not w.is_maximized and w.is_visible
    assert w.class_name == "MockWindow"
    assert w.info.handle == front.handle
    assert w.to_dict()["title"] == "Editor - doc.txt"
    assert "Editor - doc.txt" in repr(w) and "[active]" in repr(w)
    assert w == window(d, "doc.txt") and w != window(d, "Back")
    assert len({w, window(d, "Editor")}) == 1
    with pytest.raises(TypeError):
        Window("nope", d)  # type: ignore[arg-type]


def test_activate_minimize_maximize_restore(env):
    d, be, back, front = env
    b = window(d, "Back")
    assert not b.is_active
    assert b.activate() is b
    assert b.is_active and ("activate", back.handle) in be.events
    b.minimize()
    assert b.is_minimized and not b.is_active
    b.restore()
    assert not b.is_minimized and b.is_active
    b.maximize()
    assert b.is_maximized and b.rect == Rect(0, 0, 1280, 800)
    b.restore()
    assert not b.is_maximized and b.rect == Rect(50, 50, 300, 200)


def test_move_resize_set_rect(env):
    d, be, back, front = env
    w = window(d, "Back")
    w.move(10, 20)
    assert w.rect == Rect(10, 20, 300, 200) and back.rect == Rect(10, 20, 300, 200)
    w.resize(320, 240)
    assert w.rect == Rect(10, 20, 320, 240)
    w.set_rect((5, 6, 7, 8))
    assert w.rect == Rect(5, 6, 7, 8)
    with pytest.raises(ValueError):
        w.resize(0, 10)


def test_close_and_wait_closed(env):
    d, be, back, front = env
    w = window(d, "Back")
    assert w.exists
    w.close()
    assert not w.exists
    assert w.wait_closed(timeout=0) is True
    with pytest.raises(WindowNotFound):
        w.refresh()
    other = window(d, "Editor")
    with pytest.raises(ActionTimeout):
        other.wait_closed(timeout=0)


def test_wait_closed_polls(env):
    d, be, back, front = env
    w = window(d, "Back")
    calls = {"n": 0}

    def sleeper(s):
        calls["n"] += 1
        if calls["n"] == 2:
            be.remove_window(back)

    d.sleep = sleeper
    assert w.wait_closed(timeout=5)
    assert calls["n"] == 2


def test_refresh_reflects_external_changes(env):
    d, be, back, front = env
    w = window(d, "Back")
    back.title = "Back (renamed)"
    back.rect = Rect(1, 2, 3, 4)
    assert w.title == "Back"
    assert w.refresh() is w
    assert w.title == "Back (renamed)" and w.rect == Rect(1, 2, 3, 4)


def test_screenshot(env):
    d, be, back, front = env
    w = window(d, "Editor")
    img = w.screenshot()
    assert img.size == (600, 400)
    assert img.getpixel((100, 5)) == (40, 90, 160)  # active title bar
    w.minimize()
    with pytest.raises(NotSupported):
        w.screenshot()


def test_screenshot_clipped():
    be = MockBackend(width=400, height=300)
    d = StubDesktop(be)
    be.add_window("Edge", (300, 200, 200, 200))
    assert d.window("Edge").screenshot().size == (100, 100)
    be.add_window("Gone", (500, 500, 50, 50))
    with pytest.raises(NotSupported):
        d.window("Gone").screenshot()


def test_element_root_by_handle_then_title(env):
    d, be, back, front = env
    w = window(d, "Editor")
    root = w.element
    assert isinstance(root, Element)
    assert root.role == "window" and root.name == "Editor - doc.txt"
    # a WindowInfo whose handle is unknown never borrows another live window's root by
    # title (a closed window would otherwise act on a same-titled other window) ...
    ghost = Window(WindowInfo(handle=-1, title="Back", rect=Rect(0, 0, 1, 1)), d)
    with pytest.raises(ElementNotFound):
        _ = ghost.element
    # ... but a root the backend cannot relate to any window (no handle) still matches by title
    back_root = next(r for r in be.accessibility_roots() if r.name == "Back")
    real_handle = type(back_root).handle
    try:
        type(back_root).handle = property(lambda self: None)
        assert ghost.element.name == "Back"
    finally:
        type(back_root).handle = real_handle
    nowhere = Window(WindowInfo(handle=-2, title="Nope", rect=Rect(0, 0, 1, 1)), d)
    with pytest.raises(ElementNotFound):
        _ = nowhere.element
    assert nowhere.find_all("button") == []
    with pytest.raises(ElementNotFound):
        nowhere.find("button", timeout=0)


def test_find_scoped_to_window(env):
    d, be, back, front = env
    w = window(d, "Back")
    save = w.find('button "Save"', timeout=0)
    assert save.bounds == Rect(60, 60, 80, 30)  # the Back window's button, not the front one
    assert w.find('window "Back" > button', timeout=0) == save
    assert [e.name for e in w.find_all("button")] == ["Save"]
    assert w.find_all("edit") == []
    with pytest.raises(ElementNotFound):
        w.find("edit", timeout=0)
    assert w.exists_element("button") and not w.exists_element("edit")
    body = window(d, "Editor").find({"id": "body"}, timeout=0)
    assert body.selector == '[automation_id="body"]'


def test_find_waits_and_uses_default_timeout(env):
    d, be, back, front = env
    w = window(d, "Back")
    calls = {"n": 0}

    def sleeper(s):
        calls["n"] += 1
        if calls["n"] == 2:
            back.add_edit("Late", (0, 0, 10, 10))

    d.sleep = sleeper
    d.default_timeout = 5
    assert w.find("edit").name == "Late"


def test_found_element_refresh_goes_through_window(env):
    d, be, back, front = env
    w = window(d, "Editor")
    save = w.find('button "Save"', timeout=0)
    old = save.node
    save.refresh()
    assert save.node is not old and save.node.same_as(old)
    front.find("Save").name = "Save As"
    with pytest.raises(ElementNotFound):
        save.refresh()


def test_found_element_click(env):
    d, be, back, front = env
    hits = []
    back.find("Save").on_click = lambda wd: hits.append("back-save")
    w = window(d, "Back")
    w.find('button "Save"', timeout=0).click()  # visible -> real mouse click
    assert hits == ["back-save"] and any(e[0] == "down" for e in be.events)
    hits.clear()
    window(d, "Editor").activate()
    w.move(250, 200)  # now entirely behind the editor window
    be.clear_events()
    w.find('button "Save"', timeout=0).click()  # covered by the front window -> native invoke
    assert hits == ["back-save"]
    assert not any(e[0] == "down" for e in be.events)
