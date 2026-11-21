"""Polish pass 3 (2026-11) on locum.llm: fewer cross-process reads, one annotation per
changes_only observe, a second MCP serve() after a stuck call, audit/release failures logged.

Every optimisation here must leave the output unchanged: each test compares against the
pre-change behaviour (an oracle written the old way) as well as counting the reads."""

from __future__ import annotations

import collections
import io
import json
import logging
import threading
import time
from typing import Any, List

import pytest

from locum.backends import mock
from locum.backends.base import AccessibleNode
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
import importlib

observe_mod = importlib.import_module("locum.llm.observe")
from locum.llm import tools as tools_mod
from locum.llm.observe import (
    Observation, ObservedElement, _is_interactive, _node_value, _PreRead, _same, observe,
)
from locum.llm.obsdiff import _TIERS, _runtime_id, match
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit
from locum._nodeinfo import is_password
from locum.types import Point, Rect

READS: collections.Counter = collections.Counter()


class Boom(Exception):
    pass


class FakeNode(AccessibleNode):
    """A node whose every field read is counted (a cross-process call on a real desktop)."""

    def __init__(self, role, name="", rect=None, value=None, states=(), actions=(), kids=(), handle=None,
                 native_role=None, raise_on=()):
        self._role, self._name, self._rect, self._value = role, name, rect, value
        self._states = states if states is None else frozenset(states)
        self._actions, self._kids, self._handle = list(actions), list(kids), handle
        self._native, self._raise = native_role, set(raise_on)

    def _r(self, what, val):
        READS[what] += 1
        if what in self._raise:
            raise Boom(what)
        return val

    @property
    def role(self):
        return self._r("role", self._role)

    @property
    def native_role(self):
        return self._native if self._native is not None else self.role

    @property
    def name(self):
        return self._r("name", self._name)

    @property
    def value(self):
        return self._r("value", self._value)

    @property
    def bounds(self):
        return self._r("bounds", None if self._rect is None else Rect(*self._rect))

    @property
    def states(self):
        return self._r("states", self._states)

    @property
    def handle(self):
        return self._handle

    def children(self):
        return list(self._kids)

    def actions(self):
        return self._r("actions", list(self._actions))


class OwnVisible(FakeNode):
    """A backend overriding ``is_visible``: observe must still ask it."""

    @property
    def is_visible(self):
        READS["is_visible_override"] += 1
        return self._name != "hidden-by-override"


