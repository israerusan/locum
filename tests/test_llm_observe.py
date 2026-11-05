from __future__ import annotations

import base64
import io
import math

import pytest
from PIL import Image

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import ElementNotFound
from locum.llm.observe import Observation, fit_image_size, observe, visual_tokens
from locum.selectors import parse
from locum.types import Point, Rect


def make_desktop(width=1920, height=1080):
    be = MockBackend(width=width, height=height)
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0.5)
    return be, d


@pytest.fixture
def scene():
    be, d = make_desktop()
    back = be.add_window("Back", (50, 50, 600, 400), app="notes")
    hidden_btn = back.add_button("Hidden", (200, 100, 80, 30))
    back.add_button("Visible", (10, 300, 80, 30))
    front = be.add_window("Invoice", (200, 100, 400, 300), app="erp")
    ok = front.add_button("OK", (10, 240, 80, 30), automation_id="okBtn")
    name = front.add_edit("Name", (10, 50, 200, 28), value="Ann")
    front.add_text("Total: 42", (10, 100, 120, 20))
    agree = front.add_checkbox("Agree", (10, 140, 120, 24))
    return be, d, {"ok": ok, "name": name, "agree": agree, "hidden": hidden_btn}


def test_fit_image_size_limits():
    assert fit_image_size(2560, 1440) == (2560, 1440)
    assert visual_tokens(2560, 1440) == 4784
    assert fit_image_size(3840, 2160) == (2576, 1449)
    w, h = fit_image_size(1920, 1080, 1568)
    assert (w, h) == (1568, 882)
    # Very tall/odd sizes stay within both limits.
    for size in [(5000, 5000), (1000, 9000), (7680, 4320), (3000, 2000), (100, 50)]:
        w, h = fit_image_size(*size)
        assert max(w, h) <= 2576
        assert math.ceil(w / 28) * math.ceil(h / 28) <= 4784
        assert w <= size[0] and h <= size[1]
    with pytest.raises(ValueError):
        fit_image_size(0, 10)


def test_observe_lists_interactive_elements_front_first(scene):
    be, d, w = scene
    obs = observe(d)
    names = [(e.id, e.role, e.name) for e in obs.elements]
    # front window first, ids start at 1, labels excluded in interactive_only mode
    assert names[0][1:] == ("button", "OK")
    assert [e.id for e in obs.elements] == list(range(1, len(obs.elements) + 1))
    roles = {e.role for e in obs.elements}
    assert "text" not in roles and "window" not in roles
    assert {e.name for e in obs.elements} >= {"OK", "Name", "Agree", "Hidden", "Visible"}
    name_el = next(e for e in obs.elements if e.name == "Name")
    assert name_el.value == "Ann"
    assert name_el.app == "erp" and name_el.window_title == "Invoice"


def test_occlusion_flag_and_selectors_roundtrip(scene):
    be, d, w = scene
    obs = observe(d)
    hidden = next(e for e in obs.elements if e.name == "Hidden")
    visible = next(e for e in obs.elements if e.name == "Visible")
    assert hidden.occluded  # its center (290, 165) lies under the Invoice window
    assert not visible.occluded
    for e in obs.elements:
        found = parse(e.selector).find_first(d.roots())
        assert found is not None and found.same_as(e.node), e.selector
    ok = obs.element(1)
    assert ok.selector.endswith("button#okBtn")


def test_screenshot_scaled_and_annotated(scene):
    be, d, _ = scene
    obs = observe(d, max_long_edge=1568)
    assert obs.screenshot.size == (1568, 882)
    assert obs.annotated is not None and obs.annotated.size == obs.screenshot.size
    assert obs.annotated.tobytes() != obs.screenshot.tobytes()
    assert obs.scale == pytest.approx(1568 / 1920)
    png = base64.b64decode(obs.image_base64())
    assert Image.open(io.BytesIO(png)).size == (1568, 882)
    raw = base64.b64decode(obs.image_base64(annotated=False))
    assert raw != png


def test_coordinate_mapping_roundtrip(scene):
    be, d, _ = scene
    obs = observe(d)
    for sx, sy in [(0, 0), (250, 150), (1919, 1079), (960, 540)]:
        ix, iy = obs.to_image(sx, sy)
        back = obs.to_screen(ix, iy)
        assert abs(back.x - sx) <= 1 and abs(back.y - sy) <= 1
    # clamped into the region
    assert obs.to_screen(-50, 99999) == Point(0, 1079)


def test_element_lookup_errors(scene):
    _, d, _ = scene
    obs = observe(d, screenshot=False)
    assert obs.screenshot is None and obs.annotated is None
    assert obs.scale == 1.0
    with pytest.raises(ElementNotFound):
        obs.element(999)
    with pytest.raises(ElementNotFound):
        obs.element("abc")
    with pytest.raises(ValueError):
        obs.image_base64()


def test_text_rendering_uses_image_coordinates(scene):
    _, d, w = scene
    obs = observe(d)
    text = obs.text()
    assert "[1] button \"OK\"" in text
    c = obs.to_image(*w["ok"].screen_rect.center)
    assert f"at ({c.x}, {c.y})" in text
    assert 'Active window: "Invoice"' in text
    assert "[occluded]" in text
    short = obs.text(max_elements=1)
    assert "more elements not shown" in short


def test_window_scope_and_non_interactive(scene):
    _, d, _ = scene
    obs = observe(d, window="Invoice", interactive_only=False)
    assert obs.region == Rect(200, 100, 400, 300)
    assert obs.screenshot.size == (400, 300)
    names = {e.name for e in obs.elements}
    assert "Total: 42" in names and "Hidden" not in names
    p = obs.to_screen(0, 0)
    assert p == Point(200, 100)


def test_max_elements_and_add_element(scene):
    be, d, w = scene
    obs = observe(d, max_elements=2)
    assert len(obs.elements) == 2
    node = d.find('checkbox "Agree"').node
    entry = obs.add_element(node)
    assert entry.id == 3 and entry.role == "checkbox"
    assert obs.add_element(node) is entry  # no duplicates
    assert obs.to_dict()["elements"][2]["name"] == "Agree"


def test_empty_desktop():
    _, d = make_desktop(800, 600)
    obs = observe(d)
    assert obs.elements == []
    assert "No accessible interactive elements" in obs.text()
    assert isinstance(obs, Observation)
