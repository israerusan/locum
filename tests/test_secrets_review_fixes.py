"""Regression tests for review findings: secrets in traces and recordings, plugin
fixture names / screenshot policy, and heal decisions on truncated scans.

Everything runs on the MockBackend (the recorder uses the ``manual`` and
``backend`` sources only; nothing touches the real desktop).
"""

from __future__ import annotations

import os

import pytest

from locum.backends import mock
from locum.backends.mock import MockBackend
from locum.desktop import Desktop
from locum.heal import HealOptions, heal
from locum.rpa.recorder import Recorder
from locum.trace import MASK, Tracer

pytest_plugins = ["pytester"]

SECRET = "hunter2-Secret"
MOCK = ("--locum-backend", "mock", "-p", "no:cacheprovider")


@pytest.fixture
def password_states(monkeypatch):
    orig = mock.MockNode.states
    monkeypatch.setattr(mock.MockNode, "states", property(
        lambda self: orig.fget(self) | ({"password"} if self.widget.attrs.get("password") else set())))


@pytest.fixture
def login(password_states):
    be = MockBackend(width=800, height=600)
    w = be.add_window("Login", (0, 0, 400, 300))
    user = w.add("edit", "User", (20, 20, 200, 30))
    pwd = w.add("edit", "Password", (20, 60, 200, 30), attrs={"password": True})
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, user, pwd


def _outputs(rec):
    return rec.to_python() + "\n" + repr(rec.to_flow()) + "\n" + repr([a.to_dict() for a in rec.actions])


# ============================================================ 6a: trace reads

def test_trace_masks_clipboard_reads(login):
    be, d, _user, _pwd = login
    be.clipboard = SECRET
    with Tracer(d, screenshots=False) as t:
        assert d.clipboard_get() == SECRET
    step = t.steps[0]
    assert step.method == "clipboard_get"
    assert step.result.startswith(MASK)
    assert SECRET not in t.to_json(screenshots=False)
    assert SECRET not in t.to_html()


def test_trace_masks_password_values_read_through_traced_calls(login):
    be, d, _user, pwd = login
    pwd.value = SECRET
    with Tracer(d, screenshots=False) as t:
        el = d.find('edit "Password"')
        # A predicate returning the field's value: the step result must not keep it.
        assert d.wait_until(lambda: el.value) == SECRET
    assert t.steps[1].method == "wait_until"
    assert t.steps[1].result == MASK
    assert SECRET not in t.to_json(screenshots=False)
    assert SECRET not in t.to_html()


def test_trace_keeps_ordinary_results(login):
    _be, d, user, _pwd = login
    user.value = "ada"
    with Tracer(d, screenshots=False) as t:
        d.find('edit "User"')
    assert "User" in t.steps[0].result


# ============================================================ 6b: recorder fails closed

@pytest.mark.parametrize("how", ["raises", "none", "states_error"])
def test_recorder_treats_unknown_focus_as_secret(login, monkeypatch, how):
    be, d, user, _pwd = login
    if how == "raises":
        monkeypatch.setattr(be, "focused_element", lambda: (_ for _ in ()).throw(RuntimeError("no UIA")))
    elif how == "none":
        be._set_focus(None)
    else:
        be._set_focus(user)
        monkeypatch.setattr(mock.MockNode, "states", property(lambda self: (_ for _ in ()).throw(OSError("COM"))))
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", SECRET))
    rec.stop()
    out = _outputs(rec)
    assert SECRET not in out
    code = rec.to_python()
    assert "os.environ[" in code and "focused field could not be determined" in code
    compile(code, "rec.py", "exec")
    step = rec.to_flow()["steps"][0]
    assert "${env." in str(step)


def test_recorder_keeps_text_when_focus_is_known_non_password(login):
    be, d, user, _pwd = login
    be._set_focus(user)
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", "ada"))
    rec.stop()
    assert rec.actions[0].text == "ada" and not rec.actions[0].secret
    assert "type('ada')" in rec.to_python()


def test_recorder_unknown_focus_can_be_trusted_explicitly(login, monkeypatch):
    be, d, _user, _pwd = login
    be._set_focus(None)
    rec = Recorder(d, trust_unknown_focus=True).start(source="manual")
    rec.feed(("type", "ada"))
    rec.stop()
    assert rec.actions[0].text == "ada" and not rec.actions[0].secret