def _fake_desktop():
    be = MockBackend()
    front = be.add_window("Front", (150, 150, 200, 200), app="front")
    back = be.add_window("Back", (100, 100, 600, 400), app="back", activate=False)
    be._raise(front)
    kids = [
        FakeNode("button", "OK", (110, 110, 50, 20)),
        FakeNode("button", "Under front", (200, 200, 40, 20)),
        FakeNode("edit", "Pass", (110, 140, 100, 20), value="hunter2", states={"password", "editable"}),
        FakeNode("text", "pw-native", (110, 170, 100, 20), value="s3cret", native_role="password text"),
        FakeNode("edit", "Plain", (110, 200, 100, 20), value="hello", states={"editable", "focused"}),
        FakeNode("text", "Label only", (110, 260, 100, 20)),
        FakeNode("text", "", (110, 290, 100, 20), value="value only"),
        FakeNode("text", "", (110, 320, 100, 20)),
        FakeNode("pane", "", (110, 350, 100, 20), actions=["invoke"]),
        FakeNode("pane", "", (110, 380, 100, 20), states={"editable"}),
        FakeNode("button", "Offscreen", (110, 410, 50, 20), states={"offscreen"}),
        FakeNode("button", "Invisible", (110, 410, 50, 20), states={"invisible"}),
        FakeNode("button", "Empty", (110, 410, 0, 0)),
        FakeNode("button", "NoBounds", None),
        FakeNode("button", "Outside", (5000, 5000, 10, 10)),
        FakeNode("button", "StatesNone", (400, 110, 50, 20), states=None),
        FakeNode("button", "StatesRaise", (400, 140, 50, 20), raise_on={"states"}),
        FakeNode("button", "RoleRaise", (400, 170, 50, 20), raise_on={"role"}),
        FakeNode("Button", "BoundsRaise", (400, 200, 50, 20), raise_on={"bounds"}),
        FakeNode("CheckBox", "Upper", (400, 230, 50, 20), states={"checked"}),
        FakeNode("pane", "ActionsRaise", (400, 260, 50, 20), raise_on={"actions"}),
        OwnVisible("button", "hidden-by-override", (400, 290, 50, 20)),
        OwnVisible("button", "shown-by-override", (400, 320, 50, 20), states={"offscreen"}),
        OwnVisible("button", "override-empty", (400, 350, 0, 0)),
        FakeNode("group", "Grp", (450, 110, 100, 100), kids=[
            FakeNode("button", "Nested", (460, 120, 40, 20)),
            FakeNode("document", "Doc", (460, 150, 80, 40), kids=[FakeNode("link", "In doc", (470, 160, 30, 10))]),
        ]),
    ]
    roots = [
        FakeNode("window", "Back", (100, 100, 600, 400), kids=kids, handle=back.handle),
        FakeNode("menu", "Popup", (700, 600, 100, 50), kids=[FakeNode("menuitem", "Item", (705, 605, 80, 20))]),
        FakeNode("window", "Front", (150, 150, 200, 200), handle=front.handle,
                 kids=[FakeNode("button", "Front btn", (160, 160, 50, 20))]),
    ]
    be.accessibility_roots = lambda: list(roots)
    return Desktop(be, failsafe=False), roots


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _old_walk(desktop, roots, interactive_only):
    """The element filter and fields of observe() before this pass (every field re-read)."""
    front = [(w.handle, w.rect) for w in desktop.backend.list_windows() if w.is_visible and not w.is_minimized]
    z_index = {h: i for i, (h, _) in enumerate(front)}
    titles = {w.handle: w for w in desktop.backend.list_windows()}
    zs = [z_index.get(r.handle) for r in roots]
    slots = [i for i, z in enumerate(zs) if z is not None]
    picked = [roots[k] for k in sorted(slots, key=lambda k: zs[k])]
    roots = list(roots)
    for i, r in zip(slots, picked):
        roots[i] = r
    region = desktop.virtual_rect()
    out = []
    for root in roots:
        info = titles.get(root.handle)
        title = info.title if info is not None else (_safe(lambda: root.name, "") or "")
        app = info.app if info is not None else ""
        zi = z_index.get(root.handle)
        for node in root.walk():
            if node is root and _safe(lambda: node.role, "") == "window":
                continue
            b = _safe(lambda: node.bounds)
            if b is None or b.is_empty or not _safe(lambda: node.is_visible, False):
                continue
            if b.intersect(region) is None:
                continue
            if interactive_only:
                if not _is_interactive(node):
                    continue
            elif not (_safe(lambda: node.name, "") or _safe(lambda: node.value) or _is_interactive(node)):
                continue
            states = set(_safe(lambda: node.states, frozenset()) or ())
            if zi is not None and any(r.contains(b.center) for _, r in front[:zi]):
                states.add("occluded")
            out.append((len(out) + 1, (_safe(lambda: node.role, "") or "unknown").lower(),
                        _safe(lambda: node.name, "") or "", _node_value(node), tuple(b), tuple(sorted(states)),
                        title, app))
    return out


# ============================================================ 1. observe walk reads
@pytest.mark.parametrize("interactive_only", [True, False])
def test_observe_output_identical_to_the_old_walk(interactive_only):
    d, roots = _fake_desktop()
    obs = observe(d, screenshot=False, selectors=False, interactive_only=interactive_only)
    got = [(e.id, e.role, e.name, e.value, tuple(e.bounds), tuple(sorted(e.states)), e.window_title, e.app)
           for e in obs.elements]
    assert got == _old_walk(d, roots, interactive_only)
    names = [g[2] for g in got]
    # The cases the pre-read values must decide exactly as before:
    assert "shown-by-override" in names and "hidden-by-override" not in names  # override consulted
    for gone in ("Offscreen", "Invisible", "Empty", "NoBounds", "Outside", "StatesNone", "StatesRaise",
                 "BoundsRaise"):
        assert gone not in names
    by_name = {g[2]: g for g in got}
    assert by_name["Pass"][3] == "***"  # password masking unchanged
    assert by_name["Upper"][1] == "checkbox" and "occluded" in by_name["Under front"][5]
    if not interactive_only:
        assert by_name["pw-native"][3] == "***"  # native password role, via the pre-read proxy


