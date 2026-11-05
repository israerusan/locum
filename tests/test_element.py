from __future__ import annotations

import pytest

from _stub_desktop import StubDesktop
from pypilot.backends.mock import MockBackend
from pypilot.element import Element
from pypilot.errors import ActionTimeout, ElementNotFound, NotSupported
from pypilot.types import Point, Rect


@pytest.fixture
def env():
    be = MockBackend(width=1280, height=800)
    d = StubDesktop(be)
    win = be.add_window("Form", (100, 100, 500, 400), app="form.exe")
    clicked = []
    ok = win.add_button("OK", (20, 340, 80, 30), automation_id="ok", on_click=lambda w: clicked.append("ok"))
    name = win.add_edit("Name", (20, 40, 200, 28), automation_id="name")
    agree = win.add_checkbox("Agree", (20, 80, 120, 24))
    grp = win.add("group", "Options", (250, 40, 200, 200), automation_id="opts")
    grp.add_button("A", (260, 50, 60, 24))
    grp.add_button("B", (260, 80, 60, 24))
    lbl = win.add_text("Status: idle", (20, 120, 200, 20))
    return d, be, win, {"ok": ok, "name": name, "agree": agree, "grp": grp, "lbl": lbl, "clicked": clicked}


def el(d, sel):
    return d.find(sel, timeout=0)


def mouse_events(be):
    return [e for e in be.events if e[0] in ("down", "up")]


# ------------------------------------------------------------------ properties
def test_properties(env):
    d, be, win, w = env
    e = el(d, "#name")
    assert isinstance(e, Element)
    assert e.role == "edit" and e.name == "Name" and e.value == "" and e.automation_id == "name"
    assert e.bounds == Rect(120, 140, 200, 28)
    assert e.center == Point(220, 154)
    assert e.is_enabled and e.is_visible and not e.is_focused and not e.is_checked
    assert "editable" in e.states
    assert e.selector == "#name"
    assert e.node is not None and e.desktop is d
    assert "set_value" in e.actions
    assert e.text == "Name"
    w["name"].value = "Bob"
    assert e.value == "Bob" and e.text == "Bob"
    assert el(d, "checkbox").is_checked is False


def test_center_without_bounds_raises(env):
    d, be, win, w = env
    e = el(d, "#ok")
    win.minimized = True
    assert e.bounds is None
    with pytest.raises(ElementNotFound):
        _ = e.center


def test_tree_navigation(env):
    d, be, win, w = env
    grp = el(d, "#opts")
    kids = grp.children()
    assert [k.name for k in kids] == ["A", "B"]
    assert kids[0].parent == grp
    assert grp.parent.role == "window"
    assert grp.find("button").name == "A"
    assert [x.name for x in grp.find_all("button")] == ["A", "B"]
    assert grp.find_all("group") == []  # the root itself is excluded
    assert grp.exists('button "B"') and not grp.exists('button "OK"')
    with pytest.raises(ElementNotFound):
        grp.find('button "Nope"', timeout=0)
    # dict selector stores a canonical string selector
    b = grp.find({"role": "button", "name": "B"}, timeout=0)
    assert b.selector == 'button[name="B"]'


def test_find_waits_for_element(env):
    d, be, win, w = env
    root = el(d, "window")
    calls = {"n": 0}
    real_sleep = d.sleep

    def sleeper(s):
        calls["n"] += 1
        if calls["n"] == 2:
            win.add_button("Late", (0, 0, 10, 10))
        real_sleep(s)

    d.sleep = sleeper
    assert root.find('button "Late"', timeout=5).name == "Late"
    assert calls["n"] >= 2


def test_find_default_timeout_used(env):
    d, be, win, w = env
    d.default_timeout = 0.0
    with pytest.raises(ElementNotFound):
        el(d, "window").find("listitem")


# --------------------------------------------------------------------- click
def test_click_auto_uses_mouse_when_visible(env):
    d, be, win, w = env
    el(d, "#ok").click()
    assert w["clicked"] == ["ok"]
    assert mouse_events(be) == [("down", "left", 160, 455), ("up", "left", 160, 455)]
    assert not any(e[0] == "a11y_invoke" for e in be.events)


def test_click_auto_invokes_when_hidden(env):
    d, be, win, w = env
    e = el(d, "#ok")
    win.minimized = True
    e.click()
    assert w["clicked"] == ["ok"]
    assert mouse_events(be) == []
    assert ("a11y_invoke", "OK") in be.events


def test_click_auto_invokes_when_covered(env):
    d, be, win, w = env
    e = el(d, "#ok")
    cover = be.add_window("Popup", (100, 400, 300, 200))  # covers the OK button
    assert be.window_at(160, 455) is cover
    e.click()
    assert w["clicked"] == ["ok"]
    assert mouse_events(be) == []


def test_click_auto_activates_window_when_covered_and_not_invokable(env):
    d, be, win, w = env
    e = el(d, "#name")
    be.add_window("Popup", (100, 100, 300, 200))
    e.click()
    assert ("activate", win.handle) in be.events
    assert be.focused_widget is w["name"]
    assert len(mouse_events(be)) == 2


