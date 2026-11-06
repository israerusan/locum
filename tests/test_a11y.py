"""``locum.a11y.audit`` and ``locum a11y-audit`` on the mock backend (read-only: nothing is clicked)."""

from __future__ import annotations

import io
import json

import pytest

from locum import cli
from locum.a11y import RULES, AuditReport, Finding, audit
from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.errors import WindowNotFound
from locum.selectors import parse


def form():
    """A window with one instance of every problem, next to a clean window."""
    be = MockBackend(width=1600, height=900)
    w = be.add_window("Form", (0, 0, 800, 600), app="formapp")
    w.add_button("", (20, 20, 80, 30))                                     # unnamed-control (error)
    icon_btn = w.add_button("", (120, 20, 80, 30))                         # named only by its child text
    icon_btn.add("text", "Print", (125, 25, 40, 20))
    w.add_text("Email:", (20, 70, 60, 24))
    w.add_edit("", (90, 70, 200, 28))                                      # unlabeled-input + hint
    w.add("image", "", (300, 70, 40, 40))                                  # unnamed-image (info)
    named = w.add_button("Open", (360, 70, 60, 30))
    named.add("image", "", (365, 75, 16, 16))                              # icon inside a button: fine
    w.add("togglebutton", "Bold", (20, 130, 40, 40))                       # mock: not focusable
    w.add_button("+", (20, 200, 12, 12))                                   # tiny and crowded
    w.add_button("-", (34, 200, 12, 12))
    w.add_button("?", (400, 400, 12, 12))                                  # tiny but isolated: exempt
    w.add_button("Delete", (20, 260, 80, 30))                              # duplicate siblings
    w.add_button("Delete", (110, 260, 80, 30))
    w.add_list("Files", (300, 200, 200, 100), items=["a.txt", "a.txt"])   # duplicate data rows: fine
    w.add_button("Hidden", (20, 500, 80, 30), visible=False)
    clean = be.add_window("Clean", (820, 0, 400, 300), app="clean", activate=False)
    clean.add_button("OK", (20, 20, 80, 30))
    clean.add_edit("Name", (20, 60, 200, 28))
    return be, w, clean


def desk(be):
    return Desktop(be, sleep=lambda s: None, default_timeout=0)


def by_rule(report):
    out = {}
    for f in report.findings:
        out.setdefault(f.rule, []).append(f)
    return out


def test_each_rule_fires_once_on_the_form():
    be, w, _ = form()
    r = audit(desk(be), "Form")
    rules = by_rule(r)
    assert r.windows == ["Form"]
    assert [f.severity for f in rules["unnamed-control"]] == ["error", "warning"]
    assert "Print" in rules["unnamed-control"][1].message
    [inp] = rules["unlabeled-input"]
    assert inp.severity == "error" and inp.role == "edit" and "'Email:'" in inp.message
    [img] = rules["unnamed-image"]
    assert img.severity == "info" and img.bounds.width == 40
    [nf] = rules["not-focusable"]
    assert nf.role == "togglebutton" and nf.name == "Bold"
    assert sorted(f.name for f in rules["small-target"]) == ["+", "-"]
    [dup] = rules["duplicate-name"]
    assert dup.name == "Delete" and "2 sibling buttons" in dup.message
    assert "offscreen-visible" not in rules and "unnamed-window" not in rules


def test_findings_carry_a_selector_that_finds_the_element_again():
    be, w, _ = form()
    d = desk(be)
    r = audit(d, "Form")
    roots = d.roots()
    for f in r.findings:
        assert f.selector, f
        node = parse(f.selector).find_first(roots)
        assert node is not None and node.bounds == f.bounds, (f.selector, f.element)
        assert f.window == "Form" and f.element.startswith(f.role)


def test_clean_window_has_no_findings_and_does_not_fail():
    be, _, _ = form()
    r = audit(desk(be), "Clean")
    assert r.findings == [] and not r.failed("warning") and not r.failed("error")
    assert r.nodes == 3


def test_auditing_every_window_and_document_order():
    be, _, _ = form()
    r = audit(desk(be))
    assert sorted(r.windows) == ["Clean", "Form"]
    assert all(f.window == "Form" for f in r.findings)
    tops = [f.bounds.top for f in r.findings]
    assert tops[0] == 20  # first control in the tree comes first