def test_observe_reads_each_node_field_once_and_still_asks_is_visible_overrides():
    READS.clear()
    d, _ = _fake_desktop()
    observe(d, screenshot=False, selectors=False)
    walked = 1 + 25 + 2 + 1 + 1 + 2 + 1  # Back + its kids + Grp kids + In doc + Popup + Item + Front + btn
    assert READS["bounds"] == walked - 2  # every node once (the two window roots are skipped by role)
    assert READS["states"] <= walked
    assert READS["is_visible_override"] == 2  # the two override nodes with non-empty bounds


def test_observe_mock_tree_read_counts():
    cnt = collections.Counter()
    saved = {}
    for prop in ("states", "role", "bounds"):
        orig = getattr(mock.MockNode, prop)
        saved[prop] = orig
        setattr(mock.MockNode, prop, property(lambda s, _o=orig, _p=prop: (cnt.update([_p]), _o.fget(s))[1]))
    try:
        be = MockBackend()
        d = Desktop(be, failsafe=False)
        w = be.add_window("App", (100, 100, 600, 400))
        for i in range(10):
            w.add_edit(f"E{i}", (10, 10 + i * 30, 100, 20), value=f"v{i}")
        for i in range(10):
            w.add_button(f"B{i}", (200, 10 + i * 30, 60, 20))
        obs = observe(d, screenshot=False)
    finally:
        for prop, orig in saved.items():
            setattr(mock.MockNode, prop, orig)
    assert len(obs.elements) == 20
    # Before: states 50, role 62, bounds 40. Now one each per node (+ the root window's role,
    # + native_role -> role in the password check of the 10 edits with a value).
    assert cnt["bounds"] == 20 and cnt["states"] == 20 and cnt["role"] <= 32


@pytest.mark.parametrize("states,role,native", [
    ({"password"}, "edit", None), ({"protected"}, "text", None), (set(), "edit", None),
    (set(), "Password Text", None), (set(), "text", "AXSecureTextField"), (None, "edit", None),
    (set(), None, "password text"), ({"editable"}, 7, None),
])
def test_preread_password_check_matches_is_password(states, role, native):
    node = FakeNode(role, "f", (0, 0, 5, 5), value="secret-ish", states=states, native_role=native)
    pre = _PreRead(node, node.role, node.states)
    assert is_password(pre) == is_password(node)
    assert _node_value(node, node.role, node.states) == _node_value(node)
    # Reads that raised are not handed down: the check re-reads, exactly as before.
    raising = FakeNode("edit", "f", (0, 0, 5, 5), value="x", raise_on={"states"})
    assert is_password(_PreRead(raising, raising.role)) == is_password(raising)


# ========================================================= 2. add_element by runtime id
class RidNode:
    def __init__(self, i, rid=True):
        self.i, self.has_rid = i, rid
        self.automation_id, self.role, self.name, self.value = "", "button", f"n{i}", None
        self.states, self.bounds, self.handle = frozenset(), Rect(i, 0, 5, 5), None

    @property
    def runtime_id(self):
        READS["runtime_id"] += 1
        return (42, self.i) if self.has_rid else None

    def same_as(self, other):
        a, b = self.runtime_id, getattr(other, "runtime_id", None)
        if a and b:
            return a == b
        return getattr(other, "i", None) == self.i

    def parent(self):
        return None


def _obs(nodes):
    els = [ObservedElement(k + 1, "button", f"b{k}", None, Rect(k, 0, 10, 10), frozenset(), "", n)
           for k, n in enumerate(nodes)]
    return Observation(None, els, [], None, Point(0, 0), (100, 100), 1.0, None)


def _old_find(obs, node):
    for el in obs.elements:
        if _same(el.node, node):
            return el
    return None


def test_add_element_runtime_id_index_reads_and_results():
    obs = _obs([RidNode(i) for i in range(300)])
    READS.clear()
    for j in range(100):
        obs.add_element(RidNode(1000 + j), selector="x")
    assert len(obs.elements) == 400
    assert READS["runtime_id"] <= 500  # was 69,900 (every add compared with every element)
    # An existing node keeps its entry (and its id), found through the index.
    hit = obs.add_element(RidNode(150), selector="x")
    assert hit is obs.elements[150] and len(obs.elements) == 400
    added = obs.add_element(RidNode(1050), selector="x")
    assert added is obs.elements[350]