def test_click_native_true_and_false(env):
    d, be, win, w = env
    e = el(d, "#ok")
    e.click(native=True)
    assert w["clicked"] == ["ok"] and mouse_events(be) == []
    with pytest.raises(NotSupported):
        el(d, "#name").click(native=True)
    with pytest.raises(ValueError):
        e.click(clicks=2, native=True)
    win.minimized = True
    with pytest.raises(ElementNotFound):
        e.click(native=False)
    with pytest.raises(ValueError):
        e.click(native="sometimes")


def test_click_variants(env):
    d, be, win, w = env
    e = el(d, "#ok")
    e.click(offset=(5, -3))
    assert mouse_events(be)[0] == ("down", "left", 165, 452)
    be.clear_events()
    e.double_click()
    assert len(mouse_events(be)) == 4
    be.clear_events()
    e.right_click()
    assert mouse_events(be) == [("down", "right", 160, 455), ("up", "right", 160, 455)]
    d.mouse.move(10, 10)
    be.clear_events()
    e.hover()
    assert be.events[-1] == ("move", 160, 455)
    assert mouse_events(be) == []
    assert e.click(native="mouse") is e


def test_invoke_and_focus(env):
    d, be, win, w = env
    assert el(d, "#ok").invoke() is not None
    assert w["clicked"] == ["ok"]
    with pytest.raises(NotSupported):
        el(d, "#name").invoke()
    e = el(d, "#name").focus()
    assert e.is_focused


def test_focus_fallback_clicks_editable(env):
    d, be, win, w = env

    class NoFocus(type(el(d, "#name").node)):
        def focus(self):
            return False

    node = el(d, "#name").node
    nf = NoFocus(be, node.widget, node.parent)
    Element(nf, d).focus()
    assert be.focused_widget is w["name"] and len(mouse_events(be)) == 2
    lbl_node = el(d, "text").node
    with pytest.raises(NotSupported):
        Element(NoFocus(be, lbl_node.widget, lbl_node.parent), d).focus()


# ------------------------------------------------------------------ text entry
def test_set_text_native(env):
    d, be, win, w = env
    e = el(d, "#name").set_text("Alice")
    assert w["name"].value == "Alice" and e.value == "Alice"
    assert ("a11y_set_value", "Name", "Alice") in be.events
    assert not any(ev[0] in ("type", "key_down") for ev in be.events)
    e.set_text(" Smith", clear=False)
    assert w["name"].value == "Alice Smith"
    e.clear()
    assert w["name"].value == ""


def test_set_text_typing_fallback(env):
    d, be, win, w = env
    w["name"].value = "old"
    e = el(d, "#name").set_text("new", native=False)
    assert w["name"].value == "new"
    kinds = [ev[0] for ev in be.events]
    assert "type" in kinds and "a11y_set_value" not in kinds
    assert ("key_down", "ctrl") in be.events and ("key_down", "a") in be.events
    assert ("key_down", "backspace") in be.events
    e.set_text("er", native=False, clear=False)
    assert w["name"].value == "newer"


def test_set_text_auto_falls_back_when_native_ignored(env):
    d, be, win, w = env
    node = el(d, "#name").node

    class Liar(type(node)):
        def set_value(self, text):  # claims success but the app ignores it
            return True

    e = Element(Liar(be, node.widget, node.parent), d)
    e.set_text("typed")
    assert w["name"].value == "typed"
    with pytest.raises(NotSupported):
        el(d, "#ok").set_text("x", native=True)
    with pytest.raises(TypeError):
        e.set_text(123)  # type: ignore[arg-type]


def test_set_text_uses_desktop_typing_mode(env):
    d, be, win, w = env
    d.typing_mode = "keys"
    el(d, "#name").set_text("Hi!", native=False)
    assert w["name"].value == "Hi!"
    assert not any(ev[0] == "type" for ev in be.events)


def test_type_and_press(env):
    d, be, win, w = env
    e = el(d, "#name")
    e.type("abc")
    assert w["name"].value == "abc" and e.is_focused
    e.type("D", mode="keys")
    assert w["name"].value == "abcD"
    e.press("backspace", "backspace")
    assert w["name"].value == "ab"


# ----------------------------------------------------------- toggles & misc
def test_toggle_check_uncheck(env):
    d, be, win, w = env
    cb = el(d, "checkbox")
    cb.check()
    assert w["agree"].checked
    cb.check()
    assert w["agree"].checked
    cb.uncheck()
    assert not w["agree"].checked
    cb.toggle()
    assert w["agree"].checked
    ok = el(d, "#ok")
    ok.toggle()  # not natively toggleable -> click
    assert w["clicked"] == ["ok"]