def test_backend_without_focusability_skips_the_rule(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(lambda self: orig.fget(self) - {"focusable", "focused"}))
    be, _, _ = form()
    r = audit(desk(be), "Form")
    assert "not-focusable" not in by_rule(r)
    assert any("focusability" in n for n in r.notes)


def test_caption_buttons_and_roving_children_are_not_flagged_unfocusable():
    be = MockBackend()
    w = be.add_window("App", (0, 0, 600, 400))
    w.add("togglebutton", "Close", (560, 0, 40, 30), automation_id="Close")
    tb = w.add("toolbar", "Tools", (0, 40, 600, 40))
    tb.add("togglebutton", "Brush", (10, 45, 30, 30))
    w.add_button("OK", (20, 300, 80, 30))  # focusable: the backend reports focusability
    r = audit(desk(be), "App", rules=["not-focusable"])
    assert r.findings == []


def test_unnamed_window_and_disabled_or_hidden_controls():
    be = MockBackend()
    w = be.add_window("", (0, 0, 400, 300))
    w.add_button("", (20, 20, 80, 30), enabled=False)  # still unnamed: reported
    w.add_button("", (20, 60, 80, 30), visible=False)   # hidden: ignored
    w.add("togglebutton", "T", (20, 100, 40, 40), enabled=False)  # disabled: focus not required
    r = audit(desk(be))  # every window
    rules = by_rule(r)
    assert len(rules["unnamed-window"]) == 1
    assert len(rules["unnamed-control"]) == 1
    assert "not-focusable" not in rules


def test_offscreen_but_inside_visible_parent(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: (orig.fget(self) - {"visible"}) | {"offscreen"} if self.widget.attrs.get("offscreen")
        else orig.fget(self)))
    be = MockBackend()
    w = be.add_window("Win", (0, 0, 400, 300))
    w.add_button("Ghost", (20, 20, 80, 30), attrs={"offscreen": True})
    w.add_button("Far", (20, 20, 900, 30), attrs={"offscreen": True})  # sticks out of the window: scrolled
    r = audit(desk(be), "Win")
    [f] = by_rule(r)["offscreen-visible"]
    assert f.name == "Ghost" and f.severity == "info"


def test_clipped_gallery_row_is_not_a_small_target():
    be = MockBackend()
    w = be.add_window("Gallery", (0, 0, 400, 300))
    g = w.add("group", "Shapes", (10, 10, 200, 60))
    for i in range(4):
        g.add("button", f"Shape {i}", (10 + i * 26, 63, 25, 7))  # bottom row cut off by the group's edge
    r = audit(desk(be), "Gallery", rules=["small-target"])
    assert r.findings == []


def test_min_target_size_parameter():
    be, _, _ = form()
    assert "small-target" not in by_rule(audit(desk(be), "Form", min_target_size=10))
    assert "small-target" not in by_rule(audit(desk(be), "Form", min_target_size=0))
    big = by_rule(audit(desk(be), "Form", min_target_size=40))["small-target"]
    assert {"+", "-"} <= {f.name for f in big}


def test_rules_filter_and_validation():
    be, _, _ = form()
    d = desk(be)
    r = audit(d, "Form", rules=["duplicate-name"])
    assert {f.rule for f in r.findings} == {"duplicate-name"}
    with pytest.raises(ValueError, match="unknown rule"):
        audit(d, "Form", rules=["nope"])
    with pytest.raises(ValueError):
        audit(d, "Form", max_depth=-1)
    with pytest.raises(WindowNotFound):
        audit(d, "No such window")


def test_max_depth_and_time_budget_truncate():
    be, _, _ = form()
    d = desk(be)
    shallow = audit(d, "Form", max_depth=0)
    assert shallow.nodes == 1 and shallow.findings == []
    ticks = iter(range(1000))
    r = audit(d, "Form", time_budget=5, clock=lambda: next(ticks))
    assert r.truncated and any("time budget" in n for n in r.notes)
    r = audit(d, "Form", max_nodes=3)
    assert r.truncated and r.nodes == 3


def test_selectors_can_be_disabled():
    be, _, _ = form()
    r = audit(desk(be), "Form", selectors=False)
    assert r.findings and all(f.selector is None for f in r.findings)


def test_audit_accepts_window_element_and_node():
    be, w, _ = form()
    d = desk(be)
    win = d.window("Form", timeout=0)
    n1 = len(audit(d, win).findings)
    assert len(audit(d, win.element).findings) == n1
    assert len(audit(d, win.element.node).findings) == n1


