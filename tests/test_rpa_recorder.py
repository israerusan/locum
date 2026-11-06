from __future__ import annotations

import json

import pytest

from locum.backends import set_backend
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import NotSupported
from locum.rpa import Recorder, RecordedAction, run_flow


def build_scene():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Signup", (100, 100, 600, 450), app="signup")
    w.add_edit("Name", (20, 40, 220, 28), automation_id="nameField")
    w.add_edit("Email", (20, 80, 220, 28))
    w.add_edit("Notes", (20, 300, 300, 60), attrs={"multiline": True})
    w.add_checkbox("Subscribe", (20, 120, 150, 24))
    w.add_list("Plan", (300, 40, 200, 100), items=["Free", "Pro", "Team"])
    status = w.add_text("pending", (20, 250, 300, 24))

    def submit(_):
        status.value = "submitted"

    w.add_button("Submit", (20, 200, 90, 30), on_click=submit)
    return be, w


def snapshot(window):
    return [(wd.role, wd.name, wd.value, wd.checked, bool(wd.attrs.get("selected")), wd.clicks) for wd in window.widgets]


def manual_recorder(be, **kw):
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    return Recorder(d, **kw).start(source="manual")


# ------------------------------------------------------------------ feed core

def test_typing_coalesces_with_shift_and_backspace():
    be, w = build_scene()
    # Nothing has the focus in this scene: without trust_unknown_focus the text would be
    # recorded as a secret (fail closed, see test_secrets_review_fixes.py).
    rec = manual_recorder(be, trust_unknown_focus=True)
    w.find("Name").window  # noqa: B018 - scene sanity
    for ev in [("key_down", "h"), ("key_up", "h"), ("key_down", "shift"), ("key_down", "i"), ("key_down", "1"),
               ("key_up", "shift"), ("key_down", "x"), ("key_down", "backspace"), ("key_down", "space"),
               ("type", "there")]:
        rec.feed(ev)
    acts = rec.stop()
    assert len(acts) == 1
    assert acts[0].kind == "type" and acts[0].text == "hI! there"


def test_hotkeys_presses_and_lone_modifiers():
    be, _ = build_scene()
    rec = manual_recorder(be, trust_unknown_focus=True)  # no focused field in this scene
    seq = [
        ("key_down", "a"),
        ("key_down", "ctrl"), ("key_down", "shift"), ("key_down", "S"), ("key_up", "S"), ("key_up", "shift"), ("key_up", "ctrl"),
        ("key_down", "tab"), ("key_up", "tab"), ("key_down", "tab"), ("key_down", "tab"),
        ("key_down", "shift"), ("key_down", "tab"), ("key_up", "shift"),
        ("key_down", "enter"),
        ("key_down", "alt"), ("key_up", "alt"),
        ("key_down", "Control_R"), ("key_down", "c"), ("key_up", "Control_R"),
        ("key_down", "backspace"),
    ]
    for ev in seq:
        rec.feed(ev)
    acts = [(a.kind, a.keys, a.presses, a.text) for a in rec.stop()]
    assert acts == [
        ("type", [], 1, "a"),
        ("hotkey", ["ctrl", "shift", "s"], 1, ""),
        ("press", ["tab"], 3, ""),
        ("hotkey", ["shift", "tab"], 1, ""),
        ("press", ["enter"], 1, ""),
        ("press", ["alt"], 1, ""),
        ("hotkey", ["ctrl", "c"], 1, ""),
        ("press", ["backspace"], 1, ""),
    ]


def test_clicks_selectors_multi_click_and_coordinates_fallback():
    be, w = build_scene()
    rec = manual_recorder(be)
    submit = w.find("Submit").screen_rect.center
    rec.feed({"event": "down", "button": "left", "x": submit.x, "y": submit.y, "time": 0.0})
    rec.feed({"event": "up", "button": "left", "x": submit.x, "y": submit.y, "time": 0.05})
    # double click (two quick clicks at the same spot)
    rec.feed({"event": "down", "button": "left", "x": submit.x, "y": submit.y, "time": 1.0})
    rec.feed({"event": "up", "button": "left", "x": submit.x, "y": submit.y, "time": 1.05})
    rec.feed({"event": "down", "button": "left", "x": submit.x + 1, "y": submit.y, "time": 1.2})
    rec.feed({"event": "up", "button": "left", "x": submit.x + 1, "y": submit.y, "time": 1.25})
    # right click on the desktop background: no element -> coordinates
    rec.feed(("down", "right", 1000, 700))
    rec.feed(("up", "right", 1000, 700))
    # click on the window body (a container): selector + offset
    rec.feed(("down", "left", 650, 500))
    rec.feed(("up", "left", 650, 500))
    acts = rec.stop()
    assert [a.kind for a in acts] == ["click", "double_click", "right_click", "click"]
    assert acts[0].selector == 'window "Signup" button "Submit"'
    assert acts[0].element == {"role": "button", "name": "Submit"}
    assert acts[0].clicks == 1 and acts[1].clicks == 2
    assert acts[2].selector is None and (acts[2].x, acts[2].y) == (1000, 700)
    assert acts[3].selector == 'window "Signup"'
    center = w.root.screen_rect.center
    assert acts[3].offset == (650 - center.x, 500 - center.y)
    flow = [a.to_flow_step() for a in acts]
    assert flow[0] == {"click": 'window "Signup" button "Submit"'}
    assert flow[1] == {"double_click": 'window "Signup" button "Submit"'}
    assert flow[2] == {"right_click": "1000,700"}
    assert flow[3] == {"click": {"target": 'window "Signup"', "offset": [650 - center.x, 500 - center.y]}}


