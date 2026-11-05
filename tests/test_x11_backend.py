"""Tests for the X11 backend.

Pure-Python parts (keysym tables, AT-SPI role mapping and node wrapper) run
everywhere; tests marked ``x11`` need a real X server and use the session-wide
``xvfb_display`` fixture (skipped when neither Xvfb nor $DISPLAY is available).
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
import types

import numpy as np
import pytest

pytest.importorskip("Xlib")

from Xlib import X, Xatom  # noqa: E402
from Xlib import display as xdisplay  # noqa: E402
from Xlib import error as xerror  # noqa: E402

from pypilot.backends import create_backend  # noqa: E402
from pypilot.backends.base import (  # noqa: E402
    CAP_ACCESSIBILITY,
    CAP_CLIPBOARD,
    CAP_HSCROLL,
    CAP_KEYBOARD,
    CAP_MOUSE,
    CAP_SCREENSHOT,
    CAP_UNICODE_TYPING,
    CAP_WINDOWS,
    ROLES,
)
from pypilot.backends.x11 import (  # noqa: E402
    BUTTONS,
    KEYSYMS,
    AtspiNode,
    X11Backend,
    char_to_keysym,
    iter_keysym_table,
    key_to_keysym,
    map_atspi_role,
)
from pypilot.errors import BackendError, BackendUnavailable, NotSupported, WindowNotFound  # noqa: E402
from pypilot.keys import KEYS  # noqa: E402
from pypilot.types import MouseButton, Rect  # noqa: E402


# =============================================================================== pure tests
def test_every_canonical_key_has_a_keysym():
    assert set(KEYS) <= set(KEYSYMS)
    assert dict(iter_keysym_table()).keys() == set(KEYS)
    assert all(isinstance(v, int) and v > 0 for v in KEYSYMS.values())


def test_keysyms_match_xlib_names():
    from Xlib import XK

    for name, xname in [
        ("enter", "Return"), ("backspace", "BackSpace"), ("pageup", "Prior"), ("pagedown", "Next"),
        ("ctrl", "Control_L"), ("shift_r", "Shift_R"), ("cmd", "Super_L"), ("f1", "F1"), ("f24", "F24"),
        ("num0", "KP_0"), ("num9", "KP_9"), ("num_enter", "KP_Enter"), ("menu", "Menu"), ("printscreen", "Print"),
        ("delete", "Delete"), ("escape", "Escape"), ("space", "space"), ("capslock", "Caps_Lock"),
    ]:
        assert KEYSYMS[name] == XK.string_to_keysym(xname), name


def test_char_to_keysym():
    assert char_to_keysym("a") == ord("a")
    assert char_to_keysym("~") == ord("~")
    assert char_to_keysym("é") == 0xE9
    assert char_to_keysym("€") == 0x010020AC
    assert char_to_keysym("😀") == 0x0101F600
    with pytest.raises(ValueError):
        char_to_keysym("ab")
    assert key_to_keysym("enter") == 0xFF0D
    assert key_to_keysym("\n") == 0xFF0D
    assert key_to_keysym("Z") == ord("Z")
    with pytest.raises(ValueError):
        key_to_keysym("notakey")


def test_buttons_mapping():
    assert BUTTONS[MouseButton.LEFT] == 1
    assert BUTTONS[MouseButton.MIDDLE] == 2
    assert BUTTONS[MouseButton.RIGHT] == 3
    assert BUTTONS[MouseButton.X1] == 8
    assert BUTTONS[MouseButton.X2] == 9


def test_role_mapping():
    assert map_atspi_role("push button") == "button"
    assert map_atspi_role("frame") == "window"
    assert map_atspi_role("check box") == "checkbox"
    assert map_atspi_role("text", editable=True) == "edit"
    assert map_atspi_role("text") == "text"
    assert map_atspi_role("entry") == "edit"
    assert map_atspi_role("page tab list") == "tablist"
    assert map_atspi_role("Some Exotic Role") == "someexoticrole"
    assert map_atspi_role("") == "unknown"
    for native in ("push button", "frame", "dialog", "menu item", "list item", "table cell", "tree item", "label"):
        assert map_atspi_role(native) in ROLES


def test_backend_unavailable_without_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(BackendUnavailable):
        X11Backend()


def test_backend_unavailable_bad_display():
    with pytest.raises(BackendUnavailable):
        X11Backend(":4711")


# ---------------------------------------------------------------- fake AT-SPI
class _Nick:
    def __init__(self, nick):
        self.value_nick = nick


class _StateSet:
    def __init__(self, states):
        self._s = states

    def get_states(self):
        return [_Nick(s) for s in self._s]


class _Ext:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class FakeAcc:
    """Duck-typed stand-in for ``Atspi.Accessible``."""

    def __init__(self, role, name="", rect=(0, 0, 0, 0), states=("enabled", "showing", "visible"), children=(),
                 pid=0, actions=(), text=None, editable=False, value=None, attrs=None, selection=False):
        self.role, self.nm, self.rect, self.st = role, name, rect, list(states)
        self.kids = list(children)
        self.par = None
        for k in self.kids:
            k.par = self
        self.pid = pid
        self.acts = list(actions)
        self.done = []
        self.text = text
        self.editable = editable
        self.val = value
        self.attrs = attrs or {}
        self.selection = selection
        self.selected = []
        self.focused = False

    # Accessible
    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.nm

    def get_description(self):
        return "desc of " + self.nm

    def get_extents(self, coord):
        return _Ext(*self.rect)

    def get_state_set(self):
        return _StateSet(self.st)

    def get_process_id(self):
        return self.pid

    def get_attributes(self):
        return dict(self.attrs)

    def get_toolkit_name(self):
        return "faketk"

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, i):
        return self.kids[i]

    def get_parent(self):
        return self.par

    def get_index_in_parent(self):
        return self.par.kids.index(self) if self.par else -1

    # interfaces
    def get_action_iface(self):
        return self if self.acts else None

    def get_n_actions(self):
        return len(self.acts)

    def get_action_name(self, i):
        return self.acts[i]

    def do_action(self, i):
        self.done.append(self.acts[i])
        return True

    def get_text_iface(self):
        return self if self.text is not None else None

    def get_text(self, start, end):
        return self.text

    def get_editable_text_iface(self):
        return self if self.editable else None

    def set_text_contents(self, t):
        self.text = t
        return True

    def get_value_iface(self):
        return _Value(self) if self.val is not None else None

    def get_component_iface(self):
        return self

    def grab_focus(self):
        self.focused = True
        return True

    def get_accessible_at_point(self, x, y, coord):
        for k in self.kids:
            l, t, w, h = k.rect
            if l <= x < l + w and t <= y < t + h:
                return k
        return None

    def get_selection_iface(self):
        return self if self.selection else None

    def select_child(self, idx):
        self.selected.append(idx)
        return True


class _Value:
    def __init__(self, acc):
        self.acc = acc

    def get_current_value(self):
        return self.acc.val

    def set_current_value(self, v):
        self.acc.val = v
        return True


def make_fake_atspi(desktop):
    mod = types.SimpleNamespace()
    mod.CoordType = types.SimpleNamespace(SCREEN=0, WINDOW=1)
    mod.ScrollType = types.SimpleNamespace(ANYWHERE=6)
    mod.get_desktop = lambda i: desktop
    return mod


def build_fake_tree(pid=1234, title="Fake App Window", rect=(100, 100, 400, 300)):
    l, t = rect[0], rect[1]
    ok = FakeAcc("push button", "OK", (l + 10, t + 10, 80, 30), actions=["click"])
    entry = FakeAcc("text", "Name", (l + 10, t + 50, 200, 28), text="hello",
                    states=("enabled", "showing", "visible", "editable", "focused", "focusable"), editable=True,
                    attrs={"id": "name-field"})
    chk = FakeAcc("check box", "Remember", (l + 10, t + 90, 120, 20), actions=["toggle"],
                  states=("sensitive", "showing", "visible", "checked"))
    slider = FakeAcc("slider", "Volume", (l + 10, t + 120, 150, 20), value=0.5)
    item1 = FakeAcc("list item", "One", (l + 10, t + 150, 100, 20))
    item2 = FakeAcc("list item", "Two", (l + 10, t + 170, 100, 20))
    lst = FakeAcc("list", "Items", (l + 10, t + 150, 100, 40), children=[item1, item2], selection=True)
    hidden = FakeAcc("label", "Hidden", (0, 0, 0, 0), states=("enabled",))
    panel = FakeAcc("panel", "", rect, children=[ok, entry, chk, slider, lst, hidden])
    win = FakeAcc("frame", title, rect, children=[panel], pid=pid, states=("enabled", "showing", "visible", "active"))
    app = FakeAcc("application", "fakeapp", (0, 0, 0, 0), children=[win], pid=pid)
    desktop = FakeAcc("desktop frame", "main", (0, 0, 1280, 800), children=[app])
    return desktop, dict(ok=ok, entry=entry, chk=chk, slider=slider, lst=lst, item2=item2, win=win, hidden=hidden)


def test_atspi_node_properties_and_actions():
    desktop, acc = build_fake_tree()
    atspi = make_fake_atspi(desktop)
    root = AtspiNode(acc["win"], atspi)
    assert root.role == "window" and root.name == "Fake App Window"
    assert root.bounds == Rect(100, 100, 400, 300)
    assert root.pid == 1234
    assert root.handle is None  # no resolver
    nodes = {n.name: n for n in root.walk()}
    ok = nodes["OK"]
    assert ok.role == "button" and ok.native_role == "push button"
    assert ok.is_enabled and ok.is_visible
    assert "invoke" in ok.actions()
    assert ok.invoke() is True and acc["ok"].done == ["click"]
    entry = nodes["Name"]
    assert entry.role == "edit"
    assert entry.value == "hello"
    assert entry.automation_id == "name-field"
    assert {"focused", "editable", "enabled", "visible"} <= entry.states
    assert entry.set_value("world") is True and entry.value == "world"
    assert entry.focus() is True and acc["entry"].focused
    assert entry.attributes()["toolkit"] == "faketk"
    chk = nodes["Remember"]
    assert chk.role == "checkbox" and "checked" in chk.states and chk.is_enabled
    assert chk.toggle() is True and acc["chk"].done == ["toggle"]
    slider = nodes["Volume"]
    assert slider.value == "0.5"
    assert slider.set_value("0.75") is True and acc["slider"].val == 0.75
    item2 = nodes["Two"]
    assert item2.role == "listitem"
    assert "select" in item2.actions()
    assert item2.select() is True and acc["lst"].selected == [1]
    hidden = nodes["Hidden"]
    assert not hidden.is_visible and "offscreen" in hidden.states
    assert item2.parent is not None and item2.parent.name == "Items"
    assert ok.same_as(AtspiNode(acc["ok"], atspi)) and not ok.same_as(item2)
    assert root.child_at_point(115, 115).name == ""  # the panel
    assert root.child_at_point(5, 5) is None


def test_atspi_nodes_expose_app_for_selectors():
    """``[app=...]`` selectors must match AT-SPI nodes (regression: no 'app' attribute before)."""
    from pypilot.selectors import parse

    # pid of this very process -> /proc/<pid>/comm is the process name, like list_windows() reports.
    me = os.getpid()
    with open(f"/proc/{me}/comm") as fh:
        comm = fh.read().strip()
    desktop, acc = build_fake_tree(pid=me)
    for a in (acc["ok"], acc["entry"]):
        a.pid = me
    atspi = make_fake_atspi(desktop)
    app_node = AtspiNode(desktop, atspi).children()[0]
    root = AtspiNode(acc["win"], atspi, app_node)
    ok = next(n for n in root.walk() if n.name == "OK")
    assert root.attributes()["app"] == comm and ok.attributes()["app"] == comm
    assert [n.name for n in parse(f"button[app={comm}]").find_all([root])] == ["OK"]
    assert parse("button[app=definitely-not-it]").find_all([root]) == []
    # no pid -> the AT-SPI application node's name
    desktop2, acc2 = build_fake_tree(pid=0)
    atspi2 = make_fake_atspi(desktop2)
    root2 = AtspiNode(acc2["win"], atspi2)
    assert root2.attributes()["app"] == "fakeapp"
    assert [n.name for n in parse("window[app=fakeapp]").find_all([root2])] == ["Fake App Window"]


def test_atspi_node_swallow_errors():
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("stale")
            return boom

    n = AtspiNode(Broken(), make_fake_atspi(None))
    assert n.name == "" and n.role == "unknown" and n.bounds is None
    assert n.states == frozenset() and n.children() == [] and n.value is None
    assert n.pid is None and n.actions() == [] and n.invoke() is False
    assert n.set_value("x") is False and n.focus() is False


# ============================================================================= X11 tests
@pytest.fixture(scope="module")
def be(xvfb_display):
    try:
        backend = X11Backend(xvfb_display, clipboard_tool="none", accessibility=False)
    except BackendUnavailable as exc:  # pragma: no cover - environment specific
        pytest.skip(f"X11 backend unavailable: {exc}")
    yield backend
    backend.close()


@pytest.fixture
def xc(xvfb_display):
    d = xdisplay.Display(xvfb_display)
    yield d
    d.close()


def _pixel(d, rgb):
    v = d.screen().root_visual
    visual = None
    for depth in d.screen().allowed_depths:
        for vis in depth.visuals:
            if vis.visual_id == v:
                visual = vis
    if visual is None:
        return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]

    def put(c, mask):
        shift = (mask & -mask).bit_length() - 1
        bits = bin(mask).count("1")
        return ((c >> (8 - bits)) << shift) & mask

    return put(rgb[0], visual.red_mask) | put(rgb[1], visual.green_mask) | put(rgb[2], visual.blue_mask)


def make_window(d, rect, rgb=(200, 30, 90), title="pypilot-x11-test", event_mask=0, delete_protocol=True):
    scr = d.screen()
    x, y, w, h = rect
    win = scr.root.create_window(
        x, y, w, h, 0, scr.root_depth, X.InputOutput, X.CopyFromParent,
        background_pixel=_pixel(d, rgb), event_mask=event_mask | X.StructureNotifyMask,
    )
    win.set_wm_name(title)
    win.change_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"), 8, title.encode("utf-8"))
    win.set_wm_class("pypilottest", "PypilotTest")
    win.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
    if delete_protocol:
        win.set_wm_protocols([d.intern_atom("WM_DELETE_WINDOW")])
    win.map()
    d.sync()
    deadline = time.time() + 3
    while time.time() < deadline:
        if win.get_attributes().map_state == X.IsViewable:
            break
        time.sleep(0.01)
    win.clear_area(0, 0, 0, 0)
    d.sync()
    time.sleep(0.05)
    return win


def drain(d, until=None, timeout=2.0):
    """Collect events from ``d`` until ``until(events)`` holds or ``timeout`` elapses."""
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type == X.MappingNotify:
                d.refresh_keyboard_mapping(ev)
            events.append(ev)
        if until is not None and until(events):
            break
        time.sleep(0.01)
    return events


def keysym_to_text(ks):
    if ks == 0xFF0D:
        return "\n"
    if ks == 0xFF09:
        return "\t"
    if 0x20 <= ks <= 0xFF:
        return chr(ks)
    if ks & 0xFF000000 == 0x01000000:
        return chr(ks & 0xFFFFFF)
    return ""


def decode_key(d, ev):
    shift = bool(ev.state & X.ShiftMask)
    ks = d.keycode_to_keysym(ev.detail, 1 if shift else 0)
    if ks == 0 and shift:
        ks = ord(chr(d.keycode_to_keysym(ev.detail, 0)).upper())
    return keysym_to_text(ks)


@pytest.mark.x11
def test_capabilities(be):
    caps = be.capabilities
    assert {CAP_MOUSE, CAP_KEYBOARD, CAP_UNICODE_TYPING, CAP_SCREENSHOT, CAP_WINDOWS, CAP_HSCROLL} <= caps
    assert CAP_CLIPBOARD not in caps  # disabled for this fixture
    assert CAP_ACCESSIBILITY not in caps
    with pytest.raises(NotSupported):
        be.accessibility_roots()
    with pytest.raises(NotSupported):
        be.get_clipboard()
    with pytest.raises(NotSupported):
        be.set_clipboard("x")


@pytest.mark.x11
def test_create_backend_registry(xvfb_display):
    b = create_backend("x11", display=xvfb_display)
    try:
        assert isinstance(b, X11Backend) and b.name == "x11"
    finally:
        b.close()


@pytest.mark.x11
def test_mouse_move_roundtrip(be):
    for x, y in [(10, 20), (640, 400), (1, 1), (333, 222)]:
        be.mouse_move(x, y)
        assert tuple(be.mouse_position()) == (x, y)
    vs = be.virtual_screen()
    be.mouse_move(-50, vs.bottom + 500)  # clamped onto the screen
    assert tuple(be.mouse_position()) == (0, vs.bottom - 1)


@pytest.mark.x11
def test_buttons_and_scroll_reach_window(be, xc):
    win = make_window(xc, (200, 150, 300, 200), event_mask=X.ButtonPressMask | X.ButtonReleaseMask)
    try:
        drain(xc, timeout=0.1)
        be.mouse_move(250, 180)
        for b in (MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE, MouseButton.X1, MouseButton.X2):
            be.mouse_down(b)
            be.mouse_up(b)
        be.scroll(dy=2)
        be.scroll(dy=-1)
        be.scroll(dx=1)
        be.scroll(dx=-1)
        be.scroll()  # no-op
        evs = drain(xc, until=lambda e: sum(1 for x in e if x.type == X.ButtonPress) >= 10)
        presses = [e for e in evs if e.type == X.ButtonPress]
        assert [e.detail for e in presses] == [1, 3, 2, 8, 9, 4, 4, 5, 7, 6]
        assert (presses[0].event_x, presses[0].event_y) == (50, 30)
        releases = [e.detail for e in evs if e.type == X.ButtonRelease]
        assert releases[:5] == [1, 3, 2, 8, 9]
        assert not be._state.held_buttons
    finally:
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_key_events_reach_focused_window(be, xc):
    win = make_window(xc, (100, 100, 200, 120), event_mask=X.KeyPressMask | X.KeyReleaseMask)
    try:
        be.activate_window(win.id)
        assert xc.get_input_focus().focus.id == win.id
        drain(xc, timeout=0.1)
        # plain key, shifted char, hotkey
        be.key_down("a")
        be.key_up("a")
        be.key_down("A")
        be.key_up("A")
        be.key_down("ctrl")
        be.key_down("c")
        be.key_up("c")
        be.key_up("ctrl")
        be.key_down("enter")
        be.key_up("enter")
        evs = drain(xc, until=lambda e: sum(1 for x in e if x.type == X.KeyRelease) >= 6)
        presses = [e for e in evs if e.type == X.KeyPress]
        texts = [decode_key(xc, e) for e in presses]
        assert texts[0] == "a"
        assert "A" in texts
        ctrl_c = [e for e in presses if decode_key(xc, e) == "c"]
        assert ctrl_c and ctrl_c[0].state & X.ControlMask
        assert texts[-1] == "\n"
        assert not be._state.held_keycodes
        assert not be._held_keys
    finally:
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_type_text_unicode(be, xc):
    win = make_window(xc, (120, 120, 200, 120), event_mask=X.KeyPressMask)
    try:
        be.activate_window(win.id)
        drain(xc, timeout=0.1)
        text = "Hi, World! ~é€😀\tok\n"
        be.type_text(text)
        expected = len(text)
        evs = drain(xc, until=lambda e: sum(1 for x in e if x.type == X.KeyPress and x.detail not in _shift_codes(xc)) >= expected)
        typed = "".join(decode_key(xc, e) for e in evs if e.type == X.KeyPress and e.detail not in _shift_codes(xc))
        assert typed == text
        assert not be._state.held_keycodes
        be.type_text("")  # no-op
        with pytest.raises(TypeError):
            be.type_text(123)  # type: ignore[arg-type]
    finally:
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_type_text_lowercase_scratch_char_with_capslock_on(xvfb_display, xc):
    """CapsLock uppercases scratch-bound lowercase letters in clients; type_text must
    switch it off around them (and restore it), so 'é' does not come out as 'É'."""
    # own backend (closed at the end) so its scratch keycodes are given back
    be = X11Backend(xvfb_display, clipboard_tool="none", accessibility=False)
    win = make_window(xc, (130, 130, 200, 120), event_mask=X.KeyPressMask)
    try:
        be.activate_window(win.id)
        be.key_down("capslock")
        be.key_up("capslock")
        assert xc.screen().root.query_pointer().mask & X.LockMask
        drain(xc, timeout=0.1)
        be.type_text("\u00e9\u00e4B")
        caps_kc = xc.keysym_to_keycode(0xFFE5)
        skip = _shift_codes(xc) | {caps_kc}
        evs = drain(xc, until=lambda e: sum(1 for x in e if x.type == X.KeyPress and x.detail not in skip) >= 3)
        presses = [e for e in evs if e.type == X.KeyPress and e.detail not in skip]
        assert len(presses) == 3
        for ev, ch in zip(presses[:2], "\u00e9\u00e4"):
            assert not ev.state & X.LockMask  # CapsLock was off while the letter was typed
            assert decode_key(xc, ev) == ch
        # CapsLock is back on afterwards
        assert xc.screen().root.query_pointer().mask & X.LockMask
        assert decode_key(xc, presses[2]) in ("B", "b")  # real key; case decided by the client
    finally:
        be.key_down("capslock")
        be.key_up("capslock")
        be.close()
        win.destroy()
        xc.sync()
    assert not xc.screen().root.query_pointer().mask & X.LockMask


def _shift_codes(d):
    return {kc for kc in d.get_modifier_mapping()[X.ShiftMapIndex] if kc}


@pytest.mark.x11
def test_every_canonical_key_presses(be):
    for k in sorted(KEYS):
        be.key_down(k)
        be.key_up(k)
    # capslock/numlock toggled their lock state: toggle back so later tests start clean
    for k in ("capslock", "numlock"):
        be.key_down(k)
        be.key_up(k)
    assert not be._state.held_keycodes
    with pytest.raises(ValueError):
        be.key_down("definitely-not-a-key")


@pytest.mark.x11
def test_scratch_keycode_eviction_and_restore(xvfb_display, xc):
    b = X11Backend(xvfb_display, clipboard_tool="none", accessibility=False, max_scratch_keycodes=2, remap_settle=0.1)
    win = make_window(xc, (140, 140, 200, 120), event_mask=X.KeyPressMask)
    got = []
    stop = threading.Event()
    reader_conn = xdisplay.Display(xvfb_display)
    reader_win = reader_conn.create_resource_object("window", win.id)
    reader_win.change_attributes(event_mask=X.KeyPressMask)
    reader_conn.sync()

    def reader():
        while not stop.is_set():
            while reader_conn.pending_events():
                ev = reader_conn.next_event()
                if ev.type == X.MappingNotify:
                    reader_conn.refresh_keyboard_mapping(ev)
                elif ev.type == X.KeyPress:
                    ch = decode_key(reader_conn, ev)
                    if ch:  # skip modifier/lock keys (e.g. a CapsLock compensation tap)
                        got.append(ch)
            time.sleep(0.002)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        b.activate_window(win.id)
        text = "αβγδα"
        b.type_text(text)
        deadline = time.time() + 3
        while time.time() < deadline and len(got) < len(text):
            time.sleep(0.01)
        assert "".join(got) == text
        used = list(b._state.original)
        assert len(used) == 2
        b.close()
        assert b.closed
        km = xc.get_keyboard_mapping(min(used), max(used) - min(used) + 1)
        for kc in used:
            assert not any(km[kc - min(used)]), "scratch keycode was not restored"
        with pytest.raises(BackendError):
            b.mouse_position()
    finally:
        stop.set()
        t.join(timeout=2)
        reader_conn.close()
        b.close()
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_close_releases_held_keys(xvfb_display, xc):
    b = X11Backend(xvfb_display, clipboard_tool="none", accessibility=False)
    b.key_down("shift")
    b.mouse_down("left")
    shift_kc = xc.keysym_to_keycode(0xFFE1)
    keymap = xc.query_keymap()
    assert keymap[shift_kc // 8] & (1 << (shift_kc % 8))
    b.close()
    keymap = xc.query_keymap()
    assert not keymap[shift_kc // 8] & (1 << (shift_kc % 8))
    assert not xc.screen().root.query_pointer().mask & X.Button1Mask


@pytest.mark.x11
def test_screenshot_finds_window_color(be, xc):
    color = (200, 30, 90)
    rect = (300, 200, 120, 80)
    win = make_window(xc, rect, rgb=color)
    try:
        be.mouse_move(5, 5)  # keep the cursor away (it is not captured anyway)
        full = be.screenshot()
        vs = be.virtual_screen()
        assert full.mode == "RGB" and full.size == (vs.width, vs.height)
        assert full.getpixel((310, 210)) == color
        assert full.getpixel((419, 279)) == color
        region = be.screenshot(Rect(*rect))
        assert region.size == (120, 80)
        assert region.getpixel((0, 0)) == color and region.getpixel((119, 79)) == color
        assert (np.asarray(region) == np.array(color, dtype=np.uint8)).all()
        assert be.pixel(350, 240) == color
        # region partly off-screen is padded with black, keeping (0, 0) = region top-left
        part = be.screenshot(Rect(vs.right - 10, vs.bottom - 10, 30, 30))
        assert part.size == (30, 30)
        assert part.getpixel((25, 25)) == (0, 0, 0)
        off = be.screenshot(Rect(-100, -100, 10, 10))
        assert off.size == (10, 10)
        edge = be.screenshot(Rect(290, 190, 20, 20))
        assert edge.getpixel((15, 15)) == color
        with pytest.raises(ValueError):
            be.screenshot(Rect(0, 0, 0, 10))
    finally:
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_screenshot_xlib_fallback_matches_mss(xvfb_display, xc):
    color = (10, 220, 40)
    win = make_window(xc, (500, 300, 64, 48), rgb=color)
    xb = X11Backend(xvfb_display, screenshot_method="xlib", clipboard_tool="none", accessibility=False)
    mb = X11Backend(xvfb_display, screenshot_method="auto", clipboard_tool="none", accessibility=False)
    try:
        r = Rect(480, 290, 100, 70)
        a = xb.screenshot(r)
        b = mb.screenshot(r)
        assert a.mode == b.mode == "RGB"
        assert a.size == b.size == (100, 70)
        assert np.array_equal(np.asarray(a), np.asarray(b))
        assert a.getpixel((30, 20)) == color
    finally:
        xb.close()
        mb.close()
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_monitors(be, xc):
    mons = be.monitors()
    assert mons and mons[0].is_primary and mons[0].index == 0
    assert sum(1 for m in mons if m.is_primary) == 1
    g = xc.screen().root.get_geometry()
    vs = be.virtual_screen()
    assert (vs.width, vs.height) == (g.width, g.height)
    for m in mons:
        assert m.rect.width > 0 and m.rect.height > 0
        assert m.rect.intersect(vs) == m.rect
        assert m.scale > 0


@pytest.mark.x11
def test_list_and_manage_windows_without_wm(be, xc):
    win = make_window(xc, (60, 70, 210, 110), title="pypilot ü window")
    other = make_window(xc, (400, 300, 100, 100), title="pypilot other", delete_protocol=False)
    try:
        wins = be.list_windows()
        mine = [w for w in wins if w.handle == win.id]
        assert mine, wins
        info = mine[0]
        assert info.title == "pypilot ü window"
        assert info.rect == Rect(60, 70, 210, 110)
        assert info.pid == os.getpid()
        assert info.class_name == "PypilotTest"
        assert info.app  # process name from /proc or WM_CLASS
        assert info.is_visible and not info.is_minimized
        # z-order: the most recently mapped window is on top
        handles = [w.handle for w in wins]
        assert handles.index(other.id) < handles.index(win.id)

        be.activate_window(win.id)
        act = be.active_window()
        assert act is not None and act.handle == win.id and act.is_active
        assert [w for w in be.list_windows() if w.handle == win.id][0].is_active
        assert xc.get_input_focus().focus.id == win.id

        be.move_window(win.id, Rect(30, 40, 250, 150))
        g = win.get_geometry()
        assert (g.x, g.y, g.width, g.height) == (30, 40, 250, 150)
        assert [w for w in be.list_windows() if w.handle == win.id][0].rect == Rect(30, 40, 250, 150)

        be.set_window_state(win.id, "maximized")
        mon = be.monitors()[0].rect
        g = win.get_geometry()
        assert (g.x, g.y, g.width, g.height) == mon.as_tuple()
        assert [w for w in be.list_windows() if w.handle == win.id][0].is_maximized
        be.set_window_state(win.id, "normal")
        g = win.get_geometry()
        assert (g.x, g.y, g.width, g.height) == (30, 40, 250, 150)

        be.set_window_state(win.id, "minimized")
        info = [w for w in be.list_windows() if w.handle == win.id][0]
        assert info.is_minimized and not info.is_visible
        be.set_window_state(win.id, "normal")
        xc.sync()
        assert win.get_attributes().map_state == X.IsViewable
        with pytest.raises(ValueError):
            be.set_window_state(win.id, "sideways")

        # close: WM_DELETE_WINDOW is delivered to a window that supports it ...
        drain(xc, timeout=0.05)
        be.close_window(win.id)
        evs = drain(xc, until=lambda e: any(x.type == X.ClientMessage for x in e))
        cm = [e for e in evs if e.type == X.ClientMessage]
        assert cm and cm[0].data[1][0] == xc.intern_atom("WM_DELETE_WINDOW")
        # ... and a window without the protocol is destroyed
        be.close_window(other.id)
        with pytest.raises(xerror.XError):
            other.get_geometry()
        assert other.id not in [w.handle for w in be.list_windows()]
        with pytest.raises(WindowNotFound):
            be.activate_window(other.id)
        with pytest.raises(WindowNotFound):
            be.move_window("not-a-handle", Rect(0, 0, 10, 10))
    finally:
        for w in (win, other):
            try:
                w.destroy()
            except Exception:
                pass
        xc.sync()


@pytest.mark.x11
def test_ewmh_window_properties(be, xc):
    """Simulate the EWMH properties a window manager would publish."""
    win = make_window(xc, (90, 90, 160, 100), title="pypilot ewmh")
    root = xc.screen().root
    check = root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent)
    atom = xc.intern_atom
    try:
        check.change_property(atom("_NET_SUPPORTING_WM_CHECK"), Xatom.WINDOW, 32, [check.id])
        root.change_property(atom("_NET_SUPPORTING_WM_CHECK"), Xatom.WINDOW, 32, [check.id])
        root.change_property(atom("_NET_SUPPORTED"), Xatom.ATOM, 32,
                             [atom("_NET_CLIENT_LIST_STACKING"), atom("_NET_ACTIVE_WINDOW"), atom("_NET_WM_STATE")])
        root.change_property(atom("_NET_CLIENT_LIST_STACKING"), Xatom.WINDOW, 32, [win.id])
        root.change_property(atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [win.id])
        win.change_property(atom("_NET_WM_STATE"), Xatom.ATOM, 32,
                            [atom("_NET_WM_STATE_MAXIMIZED_VERT"), atom("_NET_WM_STATE_MAXIMIZED_HORZ")])
        win.change_property(atom("_NET_FRAME_EXTENTS"), Xatom.CARDINAL, 32, [2, 3, 20, 4])
        xc.sync()
        wins = be.list_windows()
        assert [w.handle for w in wins] == [win.id]
        w = wins[0]
        assert w.is_active and w.is_maximized and not w.is_minimized
        assert w.rect == Rect(88, 70, 165, 124)
        assert be.active_window().handle == win.id
        win.change_property(atom("_NET_WM_STATE"), Xatom.ATOM, 32, [atom("_NET_WM_STATE_HIDDEN")])
        xc.sync()
        w = be.list_windows()[0]
        assert w.is_minimized and not w.is_visible and not w.is_maximized
    finally:
        for name in ("_NET_SUPPORTING_WM_CHECK", "_NET_SUPPORTED", "_NET_CLIENT_LIST_STACKING", "_NET_ACTIVE_WINDOW"):
            root.delete_property(atom(name))
        check.destroy()
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_thread_safety(be):
    errors = []

    def worker(seed):
        try:
            for i in range(40):
                be.mouse_move(10 + (seed * 7 + i) % 300, 10 + i)
                be.mouse_position()
                be.screenshot(Rect(0, 0, 8, 8))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors


@pytest.mark.x11
def test_clipboard_via_helper(xvfb_display, tmp_path, monkeypatch):
    store = tmp_path / "clip.txt"
    script = tmp_path / "bin" / "xclip"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"store = {str(store)!r}\n"
        "assert os.environ.get('DISPLAY')\n"
        "if '-o' in sys.argv:\n"
        "    if not os.path.exists(store):\n"
        "        sys.stderr.write('Error: target STRING not available\\n'); sys.exit(1)\n"
        "    sys.stdout.buffer.write(open(store, 'rb').read())\n"
        "else:\n"
        "    open(store, 'wb').write(sys.stdin.buffer.read())\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(script.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    b = X11Backend(xvfb_display, accessibility=False)
    try:
        assert CAP_CLIPBOARD in b.capabilities
        assert b.get_clipboard() == ""  # empty clipboard -> ""
        b.set_clipboard("héllo\nwörld 😀")
        assert b.get_clipboard() == "héllo\nwörld 😀"
        with pytest.raises(TypeError):
            b.set_clipboard(None)  # type: ignore[arg-type]
    finally:
        b.close()
    with pytest.raises(ValueError):
        X11Backend(xvfb_display, clipboard_tool="bogus")


@pytest.mark.x11
def test_accessibility_with_fake_atspi(xvfb_display, xc):
    win = make_window(xc, (100, 100, 400, 300), title="Fake App Window")
    desktop, acc = build_fake_tree(pid=os.getpid(), title="Fake App Window", rect=(100, 100, 400, 300))
    b = X11Backend(xvfb_display, clipboard_tool="none", accessibility=make_fake_atspi(desktop))
    try:
        assert CAP_ACCESSIBILITY in b.capabilities
        roots = b.accessibility_roots()
        assert len(roots) == 1
        root = roots[0]
        assert root.role == "window" and root.handle == win.id
        assert root.parent is not None and root.parent.role == "application"
        el = b.element_at(115, 115)
        assert el is not None and el.name == "OK" and el.role == "button"
        nested = b.element_at(115, 275)
        assert nested is not None and nested.name == "Two"
        assert b.element_at(5, 5) is None
        foc = b.focused_element()
        assert foc is not None and foc.name == "Name"
    finally:
        b.close()
        win.destroy()
        xc.sync()


@pytest.mark.x11
def test_launch_sets_display(be):
    import subprocess

    proc = be.launch([sys.executable, "-c", "import os; print(os.environ['DISPLAY'])"], stdout=subprocess.PIPE)
    out, _ = proc.communicate(timeout=10)
    assert out.decode().strip() == be.display_name


@pytest.mark.x11
def test_native_clipboard_roundtrip_between_connections(xvfb_display):
    """Without xclip/xsel the backend owns CLIPBOARD itself and other X clients can read it."""
    owner = X11Backend(xvfb_display, clipboard_tool="native", accessibility=False)
    reader = X11Backend(xvfb_display, clipboard_tool="native", accessibility=False)
    try:
        assert CAP_CLIPBOARD in owner.capabilities
        text = "Factura Nº 42 – €1.234,56 ✓\nsecond line"
        owner.set_clipboard(text)
        assert owner.get_clipboard() == text  # fast path (we own it)
        assert reader.get_clipboard() == text  # real selection conversion from another client
        big = "x" * 100_000
        owner.set_clipboard(big)
        assert reader.get_clipboard() == big
        reader.set_clipboard("taken over")  # ownership moves; old owner gets SelectionClear
        assert owner.get_clipboard() == "taken over"
    finally:
        owner.close()
        reader.close()
