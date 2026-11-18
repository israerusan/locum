"""Polish pass 2026-11b: bridges (SAP, Java, remote), Secret scrubbing, credentials."""

from __future__ import annotations

import pickle
import sys
import threading
import time
from typing import Any

import pytest

import test_bridge_java as tj
import test_bridge_sap as ts
from locum._secret import Secret, forget_all, scrub_exception, scrubbing_errors
from locum.bridges import sap
from locum.bridges.remote import ImageSession
from locum.errors import ActionTimeout, ElementNotFound
from locum.llm.safety import AuditLog, Policy
from locum.types import Rect


@pytest.fixture(autouse=True)
def _clean_registry():
    forget_all()
    yield
    forget_all()


# ---------------------------------------------------------------------- SAP: Secret values
def test_sap_text_field_types_a_secret_not_its_mask(monkeypatch):
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    s = ts._session(eng)
    s.find("wnd[0]/usr/ctxtBKPF-BUKRS").set_text(Secret("4711"))
    assert ts.raw(eng).bukrs.Text == "4711"
    s.find("wnd[0]/usr/pwdRSYST-BCODE").text = Secret("hunter2!")
    assert ts.raw(eng).pwd.Text == "hunter2!"


def test_sap_secret_is_masked_in_the_audit_log_even_on_a_plain_field(monkeypatch):
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    audit = AuditLog()
    s = ts._session(eng, policy=Policy(), audit=audit)
    s.find("wnd[0]/usr/ctxtBKPF-BUKRS").set_text(Secret("9911"))
    assert ts.raw(eng).bukrs.Text == "9911"
    assert "9911" not in repr(audit.events)


def test_sap_grid_write_reveals_a_secret(monkeypatch):
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    s = ts._session(eng)
    s.find("wnd[0]/usr/cntlGRID/shellcont/shell").write(1, "BUTXT", Secret("s3cr3t-value"))
    assert ts.raw(eng).grid.values[(1, "BUTXT")] == "s3cr3t-value"


def test_sap_ungated_action_reads_no_policy_context(monkeypatch):
    """Without a policy, an action must not pay for the policy context (each read is a COM round trip)."""
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    s = ts._session(eng)
    called = []
    monkeypatch.setattr(sap.SapSession, "_ctx", lambda self, *a, **k: called.append(1) or {})
    s.find("wnd[0]/tbar[1]/btn[8]").press()
    s.send_vkey(0)
    assert called == []
    assert ("press",) in ts.raw(eng).post.calls


