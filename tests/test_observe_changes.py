"""observe(changes_only=True): only what changed since the last observe, ids kept, cropped image.

Live session 2026-11-05: every full observe re-sent ~290 KB of screenshot.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.llm.obsdiff import changed_regions, compare
from locum.llm.observe import observe
from locum.llm.tools import DesktopToolkit


@pytest.fixture
def env():
    be = MockBackend(width=1280, height=800)
    calc = be.add_window("Calculator", (0, 0, 400, 400), app="calc", pid=1)
    w = {
        "display": calc.add_text("Display is 0", (10, 40, 380, 40)),
        "seven": calc.add_button("Seven", (10, 100, 80, 40)),
        "equals": calc.add_button("Equals", (100, 100, 80, 40)),
        "clear": calc.add_button("Clear", (190, 100, 80, 40)),
    }
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, calc, w


def _img(result):
    return Image.open(io.BytesIO(base64.b64decode(result.image_base64))) if result.image_base64 else None


def test_unchanged_screen_reports_nothing_and_sends_no_image(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d)
    full = tk.call("observe", {})
    assert full.image_base64
    again = tk.call("observe", {"changes_only": True})
    assert "Nothing changed" in again.text and "0 new, 0 changed, 0 gone" in again.text
    assert again.image_base64 is None and "no image attached" in again.text


def test_changed_element_keeps_its_id_and_the_crop_covers_it(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d, observe_kwargs={"interactive_only": False})
    tk.call("observe", {})
    first = {e.name: e.id for e in tk.last_observation.elements}
    w["display"].name = "Display is 56,154"
    r = tk.call("observe", {"changes_only": True})
    assert "1 changed" in r.text and "Display is 56,154" in r.text and "was name 'Display is 0'" in r.text
    after = {e.name: e.id for e in tk.last_observation.elements}
    assert after["Display is 56,154"] == first["Display is 0"]  # same element, same id
    assert after["Equals"] == first["Equals"]
    crop = _img(r)
    full_w, full_h = tk.last_observation.image_size
    assert crop is not None and crop.size[0] * crop.size[1] < 0.5 * full_w * full_h
    # element ids from before still work for actions
    assert tk.call("click", {"element_id": first["Equals"]}).is_error is False
    assert w["equals"].clicks == 1


def test_new_and_gone_elements_and_windows(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d)
    tk.call("observe", {})
    old_ids = {e.name: e.id for e in tk.last_observation.elements}
    calc.root.children.remove(w["clear"]) if hasattr(calc, "root") else w["clear"].parent.children.remove(w["clear"])
    dlg = be.add_window("Save As", (500, 200, 300, 200), app="calc", pid=1)
    dlg.add_button("Save", (10, 150, 80, 30))
    r = tk.call("observe", {"changes_only": True})
    assert "Windows opened: \"Save As\"" in r.text
    assert "Active window: \"Save As\" (was \"Calculator\")" in r.text
    assert f"[{old_ids['Clear']}] button \"Clear\"" in r.text and "no longer valid" in r.text
    new_ids = {e.name: e.id for e in tk.last_observation.elements}
    assert new_ids["Save"] > max(old_ids.values())  # never reuses an id
    assert new_ids["Seven"] == old_ids["Seven"]


def test_first_changes_only_call_falls_back_to_a_full_observation(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d)
    r = tk.call("observe", {"changes_only": True})
    assert "Elements" in r.text and "no earlier observation" in r.text and r.image_base64


def test_mostly_changed_screen_sends_the_full_image(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d)
    tk.call("observe", {})
    big = be.add_window("Fullscreen", (0, 0, 1280, 800), app="video", pid=2)
    big.add_button("Play", (600, 380, 80, 40))
    r = tk.call("observe", {"changes_only": True})
    assert "Screenshot: full" in r.text and _img(r).size == tk.last_observation.image_size


def test_changed_regions_ignore_tiny_isolated_changes():
    a = Image.new("RGB", (400, 300), "white")
    b = a.copy()
    b.putpixel((5, 5), (0, 0, 0))  # a caret blink
    assert changed_regions(a, b) == []
    for x in range(100, 200):
        for y in range(100, 160):
            b.putpixel((x, y), (0, 0, 0))
    (r,) = changed_regions(a, b)
    assert r.left <= 100 and r.right >= 200 and r.top <= 100 and r.bottom >= 160


def test_compare_matches_by_runtime_id_when_available(env):
    be, d, calc, w = env
    o1 = observe(d, screenshot=False)
    o2 = observe(d, screenshot=False)
    for i, (a, b) in enumerate(zip(o1.elements, o2.elements)):
        a.node.__dict__["runtime_id"] = (42, i)
        b.node.__dict__["runtime_id"] = (42, i)
    w["seven"].rect = w["seven"].rect.offset(0, 200)  # moved: matched by runtime id, reported as moved
    o3 = observe(d, screenshot=False)
    for i, e in enumerate(o3.elements):
        e.node.__dict__["runtime_id"] = (42, i)
    diff = compare(o2, o3)
    assert not diff.added and not diff.removed
    assert [c for _, n, c in diff.changed if n.name == "Seven"] == [["moved/resized"]]


def test_changes_behind_another_window_add_no_image(env):
    be, d, calc, w = env
    front = be.add_window("Editor", (0, 0, 600, 600), app="edit", pid=3)  # covers the calculator
    front.add_button("Bold", (300, 500, 60, 30))
    tk = DesktopToolkit(d, observe_kwargs={"interactive_only": False})
    tk.call("observe", {})
    w["display"].name = "Display is 9"  # changes, but nobody can see it
    r = tk.call("observe", {"changes_only": True})
    assert "1 changed" in r.text and "[occluded]" in r.text
    assert r.image_base64 is None


def test_cap_churn_is_flagged(env):
    be, d, calc, w = env
    tk = DesktopToolkit(d, observe_kwargs={"max_elements": 2})
    tk.call("observe", {})
    be.add_window("Popup", (600, 0, 200, 100), app="x", pid=9).add_button("Ok", (10, 10, 50, 30))
    r = tk.call("observe", {"changes_only": True})
    assert "element cap" in r.text
