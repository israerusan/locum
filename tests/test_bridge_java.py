"""locum.bridges.java: struct layouts against AccessBridgePackages.h and a fake bridge DLL."""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any, Dict, List, Optional

import pytest

from locum.bridges import java
from locum.bridges.java import (
    AccessibleActionInfo,
    AccessibleActions,
    AccessibleActionsToDo,
    AccessibleContextInfo,
    AccessibleTextInfo,
    JavaBridge,
)
from locum.errors import ActionTimeout, ElementNotFound, NotSupported, PolicyViolation, TargetLost
from locum.llm.safety import AuditLog, Policy


# ---------------------------------------------------------------------- layouts
def test_context_info_layout_matches_header():
    # AccessBridgePackages.h: wchar_t name[MAX_STRING_SIZE=1024], description[1024], role[SHORT_STRING_SIZE=256],
    # role_en_US[256], states[256], states_en_US[256]; jint indexInParent, childrenCount, x, y, width, height;
    # BOOL accessibleComponent, accessibleAction, accessibleSelection, accessibleText, accessibleInterfaces.
    assert java.MAX_STRING_SIZE == 1024 and java.SHORT_STRING_SIZE == 256
    expected = [
        ("name", 0), ("description", 2048), ("role", 4096), ("role_en_US", 4608), ("states", 5120),
        ("states_en_US", 5632), ("indexInParent", 6144), ("childrenCount", 6148), ("x", 6152), ("y", 6156),
        ("width", 6160), ("height", 6164), ("accessibleComponent", 6168), ("accessibleAction", 6172),
        ("accessibleSelection", 6176), ("accessibleText", 6180), ("accessibleInterfaces", 6184),
    ]
    assert [(n, getattr(AccessibleContextInfo, n).offset) for n, _ in expected] == expected
    assert [f[0] for f in AccessibleContextInfo._fields_] == [n for n, _ in expected]
    assert ctypes.sizeof(AccessibleContextInfo) == 6188
    assert AccessibleContextInfo.name.size == 2048 and AccessibleContextInfo.role.size == 512


def test_other_layouts_match_header():
    assert ctypes.sizeof(AccessibleTextInfo) == 12
    assert [(f, getattr(AccessibleTextInfo, f).offset) for f in ("charCount", "caretIndex", "indexAtPoint")] == [
        ("charCount", 0), ("caretIndex", 4), ("indexAtPoint", 8)]
    assert ctypes.sizeof(AccessibleActionInfo) == 512  # wchar_t name[256]
    assert java.MAX_ACTION_INFO == 256 and java.MAX_ACTIONS_TO_DO == 32
    assert AccessibleActions.actionInfo.offset == 4 and ctypes.sizeof(AccessibleActions) == 4 + 256 * 512
    assert AccessibleActionsToDo.actions.offset == 4 and ctypes.sizeof(AccessibleActionsToDo) == 4 + 32 * 512


def test_object_handles_are_64_bit_and_vm_id_is_a_win32_long():
    assert ctypes.sizeof(java.JOBJECT64) == 8  # jlong in both WindowsAccessBridge-32.dll and -64.dll
    assert ctypes.sizeof(java.VMID) == 4
    assert ctypes.sizeof(java.WCHAR) == 2


def test_dll_name_follows_python_bitness():
    import struct

    assert java.dll_name() == ("WindowsAccessBridge-64.dll" if struct.calcsize("P") == 8
                               else "WindowsAccessBridge-32.dll")


def test_wide_string_round_trip():
    info = AccessibleContextInfo()
    java._set_wstr(info.name, "Kundennummer ü€")
    assert java._wstr(info.name) == "Kundennummer ü€"
    java._set_wstr(info.role, "x" * 1000)  # truncated to the field, NUL kept
    assert java._wstr(info.role) == "x" * 255


