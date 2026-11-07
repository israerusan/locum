"""High-level keyboard control: key taps, hotkeys, held keys and text entry.

:class:`Keyboard` wraps the backend's instant ``key_down``/``key_up``/``type_text``
primitives and adds:

* alias-tolerant key names (``"Ctrl"``, ``"Return"``, ``"⌘"``, ``"mod"`` ...);
* hotkeys that release keys in reverse order **in a finally block**, so a
  failure halfway through never leaves a modifier stuck down;
* four text-entry strategies -- ``unicode`` (layout independent injection),
  ``keys`` (per-character key taps; the backend adds Shift/AltGr as the
  active layout needs), ``clipboard``
  (paste, with the user's clipboard saved and restored) and ``auto``;
* bookkeeping of every key this instance holds, so :meth:`Keyboard.release_all`
  can always clean up.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple, Union

from .backends.base import CAP_CLIPBOARD, CAP_UNICODE_TYPING
from . import _inputlock
from ._cancel import check as _cancel_check
from ._target import check_input as _target_check
from .errors import NotSupported
from .failsafe import FailSafe
from .keys import check_combo, lower_combo_letters, normalize_key, parse_hotkey, press_actions, split_text_tokens

KeySpec = Union[str, Sequence[str]]

TYPING_MODES = ("auto", "unicode", "keys", "clipboard")

#: Default seconds to wait after a paste before the clipboard is restored, on
#: backends that cannot tell when the target application has read it.
DEFAULT_CLIPBOARD_SETTLE = 0.5
#: Default seconds to wait for the target application to read a paste, on
#: backends that can tell (X11 with the native clipboard).
DEFAULT_CLIPBOARD_TIMEOUT = 2.0

# A typing "unit": ("text", s) inject via type_text, ("char", c) key tap with
# shift handling, ("key", name) canonical key tap, ("paste", s) clipboard paste.
_Unit = Tuple[str, str]

#: Longest run of text injected with one ``type_text`` call in unicode mode: the
#: target window and the fail-safe/abort are checked again before every chunk.
_UNICODE_CHUNK = 32


def _is_key_typable(ch: str) -> bool:
    """Characters that are typed with key taps by ``auto`` mode on backends without unicode typing."""
    return ch in ("enter", "tab") or (len(ch) == 1 and 32 <= ord(ch) < 127)


class Keyboard:
    """Keyboard controller bound to a backend.

    Args:
        backend: The platform backend.
        failsafe: Optional :class:`FailSafe` checked before presses and between typed characters.
        pause: Seconds to sleep after every public action (PyAutoGUI's ``PAUSE``).
        sleep: Sleep function (inject ``lambda s: None`` in tests).
        clipboard_settle: Seconds to wait after a paste before the next paste or
            restoring the clipboard, when the backend cannot tell whether the
            target application has read it (Windows, macOS, X11 helper tools).
            Applications read the clipboard asynchronously: a busy app that reads
            it after the restore would paste the user's *previous* clipboard.
        clipboard_timeout: On backends that can tell (X11 native clipboard):
            seconds to wait for the target application to read the paste. If it
            has not read it by then the clipboard is **not** restored, so a late
            paste still inserts the typed text.
    """

    def __init__(
        self,
        backend: Any,
        failsafe: Optional[FailSafe] = None,
        pause: float = 0.0,
        sleep: Callable[[float], Any] = time.sleep,
        *,
        clipboard_settle: float = DEFAULT_CLIPBOARD_SETTLE,
        clipboard_timeout: float = DEFAULT_CLIPBOARD_TIMEOUT,
    ):
        self.backend = backend
        self.failsafe = failsafe
        self.pause = float(pause)
        self.clipboard_settle = float(clipboard_settle)
        self.clipboard_timeout = float(clipboard_timeout)
        self._sleep = sleep
        self._pressed: List[str] = []  # keys held by this instance, in press order

    # ------------------------------------------------------------------ helpers
    def _check(self) -> None:
        # The running call's cancel scope first: a call the MCP server already
        # reported as timed out / stopped must not send further input.
        _cancel_check()
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
        # Record the key *before* the backend call: a KeyboardInterrupt that lands
        # inside key_down after the press reached the OS (X11 spends most of the call
        # waiting for the server) must still leave release_all() knowing about it.
        # Every key press goes through here (taps, hotkeys, typed characters, paste):
        # a cancelled / timed-out call stops before the press. Releases never check.
        _cancel_check()
        _target_check("key", key=key)
        added = key not in self._pressed
        if added:
            self._pressed.append(key)
        try:
            self.backend.key_down(key)
        except Exception:
            if added:  # an ordinary error: the press was not made
                self._pressed.remove(key)
            raise

    def _raw_up(self, key: str) -> None:
        # Forget the key only once the backend released it: a key whose release
        # failed is still down, so release_all() must retry it later.
        self.backend.key_up(key)
        if key in self._pressed:
            self._pressed.remove(key)

    def _release_many(self, keys: Sequence[str]) -> Optional[BaseException]:
        """Release ``keys`` in the given order; keep going on errors and return the first one."""
        first: Optional[BaseException] = None
        for k in keys:
            try:
                self._raw_up(k)
            except Exception as exc:
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
        return check_combo(lower_combo_letters(out), keys[0] if len(keys) == 1 else list(keys))

    def _tap(self, key: str) -> None:
        """Tap a canonical key or a character.

        Characters are passed to the backend as-is: backends map them onto the
        active keyboard layout and add Shift/AltGr themselves (``"@"`` is
        Shift+2 on a US layout but AltGr+Q on a German one), honouring CapsLock.
        """
        was_held = key in self._pressed
        try:
            self._raw_down(key)
        except BaseException:
            if not was_held and key in self._pressed:  # interrupted after the press may have been sent
                self._release_many([key])
            raise
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
                try:
                    self._raw_down(k)
                finally:
                    if k in self._pressed:  # also when interrupted inside the backend call
                        pressed.append(k)
            yield list(keys)
        finally:
            err = self._release_many(list(reversed(pressed)))
        if err is not None:
            raise err

    # --------------------------------------------------------------- public API
    @_inputlock.locked
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

    @_inputlock.locked
    def press(self, *keys: KeySpec, presses: int = 1, interval: float = 0.0) -> None:
        """Tap each key in sequence, ``presses`` times: ``press("tab", "tab", "enter")``.

        An argument that is a combo (``"ctrl+c"``) or a list (``["ctrl", "c"]``)
        is performed as a hotkey. A string of several keys without modifiers
        (``"tab tab enter"``) is tapped in sequence; ambiguous strings
        (``"shift tab tab"``) and combos naming a key twice raise ``ValueError``.
        Uppercase letters and shifted symbols get shift automatically.
        ``interval`` seconds are waited between taps.
        """
        actions: List[Union[str, List[str]]] = []
        for spec in keys:
            for parsed in press_actions(spec if isinstance(spec, str) else list(spec)):
                actions.append(parsed[0] if len(parsed) == 1 else parsed)
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

    @_inputlock.locked
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
        with _inputlock.hold(self), self._held(parsed) as held:  # the whole chord is one input action
            yield held
        self._pause()

    def release_all(self) -> List[str]:
        """Release every key this instance holds (reverse press order). Returns the released keys.

        Every key is attempted; if a release fails the key stays recorded (so a
        later call retries it) and the first error is raised afterwards.
        """
        keys = list(reversed(self._pressed))
        err = self._release_many(keys)
        if err is not None:
            raise err
        return keys

    # --------------------------------------------------------------------- typing
    @_inputlock.locked
    def type(self, text: str, *, interval: float = 0.0, mode: str = "auto") -> None:
        """Type ``text``.

        Args:
            text: The text; ``\\n`` presses Enter and ``\\t`` presses Tab (except in clipboard mode,
                where text is pasted verbatim).
            interval: Seconds between characters.
            mode: ``"unicode"`` (backend unicode injection), ``"keys"`` (per-character key taps;
                the backend adds Shift/AltGr as the active layout needs), ``"clipboard"`` (paste via ``mod+v``,
                restoring the previous clipboard), or ``"auto"`` (unicode when the backend supports
                it; otherwise key taps for ASCII and clipboard paste for other characters).

        ``text`` may be a :class:`~locum.rpa.credentials.Secret` (revealed here).
        """
        from ._secret import reveal

        text = reveal(text)
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
            # Probe with the first text unit; leading Enter/Tab taps go first and are
            # the same in both modes, so a fallback must not repeat them.
            k = next((i for i, u in enumerate(units) if u[0] == "text"), len(units))
            if k:
                self._run_units(units[:k], interval, first=True)
            try:
                self._run_units(units[k : k + 1], interval, first=not k)
            except NotSupported:
                # Backend advertised unicode typing but cannot do it right now: fall back.
                units = self._units(text, "fallback", interval)[k:]
                self._run_units(units, interval, first=not k)
            else:
                self._run_units(units[k + 1 :], interval, first=False)
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
                    if len(buf) >= _UNICODE_CHUNK:
                        units.append(("text", "".join(buf)))
                        buf = []
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

    def _await_paste(self, since: float) -> bool:
        """Wait until the target app has (presumably) read the paste. False: it has not read it yet."""
        wait = getattr(self.backend, "wait_clipboard_read", None)  # optional for duck-typed backends
        try:
            read = wait(since, self.clipboard_timeout) if wait is not None else None
        except NotSupported:
            read = None
        if read is None:  # the backend cannot tell: give the app a fixed time
            if self.clipboard_settle > 0:
                self._sleep(self.clipboard_settle)
            return True
        return bool(read)

    def _save_clipboard(self) -> Any:
        """Snapshot the user's clipboard (every format where the backend can, else its text)."""
        save = getattr(self.backend, "save_clipboard", None)
        return save() if callable(save) else self.backend.get_clipboard()

    def _restore_clipboard(self, saved: Any) -> None:
        restore = getattr(self.backend, "restore_clipboard", None)
        if callable(restore):
            restore(saved)
        else:
            self.backend.set_clipboard(saved if saved is not None else "")

    def _run_units(self, units: Sequence[_Unit], interval: float, *, first: bool) -> None:
        saved: Any = None
        saved_ok = False
        pasted = False
        settled = True  # the last paste was waited for
        restore = True  # False: the app has not read the last paste, keep it on the clipboard
        try:
            for i, (kind, value) in enumerate(units):
                if (i or not first) and interval > 0:
                    self._sleep(interval)
                self._check()
                if kind == "text":
                    _target_check("text")
                    self.backend.type_text(value)
                elif kind == "char":
                    self._tap(value)
                elif kind == "key":
                    self._tap(value)
                elif kind == "paste":
                    if not pasted:
                        try:
                            saved, saved_ok = self._save_clipboard(), True
                        except NotSupported:
                            saved_ok = False
                    self.backend.set_clipboard(value)
                    pasted, settled, restore = True, False, True
                    since = time.monotonic()
                    with self._held(parse_hotkey("mod+v")):
                        pass
                    # Before the next paste or the restore replaces it, the app must
                    # have read this text, or it would paste other data.
                    restore = self._await_paste(since)
                    settled = True
                else:  # pragma: no cover - internal invariant
                    raise AssertionError(kind)
        finally:
            if pasted and saved_ok and restore:
                if not settled and self.clipboard_settle > 0:  # failed mid-paste: mod+v may still be read
                    self._sleep(self.clipboard_settle)
                self._restore_clipboard(saved)

    def __repr__(self) -> str:
        return f"<Keyboard backend={getattr(self.backend, 'name', '?')} pressed={self._pressed}>"


__all__ = ["DEFAULT_CLIPBOARD_SETTLE", "DEFAULT_CLIPBOARD_TIMEOUT", "Keyboard", "TYPING_MODES"]