def test_select_expand_collapse(env):
    d, be, win, w = env
    lst = win.add_list("L", (300, 300, 100, 72), items=["x", "y"])
    item = d.find('listitem "y"', timeout=0)
    item.select()
    assert lst.children[1].attrs.get("selected")
    assert item.is_selected
    with pytest.raises(NotSupported):
        el(d, "#ok").expand()
    with pytest.raises(NotSupported):
        el(d, "#ok").collapse()
    tree_item = win.add("treeitem", "Node", (300, 250, 100, 20))
    t = d.find("treeitem", timeout=0)
    t.expand()
    assert ("key_down", "right") in be.events
    t.collapse()
    assert ("key_down", "left") in be.events
    tree_item.attrs["selected"] = False


def test_scroll_and_scroll_into_view(env):
    d, be, win, w = env
    e = el(d, "#opts")
    assert e.scroll_into_view() is e
    e.scroll(-3)
    assert be.events[-1] == ("scroll", 0, -3, 450, 240)


def test_screenshot(env):
    d, be, win, w = env
    img = el(d, "#ok").screenshot()
    assert img.size == (80, 30)
    assert img.getpixel((40, 2)) == (210, 220, 240)
    win.minimized = True
    with pytest.raises(ElementNotFound):
        el(d, "window").screenshot()


def test_screenshot_clipped_to_screen():
    be = MockBackend(width=300, height=300)
    d = StubDesktop(be)
    win = be.add_window("Edge", (250, 250, 200, 200))
    win.add_button("Half", (0, 0, 100, 100))
    img = d.find("button", timeout=0).screenshot()
    assert img.size == (50, 50)


def test_highlight_is_safe(env):
    d, be, win, w = env
    e = el(d, "#ok")
    assert e.highlight(0.01) is e
    seen = []
    be.highlight = lambda rect, dur: seen.append((rect, dur))
    e.highlight(0.5)
    assert seen == [(Rect(120, 440, 80, 30), 0.5)]


# ---------------------------------------------------------------- waits / refresh
def test_wait_until_states(env):
    d, be, win, w = env
    e = el(d, "#ok")
    assert e.wait_until("visible", timeout=0) is e
    assert e.wait_until("enabled", timeout=0) is e
    assert e.wait_until(lambda x: x.name == "OK", timeout=0) is e
    with pytest.raises(ActionTimeout):
        e.wait_until("disabled", timeout=0)
    with pytest.raises(ValueError):
        e.wait_until("purple", timeout=0)
    w["ok"].enabled = False
    e.wait_until("disabled", timeout=0)
    el(d, "#name").focus().wait_until("focused", timeout=0)


def test_wait_until_becomes_enabled_over_time(env):
    d, be, win, w = env
    w["ok"].enabled = False
    e = el(d, "#ok")
    calls = {"n": 0}

    def sleeper(s):
        calls["n"] += 1
        if calls["n"] == 3:
            w["ok"].enabled = True

    d.sleep = sleeper
    e.wait_until("enabled", timeout=5)
    assert calls["n"] == 3


def test_wait_gone_with_selector(env):
    d, be, win, w = env
    e = el(d, 'button "OK"')
    with pytest.raises(ActionTimeout):
        e.wait_until("gone", timeout=0)
    w["ok"].visible = False
    assert e.wait_until("gone", timeout=0) is e
    assert not e.wait_until("hidden", timeout=0).is_visible


def test_wait_gone_without_selector(env):
    d, be, win, w = env
    e = Element(el(d, "#ok").node, d)
    win.minimized = True
    e.wait_until("gone", timeout=0)
    win.minimized = False
    e.wait_until("exists", timeout=0)


def test_refresh(env):
    d, be, win, w = env
    e = el(d, "#ok")
    old = e.node
    assert e.refresh() is e
    assert e.node is not old and e.node.same_as(old)
    # scoped refresh goes through the parent element
    a = el(d, "#opts").find('button "A"', timeout=0)
    w["grp"].children[0].name = "A"
    assert a.refresh().name == "A"
    w["ok"].automation_id = "changed"
    with pytest.raises(ElementNotFound):
        e.refresh()
    loose = Element(old, d)
    assert loose.refresh() is loose


def test_window_property(env):
    d, be, win, w = env
    wnd = el(d, "#ok").window
    assert wnd is not None and wnd.title == "Form"


def test_to_dict_repr_eq(env):
    d, be, win, w = env
    e = el(d, "#ok")
    dd = e.to_dict()
    assert dd["role"] == "button" and dd["name"] == "OK" and dd["automation_id"] == "ok"
    assert dd["bounds"] == {"left": 120, "top": 440, "width": 80, "height": 30}
    assert dd["selector"] == "#ok" and "enabled" in dd["states"]
    assert repr(e) == '<Element button "OK" #ok (120,440 80x30)>'
    assert e == el(d, 'button "OK"') and hash(e) == hash(el(d, 'button "OK"'))
    assert e != el(d, "#name")
    assert len({e, el(d, "#ok")}) == 1
    with pytest.raises(ValueError):
        Element(None, d)  # type: ignore[arg-type]


def test_center_is_usable_by_coerce_point(env):
    from pypilot.types import coerce_point

    d, be, win, w = env
    assert coerce_point(el(d, "#ok")) == Point(160, 455)
