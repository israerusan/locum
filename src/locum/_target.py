"""Input target binding: input that is meant for one window (or one element) only.

Keyboard input goes wherever the operating system's focus is, and a click lands
on whatever window is at the point. A focus change between finding a target and
sending input to it -- a notification, a dialog, the user clicking elsewhere, a
chat window stealing the foreground -- would send the text (and its Enter) to
another application. Checking once before typing is not enough: the focus can
change while a long text is being typed.

:func:`bind` installs an :class:`InputTarget` for the calls made on this thread
(the same pattern as :mod:`locum._cancel`). The keyboard and mouse call
:func:`check_input` immediately before every key press, typed text unit,
button press and scroll that reaches the backend, and the guard raises
:class:`~locum.errors.TargetLost` -- nothing is sent -- when:

* keyboard input: the foreground window is no longer the target window (or a
  window it owns: its dialog, its menu); with an element target, keyboard focus
  is known to be outside the element (a Tab or Enter typed *through* the guard
  may move the focus within the window: after one, only the window is checked);
* a button press or scroll: the top-level window at the pointer is not the
  target window (another window covers the point).

Releases are never checked: a held key or button is always released.

When the backend cannot tell (no window support, a failed query) a *strict*
target refuses (explicit bindings: the CLI's ``--window``) and a lenient one lets
the input through (the automatic binding of :class:`~locum.element.Element`
actions, which already proved focus before typing).
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, Iterator, List, Optional, Tuple

from .errors import TargetLost

_local = threading.local()

#: Keys after which the focus may legitimately move to another control of the window.
FOCUS_MOVING_KEYS = frozenset({"tab", "enter", "return", "escape", "esc", "f6"})


def _safe(fn: Any) -> Tuple[bool, Any]:
    try:
        return True, fn()
    except Exception:
        return False, None


class InputTarget:
    """The window (and optionally element) the input of a ``with bind(...)`` block is for.

    Args:
        backend: The backend that sends the input.
        handle: Native handle of the target top-level window.
        element: An :class:`~locum.element.Element` that must keep keyboard focus.
        title: For messages.
        strict: Refuse input when the backend cannot tell where it would go.
    """

    def __init__(self, backend: Any, handle: Any, *, element: Any = None, title: str = "",
                 strict: bool = True) -> None:
        self.backend = backend
        self.handle = handle
        self.element = element
        self.title = title
        self.strict = strict
        self._element_focus = element is not None

    # ------------------------------------------------------------------ helpers
    def _what(self) -> str:
        return repr(self.title) if self.title else "the target window"

    def _belongs(self, handle: Any) -> Optional[bool]:
        if handle is None:
            return None
        if handle == self.handle:
            return True
        ok, res = _safe(lambda: self.backend.window_belongs_to(handle, self.handle))
        return bool(res) if ok else None

    def _unknown(self, what: str) -> None:
        if self.strict:
            raise TargetLost(f"{what} for {self._what()} was not sent: the backend cannot tell which window "
                             "would receive it, so it cannot be kept from reaching another application")

    # -------------------------------------------------------------------- check
    def check(self, kind: str, key: Optional[str] = None, point: Optional[Tuple[int, int]] = None) -> None:
        if kind in ("key", "text"):
            self._check_keyboard(kind, key)
        elif kind in ("button", "scroll"):
            self._check_pointer(kind, point)

    def _check_keyboard(self, kind: str, key: Optional[str]) -> None:
        what = f"key {key!r}" if kind == "key" and key else "typed text"
        ok, fg = _safe(self.backend.foreground_handle)
        belongs = self._belongs(fg) if ok else None
        if belongs is None:
            self._unknown(what.capitalize())
        elif not belongs:
            raise TargetLost(
                f"{what.capitalize()} for {self._what()} was not sent: another window is now in the "
                "foreground (it took the focus after the target was found). Nothing was typed into it; "
                "activate the target again and retry."
            )
        if self._element_focus:
            ok, state = _safe(lambda: self.element._has_focus(True))
            if ok and state is False:
                raise TargetLost(
                    f"{what.capitalize()} for {self.element!r} was not sent: keyboard focus moved to "
                    "another control after the element was focused."
                )
        if kind == "key" and key and key.lower() in FOCUS_MOVING_KEYS:
            self._element_focus = False  # the focus may now be on the next control of the window

    def _check_pointer(self, kind: str, point: Optional[Tuple[int, int]]) -> None:
        what = "click" if kind == "button" else "scroll"
        if point is None:
            ok, pos = _safe(self.backend.mouse_position)
            if not ok or pos is None:
                return self._unknown(what.capitalize())
            point = (int(getattr(pos, "x", pos[0])), int(getattr(pos, "y", pos[1])))
        x, y = point
        ok, at = _safe(lambda: self.backend.window_handle_at(x, y))
        belongs = self._belongs(at) if ok else None
        if belongs is None:
            return self._unknown(what.capitalize())
        if not belongs:
            raise TargetLost(
                f"The {what} at ({x}, {y}) for {self._what()} was not sent: another window covers that "
                "point. Bring the target to the front (or close what covers it) and retry."
            )
        return None


def _stack() -> List[InputTarget]:
    st = getattr(_local, "stack", None)
    if st is None:
        st = _local.stack = []
    return st


@contextlib.contextmanager
def bind(target: Optional[InputTarget]) -> Iterator[Optional[InputTarget]]:
    """Check every input sent on this thread inside the block against ``target`` (``None``: no-op)."""
    if target is None:
        yield None
        return
    st = _stack()
    st.append(target)
    try:
        yield target
    finally:
        st.remove(target)


def check_input(kind: str, key: Optional[str] = None, point: Optional[Tuple[int, int]] = None) -> None:
    """Called right before input reaches the backend: raise :class:`TargetLost` if a bound
    target would not receive it. ``kind``: ``key`` / ``text`` / ``button`` / ``scroll``."""
    st = getattr(_local, "stack", None)
    if not st:
        return
    for target in list(st):
        target.check(kind, key=key, point=point)


def current() -> Optional[InputTarget]:
    st = getattr(_local, "stack", None)
    return st[-1] if st else None


def for_window(window: Any, *, element: Any = None, strict: bool = True) -> Optional[InputTarget]:
    """An :class:`InputTarget` for a :class:`~locum.window.Window` (``None`` without a handle)."""
    handle = getattr(window, "handle", None)
    if handle is None:
        return None
    return InputTarget(window.backend, handle, element=element, title=getattr(window, "title", "") or "",
                       strict=strict)


__all__ = ["FOCUS_MOVING_KEYS", "InputTarget", "bind", "check_input", "current", "for_window"]
