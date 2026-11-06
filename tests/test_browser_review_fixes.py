"""Regression tests for the browser (DevTools) review findings of 2026-11-06.

1. browser_type(submit=True) must judge what Enter activates (the form's "Pay now").
2. A field's value (autofilled password!) never becomes its name.
3. No DevTools call (no Input.*) is sent once the MCP call is cancelled / timed out.
5. A frame that stalls mid-way past the socket timeout does not desynchronise the stream;
   a broken session is recreated by Browser.page().
7. Browser.attach() without a port only connects to locum-managed / explicitly chosen
   browsers, and only to local DevTools endpoints.
-  Browser.new_page keeps the URL's #fragment.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import socket
import struct
import threading
import time
import urllib.parse

import pytest

from locum import Desktop
from locum._cancel import CallCancelled, CancelScope
from locum.backends.mock import MockBackend
from locum.browser import _ws, cdp
from locum.browser.cdp import Browser, CDPError, CDPSession, Page, PageInfo, find_executable
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit


# ------------------------------------------------------------------ helpers
def _ws_server(handler):
    """A one-connection WebSocket server; ``handler(conn)`` runs after the handshake."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1((key + _ws._GUID).encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        try:
            handler(conn)
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return f"ws://127.0.0.1:{srv.getsockname()[1]}/devtools/page/X"


def _frame(opcode, data, fin=True):
    n = len(data)
    head = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + data


def _read_client_frame(conn):
    def exact(n):
        b = b""
        while len(b) < n:
            chunk = conn.recv(n - len(b))
            if not chunk:
                raise ConnectionError("closed")
            b += chunk
        return b

    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", exact(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", exact(8))[0]
    mask = exact(4)
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))


# ------------------------------------------------------------------ 5. stalled frames
def test_websocket_frame_stalled_past_the_timeout_is_not_lost():
    big = json.dumps({"id": 1, "result": {"data": "x" * 100000}}).encode()
    frame = _frame(_ws.OP_TEXT, big)

    def handler(conn):
        conn.sendall(frame[:30000])
        time.sleep(0.9)  # longer than the 0.5 s receive timeout
        conn.sendall(frame[30000:])
        conn.sendall(_frame(_ws.OP_TEXT, b'{"id":2,"result":{}}'))
        time.sleep(0.5)

    ws = _ws.WebSocket(_ws_server(handler))
    msgs = []
    end = time.monotonic() + 5
    while len(msgs) < 2 and time.monotonic() < end:
        try:
            msgs.append(json.loads(ws.recv(timeout=0.5)))
        except socket.timeout:
            continue
    assert [m["id"] for m in msgs] == [1, 2]
    assert len(msgs[0]["result"]["data"]) == 100000
    ws.close()


def test_cdp_call_survives_a_response_that_stalls_mid_frame():
    def handler(conn):
        _, data = _read_client_frame(conn)
        mid = json.loads(data)["id"]
        frame = _frame(_ws.OP_TEXT, json.dumps({"id": mid, "result": {"value": "y" * 50000}}).encode())
        conn.sendall(frame[:1000])
        time.sleep(0.9)
        conn.sendall(frame[1000:])
        _, data = _read_client_frame(conn)
        conn.sendall(_frame(_ws.OP_TEXT, json.dumps({"id": json.loads(data)["id"], "result": {"ok": True}}).encode()))
        time.sleep(0.3)

    s = CDPSession(_ws_server(handler))
    assert len(s.call("Runtime.evaluate", timeout=5)["value"]) == 50000
    assert s.call("Runtime.evaluate", timeout=5) == {"ok": True}
    assert not s.broken
    s.close()