def test_add_element_identical_to_the_scan_with_mixed_and_mutated_elements():
    # Elements without a runtime id (compared one by one) before and after rid elements.
    nodes = [RidNode(1, rid=False), RidNode(2), RidNode(3, rid=False), RidNode(4), RidNode(2)]
    obs = _obs(nodes)
    probes = [RidNode(1), RidNode(2), RidNode(3), RidNode(4), RidNode(9), RidNode(5, rid=False)]
    for p in probes:
        expected = _old_find(obs, p)
        got = obs._find_same(p, observe_mod._rid_key(p.runtime_id))
        assert got is expected, p.i
    # Elements replaced from outside (Observation.elements is public): the index is rebuilt.
    obs._find_same(RidNode(4), (42, 4))
    obs.elements[3] = ObservedElement(4, "button", "x", None, Rect(0, 0, 1, 1), frozenset(), "", RidNode(77))
    assert obs.add_element(RidNode(77)) is obs.elements[3]
    obs.elements.append(ObservedElement(99, "button", "y", None, Rect(0, 0, 1, 1), frozenset(), "", RidNode(88)))
    assert obs.add_element(RidNode(88)) is obs.elements[-1]
    n = len(obs.elements)
    assert obs.add_element(RidNode(4)).node.i == 4 and len(obs.elements) == n + 1  # replaced: now new


def test_add_element_without_runtime_ids_unchanged():
    be = MockBackend()
    d = Desktop(be, failsafe=False)
    w = be.add_window("App", (100, 100, 400, 300))
    w.add_button("OK", (10, 10, 50, 20))
    obs = observe(d, screenshot=False)
    node = obs.elements[0].node
    again = obs.add_element(mock.MockNode(be, node.widget))
    assert again is obs.elements[0] and len(obs.elements) == 1


# ============================================================ 3. one window snapshot
def _crm():
    be = MockBackend(width=1920, height=1080)
    d = Desktop(be, failsafe=False, default_timeout=0.2)
    win = be.add_window("New Customer", (200, 150, 600, 400), app="crm")
    for i in range(3):
        be.add_window(f"Other {i}", (900, 100 + i * 10, 300, 200), app=f"app{i}", activate=False)
    edit = win.add_edit("Customer name", (20, 60, 300, 28))
    win.add_button("OK", (20, 340, 90, 32))
    be._set_focus(edit)
    tk = DesktopToolkit(d, Policy.permissive())
    obs = tk.call("observe", {}).data
    return be, tk, {e.name: e.id for e in obs.elements}


def _fields(t):
    return (t.description, t.name, t.app, t.apps, t.unknown, t.secret, t.window, t.point)


@pytest.mark.parametrize("how", ["element", "point", "focus", "selector", "text"])
def test_target_resolution_lists_windows_once_with_identical_targets(how):
    be, tk, ids = _crm()

    def resolve():
        if how == "element":
            return tk._target(element_id=ids["OK"], activates=True)
        if how == "point":
            return tk._target(x=300, y=300, activates=True)
        if how == "selector":
            return tk._target(selector='button[name="OK"]', activates=True)
        if how == "text":
            return tk._target(text="OK", activates=True)
        return tk._focus_target(True)

    n = [0]
    orig = be.list_windows

    def counting():
        n[0] += 1
        return orig()

    be.list_windows = counting
    new = resolve()
    assert n[0] == 1  # was 2 (element), 4 (point), 2 (focus)
    # The old way: every lookup lists the windows itself.
    tk._windows = lambda: None
    n[0] = 0
    old = resolve()
    assert n[0] >= 1
    assert _fields(new) == _fields(old)


# ===================================================== 4. changes_only: annotate once
def _changes_scenario(force_old: bool, monkeypatch):
    if force_old:  # the old path: observe() annotates, compare() redraws
        real = tools_mod.observe
        monkeypatch.setattr(tools_mod, "observe", lambda *a, **k: real(*a, **{**k, "annotate": True}))
    draws = [0]
    real_draw = observe_mod._draw_boxes

    def counting(*a, **k):
        draws[0] += 1
        return real_draw(*a, **k)

    monkeypatch.setattr(observe_mod, "_draw_boxes", counting)
    be = MockBackend()
    d = Desktop(be, failsafe=False)
    w = be.add_window("App", (100, 100, 400, 300))
    w.add_button("OK", (10, 10, 50, 20))
    w.add_edit("Name", (10, 50, 100, 20))
    tk = DesktopToolkit(d, Policy.permissive())
    out = []

    def call(args):
        draws[0] = 0
        r = tk.call("observe", args)
        out.append((r.image_base64, r.text, draws[0]))

    call({"changes_only": True})  # no earlier observation: the full fallback
    call({})
    w.add_button("Zed", (200, 10, 50, 20))
    w.add_button("Aaa", (10, 100, 60, 20))  # new ids between old ones: renumbered by the diff
    call({"changes_only": True})
    w.add_button("Big", (0, 0, 390, 280))  # a large change: the full (annotated) image
    call({"changes_only": True})
    call({"changes_only": True})  # nothing changed
    call({"changes_only": True, "window": "App"})  # another region: the full fallback
    monkeypatch.undo()
    return out