# ============================================================ 6c: focus moves mid-burst

def test_recorder_tab_then_password_with_key_events(login):
    be, d, user, pwd = login
    be._set_focus(user)
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", "ada"))
    rec.feed(("key_down", "tab"))
    rec.feed(("key_up", "tab"))
    be._set_focus(pwd)
    rec.feed(("type", SECRET))
    rec.stop()
    assert SECRET not in _outputs(rec)
    assert "type('ada')" in rec.to_python()


def test_recorder_tab_inside_one_text_event_splits_the_burst(login):
    be, d, user, _pwd = login
    be._set_focus(user)
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", "ada\t" + SECRET))  # focus after the Tab is unknown at feed time
    rec.stop()
    out = _outputs(rec)
    assert SECRET not in out
    kinds = [(a.kind, a.text, a.secret) for a in rec.actions]
    assert kinds[0] == ("type", "ada\t", False)
    assert kinds[-1][2] is True


def test_recorder_focus_change_without_a_key_splits_the_burst(login):
    be, d, user, pwd = login
    be._set_focus(user)
    rec = Recorder(d).start(source="manual")
    rec.feed(("type", "ada"))
    be._set_focus(pwd)  # e.g. an auto-advancing form, or a native focus() call
    for ch in SECRET:
        rec.feed(("type", ch))
    rec.stop()
    assert SECRET not in _outputs(rec)
    texts = [(a.text, a.secret) for a in rec.actions]
    assert texts == [("ada", False), ("", True)]


def test_recorder_backend_source_type_with_tab(login):
    be, d, user, pwd = login
    be._set_focus(user)
    rec = Recorder(d).start(source="backend")
    d.type("ada\t" + SECRET)
    rec.stop()
    assert SECRET not in _outputs(rec)
    assert "'ada'" in rec.to_python()


def test_recorder_backend_type_text_hook_uses_focus_before_typing(login, monkeypatch):
    """The type_text hook feeds after typing; a Tab inside the text must not attribute
    the password typed *before* it to the field focused *after* it."""
    be, d, user, pwd = login
    orig = be.type_text

    def moving_type_text(text):  # a backend that does move the focus on a typed Tab
        head, sep, tail = text.partition("\t")
        orig(head)
        if sep:
            be._set_focus(user)
            orig(tail)

    monkeypatch.setattr(be, "type_text", moving_type_text)
    be._set_focus(pwd)
    rec = Recorder(d).start(source="backend")
    be.type_text(SECRET + "\tada")
    rec.stop()
    assert SECRET not in _outputs(rec)


# ============================================================ pytest plugin

def test_plugin_fixtures_are_namespaced(pytester):
    pytester.makepyfile(
        """
        import pytest

        def test_names(request, locum_desktop, locum_app):
            assert locum_desktop.backend.name == "mock"
            assert callable(locum_app)
            for name in ("desktop", "app"):
                with pytest.raises(pytest.FixtureLookupError):
                    request.getfixturevalue(name)
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=1)


def test_plugin_does_not_shadow_a_third_party_app_fixture(pytester):
    pytester.makeconftest(
        """
        import pytest

        class WebPlugin:
            @pytest.fixture
            def app(self):
                return "flask app"

        def pytest_configure(config):
            config.pluginmanager.register(WebPlugin(), "fake-web-plugin")
        """
    )
    pytester.makepyfile(
        """
        def test_web(app):
            assert app == "flask app"
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=1)


@pytest.mark.parametrize("how", [("--locum-aliases",), ("ini",)])
def test_short_aliases_are_opt_in(pytester, how):
    if how == ("ini",):
        pytester.makeini("[pytest]\nlocum_fixture_aliases = true\n")
        args = MOCK
    else:
        args = MOCK + how
    pytester.makepyfile(
        """
        def test_alias(desktop, app, locum_desktop):
            assert desktop is locum_desktop
            assert callable(app)
        """
    )
    pytester.runpytest(*args).assert_outcomes(passed=1)


