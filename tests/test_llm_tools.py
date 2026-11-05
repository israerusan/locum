from __future__ import annotations

import base64
from typing import List, Literal, Optional

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import FailSafeTriggered, PolicyViolation
from pypilot.llm.safety import AuditLog, Policy
from pypilot.llm.tools import DesktopToolkit, ToolRegistry, ToolResult, ToolSpec, parse_docstring, schema_from_function


# ============================================================================ registry


def test_parse_docstring():
    desc, args = parse_docstring(
        """Do a thing.

        More detail here.

        Args:
            a (int): First value
                continued on the next line.
            b: Second value.

        Returns:
            Something.
        """
    )
    assert desc == "Do a thing.\n\nMore detail here."
    assert args == {"a": "First value continued on the next line.", "b": "Second value."}
    assert parse_docstring(None) == ("", {})


def test_schema_from_type_hints():
    def f(name: str, count: int, ratio: float = 0.5, flag: bool = False, tags: Optional[List[str]] = None,
          mode: Literal["fast", "slow"] = "fast", anything=None) -> None:
        """Summary.

        Args:
            name: The name.
            tags: Some tags.
        """

    desc, schema = schema_from_function(f)
    assert desc == "Summary."
    props = schema["properties"]
    assert schema["required"] == ["name", "count"]
    assert props["name"] == {"type": "string", "description": "The name."}
    assert props["count"] == {"type": "integer"}
    assert props["ratio"] == {"type": "number", "default": 0.5}
    assert props["flag"] == {"type": "boolean", "default": False}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}, "description": "Some tags."}
    assert props["mode"] == {"enum": ["fast", "slow"], "type": "string", "default": "fast"}
    assert props["anything"] == {}


def test_registry_register_call_and_exports():
    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int = 0) -> int:
        """Add numbers.

        Args:
            a: First.
            b: Second.
        """
        return a + b

    @reg.register(name="boom", description="Always fails")
    def _boom() -> None:
        raise RuntimeError("kaput")

    @reg.register
    def blocked() -> None:
        """Blocked by policy."""
        raise PolicyViolation("nope")

    @reg.register
    def info() -> dict:
        """Structured output."""
        return {"k": [1, 2]}

    assert add(1, 2) == 3  # decorator returns the function
    assert reg.names() == ["add", "boom", "blocked", "info"]
    assert "add" in reg and len(reg) == 4

    r = reg.call("add", {"a": "2", "b": 3.0})
    assert r == ToolResult("5", data=5) or r.text == "5"
    assert reg.call("add", {"a": 1}).text == "1"
    assert reg.call("add", {}).is_error
    assert "Missing" in reg.call("add", {}).text
    bad = reg.call("add", {"a": 1, "c": 2})
    assert bad.is_error and "Unexpected" in bad.text and "a, b" in bad.text
    assert reg.call("add", {"a": "xyz"}).is_error
    assert reg.call("add", ["not", "a", "dict"]).is_error
    unknown = reg.call("nope", {})
    assert unknown.is_error and "Unknown tool" in unknown.text
    boom = reg.call("boom", {})
    assert boom.is_error and "RuntimeError: kaput" in boom.text
    pv = reg.call("blocked", {})
    assert pv.is_error and pv.text.startswith("Blocked by policy")
    out = reg.call("info", {})
    assert out.text == '{"k": [1, 2]}' and out.data == {"k": [1, 2]}

    a = reg.to_anthropic()[0]
    assert a["name"] == "add" and a["input_schema"]["required"] == ["a"]
    assert reg.to_openai()[0]["function"]["parameters"]["properties"]["b"]["default"] == 0
    assert reg.to_mcp()[1] == {"name": "boom", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}}


def test_failsafe_propagates_through_registry():
    reg = ToolRegistry()

    @reg.register
    def stop() -> None:
        """Abort."""
        raise FailSafeTriggered("corner")

    with pytest.raises(FailSafeTriggered):
        reg.call("stop", {})


