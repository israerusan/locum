"""Allow-list (default-deny) authorization: Policy(allow=[...]) / Policy.strict(allow=[...])."""

from __future__ import annotations

import pytest

from pypilot.backends.mock import MockBackend
from pypilot.desktop import Desktop
from pypilot.errors import PolicyViolation
from pypilot.llm.agent import Agent, ScriptedClient, scripted_response, text_block, tool_use_block
from pypilot.llm.computer_use import ComputerUseAdapter
from pypilot.llm.safety import READ_ONLY_TOOLS, AllowRule, AuditLog, Policy, keys_navigate_only
from pypilot.llm.tools import DesktopToolkit, ToolSpec


def _fake_time():
    now = [0.0]

    def sleep(seconds):
        now[0] += max(0.0, float(seconds))

    return {"sleep": sleep, "clock": lambda: now[0]}


class _Backend(MockBackend):
    """Like UIA/AT-SPI, the focused element knows its parents (the mock's plain node does not)."""

    def focused_element(self):
        return self._node_with_parents(self._focus)


@pytest.fixture
def app():
    be = _Backend(width=1280, height=800)
    d = Desktop(be, failsafe=False, default_timeout=0.2, **_fake_time())
    notes = be.add_window("Notes", (700, 100, 400, 300), app="notepad")
    notes.add_edit("Text", (10, 40, 380, 200))
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    w = {
        "ok": win.add_button("OK", (20, 340, 80, 30)),
        "save": win.add_button("Save", (120, 340, 80, 30)),
        "apply": win.add_button("Apply", (220, 340, 80, 30)),
        "amount": win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount"),
        "win": win,
        "notes": notes,
    }
    return be, d, w


def _toolkit(d, allow, **kw):
    asked = []
    kw.setdefault("confirm", lambda desc, details: asked.append(details["reason"]) or False)
    policy = Policy(allow=allow, **kw)
    return DesktopToolkit(d, policy, AuditLog(), find_timeout=0.1), asked


# ---------------------------------------------------------------------------- rules


def test_allow_rule_shorthands_and_dicts():
    assert AllowRule.coerce('button "Save"') == AllowRule(selector='button "Save"')
    assert AllowRule.coerce("selector=edit#amount") == AllowRule(selector="edit#amount")
    assert AllowRule.coerce("window=Invoice*") == AllowRule(window="Invoice*")
    assert AllowRule.coerce("app=excel") == AllowRule(app="excel")
    assert AllowRule.coerce("launch=notepad.exe") == AllowRule(command="notepad.exe")
    assert AllowRule.coerce("keys=ctrl+s ctrl+p").keys == ("ctrl+s", "ctrl+p")
    assert AllowRule.coerce("tool=write_clipboard").tools == ("write_clipboard",)
    assert AllowRule.coerce({"selector": 'button "OK"', "window": "Invoice*"}) == \
        AllowRule(selector='button "OK"', window="Invoice*")
    assert AllowRule.coerce({"launch": "calc"}).command == "calc"
    assert "window='Invoice*'" in str(AllowRule(window="Invoice*"))
    with pytest.raises(ValueError, match="at least one"):
        AllowRule()
    with pytest.raises(ValueError, match="Unknown allow-rule field"):
        AllowRule.coerce({"title": "x"})
    with pytest.raises(TypeError):
        AllowRule.coerce(42)
    with pytest.raises(Exception):
        AllowRule(selector='button[name="unterminated')  # a typo fails at configuration time


def test_policy_allow_accepts_single_rule_and_reports_mode():
    p = Policy(allow="window=Invoice*")
    assert p.allow_list_mode and p.allow == (AllowRule(window="Invoice*"),)
    assert "allow-list" in repr(p)
    assert not Policy().allow_list_mode and "heuristic" in repr(Policy())


@pytest.mark.parametrize("keys,expected", [
    ("tab", True), ("shift+tab", True), ("ctrl+right", True), ("tab tab down", True),
    ("enter", False), ("space", False), ("ctrl+a", False), ("escape", False), ("alt+left", False),
    ("delete", False), ("f8", False), ("tab enter", False), ("", False),
])
def test_keys_navigate_only(keys, expected):
    assert keys_navigate_only(keys) is expected


# ---------------------------------------------------------------------- Policy.check


