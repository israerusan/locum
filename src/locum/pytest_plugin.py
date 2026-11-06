"""pytest plugin: ``desktop`` / ``app`` fixtures and a trace for every failing test.

Installed with locum and registered automatically (``pytest11`` entry point)::

    def test_calculator(app):
        calc = app("calc.exe", window="Calculator")
        calc.find('button "Seven"').click()

    def test_on_ci(desktop):             # pytest --locum-backend mock
        win = desktop.backend.add_window("Invoice", (100, 100, 400, 300))
        win.add_button("OK", (20, 240, 80, 30))
        desktop.click('button "OK"')

Options (command line, or the ini keys in brackets):

* ``--locum-backend NAME`` [``locum_backend``] -- ``mock`` (in-memory virtual
  desktop, for CI), ``windows``, ``macos``, ``x11``. Default: the
  ``LOCUM_BACKEND`` environment variable, else the ini key, else the platform's.
* ``--locum-trace {off,on,failed}`` [``locum_trace``] -- trace tests that use
  ``desktop`` and save the trace never, always, or (default) when the test fails.
* ``--locum-trace-dir DIR`` [``locum_trace_dir``] -- where traces go
  (default ``.locum-traces``, relative to the directory pytest was started in).

A saved trace is ``<dir>/<sanitized test id>.html`` (see :mod:`locum.trace`);
its path is added to the report's ``user_properties`` (``locum_trace``, which
JUnit XML includes) and shown in a "locum trace" section of the failure report.

Importing this module is cheap: no desktop or backend is created and no
backend module is imported until a test requests one of the fixtures.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Callable, Iterator, List, Optional

import pytest

__all__ = ["TRACE_MODES", "trace_path_for"]

#: Values of ``--locum-trace``.
TRACE_MODES = ("off", "on", "failed")

_TRACER_KEY = "_locum_tracer"
_TRACE_PATH_KEY = "_locum_trace_path"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("locum", "locum desktop automation")
    group.addoption("--locum-backend", dest="locum_backend", default=None, metavar="NAME",
                    help="Backend for the desktop/app fixtures: mock (CI), windows, macos, x11 "
                         "(default: $LOCUM_BACKEND, else the platform's).")
    group.addoption("--locum-trace", dest="locum_trace", default=None, choices=TRACE_MODES,
                    help="Save a locum trace of tests using the desktop fixture: off, on (always) "
                         "or failed (default).")
    group.addoption("--locum-trace-dir", dest="locum_trace_dir", default=None, metavar="DIR",
                    help="Directory for saved traces (default: .locum-traces).")
    parser.addini("locum_backend", "Default backend for the locum fixtures (overridden by $LOCUM_BACKEND).", default="")
    parser.addini("locum_trace", "locum trace mode: off, on or failed.", default="failed")
    parser.addini("locum_trace_dir", "Directory for saved locum traces.", default=".locum-traces")


# ---------------------------------------------------------------- settings

def _backend_name(config: Any) -> str:
    name = config.getoption("locum_backend") or os.environ.get("LOCUM_BACKEND") or config.getini("locum_backend")
    if not name:
        from .backends import platform_backend_name

        name = platform_backend_name()
    return str(name).strip().lower()


def _trace_mode(config: Any) -> str:
    mode = str(config.getoption("locum_trace") or config.getini("locum_trace") or "failed").strip().lower()
    if mode not in TRACE_MODES:
        raise pytest.UsageError(f"locum_trace must be one of {', '.join(TRACE_MODES)}, got {mode!r}")
    return mode


def _trace_dir(config: Any) -> str:
    d = config.getoption("locum_trace_dir") or config.getini("locum_trace_dir") or ".locum-traces"
    d = os.path.expanduser(str(d))
    if not os.path.isabs(d):
        d = os.path.join(str(config.invocation_params.dir), d)
    return d


def trace_path_for(nodeid: str, directory: str) -> str:
    """Where the trace of test ``nodeid`` is saved: ``<directory>/<sanitized nodeid>.html``.

    Characters other than letters, digits, ``.``, ``_`` and ``-`` become ``_``;
    very long ids are shortened and made unique with a hash.
    """
    name = _UNSAFE.sub("_", nodeid).strip("._") or "test"
    if len(name) > 150:
        digest = hashlib.sha1(nodeid.encode("utf-8")).hexdigest()[:10]
        name = name[:139] + "-" + digest
    return os.path.join(directory, name + ".html")


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def desktop(request: Any) -> Iterator[Any]:
    """A fresh :class:`~locum.desktop.Desktop` on the selected backend (see ``--locum-backend``).

    ``mock`` gets a new in-memory :class:`~locum.backends.mock.MockBackend` per test
    (reach it as ``desktop.backend`` to build the scene); real backends run with
    the screen-corner fail-safe on. The desktop is closed after the test (held
    keys and buttons are released). With ``--locum-trace`` other than ``off``
    the test is traced (:func:`locum_tracer`).
    """
    from .desktop import Desktop

    config = request.config
    name = _backend_name(config)
    if name == "mock":
        from .backends.mock import MockBackend

        d = Desktop(MockBackend(), failsafe=False)
    else:
        d = Desktop(name, failsafe=True)
    tracer = None
    if _trace_mode(config) != "off":
        from .trace import Tracer

        tracer = Tracer(d, title=request.node.nodeid).start()
        setattr(request.node, _TRACER_KEY, tracer)
    try:
        yield d
    finally:
        try:
            if tracer is not None:
                tracer.stop()
        finally:
            d.close()


@pytest.fixture
def locum_tracer(request: Any, desktop: Any) -> Any:
    """The :class:`~locum.trace.Tracer` recording this test's ``desktop`` (``None`` with ``--locum-trace off``)."""
    return getattr(request.node, _TRACER_KEY, None)


