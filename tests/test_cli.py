"""Tests for the ``locum`` command line (run in-process on the demo MockBackend)."""

from __future__ import annotations

import io
import json
import pathlib
import runpy
import subprocess
import sys

import pytest

from locum import cli
from locum.cli import (
    EXIT_ABORTED,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    DemoBackend,
    build_parser,
    main,
)
from locum.desktop import Desktop
from locum.errors import NotSupported
from locum.llm.agent import scripted_response, tool_use_block

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _noop(_s: float) -> None:
    return None


@pytest.fixture
def be() -> DemoBackend:
    return DemoBackend()


@pytest.fixture
def desk(be: DemoBackend) -> Desktop:
    return Desktop(be, sleep=_noop, default_timeout=0.2, poll_interval=0.01)


def run(argv, desktop=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err, **kw)
    return code, out.getvalue(), err.getvalue()


# ============================================================================== parser & basics


def test_version_and_help():
    code, out, _ = run(["--version"])
    assert code == EXIT_OK
    code, out, _ = run([])
    assert code == EXIT_USAGE and "usage: locum" in out
    code, _, _ = run(["--help"])
    assert code == EXIT_OK


def test_unknown_command_and_bad_arguments_are_usage_errors(capsys):
    assert run(["frobnicate"])[0] == EXIT_USAGE
    assert run(["click"])[0] == EXIT_USAGE  # missing TARGET
    assert run(["--timeout", "-1", "info"])[0] == EXIT_USAGE


def test_every_spec_command_is_registered():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for name in ("info", "screenshot", "windows", "tree", "find", "click", "type", "hotkey", "locate",
                 "observe", "mcp", "agent", "run", "record"):
        assert name in choices


