"""Drive Chromium browsers (Edge, Chrome) through the Chrome DevTools Protocol.

A browser tab's accessibility tree can hold thousands of nodes (the live session
that prompted this saw ~2,400 for one Edge window); the page's own DOM is far
cheaper to query and act on. Two ways to get a DevTools connection:

* :meth:`Browser.attach` -- to a browser that already exposes one: an explicit
  port, ``$LOCUM_CDP_PORT``, or a browser running with a locum-managed profile.
  The everyday profiles' ``DevToolsActivePort`` and port 9222 are only tried with
  ``attach(allow_any=True)`` / ``$LOCUM_CDP_ATTACH_ANY=1`` (whatever answers
  there may be your signed-in browser or another program). Endpoints must answer
  like Chromium DevTools and use local WebSockets only. Your everyday browser
  profile normally has no port open, and locum never opens one there.
* :meth:`Browser.launch` -- start Edge/Chrome with a separate, locum-managed
  profile (``~/.locum/browser/<name>``) and a debugging port. Sign-ins made in
  that profile stay there; ``headless=True`` runs it without a window.

DevTools gives full control of the browser (pages, cookies, downloads): only
attach to browsers you started for automation.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence

from ..errors import ElementNotFound, LocumError, NotSupported
from ._ws import WebSocket, WebSocketError

BROWSERS = ("edge", "chrome", "chromium")


class CDPError(LocumError):
    """A DevTools Protocol call failed (or no browser connection is available)."""


# ------------------------------------------------------------------ discovery
def _user_data_dirs() -> Dict[str, List[pathlib.Path]]:
    home = pathlib.Path.home()
    if sys.platform == "win32":
        local = pathlib.Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return {"edge": [local / "Microsoft" / "Edge" / "User Data"],
                "chrome": [local / "Google" / "Chrome" / "User Data"],
                "chromium": [local / "Chromium" / "User Data"]}
    if sys.platform == "darwin":
        sup = home / "Library" / "Application Support"
        return {"edge": [sup / "Microsoft Edge"], "chrome": [sup / "Google" / "Chrome"], "chromium": [sup / "Chromium"]}
    cfg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return {"edge": [cfg / "microsoft-edge"], "chrome": [cfg / "google-chrome"], "chromium": [cfg / "chromium"]}


def managed_profile_dir(browser: str) -> pathlib.Path:
    """Where :meth:`Browser.launch` keeps the locum-managed profile of ``browser``."""
    base = os.environ.get("LOCUM_BROWSER_PROFILES")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".locum" / "browser"
    return root / browser


def find_executable(browser: str) -> Optional[str]:
    """Path of the ``edge`` / ``chrome`` / ``chromium`` executable, or ``None``."""
    browser = browser.lower()
    cands: List[str] = []
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rel = {"edge": r"Microsoft\Edge\Application\msedge.exe", "chrome": r"Google\Chrome\Application\chrome.exe",
               "chromium": r"Chromium\Application\chrome.exe"}.get(browser)
        if rel:
            cands += [os.path.join(p, rel) for p in pf if p]
        names = {"edge": ["msedge"], "chrome": ["chrome"], "chromium": ["chromium"]}.get(browser, [])
    elif sys.platform == "darwin":
        app = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "Chromium"}.get(browser)
        if app:
            cands.append(f"/Applications/{app}.app/Contents/MacOS/{app}")
        names = []
    else:
        names = {"edge": ["microsoft-edge", "microsoft-edge-stable"],
                 "chrome": ["google-chrome", "google-chrome-stable"],
                 "chromium": ["chromium", "chromium-browser"]}.get(browser, [])
    for c in cands:
        if os.path.isfile(c):
            return c
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _http_json(host: str, port: int, path: str, method: str = "GET", timeout: float = 2.0) -> Any:
    req = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost DevTools only
        return json.loads(resp.read().decode("utf-8"))


def _active_port_file(profile: pathlib.Path) -> Optional[int]:
    try:
        first = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0].strip()
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


#: The conventional ``--remote-debugging-port``; only tried when attaching to any browser is allowed.
_DEFAULT_PORT = 9222
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def attach_any_allowed() -> bool:
    """``$LOCUM_CDP_ATTACH_ANY`` is set to ``1`` / ``true`` / ``yes``."""
    return os.environ.get("LOCUM_CDP_ATTACH_ANY", "").strip().lower() in ("1", "true", "yes", "on")


def discover_ports(allow_any: bool = False) -> List[int]:
    """Candidate DevTools ports, in order: ``$LOCUM_CDP_PORT``, then the ``DevToolsActivePort``
    files of locum-managed profiles (:func:`managed_profile_dir`).

    Only with ``allow_any`` (or ``$LOCUM_CDP_ATTACH_ANY=1``) also the ``DevToolsActivePort``
    files of the browsers' default (everyday) profiles and port 9222 -- whatever answers
    there may be your signed-in profile or another program, so it is never picked silently.
    """
    allow_any = allow_any or attach_any_allowed()
    ports: List[int] = []
    env = os.environ.get("LOCUM_CDP_PORT", "").strip()
    if env.isdigit():
        ports.append(int(env))
    dirs = [managed_profile_dir(name) for name in BROWSERS]
    if allow_any:
        dirs += [d for ds in _user_data_dirs().values() for d in ds]
    for d in dirs:
        p = _active_port_file(d)
        if p and p not in ports:
            ports.append(p)
    if allow_any and _DEFAULT_PORT not in ports:
        ports.append(_DEFAULT_PORT)
    return ports


def _is_local_ws(url: Any) -> bool:
    """``url`` is a ``ws://`` URL on this machine (127.0.0.1 / localhost / ::1)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
        return parts.scheme == "ws" and (parts.hostname or "").lower() in _LOCAL_HOSTS
    except ValueError:
        return False


