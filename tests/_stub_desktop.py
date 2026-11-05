"""A minimal stand-in for ``locum.desktop.Desktop`` used by the element/window tests.

It implements only the Desktop API that :mod:`locum.element` and
:mod:`locum.window` rely on (see docs/ARCHITECTURE.md), on top of the
MockBackend, the real Mouse/Keyboard classes and the selector engine.
"""

from __future__ import annotations

from typing import Any, List, Optional

from locum.backends.mock import MockBackend
from locum.element import Element
from locum.errors import ActionTimeout, ElementNotFound, WindowNotFound
from locum.keyboard import Keyboard
from locum.mouse import Mouse
from locum.selectors import parse
from locum.wait import wait_until
from locum.window import Window, match_window


def _noop(_s: float) -> None:
    return None


class StubDesktop:
    def __init__(self, backend: Optional[MockBackend] = None, default_timeout: float = 0.05, typing_mode: str = "auto"):
        self.backend = backend or MockBackend(width=1280, height=800)
        self.mouse = Mouse(self.backend, sleep=_noop)
        self.keyboard = Keyboard(self.backend, sleep=_noop, clipboard_settle=0)
        self.default_timeout = default_timeout
        self.typing_mode = typing_mode
        self.slept: List[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)

    def roots(self):
        return self.backend.accessibility_roots()

    def find(self, selector: Any, *, timeout: Optional[float] = None, root: Any = None) -> Element:
        if root is not None:
            return root.find(selector, timeout=timeout)
        sel = parse(selector)
        t = self.default_timeout if timeout is None else timeout
        try:
            node = wait_until(lambda: sel.find_first(self.roots()), t, 0.01, sleep=self.sleep)
        except ActionTimeout as exc:
            raise ElementNotFound(str(sel)) from exc
        return Element(node, self, selector if isinstance(selector, str) else str(sel))

    def find_all(self, selector: Any, *, root: Any = None) -> List[Element]:
        if root is not None:
            return root.find_all(selector)
        sel = parse(selector)
        return [Element(n, self, selector) for n in sel.find_all(self.roots())]

    def windows(self, title: Any = None, *, app: Any = None) -> List[Window]:
        return [Window(i, self) for i in self.backend.list_windows() if match_window(i, title, app)]

    def window(self, title: Any = None, *, app: Any = None, timeout: Optional[float] = None) -> Window:
        ws = self.windows(title, app=app)
        if not ws:
            raise WindowNotFound(str(title))
        return ws[0]
