"""The recorder's selector capture: a shared prefix cache keeps it fast and correct across a session."""

from __future__ import annotations

import ast
import logging

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.rpa import Recorder, run_flow
from locum.rpa.recorder import _SelectorCache
from locum.selectors import parse


def scene():
    be = MockBackend(width=1600, height=900)
    a = be.add_window("Orders", (0, 0, 500, 400), app="erp")
    a.add_button("Save", (20, 20, 80, 30))
    a.add_edit("Customer", (20, 60, 200, 28))
    b = be.add_window("Mail", (520, 0, 500, 400), app="mail")
    b.add_button("Send", (20, 20, 80, 30))
    c = be.add_window("Notes", (1040, 0, 500, 400), app="notes")
    c.add_button("Save", (20, 20, 80, 30))
    return be, a, b, c


def recorder(be, **kw):
    return Recorder(Desktop(be, sleep=lambda s: None, default_timeout=0), **kw).start(source="manual")


def click(rec, widget):
    p = widget.screen_rect.center
    rec.feed(("down", "left", p.x, p.y))
    rec.feed(("up", "left", p.x, p.y))


class WalkCounter:
    """Counts MockNode.children() calls (one per visited node in a tree walk)."""

    def __init__(self, monkeypatch):
        self.n = 0
        orig = mock.MockNode.children

        def counted(node):
            self.n += 1
            return orig(node)

        monkeypatch.setattr(mock.MockNode, "children", counted)


def test_selectors_resolve_to_the_clicked_element_in_each_window():
    be, a, b, c = scene()
    rec = recorder(be)
    for w in (a.find("Save"), b.find("Send"), c.find("Save"), a.find("Customer")):
        click(rec, w)
    acts = rec.stop()
    assert [x.selector for x in acts] == [
        'window "Orders" button "Save"',
        'window "Mail" button "Send"',
        'window "Notes" button "Save"',
        'window "Orders" edit "Customer"',
    ]
    roots = be.accessibility_roots()
    for act, w in zip(acts, (a.find("Save"), b.find("Send"), c.find("Save"), a.find("Customer"))):
        assert parse(act.selector).find_first(roots).widget is w


def test_window_prefixes_are_proven_once_for_the_whole_session(monkeypatch):
    be, a, b, c = scene()
    rec = recorder(be)
    click(rec, a.find("Save"))  # warms the prefixes of all three windows in one walk
    cache = next(iter(rec._sel_caches.values()))
    assert {e[1] for e in cache.entries} == {'window "Orders"', 'window "Mail"', 'window "Notes"'}
    assert all(e[2] is True for e in cache.entries)
    walks = WalkCounter(monkeypatch)
    click(rec, b.find("Send"))
    click(rec, c.find("Save"))
    # Only the clicked windows are searched now (each has 2-3 nodes), never the whole desktop again.
    assert walks.n <= 12
    rec.stop()


def test_retitled_window_reproves_only_its_own_prefix():
    be, a, b, c = scene()
    rec = recorder(be)
    click(rec, a.find("Save"))
    b.title = b.root.name = "Mail (1 unread)"
    click(rec, b.find("Send"))
    click(rec, a.find("Customer"))
    acts = rec.stop()
    assert acts[1].selector == 'window "Mail (1 unread)" button "Send"'
    assert acts[2].selector == 'window "Orders" edit "Customer"'
    cache = next(iter(rec._sel_caches.values()))
    assert sorted(e[1] for e in cache.entries) == ['window "Mail (1 unread)"', 'window "Notes"', 'window "Orders"']


def test_same_titled_window_opened_mid_session_invalidates_the_unique_prefix():
    be, a, b, c = scene()
    rec = recorder(be)
    click(rec, a.find("Save"))
    assert rec.actions[0].selector == 'window "Orders" button "Save"'
    twin = be.add_window("Orders", (0, 450, 500, 400), app="erp", activate=False)
    twin.add_button("Save", (20, 20, 80, 30))
    click(rec, twin.find("Save"))
    click(rec, a.find("Save"))
    acts = rec.stop()
    roots = be.accessibility_roots()
    # Both selectors now disambiguate the two "Orders" windows, and find the right one.
    assert parse(acts[1].selector).find_first(roots).widget is twin.find("Save")
    assert parse(acts[2].selector).find_first(roots).widget is a.find("Save")
    assert acts[1].selector != acts[2].selector


def test_closed_window_entry_is_dropped():
    cache = _SelectorCache()
    be, a, b, c = scene()
    roots = be.accessibility_roots()
    entries = cache.refresh(roots)
    assert len(entries) == 3
    be.close_window(b.handle)
    entries = cache.refresh(be.accessibility_roots())
    assert sorted(e[1] for e in entries) == ['window "Notes"', 'window "Orders"']


def test_unique_entries_survive_unrelated_changes_and_non_unique_ones_do_not():
    be, a, b, c = scene()
    cache = _SelectorCache()
    roots = be.accessibility_roots()
    cache.refresh(roots)
    fake = (roots[0], 'window "X":nth(1)', False)
    cache.entries.append(fake)
    be.add_window("Calendar", (0, 450, 300, 300), activate=False)
    entries = cache.refresh(be.accessibility_roots())
    assert fake not in entries  # positions may have shifted
    # The proven unique prefixes are kept (a lone new window is proven by selector_for on demand).
    assert sorted(e[1] for e in entries if e[2]) == ['window "Mail"', 'window "Notes"', 'window "Orders"']


