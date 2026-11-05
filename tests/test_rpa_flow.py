from __future__ import annotations

import json
import os

import pytest
from PIL import Image

from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import WorkflowError
from locum.rpa import FlowStepError, RunReport, interpolate, run_flow, validate_flow
from locum.rpa.report import FAILED, PASSED


@pytest.fixture
def scene():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Invoice", (100, 100, 600, 400), app="erp")
    status = w.add_text("idle", (20, 300, 300, 24))

    def save(_):
        status.value = status.name = "saved"

    w.add_edit("Customer", (20, 40, 200, 28))
    w.add_edit("Amount", (20, 80, 200, 28))
    w.add_checkbox("Paid", (20, 120, 120, 24))
    w.add_button("Save", (20, 160, 80, 30), on_click=save)
    w.add_list("Lines", (300, 40, 200, 100), items=["one", "two"])
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    return be, w, d


def run(doc, d, **kw):
    return run_flow(doc, d, **kw)


# ---------------------------------------------------------------- interpolation

def test_interpolate_paths_env_defaults_and_types(monkeypatch):
    monkeypatch.setenv("LOCUM_TEST_USER", "bob")
    v = {"a": 1, "item": {"addr": {"city": "Oslo"}, "tags": ["x", "y"]}, "lst": [1, 2]}
    assert interpolate("${a}", v) == 1
    assert interpolate("${lst}", v) == [1, 2]
    assert interpolate("n=${a} c=${item.addr.city} t=${item.tags.1}", v) == "n=1 c=Oslo t=y"
    assert interpolate("${env.LOCUM_TEST_USER}", v) == "bob"
    assert interpolate("${missing:-fallback}", v) == "fallback"
    assert interpolate("$${a} is literal", v) == "${a} is literal"
    assert interpolate({"k": ["${a}", {"z": "${item.addr.city}"}]}, v) == {"k": [1, {"z": "Oslo"}]}
    assert interpolate("flag=${b}", {"b": True}) == "flag=true"
    with pytest.raises(WorkflowError, match=r"steps\[2\].*undefined variable 'nope'"):
        interpolate("${nope}", v, path="steps[2]")
    with pytest.raises(WorkflowError, match="did you mean 'item'"):
        interpolate("${iten.x}", v)
    with pytest.raises(WorkflowError, match="no key 'zip'"):
        interpolate("${item.addr.zip}", v)
    with pytest.raises(WorkflowError, match="no index"):
        interpolate("${lst.5}", v)
    with pytest.raises(WorkflowError, match="environment variable"):
        interpolate("${env.LOCUM_DEFINITELY_UNSET_VAR}", v)


# ---------------------------------------------------------------- validation

def test_validation_reports_every_problem_with_paths():
    doc = {
        "steps": [
            {"clik": 'button "Save"'},
            {"click": "x", "type": "y"},
            {"set_text": {"target": "edit"}},
            {"for_each": {"items": [1], "steps": [{"wait": {"target": "x", "tiemout": 3}}]}},
            "not a mapping",
            {"try": {"steps": []}},
        ],
        "setings": {},
    }
    problems = validate_flow(doc)
    text = "\n".join(problems)
    assert "steps[0]: unknown step type 'clik' (did you mean 'click'?)" in text
    assert "steps[1]: a step must have exactly one action" in text
    assert "steps[2].set_text: missing required argument 'text'" in text
    assert "steps[3].for_each.steps[0].wait: unknown argument 'tiemout' (did you mean 'timeout'?)" in text
    assert "steps[4]: a step must be a mapping" in text
    assert "steps[5].try: needs a 'catch' and/or 'finally' list" in text
    assert "unknown top-level key 'setings'" in text
    with pytest.raises(WorkflowError, match="Invalid flow"):
        run_flow(doc, Desktop(MockBackend(), sleep=lambda s: None))


def test_valid_flow_has_no_problems():
    assert validate_flow({"steps": [{"click": "a"}, {"if_exists": {"target": "x", "then": [{"log": "hi"}]}}]}) == []


# ---------------------------------------------------------------- leaf steps