def test_report_api_and_json():
    be, _, _ = form()
    r = audit(desk(be), "Form")
    c = r.counts()
    assert c["error"] == 2 and c["info"] == 1 and c["warning"] == len(r.findings) - 3
    assert r.failed("error") and r.failed("warning") and not r.failed("never")
    assert len(r.at_least("warning")) == c["error"] + c["warning"]
    with pytest.raises(ValueError):
        r.at_least("fatal")
    data = json.loads(json.dumps(r.to_dict()))
    assert data["counts"] == c and data["findings"][0]["bounds"]["width"] == 80
    text = r.format_text()
    assert text.splitlines()[0] == "window 'Form':" and "unnamed-control" in text
    assert text.splitlines()[-1] == r.summary()
    assert set(RULES) >= {f.rule for f in r.findings}
    empty = AuditReport()
    assert "0 findings" in empty.summary()
    assert Finding("x", "info", "m", "button").to_dict()["bounds"] is None


def test_audit_is_read_only():
    be, w, _ = form()
    before = list(be.events)
    audit(desk(be))
    assert be.events == before  # no mouse/keyboard event, no native action
    assert all(x.clicks == 0 for x in w.widgets)


# ============================================================================ CLI


def run(argv, d):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), desktop=d, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_exit_codes_follow_fail_on():
    be, _, _ = form()
    d = desk(be)
    assert run(["a11y-audit", "Form"], d)[0] == cli.EXIT_FAILURE
    assert run(["a11y-audit", "Form", "--fail-on", "never"], d)[0] == cli.EXIT_OK
    assert run(["a11y-audit", "Clean", "--fail-on", "warning"], d)[0] == cli.EXIT_OK
    # errors remain without the small-target warnings
    assert run(["a11y-audit", "Form", "--min-target-size", "0", "--fail-on", "error"], d)[0] == cli.EXIT_FAILURE


def test_cli_warning_only_window():
    be = MockBackend()
    w = be.add_window("Toolbox", (0, 0, 300, 200))
    w.add_button("A", (10, 10, 10, 10))
    w.add_button("B", (22, 10, 10, 10))
    d = desk(be)
    assert run(["a11y-audit", "Toolbox"], d)[0] == cli.EXIT_OK
    assert run(["a11y-audit", "Toolbox", "--fail-on", "warning"], d)[0] == cli.EXIT_FAILURE


def test_cli_text_and_json_output():
    be, _, _ = form()
    d = desk(be)
    code, out, _ = run(["a11y-audit", "Form"], d)
    assert "window 'Form':" in out and "error   unnamed-control" in out and "->" in out
    assert "findings (2 errors" in out
    code, out, _ = run(["a11y-audit", "Form", "--json", "--no-selectors"], d)
    data = json.loads(out)
    assert code == cli.EXIT_FAILURE and data["failed"] is True and data["fail_on"] == "error"
    assert data["windows"] == ["Form"] and all(f["selector"] is None for f in data["findings"])
    code, out, _ = run(["--json", "a11y-audit", "Clean"], d)
    assert code == cli.EXIT_OK and json.loads(out)["findings"] == []


def test_cli_usage_and_not_found_errors():
    be, _, _ = form()
    d = desk(be)
    assert run(["a11y-audit", "--max-depth", "-1"], d)[0] == cli.EXIT_USAGE
    assert run(["a11y-audit", "--min-target-size", "-5"], d)[0] == cli.EXIT_USAGE
    assert run(["a11y-audit", "--fail-on", "fatal"], d)[0] == cli.EXIT_USAGE
    code, _, err = run(["a11y-audit", "Nope"], d)
    assert code == cli.EXIT_FAILURE and "Nope" in err


def test_cli_demo_desktop_is_clean_and_help_documents_options():
    code, out, _ = run(["--backend", "mock", "a11y-audit"], None)
    assert code == cli.EXIT_OK and "0 findings" in out
    out = io.StringIO()
    assert cli.main(["a11y-audit", "--help"], stdout=out, stderr=io.StringIO()) == 0
    for opt in ("WINDOW", "--json", "--max-depth", "--min-target-size", "--fail-on", "--time-budget"):
        assert opt in out.getvalue()
    assert "a11y-audit" in build_choices()


def build_choices():
    parser = cli.build_parser()
    return parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