def test_empty_allow_list_confirms_every_consequential_action():
    p = Policy(allow=[])
    for tool, ctx in [("click", {"target": "OK"}), ("type_text", {"target": "Amount"}),
                      ("press_keys", {"keys": "enter"}), ("hotkey", {"keys": "ctrl+a"}), ("set_value", {}),
                      ("launch_app", {"command": "notepad"}), ("write_clipboard", {}), ("invoke", {}),
                      ("drag", {}), ("close_window", {})]:
        with pytest.raises(PolicyViolation, match="no allow rule authorizes"):
            p.check(tool, {}, context=dict(ctx))
    assert p.action_count == 0


def test_read_only_and_non_committing_tools_are_never_gated():
    p = Policy(allow=[])
    for tool in sorted(READ_ONLY_TOOLS):
        p.check(tool, {}, context={})
    for tool in ("scroll", "move_mouse", "mouse_move", "focus_window", "expand", "collapse", "focus"):
        p.check(tool, {}, context={"target": "x"})
    p.check("press_keys", {}, context={"keys": "tab shift+tab down"})
    p.check("key", {}, context={"keys": "Tab"})


def test_keyword_tripwire_still_applies_to_allowed_targets():
    p = Policy(allow=["tool=click"])
    p.check("click", {}, context={"target": "OK"})
    with pytest.raises(PolicyViolation, match="Submit"):
        p.check("click", {}, context={"target": "Submit"})
    # Explicitly turning the tripwire off makes the allow rules final.
    Policy(allow=["tool=click"], confirm_patterns=()).check("click", {}, context={"target": "Submit"})


def test_tripwire_is_never_the_only_gate_in_allow_list_mode():
    p = Policy(allow=[], confirm_patterns=(), confirm_keys=())
    with pytest.raises(PolicyViolation, match="no allow rule"):
        p.check("click", {}, context={"target": "Continue"})
    with pytest.raises(PolicyViolation, match="always requires"):
        Policy(allow=["tool=erp_api"], confirm_patterns=()).check(
            "erp_api", {}, context={"integration_tool": True, "require_confirmation": True})


def test_allowed_action_records_the_rule_and_dry_run_reports_missing_authorization():
    ctx = {"target_window": "Invoice 42"}
    Policy(allow=["window=Invoice*"]).check("click", {}, context=ctx)
    assert ctx["allowed_by"] == "AllowRule(window='Invoice*')"
    ctx = {"target": "Apply"}
    p = Policy(allow=[], dry_run=True)
    p.check("click", {}, context=ctx)
    assert "no allow rule" in ctx["needs_confirmation"]


def test_confirm_callback_approves_unlisted_actions():
    asked = []
    p = Policy(allow=[], confirm=lambda d, x: asked.append(x["reason"]) or True)
    p.check("click", {}, context={"target": "Apply", "description": "click Apply"})
    assert asked == ["no allow rule authorizes 'click' on 'Apply'"]
    assert p.confirmations == [("click Apply", True)]
    override = []
    p.check("click", {}, context={"target": "Apply"}, confirm=lambda d, x: override.append(d) or True)
    assert len(override) == 1 and len(asked) == 1


def test_window_and_app_rules_follow_the_target_not_the_active_window():
    p = Policy(allow=["window=Invoice*"])
    p.check("click", {}, context={"target_window": "Invoice 42", "window_title": "Other"})
    with pytest.raises(PolicyViolation):
        p.check("click", {}, context={"target_window": "Other", "window_title": "Invoice 42"})
    # A pointer action whose window is unknown is not covered by the active window's title.
    with pytest.raises(PolicyViolation):
        p.check("click", {}, context={"point": (5, 5), "window_title": "Invoice 42"})
    # Keyboard input (no point) goes to the active window.
    p.check("type_text", {"text": "x"}, context={"window_title": "Invoice 42"})
    a = Policy(allow=["app=erp"])
    a.check("click", {}, context={"target_apps": ["erp"]})
    with pytest.raises(PolicyViolation):
        a.check("drag", {}, context={"target_apps": ["erp", "explorer"]})


