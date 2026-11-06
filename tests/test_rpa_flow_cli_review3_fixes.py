"""Regression tests for the third review round of flows, the recorder and the CLI."""

from __future__ import annotations

import io
import logging
import sys
import threading
import types

import pytest

from locum.cli import EXIT_FAILURE, EXIT_OK, EXIT_UNAVAILABLE, DemoBackend, main
from locum.desktop import Desktop
from locum.errors import FailSafeTriggered
from locum.rpa import Recorder, run_flow
from locum.rpa.flow import load_flow

pytest.importorskip("yaml")


def _noop(_s: float) -> None:
    return None


@pytest.fixture
def be() -> DemoBackend:
    return DemoBackend()


@pytest.fixture
def desk(be: DemoBackend) -> Desktop:
    return Desktop(be, sleep=_noop, default_timeout=0.1, poll_interval=0.01, failsafe=False)


def cli(argv, desktop=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


def unpaid(be, no="X"):
    be.invoices.append({"vendor": "A", "invoice_no": no, "amount": 1.0, "due_date": "", "urgent": False, "paid": False})


# ===================================================================== flow


def test_user_variable_named_env_is_not_the_process_environment(desk, monkeypatch):
    monkeypatch.setenv("SAP_PASSWORD", "hunter2")
    flow = {"vars": {"env": "QA"}, "steps": [
        {"set_text": {"target": "edit#vendor", "text": "${env}"}},
        {"read_value": {"target": "edit#vendor", "var": "v"}},
        {"log": "system=${env}"},
    ]}
    r = run_flow(flow, desk)
    assert r.ok and r.variables["v"] == "QA"
    r = run_flow(flow, desk, {"env": "PROD"})
    assert r.variables["v"] == "PROD"
    assert all("hunter2" not in log["message"] for log in r.logs)
    # Without such a variable, a bare ${env} is an error (never the whole environment) ...
    r = run_flow({"steps": [{"log": "${env}"}]}, desk)
    assert not r.ok and "hunter2" not in str(r.to_dict())
    # ... while ${env.NAME} still reads one environment variable.
    r = run_flow({"steps": [{"set_var": {"p": "${env.SAP_PASSWORD}"}}]}, desk)
    assert r.variables["p"] == "hunter2"


def test_settings_retries_do_not_rerun_finished_loop_iterations(be, desk):
    flow = {
        "settings": {"retries": 1, "retry_delay": 0, "timeout": 0.1},
        "steps": [{"for_each": {"items": [
            {"vendor": "ACME", "no": "INV-1", "amt": "100", "after": 'button "Clear"'},
            {"vendor": "Globex", "no": "INV-2", "amt": "250", "after": 'button "Print remittance"'}],
            "as": "inv",
            "steps": [
                {"set_text": {"target": "edit#vendor", "text": "${inv.vendor}"}},
                {"set_text": {"target": "edit#invoice_no", "text": "${inv.no}"}},
                {"set_text": {"target": "edit#amount", "text": "${inv.amt}"}},
                {"click": 'button "Save"'},
                {"click": 'button "Submit Payment"'},
                {"click": "${inv.after}"},
            ]}}]}
    r = run_flow(flow, desk)
    assert not r.ok
    assert be.payments == [{"invoices": ["INV-1"], "total": 100.0}, {"invoices": ["INV-2"], "total": 250.0}]
    pays = [s for s in r.steps if s.name == 'click button "Submit Payment"']
    assert len(pays) == 2
    failing = [s for s in r.steps if s.name == "click ${inv.after}" and s.failed]
    assert failing[-1].attempts == 2  # the failing leaf step itself is still retried
    assert r.steps[0].attempts == 1  # ... but not the loop


def test_explicit_retries_on_a_block_still_rerun_it(desk):
    flow = {"steps": [{"retries": 1, "retry_delay": 0, "try": {
        "steps": [{"log": "attempt"}, {"click": 'button "Nope"', "timeout": 0}],
        "finally": [{"log": "f"}]}}]}
    r = run_flow(flow, desk)
    calls = [log for log in r.logs if log["message"] == "attempt"]
    assert len(calls) == 2 and r.steps[0].attempts == 2


def test_csv_rows_with_wrong_field_count_are_errors(tmp_path, desk):
    (tmp_path / "inv.csv").write_text("vendor,invoice,amount\nACME,INV-1,1,234.56\n", encoding="utf-8")
    (tmp_path / "short.csv").write_text("vendor,invoice,amount\nACME,INV-1,5\nGlobex,INV-2\n", encoding="utf-8")
    body = [{"log": "${row.vendor} ${row.invoice} [${row.amount}]"}]
    r = run_flow({"steps": [{"for_each": {"csv": "inv.csv", "as": "row", "steps": body}}]}, desk, base_dir=tmp_path)
    assert not r.ok and "inv.csv:2" in r.error and "got 4" in r.error
    assert not [log for log in r.logs if "[1]" in log["message"]]
    r = run_flow({"steps": [{"for_each": {"csv": "short.csv", "as": "row", "steps": body}}]}, desk, base_dir=tmp_path)
    assert not r.ok and "short.csv:3" in r.error and "got 2" in r.error
    assert not [log for log in r.logs if "INV-1" in log["message"]]  # rejected before any iteration ran
    r = run_flow({"steps": [{"for_each": {"csv": "short.csv", "as": "row", "strict": False, "steps": body}}]},
                 desk, base_dir=tmp_path)
    assert r.ok and [log["message"] for log in r.logs][:2] == ["ACME INV-1 [5]", "Globex INV-2 []"]


@pytest.mark.parametrize("abort", [KeyboardInterrupt, FailSafeTriggered])
def test_abort_in_catch_does_not_run_finally(be, abort):
    class D(Desktop):
        def sleep(self, s):
            if s == 0.123:
                raise abort("abort")
            return super().sleep(s)

    d = D(be, default_timeout=0.1, failsafe=False)
    unpaid(be)
    flow = {"steps": [{"try": {
        "steps": [{"click": 'button "Nope"', "timeout": 0}],
        "catch": [{"sleep": 0.123}],
        "finally": [{"click": 'button "Submit Payment"'}]}}]}
    try:
        r = run_flow(flow, d)
    except KeyboardInterrupt:
        assert abort is KeyboardInterrupt
    else:
        assert abort is FailSafeTriggered and r.aborted
    assert be.payments == []


def test_ordinary_error_in_catch_still_runs_finally(be, desk):
    flow = {"steps": [{"try": {
        "steps": [{"click": 'button "Nope"', "timeout": 0}],
        "catch": [{"fail": "still broken"}],
        "finally": [{"log": "cleanup"}]}}]}
    r = run_flow(flow, desk)
    assert not r.ok and "still broken" in r.error
    assert any(log["message"] == "cleanup" for log in r.logs)


def test_yaml_scalars_are_kept_as_written(tmp_path, desk):
    f = tmp_path / "y.yaml"
    f.write_text(
        "vars:\n  invoice_no: 00123\n  amount: 1250.50\n  big: 12345678901234567.89\n"
        "  country: NO\n  time: 12:30\n  n: 2\n  rate: 0.5\n"
        "steps:\n"
        "  - set_text: {target: \"edit#invoice_no\", text: \"${invoice_no}\"}\n"
        "  - read_value: {target: \"edit#invoice_no\", var: inv}\n"
        "  - set_text: {target: \"edit#amount\", text: \"${amount}\"}\n"
        "  - read_value: {target: \"edit#amount\", var: amt}\n"
        "  - log: \"${big} ${country} ${time}\"\n"
        "  - repeat: {times: \"${n}\", steps: [{log: x}]}\n"
        "  - click: [10, 20]\n",
        encoding="utf-8",
    )
    r = run_flow(f, desk)
    assert r.ok, r.error
    assert r.variables["inv"] == "00123" and r.variables["amt"] == "1250.50"
    assert r.logs[0]["message"] == "12345678901234567.89 NO 12:30"
    assert r.variables["amount"] == 1250.5 and r.variables["n"] == 2 and r.variables["rate"] == 0.5
    doc, _ = load_flow('{"vars": {"amount": 10.50}, "steps": []}')
    assert str(doc["vars"]["amount"]) == "10.50"


def test_settings_screenshot_on_error_false_is_honoured(tmp_path, desk):
    flow = {"settings": {"screenshot_on_error": False, "timeout": 0}, "steps": [{"click": "button#nope"}]}
    r = run_flow(flow, desk, run_dir=tmp_path)
    assert not r.ok
    assert not list(tmp_path.rglob("*.png"))
    flow["settings"]["screenshot_on_error"] = True
    run_flow(flow, desk, run_dir=tmp_path / "b")
    assert list((tmp_path / "b").rglob("*.png"))


def test_continue_on_error_on_a_block_handles_its_nested_failures(desk):
    r = run_flow({"steps": [
        {"call": {"flow": [{"click": 'button "Nope"', "timeout": 0}]}, "continue_on_error": True},
        {"log": "after"}]}, desk)
    assert r.ok
    assert [s.path for s in r.steps if s.failed and s.handled] == ["steps[0]", "steps[0].call.steps[0]"]
    assert cli_exit_for(r) == EXIT_OK


def cli_exit_for(report):
    return EXIT_OK if report.ok else EXIT_FAILURE


def test_activate_window_with_empty_title_fails(desk):
    r = run_flow({"vars": {"title": ""}, "steps": [{"activate_window": "${title}"}]}, desk)
    assert not r.ok and "non-empty" in r.error
    r = run_flow({"vars": {"t": ""}, "steps": [{"activate_window": {"title": "${t}", "app": "erp"}}]}, desk)
    assert r.ok, r.error


def test_boolean_arguments_from_strings(desk):
    r = run_flow({"vars": {"clr": "false"}, "steps": [
        {"set_text": {"target": "edit#vendor", "text": "ACME"}},
        {"set_text": {"target": "edit#vendor", "text": "-X", "clear": "${clr}"}},
        {"read_value": {"target": "edit#vendor", "var": "v"}}]}, desk)
    assert r.ok and r.variables["v"] == "ACME-X"
    r = run_flow({"steps": [{"set_text": {"target": "edit#vendor", "text": "x", "clear": "maybe"}}]}, desk)
    assert not r.ok and "true or false" in r.error


def test_flow_file_name_starting_with_a_bracket(tmp_path, monkeypatch, desk):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "[Q4] close.yaml").write_text("steps:\n  - log: hi\n", encoding="utf-8")
    code, out, err = cli(["run", "[Q4] close.yaml"], desktop=desk)
    assert code == EXIT_OK, err
    # inline text still works
    doc, path = load_flow("[{log: hi}]")
    assert path is None and doc == {"steps": [{"log": "hi"}]}


