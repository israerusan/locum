"""Regression tests for the public-API / docs review findings (round 7).

* ``Element.set_text`` verifies what the field holds (read-only, truncated or
  refused input raises instead of reporting success) and never turns line
  breaks/tabs into Enter/Tab key presses;
* clicking a disabled element waits for it and then raises;
* ``press("tab tab")`` is a key sequence, duplicate keys in a combo are refused;
* keyboard shortcuts that commit (``alt+p`` on "&Pay now", ``ctrl+enter``,
  ``ctrl+s``) need confirmation under the default policy;
* bare-word selectors (``find("Save")``) get a "did you mean" hint;
* the README's RPA handler reports rejected postings as business exceptions;
* clipboard-mode typing restores every clipboard format (Windows snapshot).
"""

from __future__ import annotations

import ctypes
import pathlib
import re

import pytest

from pypilot import Desktop
from pypilot.backends.mock import MockBackend, MockNode
from pypilot.cli import DemoBackend
from pypilot.errors import ActionTimeout, ElementNotFound
from pypilot.keyboard import Keyboard
from pypilot.keys import press_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _noop(_s: float) -> None:
    return None


def demo(**kw) -> tuple:
    """The demo desktop with a virtual clock: waits take no real time."""
    be = DemoBackend()
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += max(float(s), 0.001)

    kw.setdefault("default_timeout", 0.3)
    return be, Desktop(be, sleep=sleep, clock=lambda: now[0], **kw)


def keys_sent(be, since: int) -> list:
    return [e for e in be.events[since:] if e[0] == "key_down"]


# ============================================================================ set_text
def test_set_text_refuses_a_read_only_field(monkeypatch):
    be, d = demo()
    amount = be.erp.find("Amount")
    amount.value = "500.00"
    states = MockNode.states.fget
    monkeypatch.setattr(MockNode, "states", property(
        lambda self: (states(self) - {"editable"}) | {"readonly"} if self.widget is amount else states(self)))
    set_value = MockNode.set_value
    monkeypatch.setattr(MockNode, "set_value", lambda self, t: False if self.widget is amount else set_value(self, t))
    el = d.find("edit#amount")
    n = len(be.events)
    with pytest.raises(ElementNotFound, match="read-only"):
        el.set_text("1250.00")
    with pytest.raises(ElementNotFound, match="read-only"):
        el.type("1250.00")
    assert amount.value == "500.00"
    assert not keys_sent(be, n)


def test_set_text_raises_when_the_field_truncates_the_value():
    be, d = demo()
    inv = be.erp.find("Invoice No")
    inv.on_change = lambda w: setattr(w, "value", (w.value or "")[:10])  # max length 10
    with pytest.raises(ActionTimeout) as info:
        d.find("edit#invoice_no").set_text("INV-2026-000123")
    assert "INV-2026-0" in str(info.value) and "INV-2026-000123" in str(info.value)
    inv.value = ""
    d.find("edit#invoice_no").set_text("INV-2026-000123", verify=False)  # explicit opt-out
    assert inv.value == "INV-2026-0"


def test_set_text_raises_when_the_app_ignores_typing(monkeypatch):
    be, d = demo()
    amount = be.erp.find("Amount")
    amount.value = "500.00"
    set_value = MockNode.set_value
    monkeypatch.setattr(MockNode, "set_value", lambda self, t: False if self.widget is amount else set_value(self, t))
    amount.on_change = lambda w: setattr(w, "value", "500.00")  # the control refuses the input
    with pytest.raises(ActionTimeout, match="500.00"):
        d.find("edit#amount").set_text("1250.00")


@pytest.mark.parametrize("normalise, text", [
    (lambda v: v.upper(), "acme corp"),
    (lambda v: f"{float(v.replace(',', '')):,.2f}" if v else v, "1250"),
    (lambda v: " ".join(v.split()), "ACME Corp\nAccounts Payable"),
])
def test_set_text_accepts_the_apps_normalisation_without_retyping(normalise, text):
    be, d = demo()
    vendor = be.erp.find("Vendor")
    vendor.on_change = lambda w: setattr(w, "value", normalise(w.value or ""))
    n = len(be.events)
    d.find("edit#vendor").set_text(text)
    assert vendor.value == normalise(text)
    assert not keys_sent(be, n)  # no retype: no Enter for the line break, nothing typed twice


def test_set_text_never_turns_newlines_or_tabs_into_key_presses():
    be, d = demo()
    n = len(be.events)
    d.find("edit#vendor").set_text("ACME\tCorp", native=False)
    assert be.erp.find("Vendor").value == "ACME\tCorp"  # pasted literally
    assert be.erp.find("Invoice No").value == ""  # nothing tabbed into the next field
    assert ("key_down", "tab") not in be.events[n:]
    assert be.clipboard == ""  # the user's clipboard is restored