def test_launch_keys_and_tool_rules_only_cover_what_they_name():
    p = Policy(allow=["launch=notepad", "keys=ctrl+s", "tool=write_clipboard", "window=*"])
    p.check("launch_app", {"command": "notepad.exe C:\\notes.txt"}, context={})
    p.check("launch_app", {"command": '"C:\\Windows\\notepad.exe"'}, context={})
    with pytest.raises(PolicyViolation):
        p.check("launch_app", {"command": "powershell -c whoami"}, context={})
    p.check("write_clipboard", {"text": "x"}, context={})
    k = Policy(allow=[AllowRule(keys=("ctrl+s",), window="Invoice*")], confirm_keys=())
    k.check("hotkey", {}, context={"keys": "ctrl+s", "window_title": "Invoice 42"})
    with pytest.raises(PolicyViolation):
        k.check("hotkey", {}, context={"keys": "ctrl+s", "window_title": "Bank"})
    with pytest.raises(PolicyViolation):
        k.check("hotkey", {}, context={"keys": "ctrl+shift+s", "window_title": "Invoice 42"})
    # A window rule does not authorize launching programs or the clipboard.
    w = Policy(allow=["window=*"])
    for tool in ("launch_app", "write_clipboard"):
        with pytest.raises(PolicyViolation):
            w.check(tool, {"command": "notepad"}, context={"window_title": "Invoice 42"})


def test_integration_tools_need_a_rule_naming_them():
    p = Policy(allow=["window=*", "tool=erp_lookup"])
    p.check("erp_lookup", {}, context={"integration_tool": True, "target": "erp lookup"})
    with pytest.raises(PolicyViolation):
        p.check("erp_post", {}, context={"integration_tool": True, "target": "erp post", "window_title": "x"})


def test_strict_allow_lifts_default_denies_only_for_named_tools():
    assert "launch_app" in Policy.strict().deny_tools
    p = Policy.strict(allow=["launch=notepad", 'button "OK"'])
    assert "launch_app" not in p.deny_tools and "write_clipboard" in p.deny_tools
    assert p.allow_list_mode and len(p.allow) == 2
    p.check("launch_app", {"command": "notepad"}, context={})
    with pytest.raises(PolicyViolation, match="not allowed"):
        Policy.strict(allow=["launch=notepad"], deny_tools=["launch_app"]).check(
            "launch_app", {"command": "notepad"}, context={})
    assert "write_clipboard" not in Policy.strict(allow="tool=write_clipboard").deny_tools


# ------------------------------------------------------------------------- toolkit


def test_toolkit_selector_rule_authorizes_exactly_that_element(app):
    be, d, w = app
    tk, asked = _toolkit(d, ['window "Invoice 42" button "OK"', "edit#amount"])
    assert not tk.call("click", {"selector": 'button "OK"'}).is_error
    assert not tk.call("type_text", {"selector": "edit#amount", "text": "12"}).is_error
    assert w["ok"].clicks == 1 and w["amount"].value == "12" and asked == []
    for name in ("Save", "Apply"):  # Apply is a commit no keyword list knows about
        r = tk.call("click", {"selector": f'button "{name}"'})
        assert r.is_error and "declined" in r.text
    assert w["save"].clicks == 0 and w["apply"].clicks == 0
    assert asked == ["no allow rule authorizes 'click' on 'Save'", "no allow rule authorizes 'click' on 'Apply'"]


def test_toolkit_selector_rule_matches_coordinate_and_element_id_targets(app):
    be, d, w = app
    tk, asked = _toolkit(d, ['button "OK"'])
    c = w["ok"].screen_rect.center
    assert not tk.call("click", {"x": c.x, "y": c.y}).is_error
    obs = tk.call("observe", {"include_screenshot": False}).data
    ok_id = next(e.id for e in obs.elements if e.name == "OK")
    save_id = next(e.id for e in obs.elements if e.name == "Save")
    assert not tk.call("click", {"element_id": ok_id}).is_error
    assert tk.call("click", {"element_id": save_id}).is_error
    assert w["ok"].clicks == 2 and w["save"].clicks == 0


def test_toolkit_window_rule(app):
    be, d, w = app
    tk, asked = _toolkit(d, ["window=Invoice*"])
    assert not tk.call("click", {"selector": 'button "Apply"'}).is_error
    r = tk.call("click", {"selector": 'edit "Text"'})  # in the Notes window
    assert r.is_error and asked


def test_toolkit_keyboard_rules_and_navigation(app):
    be, d, w = app
    d.find('button "Save"').focus()
    tk, asked = _toolkit(d, ["keys=ctrl+a"], confirm_keys=())
    assert not tk.call("press_keys", {"keys": ["tab", "shift+tab", "tab"]}).is_error
    assert asked == []
    assert not tk.call("hotkey", {"combo": "ctrl+a"}).is_error
    r = tk.call("press_keys", {"keys": ["enter"]})
    assert r.is_error and asked == ["no allow rule authorizes 'press_keys' on 'Apply'"]  # the Tabs moved the focus