def test_python_dash_m_entry_point():
    proc = subprocess.run([sys.executable, "-m", "locum", "--version"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "locum" in proc.stdout


def test_unknown_backend_is_unavailable():
    code, _, err = run(["--backend", "nosuchbackend", "info"])
    assert code == EXIT_UNAVAILABLE
    assert "unavailable" in err and "nosuchbackend" in err


def test_backend_mock_builds_the_demo_desktop():
    code, out, _ = run(["--backend", "mock", "--json", "info"])
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["backend"] == "mock"
    assert data["backend_class"].endswith("DemoBackend")
    assert "accessibility" in data["capabilities"]
    assert data["active_window"]["title"] == "Invoice Entry - ERP"
    assert data["monitors"][0]["rect"]["width"] == 1920
    assert "yaml" in data["optional_dependencies"]


def test_env_var_selects_demo_backend(monkeypatch):
    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    code, out, _ = run(["windows"])
    assert code == EXIT_OK and "Book1 - Excel" in out


def test_info_text(desk):
    code, out, _ = run(["info"], desk)
    assert code == EXIT_OK
    assert "backend:      mock" in out
    assert "monitor 0:" in out and "primary" in out
    assert "failsafe:     on" in out


# ============================================================================== inspection commands


def test_windows_text_json_and_filters(desk):
    code, out, _ = run(["windows"], desk)
    lines = out.strip().splitlines()
    assert code == EXIT_OK and len(lines) == 3
    assert lines[0].startswith("* 'Invoice Entry - ERP'") and "[active]" in lines[0]
    code, out, _ = run(["windows", "--json", "--app", "excel"], desk)
    data = json.loads(out)
    assert [w["title"] for w in data] == ["Book1 - Excel"]
    code, out, err = run(["windows", "--title", "nothing-like-this"], desk)
    assert code == EXIT_OK and out == "" and "no windows match" in err


def test_tree_window_depth_and_all(desk):
    code, out, _ = run(["tree", "--window", "Invoice Entry"], desk)
    assert code == EXIT_OK
    assert 'edit "Amount" #amount' in out
    assert '"Vendor:"' not in out  # labels hidden without --all
    code, out, _ = run(["tree", "--window", "Invoice Entry", "--all"], desk)
    assert 'text "Vendor:"' in out
    code, out, _ = run(["tree", "--depth", "0"], desk)
    assert out.count("\n") == 3  # three window roots only
    code, out, _ = run(["--json", "tree", "--window", "Notepad"], desk)
    assert json.loads(out)["tree"][1].strip().startswith('edit "Text Editor"')
    assert run(["tree", "--depth", "-1"], desk)[0] == EXIT_USAGE


def test_find_single_all_and_json(desk):
    code, out, _ = run(["find", "edit#amount"], desk)
    assert code == EXIT_OK
    assert 'edit "Amount" #amount' in out and "->" in out and "edit#amount" in out
    code, out, _ = run(["find", "--all", "--json", 'window[name%="Book1*"] edit[name^="A"]'], desk)
    data = json.loads(out)
    assert code == EXIT_OK and [d["name"] for d in data][:3] == ["A1", "A2", "A3"]
    assert all(d["robust_selector"] for d in data)
    code, out, _ = run(["find", "--json", 'button "Save"'], desk)
    assert json.loads(out)["automation_id"] == "save"


def test_find_not_found_and_invalid_selector(desk):
    code, out, err = run(["find", 'button "Sav"', "--timeout", "0"], desk)
    assert code == EXIT_FAILURE and out == ""
    assert "ElementNotFound" in err and 'button "Save"' in err  # did-you-mean hint
    code, out, err = run(["find", "--all", 'button "Nope"'], desk)
    assert code == EXIT_FAILURE and "no element matches" in err
    code, _, err = run(["find", "button[name=", "--timeout", "0"], desk)
    assert code == EXIT_USAGE and "invalid input" in err


def test_inspect_point_and_empty_spot(desk):
    code, out, _ = run(["inspect", "200,100"], desk)
    assert code == EXIT_OK
    assert "selector: window \"Invoice Entry - ERP\" edit#vendor" in out
    code, out, _ = run(["inspect", "--json", "200,100"], desk)
    data = json.loads(out)
    assert data["automation_id"] == "vendor" and data["point"] == {"x": 200, "y": 100}
    assert data["ancestors"][0]["role"] == "window"
    code, _, err = run(["inspect", "1900,1060"], desk)
    assert code == EXIT_FAILURE and "no accessible element" in err
    assert run(["inspect", "abc"], desk)[0] == EXIT_USAGE


def test_observe_text_json_and_screenshot(desk, tmp_path):
    code, out, _ = run(["observe", "--window", "Invoice Entry"], desk)
    assert code == EXIT_OK
    assert '[1] edit "Vendor"' in out and 'button "Submit Payment"' in out
    shot = tmp_path / "obs" / "marks.png"
    code, out, _ = run(["observe", "--json", "--screenshot", str(shot), "--max-elements", "5"], desk)
    data = json.loads(out)
    assert code == EXIT_OK and len(data["elements"]) == 5
    assert data["screenshot"] == str(shot.resolve()) and shot.exists()
    assert data["elements"][0]["selector"]


# ============================================================================== actions


def test_click_selector_drives_the_app(desk, be):
    be._fields["vendor"].value = "ACME"
    be._fields["invoice_no"].value = "INV-1"
    be._fields["amount"].value = "10"
    code, out, _ = run(["click", 'button "Save"'], desk)
    assert code == EXIT_OK and out.startswith("clicked (230, 284)")
    assert [i["invoice_no"] for i in be.invoices] == ["INV-1"]
    assert be._status.name == "Saved invoice INV-1"


def test_click_coordinates_button_clicks_offset(desk, be):
    code, out, _ = run(["--json", "click", "300,200", "--button", "right", "--clicks", "2", "--offset", "5,-5"], desk)
    assert code == EXIT_OK
    assert json.loads(out) == {"clicked": {"x": 305, "y": 195}, "button": "right", "clicks": 2}
    downs = [e for e in be.events if e[0] == "down"]
    assert downs == [("down", "right", 305, 195), ("down", "right", 305, 195)]
    assert run(["click", "1,2", "--clicks", "0"], desk)[0] == EXIT_USAGE
    assert run(["click", "1,2", "--offset", "nope"], desk)[0] == EXIT_USAGE


def test_move_and_scroll(desk, be):
    code, out, _ = run(["move", "edit#amount"], desk)
    assert code == EXIT_OK and "moved to (370, 171)" in out
    code, out, _ = run(["scroll", "-3", "--dx", "2", "--at", "100,120"], desk)
    assert code == EXIT_OK
    scrolls = [e for e in be.events if e[0] == "scroll"]
    assert scrolls and scrolls[-1][3:] == (100, 120)
    assert sum(e[2] for e in scrolls) == -3 and sum(e[1] for e in scrolls) == 2


def test_type_into_with_escapes_and_replace(desk, be):
    code, out, _ = run(["type", "-e", r"line one\nline two\t!", "--into", 'edit "Text Editor"'], desk)
    assert code == EXIT_OK and "into" in out
    editor = be.notepad.find("Text Editor")
    assert editor.value == "line one\nline two!"  # \t is a Tab key press (the editor is the only focusable)
    code, _, _ = run(["type", "--into", "edit#vendor", "--replace", "Globex"], desk)
    assert code == EXIT_OK and be._fields["vendor"].value == "Globex"
    code, _, _ = run(["type", " Inc", "--into", "edit#vendor"], desk)  # without --replace: appends
    assert be._fields["vendor"].value == "Globex Inc"
    code, _, _ = run(["type", "!"], desk)  # into whatever has the focus now
    assert be._fields["vendor"].value == "Globex Inc!"


def test_hotkey_and_press(desk, be):
    be._focus = be._fields["vendor"]
    code, out, _ = run(["hotkey", "ctrl+a"], desk)
    assert code == EXIT_OK and "pressed ctrl+a" in out
    assert ("key_down", "ctrl") in be.events and ("key_up", "ctrl") in be.events
    code, out, _ = run(["press", "tab", "--presses", "2"], desk)
    assert code == EXIT_OK and be.focused_widget is be._fields["amount"]
    code, out, _ = run(["--json", "hotkey", "shift", "tab"], desk)
    assert json.loads(out) == {"hotkey": ["shift", "tab"]}
    assert be.focused_widget is be._fields["invoice_no"]
    assert run(["press", "tab", "--presses", "0"], desk)[0] == EXIT_USAGE


def test_wait_visible_and_gone(desk, be):
    code, out, _ = run(["wait", 'button "Save"'], desk)
    assert code == EXIT_OK and out.startswith("visible:")
    code, out, _ = run(["wait", 'button "Not There"', "--state", "gone", "--timeout", "0"], desk)
    assert code == EXIT_OK and "is gone" in out
    code, _, err = run(["wait", 'button "Save"', "--state", "gone", "--timeout", "0"], desk)
    assert code == EXIT_FAILURE and "ActionTimeout" in err
    assert run(["wait", 'button "Save"', "--state", "sparkly"], desk)[0] == EXIT_USAGE


def test_screenshot_region_and_default_name(desk, tmp_path, monkeypatch):
    out_file = tmp_path / "shots" / "s.png"
    code, out, _ = run(["--json", "screenshot", "-o", str(out_file), "--region", "10,20,300,200"], desk)
    data = json.loads(out)
    assert code == EXIT_OK and data["width"] == 300 and data["height"] == 200 and out_file.exists()
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(["screenshot"], desk)
    assert code == EXIT_OK and "1920x1080" in out
    assert list(tmp_path.glob("screenshot-*.png"))
    assert run(["screenshot", "--region", "1,2,3"], desk)[0] == EXIT_USAGE
    assert run(["screenshot", "--region", "0,0,0,10"], desk)[0] == EXIT_USAGE


def test_locate_found_click_all_and_missing(desk, be, tmp_path):
    needle = tmp_path / "save.png"
    desk.screenshot((180, 268, 100, 32), path=str(needle))  # the Save button
    region = ["--region", "0,0,640,560"]  # keeps the (numpy-only) matching fast
    code, out, _ = run(["locate", str(needle), *region], desk)
    assert code == EXIT_OK and "center (230, 284)" in out
    code, out, _ = run(["--json", "locate", str(needle), "--all", *region], desk)
    data = json.loads(out)
    assert data and data[0]["rect"] == {"left": 180, "top": 268, "width": 100, "height": 32}
    be.clear_events()
    code, out, _ = run(["locate", str(needle), "--click", *region], desk)
    assert code == EXIT_OK and "clicked (230, 284)" in out
    assert ("down", "left", 230, 284) in be.events
    from PIL import Image

    Image.new("RGB", (40, 40), (255, 0, 255)).save(tmp_path / "magenta.png")
    code, _, err = run(["locate", str(tmp_path / "magenta.png"), *region], desk)
    assert code == EXIT_FAILURE and "not found" in err
    assert run(["locate", str(needle), "--confidence", "1.5"], desk)[0] == EXIT_USAGE
    code, _, err = run(["locate", str(tmp_path / "missing.png")], desk)
    assert code == EXIT_FAILURE


# ============================================================================== flows


def test_run_excel_flow_fills_the_demo_sheet(desk, be):
    code, out, err = run(["run", str(EXAMPLES / "excel_report_flow.yaml")], desk)
    assert code == EXIT_OK, err
    assert "excel_report: PASSED" in out
    assert [be.cell(c + "1") for c in "ABC"] == ["Region", "Units", "Revenue"]
    assert [be.cell(c + "5") for c in "ABC"] == ["West", "88", "13002.75"]
    assert be.saved_workbooks == 1


def test_run_flow_vars_json_and_report_dir(desk, be, tmp_path):
    code, out, _ = run(["--json", "run", str(EXAMPLES / "excel_report_flow.yaml"), "--var", "start_cell=B3",
                        "--report-dir", str(tmp_path)], desk)
    data = json.loads(out)
    assert code == EXIT_OK and data["ok"] is True
    assert be.cell("B3") == "Region" and be.cell("D7") == "13002.75"
    assert list(tmp_path.glob("*/report.html"))


def test_run_flow_failure_validation_and_errors(desk, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("steps:\n  - clik: 'button \"Save\"'\n", encoding="utf-8")
    code, out, err = run(["run", str(bad), "--validate"], desk)
    assert code == EXIT_USAGE and "clik" in err
    code, _, err = run(["run", str(bad)], desk)
    assert code == EXIT_USAGE and "invalid input" in err
    failing = tmp_path / "fail.yaml"
    failing.write_text("steps:\n  - click: {target: 'button \"Missing\"', timeout: 0}\n", encoding="utf-8")
    code, out, err = run(["run", str(failing), "--report-dir", str(tmp_path / "runs")], desk)
    assert code == EXIT_FAILURE and "FAILED" in out and "FAILED steps[0]" in err
    assert list((tmp_path / "runs").glob("*/report.json"))
    code, out, _ = run(["--json", "run", str(EXAMPLES / "excel_report_flow.yaml"), "--validate"], desk)
    assert code == EXIT_OK and json.loads(out) == {"valid": True, "problems": []}
    assert run(["run", str(tmp_path / "nope.yaml")], desk)[0] == EXIT_FAILURE
    assert run(["run", str(failing), "--var", "novalue"], desk)[0] == EXIT_USAGE


# ============================================================================== mcp & agent


def _rpc(*messages):
    return io.StringIO("".join(json.dumps(m) + "\n" for m in messages))


def test_mcp_server_over_stdio(desk, be):
    stdin = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "set_value", "arguments": {"selector": "edit#vendor", "value": "Hooli"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "click", "arguments": {"selector": 'button "Submit Payment"'}}},
    )
    code, out, err = run(["mcp"], desk, stdin=stdin)
    assert code == EXIT_OK
    assert "MCP server on stdio" in err and "policy=default" in err
    responses = [json.loads(line) for line in out.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]  # nothing but protocol on stdout
    names = {t["name"] for t in responses[1]["result"]["tools"]}
    assert {"observe", "click", "type_text", "set_value"} <= names
    assert responses[2]["result"]["isError"] is False and be._fields["vendor"].value == "Hooli"
    assert responses[3]["result"]["isError"] is True  # payment needs confirmation: blocked
    assert be.payments == []


def test_mcp_dry_run_permissive_and_audit(desk, be, tmp_path):
    audit = tmp_path / "audit.jsonl"
    stdin = _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "set_value", "arguments": {"selector": "edit#vendor", "value": "X"}}})
    code, out, err = run(["mcp", "--policy", "permissive", "--dry-run", "--audit", str(audit)], desk, stdin=stdin)
    assert code == EXIT_OK and "dry-run" in err
    res = json.loads(out)["result"]
    assert res["isError"] is False and be._fields["vendor"].value == ""
    assert audit.exists() and audit.read_text(encoding="utf-8").strip()