# ===================================================================== CLI


def test_record_checks_yaml_before_recording(tmp_path, monkeypatch, desk):
    monkeypatch.setitem(sys.modules, "yaml", None)
    slept = []
    code, _out, err = cli(["record", "-o", str(tmp_path / "r.yaml"), "--duration", "0.2"], desktop=desk,
                          sleep=slept.append)
    assert code == EXIT_UNAVAILABLE and "PyYAML" in err
    assert slept == []  # failed before the recording session


def test_record_save_failure_keeps_the_recording(tmp_path, monkeypatch, desk):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "afile").write_text("", encoding="utf-8")
    code, _out, err = cli(["record", "-o", "afile/x.py", "--duration", "0"], desktop=desk)
    assert code == EXIT_FAILURE
    rescued = list(tmp_path.glob("x-rescued-*.json"))
    assert rescued and str(rescued[0]) in err


def test_argparse_output_goes_to_the_injected_streams(capsys):
    code, out, err = cli(["--version"])
    assert code == EXIT_OK and out.startswith("locum ")
    code, out, err = cli(["clik", "x"])
    assert code == 2 and "invalid choice" in err
    code, out, _ = cli(["run", "--help"])
    assert code == EXIT_OK and "--report-dir" in out
    real = capsys.readouterr()
    assert real.out == "" and real.err == ""