def test_changes_only_draws_once_and_sends_pixel_identical_images(monkeypatch):
    new = _changes_scenario(False, monkeypatch)
    old = _changes_scenario(True, monkeypatch)
    assert [(img, text) for img, text, _ in new] == [(img, text) for img, text, _ in old]
    assert any(img for img, _, _ in new[2:])  # images really compared (crop / full / fallback)
    assert [d for _, _, d in new] == [1, 1, 1, 1, 1, 1]
    assert all(d >= 2 for _, _, d in old[2:5])  # observe() drew boxes the diff then redrew


def test_changes_only_pixels_equal_after_decoding(monkeypatch):
    import base64

    from PIL import Image, ImageChops

    new = _changes_scenario(False, monkeypatch)
    old = _changes_scenario(True, monkeypatch)
    for (a, _, _), (b, _, _) in zip(new, old):
        if a is None:
            assert b is None
            continue
        ia = Image.open(io.BytesIO(base64.b64decode(a))).convert("RGB")
        ib = Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB")
        assert ia.size == ib.size and ImageChops.difference(ia, ib).getbbox() is None


def test_changes_only_respects_annotate_false(monkeypatch):
    be = MockBackend()
    d = Desktop(be, failsafe=False)
    be.add_window("App", (100, 100, 400, 300)).add_button("OK", (10, 10, 50, 20))
    tk = DesktopToolkit(d, Policy.permissive(), observe_kwargs={"annotate": False})
    tk.call("observe", {})
    tk.call("observe", {"changes_only": True})
    assert tk.last_observation.annotated is None


# ===================================================== 5. obsdiff tier 0 reads
class _RidN:
    def __init__(self, i):
        self.i, self.automation_id = i, ""

    @property
    def runtime_id(self):
        READS["rid"] += 1
        return (42, self.i) if self.i is not None else None


def _old_match(prev, new):
    pairs, left_prev, left_new = [], list(prev), list(new)
    rids = {id(e): _runtime_id(e) for e in (*left_prev, *left_new)}

    def compatible(p, n):
        a, b = rids.get(id(p)), rids.get(id(n))
        return a is None or b is None or a == b

    for key_of in _TIERS:
        if not left_prev or not left_new:
            break
        index: dict = {}
        for p in left_prev:
            k = _safe(lambda: key_of(p))
            if k is not None:
                index.setdefault(k, []).append(p)
        used, still = set(), []
        for n in left_new:
            k = _safe(lambda: key_of(n))
            bucket = index.get(k) if k is not None else None
            pick = next((i for i, p in enumerate(bucket) if compatible(p, n)), None) if bucket else None
            if pick is not None:
                p = bucket.pop(pick)
                used.add(id(p))
                pairs.append((p, n))
            else:
                still.append(n)
        left_new = still
        left_prev = [p for p in left_prev if id(p) not in used]
    return pairs


def _els(spec):
    return [ObservedElement(k + 1, "button", name, None, Rect(x, 0, 10, 10), frozenset(), "W", _RidN(rid))
            for k, (rid, name, x) in enumerate(spec)]


def test_match_reads_each_runtime_id_once_and_pairs_identically():
    prev = _els([(1, "a", 0), (2, "b", 10), (None, "c", 20), (4, "d", 30), (None, "e", 40)])
    new = _els([(2, "b", 11), (1, "zz", 0), (None, "c", 20), (5, "d", 30), (None, "e", 99), (None, "f", 50)])
    expected = [(prev.index(p), new.index(n)) for p, n in _old_match(prev, new)]
    READS.clear()
    got = [(prev.index(p), new.index(n)) for p, n in match(prev, new)]
    assert got == expected
    assert READS["rid"] == len(prev) + len(new)
    big_p, big_n = _els([(i, f"b{i}", i) for i in range(300)]), _els([(i, f"b{i}", i) for i in range(300)])
    READS.clear()
    assert len(match(big_p, big_n)) == 300 and READS["rid"] == 600  # was 1800


