from __future__ import annotations

import base64
import io
import math

import pytest
from PIL import Image

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.llm.computer_use import (
    ACTIONS,
    NOT_EXECUTED,
    ComputerUseAdapter,
    map_key,
    parse_key_combo,
    parse_key_sequence,
)
from pypilot.llm.safety import Policy


def setup(width=1920, height=1080, **kw):
    be = MockBackend(width=width, height=height)
    d = Desktop(be, failsafe=False, sleep=lambda s: None)
    win = be.add_window("Form", (400, 300, 800, 600), app="erp")
    w = {
        "edit": win.add_edit("Name", (40, 60, 300, 30)),
        "ok": win.add_button("OK", (40, 500, 100, 40)),
        "submit": win.add_button("Submit", (200, 500, 100, 40)),
    }
    cu = ComputerUseAdapter(d, policy=kw.pop("policy", Policy()), **kw)
    return be, d, cu, w


def img_of(result):
    return Image.open(io.BytesIO(base64.b64decode(result.image_base64)))


def block(name, input=None, id=None):
    return {"type": "tool_use", "id": id or f"toolu_{name}", "name": name, "input": input or {}, "toolset_name": "computer"}


# --------------------------------------------------------------------------- keys


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Return", "enter"), ("Tab", "tab"), ("BackSpace", "backspace"), ("Escape", "escape"),
        ("Page_Up", "pageup"), ("Prior", "pageup"), ("Next", "pagedown"), ("Page_Down", "pagedown"),
        ("super", "cmd"), ("Super_L", "cmd"), ("KP_Enter", "num_enter"), ("F1", "f1"), ("F12", "f12"),
        ("Delete", "delete"), ("Home", "home"), ("Left", "left"), ("space", "space"), ("KP_5", "num5"),
        ("plus", "+"), ("minus", "-"), ("period", "."), ("a", "a"), ("A", "A"), ("Control_L", "ctrl"),
        ("XF86AudioMute", "volumemute"),
    ],
)
def test_map_key(name, expected):
    assert map_key(name) == expected


def test_parse_key_combos():
    assert parse_key_combo("ctrl+s") == ["ctrl", "s"]
    assert parse_key_combo("alt+Tab") == ["alt", "tab"]
    assert parse_key_combo("ctrl+shift+t") == ["ctrl", "shift", "t"]
    assert parse_key_combo("ctrl+S") == ["ctrl", "s"]
    assert parse_key_combo("super") == ["cmd"]
    assert parse_key_combo("ctrl+plus") == ["ctrl", "+"]
    assert parse_key_combo("ctrl++") == ["ctrl", "+"]
    assert parse_key_combo("+") == ["+"]
    assert parse_key_sequence("ctrl+a Delete") == [["ctrl", "a"], ["delete"]]
    with pytest.raises(ValueError):
        parse_key_combo("ctrl+bogus_key")
    with pytest.raises(ValueError):
        parse_key_sequence("")


# --------------------------------------------------------------------- geometry


