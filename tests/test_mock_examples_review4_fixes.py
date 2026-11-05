"""Regression tests: MockBackend realism, Backend.element_at Z-order, Windows launch,
and the safety of the SAP-style example robot."""

from __future__ import annotations

import ctypes
import importlib.util
import pathlib
from decimal import Decimal

import pytest

from locum import Desktop
from locum.backends.base import Backend
from locum.backends.mock import MockBackend, MockNode

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _desk(be, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("default_timeout", 0.2)
    return Desktop(be, **kw)


# ======================================================================= mock: keyboard focus


def test_keyboard_focus_follows_the_activated_window():
    be = MockBackend()
    a = be.add_window("Excel", (0, 0, 400, 300))
    cell = a.add_edit("A1", (10, 40, 200, 28))
    b = be.add_window("SAP", (500, 0, 400, 300))
    amount = b.add_edit("Amount", (10, 40, 200, 28))
    d = _desk(be)
    d.find('edit "A1"').click()
    d.window("SAP").activate()
    d.type("999")
    assert cell.value == "" and amount.value == ""  # SAP has no focused field: keys go nowhere
    assert be.focused_widget is None
    d.find('edit "Amount"').click()
    d.window("Excel").activate()  # back to Excel: its last focused cell gets the keys again
    d.type("1")
    assert cell.value == "1" and amount.value == ""
    d.window("SAP").activate()
    d.type("2")
    assert amount.value == "2"


def test_minimized_or_closed_window_never_receives_keystrokes():
    be = MockBackend()
    a = be.add_window("Excel", (0, 0, 400, 300))
    cell = a.add_edit("A1", (10, 40, 200, 28))
    b = be.add_window("SAP", (500, 0, 400, 300))
    amount = b.add_edit("Amount", (10, 40, 200, 28))
    d = _desk(be)
    d.find('edit "Amount"').click()
    d.find('edit "A1"').click()
    be.set_window_state(a.handle, "minimized")
    assert be._active is b and be.focused_widget is amount  # the next window takes over
    d.type("5")
    assert cell.value == "" and amount.value == "5"
    be.close_window(b.handle)
    assert be.focused_widget is None
    d.type("7")
    assert cell.value == "" and amount.value == "5"


def test_new_window_takes_the_keyboard_focus():
    be = MockBackend()
    a = be.add_window("Excel", (0, 0, 400, 300))
    cell = a.add_edit("A1", (10, 40, 200, 28))
    d = _desk(be)
    d.find('edit "A1"').click()
    be.add_window("Popup", (500, 0, 200, 100))
    d.type("x")
    assert cell.value == ""


# ======================================================================= mock: hidden containers


def test_children_of_an_invisible_container_are_dead():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    first = w.add_edit("First", (10, 40, 200, 28))
    grp = w.add("group", "Hidden panel", (10, 150, 300, 100), visible=False)
    fired = []
    post = grp.add_button("Post", (20, 160, 80, 30), on_click=lambda wd: fired.append(1))
    d = _desk(be)
    assert not d.exists('button "Post"')
    d.click(post.screen_rect.center)
    assert fired == [] and post.clicks == 0
    assert be.widget_at(*post.screen_rect.center) is w.root
    assert be.screenshot().getpixel(tuple(post.screen_rect.center)) == w.fill
    assert MockNode(be, post).bounds is None and "invisible" in MockNode(be, post).states
    assert MockNode(be, post).focus() is False
    d.find('edit "First"').click()
    d.press("tab")
    assert be.focused_widget is first  # Tab skips the hidden button


# ======================================================================= mock: keys


def test_alt_accelerator_does_not_type():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    e = w.add_edit("Amount", (10, 40, 200, 28), value="100")
    d = _desk(be)
    d.find("edit").click()
    d.hotkey("alt", "s")
    assert e.value == "100"


def test_select_all_is_cancelled_by_caret_moves_focus_changes_and_clicks():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    e = w.add_edit("Amount", (10, 40, 200, 28), value="1250.00")
    w.add_edit("Other", (10, 80, 200, 28))
    d = _desk(be)
    d.find('edit "Amount"').click()
    d.hotkey("ctrl", "a")
    d.press("end")
    d.type("5")
    assert e.value == "1250.005"
    d.hotkey("ctrl", "a")
    d.press("tab")
    d.find('edit "Amount"').click()
    d.type("9")
    assert e.value == "1250.0059"
    d.hotkey("ctrl", "a")
    d.find('edit "Amount"').click()
    d.type("1")
    assert e.value == "1250.00591"
    d.hotkey("ctrl", "a")
    d.type("42")  # a real selection still gets replaced
    assert e.value == "42"


# ======================================================================= mock: misc


def test_toggle_on_a_disabled_checkbox_reports_failure():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    cb = w.add_checkbox("Approve", (10, 80, 150, 26), enabled=False)
    assert MockNode(be, cb).toggle() is False and cb.checked is False
    assert MockNode(be, cb).focus() is False


def test_maximize_twice_then_restore_returns_to_the_original_rect():
    be = MockBackend()
    w = be.add_window("Form", (100, 100, 400, 300))
    for state in ("maximized", "maximized", "normal"):
        be.set_window_state(w.handle, state)
    assert w.rect.as_tuple() == (100, 100, 400, 300)


def test_screenshot_with_a_tiny_checkbox_and_a_tuple_region():
    be = MockBackend()
    w = be.add_window("W", (0, 0, 300, 200))
    w.add_checkbox("tiny", (10, 40, 100, 6))
    w.add_checkbox("narrow", (10, 60, 3, 30))
    assert be.screenshot().size == (1920, 1080)
    assert be.screenshot((0, 0, 10, 10)).size == (10, 10)


# ======================================================================= base: element_at Z-order


class _PlainBackend(MockBackend):
    """A third-party-style backend that relies on the default element_at."""

    element_at = Backend.element_at


def test_default_element_at_respects_z_order():
    be = _PlainBackend()
    bank = be.add_window("Bank", (100, 100, 500, 400))
    bank.add_button("Approve payment", (100, 100, 90, 30))
    excel = be.add_window("Excel", (150, 150, 400, 200))
    excel.add_edit("Cell A1", (0, 0, 400, 200))
    node = be.element_at(250, 215)
    assert node is not None and node.name == "Cell A1"
    node = be.element_at(120, 120)  # only the bank window is there
    assert node is not None and node.name == "Bank"
    assert be.element_at(1500, 900) is None


# ======================================================================= windows: launch


def test_shell_launch_target_classification(tmp_path, monkeypatch):
    from locum.backends import windows as winmod

    monkeypatch.setattr(winmod.shutil, "which", lambda name: "/usr/bin/notepad" if name == "notepad" else None)
    doc = tmp_path / "Q3 report.xlsx"
    doc.write_text("x")
    exe = tmp_path / "erp.exe"
    exe.write_text("x")
    assert winmod.shell_launch_target("excel") == ("excel", "")              # App Paths, not on PATH
    assert winmod.shell_launch_target("excel /e") == ("excel", "/e")
    assert winmod.shell_launch_target(str(doc)) == (str(doc), "")           # a document, spaces and all
    assert winmod.shell_launch_target(f'"{doc}"') == (str(doc), "")
    assert winmod.shell_launch_target("https://bank.example") == ("https://bank.example", "")
    assert winmod.shell_launch_target("notepad x.txt") is None              # on PATH: Popen
    assert winmod.shell_launch_target(str(exe)) is None                     # an explicit program
    assert winmod.shell_launch_target(f'"{exe}" --flag') is None
    assert winmod.shell_launch_target("  ") is None


def test_windows_launch_uses_shellexecute_for_app_paths(monkeypatch):
    from locum.backends import windows as winmod

    calls = {}

    class Api:
        def ShellExecuteExW(self, ref):
            info = ctypes.cast(ref, ctypes.POINTER(winmod.SHELLEXECUTEINFOW)).contents
            calls["file"], calls["params"], calls["mask"] = info.lpFile, info.lpParameters, info.fMask
            info.hProcess = 1234
            return 1

        def GetProcessId(self, h):
            return 4321 if h == 1234 else 0

        def WaitForSingleObject(self, h, ms):
            return 0x102  # WAIT_TIMEOUT: still running

        def CloseHandle(self, h):
            calls["closed"] = h
            return 1

    monkeypatch.setattr(winmod.shutil, "which", lambda name: None)
    be = object.__new__(winmod.WindowsBackend)
    be._api = Api()
    proc = be.launch("excel /e")
    assert calls["file"] == "excel" and calls["params"] == "/e"
    assert calls["mask"] & winmod.SEE_MASK_NOCLOSEPROCESS
    assert proc.pid == 4321 and proc.poll() is None
    # Argument lists and Popen options still go to subprocess.Popen.
    seen = []
    monkeypatch.setattr(Backend, "launch", lambda self, command, **kw: seen.append((command, kw)) or "popen")
    assert be.launch(["excel", "/e"]) == "popen"
    assert be.launch("excel", env={}) == "popen"
    assert seen == [(["excel", "/e"], {}), ("excel", {"env": {}})]


def test_shell_process_without_a_process_handle_reports_exited():
    from locum.backends.windows import ShellProcess

    proc = ShellProcess(object(), "Q3.xlsx", None)  # handed over to the running Excel
    assert proc.pid is None and proc.poll() == 0


def test_shellexecuteinfo_layout_matches_win64():
    from locum.backends.windows import SHELLEXECUTEINFOW

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(SHELLEXECUTEINFOW) == 112
        assert SHELLEXECUTEINFOW.hProcess.offset == 104
    else:  # pragma: no cover - 32-bit interpreters
        assert ctypes.sizeof(SHELLEXECUTEINFOW) == 60


# ======================================================================= the SAP-style example


@pytest.fixture
def sap(monkeypatch):
    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    spec = importlib.util.spec_from_file_location("sap_example_under_test", EXAMPLES / "sap_style_data_entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _csv(tmp_path, body):
    p = tmp_path / "in.csv"
    p.write_text("invoice_no,vendor,amount,due_date,urgent\n" + body, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("raw", ["1.250,00", "1,25", "1,250", "NaN", "nan", "inf", "-inf", "1e3", "12.345",
                                 "0", "-15.00", "", "1 250.00", "abc"])
def test_sap_parse_amount_rejects_ambiguous_or_invalid(sap, raw):
    from locum.rpa import BusinessException

    with pytest.raises(BusinessException):
        sap.parse_amount(raw)


def test_sap_parse_amount_accepts_plain_and_decimal_comma(sap):
    assert sap.parse_amount("1250.00") == Decimal("1250.00")
    assert sap.parse_amount(" 1,250.5 ") == Decimal("1250.50")
    assert sap.parse_amount("12,345,678.90") == Decimal("12345678.90")
    assert sap.parse_amount("1.250,00", decimal_comma=True) == Decimal("1250.00")
    assert sap.parse_amount("1,25", decimal_comma=True) == Decimal("1.25")


def test_sap_example_never_reposts_after_save(sap, tmp_path, monkeypatch, capsys):
    from locum.cli import DemoBackend

    orig = DemoBackend._set_status
    swallowed = []

    def slow_status(self, text):  # the ERP saves, but its status bar answers too late
        if text.startswith("Saved") and not swallowed:
            swallowed.append(text)
            return
        orig(self, text)

    monkeypatch.setattr(DemoBackend, "_set_status", slow_status)
    created = []

    def make_desktop():  # the demo desktop on a simulated clock (no real 10 s wait)
        now = [0.0]

        def sleep(s):
            now[0] += max(float(s), 0.01)

        be = DemoBackend()
        created.append(be)
        return Desktop(be, sleep=sleep, clock=lambda: now[0], default_timeout=2)

    monkeypatch.setattr(sap, "make_desktop", make_desktop)
    csv_path = _csv(tmp_path, "INV-9,ACME Corp,1250.00,2026-12-01,no\n")
    assert sap.main(["--csv", csv_path, "--queue", str(tmp_path / "q.json"), "--run-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert [inv["invoice_no"] for inv in created[0].invoices] == ["INV-9"]
    assert "needs review" in out and "already exists" not in out


def test_sap_example_reports_conflicting_duplicates(sap, tmp_path, capsys):
    csv_path = _csv(tmp_path, (
        "1001,ACME Corp,1250.00,2026-12-01,no\n"
        "1001,Globex,880.40,2026-12-05,yes\n"       # same number, other vendor: a different invoice
        "1002,Globex,1.00,,no\n"
        "1002,Globex,2.00,,no\n"                    # conflicting duplicate: neither is entered
        "1003,Initech,5.00,,no\n"
        "1003,Initech,5.00,,no\n"                   # the same row twice: entered once
        ",Initech,9.00,,no\n"))
    assert sap.main(["--csv", csv_path, "--queue", str(tmp_path / "q.json"), "--run-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "invoices stored by the demo ERP: ['1001', '1001', '1003']" in out
    assert out.count("appears 2 times in the CSV with different data") == 2
    assert "vendor and invoice_no are required" in out
    assert "success 3, business exceptions 3, failed 0" in out


def test_sap_example_rejects_locale_amounts_unless_told(sap, tmp_path, capsys):
    csv_path = _csv(tmp_path, 'INV-1,Siemens AG,"1.250,00",,no\nINV-2,Bosch GmbH,"1,25",,no\nINV-3,Initech,NaN,,no\n')
    args = ["--csv", csv_path, "--run-dir", str(tmp_path / "runs")]
    assert sap.main(args + ["--queue", str(tmp_path / "q1.json")]) == 0
    assert "invoices stored by the demo ERP: []" in capsys.readouterr().out
    assert sap.main(args + ["--queue", str(tmp_path / "q2.json"), "--decimal-comma"]) == 0
    out = capsys.readouterr().out
    assert "invoices stored by the demo ERP: ['INV-1', 'INV-2']" in out
    assert "Saved invoice INV-1" in out and "is not a plain amount" in out


def test_excel_example_flow_never_launches_twice():
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load((EXAMPLES / "excel_report_flow.yaml").read_text(encoding="utf-8"))
    launch = flow["steps"][0]["if_exists"]["else"][0]
    assert "launch" in launch and launch.get("retries") == 0