# ===================================================== 6. MCP: serve() twice
def test_second_serve_after_stuck_call_runs_calls_and_never_concurrently():
    from locum.llm.mcp_server import MCPServer
    from locum.llm.tools import ToolSpec

    tk = DesktopToolkit(Desktop(MockBackend(), failsafe=False), Policy.permissive())
    gate = threading.Event()
    running = {"stuck": False, "overlap": False, "quick": 0}

    def stuck() -> str:
        """Blocks in 'native code' (ignores cancellation)."""
        running["stuck"] = True
        try:
            gate.wait(20)
        finally:
            running["stuck"] = False
        return "unstuck"

    def quick() -> str:
        """Returns at once."""
        running["overlap"] |= running["stuck"]
        running["quick"] += 1
        return "quick ok"

    tk.registry.add(ToolSpec.from_function(stuck, read_only=True))
    tk.registry.add(ToolSpec.from_function(quick, read_only=True))
    srv = MCPServer(toolkit=tk, call_timeout=0.2)

    def serve(i, name):
        req = json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                          "params": {"name": name, "arguments": {}}}) + "\n"
        out = io.StringIO()
        srv.serve(io.StringIO(req), out)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    first = serve(1, "stuck")
    assert "Timed out" in first["result"]["content"][0]["text"]
    # Still stuck: the new serve()'s call waits for it (two calls never drive the desktop at once).
    second = serve(2, "quick")
    assert "earlier call" in second["result"]["content"][0]["text"] and running["quick"] == 0
    gate.set()
    deadline = time.monotonic() + 5
    while running["stuck"] and time.monotonic() < deadline:
        time.sleep(0.01)
    # Before: serve() #1's stop marker stopped every later worker -- nothing ever ran again.
    third = serve(3, "quick")
    assert third["result"]["content"][0]["text"] == "quick ok"
    assert running["quick"] == 1 and not running["overlap"]


def test_each_serve_uses_its_own_queue():
    from locum.llm.mcp_server import MCPServer

    srv = MCPServer(toolkit=DesktopToolkit(Desktop(MockBackend(), failsafe=False), Policy.permissive()))
    srv.serve(io.StringIO(""), io.StringIO())
    q1 = srv._queue
    srv.serve(io.StringIO(""), io.StringIO())
    assert srv._queue is not q1 and srv._stuck_worker is None


# ===================================================== 7. failures logged, never raised
class _FailingAudit:
    events: List[Any] = []

    def record(self, event):
        raise OSError("disk full")


def test_guarded_desktop_audit_failure_is_logged_not_raised(caplog):
    from locum.llm.safety import GuardedDesktop

    g = GuardedDesktop(Desktop(MockBackend(), failsafe=False), Policy.permissive(), audit=_FailingAudit())
    with caplog.at_level(logging.ERROR, logger="locum.llm.safety"):
        g._record("click", {"x": 1}, "ok", "click OK", {})
    assert any("Audit log write failed" in r.getMessage() and "disk full" in r.getMessage() for r in caplog.records)


def test_agent_release_failures_are_logged_not_raised(caplog, monkeypatch):
    from locum.llm.agent import Agent, ScriptedClient

    ag = Agent(Desktop(MockBackend(), failsafe=False), client=ScriptedClient([]), policy=Policy.permissive())

    def boom(*a, **k):
        raise RuntimeError("stuck key")

    monkeypatch.setattr(ag.computer, "release_all", boom)
    monkeypatch.setattr(type(ag.desktop.keyboard), "release_all", boom)
    with caplog.at_level(logging.ERROR, logger="locum.llm.agent"):
        ag._release_input()  # must not raise
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("Could not release" in m and "stuck key" in m for m in msgs) == 2


def test_agent_release_audit_failure_not_raised(caplog, monkeypatch):
    from locum.llm.agent import Agent, ScriptedClient

    ag = Agent(Desktop(MockBackend(), failsafe=False), client=ScriptedClient([]), policy=Policy.permissive())
    monkeypatch.setattr(ag.computer, "release_all", lambda: ["left"])
    monkeypatch.setattr(ag, "audit", _FailingAudit())
    with caplog.at_level(logging.ERROR, logger="locum.llm.agent"):
        ag._release_input()
    assert any("Audit log write failed" in r.getMessage() for r in caplog.records)


# ===================================================== 8. dead attribute
def test_browser_snapshot_attribute_removed():
    import inspect

    assert "_browser_snapshot" not in inspect.getsource(tools_mod)
