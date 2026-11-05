"""High-level keyboard control: key taps, hotkeys, held keys and text entry.

:class:`Keyboard` wraps the backend's instant ``key_down``/``key_up``/``type_text``
primitives and adds:

* alias-tolerant key names (``"Ctrl"``, ``"Return"``, ``"⌘"``, ``"mod"`` ...);
* hotkeys that release keys in reverse order **in a finally block**, so a
  failure halfway through never leaves a modifier stuck down;
* four text-entry strategies -- ``unicode`` (layout independent injection),
  ``keys`` (per-character key taps with automatic shift), ``clipboard``
  (paste, with the user's clipboard saved and restored) and ``auto``;
* bookkeeping of every key this instance holds, so :meth:`Keyboard.release_all`
  can always clean up.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple, Union

from .backends.base import CAP_CLIPBOARD, CAP_UNICODE_TYPING
from .errors import NotSupported
from .failsafe import FailSafe
from .keys import SHIFTED_CHARS, char_needs_shift, normalize_key, parse_hotkey, split_text_tokens

KeySpec = Union[str, Sequence[str]]

TYPING_MODES = ("auto", "unicode", "keys", "clipboard")

# A typing "unit": ("text", s) inject via type_text, ("char", c) key tap with
# shift handling, ("key", name) canonical key tap, ("paste", s) clipboard paste.
_Unit = Tuple[str, str]


def _is_key_typable(ch: str) -> bool:
    """Characters that can be reliably produced with US-layout key taps."""
    return ch in ("enter", "tab") or (len(ch) == 1 and 32 <= ord(ch) < 127)


class Keyboard:
    """Keyboard controller bound to a backend.

    Args:
        backend: The platform backend.
        failsafe: Optional :class:`FailSafe` checked before presses and between typed characters.
        pause: Seconds to sleep after every public action (PyAutoGUI's ``PAUSE``).
        sleep: Sleep function (inject ``lambda s: None`` in tests).
        clipboard_settle: Seconds to wait after a paste before restoring the
            clipboard (some apps read the clipboard asynchronously).
    """

    def __init__(
        self,
        backend: Any,
        failsafe: Optional[FailSafe] = None,
        pause: float = 0.0,
        sleep: Callable[[float], Any] = time.sleep,
        *,
        clipboard_settle: float = 0.05,
    ):
        self.backend = backend
        self.failsafe = failsafe
        self.pause = float(pause)
        self.clipboard_settle = float(clipboard_settle)
        self._sleep = sleep
        self._pressed: List[str] = []  # keys held by this instance, in press order

    # ------------------------------------------------------------------ helpers
    def _check(self) -> None:
        if self.failsafe is not None:
            self.failsafe.check()

    def _pause(self) -> None:
        if self.pause > 0:
            self._sleep(self.pause)

    @property
    def pressed(self) -> List[str]:
        """Keys currently held down by this instance (press order)."""
        return list(self._pressed)

    def _raw_down(self, key: str) -> None:
        self.backend.key_down(key)
        if key not in self._pressed:
            self._pressed.append(key)

    def _raw_up(self, key: str) -> None:
        try:
            self.backend.key_up(key)
        finally:
            if key in self._pressed:
                self._pressed.remove(key)

    def _release_many(self, keys: Sequence[str]) -> Optional[BaseException]:
        """Release ``keys`` in the given order; keep going on errors and return the first one."""
        first: Optional[BaseException] = None
        for k in keys:
            try:
                self._raw_up(k)
            except Exception as exc:  # pragma: no cover - backend failure path
                if first is None:
                    first = exc
        return first

    @staticmethod
    def _parse_keys(keys: Sequence[KeySpec]) -> List[str]:
        """Flatten ``("ctrl+shift", "s")`` / ``(["ctrl", "c"],)`` into canonical key names."""
        out: List[str] = []
        for spec in keys:
            if isinstance(spec, str):
                out.extend(parse_hotkey(spec))
            else:
                out.extend(parse_hotkey(list(spec)))
        if not out:
            raise ValueError("No keys given")
        return out

    def _tap(self, key: str) -> None:
        """Tap a canonical key or a character, adding shift for uppercase/shifted symbols."""
        if len(key) == 1 and char_needs_shift(key) and "shift" not in self._pressed and "shift_r" not in self._pressed:
            base = SHIFTED_CHARS.get(key, key.lower())
            self._raw_down("shift")
            try:
                self._raw_down(base)
                self._raw_up(base)
            finally:
                self._raw_up("shift")
            return
        self._raw_down(key)
        self._raw_up(key)

    @contextlib.contextmanager
    def _held(self, keys: Sequence[str], interval: float = 0.0) -> Iterator[List[str]]:
        """Press ``keys`` in order and release them in reverse order in ``finally``.

        Keys that this instance already holds are neither pressed nor released
        (so ``with hold("ctrl"): hotkey("ctrl", "c")`` keeps ctrl held).
        """
        pressed: List[str] = []
        try:
            for i, k in enumerate(keys):
                if k in self._pressed:
                    continue
                if i and interval > 0:
                    self._sleep(interval)
                self._raw_down(k)
                pressed.append(k)
            yield list(keys)
        finally:
            err = self._release_many(list(reversed(pressed)))
        if err is not None:
            raise err

    # --------------------------------------------------------------- public API
    def down(self, key: str) -> None:
        """Press and hold ``key`` (aliases accepted)."""
        k = normalize_key(key)
        self._check()
        self._raw_down(k)
        self._pause()

    def up(self, key: str) -> None:
        """Release ``key``. Never blocked by the fail-safe."""
        self._raw_up(normalize_key(key))
        self._pause()

    def press(self, *keys: KeySpec, presses: int = 1, interval: float = 0.0) -> None:
        """Tap each key in sequence, ``presses`` times: ``press("tab", "tab", "enter")``.

        An argument that is a combo (``"ctrl+c"``) or a list (``["ctrl", "c"]``)
        is performed as a hotkey. Uppercase letters and shifted symbols get shift
        automatically. ``interval`` seconds are waited between taps.
        """
        actions: List[Union[str, List[str]]] = []
        for spec in keys:
            if isinstance(spec, str):
                parsed = parse_hotkey(spec)
                actions.append(parsed[0] if len(parsed) == 1 else parsed)
            else:
                actions.append(parse_hotkey(list(spec)))
        if not actions:
            raise ValueError("press() needs at least one key")
        presses = int(presses)
        first = True
        for _ in range(max(0, presses)):
            for action in actions:
                if not first and interval > 0:
                    self._sleep(interval)
                first = False
                self._check()
                if isinstance(action, list):
                    with self._held(action):
                        pass
                else:
                    self._tap(action)
        self._pause()

    def hotkey(self, *keys: KeySpec, interval: float = 0.0) -> None:
        """Press a key combination: ``hotkey("ctrl+shift+s")`` or ``hotkey("ctrl", "c")``.

        Keys are pressed in order and released in reverse order inside a
        ``finally`` block, so modifiers are never left stuck even if pressing a
        later key fails or the fail-safe fires.
        """
        parsed = self._parse_keys(keys)
        self._check()
        with self._held(parsed, interval):
            pass
        self._pause()

    @contextlib.contextmanager
    def hold(self, *keys: KeySpec) -> Iterator[List[str]]:
        """Context manager holding ``keys`` down: ``with kb.hold("shift"): mouse.click(...)``."""
        parsed = self._parse_keys(keys)
        self._check()
        with self._held(parsed) as held:
            yield held
        self._pause()

    def release_all(self) -> List[str]:
        """Release every key this instance holds (reverse press order). Returns the released keys."""
        keys = list(reversed(self._pressed))
        self._release_many(keys)
        return keys

    # --------------------------------------------------------------------- typing
    def type(self, text: str, *, interval: float = 0.0, mode: str = "auto") -> None:
        """Type ``text``.

        Args:
            text: The text; ``\\n`` presses Enter and ``\\t`` presses Tab (except in clipboard mode,
                where text is pasted verbatim).
            interval: Seconds between characters.
            mode: ``"unicode"`` (backend unicode injection), ``"keys"`` (per-character key taps,
                shift added for uppercase/shifted symbols), ``"clipboard"`` (paste via ``mod+v``,
                restoring the previous clipboard), or ``"auto"`` (unicode when the backend supports
                it; otherwise key taps for ASCII and clipboard paste for other characters).
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        mode = (mode or "auto").lower()
        if mode not in TYPING_MODES:
            raise ValueError(f"Unknown typing mode {mode!r}; expected one of {', '.join(TYPING_MODES)}")
        if text == "":
            return
        self._check()
        if mode == "auto" and self.backend.has(CAP_UNICODE_TYPING):
            units = self._units(text, "unicode", interval)
            try:
                self._run_units(units[:1], interval, first=True)
            except NotSupported:
                # Backend advertised unicode typing but cannot do it right now: fall back.
                units = self._units(text, "fallback", interval)
                self._run_units(units, interval, first=True)
            else:
                self._run_units(units[1:], interval, first=False)
        else:
            self._run_units(self._units(text, "fallback" if mode == "auto" else mode, interval), interval, first=True)
        self._pause()

    def _units(self, text: str, mode: str, interval: float) -> List[_Unit]:
        if mode == "clipboard":
            return [("paste", text)]
        units: List[_Unit] = []
        if mode == "unicode":
            buf: List[str] = []
            for tok in split_text_tokens(text):
                if tok in ("enter", "tab"):
                    if buf:
                        units.append(("text", "".join(buf)))
                        buf = []
                    units.append(("key", tok))
                elif interval > 0:
                    units.append(("text", tok))
                else:
                    buf.append(tok)
            if buf:
                units.append(("text", "".join(buf)))
            return units
        if mode == "keys":
            return [("key", t) if t in ("enter", "tab") else ("char", t) for t in split_text_tokens(text)]
        # fallback (auto without unicode): keys for ASCII runs, clipboard for the rest
        can_paste = self.backend.has(CAP_CLIPBOARD)
        pending: List[str] = []
        for tok in split_text_tokens(text):
            if _is_key_typable(tok) or not can_paste:
                if pending:
                    units.append(("paste", "".join(pending)))
                    pending = []
                units.append(("key", tok) if tok in ("enter", "tab") else ("char", tok))
            else:
                pending.append(tok)
        if pending:
            units.append(("paste", "".join(pending)))
        return units

    def _run_units(self, units: Sequence[_Unit], interval: float, *, first: bool) -> None:
        saved: Optional[str] = None
        saved_ok = False
        pasted = False
        try:
            for i, (kind, value) in enumerate(units):
                if (i or not first) and interval > 0:
                    self._sleep(interval)
                self._check()
                if kind == "text":
                    self.backend.type_text(value)
                elif kind == "char":
                    self._tap(value)
                elif kind == "key":
                    self._tap(value)
                elif kind == "paste":
                    if not pasted:
                        try:
                            saved, saved_ok = self.backend.get_clipboard(), True
                        except NotSupported:
                            saved_ok = False
                    self.backend.set_clipboard(value)
                    pasted = True
                    with self._held(parse_hotkey("mod+v")):
                        pass
                else:  # pragma: no cover - internal invariant
                    raise AssertionError(kind)
        finally:
            if pasted and saved_ok:
                if self.clipboard_settle > 0:
                    self._sleep(self.clipboard_settle)
                self.backend.set_clipboard(saved if saved is not None else "")

    def __repr__(self) -> str:
        return f"<Keyboard backend={getattr(self.backend, 'name', '?')} pressed={self._pressed}>"


__all__ = ["Keyboard", "TYPING_MODES"]
