"""locum.pytest_plugin: fixtures, options and traces of failing tests (inner runs on the mock backend)."""

import os
import subprocess
import sys

import pytest

from locum.pytest_plugin import trace_path_for

pytest_plugins = ["pytester"]

MOCK = ("--locum-backend", "mock", "-p", "no:cacheprovider")


def test_plugin_is_registered(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("locum")


def test_importing_the_plugin_creates_no_desktop_and_loads_no_backend():
    code = ("import sys, locum.pytest_plugin; "
            "bad = [m for m in sys.modules if m.startswith(('locum.backends', 'locum.desktop', 'PIL', 'numpy', 'mss'))]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_trace_path_is_sanitized(tmp_path):
    p = trace_path_for("tests/test_x.py::test_a[1-<b>/c]", str(tmp_path))
    assert os.path.dirname(p) == str(tmp_path)
    assert os.path.basename(p) == "tests_test_x.py_test_a_1-_b_c.html"
    long = trace_path_for("t.py::" + "x" * 500, str(tmp_path))
    assert len(os.path.basename(long)) <= 160
    assert long != trace_path_for("t.py::" + "x" * 499, str(tmp_path))


def test_desktop_fixture_on_mock_and_failure_trace(pytester):
    pytester.makepyfile(
        """
        from locum.backends.mock import MockBackend

        def _scene(desktop):
            w = desktop.backend.add_window("Invoice", (100, 100, 400, 300))
            w.add_button("OK", (20, 240, 80, 30))

        def test_passes(desktop):
            assert isinstance(desktop.backend, MockBackend)
            assert desktop.failsafe.enabled is False
            _scene(desktop)
            desktop.click('button "OK"')

        def test_fails(desktop):
            _scene(desktop)
            desktop.click('button "OK"')
            desktop.find('button "Missing"', timeout=0)
        """
    )
    result = pytester.runpytest(*MOCK)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*locum trace*", "*trace saved:*test_fails.html*"])
    traces = sorted(os.listdir(pytester.path / ".locum-traces"))
    assert traces == ["test_desktop_fixture_on_mock_and_failure_trace.py_test_fails.html"]
    page = (pytester.path / ".locum-traces" / traces[0]).read_text(encoding="utf-8")
    assert "ElementNotFound" in page and "button &quot;Missing&quot;" in page


def test_trace_path_in_user_properties_and_junit(pytester):
    pytester.makepyfile(
        """
        def test_fails(desktop):
            desktop.press("a")
            assert False
        """
    )
    result = pytester.runpytest(*MOCK, "--junitxml=out.xml")
    result.assert_outcomes(failed=1)
    xml = (pytester.path / "out.xml").read_text(encoding="utf-8")
    assert 'name="locum_trace"' in xml and "test_fails.html" in xml


def test_trace_modes_and_dir(pytester):
    pytester.makepyfile(
        """
        def test_ok(desktop):
            desktop.press("a")

        def test_no_desktop():
            pass
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=2)
    assert not (pytester.path / ".locum-traces").exists()  # failed mode: nothing failed

    pytester.runpytest(*MOCK, "--locum-trace", "on", "--locum-trace-dir", "out/tr").assert_outcomes(passed=2)
    assert os.listdir(pytester.path / "out" / "tr") == ["test_trace_modes_and_dir.py_test_ok.html"]


def test_trace_off_and_tracer_fixture(pytester):
    pytester.makepyfile(
        """
        def test_tracer(locum_tracer, desktop):
            desktop.press("a")
            assert [s.method for s in locum_tracer.steps] == ["press"]

        def test_fails(desktop):
            assert False
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=1, failed=1)
    pytester.makepyfile(
        test_off="""
        def test_tracer(locum_tracer, desktop):
            assert locum_tracer is None
            assert "click" not in vars(desktop)

        def test_fails(desktop):
            assert False
        """
    )
    result = pytester.runpytest(*MOCK, "--locum-trace", "off", "test_off.py")
    result.assert_outcomes(passed=1, failed=1)
    assert "locum trace" not in result.stdout.str()


def test_ini_options(pytester):
    pytester.makeini(
        """
        [pytest]
        locum_backend = mock
        locum_trace = on
        locum_trace_dir = ini-traces
        """
    )
    pytester.makepyfile(
        """
        def test_ok(desktop):
            assert desktop.backend.name == "mock"
        """
    )
    env_backend = os.environ.pop("LOCUM_BACKEND", None)
    try:
        pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)
    finally:
        if env_backend is not None:
            os.environ["LOCUM_BACKEND"] = env_backend
    assert len(os.listdir(pytester.path / "ini-traces")) == 1


def test_app_fixture_launches_waits_and_closes_only_its_windows(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def fake_launch(desktop):
            be = desktop.backend
            be.add_window("Other", (0, 0, 200, 200), app="other")

            def launch(command, **kw):
                be.launched.append(command)
                be.add_window(command.title() + " window", (300, 100, 300, 200), app=command, pid=777)

            be.launch = launch
            yield be
            # Runs after app's teardown: both launched windows are closed, the other one is left alone.
            assert [w.title for w in be.windows] == ["Other"]

        def test_app(fake_launch, app):
            w = app("calc", window="Calc window", timeout=1)
            assert w.title == "Calc window"
            any_new = app("notes")
            assert any_new.title == "Notes window"
            assert fake_launch.launched == ["calc", "notes"]

        def test_app_window_closed_by_the_test(fake_launch, app):
            app("calc", window="Calc window").close()
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=2)


def test_app_fixture_timeout_fails_with_a_trace(pytester):
    pytester.makepyfile(
        """
        def test_app(app):
            app("nothing.exe", window="Never", timeout=0.1)
        """
    )
    result = pytester.runpytest(*MOCK)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*WindowNotFound*", "*trace saved:*"])
    page = (pytester.path / ".locum-traces" / "test_app_fixture_timeout_fails_with_a_trace.py_test_app.html").read_text(encoding="utf-8")
    assert "launch" in page and "WindowNotFound" in page


def test_local_fixtures_override_the_plugin(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def desktop():
            return "local desktop"

        @pytest.fixture
        def app():
            return "local app"

        def test_local(desktop, app):
            assert desktop == "local desktop" and app == "local app"
        """
    )
    pytester.runpytest(*MOCK).assert_outcomes(passed=1)


def test_bad_backend_is_a_fixture_error(pytester):
    pytester.makepyfile(
        """
        def test_x(desktop):
            pass
        """
    )
    result = pytester.runpytest("--locum-backend", "nosuch", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*Unknown backend 'nosuch'*"])