def test_negative_coordinates(be, desk):
    from locum.types import Monitor, Rect

    # A second monitor left of the primary one: negative x is on screen.
    be._monitors.append(Monitor(1, Rect(-1920, 0, 1920, 1080), 1.0, False, "MOCK1"))
    code, out, err = cli(["click", "-5,10"], desktop=desk)
    assert code == EXIT_OK, err
    assert "(-5, 10)" in out
    code, out, err = cli(["move", "-1200,300"], desktop=desk)
    assert code == EXIT_OK and "(-1200, 300)" in out
    code, out, err = cli(["scroll", "-3", "--at", "-5,10"], desktop=desk)
    assert code == EXIT_OK, err
    code, out, err = cli(["click", "-5,10", "--offset", "-2,-3"], desktop=desk)
    assert code == EXIT_OK and "(-7, 7)" in out
    code, _out, err = cli(["click", "--bogus"], desktop=desk)
    assert code == 2
    code, _out, err = cli(["click", "-5,2000"], desktop=desk)  # on no monitor: refused, not clamped
    assert code == 2 and "off-screen" in err


# ===================================================================== recorder


class K:
    """A stand-in for pynput's ``Key``/``KeyCode``."""

    def __init__(self, char=None, name=None, vk=None):
        self.char, self.name, self.vk = char, name, vk

    def __repr__(self):
        return f"K({self.char!r}, {self.name!r}, {self.vk!r})"


SHIFT, CTRL, ALTGR = K(name="shift"), K(name="ctrl_l"), K(name="alt_gr")


def _pynput_run(seq):
    r = Recorder(use_selectors=False).start(source="manual")
    for kind, key in seq:
        ev = r._pynput_event(kind, key, 0.0)
        if ev is not None:
            r.feed(ev)
    return [a.to_flow_step() for a in r.stop()]


def down(k):
    return ("key_down", k)


def up(k):
    return ("key_up", k)