def test_toolkit_tab_then_enter_rechecks_the_new_focus(app):
    be, d, w = app
    d.find('button "OK"').focus()
    tk, asked = _toolkit(d, [AllowRule(selector='button "OK"', keys=("tab", "enter"))])
    # The call as a whole is authorized on OK, but the Tab moves the focus to Save:
    # the Enter is re-judged there and no rule covers Enter on Save.
    r = tk.call("press_keys", {"keys": ["tab", "enter"]})
    assert r.is_error and "already sent" in r.text
    assert asked == ["no allow rule authorizes 'press_keys' on 'Save'"]
    assert w["ok"].clicks == 0 and w["save"].clicks == 0
    d.find('button "OK"').focus()
    asked.clear()
    assert not tk.call("press_keys", {"keys": ["enter"]}).is_error
    assert w["ok"].clicks == 1 and asked == []


def test_toolkit_launch_and_clipboard_need_explicit_rules(app):
    be, d, w = app
    tk, asked = _toolkit(d, ["window=*"])
    assert tk.call("launch_app", {"command": "notepad"}).is_error
    assert tk.call("write_clipboard", {"text": "x"}).is_error
    assert be.launched == [] and be.clipboard == ""
    tk2, _ = _toolkit(d, ["launch=notepad", "tool=write_clipboard"])
    assert not tk2.call("launch_app", {"command": "notepad"}).is_error
    assert not tk2.call("write_clipboard", {"text": "x"}).is_error
    assert be.launched == ["notepad"] and be.clipboard == "x"


def test_toolkit_read_only_tools_run_with_an_empty_allow_list(app):
    be, d, w = app
    tk, asked = _toolkit(d, [])
    for name, args in [("observe", {"include_screenshot": False}), ("list_windows", {}),
                       ("find_elements", {"role": "button"}), ("read_clipboard", {}), ("wait", {"seconds": 0})]:
        r = tk.call(name, args)
        assert not r.is_error, (name, r.text)
    assert not tk.call("scroll", {"direction": "down"}).is_error
    assert not tk.call("focus_window", {"title": "Notes"}).is_error
    assert asked == []


def test_computer_use_allow_list(app):
    be, d, w = app
    asked = []
    cu = ComputerUseAdapter(d, policy=Policy(allow=['button "OK"'], confirm=lambda *a: asked.append(a[1]["reason"]) or False))
    ok = cu.to_image(*w["ok"].screen_rect.center)
    save = cu.to_image(*w["save"].screen_rect.center)
    assert not cu.execute("left_click", {"coordinate": [ok.x, ok.y]}).is_error
    assert cu.execute("left_click", {"coordinate": [save.x, save.y]}).is_error
    assert not cu.execute("mouse_move", {"coordinate": [save.x, save.y]}).is_error
    assert not cu.execute("key", {"text": "Tab"}).is_error
    assert w["ok"].clicks == 1 and w["save"].clicks == 0
    assert asked == ["no allow rule authorizes 'left_click' on 'Save'"]


def test_agent_extra_tools_are_default_denied(app):
    be, d, w = app
    calls = []

    def erp_post(doc: str) -> str:
        """Post a document."""
        calls.append(doc)
        return "posted"

    def erp_lookup(doc: str) -> str:
        """Look up a document."""
        calls.append(("lookup", doc))
        return "found"

    client = ScriptedClient([
        scripted_response(tool_use_block("erp_lookup", {"doc": "1"}, id="t1"), tool_use_block("erp_post", {"doc": "1"}, id="t2")),
        scripted_response(text_block("done")),
    ])
    agent = Agent(client=client, desktop=d, policy=Policy(allow=["tool=erp_lookup"]),
                  extra_tools=[ToolSpec.from_function(erp_lookup), ToolSpec.from_function(erp_post)])
    agent.run("do it")
    assert calls == [("lookup", "1")]


def test_enter_in_an_allowed_field_is_judged_by_the_default_button(app):
    be, d, w = app
    w["win"].add_button("Pay now", (320, 300, 100, 30), attrs={"default": True})
    d.find("edit#amount").focus()
    tk, asked = _toolkit(d, ["edit#amount"], confirm_patterns=())
    assert not tk.call("type_text", {"text": "12"}).is_error
    r = tk.call("press_keys", {"keys": ["enter"]})
    assert r.is_error and asked == ["no allow rule authorizes 'press_keys' on 'Amount -> Pay now'"]
    r = tk.call("type_text", {"text": "3\n"})
    assert r.is_error and len(asked) == 2