def test_tool_definition():
    _, _, cu, _ = setup()
    assert cu.tool_definition() == {"type": "computer_toolset_20260801"}
    _, _, cu2, _ = setup(zoom=False)
    assert cu2.tool_definition() == {"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}}}
    r = cu2.execute("zoom", {"region": [0, 0, 10, 10]})
    assert r.is_error and "disabled" in r.text
    assert len(ACTIONS) == 17


@pytest.mark.parametrize("size,expected", [((2560, 1440), (2560, 1440)), ((3840, 2160), (2576, 1449)), ((1920, 1080), (1920, 1080))])
def test_screenshot_size_limits(size, expected):
    _, _, cu, _ = setup(*size)
    assert cu.image_size() == expected
    r = cu.execute("screenshot", {})
    assert not r.is_error
    img = img_of(r)
    assert img.size == expected
    assert max(img.size) <= 2576
    assert math.ceil(img.width / 28) * math.ceil(img.height / 28) <= 4784


@pytest.mark.parametrize("size", [(2560, 1440), (3840, 2160)])
def test_coordinate_scaling_roundtrip_and_clicks(size):
    be, d, cu, w = setup(*size)
    iw, ih = cu.image_size()
    for ix in range(0, iw, 97):
        for iy in range(0, ih, 89):
            sp = cu.to_screen(ix, iy)
            assert cu.to_image(*sp) == (ix, iy)
    # click the OK button through image coordinates
    target = cu.to_image(*w["ok"].screen_rect.center)
    r = cu.execute("left_click", {"coordinate": list(target)})
    assert not r.is_error and r.text == "OK"
    assert w["ok"].clicks == 1
    down = [e for e in be.events if e[0] == "down"][-1]
    assert w["ok"].screen_rect.contains(down[2:])
    # the cursor is reported back in image space
    pos = cu.execute("cursor_position", {})
    assert pos.text == f"[{target[0]}, {target[1]}]"
    # bottom-right pixel maps inside the screen
    assert cu.to_screen(iw - 1, ih - 1) == (size[0] - 1, size[1] - 1)
    bad = cu.execute("left_click", {"coordinate": [iw, 10]})
    assert bad.is_error and "outside the screenshot bounds" in bad.text


def test_zoom_returns_scaled_region():
    _, _, cu, _ = setup(3840, 2160)
    iw, ih = cu.image_size()
    r = cu.execute("zoom", {"region": [0, 0, 400, 300]})
    assert not r.is_error
    img = img_of(r)
    assert img.width <= iw and img.height <= ih
    assert abs(img.width / img.height - 400 / 300) < 0.01
    assert img.height == ih  # scaled up to fill the usual screenshot size
    for region in ([0, 0, 0, 10], [10, 10, 5, 20], [0, 0, iw + 5, 10], "nope", [1, 2, 3]):
        assert cu.execute("zoom", {"region": region}).is_error


# ------------------------------------------------------------------------ actions


def test_type_key_and_modifiers():
    be, d, cu, w = setup()
    c = cu.to_image(*w["edit"].screen_rect.center)
    cu.execute("left_click", {"coordinate": list(c)})
    assert cu.execute("type", {"text": "hello"}).text == "OK"
    assert w["edit"].value == "hello"
    cu.execute("key", {"text": "BackSpace", "repeat": 2})
    assert w["edit"].value == "hel"
    cu.execute("key", {"text": "ctrl+a"})
    cu.execute("key", {"text": "Delete"})
    assert w["edit"].value == ""
    assert cu.execute("key", {"text": "Return", "repeat": 0}).is_error
    assert cu.execute("key", {"text": "Return", "repeat": 101}).is_error
    assert cu.execute("key", {}).is_error
    assert cu.execute("type", {}).is_error

    be.clear_events()
    ok = cu.to_image(*w["ok"].screen_rect.center)
    cu.execute("left_click", {"coordinate": list(ok), "text": "ctrl+shift"})
    kinds = [e[:2] for e in be.events if e[0] in ("key_down", "key_up", "down", "up")]
    assert kinds == [("key_down", "ctrl"), ("key_down", "shift"), ("down", "left"), ("up", "left"), ("key_up", "shift"), ("key_up", "ctrl")]
    assert cu.execute("left_click", {"coordinate": list(ok), "text": "a"}).is_error


def test_click_variants_drag_move_and_buttons():
    be, d, cu, w = setup()
    ok = list(cu.to_image(*w["ok"].screen_rect.center))
    cu.execute("double_click", {"coordinate": ok})
    assert w["ok"].clicks == 2
    cu.execute("triple_click", {"coordinate": ok})
    assert w["ok"].clicks == 5
    cu.execute("right_click", {"coordinate": ok})
    cu.execute("middle_click", {})
    assert {e[1] for e in be.events if e[0] == "down"} >= {"left", "right", "middle"}
    cu.execute("mouse_move", {"coordinate": [10, 20]})
    assert be.mouse_position() == cu.to_screen(10, 20)
    # Press inside the form: on bare desktop nothing can be hit-tested (an unknown target).
    cu.execute("mouse_move", {"coordinate": list(cu.to_image(450, 320))})
    be.clear_events()
    cu.execute("left_mouse_down", {})
    cu.execute("left_mouse_up", {})
    assert [e[0] for e in be.events if e[0] in ("down", "up")] == ["down", "up"]
    be.clear_events()
    r = cu.execute("left_click_drag", {"start_coordinate": [100, 100], "coordinate": [300, 200]})
    assert not r.is_error, r.text
    downs = [e for e in be.events if e[0] == "down"]
    ups = [e for e in be.events if e[0] == "up"]
    assert downs[0][2:] == tuple(cu.to_screen(100, 100)) and ups[-1][2:] == tuple(cu.to_screen(300, 200))
    assert cu.execute("left_click_drag", {"coordinate": [1, 1]}).is_error
    assert cu.execute("mouse_move", {}).is_error


def test_scroll_wait_hold():
    be, d, cu, w = setup()
    for direction, expected in (("up", (0, 3)), ("down", (0, -5)), ("left", (-2, 0)), ("right", (4, 0))):
        amount = abs(sum(expected))
        r = cu.execute("scroll", {"scroll_direction": direction, "scroll_amount": amount, "coordinate": [50, 60]})
        assert not r.is_error
        ev = [e for e in be.events if e[0] == "scroll"][-1]
        assert ev[1:3] == expected
        assert ev[3:] == tuple(cu.to_screen(50, 60))
    assert cu.execute("scroll", {"scroll_direction": "diagonal", "scroll_amount": 1}).is_error
    slept = []
    d._sleep_fn = slept.append
    assert not cu.execute("wait", {"duration": 1.5}).is_error
    assert sum(slept) == pytest.approx(1.5)  # slept in abort-aware slices
    assert cu.execute("wait", {"duration": 301}).is_error
    assert cu.execute("wait", {}).is_error
    be.clear_events()
    slept.clear()
    assert not cu.execute("hold_key", {"text": "shift", "duration": 2}).is_error
    assert sum(slept) == pytest.approx(2.0)
    assert [e for e in be.events if e[0].startswith("key")] == [("key_down", "shift"), ("key_up", "shift")]
    assert cu.execute("hold_key", {"text": "shift", "duration": 400}).is_error


def test_legacy_and_unknown_actions():
    be, d, cu, w = setup()
    ok = list(cu.to_image(*w["ok"].screen_rect.center))
    r = cu.execute("computer", {"action": "left_click", "coordinate": ok})
    assert not r.is_error and w["ok"].clicks == 1
    assert cu.execute("computer", {}).is_error
    assert "Unknown computer action" in cu.execute("fly", {}).text
    legacy_block = {"type": "tool_use", "id": "t1", "name": "computer", "input": {"action": "screenshot"}}
    res = cu.run_batch([legacy_block])
    assert "toolset_name" not in res[0]
    assert res[0]["content"][0]["type"] == "image"
    assert ComputerUseAdapter.is_computer_block(legacy_block)
    assert ComputerUseAdapter.is_computer_block(block("left_click"))
    assert not ComputerUseAdapter.is_computer_block({"type": "tool_use", "name": "done", "id": "x", "input": {}})


# -------------------------------------------------------------------------- batch


def test_batch_success_all_results_have_toolset_name():
    be, d, cu, w = setup()
    c = list(cu.to_image(*w["edit"].screen_rect.center))
    blocks = [
        block("left_click", {"coordinate": c}, "a"),
        block("type", {"text": "batch"}, "b"),
        block("screenshot", {}, "c"),
        block("cursor_position", {}, "d"),
    ]
    results = cu.run_batch(blocks)
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c", "d"]
    assert all(r["toolset_name"] == "computer" and r["type"] == "tool_result" for r in results)
    assert not any(r.get("is_error") for r in results)
    assert results[0]["content"] == [{"type": "text", "text": "OK"}]
    assert results[2]["content"][0]["type"] == "image"
    assert results[2]["content"][0]["source"]["media_type"] == "image/png"
    assert results[3]["content"] == [{"type": "text", "text": f"[{c[0]}, {c[1]}]"}]
    assert w["edit"].value == "batch"


def test_batch_stops_at_first_failure():
    be, d, cu, w = setup()
    iw, ih = cu.image_size()
    ok = list(cu.to_image(*w["ok"].screen_rect.center))
    blocks = [
        block("left_click", {"coordinate": ok}, "1"),
        block("left_click", {"coordinate": [iw + 100, 5]}, "2"),
        block("type", {"text": "never"}, "3"),
        block("key", {"text": "Return"}, "4"),
    ]
    results = cu.run_batch(blocks)
    assert w["ok"].clicks == 1
    assert not results[0].get("is_error")
    assert results[1]["is_error"] is True and "outside the screenshot bounds" in results[1]["content"]
    for r in results[2:]:
        assert r == {"type": "tool_result", "tool_use_id": r["tool_use_id"], "toolset_name": "computer",
                     "is_error": True, "content": NOT_EXECUTED}
    assert not any(e[0] in ("type", "key_down") for e in be.events)
    assert all(r["toolset_name"] == "computer" for r in results)


# -------------------------------------------------------------------------- safety


def test_policy_blocks_sensitive_click_and_audits():
    be, d, cu, w = setup()
    sub = list(cu.to_image(*w["submit"].screen_rect.center))
    r = cu.execute("left_click", {"coordinate": sub})
    assert r.is_error and "Blocked by policy" in r.text and "Submit" in r.text
    assert w["submit"].clicks == 0
    assert cu.audit.events[-1]["status"] == "blocked"
    approved = []
    cu.policy.confirm = lambda desc, details: approved.append(details["target"]) or True
    assert not cu.execute("left_click", {"coordinate": sub}).is_error
    assert w["submit"].clicks == 1 and approved == ["Submit"]


def test_dry_run_reports_without_acting():
    be, d, cu, w = setup(policy=Policy(dry_run=True))
    ok = list(cu.to_image(*w["ok"].screen_rect.center))
    shot = cu.execute("screenshot", {})
    assert shot.image_base64  # read-only actions still run
    be.clear_events()
    for name, inp in [("left_click", {"coordinate": ok}), ("type", {"text": "x"}), ("key", {"text": "Return"}),
                      ("scroll", {"scroll_direction": "down", "scroll_amount": 2}), ("mouse_move", {"coordinate": [5, 5]}),
                      ("left_click_drag", {"start_coordinate": [1, 1], "coordinate": [9, 9]}), ("hold_key", {"text": "shift", "duration": 1})]:
        r = cu.execute(name, inp)
        assert not r.is_error and r.text.startswith("[dry-run] Would"), (name, r.text)
    assert [e for e in be.events if e[0] != "screenshot"] == []
    assert w["ok"].clicks == 0