def test_automation_id_selector_and_triple_click():
    be, w = build_scene()
    rec = manual_recorder(be)
    c = w.find("Name").screen_rect.center
    for i in range(3):
        rec.feed({"event": "down", "button": "left", "x": c.x, "y": c.y, "time": i * 0.1})
        rec.feed({"event": "up", "button": "left", "x": c.x, "y": c.y, "time": i * 0.1 + 0.02})
    a = rec.stop()[0]
    assert a.kind == "triple_click" and a.clicks == 3
    assert "#nameField" in a.selector
    assert a.to_flow_step() == {"click": {"target": a.selector, "button": "left", "clicks": 3}}
    assert a.to_python() .startswith(f"d.triple_click({a.selector!r})")


def test_drag_scroll_and_ignored_events():
    be, w = build_scene()
    rec = manual_recorder(be)
    rec.feed(("screenshot", None))  # unknown kinds are ignored
    rec.feed(("move", 10, 10))
    rec.feed(("down", "left", 10, 10))
    rec.feed(("move", 100, 100))
    rec.feed(("up", "left", 200, 150))
    r = w.find("Plan").screen_rect
    lst = type(r.center)(r.center.x, r.bottom - 5)  # inside the list, below its items
    rec.feed(("scroll", 0, -1, lst.x, lst.y))
    rec.feed(("scroll", 0, -2, lst.x + 1, lst.y))
    rec.feed(("scroll", 0, 3, lst.x + 1, lst.y))  # opposite direction -> new action
    rec.feed(("up", "left", 5, 5))  # release without press: ignored
    item = w.find("Free").screen_rect.center
    rec.feed(("scroll", 0, 1, item.x, item.y))  # over a list item -> selector
    acts = rec.stop()
    assert [a.kind for a in acts] == ["drag", "scroll", "scroll", "scroll"]
    assert acts[3].selector == 'window "Signup" listitem "Free"'
    assert acts[3].to_python() == "d.scroll(1, at='window \"Signup\" listitem \"Free\"')"
    drag = acts[0]
    assert (drag.x, drag.y, drag.end_x, drag.end_y) == (10, 10, 200, 150)
    assert drag.to_flow_step() == {"drag": {"start": [10, 10], "end": [200, 150]}}
    assert acts[1].dy == -3
    assert acts[1].selector is None  # the list is a container: keep the exact point
    assert acts[1].to_flow_step() == {"scroll": {"dy": -3, "at": [lst.x, lst.y]}}
    assert acts[2].dy == 3


def test_use_selectors_false_records_coordinates():
    be, w = build_scene()
    rec = manual_recorder(be, use_selectors=False)
    c = w.find("Submit").screen_rect.center
    rec.feed(("down", "left", c.x, c.y))
    rec.feed(("up", "left", c.x, c.y))
    a = rec.stop()[0]
    assert a.selector is None
    assert a.to_flow_step() == {"click": f"{c.x},{c.y}"}
    assert a.to_python() == f"d.click(({c.x}, {c.y}))"