@dataclass
class PageInfo:
    """One DevTools target of type ``page`` (a tab)."""

    id: str
    title: str
    url: str
    ws_url: str

    def __str__(self) -> str:
        return f"{self.title!r} <{self.url}>"


# ------------------------------------------------------------------ sessions
class CDPSession:
    """One DevTools WebSocket: request/response by id; events are kept briefly."""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        try:
            self.ws = WebSocket(ws_url, timeout=timeout)
        except (OSError, WebSocketError) as exc:
            raise CDPError(f"cannot connect to DevTools at {ws_url}: {exc}") from exc
        self._id = 0
        self._lock = threading.Lock()
        self.events: Deque[Dict[str, Any]] = deque(maxlen=500)
        self._broken = False

    @property
    def broken(self) -> bool:
        """The connection failed (or its stream can no longer be trusted): make a new session."""
        return self._broken or self.ws.closed

    def _fail(self, exc: BaseException) -> CDPError:
        self._broken = True
        try:
            self.ws.close()
        except Exception:
            pass
        return CDPError(f"DevTools connection lost: {exc}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        from .._cancel import check

        # Before anything goes out: an MCP call that timed out or was cancelled (possibly while
        # waiting for this lock) must not send input -- an ``Input.*`` call -- afterwards.
        check()
        with self._lock:
            check()
            if self.broken:
                raise CDPError("DevTools connection lost (the session is closed)")
            self._id += 1
            mid = self._id
            try:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            except (OSError, WebSocketError) as exc:
                raise self._fail(exc) from exc
            end = time.monotonic() + timeout
            while True:
                check()  # an MCP call that timed out / was cancelled stops here
                left = end - time.monotonic()
                if left <= 0:
                    raise CDPError(f"DevTools call {method} timed out after {timeout:g}s")
                try:
                    msg = json.loads(self.ws.recv(timeout=min(left, 0.5)))
                except socket.timeout:
                    continue  # partial frames stay buffered in the WebSocket (no desync)
                except (OSError, WebSocketError, ValueError) as exc:
                    raise self._fail(exc) from exc
                if msg.get("id") == mid:
                    if "error" in msg:
                        err = msg["error"]
                        raise CDPError(f"{method}: {err.get('message', err)}")
                    return msg.get("result", {})
                if "method" in msg:
                    self.events.append(msg)

    def close(self) -> None:
        self.ws.close()


# ------------------------------------------------------------------ JS helpers
#: ``__locumName(e)``: the name of an element. Never what a user typed or the page (or a
#: password manager) filled in: an input's value names only buttons (``type=submit`` /
#: ``button`` / ``reset``, whose value *is* the label); text fields, textareas, selects and
#: contenteditable regions are named by aria-label, label, placeholder or title only.
_NAME_FN = r"""
const __locumName = (e) => {
  const tag = e.tagName.toLowerCase();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const field = tag === 'input' || tag === 'textarea' || tag === 'select' || e.isContentEditable;
  const lab = e.labels && e.labels.length ? e.labels[0].innerText : '';
  const btnValue = tag === 'input' && ['submit', 'button', 'reset'].includes(type) ? e.value : '';
  return (e.getAttribute('aria-label') || lab || (field ? '' : e.innerText) || e.getAttribute('placeholder')
          || e.getAttribute('title') || e.getAttribute('alt') || btnValue || '').trim().replace(/\s+/g, ' ');
};
"""

_ELEMENTS_JS = r"""
(max) => {
""" + _NAME_FN + r"""
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[contenteditable=""],[contenteditable=true],'
    + '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],'
    + '[role=combobox],[role=textbox],[role=searchbox],[onclick],[tabindex]:not([tabindex="-1"])';
  const out = [];
  document.querySelectorAll('[data-locum-id]').forEach(e => e.removeAttribute('data-locum-id'));
  const vw = innerWidth, vh = innerHeight;
  for (const e of document.querySelectorAll(sel)) {
    if (out.length >= max) break;
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    let role = e.getAttribute('role') || ({a: 'link', button: 'button', select: 'combobox', textarea: 'edit', summary: 'button'})[tag] || '';
    if (tag === 'input') role = ({checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button',
                                  image: 'button', range: 'slider'})[type] || 'edit';
    if (!role) role = e.isContentEditable ? 'edit' : 'button';
    const name = __locumName(e);
    const secret = type === 'password';
    let value = null;
    if (tag === 'input' || tag === 'textarea' || tag === 'select') value = secret ? (e.value ? '***' : '') : e.value;
    const states = [];
    if (e.disabled) states.push('disabled');
    if (e.checked) states.push('checked');
    if (document.activeElement === e) states.push('focused');
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) states.push('offscreen');
    if (secret) states.push('password');
    const id = out.length + 1;
    e.setAttribute('data-locum-id', String(id));
    out.push({id, role, name: name.slice(0, 120), value: value === null ? null : String(value).slice(0, 120), tag,
              x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
              w: Math.round(r.width), h: Math.round(r.height), states});
  }
  return {title: document.title, url: location.href, vw, vh, scrollY: Math.round(scrollY),
          docH: Math.round(document.documentElement.scrollHeight), elements: out};
}
"""

_RESOLVE_JS = r"""
(css, scroll) => {
""" + _NAME_FN + r"""
  const e = document.querySelector(css);
  if (!e) return null;
  if (scroll) e.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = e.getBoundingClientRect();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const name = __locumName(e).slice(0, 80);
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height,
          password: type === 'password', name, tag: e.tagName.toLowerCase(), disabled: !!e.disabled};
}
"""

#: What Enter in ``css`` (default: the focused element) will activate, so the policy can
#: judge a ``submit=True`` like a click on that control. ``kind``: ``self`` (a button or
#: link: Enter presses it), ``form`` (implicit submission: ``label`` is the form's default
#: button -- its first submit button --, ``action`` / ``form`` describe the form),
#: ``nearby`` (no form: the closest button of the field's container, which page scripts
#: usually wire to Enter -- chat "Send", search "Go"), ``none`` (nothing found), or
#: ``null`` (no such element / nothing focused). Labels never come from field values.
_ENTER_TARGET_JS = r"""
(css) => {
""" + _NAME_FN + r"""
  const e = css ? document.querySelector(css) : document.activeElement;
  if (!e || e === document.body || e === document.documentElement) return null;
  const tag = e.tagName.toLowerCase();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const short = (t) => String(t || '').slice(0, 80);
  if (tag === 'button' || tag === 'a' || tag === 'summary' || (tag === 'input' && ['submit', 'button', 'reset', 'image',
      'checkbox', 'radio'].includes(type)) || ['button', 'link', 'menuitem', 'tab'].includes(e.getAttribute('role') || ''))
    return {kind: 'self', label: short(__locumName(e)), form: '', action: ''};
  const form = e.form || e.closest('form');
  if (form) {
    const def = Array.from(form.elements).find(b => (b.tagName === 'BUTTON' || b.tagName === 'INPUT')
                                                    && (b.type === 'submit' || b.type === 'image'));
    return {kind: 'form', label: def ? short(__locumName(def) || def.getAttribute('name') || 'submit') : '',
            form: short(form.getAttribute('aria-label') || form.getAttribute('name') || form.id || ''),
            action: short(form.getAttribute('action') || '')};
  }
  const btn = 'button,input[type=submit],input[type=button],input[type=image],[role=button]';
  let c = e.parentElement;
  for (let i = 0; c && i < 6 && c !== document.body; i++, c = c.parentElement) {
    const b = Array.from(c.querySelectorAll(btn)).find(b => b !== e && !b.disabled && b.getClientRects().length);
    if (b) return {kind: 'nearby', label: short(__locumName(b)), form: '', action: ''};
  }
  return {kind: 'none', label: '', form: '', action: ''};
}
"""


class Page:
    """One tab, over its own DevTools session."""

    def __init__(self, info: PageInfo, browser: "Browser") -> None:
        self.info = info
        self.browser = browser
        self.session = CDPSession(info.ws_url)

    # -------------------------------------------------------------- basics
    def evaluate(self, expression: str, *, await_promise: bool = True, timeout: float = 30.0) -> Any:
        res = self.session.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                     "awaitPromise": await_promise}, timeout=timeout)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise CDPError(f"page script failed: {d.get('exception', {}).get('description') or d.get('text')}")
        return res.get("result", {}).get("value")

    def call_js(self, fn_source: str, *args: Any, timeout: float = 30.0) -> Any:
        """Run ``(fn_source)(...args)`` in the page; arguments travel as JSON (no string splicing)."""
        return self.evaluate(f"({fn_source})(...{json.dumps(list(args))})", timeout=timeout)

    @property
    def title(self) -> str:
        return str(self.evaluate("document.title") or "")

    @property
    def url(self) -> str:
        return str(self.evaluate("location.href") or "")

    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        self.session.call("Page.navigate", {"url": url}, timeout=timeout)
        self.wait_ready(timeout)

    def wait_ready(self, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.evaluate("document.readyState", timeout=5) == "complete":
                    return
            except CDPError:
                pass  # navigating: the old context is gone
            time.sleep(0.1)
        raise CDPError(f"page did not finish loading within {timeout:g}s")

    def text(self, css: Optional[str] = None, max_chars: int = 20000) -> str:
        t = self.call_js("(css) => { const e = css ? document.querySelector(css) : document.body; return e ? e.innerText : null; }", css)
        if t is None:
            raise ElementNotFound(f"no element matches {css!r} on the page")
        return str(t)[:max_chars]

    def snapshot(self, max_elements: int = 200) -> Dict[str, Any]:
        """Interactive elements (ids ``1..``, valid until the next snapshot), title, url, viewport."""
        return self.call_js(_ELEMENTS_JS, int(max_elements))

    def resolve(self, target: Any, *, scroll: bool = True) -> Dict[str, Any]:
        """``target`` (snapshot id or CSS selector) -> center in viewport CSS pixels and facts."""
        css = f'[data-locum-id="{int(target)}"]' if isinstance(target, int) or str(target).isdigit() else str(target)
        info = self.call_js(_RESOLVE_JS, css, bool(scroll))
        if info is None:
            raise ElementNotFound(f"no element {target!r} on the page (snapshot ids change with every browser_observe)")
        return info

    def enter_target(self, target: Any = None) -> Optional[Dict[str, Any]]:
        """What pressing Enter in ``target`` (snapshot id / CSS; default: the focused element)
        activates: ``{"kind", "label", "form", "action"}`` (see ``_ENTER_TARGET_JS``), or ``None``."""
        css = None
        if target is not None:
            css = f'[data-locum-id="{int(target)}"]' if isinstance(target, int) or str(target).isdigit() else str(target)
        return self.call_js(_ENTER_TARGET_JS, css)

    # -------------------------------------------------------------- input
    def click(self, target: Any) -> Dict[str, Any]:
        info = self.resolve(target)
        if info.get("disabled"):
            raise CDPError(f"element {target!r} is disabled")
        x, y = info["x"], info["y"]
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": kind, "x": x, "y": y, "button": "left" if kind != "mouseMoved" else "none", "clickCount": 1}
            self.session.call("Input.dispatchMouseEvent", p)
        return info

    def type(self, text: str, target: Any = None, *, clear: bool = False, submit: bool = False) -> Optional[Dict[str, Any]]:
        info = None
        if target is not None:
            info = self.click(target)
        if clear:
            self.evaluate("(() => { const e = document.activeElement; if (e && 'value' in e) { e.select && e.select(); "
                          "e.value = ''; e.dispatchEvent(new Event('input', {bubbles: true})); } })()")
        if text:
            self.session.call("Input.insertText", {"text": text})
        if submit:
            for kind in ("keyDown", "keyUp"):
                self.session.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                             "windowsVirtualKeyCode": 13, "text": "\r" if kind == "keyDown" else ""})
        return info

    def focused_is_password(self) -> bool:
        return bool(self.evaluate("(() => { const e = document.activeElement; return !!e && (e.getAttribute('type') || '').toLowerCase() === 'password'; })()"))

    def screenshot_png(self) -> bytes:
        import base64

        res = self.session.call("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(res["data"])

    def wait_for(self, css: str, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while True:
            if self.call_js("(css) => !!document.querySelector(css)", css):
                return
            if time.monotonic() >= end:
                raise ElementNotFound(f"no element matches {css!r} within {timeout:g}s")
            time.sleep(0.1)

    def close(self) -> None:
        self.session.close()


class Browser:
    """A DevTools connection to one browser (attached or launched)."""

    def __init__(self, host: str, port: int, *, process: Any = None, name: str = "", profile: Optional[pathlib.Path] = None) -> None:
        self.host, self.port = host, int(port)
        self.process = process
        self.profile = profile
        if str(host).lower() not in _LOCAL_HOSTS:
            raise CDPError(f"DevTools host {host!r} is not this machine (only 127.0.0.1 / localhost / ::1)")
        self.version = _http_json(host, port, "/json/version")
        if not isinstance(self.version, dict) or not self.version.get("Browser") \
                or not self.version.get("Protocol-Version") or not self.version.get("webSocketDebuggerUrl"):
            raise CDPError(f"port {port} does not answer like a Chromium DevTools endpoint (/json/version)")
        if not _is_local_ws(self.version["webSocketDebuggerUrl"]):
            raise CDPError(f"port {port}: the DevTools WebSocket {self.version['webSocketDebuggerUrl']!r} is not local")
        product = str(self.version.get("Browser", ""))
        self.name = name or ("edge" if "Edg" in product else "chrome" if "Chrome" in product else "chromium")
        self.managed = process is not None or profile is not None
        self._pages: Dict[str, Page] = {}
        self._pid: Optional[int] = None

    # -------------------------------------------------------------- creation
    @classmethod
    def attach(cls, port: Optional[int] = None, host: str = "127.0.0.1", *, allow_any: bool = False) -> "Browser":
        """Connect to a browser that already exposes DevTools on this machine.

        ``port`` given: exactly that port. Otherwise, in order, ``$LOCUM_CDP_PORT`` and the
        browsers running with a locum-managed profile (:meth:`launch`). The everyday
        profiles' ``DevToolsActivePort`` and port 9222 are tried only with ``allow_any=True``
        or ``$LOCUM_CDP_ATTACH_ANY=1``: whatever listens there may be your signed-in
        browser or another program. Every endpoint must answer ``/json/version`` like a
        Chromium DevTools server whose WebSocket is local (127.0.0.1 / localhost / ::1).
        """
        allow = allow_any or attach_any_allowed()
        ports = [int(port)] if port else (discover_ports(allow_any=True) if allow else discover_ports())
        errors = []
        for p in ports:
            try:
                return cls(host, p)
            except CDPError as exc:
                errors.append(f"{p}: {exc}")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{p}: {exc.__class__.__name__}")
        tried = ", ".join(map(str, ports)) or "none"
        rejected = [e for e in errors if "DevTools" in e or "not local" in e]
        hint = "" if port or allow else (
            " Browsers with your everyday profile or on port 9222 are not attached to unless you pass "
            "Browser.attach(port=...) / Browser.attach(allow_any=True) or set LOCUM_CDP_ATTACH_ANY=1.")
        raise CDPError(
            f"No locum-usable browser with a DevTools port found (tried {tried})."
            + (" Rejected: " + "; ".join(rejected) + "." if rejected else "")
            + " Start one with browser_launch / Browser.launch(), or start Edge/Chrome yourself with "
            "--remote-debugging-port=9222 --user-data-dir=<a separate profile folder> and set LOCUM_CDP_PORT=9222."
            + hint)

    @classmethod
    def launch(cls, browser: str = "edge", *, url: Optional[str] = None, profile_dir: Optional[str] = None,
               headless: bool = False, timeout: float = 30.0, extra_args: Sequence[str] = ()) -> "Browser":
        """Start ``browser`` with a locum-managed profile and a DevTools port (reuses a running one)."""
        browser = browser.lower()
        if browser not in BROWSERS:
            raise ValueError(f"browser must be one of {BROWSERS}, got {browser!r}")
        exe = find_executable(browser)
        if exe is None:
            raise NotSupported(f"{browser} is not installed (or not where locum looks for it)")
        profile = pathlib.Path(profile_dir) if profile_dir else managed_profile_dir(browser)
        profile.mkdir(parents=True, exist_ok=True)
        existing = _active_port_file(profile)
        if existing:
            try:
                b = cls("127.0.0.1", existing, name=browser, profile=profile)
                if url:
                    b.new_page(url)
                return b
            except (CDPError, OSError, ValueError, urllib.error.URLError):
                try:
                    (profile / "DevToolsActivePort").unlink()
                except OSError:
                    pass
        args = [exe, "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
                "--no-default-browser-check", *(["--headless=new"] if headless else []), *extra_args, url or "about:blank"]
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            port = _active_port_file(profile)
            if port:
                try:
                    return cls("127.0.0.1", port, process=proc, name=browser, profile=profile)
                except (CDPError, OSError, ValueError, urllib.error.URLError):
                    pass
            if proc.poll() is not None and not port:
                break
            time.sleep(0.2)
        raise CDPError(f"{browser} did not open a DevTools port within {timeout:g}s")

    # -------------------------------------------------------------- pages
    def pages(self) -> List[PageInfo]:
        out = []
        for t in _http_json(self.host, self.port, "/json/list"):
            # A target whose WebSocket is not on this machine is never connected to.
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl") and _is_local_ws(t["webSocketDebuggerUrl"]):
                out.append(PageInfo(t["id"], t.get("title", ""), t.get("url", ""), t["webSocketDebuggerUrl"]))
        return out

    def page(self, match: Optional[str] = None, timeout: float = 5.0) -> Page:
        """The tab whose title or URL contains ``match`` (default: the most recently used tab).

        Waits up to ``timeout`` seconds for such a tab: a page that is still loading
        has no title yet."""
        end = time.monotonic() + max(0.0, float(timeout))
        while True:
            pages = self.pages()
            if match:
                m = match.lower()
                pages = [p for p in pages if m in p.title.lower() or m in p.url.lower()]
            if pages or time.monotonic() >= end:
                break
            time.sleep(0.1)
        if not pages:
            raise ElementNotFound(f"no browser tab matches {match!r}" if match else "the browser has no open tab")
        info = pages[0]
        cached = self._pages.get(info.id)
        if cached is not None and not cached.session.broken:
            cached.info = info
            return cached
        if cached is not None:
            try:
                cached.close()
            except Exception:
                pass
        page = Page(info, self)
        self._pages[info.id] = page
        return page

    def new_page(self, url: str = "about:blank") -> Page:
        """Open a new tab on ``url`` (its ``#fragment`` included).

        The tab is created blank and then navigated over DevTools: ``/json/new?<url>``
        cannot carry a fragment (HTTP clients strip it from the request)."""
        t = _http_json(self.host, self.port, "/json/new?about:blank", method="PUT")
        if not _is_local_ws(t.get("webSocketDebuggerUrl")):
            raise CDPError(f"the new tab's DevTools WebSocket {t.get('webSocketDebuggerUrl')!r} is not local")
        info = PageInfo(t["id"], t.get("title", ""), t.get("url", ""), t["webSocketDebuggerUrl"])
        page = Page(info, self)
        self._pages[info.id] = page
        if url and url != "about:blank":
            page.session.call("Page.navigate", {"url": url})
            info.url = url
            end = time.monotonic() + 5.0  # until the new document replaced about:blank
            while time.monotonic() < end:
                try:
                    if page.evaluate("location.href", timeout=2) != "about:blank":
                        break
                except CDPError:
                    pass
                time.sleep(0.05)
        return page

    @property
    def pid(self) -> Optional[int]:
        """Process id of the browser's main process (owns its top-level windows)."""
        if self._pid is None:
            try:
                s = CDPSession(self.version["webSocketDebuggerUrl"])
                try:
                    procs = s.call("SystemInfo.getProcessInfo", timeout=5).get("processInfo", [])
                finally:
                    s.close()
                self._pid = next((int(p["id"]) for p in procs if p.get("type") == "browser"), None)
            except Exception:
                self._pid = None
            if self._pid is None and self.process is not None:
                self._pid = getattr(self.process, "pid", None)
        return self._pid

    @property
    def alive(self) -> bool:
        try:
            _http_json(self.host, self.port, "/json/version", timeout=1.0)
            return True
        except Exception:
            return False

    @property
    def app(self) -> str:
        """Process name the safety policy matches (``msedge.exe``, ``chrome.exe``, ...)."""
        if sys.platform == "win32":
            return {"edge": "msedge.exe", "chrome": "chrome.exe", "chromium": "chrome.exe"}.get(self.name, self.name)
        return {"edge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "Chromium"}.get(self.name, self.name)

    def close(self, *, quit: bool = False) -> None:
        """Drop the DevTools sessions; ``quit=True`` also closes a browser locum launched."""
        for p in self._pages.values():
            try:
                p.close()
            except Exception:
                pass
        self._pages.clear()
        if quit and self.process is not None:
            try:
                s = CDPSession(self.version["webSocketDebuggerUrl"])
                s.call("Browser.close", timeout=5)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass


__all__ = ["BROWSERS", "Browser", "CDPError", "CDPSession", "Page", "PageInfo", "attach_any_allowed", "discover_ports",
           "find_executable", "managed_profile_dir"]