def test_basic_form_flow(scene, tmp_path):
    be, w, d = scene
    doc = {
        "name": "fill",
        "vars": {"customer": "ACME", "amount": 12.5, "greeting": "for ${customer}"},
        "steps": [
            {"activate_window": "Invoice"},
            {"set_text": {"target": 'edit "Customer"', "text": "${customer}"}},
            {"click": 'edit "Amount"'},
            {"type": "${amount}"},
            {"click": 'checkbox "Paid"', "name": "tick paid"},
            {"click": 'button "Save"'},
            {"wait": {"target": 'text "saved"', "timeout": 0}},
            {"assert_exists": {"target": 'edit "Customer"', "equals": "ACME"}},
            {"read_value": {"target": 'edit "Amount"', "var": "amt"}},
            {"set_var": {"total": "${amt}", "msg": "${greeting}"}},
            {"clipboard_set": "copied ${customer}"},
            {"clipboard_get": "clip"},
            {"log": {"message": "done ${msg}", "level": "info"}},
            {"screenshot": {"path": str(tmp_path / "s.png"), "region": [100, 100, 50, 40]}},
            {"sleep": 0.01},
        ],
    }
    report = run(doc, d)
    assert report.ok, report.summary() + "\n" + str(report.failures)
    assert w.find("Customer").value == "ACME"
    assert w.find("Amount").value == "12.5"
    assert w.find("Paid").checked is True
    assert report.variables["total"] == "12.5" and report.variables["msg"] == "for ACME"
    assert report.variables["clip"] == "copied ACME"
    assert any(r["message"] == "done for ACME" for r in report.logs)
    assert Image.open(tmp_path / "s.png").size == (50, 40)
    assert report.find("tick paid").status == PASSED
    assert report.steps[0].path == "steps[0]"


def test_mouse_keyboard_steps(scene):
    be, w, d = scene
    doc = {"steps": [
        {"click": {"x": 130, "y": 155}},
        {"type": {"text": "abc", "into": 'edit "Customer"'}},
        {"press": {"keys": "backspace", "presses": 2}},
        {"hotkey": "ctrl+a"},
        {"type": "Z"},
        {"double_click": 'listitem "two"'},
        {"right_click": [400, 400]},
        {"move": {"target": 'button "Save"', "offset": [5, 0]}},
        {"scroll": {"dy": -3, "at": "500,300"}},
        {"drag": {"start": [150, 450], "end": [300, 450], "duration": 0}},
        {"press": ["tab", "tab"]},
        {"hotkey": ["ctrl", "c"]},
    ]}
    report = run(doc, d)
    assert report.ok, [s.error for s in report.failures]
    assert w.find("Customer").value == "Z"
    assert w.find("two").attrs.get("selected") is True
    assert ("scroll", 0, -3, 500, 300) in be.events
    assert ("down", "right", 400, 400) in be.events


def test_launch_and_image_steps(scene, tmp_path):
    be, w, d = scene
    needle = Image.new("RGB", (24, 24), (250, 20, 200))
    for x in range(24):
        needle.putpixel((x, x), (10, 240, 30))
    be.paint(needle, (700, 600))
    needle.save(tmp_path / "btn.png")
    doc = {"steps": [
        {"launch": "notepad.exe"},
        {"wait_image": {"image": "btn.png", "timeout": 0, "var": "m"}},
        {"click_image": {"image": "btn.png", "timeout": 0}},
        {"click": "btn.png"},
        {"if_exists": {"image": "btn.png", "then": [{"set_var": {"seen": True}}]}},
        {"assert_exists": {"image": "btn.png"}},
    ]}
    report = run(doc, d, base_dir=tmp_path)
    assert report.ok, [s.error for s in report.failures]
    assert be.launched == ["notepad.exe"]
    assert report.variables["m"]["center"] == {"x": 712, "y": 612}
    assert report.variables["seen"] is True
    assert ("down", "left", 712, 612) in be.events


# ---------------------------------------------------------------- control flow

def test_if_exists_branches(scene):
    _, w, d = scene
    doc = {"steps": [
        {"if_exists": {"target": 'button "Save"', "then": [{"set_var": {"a": "yes"}}], "else": [{"set_var": {"a": "no"}}]}},
        {"if_exists": {"target": 'button "Nope"', "then": [{"set_var": {"b": "yes"}}], "else": [{"set_var": {"b": "no"}}]}},
    ]}
    report = run(doc, d)
    assert report.variables["a"] == "yes" and report.variables["b"] == "no"
    assert report.find("steps[1].if_exists.else[0]").status == PASSED