def test_new_window_sharing_a_long_title_prefix_drops_the_entry():
    """Long titles are matched by their first 60 characters (name^=): a new window starting
    the same way may match that prefix too."""
    be = MockBackend()
    long_title = "Quarterly report for the finance department, fiscal year 2026, draft 1 (final review)"
    assert len(long_title) > 80
    be.add_window(long_title, (0, 0, 300, 300))
    be.add_window("Other", (310, 0, 300, 300))
    cache = _SelectorCache()
    cache.refresh(be.accessibility_roots())
    assert len(cache.entries) == 2
    be.add_window(long_title[:60] + " - copy", (0, 310, 300, 300), activate=False)
    entries = cache.refresh(be.accessibility_roots())
    assert [e[1] for e in entries] == ['window "Other"']


def test_backend_session_replays_with_cached_selectors():
    be, a, b, c = scene()
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")
    d.click('window "Orders" edit "Customer"')
    d.type("ACME")
    d.click('window "Notes" button "Save"')
    d.click('window "Mail" button "Send"')
    rec.stop()
    flow = rec.to_flow()
    assert [list(s)[0] for s in flow["steps"]] == ["click", "type", "click", "click"]
    assert flow["steps"][1] == {"type": {"text": "ACME", "into": 'window "Orders" edit "Customer"'}}
    be2, a2, b2, c2 = scene()
    report = run_flow(flow, Desktop(be2, sleep=lambda s: None, default_timeout=0))
    assert report.ok, [s.error for s in report.failures]
    assert a2.find("Customer").value == "ACME"
    assert c2.find("Save").clicks == 1 and b2.find("Send").clicks == 1 and a2.find("Save").clicks == 0


def test_generated_script_is_valid_python_using_selectors_and_coordinates_only_as_fallback():
    be, a, b, c = scene()
    rec = recorder(be)
    click(rec, a.find("Save"))
    rec.feed(("down", "left", 1590, 890))  # empty desktop: no element
    rec.feed(("up", "left", 1590, 890))
    code = rec.to_python()
    tree = ast.parse(code)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "click"]
    args = [ast.literal_eval(cl.args[0]) for cl in calls]
    assert args == ['window "Orders" button "Save"', (1590, 890)]


def test_password_never_reaches_logs_flow_or_script(monkeypatch, caplog):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))
    be = MockBackend()
    w = be.add_window("Login", (0, 0, 400, 300))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    rec = Recorder(d).start(source="backend")

    def boom(*a, **k):
        raise RuntimeError("processing failed")

    be._set_focus(pwd)
    monkeypatch.setattr(rec, "_on_text", boom)
    with caplog.at_level(logging.DEBUG, logger="locum.recorder"):
        rec._feed_hooked(("type", "hunter2"))  # a failing event is logged without its content
    monkeypatch.undo()
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))
    for ch in "hunter2":
        rec.feed(("type", ch))
    rec.stop()
    assert "hunter2" not in caplog.text and "type event" in caplog.text
    code = rec.to_python()
    assert "hunter2" not in code and "os.environ['LOCUM_PASSWORD']" in code
    assert "LOCUM_PASSWORD environment variable" in code
    compile(code, "rec.py", "exec")
    assert "hunter2" not in str(rec.to_flow())


def test_selector_failure_falls_back_to_coordinates(monkeypatch):
    be, a, b, c = scene()
    rec = recorder(be)
    import locum.selectors as sel_mod

    monkeypatch.setattr(sel_mod, "selector_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    click(rec, a.find("Save"))
    act = rec.stop()[0]
    p = a.find("Save").screen_rect.center
    assert act.selector is None and (act.x, act.y) == (p.x, p.y)
    assert act.to_flow_step() == {"click": f"{p.x},{p.y}"}


def test_new_session_starts_with_a_fresh_cache():
    be, a, b, c = scene()
    rec = recorder(be)
    click(rec, a.find("Save"))
    rec.stop()
    assert rec._sel_caches
    rec.start(source="manual")
    assert rec._sel_caches == {}
    rec.stop()


@pytest.mark.parametrize("use_selectors", [True, False])
def test_cli_record_no_selectors_option(tmp_path, monkeypatch, use_selectors):
    from locum import cli
    from locum.rpa import recorder as rec_mod

    def fake_start(self):
        for ev in (("down", "left", 370, 99), ("up", "left", 370, 99)):
            self.feed(ev)

    monkeypatch.setattr(rec_mod.Recorder, "_start_pynput", fake_start)
    be = cli.DemoBackend()
    desk = Desktop(be, sleep=lambda s: None, default_timeout=0)
    out = tmp_path / "r.json"
    argv = ["record", "--source", "pynput", "-o", str(out), "--duration", "0", "--name", "demo"]
    if not use_selectors:
        argv.append("--no-selectors")
    import io
    import json

    code = cli.main(argv, desktop=desk, stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 0
    flow = json.loads(out.read_text(encoding="utf-8"))
    assert flow["name"] == "demo"
    target = flow["steps"][0]["click"]
    if use_selectors:
        assert "window" in target
    else:
        assert target == "370,99"


def test_cli_record_help_documents_options():
    import io

    from locum import cli

    out = io.StringIO()
    assert cli.main(["record", "--help"], stdout=out, stderr=io.StringIO()) == 0
    text = out.getvalue()
    for opt in ("--output", "--duration", "--no-selectors", "--name", "--source", "LOCUM_PASSWORD"):
        assert opt in text