def test_windows_altgr_characters_are_text_not_ctrl_hotkeys():
    seq = [down(K("a")), down(CTRL), down(ALTGR), down(K("@")), up(K("@")), up(ALTGR), up(CTRL), down(K("b"))]
    assert _pynput_run(seq) == [{"type": "a@b"}]
    assert _pynput_run([down(CTRL), down(ALTGR), up(ALTGR), up(CTRL)]) == []  # no lone "press ctrl"
    assert _pynput_run([down(CTRL), down(K("\x13")), up(CTRL)]) == [{"hotkey": "ctrl+s"}]


def test_layout_produced_characters_are_not_shifted_again():
    de = [down(K(c)) for c in "4711"] + [down(SHIFT), down(K("/")), up(K("/")), up(SHIFT), down(K("2"))]
    assert _pynput_run(de) == [{"type": "4711/2"}]
    fr = [down(SHIFT)] + [down(K(c)) for c in "120.50"] + [up(SHIFT)]
    assert _pynput_run(fr) == [{"type": "120.50"}]
    # Backend key events (raw, unshifted key names) still get Shift applied.
    r = Recorder(use_selectors=False).start(source="manual")
    for e in [("key_down", "shift"), ("key_down", "1"), ("key_up", "1"), ("key_up", "shift")]:
        r.feed(e)
    assert [a.to_flow_step() for a in r.stop()] == [{"type": "!"}]


def test_vk_only_keys_are_translated():
    from locum.rpa.recorder import _vk_key

    assert [_vk_key(v, "win32") for v in (96, 97, 101, 105, 106, 107, 109, 111)] == list("0159*+-/")
    assert _vk_key(110, "win32") in (".", ",")
    assert _vk_key(49, "win32") == "1"
    assert [_vk_key(v, "linux") for v in (0xFFB1, 0xFF9D, 0xFFAE, 0xFF8D)] == ["1", "5", ".", "enter"]
    assert _vk_key(96, "darwin") is None  # F5 there
    seq = [down(K(vk=0xFFB1)), down(K(vk=0xFFB2)), down(K(vk=0xFF9D)), down(K(",")), down(K(vk=0xFF9D)), down(K("0"))]
    if sys.platform not in ("win32", "darwin"):
        assert _pynput_run(seq) == [{"type": "125,50"}]


def test_untranslatable_vk_key_is_logged(caplog):
    r = Recorder(use_selectors=False)
    with caplog.at_level(logging.WARNING, logger="locum.recorder"):
        assert r._pynput_key(K(vk=0x12345)) is None
    assert "cannot translate key" in caplog.text


def test_clipboard_typing_is_recorded_as_text():
    from locum.backends.mock import MockBackend

    d = Desktop(MockBackend(), sleep=_noop, default_timeout=0)
    r = Recorder(d, use_selectors=False, trust_unknown_focus=True).start(source="backend")  # empty desktop: no focus
    d.keyboard.type("Hello 1,234.56", mode="clipboard")
    d.press("tab")
    d.hotkey("ctrl+v")  # an explicit paste later is still a paste
    r.stop()
    steps = r.to_flow()["steps"]
    assert steps[0] == {"type": "Hello 1,234.56"}
    assert steps[1] == {"press": "tab"}
    assert steps[-1] == {"hotkey": "ctrl+v"}


def test_pynput_callbacks_do_not_run_selector_capture(monkeypatch, be):
    listeners = {}

    class Listener:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            listeners["mouse" if "on_click" in callbacks else "keyboard"] = self

        def start(self):
            pass

        def stop(self):
            pass

    fake = types.ModuleType("pynput")
    fake.mouse = types.SimpleNamespace(Listener=Listener)
    fake.keyboard = types.SimpleNamespace(Listener=Listener)
    monkeypatch.setitem(sys.modules, "pynput", fake)

    d = Desktop(be, sleep=_noop, default_timeout=0)
    release = threading.Event()
    seen = []
    original = be.element_at

    def slow_element_at(x, y):
        seen.append(threading.current_thread())
        release.wait(5)
        return original(x, y)

    monkeypatch.setattr(be, "element_at", slow_element_at)
    rec = Recorder(d).start(source="pynput")
    button = d.find('button "Save"').bounds.center
    on_click = listeners["mouse"].callbacks["on_click"]
    t0 = threading.Event()

    def hook_thread():
        on_click(button.x, button.y, types.SimpleNamespace(name="left"), True)
        on_click(button.x, button.y, types.SimpleNamespace(name="left"), False)
        t0.set()

    h = threading.Thread(target=hook_thread)
    h.start()
    assert t0.wait(2), "the input-hook callback blocked on selector capture"
    release.set()
    actions = rec.stop()
    h.join()
    assert seen and all(t is not h for t in seen)
    assert [a.kind for a in actions] == ["click"] and actions[0].selector