# ---------------------------------------------------------------------- fake DLL
class Obj:
    def __init__(self, name: str, role: str, states: str, rect=(0, 0, 0, 0), kids=None, actions=(),
                 text: Optional[str] = None, editable_text: bool = False, fresh_kids: bool = False) -> None:
        self.name, self.role, self.states, self.rect = name, role, states, rect
        self.kids: List[Obj] = list(kids or [])
        self.actions = list(actions)
        self.text = text
        self.editable_text = editable_text
        self.done: List[str] = []
        #: Like Swing's AccessibleJTable/JList/JTree: every getAccessibleChildFromContext returns a NEW
        #: proxy object (isSameObject never matches an earlier read of the same cell).
        self.fresh_kids = fresh_kids


class Proxy:
    """A fresh accessible object standing for ``target`` (an ``AccessibleJTableCell``)."""

    def __init__(self, target: Obj) -> None:
        self.__dict__["_target"] = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target, name, value)


class FakeDll:
    """Implements the bridge entry points the module calls, over a tree of :class:`Obj`."""

    def __init__(self, roots: Dict[int, Obj], vm: int = 7) -> None:
        self.roots = roots
        self.vm = vm
        self.handles: Dict[int, Obj] = {}
        self.refs: Dict[int, int] = {}
        self.next = 1000
        self.released: List[int] = []
        self.run_thread: Optional[str] = None
        self.java_ready_after = 0  # isJavaWindow polls that return False first

    def _ref(self, obj: Obj) -> int:
        self.next += 1
        self.handles[self.next] = obj
        self.refs[self.next] = 1
        return self.next

    def _obj(self, ac: int) -> Obj:
        if ac not in self.refs:
            raise AssertionError(f"use of released/unknown object {ac}")
        return self.handles[ac]

    # entry points
    def Windows_run(self) -> None:
        self.run_thread = threading.current_thread().name

    def isJavaWindow(self, hwnd) -> int:
        if self.java_ready_after > 0:
            self.java_ready_after -= 1
            return 0
        return int(hwnd in self.roots)

    def getAccessibleContextFromHWND(self, hwnd, vm_ref, ac_ref) -> int:
        if hwnd not in self.roots:
            return 0
        vm_ref._obj.value = self.vm
        ac_ref._obj.value = self._ref(self.roots[hwnd])
        return 1

    def getHWNDFromAccessibleContext(self, vm, ac):
        return None

    def getAccessibleContextInfo(self, vm, ac, info_ref) -> int:
        assert vm == self.vm
        o = self._obj(ac)
        info = info_ref._obj
        java._set_wstr(info.name, o.name)
        java._set_wstr(info.role, o.role)
        java._set_wstr(info.role_en_US, o.role)
        java._set_wstr(info.states, o.states)
        java._set_wstr(info.states_en_US, o.states)
        info.childrenCount = len(o.kids)
        info.x, info.y, info.width, info.height = o.rect
        info.accessibleAction = int(bool(o.actions))
        info.accessibleText = int(o.text is not None)
        info.accessibleComponent = 1
        return 1

    def getAccessibleChildFromContext(self, vm, ac, i) -> int:
        o = self._obj(ac)
        if not 0 <= i < len(o.kids):
            return 0
        return self._ref(Proxy(o.kids[i]) if o.fresh_kids else o.kids[i])

    def getAccessibleActions(self, vm, ac, acts_ref) -> int:
        o = self._obj(ac)
        acts = acts_ref._obj
        acts.actionsCount = len(o.actions)
        for i, a in enumerate(o.actions):
            java._set_wstr(acts.actionInfo[i].name, a)
        return 1

    def doAccessibleActions(self, vm, ac, todo_ref, failure_ref) -> int:
        o = self._obj(ac)
        todo = todo_ref._obj
        names = [java._wstr(todo.actions[i].name) for i in range(todo.actionsCount)]
        ok = all(n in o.actions for n in names)
        if ok:
            o.done.extend(names)
            failure_ref._obj.value = -1
        else:
            failure_ref._obj.value = 0
        return int(ok)

    def getAccessibleTextInfo(self, vm, ac, ti_ref, x, y) -> int:
        o = self._obj(ac)
        ti_ref._obj.charCount = len(o.text or "")
        return 1

    def getAccessibleTextRange(self, vm, ac, start, end, buf, length) -> int:
        o = self._obj(ac)
        piece = (o.text or "")[start:end + 1]
        assert len(piece) == length
        java._set_wstr(buf, piece)
        return 1

    def setTextContents(self, vm, ac, buf) -> int:
        o = self._obj(ac)
        if not o.editable_text:
            return 0
        o.text = java._wstr(buf)
        return 1

    def requestFocus(self, vm, ac) -> int:
        self._obj(ac).done.append("focus")
        return 1

    def isSameObject(self, vm, a, b) -> int:
        return int(self.handles.get(a) is self.handles.get(b))

    def releaseJavaObject(self, vm, ac) -> None:
        assert vm == self.vm
        self.refs[ac] -= 1
        if self.refs[ac] == 0:
            del self.refs[ac]
        self.released.append(ac)