def test_a_session_whose_stream_broke_is_marked_and_page_recreates_it(monkeypatch):
    def handler(conn):
        _read_client_frame(conn)
        conn.sendall(b"\x81\x7f")  # a frame header announcing 2^63+ bytes -> protocol error
        conn.sendall(b"\xff" * 8)
        time.sleep(0.3)

    s = CDPSession(_ws_server(handler))
    with pytest.raises(CDPError):
        s.call("Runtime.evaluate", timeout=5)
    assert s.broken

    # Browser.page() must not hand out the cached, broken page again.
    b = object.__new__(Browser)
    b._pages = {}
    info = PageInfo("T1", "Tab", "about:blank", "ws://127.0.0.1:1/devtools/page/T1")
    monkeypatch.setattr(Browser, "pages", lambda self: [info])
    old = object.__new__(Page)
    old.info, old.browser, old.session = info, b, s
    b._pages["T1"] = old
    made = []

    def fake_init(self, info, browser):
        self.info, self.browser, self.session = info, browser, None
        made.append(self)

    monkeypatch.setattr(Page, "__init__", fake_init)
    p = b.page()
    assert p is not old and made == [p]


# ------------------------------------------------------------------ 3. cancellation
def test_cancelled_call_sends_nothing():
    got = []

    def handler(conn):
        conn.settimeout(1.5)
        try:
            while True:
                got.append(_read_client_frame(conn))
        except (OSError, ConnectionError):
            pass

    s = CDPSession(_ws_server(handler))
    scope = CancelScope()
    scope.cancel("timed out")
    with scope:
        with pytest.raises(CallCancelled):
            s.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1})
    time.sleep(1.7)
    assert not any(op == _ws.OP_TEXT for op, _ in got), got
    s.close()


# ------------------------------------------------------------------ 1. submit judges the default button (fake page)
class _FakePage:
    def __init__(self, enter):
        self.info = type("I", (), {"id": "p1", "title": "Checkout", "url": "https://shop.example/checkout"})()
        self.title, self.url = "Checkout", "https://shop.example/checkout"
        self.enter = enter
        self.typed = []

    def resolve(self, target, scroll=True):
        return {"name": "Card number", "password": False, "tag": "input"}

    def enter_target(self, target=None):
        return self.enter

    def focused_is_password(self):
        return False

    def type(self, text, target=None, clear=False, submit=False):
        self.typed.append((text, target, submit))


class _FakeBrowser:
    alive, managed, port, name, app, pid = True, True, 9333, "edge", "msedge.exe", 4242
    version = {"Browser": "Edg/156"}

    def __init__(self, page):
        self.p = page

    def page(self, match=None):
        return self.p


def _kit(policy=None):
    be = MockBackend()
    be.add_window("Desk", (0, 0, 400, 300))
    return DesktopToolkit(Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0), policy or Policy())


def test_browser_type_submit_into_a_pay_form_needs_confirmation():
    kit = _kit()
    kit._browser = _FakeBrowser(_FakePage({"kind": "form", "label": "Pay now", "form": "", "action": "/checkout"}))
    r = kit.call("browser_type", {"text": "4242424242424242", "element": 1, "submit": True})
    assert r.is_error and "Blocked by policy" in r.text and "Pay now" in r.text
    assert kit._browser.p.typed == []
    # Without Enter only the field is edited: no confirmation.
    assert not kit.call("browser_type", {"text": "4242", "element": 1}).is_error
    assert kit._browser.p.typed == [("4242", 1, False)]


def test_browser_type_submit_judges_the_form_action_when_it_has_no_button():
    kit = _kit()
    kit._browser = _FakeBrowser(_FakePage({"kind": "form", "label": "", "form": "", "action": "/payments/execute"}))
    r = kit.call("browser_type", {"text": "100", "element": 1, "submit": True})
    assert r.is_error and "Blocked by policy" in r.text


def test_browser_type_submit_in_a_harmless_form_runs_and_names_the_button():
    kit = _kit(Policy(dry_run=True))
    kit._browser = _FakeBrowser(_FakePage({"kind": "form", "label": "Search", "form": "", "action": "/q"}))
    r = kit.call("browser_type", {"text": "socks", "element": 1, "submit": True})
    assert not r.is_error and "Search" in r.text