@pytest.fixture
def app(desktop: Any) -> Iterator[Callable[..., Any]]:
    """Factory that launches an application and returns its window.

    ``app(command, window=None, timeout=None, **launch_kw)`` runs
    :meth:`Desktop.launch(command, wait_window=window) <locum.desktop.Desktop.launch>`
    and returns the new :class:`~locum.window.Window` (``window=None`` = any
    window that appears after the launch). At teardown it closes the windows it
    returned -- only those, and only if they are still open::

        def test_notepad(app):
            pad = app("notepad.exe", window="*Notepad", timeout=15)
            pad.find("document").type("hello")
    """
    opened: List[Any] = []

    def launch(command: Any, window: Any = None, *, timeout: Optional[float] = None, **kw: Any) -> Any:
        w = desktop.launch(command, wait_window="*" if window is None else window, timeout=timeout, **kw)
        opened.append(w)
        return w

    yield launch
    errors: List[BaseException] = []
    for w in reversed(opened):
        try:
            if w.exists:
                w.close()
        except Exception as exc:  # close the others anyway
            errors.append(exc)
    if errors:
        raise errors[0]


# ------------------------------------------------------------------- hooks

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    outcome = yield
    tracer = getattr(item, _TRACER_KEY, None)
    if tracer is None:
        return
    report = outcome.get_result()
    mode = _trace_mode(item.config)
    if mode == "on":
        save = report.when == "teardown"
    else:
        save = report.failed or (report.when == "teardown" and getattr(item, _TRACE_PATH_KEY, None) is not None)
    if not save:
        return
    path = trace_path_for(item.nodeid, _trace_dir(item.config))
    try:
        tracer.save_html(path)
    except Exception as exc:  # a trace must never turn into the test's failure
        report.sections.append(("locum trace", f"could not save the trace to {path}: {type(exc).__name__}: {exc}"))
        return
    first = getattr(item, _TRACE_PATH_KEY, None) is None
    setattr(item, _TRACE_PATH_KEY, path)
    if first:
        item.user_properties.append(("locum_trace", path))
        if report.user_properties is not item.user_properties:
            report.user_properties.append(("locum_trace", path))
    if report.failed or mode == "on":
        report.sections.append(("locum trace", f"trace saved: {path}"))
