"""Polish pass 3: CDP robustness and time budgets, WebSocket send timeout, shared selector
state in the CLI, lower peak memory in vision._score_map, ungated Java actions, SAP type cache.

Everything runs on fakes and localhost sockets: no real browser, app or desktop input."""

from __future__ import annotations

import http.server
import io
import json
import socket
import threading
from collections import Counter
from typing import Any, Dict, List

import numpy as np
import pytest
from PIL import Image

from locum import vision
from locum.browser import cdp
from locum.browser._ws import WebSocket


# ============================================================================== helpers
def _non_http_listener(banner: bytes = b"SSH-2.0-OpenSSH_9.6\r\n"):
    """A localhost listener that answers anything with a non-HTTP banner."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(5)

    def serve() -> None:
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.settimeout(2)
                c.recv(4096)
                c.sendall(banner)
            except OSError:
                pass
            finally:
                c.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


class _DevToolsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"Browser": "Chrome/130.0", "Protocol-Version": "1.3",
                           "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def bad_port():
    srv, port = _non_http_listener()
    yield port
    srv.close()


@pytest.fixture
def devtools_port():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _DevToolsHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# ============================================================================== 1. HTTPException -> CDPError
def test_http_json_turns_a_non_http_answer_into_cdperror(bad_port):
    with pytest.raises(cdp.CDPError, match="does not answer HTTP"):
        cdp._http_json("127.0.0.1", bad_port, "/json/version")


def test_attach_explicit_non_http_port_raises_cdperror(bad_port):
    with pytest.raises(cdp.CDPError, match="No locum-usable browser"):
        cdp.Browser.attach(port=bad_port)


def test_attach_moves_on_to_the_next_port_after_a_non_http_listener(bad_port, devtools_port, monkeypatch):
    monkeypatch.setattr(cdp, "discover_ports", lambda allow_any=False: [bad_port, devtools_port])
    b = cdp.Browser.attach()
    assert b.port == devtools_port and b.name == "chrome"


def test_launch_unlinks_a_stale_port_file_whose_port_is_not_http(bad_port, tmp_path, monkeypatch):
    (tmp_path / "DevToolsActivePort").write_text(f"{bad_port}\n/devtools/browser/x\n")
    monkeypatch.setattr(cdp, "find_executable", lambda b: "C:/not/a/browser.exe")

    class Reached(Exception):
        pass

    def popen(*a: Any, **k: Any) -> Any:
        raise Reached

    monkeypatch.setattr(cdp.subprocess, "Popen", popen)
    with pytest.raises(Reached):  # got past the stale endpoint to starting a browser
        cdp.Browser.launch("edge", profile_dir=str(tmp_path))
    assert not (tmp_path / "DevToolsActivePort").exists()


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False
        self.waited = 0

    def poll(self) -> Any:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: Any = None) -> int:
        self.waited += 1
        return 0


def test_launch_stops_the_started_browser_when_the_wait_loop_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cdp, "find_executable", lambda b: "C:/not/a/browser.exe")
    proc = _FakeProc()
    monkeypatch.setattr(cdp.subprocess, "Popen", lambda *a, **k: proc)
    reads = iter([None, 4242])  # no port file before Popen, then one appears
    monkeypatch.setattr(cdp, "_active_port_file", lambda profile: next(reads, 4242))

    def boom(self: Any, *a: Any, **k: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cdp.Browser, "__init__", boom)
    with pytest.raises(RuntimeError, match="unexpected"):
        cdp.Browser.launch("edge", profile_dir=str(tmp_path), timeout=5)
    assert proc.terminated and proc.waited


# ============================================================================== 2. per-call timeouts
class _FakeSession:
    broken = False

    def __init__(self, navigate: Any = None) -> None:
        self.seen: List[tuple] = []
        self.navigate = navigate

    def call(self, method: str, params: Any = None, timeout: float = 30.0) -> Dict[str, Any]:
        self.seen.append((method, (params or {}).get("expression", ""), timeout))
        if method == "Page.navigate":
            return self.navigate or {}
        if "timeOrigin" in (params or {}).get("expression", ""):
            return {"result": {"value": 1.0}}  # the old document never goes away
        return {"result": {"value": False}}


def _page(session: Any) -> cdp.Page:
    p = cdp.Page.__new__(cdp.Page)
    p.session = session
    return p


def test_wait_for_and_wait_ready_bound_each_call_by_the_remaining_time():
    s = _FakeSession()
    p = _page(s)
    with pytest.raises(cdp.ElementNotFound):
        p.wait_for("#x", timeout=0.2)
    with pytest.raises(cdp.CDPError):
        p.wait_ready(timeout=0.2)
    evals = [t for m, _, t in s.seen if m == "Runtime.evaluate"]
    assert evals and all(0.1 <= t <= 0.2 + 1e-6 for t in evals)


def test_navigate_probe_loop_bounds_each_call_by_the_remaining_time():
    s = _FakeSession(navigate={"frameId": "f", "loaderId": "l"})
    p = _page(s)
    with pytest.raises(cdp.CDPError, match="did not start loading"):
        p.navigate("http://127.0.0.1:1/", timeout=0.3)
    probes = [t for m, e, t in s.seen if m == "Runtime.evaluate"][1:]  # after the initial origin read
    assert probes and all(0.1 <= t <= 0.3 + 1e-6 for t in probes)


def test_long_timeouts_keep_the_old_per_call_caps():
    s = _FakeSession()
    p = _page(s)
    p.call_js = lambda src, *a, timeout=30.0: s.seen.append(("js", "", timeout)) or True  # type: ignore[method-assign]
    p.wait_for("#x", timeout=600)
    assert s.seen[-1][2] == 30.0


# ============================================================================== 3. WebSocket send timeout
class _SockProxy:
    def __init__(self, sock: socket.socket) -> None:
        self._s = sock
        self.send_timeouts: List[Any] = []

    def sendall(self, data: bytes) -> None:
        self.send_timeouts.append(self._s.gettimeout())
        self._s.sendall(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)


def _ws_server():
    import base64
    import hashlib

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5)
    got: List[bytes] = []

    def serve() -> None:
        c, _ = srv.accept()
        c.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            data += c.recv(4096)
        key = [ln.split(b":", 1)[1].strip() for ln in data.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                got.append(chunk)
        except OSError:
            pass
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1], got


def test_send_uses_the_connect_timeout_not_the_last_recv_poll():
    srv, port, _ = _ws_server()
    try:
        ws = WebSocket(f"ws://127.0.0.1:{port}/devtools/page/x", timeout=7.0)
        proxy = _SockProxy(ws._sock)
        ws._sock = proxy  # type: ignore[assignment]
        with pytest.raises(socket.timeout):
            ws.recv(timeout=0.005)
        ws.send("hello")
        assert proxy.send_timeouts == [7.0]
        assert proxy.gettimeout() == 0.005  # restored for a recv that sends a pong mid-message
        ws.close()
        assert proxy.send_timeouts == [7.0, 7.0]  # the close frame too
    finally:
        srv.close()


# ============================================================================== 4. CLI shared selector state
@pytest.fixture
def demo_desk():
    from locum.cli import DemoBackend
    from locum.desktop import Desktop

    return Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=0.2, poll_interval=0.01)


def _run(argv: List[str], desk: Any) -> str:
    from locum.cli import EXIT_OK, main

    out, err = io.StringIO(), io.StringIO()
    assert main(list(argv), desktop=desk, stdout=out, stderr=err) == EXIT_OK, err.getvalue()
    return out.getvalue()


@pytest.mark.parametrize("argv", [["find", "--all", "*"], ["find", "--all", "edit"],
                                  ["--json", "find", "--all", "*"]])
def test_find_all_output_is_identical_with_shared_roots_and_cache(argv, demo_desk, monkeypatch):
    from locum import cli

    calls = Counter()
    real_roots = demo_desk.roots

    def roots(*a: Any, **k: Any) -> Any:
        calls["roots"] += 1
        return real_roots(*a, **k)

    monkeypatch.setattr(demo_desk, "roots", roots)
    new = _run(argv, demo_desk)
    shared_roots = calls["roots"]
    monkeypatch.setattr(cli, "_selector_state", lambda: None)  # the 0.5.0 per-element path
    calls.clear()
    old = _run(argv, demo_desk)
    assert new == old and new.count("->") + new.count("robust_selector") > 10
    # one read for the search itself, one shared by every listed element's selector
    assert shared_roots == 2 and calls["roots"] > 10


def test_inspect_json_computes_the_robust_selector_once(demo_desk, monkeypatch):
    from locum import selectors

    target = demo_desk.find("edit#vendor").center
    calls = Counter()
    real = selectors.selector_for

    def counting(*a: Any, **k: Any) -> str:
        calls["selector_for"] += 1
        return real(*a, **k)

    monkeypatch.setattr(selectors, "selector_for", counting)
    data = json.loads(_run(["inspect", "--json", f"{target.x},{target.y}"], demo_desk))
    assert calls["selector_for"] == 1
    assert data["robust_selector"] == real(demo_desk.element_at(target).node, roots=demo_desk.roots())


# ============================================================================== 5. vision peak memory, bit-identical
# _score_map as released in 0.5.0 (before s1/s2 were freed early): the reference.
_SCORE_MAP_050 = '''
def _score_map_050(hay, needle, mask):
    th, tw, c = needle.shape
    shifted = needle - hay.mean
    if mask is None:
        n = float(th * tw)
        floor = (_FLAT_WINDOW_STD ** 2) * n * c
        ii, ii_sq = hay.integrals()
        s1 = [_window_sum(ii[k], th, tw) for k in range(c)]
        s2 = _window_sum(ii_sq, th, tw)
        tmean = shifted.reshape(-1, c).mean(axis=0)
        tz = shifted - tmean
        var_t = float((tz ** 2).sum())
        if var_t <= (_FLAT_NEEDLE_STD ** 2) * n * c:
            return _flat_needle_score(s1, s2, tmean, n)
        var_h = _window_variance(s1, s2, n)
        cv2 = _cv2()
        if cv2 is not None:
            hay_src = hay.raw if not hay.grayscale else (hay.data[0] + hay.mean[0])
            ndl_src = needle if not hay.grayscale else needle[:, :, 0]
            score = cv2.matchTemplate(
                np.ascontiguousarray(hay_src, dtype=np.float32),
                np.ascontiguousarray(ndl_src, dtype=np.float32),
                cv2.TM_CCOEFF_NORMED,
            ).astype(np.float64)
            score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)
            score[var_h <= floor] = 0.0
            return np.clip(score, -1.0, 1.0)
        num = hay.correlate(hay.fft(), [tz[:, :, k] for k in range(c)], th, tw)
        return _normalize_ncc(num, var_h, var_t, floor)
    w = mask
    nw = float(w.sum())
    floor = (_FLAT_WINDOW_STD ** 2) * nw * c
    tmean = (shifted * w[:, :, None]).reshape(-1, c).sum(axis=0) / nw
    dev = shifted - tmean
    var_t = float(((dev ** 2) * w[:, :, None]).sum())
    spectra = hay.fft()
    wspec = hay.kernel_spectrum(w)
    s1 = [hay.correlate([spectra[k]], [wspec], th, tw) for k in range(c)]
    s2 = hay.correlate(hay.fft_sq(), [wspec] * c, th, tw)
    if var_t <= (_FLAT_NEEDLE_STD ** 2) * nw * c:
        score = _flat_needle_score(s1, s2, tmean, nw)
        _apply_shape_contrast(score, hay, tmean, w)
        return score
    num = hay.correlate(spectra, [dev[:, :, k] * w for k in range(c)], th, tw)
    var_h = _window_variance(s1, s2, nw)
    return _normalize_ncc(num, var_h, var_t, floor)
'''


def _reference_score_map():
    ns = dict(vision.__dict__)
    exec(_SCORE_MAP_050, ns)  # noqa: S102 - a fixed reference copy
    return ns["_score_map_050"]


def _score_inputs(grayscale: bool, masked: bool, flat: bool = False):
    rng = np.random.default_rng(7)
    hay = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    hay[10:40, 10:60] = 200  # a flat patch: the flat-window floor matters
    needle = hay[50:66, 70:94].copy()
    if flat:
        needle[:] = 200
    img = Image.fromarray(needle)
    if masked:
        rgba = np.dstack([needle, np.full(needle.shape[:2], 255, np.uint8)])
        rgba[:4, :5, 3] = 0
        img = Image.fromarray(rgba, "RGBA")
    prepared = vision._prepare_needle(img, 1.0, grayscale)
    assert prepared is not None
    data, mask = prepared
    if masked:
        assert mask is not None
    return vision._Haystack(vision._rgb_array(Image.fromarray(hay)), grayscale), data, mask


@pytest.mark.parametrize("grayscale", [False, True])
@pytest.mark.parametrize("masked", [False, True])
@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("use_cv2", [False, True])
def test_score_map_is_bit_identical_to_050(grayscale, masked, flat, use_cv2, monkeypatch):
    if use_cv2 and vision._cv2() is None:
        pytest.skip("OpenCV not installed")
    if not use_cv2:
        monkeypatch.setattr(vision, "_cv2", lambda: None)
    ref = _reference_score_map()
    hay, data, mask = _score_inputs(grayscale, masked, flat)
    expected = ref(hay, data, mask)
    hay2, data2, mask2 = _score_inputs(grayscale, masked, flat)
    got = vision._score_map(hay2, data2, mask2)
    assert got.dtype == expected.dtype and np.array_equal(got, expected)


# ============================================================================== 6. ungated Java actions
def test_ungated_java_actions_skip_the_policy_context(monkeypatch, mock_backend):
    from test_bridge_java import FakeDll, bridge, make_tree

    from locum import Desktop
    from locum.bridges import java

    for i in range(5):
        mock_backend.add_window(f"Other {i}", (0, 0, 100, 100), app=f"app{i}.exe")
    w = mock_backend.add_window("Order Entry", (100, 100, 640, 480), app="javaw.exe")
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    calls = Counter()
    real_lw = mock_backend.list_windows

    def lw(*a: Any, **k: Any) -> Any:
        calls["list_windows"] += 1
        return real_lw(*a, **k)

    monkeypatch.setattr(mock_backend, "list_windows", lw)
    d = Desktop(mock_backend, failsafe=False)
    with bridge(desktop=d) as jab:
        assert jab.gate.policy is None
        win = jab.from_hwnd(w.handle)
        save, cust = win.find('button "Save"'), win.find('edit "Customer"')
        calls.clear()
        save.click()
        cust.set_text("ACME")
        cust.focus()
        save.do_action("click")
        assert calls["list_windows"] == 0
        assert dll.roots[w.handle].kids[0].kids[1].text == "ACME"
        assert dll.roots[w.handle].kids[0].kids[4].done == ["click", "click"]


def test_ungated_java_action_still_honours_the_failsafe(monkeypatch, mock_backend):
    from test_bridge_java import FakeDll, bridge, make_tree

    from locum.bridges import java
    from locum.errors import FailSafeTriggered

    dll = FakeDll(make_tree())
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    with bridge() as jab:
        save = jab.from_hwnd(next(iter(dll.roots))).find('button "Save"')

        class Tripped:
            def check(self) -> None:
                raise FailSafeTriggered("aborted")

        jab.gate.failsafe = Tripped()
        with pytest.raises(FailSafeTriggered):
            save.click()
        assert dll.roots[next(iter(dll.roots))].kids[0].kids[4].done == []


def test_gated_java_actions_still_build_the_context(monkeypatch, mock_backend):
    from test_bridge_java import FakeDll, bridge, make_tree

    from locum import Desktop
    from locum.bridges import java
    from locum.llm.safety import AuditLog, Policy

    w = mock_backend.add_window("Order Entry", (100, 100, 640, 480), app="javaw.exe")
    dll = FakeDll(make_tree(w.handle))
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    calls = Counter()
    real_lw = mock_backend.list_windows
    monkeypatch.setattr(mock_backend, "list_windows",
                        lambda *a, **k: calls.update(["lw"]) or real_lw(*a, **k))
    audit = AuditLog()
    d = Desktop(mock_backend, failsafe=False)
    with bridge(desktop=d, policy=Policy(), audit=audit) as jab:
        jab.from_hwnd(w.handle).find('button "Save"').click()
    assert calls["lw"] >= 1
    ev = [e for e in audit.events if e.get("event") == "rpa_action"]
    assert ev and ev[-1]["tool"] == "click" and ev[-1]["status"] == "allowed"


# ============================================================================== 7. SAP Type cached
def test_sap_node_reads_type_once():
    from locum.bridges.sap import SapNode

    reads = Counter()

    class FakeCom:
        def __init__(self, **props: Any) -> None:
            object.__setattr__(self, "_p", props)

        def __getattr__(self, name: str) -> Any:
            reads[name] += 1
            if name in self._p:
                return self._p[name]
            raise AttributeError(name)

    label = FakeCom(Text="Customer")
    f = FakeCom(Type="GuiCTextField", LeftLabel=label, Text="ACME", Changeable=True,
                Id="/app/con[0]/ses[0]/wnd[0]/usr/ctxtX")
    n = SapNode(f)
    assert n.role == "edit"
    _ = (n.name, n.states, n.value, n.attributes())
    assert reads["Type"] == 1


def test_sap_node_retries_an_unreadable_type():
    from locum.bridges.sap import SapNode

    class Flaky:
        n = 0

        @property
        def Type(self) -> str:  # noqa: N802
            Flaky.n += 1
            if Flaky.n == 1:
                raise RuntimeError("COM busy")
            return "GuiButton"

    node = SapNode(Flaky())
    assert node._type == ""
    assert node._type == "GuiButton" and node._type == "GuiButton"
    assert Flaky.n == 2


# ============================================================================== 8. _guard imports
def test_guard_policy_branch_imports_only_what_it_uses():
    import ast
    import inspect

    from locum.bridges import _guard

    tree = ast.parse(inspect.getsource(_guard))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            imported = [a.name for n in ast.walk(node) if isinstance(n, ast.ImportFrom) for a in n.names]
            assert sorted(imported).count("AuditLog") == 1