HWND = 0x5150


def make_tree(hwnd: int = HWND, origin=(100, 100)) -> Dict[int, Obj]:
    ox, oy = origin
    customer = Obj("Customer", "text", "enabled,focusable,visible,showing,editable,single line",
                   (ox + 120, oy + 40, 200, 24), text="", editable_text=True)
    save = Obj("Save", "push button", "enabled,focusable,visible,showing", (ox + 10, oy + 300, 80, 30),
               actions=["click"])
    legacy = Obj("Legacy", "push button", "enabled,visible,showing", (ox + 100, oy + 300, 80, 30))
    disabled = Obj("Delete", "push button", "focusable,visible,showing", (ox + 200, oy + 300, 80, 30),
                   actions=["click"])
    rush = Obj("Rush order", "check box", "enabled,visible,showing,checked", (ox + 10, oy + 80, 120, 20),
               actions=["click"])
    secret = Obj("PIN", "password text", "enabled,visible,showing,editable", (ox + 120, oy + 120, 100, 24),
                 text="", editable_text=True)
    status = Obj("Status", "label", "enabled,visible,showing", (ox + 10, oy + 400, 300, 20), text="Ready")
    hidden = Obj("Ghost", "push button", "enabled,visible", (0, 0, 0, 0), actions=[])
    pay = Obj("Pay now", "push button", "enabled,visible,showing", (ox + 300, oy + 300, 80, 30), actions=["click"])
    panel = Obj("", "panel", "enabled,visible,showing", (ox, oy, 600, 440),
                kids=[Obj("Customer:", "label", "enabled,visible,showing", (ox + 10, oy + 40, 100, 24)),
                      customer, rush, secret, save, legacy, disabled, status, hidden, pay])
    frame = Obj("Order Entry", "frame", "enabled,visible,showing,active", (ox, oy, 640, 480), kids=[panel])
    return {hwnd: frame}


@pytest.fixture
def fake(monkeypatch):
    dll = FakeDll(make_tree())
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    return dll


def bridge(**kw: Any) -> JavaBridge:
    kw.setdefault("pump", False)
    kw.setdefault("settle", 0)
    kw.setdefault("sleep", lambda s: None)
    return JavaBridge(**kw)


# ---------------------------------------------------------------------- tree / selectors
def test_tree_roles_states_and_selectors(fake):
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        assert win.role == "window" and win.name == "Order Entry" and win.rect.width == 640
        cust = win.find('edit[name="Customer"]')
        assert cust.java_role == "text" and "editable" in cust.states and "visible" in cust.states
        assert win.find('button "Save"').actions() == ["click"]
        assert "disabled" in win.find('button "Delete"').states
        assert "checked" in win.find('checkbox "Rush order"').states
        assert "password" in win.find("edit:not([name=Customer])").states
        assert "offscreen" in win.find('button "Ghost"').states
        assert win.find('text[name="Status"]').text == "Ready"
        assert len(win.find_all("button")) == 5
        assert 'button "Save"' in win.tree()
        assert win.exists('button "Save"') and not win.exists('button "Nope"')
        with pytest.raises(ElementNotFound):
            win.find('button "Nope"')
        assert win.find('button "Save"').parent.role == "pane"


def test_text_reads_in_chunks(fake):
    big = "x" * 2500 + "END"
    fake.roots[HWND].kids[0].kids[7].text = big  # the Status label
    with bridge() as jab:
        assert jab.from_hwnd(HWND).find('text "Status"').text == big