def test_mcp_strict_policy_blocks_launch(desk):
    stdin = _rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "launch_app", "arguments": {"command": "calc"}}})
    code, out, _ = run(["mcp", "--policy", "strict"], desk, stdin=stdin)
    res = json.loads(out)["result"]
    assert code == EXIT_OK and res["isError"] is True and "not allowed" in res["content"][0]["text"]


def _agent_script():
    from locum.llm.agent import ScriptedClient

    return ScriptedClient([
        scripted_response("Filling the form.",
                          tool_use_block("set_value", {"selector": "edit#vendor", "value": "ACME"}),
                          tool_use_block("set_value", {"selector": "edit#invoice_no", "value": "INV-9"}),
                          tool_use_block("set_value", {"selector": "edit#amount", "value": "12.5"}),
                          tool_use_block("click", {"selector": 'button "Save"'})),
        scripted_response(tool_use_block("done", {"summary": "Saved INV-9", "success": True})),
    ])


def test_agent_with_injected_client(desk, be):
    code, out, err = run(["agent", "Enter invoice INV-9", "--no-confirm"], desk, client=_agent_script())
    assert code == EXIT_OK, err
    assert out.strip() == "Saved INV-9"
    assert "[1] set_value(" in err and "success after 2 step(s)" in err
    assert [i["invoice_no"] for i in be.invoices] == ["INV-9"]