def test_for_each_items_csv_and_loop_vars(scene, tmp_path):
    _, w, d = scene
    (tmp_path / "rows.csv").write_text("name,qty\nA,1\nB,2\n")
    doc = {
        "vars": {"people": [{"name": "x"}, {"name": "y"}], "out": ""},
        "steps": [
            {"for_each": {"items": "${people}", "as": "p", "steps": [
                {"set_var": {"out": "${out}${p.name}${loop.index}/${loop.count};"}},
            ]}},
            {"for_each": {"csv": "rows.csv", "steps": [
                {"set_var": {"out": "${out}${item.name}${item.qty}"}},
            ]}},
            {"repeat": {"times": 3, "as": "i", "steps": [{"set_var": {"out": "${out}.${i}"}}]}},
            {"for_each": {"items": {"k1": "v1"}, "steps": [{"set_var": {"out": "${out}|${item.key}=${item.value}"}}]}},
        ],
    }
    report = run(doc, d, base_dir=tmp_path)
    assert report.ok, [s.error for s in report.failures]
    assert report.variables["out"] == "x0/2;y1/2;A1B2.0.1.2|k1=v1"
    assert "p" not in report.variables and "loop" not in report.variables and "item" not in report.variables


def test_nested_error_path_and_message(scene):
    _, w, d = scene
    doc = {"steps": [
        {"log": "start"},
        {"set_var": {"x": 1}},
        {"click": 'button "Save"'},
        {"for_each": {"items": [1, 2, 3], "steps": [
            {"log": "row ${item}"},
            {"click": 'button "Missing ${item}"'},
        ]}},
        {"log": "never"},
    ]}
    report = run(doc, d)
    assert not report.ok
    assert "steps[3].for_each.steps[1]" in report.error
    assert "for_each iteration 1/3" in report.error
    assert "ElementNotFound" in report.error
    failed = report.find("steps[3].for_each.steps[1]")
    assert failed.status == FAILED and failed.error_type == "ElementNotFound"
    assert report.find("steps[3]").status == FAILED
    assert report.find("steps[4]") is None
    with pytest.raises(FlowStepError) as ei:
        run(doc, d, raise_on_error=True)
    assert ei.value.path == "steps[3].for_each.steps[1]" and ei.value.step_type == "click"
    assert type(ei.value.original).__name__ == "ElementNotFound"


def test_try_catch_finally(scene):
    _, w, d = scene
    doc = {"steps": [
        {"try": {
            "steps": [{"log": "a"}, {"click": 'button "Ghost"'}, {"set_var": {"skipped": True}}],
            "catch": [{"set_var": {"err": "${error.type} at ${error.step}"}}],
            "finally": [{"set_var": {"cleaned": True}}],
        }},
    ]}
    report = run(doc, d)
    assert report.ok
    v = report.variables
    assert v["err"] == "ElementNotFound at steps[0].try.steps[1]"
    assert v["cleaned"] is True and "skipped" not in v and "error" not in v
    inner = report.find("steps[0].try.steps[1]")
    assert inner.status == FAILED and inner.handled


def test_try_finally_without_catch_reraises(scene):
    _, w, d = scene
    report = run({"steps": [{"try": {"steps": [{"fail": "nope"}], "finally": [{"set_var": {"f": 1}}]}}]}, d)
    assert not report.ok and report.variables["f"] == 1
    assert "FlowAssertionError" in report.error and "nope" in report.error


def test_for_each_on_error_continue(scene):
    _, w, d = scene
    doc = {"steps": [{"for_each": {"items": [1, 2, 3], "on_error": "continue", "steps": [
        {"assert_exists": {"target": 'button "B${item}"'}},
    ]}}, {"set_var": {"after": True}}]}
    report = run(doc, d)
    assert report.ok and report.variables["after"]
    assert report.counts()["handled"] == 3


def test_retries_and_continue_on_error(scene):
    be, w, d = scene
    calls = []
    orig = d.find

    def flaky_find(sel, **kw):
        calls.append(sel)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return orig(sel, **kw)

    d.find = flaky_find
    doc = {"settings": {"retry_delay": 0}, "steps": [
        {"click": 'button "Save"', "retries": 2},
        {"click": 'button "Nope"', "continue_on_error": True},
        {"set_var": {"ok": 1}},
    ]}
    report = run(doc, d)
    assert report.ok, report.error
    assert report.steps[0].attempts == 3
    assert report.steps[1].status == FAILED and report.steps[1].handled
    assert w.find("Save").clicks == 1