def test_tool_result_content_shapes():
    r = ToolResult("hi", image_base64="QUJD")
    blocks = r.to_anthropic_content()
    assert blocks[0] == {"type": "text", "text": "hi"}
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    assert r.to_mcp_content()[1] == {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    assert ToolResult().to_anthropic_content() == [{"type": "text", "text": "OK"}]
    err = ToolResult.error("bad").to_anthropic("toolu_1")
    assert err["is_error"] is True and err["tool_use_id"] == "toolu_1"
    assert ToolResult(image_base64="QUJD").to_mcp_content() == [{"type": "image", "data": "QUJD", "mimeType": "image/png"}]
    with pytest.raises(ValueError):
        ToolSpec.from_function(lambda: None, name="bad name!")


# ============================================================================ toolkit


def fake_time():
    """A sleep that advances a fake clock, so polling waits are instant and deterministic."""
    now = [0.0]

    def sleep(seconds):
        now[0] += max(0.0, float(seconds))

    return {"sleep": sleep, "clock": lambda: now[0]}


@pytest.fixture
def app():
    be = MockBackend(width=1920, height=1080)
    d = Desktop(be, failsafe=False, default_timeout=0.2, **fake_time())
    win = be.add_window("Invoice", (100, 100, 500, 400), app="erp")
    w = {
        "win": win,
        "name": win.add_edit("Customer", (20, 50, 250, 28)),
        "amount": win.add_edit("Amount", (20, 90, 250, 28), value="0"),
        "ok": win.add_button("OK", (20, 340, 80, 30)),
        "submit": win.add_button("Submit payment", (120, 340, 140, 30)),
        "agree": win.add_checkbox("Agree", (20, 140, 150, 24)),
        "list": win.add_list("Items", (300, 50, 150, 100), items=["Apple", "Banana", "Cherry"]),
    }
    audit = AuditLog()
    tk = DesktopToolkit(d, Policy(), audit, find_timeout=0.1)
    return be, d, tk, w


def _id(obs, name):
    return next(e.id for e in obs.elements if e.name == name)


def test_toolkit_tool_names(app):
    _, _, tk, _ = app
    expected = {"observe", "screenshot", "click", "double_click", "right_click", "type_text", "press_keys", "hotkey", "scroll",
                "move_mouse", "drag", "set_value", "find_elements", "wait_for", "list_windows", "focus_window", "launch_app",
                "read_clipboard", "write_clipboard", "read_text", "wait"}
    assert set(tk.names()) == expected
    mcp = {t["name"]: t for t in tk.to_mcp()}
    assert mcp["observe"]["annotations"] == {"readOnlyHint": True}
    assert "annotations" not in mcp["click"]


def test_observe_then_act_by_element_id(app):
    be, d, tk, w = app
    r = tk.call("observe", {})
    assert not r.is_error and r.image_base64
    assert base64.b64decode(r.image_base64)[:8] == b"\x89PNG\r\n\x1a\n"
    obs = tk.last_observation
    assert '[1] edit "Customer"' in r.text

    res = tk.call("type_text", {"text": "ACME Corp", "element_id": _id(obs, "Customer")})
    assert not res.is_error, res.text
    assert w["name"].value == "ACME Corp"
    assert "Field value is now 'ACME Corp'" in res.text

    res = tk.call("type_text", {"text": "125.50", "element_id": _id(obs, "Amount"), "clear": True})
    assert w["amount"].value == "125.50"

    res = tk.call("click", {"element_id": _id(obs, "OK")})
    assert not res.is_error and w["ok"].clicks == 1
    downs = [e for e in be.events if e[0] == "down"]
    assert downs[-1][2:] == tuple(w["ok"].screen_rect.center)

    tk.call("click", {"element_id": _id(obs, "Agree")})
    assert w["agree"].checked is True


def test_click_by_image_coordinates_maps_to_screen(app):
    be, d, tk, w = app
    tk.call("observe", {})
    obs = tk.last_observation
    assert obs.scale < 1
    ix, iy = obs.to_image(*w["ok"].screen_rect.center)
    res = tk.call("click", {"x": ix, "y": iy})
    assert not res.is_error, res.text
    assert w["ok"].clicks == 1
    assert "OK" in res.text
    bad = tk.call("click", {"x": 99999, "y": 5})
    assert bad.is_error and "outside the screenshot" in bad.text
    half = tk.call("click", {"x": 10})
    assert half.is_error


def test_click_by_selector_and_text(app):
    _, _, tk, w = app
    assert not tk.call("click", {"selector": 'button "OK"'}).is_error
    assert not tk.call("click", {"text": "OK"}).is_error
    assert w["ok"].clicks == 2
    missing = tk.call("click", {"selector": 'button "Nope"'})
    assert missing.is_error and "ElementNotFound" in missing.text
    both = tk.call("click", {"selector": "button", "element_id": 1})
    assert both.is_error and "only one target" in both.text
    none = tk.call("click", {})
    assert none.is_error and "target is required" in none.text


def test_stale_element_id_error(app):
    _, _, tk, _ = app
    tk.call("observe", {})
    r = tk.call("click", {"element_id": 999})
    assert r.is_error and "No element with id 999" in r.text


def test_double_right_click_and_list_selection(app):
    be, _, tk, w = app
    tk.call("observe", {})
    obs = tk.last_observation
    tk.call("double_click", {"element_id": _id(obs, "OK")})
    assert w["ok"].clicks == 2
    tk.call("right_click", {"element_id": _id(obs, "Customer")})
    assert any(e[0] == "down" and e[1] == "right" for e in be.events)
    tk.call("click", {"element_id": _id(obs, "Banana")})
    banana = w["list"].find("Banana")
    assert banana.attrs.get("selected") is True


def test_keyboard_tools(app):
    be, _, tk, w = app
    tk.call("click", {"selector": 'edit "Customer"'})
    tk.call("type_text", {"text": "abc"})
    assert w["name"].value == "abc"
    r = tk.call("press_keys", {"keys": ["backspace", "backspace"]})
    assert not r.is_error and w["name"].value == "a"
    tk.call("hotkey", {"combo": "ctrl+a"})
    tk.call("press_keys", {"keys": "backspace"})  # a lone string is coerced to a list
    assert w["name"].value == ""
    bad = tk.call("press_keys", {"keys": ["notakey"]})
    assert bad.is_error
    bad2 = tk.call("hotkey", {"combo": "ctrl+bogus"})
    assert bad2.is_error
    tk.call("type_text", {"text": "x", "press_enter": True})
    assert ("key_down", "enter") in be.events


def test_set_value_and_read_text(app):
    _, _, tk, w = app
    r = tk.call("set_value", {"selector": 'edit "Amount"', "value": "99"})
    assert not r.is_error and w["amount"].value == "99"
    tk.call("observe", {})
    rt = tk.call("read_text", {"element_id": _id(tk.last_observation, "Amount")})
    assert "value: 99" in rt.text and "name: Amount" in rt.text
    assert tk.call("read_text", {"element_id": 1, "x": 3}).is_error
    assert tk.call("read_text", {"x": 1, "y": 2}).is_error


def test_scroll_move_and_drag(app):
    be, _, tk, w = app
    tk.call("observe", {})
    obs = tk.last_observation
    r = tk.call("scroll", {"direction": "down", "amount": 4, "selector": 'list "Items"'})
    assert not r.is_error, r.text
    ev = [e for e in be.events if e[0] == "scroll"][-1]
    assert ev[1:3] == (0, -4)
    assert ev[3:] == tuple(w["list"].screen_rect.center)
    tk.call("scroll", {"direction": "right", "amount": 2})
    assert [e for e in be.events if e[0] == "scroll"][-1][1:3] == (2, 0)
    assert tk.call("scroll", {"direction": "sideways"}).is_error
    tk.call("move_mouse", {"element_id": _id(obs, "OK")})
    assert be.mouse_position() == w["ok"].screen_rect.center
    r = tk.call("drag", {"start_element_id": _id(obs, "Apple"), "end_element_id": _id(obs, "Cherry"), "duration": 0})
    assert not r.is_error, r.text
    downs = [e for e in be.events if e[0] == "down"]
    ups = [e for e in be.events if e[0] == "up"]
    assert downs[-1][2:] == tuple(w["list"].find("Apple").screen_rect.center)
    assert ups[-1][2:] == tuple(w["list"].find("Cherry").screen_rect.center)


def test_find_elements_assigns_ids(app):
    _, _, tk, w = app
    r = tk.call("find_elements", {"role": "button"})
    assert "2 element(s)" in r.text
    obs = tk.last_observation
    ok_id = _id(obs, "OK")
    assert not tk.call("click", {"element_id": ok_id}).is_error
    assert w["ok"].clicks == 1
    r = tk.call("find_elements", {"text": "ban"})
    assert "Banana" in r.text
    assert "No elements" in tk.call("find_elements", {"selector": 'button "Zzz"'}).text
    assert tk.call("find_elements", {}).is_error


def test_wait_for(app):
    be, _, tk, w = app
    assert "is visible" in tk.call("wait_for", {"selector": 'button "OK"', "timeout": 0}).text
    r = tk.call("wait_for", {"selector": 'button "Later"', "timeout": 0})
    assert r.is_error and "Timed out" in r.text
    w["win"].root.children.remove(w["ok"])
    assert "gone" in tk.call("wait_for", {"selector": 'button "OK"', "state": "gone", "timeout": 0}).text
    assert "is visible" in tk.call("wait_for", {"text": "Submit", "timeout": 0}).text
    assert tk.call("wait_for", {}).is_error


def test_windows_apps_clipboard_wait(app):
    be, d, tk, w = app
    other = be.add_window("Notes", (700, 100, 300, 200), app="notes")
    lw = tk.call("list_windows", {})
    assert "'Notes' app=notes" in lw.text and "[active]" in lw.text
    r = tk.call("focus_window", {"title": "Invoice"})
    assert not r.is_error
    assert be.active_window().title == "Invoice"
    assert tk.call("focus_window", {"title": "Missing"}).is_error
    r = tk.call("launch_app", {"command": "calc.exe"})
    assert not r.is_error and be.launched == ["calc.exe"]
    tk.call("write_clipboard", {"text": "hello"})
    assert be.clipboard == "hello"
    assert tk.call("read_clipboard", {}).text == "hello"
    assert tk.call("wait", {"seconds": 1000}).text == "Waited 60s."
    assert other.title == "Notes"


def test_policy_blocks_and_confirms(app):
    be, d, tk, w = app
    tk.call("observe", {})
    obs = tk.last_observation
    submit = _id(obs, "Submit payment")
    r = tk.call("click", {"element_id": submit})
    assert r.is_error and "Blocked by policy" in r.text and "confirmation" in r.text
    assert w["submit"].clicks == 0
    blocked = [e for e in tk.audit.events if e.get("status") == "blocked"]
    assert blocked and blocked[-1]["tool"] == "click"

    answers = []
    tk.policy.confirm = lambda desc, details: answers.append(desc) or True
    r = tk.call("click", {"element_id": submit})
    assert not r.is_error and w["submit"].clicks == 1
    assert "Submit payment" in answers[0]
    tk.policy.confirm = lambda desc, details: False
    r = tk.call("click", {"text": "Submit payment"})
    assert r.is_error and "declined" in r.text and w["submit"].clicks == 1


def test_dry_run_never_touches_backend(app):
    be, d, _, w = app
    tk = DesktopToolkit(d, Policy(dry_run=True), find_timeout=0.1)
    obs_res = tk.call("observe", {})  # read-only tools still run
    assert not obs_res.is_error
    obs = tk.last_observation
    be.clear_events()
    r = tk.call("click", {"element_id": _id(obs, "OK")})
    assert r.text.startswith("[dry-run] Would click element") and "OK" in r.text
    assert r.data["dry_run"] is True
    r = tk.call("type_text", {"text": "x", "element_id": _id(obs, "Customer")})
    assert "[dry-run]" in r.text
    r = tk.call("click", {"element_id": _id(obs, "Submit payment")})
    assert "would require human confirmation" in r.text
    tk.call("launch_app", {"command": "notepad"})
    tk.call("write_clipboard", {"text": "z"})
    assert [e for e in be.events if e[0] != "screenshot"] == []
    assert w["ok"].clicks == 0 and w["name"].value == "" and be.launched == [] and be.clipboard == ""
    assert {e["status"] for e in tk.audit.events if e["tool"] == "click"} == {"dry_run"}


def test_deny_tools_and_audit(app, tmp_path):
    _, d, _, _ = app
    path = tmp_path / "audit.jsonl"
    tk = DesktopToolkit(d, Policy(deny_tools=["launch_app", "*clipboard*"]), AuditLog(path))
    assert tk.call("launch_app", {"command": "x"}).is_error
    assert "not allowed" in tk.call("read_clipboard", {}).text
    tk.call("observe", {"include_screenshot": False})
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert '"status": "blocked"' in lines[0] and '"status": "ok"' in lines[2]