def test_set_text_round_trip_and_refusals(fake):
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find('edit "Customer"').set_text("ACME GmbH")
        assert fake.roots[HWND].kids[0].kids[1].text == "ACME GmbH"
        fake.roots[HWND].kids[0].kids[1].editable_text = False
        with pytest.raises(NotSupported, match="setTextContents"):
            win.find('edit "Customer"').set_text("x")
        fake.roots[HWND].kids[0].kids[1].states = "enabled,visible,showing"  # not editable any more
        el = win.find('edit "Customer"')
        el.node.refresh()
        with pytest.raises(ElementNotFound, match="read-only"):
            el.set_text("x")


def test_set_text_verifies_read_back(fake, monkeypatch):
    with bridge() as jab:
        el = jab.from_hwnd(HWND).find('edit "Customer"')
        real = fake.setTextContents

        def truncating(vm, ac, buf):
            real(vm, ac, buf)
            fake._obj(ac).text = fake._obj(ac).text[:3]
            return 1

        monkeypatch.setattr(fake, "setTextContents", truncating)
        with pytest.raises(ActionTimeout, match="reads 'ACM'"):
            el.set_text("ACME")


def test_click_uses_accessible_action(fake):
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find('button "Save"').click()
        assert fake.roots[HWND].kids[0].kids[4].done == ["click"]
        with pytest.raises(ElementNotFound, match="disabled"):
            win.find('button "Delete"').click()
        with pytest.raises(NotSupported, match="no click action"):
            win.find('button "Legacy"').click(native=True)
        win.find('edit "Customer"').focus()
        assert fake.roots[HWND].kids[0].kids[1].done == ["focus"]


def test_click_without_action_falls_back_to_a_bound_mouse_click(monkeypatch, mock_backend):
    from locum import Desktop

    w = mock_backend.add_window("Order Entry", (100, 100, 640, 480), app="javaw.exe")
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    d = Desktop(mock_backend, failsafe=False)
    with bridge(desktop=d) as jab:
        jab.from_hwnd(w.handle).find('button "Legacy"').click()
    downs = [e for e in mock_backend.events if e[0] == "down"]
    assert downs and downs[-1][2:] == (240, 415)  # center of (200, 400, 80, 30)


def test_mouse_fallback_refuses_when_another_window_covers_the_point(monkeypatch, mock_backend):
    from locum import Desktop

    w = mock_backend.add_window("Order Entry", (100, 100, 640, 480), app="javaw.exe")
    mock_backend.add_window("Chat", (150, 350, 300, 200), app="chat.exe")  # covers the Legacy button
    monkeypatch.setattr(mock_backend, "activate_window", lambda h: None)  # cannot raise the Java window
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    d = Desktop(mock_backend, failsafe=False)
    with bridge(desktop=d) as jab:
        with pytest.raises(TargetLost):
            jab.from_hwnd(w.handle).find('button "Legacy"').click()
    assert not [e for e in mock_backend.events if e[0] == "down"]


def test_windows_and_window_lookup(monkeypatch, mock_backend):
    from locum import Desktop

    w = mock_backend.add_window("Order Entry - ACME", (100, 100, 640, 480), app="javaw.exe")
    mock_backend.add_window("Notepad", (0, 0, 100, 100), app="notepad.exe")
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    with bridge(desktop=Desktop(mock_backend, failsafe=False)) as jab:
        assert [x.title for x in jab.windows()] == ["Order Entry - ACME"]
        assert jab.window("order entry*").name == "Order Entry"
        from locum.errors import WindowNotFound

        with pytest.raises(WindowNotFound):
            jab.window("SAP", timeout=0)


# ---------------------------------------------------------------------- init / release
def test_not_a_java_window_explains_jabswitch(fake):
    with bridge() as jab:
        with pytest.raises(NotSupported, match="jabswitch -enable"):
            jab.from_hwnd(0xDEAD, timeout=0.01)


def test_is_java_window_is_polled_after_startup(fake):
    fake.java_ready_after = 3
    with bridge() as jab:
        assert jab.from_hwnd(HWND, timeout=5).name == "Order Entry"