def test_feed_validation():
    be, _ = build_scene()
    rec = manual_recorder(be)
    with pytest.raises(TypeError):
        rec.feed(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rec.feed(("move", 1))
    rec.feed(("down", "weird-button", 1, 1))  # unknown buttons are ignored
    rec.feed(("key_down", ""))
    assert rec.stop() == []
    with pytest.raises(ValueError):
        RecordedAction("teleport")


def test_start_twice_and_pynput_unavailable(monkeypatch):
    be, _ = build_scene()
    rec = manual_recorder(be)
    with pytest.raises(RuntimeError):
        rec.start(source="manual")
    rec.stop()
    with pytest.raises(ValueError):
        rec.start(source="telepathy")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("pynput"):
            raise ImportError("No module named 'pynput'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(NotSupported, match="locum\\[record\\]"):
        rec.start()
    assert not rec.is_recording


# ------------------------------------------------------------------ end to end

def perform_session(d, w):
    d.click('edit "Name"')
    d.type("Alice")
    d.press("tab")
    d.type("alice@example.com")
    d.hotkey("ctrl+a")
    d.type("alice@corp.example")
    d.click('checkbox "Subscribe"')
    d.click('listitem "Pro"')
    d.click('edit "Notes"')
    d.type("line one")
    d.press("backspace", presses=3)
    d.type("1")
    d.click('button "Submit"')


def test_backend_recording_replays_via_flow_on_fresh_scene():
    be, w = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")
    assert "mouse_down" in vars(be)  # hooked on the instance
    perform_session(d, w)
    actions = rec.stop()
    assert "mouse_down" not in vars(be)  # hooks removed
    kinds = [a.kind for a in actions]
    assert kinds.count("type") == 4
    assert all(a.selector for a in actions if a.kind == "click")
    expected = snapshot(w)
    assert w.find("Email").value == "alice@corp.example"

    flow = rec.to_flow(name="signup")
    json.dumps(flow)  # plain data
    be2, w2 = build_scene()
    d2 = Desktop(be2, sleep=lambda s: None, default_timeout=0)
    report = run_flow(flow, d2, policy=None)  # replays a recorded Submit click
    assert report.ok, [s.error for s in report.failures]
    assert snapshot(w2) == expected
    assert w2.find("pending").value == "submitted"


def test_recorded_flow_survives_layout_change():
    """Selectors (not coordinates) make the replay robust to moved widgets/windows."""
    be, w = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")
    perform_session(d, w)
    rec.stop()
    expected = snapshot(w)
    be2, w2 = build_scene()
    be2.move_window(w2.handle, w2.rect.offset(150, 120))
    w2.find("Submit").rect = w2.find("Submit").rect.offset(200, 0)
    report = run_flow(rec.to_flow(), Desktop(be2, sleep=lambda s: None, default_timeout=0), policy=None)
    assert report.ok, [s.error for s in report.failures]
    assert snapshot(w2) == expected


def test_python_script_replays_on_fresh_scene():
    be, w = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")
    perform_session(d, w)
    rec.stop()
    expected = snapshot(w)
    code = rec.to_python()
    assert "from locum import Desktop" in code and "d = Desktop()" in code
    compile(code, "<recorded>", "exec")
    be2, w2 = build_scene()
    try:
        set_backend(be2)
        exec(code, {"__name__": "__recorded__"})
    finally:
        set_backend(None)
    assert snapshot(w2) == expected


def test_feeding_event_log_after_the_fact():
    """A MockBackend event log can be fed wholesale (the recorder ignores non-input events)."""
    be, w = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    d.click('button "Submit"')
    d.type("hello")
    d.hotkey("ctrl+s")
    rec = Recorder(d, use_selectors=False).start(source="manual")
    for ev in be.events:
        rec.feed(ev)
    acts = rec.stop()
    assert [a.kind for a in acts] == ["click", "type", "hotkey"]
    assert acts[1].text == "hello" and acts[2].keys == ["ctrl", "s"]


def test_save_formats(tmp_path):
    be, w = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")
    d.click('button "Submit"')
    d.type("x")
    rec.stop()
    py = rec.save(tmp_path / "out.py")
    assert "d.click('window \"Signup\" button \"Submit\"')" in open(py).read()
    js = rec.save(tmp_path / "out.json")
    assert json.load(open(js))["name"] == "out"
    yaml = pytest.importorskip("yaml")
    y = rec.save(tmp_path / "flow.yaml")
    loaded = yaml.safe_load(open(y))
    assert loaded["steps"][0] == {"click": 'window "Signup" button "Submit"'}
    be2, w2 = build_scene()
    report = run_flow(y, Desktop(be2, sleep=lambda s: None, default_timeout=0), policy=None)
    assert report.ok and w2.find("pending").value == "submitted"
    with pytest.raises(ValueError):
        rec.save(tmp_path / "out.txt")


def test_empty_recording_outputs():
    be, _ = build_scene()
    rec = manual_recorder(be)
    rec.stop()
    assert rec.to_flow()["steps"] == []
    assert "# (no actions were recorded)" in rec.to_python()
    with pytest.raises(ValueError):
        rec.to_python(var="not valid")


def test_context_manager_and_pending_text_in_outputs():
    be, _ = build_scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d, trust_unknown_focus=True)  # no focused field in this scene
    rec.start(source="manual")
    with rec:
        rec.feed(("type", "abc"))
        assert rec.actions == []  # still buffered
        assert rec.to_flow()["steps"] == [{"type": "abc"}]
    assert not rec.is_recording
    assert rec.actions[0].text == "abc"
    rec.clear()
    assert rec.actions == []