def test_policy_reads_the_enter_target_of_a_submit():
    p = Policy(dry_run=True)
    ctx = {"target": "Card number", "enter": True, "enter_target": "Pay now", "app": "msedge.exe"}
    p.check("browser_type", {"text": "1"}, context=ctx)
    assert "Pay now" in str(ctx.get("needs_confirmation"))


# ------------------------------------------------------------------ real headless browser
def _browser_name():
    for name in ("edge", "chrome", "chromium"):
        if find_executable(name):
            return name
    return None


@pytest.fixture(scope="module")
def real_browser(tmp_path_factory):
    name = _browser_name()
    if name is None:
        pytest.skip("no Chromium-based browser installed")
    try:
        b = Browser.launch(name, headless=True, profile_dir=str(tmp_path_factory.mktemp("cdp-review") / "profile"),
                           timeout=60, extra_args=["--no-sandbox"] if name != "edge" else [])
    except (CDPError, OSError) as exc:
        pytest.skip(f"{name} could not start headless here: {exc}")
    try:
        yield b
    finally:
        b.close(quit=True)


def _open(b, tmp_path, html, name="page.html", fragment=""):
    f = tmp_path / name
    f.write_text(html, encoding="utf-8")
    p = b.new_page(f.as_uri() + fragment)
    p.wait_ready()
    return p


_PAY_HTML = ("<title>Checkout</title><form action='/charge' onsubmit='document.title=\"SUBMITTED\";return false'>"
             "<input id=card aria-label='Card number'><button type=submit>Pay now</button></form>")


def test_real_enter_target_of_a_field_in_a_pay_form(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, _PAY_HTML)
    t = p.enter_target("#card")
    assert t["kind"] == "form" and t["label"] == "Pay now" and t["action"] == "/charge"


def test_real_browser_type_submit_into_pay_form_is_blocked(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, _PAY_HTML, name="pay2.html")
    kit = _kit()
    kit._browser = real_browser
    r = kit.call("browser_type", {"text": "4242424242424242", "selector": "#card", "submit": True, "tab": "pay2.html"})
    assert r.is_error and "Blocked by policy" in r.text
    assert p.evaluate("document.title") == "Checkout" and p.evaluate("document.getElementById('card').value") == ""
    r = kit.call("browser_click", {"selector": "button", "tab": "pay2.html"})
    assert r.is_error and "Blocked by policy" in r.text and "Pay now" in r.text
    assert p.evaluate("document.title") == "Checkout"


def test_real_enter_target_outside_a_form_uses_the_nearby_button(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, "<title>Chat</title><div><textarea id=m></textarea><div><button>Send</button></div></div>",
              name="chat.html")
    assert p.enter_target("#m")["label"] == "Send"


def test_real_password_value_never_becomes_the_name(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, "<title>Login</title><input id=pw type=password><input id=t>"
              "<input id=s type=submit value='Sign in'>", name="login.html")
    p.evaluate("document.getElementById('pw').value='hunter2'; document.getElementById('t').value='secret-text'; 0")
    assert "hunter2" not in json.dumps(p.resolve("#pw"))
    assert "secret-text" not in json.dumps(p.resolve("#t"))
    assert p.resolve("#s")["name"] == "Sign in"  # a submit button's value is its label
    kit = _kit()
    kit._browser = real_browser
    r = kit.call("browser_click", {"selector": "#pw", "tab": "login.html"})
    assert not r.is_error and "hunter2" not in r.text
    assert "hunter2" not in json.dumps([e for e in kit.audit.events], default=str)
    t = p.enter_target("#pw")
    assert "hunter2" not in json.dumps(t)