def test_every_reference_is_released_on_close(fake):
    jab = bridge()
    win = jab.from_hwnd(HWND)
    win.find_all("*")
    assert jab.live_references > 10 and fake.refs
    jab.close()
    assert fake.refs == {} and jab.live_references == 0
    jab.close()  # idempotent


def test_element_release_frees_its_subtree_only(fake):
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        panel = win.children()[0]
        panel.find_all("*")
        before = jab.live_references
        panel.release()
        assert jab.live_references == 1  # the window itself
        assert before > 1
    assert fake.refs == {}


def test_message_pump_thread_runs_windows_run(fake):
    jab = JavaBridge(pump=True, settle=0)
    try:
        assert fake.run_thread == "locum-java-access-bridge"
        assert jab._pump is not None and jab._pump.is_alive()
    finally:
        jab.close()
    jab._pump.join(2)
    assert not jab._pump.is_alive()


def test_missing_dll_raises_not_supported_with_hint(monkeypatch):
    monkeypatch.setenv("LOCUM_JAB_DLL", r"C:\nowhere\WindowsAccessBridge-64.dll")
    monkeypatch.setattr(java.shutil, "which", lambda name: None)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.delenv("JRE_HOME", raising=False)
    if sys.platform == "win32":
        real = ctypes.CDLL

        def no_dll(name, *a, **kw):
            if "WindowsAccessBridge" in str(name):
                raise OSError("module not found")
            return real(name, *a, **kw)

        monkeypatch.setattr(java.ctypes, "CDLL", no_dll)
    with pytest.raises(NotSupported) as ei:
        java._load_dll()
    assert "jabswitch -enable" in str(ei.value) or "only available on Windows" in str(ei.value)


# ---------------------------------------------------------------------- policy
def test_policy_confirms_pay_and_allow_rules_use_java_selectors(fake):
    asked: List[str] = []
    audit = AuditLog()
    with bridge(policy=Policy(), confirm=lambda d, det: asked.append(d) or False, audit=audit) as jab:
        win = jab.from_hwnd(HWND)
        with pytest.raises(PolicyViolation, match="declined"):
            win.find('button "Pay now"').click()
        assert fake.roots[HWND].kids[0].kids[9].done == []
        win.find('button "Save"').click()
        assert asked == ["click 'Pay now'"]
        assert [e["status"] for e in audit.read()] == ["blocked", "allowed"]
        assert all(e["bridge"] == "java" for e in audit.read())
    pol = Policy(allow=['window[name="Order Entry"] edit[name="Customer"]'], confirm_patterns=())
    with bridge(policy=pol) as jab:
        win = jab.from_hwnd(HWND)
        win.find('edit "Customer"').set_text("ok")
        with pytest.raises(PolicyViolation, match="no allow rule"):
            win.find('button "Save"').click()


def test_policy_dry_run_and_password_masking(fake):
    audit = AuditLog()
    with bridge(policy=Policy(dry_run=True), audit=audit) as jab:
        win = jab.from_hwnd(HWND)
        win.find("edit:not([name=Customer])").set_text("1234")
        win.find('button "Save"').click()
    assert fake.roots[HWND].kids[0].kids[3].text == ""
    assert fake.roots[HWND].kids[0].kids[4].done == []
    events = audit.read()
    assert [e["status"] for e in events] == ["dry_run", "dry_run"]
    assert events[0]["args"]["text"] == "***"


# ---------------------------------------------------------------------- live reads
# Found by the real-JVM CI job: nodes cached their context info forever, so a
# checkbox that a click had checked still read as unchecked through the same
# window element. These pin the fix on the fake DLL.
def test_reads_follow_the_ui_after_it_changes(fake):
    panel = fake.roots[HWND].kids[0]
    rush, status = panel.kids[2], panel.kids[7]
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        box = win.find('checkbox "Rush order"')
        assert "checked" in box.states
        rush.states = "enabled,visible,showing"  # the user (or a click) unchecked it
        assert "checked" not in box.states  # an element already held
        assert "checked" not in win.find('checkbox "Rush order"').states  # a fresh lookup
        status.name = "Saved: ACME"  # a label's visible text is its accessible name
        assert win.find('text "Saved: ACME"').name == "Saved: ACME"
        assert not win.exists('text "Status"')
        panel.kids[4].states = "focusable,visible,showing"  # Save got disabled
        with pytest.raises(ElementNotFound, match="disabled"):
            win.find('button "Save"').click()


