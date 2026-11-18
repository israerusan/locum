"""Regression tests for the 2026-11 (b) polish pass over the CLI / browser / vision / trace / compat modules."""

from __future__ import annotations

import io
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading

import numpy as np
import pytest
from PIL import Image

import locum
from locum import _versioning, cli, selftest, trace, vision
from locum.browser import _ws
from locum.browser import cdp
from locum.browser.cdp import Browser, CDPError, Page, PageInfo


# ------------------------------------------------------------------ M1 version
def test_format_describe_matches_hatch_vcs():
    assert _versioning.format_describe("v0.4.3-0-ge7cb4dc66\n") == "0.4.3"
    assert _versioning.format_describe("v0.4.3-3-ge7cb4dc66") == "0.4.4.dev3+ge7cb4dc66"
    assert _versioning.format_describe("v1.9-12-gabc") == "1.10.dev12+gabc"
    assert _versioning.format_describe("release-x") is None


def _git_ok() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _git_ok(), reason="git is not available")
def test_version_of_a_checkout_comes_from_its_own_tag(tmp_path):
    """A checkout at a newer tag must not report the version frozen in another install's metadata."""
    root = pathlib.Path(__file__).resolve().parents[1]
    clone = tmp_path / "clone"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false"]
    subprocess.run(git + ["clone", "-q", "--no-checkout", str(root), str(clone)], check=True, timeout=120)
    # This tree's code (not the committed one) in the clone, without the build's _version.py.
    shutil.copytree(root / "src" / "locum", clone / "src" / "locum",
                    ignore=shutil.ignore_patterns("__pycache__", "_version.py"))
    shutil.copy2(root / "pyproject.toml", clone / "pyproject.toml")
    subprocess.run(git + ["tag", "-a", "v97.5.3", "-m", "t"], cwd=clone, check=True, timeout=60)
    code = "import locum, sys; sys.stdout.write(locum.__version__)"
    env = dict(os.environ, PYTHONPATH=str(clone / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "97.5.3"


def test_version_is_lazy_and_cached(monkeypatch):
    calls = []
    saved = locum.__dict__.pop("__version__", None)
    try:
        monkeypatch.setattr(_versioning, "resolve_version", lambda: calls.append(1) or "1.2.3")
        assert locum.__version__ == "1.2.3" and locum.__version__ == "1.2.3"
        assert calls == [1] and "__version__" in dir(locum)
    finally:
        locum.__dict__.pop("__version__", None)
        if saved is not None:
            locum.__dict__["__version__"] = saved


def test_vendored_copy_does_not_report_the_host_projects_tags(tmp_path, monkeypatch):
    pkg = tmp_path / "src" / "locum"
    pkg.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "someone-else"\n', encoding="utf-8")
    monkeypatch.setattr(_versioning, "__file__", str(pkg / "_versioning.py"))
    assert _versioning._source_root() is None
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "locum"\n', encoding="utf-8")
    assert _versioning._source_root() == tmp_path.resolve()


def test_cli_import_does_not_resolve_the_version():
    code = ("import locum, locum.cli, sys; "
            "sys.stdout.write(str('__version__' in vars(locum)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "False"


# ------------------------------------------------------------------ M2 selftest on a cp1252 pipe
def test_selftest_survives_a_legacy_codepage_stdout(tmp_path, monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(selftest.SelfTest, "c_environment", lambda self: "title ⠋ spinner \U0001f600")
    rc = selftest.main(["--backend", "mock", "--yes", "--out", str(tmp_path / "st")])
    text = raw.getvalue().decode("cp1252")
    assert "ABORTED" not in text and "UnicodeEncodeError" not in text
    assert "title ? spinner ?" in text
    assert rc == 0
    assert stream.errors == "strict"  # restored


# ------------------------------------------------------------------ M3 websocket
def test_websocket_handshake_failure_closes_the_socket(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        c.recv(4096)
        c.close()  # hang up without answering

    threading.Thread(target=run, daemon=True).start()
    made = []
    real = socket.create_connection

    def track(*a, **kw):
        s = real(*a, **kw)
        made.append(s)
        return s

    monkeypatch.setattr(_ws.socket, "create_connection", track)
    with pytest.raises(_ws.WebSocketError):
        _ws.WebSocket(f"ws://127.0.0.1:{srv.getsockname()[1]}/x", timeout=5)
    srv.close()
    assert made and made[0].fileno() == -1


def test_websocket_small_frames_are_masked_like_large_ones():
    mask = b"\x01\x02\x03\x04"
    for n in (0, 1, 5, 125, 4095, 4096, 5000):
        payload = os.urandom(n)
        expected = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        assert _ws._mask(payload, mask) == expected


# ------------------------------------------------------------------ M4-M6 browser
class _FakeProc:
    def __init__(self, exits_on_wait: bool = True):
        self.returncode = None
        self.terminated = self.killed = False
        self.exits_on_wait = exits_on_wait
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None and not (self.exits_on_wait or self.terminated or self.killed):
            raise subprocess.TimeoutExpired("browser", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_launch_timeout_stops_the_browser_it_started(monkeypatch, tmp_path):
    proc = _FakeProc(exits_on_wait=False)
    monkeypatch.setattr(cdp, "find_executable", lambda name: "browser.exe")
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(cdp, "_active_port_file", lambda profile: None)
    with pytest.raises(CDPError, match="did not open a DevTools port"):
        Browser.launch("edge", profile_dir=str(tmp_path / "p"), timeout=0.05)
    assert proc.terminated and proc.returncode == 0


def _bare_browser(proc=None):
    b = object.__new__(Browser)
    b._pages = {}
    b.process = proc
    b.version = {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}
    return b


def test_close_quit_closes_its_session_and_waits_for_the_browser(monkeypatch):
    proc = _FakeProc(exits_on_wait=True)
    sessions = []

    class FakeSession:
        def __init__(self, url, timeout=10.0):
            self.closed = False
            sessions.append(self)

        def call(self, method, params=None, timeout=None):
            raise CDPError("connection closed")  # the browser hangs up before it answers

        def close(self):
            self.closed = True

    monkeypatch.setattr(cdp, "CDPSession", FakeSession)
    _bare_browser(proc).close(quit=True)
    assert sessions and sessions[0].closed
    assert not proc.terminated and proc.returncode == 0  # it exited by itself: never killed
    assert proc.waits and proc.waits[0] > 0


def test_close_quit_terminates_a_browser_that_does_not_exit(monkeypatch):
    proc = _FakeProc(exits_on_wait=False)

    class Unreachable:
        def __init__(self, *a, **kw):
            raise OSError("refused")

    monkeypatch.setattr(cdp, "CDPSession", Unreachable)
    _bare_browser(proc).close(quit=True)
    assert proc.terminated and proc.returncode == 0


def test_page_forgets_sessions_of_closed_tabs(monkeypatch):
    b = _bare_browser()
    infos = [PageInfo("A", "alpha", "http://x/1", "ws://127.0.0.1:1/devtools/page/A"),
             PageInfo("B", "beta", "http://x/2", "ws://127.0.0.1:1/devtools/page/B")]
    current = list(infos)
    monkeypatch.setattr(Browser, "pages", lambda self: list(current))
    closed = []

    class FakeSession:
        broken = False

    def fake_init(self, info, browser):
        self.info, self.browser, self.session = info, browser, FakeSession()

    monkeypatch.setattr(Page, "__init__", fake_init)
    monkeypatch.setattr(Page, "close", lambda self: closed.append(self.info.id))
    b.page("alpha")
    b.page("beta")
    assert set(b._pages) == {"A", "B"}
    current[:] = [infos[1]]  # tab A was closed
    b.page()
    assert set(b._pages) == {"B"} and closed == ["A"]


# ------------------------------------------------------------------ M7/M8 + perf: vision
def _pair():
    rng = np.random.default_rng(7)
    a = rng.integers(0, 256, (61, 83, 3), dtype=np.uint8)
    b = a.copy()
    b[5:30, 10:40] = rng.integers(0, 256, (25, 30, 3), dtype=np.uint8)
    return a, b


def test_image_diff_and_changed_fraction_are_unchanged():
    a, b = _pair()
    ia, ib = Image.fromarray(a), Image.fromarray(b)
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    assert vision.image_diff(ia, ib) == float(d.mean() / 255.0)
    assert vision.image_diff(ia, ib, tile=16) == vision._max_tile_mean(d.sum(axis=2, dtype=np.int32), 16) / 765.0
    for tol in (-1, 0, 10, 254, 255, 300):
        changed = d.max(axis=2) > tol
        assert vision.changed_fraction(ia, ib, pixel_tolerance=tol) == float(changed.mean())
        assert vision.changed_fraction(ia, ib, pixel_tolerance=tol, tile=8) == \
            vision._max_tile_mean(changed.astype(np.int32), 8)
    assert vision.image_diff(ia, Image.new("RGB", (5, 5))) == 1.0


def test_window_sum_and_gray_are_bit_identical():
    rng = np.random.default_rng(3)
    ii = vision._integral(rng.random((40, 50)))
    assert np.array_equal(vision._window_sum(ii, 7, 9),
                          ii[7:, 9:] - ii[:-7, 9:] - ii[7:, :-9] + ii[:-7, :-9])
    arr = rng.integers(0, 256, (20, 30, 3), dtype=np.uint8)
    x = arr.astype(np.float64)
    ref = (x[:, :, 0] * 0.299 + x[:, :, 1] * 0.587 + x[:, :, 2] * 0.114)[:, :, None]
    g = vision._gray(arr)
    assert g.dtype == np.float64 and g.shape == (20, 30, 1) and np.array_equal(g, ref)


@pytest.mark.parametrize("color,tol", [((10, 20, 30), 0), ((10, 20, 30), 15), ((0, 255, 128), 3),
                                       ((200, 5, 250), 300), ((7, 7, 7), -4)])
def test_find_color_matches_the_reference_mask(color, tol):
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 256, (40, 60, 3), dtype=np.uint8)
    arr[3:9, 4:12] = color
    arr[20:22, 40:55] = np.clip(np.array(color) + 2, 0, 255)
    img = Image.fromarray(arr)
    t = np.array(color, dtype=np.int16)
    ref_mask = np.all(np.abs(arr.astype(np.int16) - t) <= max(0, tol), axis=2)
    got = vision.find_color(img, color, tolerance=tol)
    area = sum(r.width * r.height for r in got)
    assert area >= int(ref_mask.sum())  # boxes cover every matching pixel
    covered = np.zeros(ref_mask.shape, bool)
    for r in got:
        covered[r.top:r.top + r.height, r.left:r.left + r.width] = True
    assert not (ref_mask & ~covered).any()
    assert (len(got) > 0) == bool(ref_mask.any())


# ------------------------------------------------------------------ M9 trace temp files
def test_trace_write_removes_its_temp_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "t.html"

    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(trace.os, "replace", boom)
    with pytest.raises(PermissionError):
        trace._write(target, "x")
    assert list(tmp_path.iterdir()) == []


def test_trace_write_temp_names_differ_per_thread(tmp_path, monkeypatch):
    names = []
    real_open = open

    def spy(path, *a, **kw):
        names.append(os.path.basename(str(path)))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    t = threading.Thread(target=trace._write, args=(tmp_path / "t.html", "a"))
    t.start()
    t.join()
    trace._write(tmp_path / "t.html", "b")
    assert len(set(names)) == 2 and (tmp_path / "t.html").read_text(encoding="utf-8") == "b"


# ------------------------------------------------------------------ fresh review: CLI
def test_click_selector_without_window_honours_timeout(monkeypatch):
    from locum.desktop import Desktop

    be = cli.DemoBackend()
    desk = Desktop(be, sleep=lambda s: None, default_timeout=0.2, poll_interval=0.01)
    seen = {}
    real = desk.find

    def spy(selector, **kw):
        seen.update(kw)
        return real(selector, **kw)

    monkeypatch.setattr(desk, "find", spy)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["--timeout", "7", "click", 'button "Save"'], desktop=desk, stdout=out, stderr=err)
    assert code == cli.EXIT_OK, err.getvalue()
    assert seen.get("timeout") == 7


# ------------------------------------------------------------------ fresh review: pyautogui compat
def test_log_screenshot_rotation_never_fails_the_action(tmp_path, monkeypatch):
    import locum.compat.pyautogui as pg

    monkeypatch.setattr(pg, "G_LOG_SCREENSHOTS_FILENAMES", ["old.png"])
    monkeypatch.setattr(pg, "LOG_SCREENSHOTS_LIMIT", 1)
    monkeypatch.setattr(pg, "screenshot", lambda path: pathlib.Path(path).write_bytes(b"png"))

    def locked(path):
        raise PermissionError("the file is open in a viewer")

    monkeypatch.setattr(pg.os, "unlink", locked)
    path = pg._logScreenshot(True, "click", "left,1,2,3", folder=str(tmp_path))
    assert path is not None and os.path.isfile(path)
    assert pg.G_LOG_SCREENSHOTS_FILENAMES == [os.path.basename(path)]
