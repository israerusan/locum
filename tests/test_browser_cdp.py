"""Browser automation over DevTools (P1 #7 of the 2026-11-05 live-session report)."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import urllib.parse

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.browser import _ws
from locum.browser.cdp import Browser, CDPError, find_executable
from locum.llm.observe import observe
from locum.llm.safety import Policy
from locum.llm.tools import DesktopToolkit


# ------------------------------------------------------------- WebSocket client
def _server(handler):
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
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        handler(conn)
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return f"ws://127.0.0.1:{srv.getsockname()[1]}/devtools"


def _read_client_frame(conn):
    def exact(n):
        b = b""
        while len(b) < n:
            b += conn.recv(n - len(b))
        return b

    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", exact(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", exact(8))[0]
    assert b2 & 0x80, "client frames must be masked"
    mask = exact(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))
    return b1 & 0x0F, data


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


def test_websocket_roundtrip_fragments_ping_and_large_frames():
    got = {}

    def handler(conn):
        op, data = _read_client_frame(conn)
        got["first"] = data.decode()
        conn.sendall(_frame(_ws.OP_PING, b"hi"))
        conn.sendall(_frame(_ws.OP_TEXT, b"hel", fin=False) + _frame(_ws.OP_CONT, "lo wörld".encode()))
        op, data = _read_client_frame(conn)
        got["pong"] = (op, data)
        big = ("x" * 200000).encode()
        op, data = _read_client_frame(conn)
        got["big_in"] = len(data)
        conn.sendall(_frame(_ws.OP_TEXT, big))
        conn.sendall(_frame(_ws.OP_CLOSE, struct.pack("!H", 1000)))

    ws = _ws.WebSocket(_server(handler))
    ws.send('{"id":1}')
    assert ws.recv(timeout=5) == "hello wörld"  # ping answered transparently, fragments joined
    ws.send("y" * 70000)
    assert len(ws.recv(timeout=5)) == 200000
    with pytest.raises(_ws.WebSocketError):
        ws.recv(timeout=5)
    assert got["first"] == '{"id":1}' and got["pong"] == (_ws.OP_PONG, b"hi") and got["big_in"] == 70000


def test_websocket_rejects_a_non_upgrade_answer():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
        c.close()

    threading.Thread(target=run, daemon=True).start()
    with pytest.raises(_ws.WebSocketError):
        _ws.WebSocket(f"ws://127.0.0.1:{srv.getsockname()[1]}/x")


def test_attach_without_any_port_explains_how_to_enable(monkeypatch):
    monkeypatch.setattr("locum.browser.cdp.discover_ports", lambda: [1])
    with pytest.raises(CDPError) as e:
        Browser.attach()
    assert "--remote-debugging-port" in str(e.value) and "browser_launch" in str(e.value)


# ------------------------------------------------------------- toolkit tools (fake browser)
class FakePage:
    title = "Checkout"
    url = "https://shop.example/pay"

    def __init__(self):
        self.info = type("I", (), {"id": "p1", "title": "Checkout", "url": "https://shop.example/pay"})()
        self.typed = []
        self.clicked = []
        self.focused_password = False

    def snapshot(self, n):
        return {"title": "Checkout", "url": "https://shop.example/pay", "vw": 1280, "vh": 720, "scrollY": 0, "docH": 900,
                "elements": [{"id": 1, "role": "edit", "name": "Card holder", "value": "", "x": 100, "y": 50, "states": []},
                             {"id": 2, "role": "edit", "name": "Password", "value": "", "x": 100, "y": 90, "states": ["password"]},
                             {"id": 3, "role": "button", "name": "Pay now", "value": None, "x": 100, "y": 130, "states": []}]}

    def text(self, css=None, max_chars=3000):
        return "Total: 42 EUR"

    def resolve(self, target, scroll=True):
        names = {1: "Card holder", 2: "Password", 3: "Pay now"}
        return {"name": names[int(target)], "password": int(target) == 2, "tag": "input"}

    def click(self, target):
        self.clicked.append(target)
        return {}

    def type(self, text, target=None, clear=False, submit=False):
        self.typed.append((text, target, submit))

    def focused_is_password(self):
        return self.focused_password

    def screenshot_png(self):
        return b"\x89PNG"


class FakeBrowser:
    alive = True
    managed = True
    port = 9333
    name = "edge"
    app = "msedge.exe"
    pid = 4242
    version = {"Browser": "Edg/156"}

    def __init__(self):
        self.p = FakePage()

    def pages(self):
        return [self.p.info]

    def page(self, match=None):
        return self.p


@pytest.fixture
def kit():
    be = MockBackend()
    be.add_window("Desk", (0, 0, 400, 300)).add_button("OK", (10, 10, 50, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    tk = DesktopToolkit(d, Policy())
    tk._browser = FakeBrowser()
    return tk


def test_browser_observe_lists_page_elements_and_text(kit):
    r = kit.call("browser_observe", {})
    assert not r.is_error
    assert "[3] button 'Pay now' at (100, 130)" in r.text and "Total: 42 EUR" in r.text
    assert "[password]" in r.text


def test_browser_click_goes_through_the_policy(kit):
    r = kit.call("browser_click", {"element": 3})
    assert r.is_error and "Blocked by policy" in r.text  # "Pay now" needs a human confirmation
    assert kit._browser.p.clicked == []
    assert not kit.call("browser_click", {"element": 1}).is_error
    assert kit._browser.p.clicked == [1]


def test_browser_type_into_a_password_field_is_redacted(kit):
    r = kit.call("browser_type", {"text": "hunter2", "element": 2})
    assert not r.is_error and "hunter2" not in r.text and "***" in r.text
    assert kit._browser.p.typed == [("hunter2", 2, False)]
    events = [e for e in kit.audit.events if e.get("tool") == "browser_type"]
    assert events and "hunter2" not in json.dumps(events)


def test_browser_tools_respect_dry_run_and_allowed_apps(kit):
    kit.policy = Policy(dry_run=True)
    r = kit.call("browser_type", {"text": "Ada", "element": 1})
    assert "[dry-run]" in r.text and kit._browser.p.typed == []
    kit.policy = Policy(allowed_apps=["excel*"])
    assert kit.call("browser_click", {"element": 1}).is_error


def test_read_only_browser_tools_are_marked():
    mcp = {t["name"]: t for t in DesktopToolkit(Desktop(MockBackend(), failsafe=False)).to_mcp()}
    for name in ("browser_pages", "browser_observe", "browser_read"):
        assert mcp[name]["annotations"] == {"readOnlyHint": True}
    assert "annotations" not in mcp["browser_click"] or mcp["browser_click"]["annotations"] != {"readOnlyHint": True}


# ------------------------------------------------------------- observe skips page content
def test_observe_skips_document_subtree_of_connected_browser():
    be = MockBackend(width=1280, height=800)
    w = be.add_window("Shop - Microsoft Edge", (0, 0, 800, 600), app="msedge.exe", pid=4242)
    w.add_button("Back", (10, 10, 40, 30))
    doc = w.add("document", "Shop", (0, 50, 800, 550))
    for i in range(50):
        doc.add_button(f"Item {i}", (10 + (i % 10) * 70, 60 + (i // 10) * 40, 60, 30))
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    full = observe(d, screenshot=False)
    assert sum(e.name.startswith("Item") for e in full.elements) == 50
    pruned = observe(d, screenshot=False, prune_documents=[4242])
    names = [e.name for e in pruned.elements]
    assert "Back" in names and not any(n.startswith("Item") for n in names)
    assert any("browser_observe" in n for n in pruned.notes)


# ------------------------------------------------------------- real headless browser
def _browser_or_skip():
    from conftest import skip_unless_real_browser_allowed

    skip_unless_real_browser_allowed()
    for name in ("edge", "chrome", "chromium"):
        if find_executable(name):
            return name
    pytest.skip("no Chromium-based browser installed")


def test_real_headless_browser_end_to_end(tmp_path):
    name = _browser_or_skip()
    html = ("<title>Form test</title><label for=u>User</label><input id=u>"
            "<button onclick=\"document.getElementById('o').innerText='hi '+document.getElementById('u').value\">Go</button>"
            "<p id=o>idle</p>")
    try:
        b = Browser.launch(name, headless=True, profile_dir=str(tmp_path / "profile"),
                           url="data:text/html," + urllib.parse.quote(html), timeout=60,
                           extra_args=["--no-sandbox"] if name != "edge" else [])
    except (CDPError, OSError) as exc:
        pytest.skip(f"{name} could not start headless here: {exc}")
    try:
        p = b.page("Form test", timeout=30)
        p.wait_ready()
        els = {e["name"]: e["id"] for e in p.snapshot()["elements"]}
        p.type("Ada", els["User"])
        p.click(els["Go"])
        p.wait_for("#o")
        import time

        for _ in range(50):
            if p.text("#o") == "hi Ada":
                break
            time.sleep(0.05)
        assert p.text("#o") == "hi Ada"
        assert p.screenshot_png().startswith(b"\x89PNG")
        assert b.pid
    finally:
        b.close(quit=True)