def test_lookups_see_added_and_removed_children_and_keep_held_elements(fake):
    panel = fake.roots[HWND].kids[0]
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        box = win.find('checkbox "Rush order"')
        assert not win.exists('button "Approve"')
        panel.kids.append(Obj("Approve", "push button", "enabled,visible,showing", (0, 0, 10, 10),
                              actions=["click"]))
        assert win.exists('button "Approve"')
        del panel.kids[0]  # the "Customer:" label goes away
        assert not win.exists('text "Customer:"')
        assert "checked" in box.states  # an element held across the change is still usable
        assert box.node is win.find('checkbox "Rush order"').node


def test_repeated_lookups_do_not_pile_up_references(fake):
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find_all("*")
        held = jab.live_references
        for _ in range(5):
            win.find('button "Save"')
            win.find_all("*")
            win.tree()
            assert win.exists('checkbox "Rush order"')
        assert jab.live_references == held
        # find(timeout=) re-reads the tree while it polls, without leaking either.
        with pytest.raises(ElementNotFound):
            win.find('button "Nope"', timeout=1.0)
        assert jab.live_references == held


def test_re_reading_a_big_container_costs_a_bounded_number_of_bridge_calls(fake):
    panel = fake.roots[HWND].kids[0]
    panel.kids[:] = [Obj(f"row {i}", "label", "enabled,visible,showing", (0, 0, 1, 1)) for i in range(500)]
    calls = []
    same = fake.isSameObject
    fake.isSameObject = lambda vm, a, b: calls.append(1) or same(vm, a, b)
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        assert len(win.find_all("text")) == 500
        del calls[:]
        del panel.kids[0]  # every row moves up one place
        rows = win.find_all("text")
        assert len(rows) == 499 and rows[0].name == "row 1"
        assert len(calls) <= 3 * 500  # index-aligned matching, not 500 x 500


# ---------------------------------------------------------------------- Swing proxies (JTable/JList/JTree)
def _cells(n: int) -> List[Obj]:
    return [Obj(f"cell {i}", "label", "enabled,visible,showing", (0, 0, 1, 1)) for i in range(n)]


def test_table_cells_are_fresh_proxies_and_references_stay_bounded(fake):
    panel = fake.roots[HWND].kids[0]
    table = Obj("Orders", "table", "enabled,visible,showing", (0, 0, 300, 300), kids=_cells(500), fresh_kids=True)
    panel.kids.append(table)
    calls = []
    same = fake.isSameObject
    fake.isSameObject = lambda vm, a, b: calls.append(1) or same(vm, a, b)
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        assert len(win.find_all("table text")) == 500
        held = jab.live_references
        cell = win.find('table text "cell 7"')
        del calls[:]
        for _ in range(5):
            assert len(win.find_all("table text")) == 500
            win.tree()
        assert jab.live_references == held  # each re-read swaps a cell's reference, it does not add one
        assert len(fake.refs) == held  # and the swapped-out ones were released in the JVM
        # no isSameObject matching of cells that can never match: only the window's dozen other
        # controls are matched, once per re-read (10 re-reads), never the 500 cells
        assert len(calls) <= 10 * 12
        assert cell.name == "cell 7"  # a held cell element stays valid (it is kept by index)
        table.kids[7].name = "cell 7 (edited)"
        assert cell.name == "cell 7 (edited)"
    assert fake.refs == {}


def test_tree_nodes_below_a_tree_are_kept_by_index(fake):
    leaves = [Obj("Invoices", "label", "enabled,visible,showing", (0, 0, 1, 1)),
              Obj("Orders", "label", "enabled,visible,showing", (0, 0, 1, 1))]
    node = Obj("Sales", "label", "enabled,visible,showing", (0, 0, 1, 1), kids=leaves, fresh_kids=True)
    tree = Obj("Folders", "tree", "enabled,visible,showing", (0, 0, 100, 100), kids=[node], fresh_kids=True)
    fake.roots[HWND].kids[0].kids.append(tree)
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        assert win.find('tree text "Orders"').name == "Orders"
        held = jab.live_references
        for _ in range(3):
            assert win.exists('tree text "Invoices"')
        assert jab.live_references == held and len(fake.refs) == held