def test_set_text_with_a_single_line_field_dropping_the_line_break_raises():
    be, d = demo()
    vendor = be.erp.find("Vendor")
    vendor.on_change = lambda w: setattr(w, "value", (w.value or "").split("\n")[0])  # keeps only line 1
    n = len(be.events)
    with pytest.raises(ActionTimeout, match="Accounts Payable"):
        d.find("edit#vendor").set_text("ACME Corp\nAccounts Payable")
    assert ("key_down", "enter") not in be.events[n:]


# =============================================================================== click
def test_click_on_a_disabled_button_raises_instead_of_reporting_success():
    from pypilot.rpa import run_flow

    be, d = demo()
    save = be.erp.find("Save")
    save.enabled = False
    with pytest.raises(ElementNotFound, match="disabled"):
        d.find('button "Save"').click()
    with pytest.raises(ElementNotFound, match="disabled"):
        d.click('button "Save"')
    with pytest.raises(ElementNotFound, match="disabled"):
        d.find('button "Save"').invoke()
    report = run_flow({"steps": [{"click": 'button "Save"'}]}, d)
    assert not report.ok
    assert save.clicks == 0


def test_click_waits_for_a_button_to_become_enabled():
    be = DemoBackend()
    save = be.erp.find("Save")
    save.enabled = False
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 2:
            save.enabled = True

    d = Desktop(be, sleep=sleep, default_timeout=5)
    d.find('button "Save"').click()
    assert save.clicks == 1


# =============================================================================== press
def test_press_space_separated_keys_is_a_sequence():
    be, d = demo()
    d.find("edit#vendor").click()
    n = len(be.events)
    d.press("tab tab")
    assert [e for e in be.events[n:] if e[0].startswith("key")] == [
        ("key_down", "tab"), ("key_up", "tab"), ("key_down", "tab"), ("key_up", "tab")]
    assert d.focused().name == "Amount"
    assert press_actions("tab tab enter") == [["tab"], ["tab"], ["enter"]]
    assert press_actions("ctrl alt del") == [["ctrl", "alt", "delete"]]
    assert press_actions("page down") == [["pagedown"]]


@pytest.mark.parametrize("call", [
    lambda d: d.hotkey("tab", "tab"),
    lambda d: d.hotkey("tab+tab"),
    lambda d: d.press("ctrl+c+c"),
    lambda d: d.press("shift tab tab"),
])
def test_ambiguous_or_duplicate_key_combos_are_refused(call):
    be, d = demo()
    n = len(be.events)
    with pytest.raises(ValueError):
        call(d)
    assert not keys_sent(be, n)


def test_flow_press_step_sends_every_key():
    from pypilot.rpa import run_flow

    be, d = demo()
    d.find("edit#vendor").click()
    assert run_flow({"steps": [{"press": "tab tab"}]}, d).ok
    assert d.focused().name == "Amount"


def test_toolkit_press_keys_splits_key_sequences():
    from pypilot.llm import DesktopToolkit, Policy

    be, d = demo()
    d.find("edit#vendor").click()
    kit = DesktopToolkit(d, policy=Policy.permissive())
    r = kit.call("press_keys", {"keys": ["tab tab"]})
    assert not r.is_error, r.text
    assert d.focused().name == "Amount"


# ============================================================ policy: committing shortcuts
def _pay_now_kit(name="Pay now", access_key="Alt+P"):
    from pypilot.llm import DesktopToolkit, Policy

    be, d = demo()
    pay = be.erp.find("Submit Payment")
    pay.name = name
    if access_key:
        pay.attrs["access_key"] = access_key
    asked = []
    kit = DesktopToolkit(d, policy=Policy(confirm=lambda desc, det: asked.append(desc) or False))
    d.find("edit#amount").click()
    return be, d, kit, asked


@pytest.mark.parametrize("name, access_key", [("Pay now", "Alt+P"), ("&Pay now", None)])
def test_alt_mnemonic_of_a_pay_button_needs_confirmation(name, access_key):
    be, d, kit, asked = _pay_now_kit(name, access_key)
    r = kit.call("hotkey", {"combo": "alt+p"})
    assert r.is_error and "declined" in r.text
    assert asked and "Pay now" in asked[0]
    r = kit.call("press_keys", {"keys": ["alt+p"]})
    assert r.is_error


@pytest.mark.parametrize("combo", ["ctrl+enter", "ctrl+s", "Ctrl+S", "ctrl+KP_Enter"])
def test_committing_shortcuts_need_confirmation(combo):
    be, d, kit, asked = _pay_now_kit()
    r = kit.call("hotkey", {"combo": combo})
    assert r.is_error and "declined" in r.text, r.text
    assert len(asked) == 1