def test_real_new_page_keeps_the_fragment(real_browser, tmp_path):
    p = _open(real_browser, tmp_path, "<title>Frag</title><p id=sec>x</p>", name="frag.html", fragment="#sec")
    assert p.evaluate("location.hash") == "#sec"


# ------------------------------------------------------------------ 7. attach rules
class _DevTools(http.server.BaseHTTPRequestHandler):
    version: dict = {}
    pages: list = []

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.version if self.path == "/json/version" else self.pages).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _devtools_server(version, pages=()):
    handler = type("H", (_DevTools,), {"version": version, "pages": list(pages)})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _ver(port, host="127.0.0.1"):
    return {"Browser": "Chrome/150.0", "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": f"ws://{host}:{port}/devtools/browser/abc"}


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    everyday = tmp_path / "everyday"
    everyday.mkdir()
    monkeypatch.setattr(cdp, "_user_data_dirs", lambda: {"chrome": [everyday], "edge": [], "chromium": []})
    monkeypatch.setenv("LOCUM_BROWSER_PROFILES", str(tmp_path / "managed"))
    monkeypatch.delenv("LOCUM_CDP_PORT", raising=False)
    monkeypatch.delenv("LOCUM_CDP_ATTACH_ANY", raising=False)
    return everyday


def test_attach_does_not_pick_the_everyday_profile_by_default(isolated_profiles, monkeypatch):
    srv = _devtools_server({})
    port = srv.server_address[1]
    srv.RequestHandlerClass.version = _ver(port)
    try:
        (isolated_profiles / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/abc")
        monkeypatch.setattr(cdp, "_DEFAULT_PORT", port)  # stands in for 127.0.0.1:9222
        with pytest.raises(CDPError) as e:
            Browser.attach()
        assert "allow_any" in str(e.value) and "LOCUM_CDP_ATTACH_ANY" in str(e.value)
        assert Browser.attach(allow_any=True).port == port
        monkeypatch.setenv("LOCUM_CDP_ATTACH_ANY", "1")
        assert Browser.attach().port == port
    finally:
        srv.shutdown()


def test_attach_prefers_managed_profiles_and_env_port(isolated_profiles, monkeypatch, tmp_path):
    srv = _devtools_server({})
    port = srv.server_address[1]
    srv.RequestHandlerClass.version = _ver(port)
    try:
        managed = tmp_path / "managed" / "edge"
        managed.mkdir(parents=True)
        (managed / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/abc")
        assert Browser.attach().port == port
        (managed / "DevToolsActivePort").unlink()
        monkeypatch.setenv("LOCUM_CDP_PORT", str(port))
        assert Browser.attach().port == port
    finally:
        srv.shutdown()


def test_attach_rejects_non_devtools_and_non_local_endpoints(isolated_profiles):
    srv = _devtools_server({"hello": "world"})
    port = srv.server_address[1]
    try:
        with pytest.raises(CDPError):
            Browser.attach(port=port)  # not a DevTools /json/version
        srv.RequestHandlerClass.version = _ver(port, host="evil.example")
        with pytest.raises(CDPError):
            Browser.attach(port=port)  # browser websocket is not on this machine
        srv.RequestHandlerClass.version = _ver(port)
        srv.RequestHandlerClass.pages = [
            {"id": "A", "type": "page", "title": "ok", "url": "about:blank",
             "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/A"},
            {"id": "B", "type": "page", "title": "remote", "url": "about:blank",
             "webSocketDebuggerUrl": "ws://10.0.0.5:9222/devtools/page/B"},
        ]
        b = Browser.attach(port=port)
        assert [p.id for p in b.pages()] == ["A"]
    finally:
        srv.shutdown()


def test_existing_attach_error_message_still_explains(monkeypatch, isolated_profiles):
    monkeypatch.setattr(cdp, "discover_ports", lambda *a, **k: [1])
    with pytest.raises(CDPError) as e:
        Browser.attach()
    assert "--remote-debugging-port" in str(e.value) and "browser_launch" in str(e.value)