def test_unmatched_children_of_an_ordinary_container_are_released(fake):
    # A custom component that also hands out fresh objects: no match, so the old nodes are freed
    # (they used to stay pinned until close) and matching stops after a few misses.
    panel = fake.roots[HWND].kids[0]
    panel.kids[:] = _cells(300)
    panel.fresh_kids = True
    calls = []
    same = fake.isSameObject
    fake.isSameObject = lambda vm, a, b: calls.append(1) or same(vm, a, b)
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find_all("text")
        held = jab.live_references
        del calls[:]
        for _ in range(5):
            assert len(win.find_all("text")) == 300
        assert jab.live_references == held and len(fake.refs) == held
        assert len(calls) <= 5 * 9 * 9
        del panel.kids[100:]
        assert len(win.find_all("text")) == 100
        assert jab.live_references == held - 200 and len(fake.refs) == held - 200


def test_removed_child_reference_is_released_at_once(fake):
    panel = fake.roots[HWND].kids[0]
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find_all("*")
        held = jab.live_references
        del panel.kids[0]
        assert not win.exists('text "Customer:"')
        assert jab.live_references == held - 1 and len(fake.refs) == held - 1


# ---------------------------------------------------------------------- localised click action
@pytest.mark.parametrize("name", ["Klicken", "cliquer", "hacer clic"])
def test_click_uses_a_localised_single_action(fake, name):
    panel = fake.roots[HWND].kids[0]
    panel.kids[4].actions = [name]  # Save
    panel.kids[2].actions = [name]  # Rush order
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find('button "Save"').click(native=True)
        win.find('checkbox "Rush order"').click(native=True)
    assert panel.kids[4].done == [name] and panel.kids[2].done == [name]


def test_english_click_is_preferred_and_other_roles_need_an_english_name(fake):
    panel = fake.roots[HWND].kids[0]
    panel.kids[4].actions = ["Fokus", "click"]
    panel.kids.append(Obj("Country", "combo box", "enabled,visible,showing", (0, 0, 10, 10),
                          actions=["togglePopup"]))
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        win.find('button "Save"').click(native=True)
        with pytest.raises(NotSupported, match="no click action"):
            win.find('combobox "Country"').click(native=True)
    assert panel.kids[4].done == ["click"]


# ---------------------------------------------------------------------- DLL search / window / set_text limits
def test_dll_candidates_search_path(monkeypatch, tmp_path):
    (tmp_path / java.dll_name()).write_bytes(b"")
    monkeypatch.delenv("LOCUM_JAB_DLL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(java.shutil, "which", lambda name: None)
    assert str(tmp_path / java.dll_name()) in java._candidates(None)


def test_window_with_timeout_raises_window_not_found(monkeypatch, mock_backend):
    from locum import Desktop
    from locum.errors import WindowNotFound

    w = mock_backend.add_window("Order Entry", (100, 100, 640, 480), app="javaw.exe")
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    now = [0.0]

    def sleep(s):
        now[0] += s

    with bridge(desktop=Desktop(mock_backend, failsafe=False), sleep=sleep, clock=lambda: now[0]) as jab:
        with pytest.raises(WindowNotFound) as ei:
            jab.window("SAP", timeout=1.0)
        assert isinstance(ei.value.__cause__, ActionTimeout)


def test_set_text_longer_than_the_bridge_limit_is_refused_up_front(fake):
    audit = AuditLog()
    with bridge(policy=Policy(confirm_patterns=()), audit=audit) as jab:
        el = jab.from_hwnd(HWND).find('edit "Customer"')
        el.set_text("y" * (java.MAX_STRING_SIZE - 1))  # the limit itself fits
        with pytest.raises(ValueError, match="limit"):
            el.set_text("x" * java.MAX_STRING_SIZE)
    assert fake.roots[HWND].kids[0].kids[1].text == "y" * (java.MAX_STRING_SIZE - 1)
    assert len(audit.read()) == 1