def test_default_screenshots_only_on_failure(pytester):
    pytester.makepyfile(
        """
        def _scene(d):
            w = d.backend.add_window("Invoice", (100, 100, 400, 300))
            w.add_button("OK", (20, 240, 80, 30))

        def test_steps_without_screenshots(locum_desktop, locum_tracer):
            _scene(locum_desktop)
            locum_desktop.click('button "OK"')
            locum_desktop.press("a")
            assert len(locum_tracer.steps) == 2
            assert all(s.screenshot is None for s in locum_tracer.steps)

        def test_fails_in_assert(locum_desktop, locum_tracer):
            _scene(locum_desktop)
            locum_desktop.click('button "OK"')
            assert False

        def test_fails_in_step(locum_desktop):
            _scene(locum_desktop)
            locum_desktop.click('button "OK"')
            locum_desktop.find('button "Missing"', timeout=0)
        """
    )
    result = pytester.runpytest(*MOCK, "--locum-trace", "on", "--locum-trace-dir", "tr")
    result.assert_outcomes(passed=1, failed=2)
    tr = pytester.path / "tr"
    ok = next(tr.glob("test_default_screenshots_only_on_failure.py_test_steps_without_screenshots-*.html")).read_text("utf-8")
    assert "data:image/jpeg" not in ok
    in_assert = next(tr.glob("test_default_screenshots_only_on_failure.py_test_fails_in_assert-*.html")).read_text("utf-8")
    assert in_assert.count("data:image/jpeg") == 1 and "Screen at failure" in in_assert
    in_step = next(tr.glob("test_default_screenshots_only_on_failure.py_test_fails_in_step-*.html")).read_text("utf-8")
    # The failing step's screenshot, plus the final one.
    assert in_step.count("data:image/jpeg") == 2


@pytest.mark.parametrize("mode,expected", [("all", 3), ("none", 0)])
def test_screenshots_option(pytester, mode, expected):
    pytester.makepyfile(
        """
        def test_fails(locum_desktop):
            locum_desktop.press("a")
            locum_desktop.press("b")
            assert False
        """
    )
    pytester.runpytest(*MOCK, "--locum-screenshots", mode, "--locum-trace-dir", "tr").assert_outcomes(failed=1)
    page = (pytester.path / "tr" / os.listdir(pytester.path / "tr")[0]).read_text("utf-8")
    assert page.count("data:image/jpeg") == expected


# ============================================================ heal: truncated scans

@pytest.fixture
def crowded():
    be = MockBackend(width=1000, height=700)
    w = be.add_window("Editor", (0, 0, 900, 600), app="editor")
    w.add_button("Save as", (20, 20, 80, 30))  # early: plausible, but not the best match
    for i in range(30):
        w.add_text(f"filler {i}", (20, 60 + i * 2, 80, 2))
    w.add_button("Save", (500, 500, 80, 30), automation_id="saveBtn")  # scanned late
    return be


def test_heal_refuses_to_decide_on_a_truncated_scan(crowded):
    roots = crowded.accessibility_roots()
    # The full scan sees both and picks the exact word match "Save"...
    full = heal('button "Save!"', roots, log=False)
    assert full and full.node.name == "Save" and not full.truncated
    # ...a scan cut after 5 nodes only saw "Save as" and must not call it a clear winner.
    cut = heal('button "Save!"', roots, options=HealOptions(max_nodes=5), log=False)
    assert not cut
    assert cut.reason.startswith("scan truncated after 5 nodes")
    # Same when the real target is simply out of reach.
    sel = 'button#saveBtn[name="Sav"]'
    assert heal(sel, roots, log=False).node.name == "Save"
    cut = heal(sel, roots, options=HealOptions(max_nodes=5), log=False)
    assert not cut and cut.reason.startswith("scan truncated after 5 nodes")


def test_heal_partial_decisions_are_opt_in(crowded):
    roots = crowded.accessibility_roots()
    out = heal('button "Save!"', roots, options=HealOptions(max_nodes=5, allow_partial=True), log=False)
    assert out and out.node.name == "Save as"
    # A scan that fits within max_nodes is not truncated.
    full = heal('button "Save!"', roots, options=HealOptions(max_nodes=33), log=False)
    assert full and full.node.name == "Save" and not full.truncated