def test_call_inline_and_file_subflows(scene, tmp_path):
    _, w, d = scene
    sub = {"vars": {"suffix": "!"}, "steps": [{"set_var": {"result": "${who}${suffix}"}}]}
    (tmp_path / "sub.json").write_text(json.dumps(sub))
    doc = {
        "flows": {"greet": {"steps": [{"set_var": {"greeting": "hi ${who}"}}]}},
        "vars": {"who": "ann"},
        "steps": [
            {"call": {"flow": "greet", "outputs": ["greeting"]}},
            {"call": {"flow": "sub.json", "vars": {"who": "bob"}, "outputs": {"result": "r"}}},
            {"call": "greet"},
        ],
    }
    report = run(doc, d, base_dir=tmp_path)
    assert report.ok, report.error
    assert report.variables["greeting"] == "hi ann"
    assert report.variables["r"] == "bob!"
    assert report.variables["who"] == "ann" and "suffix" not in report.variables
    assert report.find("steps[1].call.steps[0]").status == PASSED


def test_call_errors(scene, tmp_path):
    _, w, d = scene
    report = run({"steps": [{"call": "nowhere"}]}, d, base_dir=tmp_path)
    assert "no sub-flow named 'nowhere'" in report.error
    loop = {"flows": {"me": [{"call": "me"}]}, "steps": [{"call": "me"}]}
    report = run(loop, d)
    assert "call depth limit" in report.error
    (tmp_path / "bad.json").write_text(json.dumps({"steps": [{"clack": 1}]}))
    report = run({"steps": [{"call": "bad.json"}]}, d, base_dir=tmp_path)
    assert "steps[0].call.steps[0]: unknown step type 'clack'" in report.error


def test_undefined_variable_error_names_step(scene):
    _, w, d = scene
    report = run({"steps": [{"log": "ok"}, {"type": "${nope}"}]}, d)
    assert not report.ok
    assert "steps[1]" in report.error and "undefined variable 'nope'" in report.error


def test_yaml_file_with_evidence(scene, tmp_path):
    pytest.importorskip("yaml")
    _, w, d = scene
    flow = tmp_path / "flow.yaml"
    flow.write_text(
        "name: yaml flow\n"
        "vars:\n  who: ${env.LOCUM_FLOW_WHO:-nobody}\n"
        "steps:\n"
        "  - set_text: {target: 'edit \"Customer\"', text: '${who}'}\n"
        "  - click: 'button \"DoesNotExist\"'\n"
    )
    report = run_flow(flow, d, variables={"extra": 1}, run_dir=tmp_path / "runs")
    assert w.find("Customer").value == "nobody"
    assert not report.ok
    run_dir = tmp_path / "runs" / report.run_id
    assert (run_dir / "report.json").is_file() and (run_dir / "report.html").is_file()
    failed = report.find("steps[1]")
    assert failed.screenshot and os.path.isfile(failed.screenshot)
    assert RunReport.load_json(run_dir / "report.json").error == report.error
    assert report.name == "yaml flow"


def test_text_sources_and_bare_list(scene):
    _, w, d = scene
    report = run_flow('{"steps": [{"set_var": {"a": 1}}]}', d)
    assert report.variables["a"] == 1
    report = run_flow([{"set_var": {"b": 2}}], d)
    assert report.variables["b"] == 2
    with pytest.raises(FileNotFoundError):
        run_flow("missing_flow.yaml", d)


def test_caller_variables_override_doc_vars(scene):
    _, w, d = scene
    doc = {"vars": {"a": "${undefined_thing}", "b": "x"}, "steps": [{"set_var": {"c": "${a}${b}"}}]}
    report = run(doc, d, variables={"a": "A"})
    assert report.ok and report.variables["c"] == "Ax"


def test_assert_exists_failures(scene):
    _, w, d = scene
    r1 = run({"steps": [{"assert_exists": {"target": 'button "Save"', "equals": "nope", "message": "custom msg"}}]}, d)
    assert "custom msg" in r1.error
    r2 = run({"steps": [{"assert_exists": 'button "Ghost"'}]}, d)
    assert "FlowAssertionError" in r2.error and "Ghost" in r2.error