def test_harmless_shortcuts_and_permissive_policy_are_not_gated():
    from pypilot.llm import DesktopToolkit, Policy

    be, d, kit, asked = _pay_now_kit()
    assert not kit.call("hotkey", {"combo": "ctrl+c"}).is_error
    assert not kit.call("press_keys", {"keys": ["tab"]}).is_error
    assert asked == []
    assert Policy.permissive().confirm_keys == ()
    free = DesktopToolkit(d, policy=Policy(confirm_keys=(), confirm=lambda *a: False))
    assert not free.call("hotkey", {"combo": "ctrl+s"}).is_error


def test_computer_use_key_action_is_judged_by_the_shortcut_target():
    from pypilot.llm import ComputerUseAdapter, Policy

    be, d, kit, asked = _pay_now_kit()
    cu = ComputerUseAdapter(d, policy=Policy())
    for text in ("alt+p", "ctrl+Return"):
        r = cu.execute("key", {"text": text})
        assert r.is_error and "confirmation" in r.text, text
    assert not cu.execute("key", {"text": "Tab"}).is_error


# ======================================================================= selector hints
def test_bare_word_selector_suggests_the_named_element():
    be, d = demo()
    with pytest.raises(ElementNotFound) as info:
        d.find("Save")
    msg = str(info.value)
    assert "'Save'" in msg  # as typed, not lower-cased
    assert 'button "Save"' in msg and "quotes" in msg


# ========================================================================= README RPA
def test_readme_rpa_handler_reports_rejected_postings_as_business_exceptions(tmp_path, monkeypatch):
    from pypilot.rpa import BusinessException

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = next(c for c in re.findall(r"```python\n(.*?)```", text, re.S) if "def enter_invoice" in c)
    monkeypatch.chdir(tmp_path)
    be, d = demo()
    ns = {"d": d}
    exec(compile(block, "README.md[rpa]", "exec"), ns)
    assert [i["invoice_no"] for i in be.invoices] == ["INV-43"]
    with pytest.raises(BusinessException, match="already exists"):
        ns["enter_invoice"]({"vendor": "Initech", "invoice_no": "INV-43", "amount": "75.50"})
    with pytest.raises(BusinessException, match="required"):
        ns["enter_invoice"]({"vendor": "", "invoice_no": "INV-45", "amount": "10"})
    assert ns["enter_invoice"]({"vendor": "Hooli", "invoice_no": "INV-46", "amount": "10"}) == {
        "status": "Saved invoice INV-46"}


# ==================================================================== clipboard formats
class _RecordingClipboard(MockBackend):
    def __init__(self):
        super().__init__()
        self.saved_calls = 0
        self.restored = []

    def save_clipboard(self):
        self.saved_calls += 1
        return ("snapshot", self.clipboard)

    def restore_clipboard(self, saved):
        self.restored.append(saved)
        self.clipboard = saved[1]


def test_clipboard_typing_restores_the_backend_snapshot():
    be = _RecordingClipboard()
    be.clipboard = "user data"
    kb = Keyboard(be, sleep=_noop)
    kb.type("héllo", mode="clipboard")
    assert be.saved_calls == 1 and be.restored == [("snapshot", "user data")]
    assert be.clipboard == "user data"


def test_windows_clipboard_snapshot_keeps_every_format():
    from test_windows_backend import FakeApi

    from pypilot.backends import windows as winmod

    class MultiFormatApi(FakeApi):
        """A clipboard holding several formats (an image / Excel range), like the real one."""

        def __init__(self):
            super().__init__()
            self.formats = {}

        def EnumClipboardFormats(self, fmt):
            order = list(self.formats)
            if fmt == 0:
                return order[0] if order else 0
            i = order.index(fmt) + 1
            return order[i] if i < len(order) else 0

        def IsClipboardFormatAvailable(self, fmt):
            return int(fmt in self.formats)

        def GetClipboardData(self, fmt):
            assert self.clip_open
            return self._alloc(self.formats[fmt]) if fmt in self.formats else None

        def SetClipboardData(self, fmt, h):
            assert self.clip_open
            self.formats[fmt] = ctypes.string_at(h, len(self.mem[h]))
            return h

        def EmptyClipboard(self):
            assert self.clip_open
            self.formats.clear()
            return 1

    api = MultiFormatApi()
    be = winmod.WindowsBackend(_api=api, dpi_aware=False, sleep=_noop)
    dib = b"BM-dib-bytes"
    api.formats = {8: dib, 0xC123: b"<Biff12 range>", 2: b"hbitmap"}  # CF_DIB, Excel, CF_BITMAP (a handle)
    snap = be.save_clipboard()
    assert [f for f, _ in snap.items] == [8, 0xC123]
    kb = Keyboard(be, sleep=_noop)
    saved = kb._save_clipboard()
    be.set_clipboard("typed")
    kb._restore_clipboard(saved)
    assert api.formats == {8: dib, 0xC123: b"<Biff12 range>"}