def test_agent_json_dry_run_and_quiet(desk, be):
    code, out, err = run(["--json", "agent", "task", "--dry-run", "-q"], desk, client=_agent_script())
    data = json.loads(out)
    assert code == EXIT_OK and data["success"] is True and len(data["steps"]) == 2
    assert err == "" and be.invoices == []  # dry-run: nothing happened


def test_agent_replay_file_and_failures(desk, tmp_path):
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"responses": [
        scripted_response("I can't do that.", stop_reason="refusal"),
    ]}), encoding="utf-8")
    code, out, err = run(["agent", "do something", "--replay", str(replay)], desk)
    assert code == EXIT_FAILURE and "refusal" in out and "FAILED" in err
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert run(["agent", "x", "--replay", str(empty)], desk)[0] == EXIT_USAGE
    assert run(["agent", "x", "--max-steps", "0"], desk)[0] == EXIT_USAGE


def test_agent_without_sdk_reports_missing_dependency(desk, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    code, _, err = run(["agent", "x"], desk)
    assert code == EXIT_UNAVAILABLE
    assert "missing dependency" in err and "locum[llm]" in err


# ============================================================================== record


def test_record_saves_python_and_json(desk, be, tmp_path, monkeypatch):
    from locum.rpa import recorder as rec_mod

    def fake_start(self):
        # Fed events never really focus the field: tell the (fail-closed) recorder to trust it.
        self.trust_unknown_focus = True
        for ev in (("move", 370, 99), ("down", "left", 370, 99), ("up", "left", 370, 99), ("type", "ACME")):
            self.feed(ev)

    monkeypatch.setattr(rec_mod.Recorder, "_start_pynput", fake_start)
    script = tmp_path / "rec.py"
    code, out, err = run(["record", "--source", "pynput", "-o", str(script), "--duration", "0"], desk)
    assert code == EXIT_OK, err
    assert "recorded 2 action(s)" in out
    text = script.read_text(encoding="utf-8")
    assert "edit#vendor" in text and "ACME" in text
    flow = tmp_path / "rec.json"
    code, out, _ = run(["--json", "record", "--source", "pynput", "-o", str(flow), "--duration", "0"], desk)
    data = json.loads(out)
    assert code == EXIT_OK and [a["kind"] for a in data["actions"]] == ["click", "type"]
    assert json.loads(flow.read_text(encoding="utf-8"))["steps"]


def test_record_until_interrupted(desk, tmp_path, monkeypatch):
    from locum.rpa import recorder as rec_mod

    monkeypatch.setattr(rec_mod.Recorder, "_start_pynput", lambda self: None)
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 3:
            raise KeyboardInterrupt

    code, out, _ = run(["record", "-o", str(tmp_path / "r.py")], desk, sleep=sleep)
    assert code == EXIT_OK and len(calls) == 3 and "recorded 0 action(s)" in out


def test_record_on_mock_backend_uses_backend_source(desk, tmp_path, monkeypatch):
    """'auto' on the mock backend must not start global pynput hooks on the real display (regression)."""
    from locum.rpa import recorder as rec_mod

    def boom(self):
        raise AssertionError("pynput must not be used for the mock backend")

    monkeypatch.setattr(rec_mod.Recorder, "_start_pynput", boom)
    code, out, err = run(["record", "-o", str(tmp_path / "r.py"), "--duration", "0"], desk)
    assert code == EXIT_OK, err
    assert "recorded 0 action(s)" in out and "mock backend" in err
    assert not any(k in vars(desk.backend) for k in ("mouse_down", "type_text"))  # hooks removed


def test_record_errors(desk, tmp_path, monkeypatch):
    from locum.rpa import recorder as rec_mod

    def unavailable(self):
        raise NotSupported("Live recording needs pynput: pip install locum[record]")

    monkeypatch.setattr(rec_mod.Recorder, "_start_pynput", unavailable)
    code, _, err = run(["record", "--source", "pynput", "-o", str(tmp_path / "r.py"), "--duration", "0"], desk)
    assert code == EXIT_UNAVAILABLE and "locum[record]" in err
    assert run(["record", "-o", str(tmp_path / "r.txt")], desk)[0] == EXIT_USAGE
    assert run(["record", "--duration", "-1"], desk)[0] == EXIT_USAGE


# ============================================================================== error handling


def test_failsafe_abort_exit_code(be):
    d = Desktop(be, sleep=_noop, default_timeout=0)
    be.mouse_move(0, 0)  # cursor in a fail-safe corner
    code, _, err = run(["click", "100,100"], d)
    assert code == EXIT_ABORTED and "aborted" in err


def test_keyboard_interrupt_and_unexpected_errors(desk, monkeypatch):
    def interrupted(ctx):
        raise KeyboardInterrupt

    def boom(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cli.COMMANDS, "info", interrupted)
    assert run(["info"], desk)[0] == EXIT_INTERRUPTED
    monkeypatch.setitem(cli.COMMANDS, "info", boom)
    code, _, err = run(["info"], desk)
    assert code == EXIT_FAILURE and "kaboom" in err and "--debug" in err
    code, _, err = run(["--debug", "info"], desk)
    assert code == EXIT_FAILURE and "Traceback" in err


def test_kill_hotkey_flag_is_best_effort(desk, monkeypatch):
    import locum.failsafe as fs

    monkeypatch.setattr(fs, "install_kill_hotkey", lambda combo="": False)
    code, _, err = run(["--kill-hotkey", "info"], desk)
    assert code == EXIT_OK and "could not install the kill hotkey" in err
    monkeypatch.setattr(fs, "install_kill_hotkey", lambda combo="": True)
    code, _, err = run(["--kill-hotkey", "--kill-combo", "ctrl+alt+k", "info"], desk)
    assert code == EXIT_OK and "ctrl+alt+k armed" in err


def test_unescape():
    assert cli._unescape(r"a\nb\tc\\dé\q") == "a\nb\tc\\dé\\q"


# ============================================================================== demo backend


def test_demo_erp_validation_duplicates_and_payment(desk, be):
    save = 'button "Save"'
    desk.click(save)
    assert be._status.name.startswith("Error: vendor and invoice number")
    desk.find("edit#vendor").set_text("A")
    desk.find("edit#invoice_no").set_text("I-1")
    desk.find("edit#amount").set_text("abc")
    desk.click(save)
    assert "is not a number" in be._status.name
    desk.find("edit#amount").set_text("-1")
    desk.click(save)
    assert be._status.name == "Error: amount must be positive"
    desk.find("edit#amount").set_text("1,234.5")
    desk.find("checkbox#urgent").check()
    desk.click(save)
    assert be.invoices[-1] == {"vendor": "A", "invoice_no": "I-1", "amount": 1234.5, "due_date": "",
                               "urgent": True, "paid": False}
    assert desk.find('listitem "I-1 | A | 1234.50"', timeout=0)
    for field, value in (("vendor", "A"), ("invoice_no", "I-1"), ("amount", "5")):
        desk.find(f"edit#{field}").set_text(value)
    desk.click(save)
    assert be._status.name == "Error: invoice I-1 already exists"
    desk.click('button "Clear"')
    assert be._status.name == "Cleared" and be._fields["vendor"].value == ""
    desk.click('button "Submit Payment"')
    assert be.payments == [{"invoices": ["I-1"], "total": 1234.5}] and be.invoices[0]["paid"]
    desk.click('button "Submit Payment"')
    assert be._status.name == "Nothing to pay"


def test_demo_invoice_list_scrolls(be):
    for i in range(10):
        be._add_list_item(f"item {i}")
    visible = [w.name for w in be._list.children if w.visible]
    assert visible[0] == "item 4" and visible[-1] == "item 9" and len(visible) == 6
    assert all(w.screen_rect.bottom <= be._list.screen_rect.bottom for w in be._list.children if w.visible)


def test_demo_spreadsheet_navigation(desk, be):
    desk.click('edit "Name Box"')
    desk.type("C2\n")
    assert be.focused_widget is be._cells["C2"]
    desk.type("x\ty\n")
    assert be.cell("C2") == "x" and be.cell("D2") == "y"
    assert be.focused_widget is be._cells["C3"]  # Enter returns to the anchor column
    desk.press("up", "right", "down", "left")
    assert be.focused_widget is be._cells["C3"]
    desk.hotkey("shift+tab")
    assert be.focused_widget is be._cells["B3"] and be._name_box.value == "B3"
    desk.click('edit "D2"')
    desk.type("z")  # typing replaces the cell
    assert be.cell("D2") == "z" and be._formula.value == "z"
    desk.hotkey("ctrl+s")
    assert be.saved_workbooks == 1
    desk.click('edit "Name Box"')
    desk.type("Z99\n")  # invalid reference: stays in the Name Box
    assert be.focused_widget is be._name_box
    with pytest.raises(KeyError):
        be.cell("Q1")


# ============================================================================== examples & config


def _run_example(name, monkeypatch, argv=()):
    monkeypatch.setenv("LOCUM_BACKEND", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", [str(EXAMPLES / name), *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(EXAMPLES / name), run_name="__main__")
    return exc.value.code


@pytest.mark.parametrize("name", ["basic_automation.py", "sap_style_data_entry.py", "pyautogui_migration.py"])
def test_examples_run_on_the_demo_desktop(name, monkeypatch, capsys):
    assert _run_example(name, monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.strip()


def test_llm_agent_example_offline(monkeypatch, capsys, tmp_path):
    assert _run_example("llm_agent.py", monkeypatch, ["--audit", str(tmp_path / "a.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "[confirm] The agent wants to: click button 'Submit Payment'" in out
    assert "declined" in out and "'INV-7'" in out and "payments: []" in out


def test_sap_example_records_business_exception(monkeypatch, capsys):
    assert _run_example("sap_style_data_entry.py", monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Initech | INV-1003       business_exception" in out
    assert "invoices stored by the demo ERP: ['INV-1001', 'INV-1002', 'INV-1004']" in out


def test_mcp_config_example_matches_the_cli():
    config = json.loads((EXAMPLES / "mcp_config.json").read_text(encoding="utf-8"))
    parser = build_parser()
    for server in config["mcpServers"].values():
        assert server["command"] == "locum"
        args = parser.parse_args(server["args"])
        assert args.command == "mcp"


def test_ci_workflow_matrix():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = next(iter(wf["jobs"].values()))
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest", "macos-latest"}
    assert {"3.9", "3.12", "3.13"} <= set(map(str, matrix["python-version"]))


# ============================================================================== documentation snippets

_FENCE_RE = __import__("re").compile(r"(<!-- norun -->\s*\n)?```python\n(.*?)```", __import__("re").S)


def _python_snippets(path: pathlib.Path):
    """Runnable ```python blocks of a Markdown file (blocks preceded by ``<!-- norun -->`` are skipped)."""
    return [code for norun, code in _FENCE_RE.findall(path.read_text(encoding="utf-8")) if not norun]


@pytest.mark.parametrize("doc", ["README.md", "docs/LLM.md", "docs/COMPARISON.md"])
def test_documentation_snippets_run_on_the_demo_desktop(doc, tmp_path, monkeypatch, capsys):
    path = ROOT / doc
    snippets = _python_snippets(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setitem(Desktop.__init__.__kwdefaults__, "sleep", _noop)  # snippets' Desktop() never sleeps
    namespace = {"__name__": "__doc_snippets__"}
    if "from locum.cli import DemoBackend" not in "".join(snippets[:1]):
        exec("from locum import Desktop\nfrom locum.cli import DemoBackend\nd = Desktop(DemoBackend())", namespace)
    for i, code in enumerate(snippets):
        try:
            exec(compile(code, f"{doc}[python block {i}]", "exec"), namespace)
        except Exception as exc:  # pragma: no cover - failure path
            pytest.fail(f"{doc} python block {i} failed: {type(exc).__name__}: {exc}\n{code}")