# ---------------------------------------------------------------------- SAP: frozen sessions
class _Frozen:
    """A session whose every property read blocks (a frozen SAP GUI session)."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def __getattr__(self, name: str) -> Any:
        self._release.wait(30)
        raise AttributeError(name)


def _engine_with_frozen_session(release: threading.Event) -> ts.Engine:
    eng = ts.make_engine()
    eng.Children(0)._s.append(_Frozen(release))  # con[0]/ses[1] frozen
    return eng


def test_sessions_lists_a_frozen_session_as_hung_without_hanging(monkeypatch):
    release = threading.Event()
    eng = _engine_with_frozen_session(release)
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    try:
        t0 = time.monotonic()
        infos = sap.SapGui(eng, probe_timeout=0.5).sessions()
        assert time.monotonic() - t0 < 5
        rows = {(i.connection, i.session): i for i in infos}
        assert rows[(0, 1)].hung and rows[(0, 1)].busy
        assert not rows[(0, 0)].hung and rows[(0, 0)].system == "PRD"
        assert rows[(1, 0)].system == "QAS"
    finally:
        release.set()


def test_attach_skips_a_frozen_session_and_refuses_a_pinned_one(monkeypatch):
    release = threading.Event()
    eng = _engine_with_frozen_session(release)
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    try:
        gui = sap.SapGui(eng, probe_timeout=0.5, sleep=lambda s: None)
        t0 = time.monotonic()
        s = gui.attach(system="PRD")  # ses[0]; the frozen ses[1] is never touched again
        assert s.com is ts.raw(eng, 0, 0)
        assert gui.attach(system="QAS").info["system"] == "QAS"
        with pytest.raises(ActionTimeout, match="not responding"):
            gui.attach(connection=0, session=1)
        assert time.monotonic() - t0 < 10
    finally:
        release.set()


def test_probe_disabled_keeps_the_direct_reads(monkeypatch):
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    started = []
    real = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self) or real(self))
    infos = sap.SapGui(eng, probe_timeout=None).sessions()
    assert len(infos) == 2 and started == []


# ---------------------------------------------------------------------- Java: Secret values
def test_java_set_text_accepts_a_secret(monkeypatch):
    dll = tj.FakeDll(tj.make_tree())
    monkeypatch.setattr(tj.java, "_dll_loader", lambda path=None: dll)
    audit = AuditLog()
    with tj.bridge(policy=Policy(), audit=audit) as jab:
        win = jab.from_hwnd(tj.HWND)
        win.find('edit "Customer"').set_text(Secret("cust-secret-42"))
        assert dll.roots[tj.HWND].kids[0].kids[1].text == "cust-secret-42"
        win.find("edit:not([name=Customer])").set_text(Secret("pin-9876"))
        assert dll.roots[tj.HWND].kids[0].kids[3].text == "pin-9876"
    assert "cust-secret-42" not in repr(audit.events) and "pin-9876" not in repr(audit.events)


def test_java_secret_read_back_mismatch_does_not_name_the_value(monkeypatch):
    dll = tj.FakeDll(tj.make_tree())
    monkeypatch.setattr(tj.java, "_dll_loader", lambda path=None: dll)
    real = dll.setTextContents

    def truncating(vm, ac, buf):
        real(vm, ac, buf)
        dll._obj(ac).text = dll._obj(ac).text[:4]
        return 1

    monkeypatch.setattr(dll, "setTextContents", truncating)
    with tj.bridge() as jab:
        el = jab.from_hwnd(tj.HWND).find('edit "Customer"')
        with pytest.raises(ActionTimeout) as ei:
            el.set_text(Secret("abcdefgh-secret"))
    assert "abcd" not in str(ei.value) and "abcdefgh-secret" not in str(ei.value)


# ---------------------------------------------------------------------- remote: minimized window
def test_image_session_click_uses_the_window_position_after_activation(mock_backend):
    from locum import Desktop

    d = Desktop(mock_backend, failsafe=False, sleep=lambda s: None)
    w = mock_backend.add_window("Citrix - SAP", (-32000, -32000, 160, 28), app="wfica32.exe")
    other = mock_backend.add_window("Notepad", (0, 0, 50, 50), app="notepad.exe")
    win = d.window("Citrix - SAP")
    real_activate = win.activate

    def restore(*a: Any, **k: Any) -> Any:  # activating a minimized window restores it
        mock_backend.move_window(w.handle, Rect(100, 100, 600, 400))
        return real_activate(*a, **k)

    win.activate = restore  # type: ignore[method-assign]
    assert other is not None
    s = ImageSession(win)
    assert s.click(10, 10) == (110, 110)
    assert any(e[0] == "down" and e[2:] == (110, 110) for e in mock_backend.events)


# ---------------------------------------------------------------------- Secret scrubbing
def test_scrub_exception_covers_container_args():
    Secret("Tr0ub4dor&3")
    e = ValueError({"password": "Tr0ub4dor&3"}, ["x", "Tr0ub4dor&3"])
    scrub_exception(e)
    assert "Tr0ub4dor&3" not in str(e)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="add_note() and ExceptionGroup are Python 3.11+")
def test_scrub_exception_covers_notes_and_groups():
    Secret("Tr0ub4dor&3")
    n = RuntimeError("boom")
    n.add_note("typed Tr0ub4dor&3 into the field")
    scrub_exception(n)
    assert "Tr0ub4dor&3" not in "".join(n.__notes__)
    inner = KeyError("Tr0ub4dor&3")
    g = ExceptionGroup("several", [inner])
    with pytest.raises(ExceptionGroup) as ei:
        with scrubbing_errors():
            raise g
    assert "Tr0ub4dor&3" not in str(ei.value.exceptions[0])


def test_secret_never_pickles_or_shows():
    s = Secret("p@ss-w0rd")
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(s, protocol=proto)
    assert "p@ss" not in f"{s} {s!r} {s!s} {s:>12} %s %r" % (s, s)


# ---------------------------------------------------------------------- ActionGate policy values
def test_action_gate_accepts_policy_off_and_presets():
    from locum.bridges._guard import ActionGate

    assert ActionGate(Policy.off()).policy is None  # explicit opt-out, not a TypeError
    assert ActionGate(False).policy is None
    strict = ActionGate("strict").policy
    assert isinstance(strict, Policy) and strict.preset == "strict"
    with pytest.raises(TypeError):
        ActionGate(object())


def test_table_control_refuses_a_row_the_scrollbar_does_not_reach(monkeypatch):
    eng = ts.make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    s = ts._session(eng)
    real = ts.Table._round_trip
    monkeypatch.setattr(ts.Table, "_round_trip", lambda self, position: real(self, min(position, 4)))  # clamps
    table = s.find("wnd[0]/usr/tblSAPLTABLE")
    assert table.read(5, "WRBTR") == "500.00"  # clamped to 4: row 5 is still visible, read at its real offset
    with pytest.raises(sap.BackendError, match="did not scroll to row 9"):
        table.write(9, "WRBTR", "1.00")
    assert ts.raw(eng).table.data[9] == "900.00"
