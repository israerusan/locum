"""``LOG_SCREENSHOTS`` / ``LOG_SCREENSHOTS_LIMIT`` of the PyAutoGUI compatibility layer (MockBackend, tmp_path)."""

from __future__ import annotations

import os
import re

import pytest
from PIL import Image

import locum.compat.pyautogui as pg
from locum.backends.mock import MockBackend
from locum.desktop import Desktop

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{1,2}-\d{1,2}-\d{1,2}-\d{1,3}_([A-Za-z]+)_(.*)\.png$")


@pytest.fixture
def env(tmp_path, monkeypatch):
    be = MockBackend(width=800, height=600)
    win = be.add_window("Editor", (100, 100, 400, 300), app="editor")
    edit = win.add_edit("Text", (20, 40, 200, 28))
    d = Desktop(be, sleep=lambda s: None, pause=0.0, default_timeout=0.0)
    keys = ("PAUSE", "_SLEEP", "LOG_SCREENSHOTS", "LOG_SCREENSHOTS_LIMIT")
    saved = {k: getattr(pg, k) for k in keys}
    saved_names = list(pg.G_LOG_SCREENSHOTS_FILENAMES)
    pg.G_LOG_SCREENSHOTS_FILENAMES.clear()
    pg._SLEEP = lambda s: None
    pg.PAUSE = 0.0
    old = pg.set_desktop(d)
    be.mouse_move(400, 300)
    be._set_focus(edit)
    monkeypatch.chdir(tmp_path)
    try:
        yield {"be": be, "edit": edit, "dir": tmp_path}
    finally:
        pg.set_desktop(old)
        for k, v in saved.items():
            setattr(pg, k, v)
        pg.G_LOG_SCREENSHOTS_FILENAMES[:] = saved_names


def pngs(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".png")


def parsed(folder):
    out = []
    for n in pngs(folder):
        m = NAME_RE.match(n)
        assert m, n
        out.append((m.group(1), m.group(2)))
    return out


def test_off_by_default(env):
    assert pg.LOG_SCREENSHOTS is False and pg.LOG_SCREENSHOTS_LIMIT == 10
    pg.click(10, 10)
    pg.press("a")
    pg.write("hi")
    assert pngs(env["dir"]) == []


def test_actions_log_a_screenshot_named_after_the_call(env):
    pg.LOG_SCREENSHOTS = True
    pg.click(150, 160)
    pg.scroll(3, x=200, y=210)
    pg.press(["tab", "enter"])
    pg.keyDown("shift")
    pg.keyUp("shift")
    pg.hotkey("ctrl", "a")
    pg.write("hello")
    pg.mouseDown(300, 310)
    pg.mouseUp()
    pg.dragTo(320, 330)
    got = parsed(env["dir"])
    funcs = [f for f, _a in got]
    assert sorted(funcs) == sorted(["click", "scroll", "press", "keyDown", "keyUp", "hotkey", "write",
                                    "mouseDown", "mouseUp", "dragTo"])
    args = dict(got)
    assert args["click"] == "left,1,150,160"
    assert args["scroll"] == "3,200,210"
    assert args["press"] == "tab,enter"
    assert args["hotkey"] == "ctrl,a"
    assert args["write"] == "hello"
    assert args["dragTo"] == "320,330"
    # The files are real PNG screenshots of the (mock) screen.
    with Image.open(env["dir"] / pngs(env["dir"])[0]) as img:
        assert img.size == (800, 600)
    assert len(pg.G_LOG_SCREENSHOTS_FILENAMES) == 10


def test_screenshot_is_taken_before_the_action(env, monkeypatch):
    pg.LOG_SCREENSHOTS = True
    order = []
    real = pg.screenshot
    monkeypatch.setattr(pg, "screenshot", lambda path=None, **kw: (order.append(("shot", env["edit"].value)),
                                                                    real(path, **kw))[1])
    pg.write("abc")
    assert order == [("shot", None)] or order == [("shot", "")]
    assert env["edit"].value == "abc"


def test_moves_only_log_when_asked(env):
    pg.LOG_SCREENSHOTS = True
    pg.moveTo(50, 60)          # PyAutoGUI: moveTo/moveRel default to logScreenshot=False
    pg.moveRel(5, 5)
    assert pngs(env["dir"]) == []
    pg.moveTo(70, 80, logScreenshot=True)
    assert [f for f, _a in parsed(env["dir"])] == ["moveTo"]


def test_per_call_argument_overrides_the_global(env):
    pg.LOG_SCREENSHOTS = False
    pg.click(10, 10, logScreenshot=True)
    pg.hotkey("ctrl", "c", logScreenshot=True)
    assert sorted(f for f, _a in parsed(env["dir"])) == ["click", "hotkey"]
    pg.LOG_SCREENSHOTS = True
    pg.click(10, 10, logScreenshot=False)
    pg.hotkey("ctrl", "c", logScreenshot=False)
    assert len(pngs(env["dir"])) == 2


def test_limit_deletes_the_oldest(env):
    pg.LOG_SCREENSHOTS = True
    pg.LOG_SCREENSHOTS_LIMIT = 3
    for i in range(6):
        pg.press(f"f{i + 1}")
    names = pngs(env["dir"])
    assert len(names) == 3
    assert sorted(a for _f, a in parsed(env["dir"])) == ["f4", "f5", "f6"]
    assert pg.G_LOG_SCREENSHOTS_FILENAMES == [n for n in pg.G_LOG_SCREENSHOTS_FILENAMES if n in names]
    assert len(pg.G_LOG_SCREENSHOTS_FILENAMES) == 3


def test_limit_none_keeps_everything(env):
    pg.LOG_SCREENSHOTS = True
    pg.LOG_SCREENSHOTS_LIMIT = None
    for i in range(12):
        pg.press(f"f{i + 1}")
    assert len(pngs(env["dir"])) == 12


def test_a_file_deleted_by_hand_does_not_break_the_rotation(env):
    pg.LOG_SCREENSHOTS = True
    pg.LOG_SCREENSHOTS_LIMIT = 1
    pg.press("a")
    os.unlink(env["dir"] / pngs(env["dir"])[0])
    pg.press("b")
    assert [a for _f, a in parsed(env["dir"])] == ["b"]


def test_unsafe_filename_characters_and_long_args(env, monkeypatch):
    pg.LOG_SCREENSHOTS = True
    paths = []
    monkeypatch.setattr(pg, "screenshot", lambda path=None, **kw: paths.append(path))
    pg.write('a:b/c\\d*e?"<>|\n' + "x" * 300)
    (path,) = paths
    name = os.path.basename(path)
    assert os.path.dirname(path) == "." and len(name) < 255
    func, args = NAME_RE.match(name).groups()
    assert func == "write" and args.startswith("a_b_c_d_e_____") and args.endswith("...")


def test_a_failed_save_does_not_fail_the_action(env, monkeypatch):
    pg.LOG_SCREENSHOTS = True

    def boom(path=None, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pg, "screenshot", boom)
    pg.write("ok")
    assert env["edit"].value == "ok" and pg.G_LOG_SCREENSHOTS_FILENAMES == []


def test_logged_screenshots_go_to_the_current_directory(env, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(other)
    pg.LOG_SCREENSHOTS = True
    pg.click(10, 10)
    assert len(pngs(other)) == 1 and pngs(env["dir"]) == []
