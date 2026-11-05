"""Input (XTEST) part of the X11 backend.

The cleanup-safe :class:`_InputState`, its finalizer, the scratch-keycode
bookkeeping shared between processes and the mouse/keyboard mixin of
:class:`X11Backend`.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ...errors import BackendError, NotSupported
from ...keys import split_text_tokens
from ...types import MouseButton, Point
from ._xcore import _abandon_display, _close_quietly, _x_errors, _xlib
from .keysyms import (
    BUTTONS,
    KEYSYMS,
    _COMMAND_MODIFIERS,
    _CONTROL_CHAR_KEYS,
    _KEYSYM_CAPS_LOCK,
    _KEYSYM_NUM_LOCK,
    _KEYSYM_SHIFT_L,
    _WHEEL_DOWN,
    _WHEEL_LEFT,
    _WHEEL_RIGHT,
    _WHEEL_UP,
    _is_char_keysym,
    _is_keypad_keysym,
    _lower_upper,
    char_to_keysym,
    key_to_keysym,
)


# --------------------------------------------------------------------------- key state
class _InputState:
    """State that must be cleaned up even if the backend is garbage collected.

    Held separately from :class:`X11Backend` so a :func:`weakref.finalize`
    callback can restore the keyboard mapping, release stuck keys and close the
    helper connections (clipboard owner, screenshot grabbers) without keeping
    the backend alive.
    """

    def __init__(self, display: Any, lock: threading.RLock, remap_settle: float, display_name: str = "") -> None:
        self.display = display
        self.display_name = display_name
        self.lock = lock
        self.remap_settle = remap_settle
        self.bound: Dict[int, int] = {}  # keysym -> scratch keycode
        self.keycode_sym: Dict[int, int] = {}  # scratch keycode -> keysym
        self.original: Dict[int, Tuple[int, ...]] = {}  # scratch keycode -> original keysyms
        self.last_used: Dict[int, float] = {}  # scratch keycode -> monotonic time of last use
        self.held_keycodes: Dict[int, int] = {}  # keycode -> press count
        self.held_buttons: set = set()
        self.clipboard: Any = None  # native X11Clipboard (own connection + serving thread)
        self.mss_closers: List[Any] = []  # weakref.finalize objects closing per-thread mss grabbers
        self.closed = False
        # A KeyboardInterrupt (or other BaseException) landed inside an X call: python-xlib
        # may have been waiting for a reply and its connection state is now unusable (every
        # later request would block forever). The connection is dropped and reopened on demand.
        self.desynced = False


def _cleanup(state: _InputState) -> None:
    """Release keys/buttons we hold, restore scratch keycodes and close every X connection."""
    if state.closed:
        return
    state.closed = True
    for closer in list(state.mss_closers):
        try:
            closer()
        except Exception:
            pass
    state.mss_closers.clear()
    with state.lock:
        clipboard, state.clipboard = state.clipboard, None
    if clipboard is not None:
        _close_quietly(clipboard)
    try:
        X = _xlib().X
        xtest = _xlib().xtest
        with state.lock:
            d = state.display
            if state.desynced:
                # The old connection cannot be used any more (see _InputState.desynced): release
                # the keys and restore the keymap over a fresh one (that is server-side state).
                _abandon_display(d)
                try:
                    d = _xlib().display.Display(state.display_name)
                except Exception:
                    return
                state.display = d
                state.desynced = False
            try:
                for kc in list(state.held_keycodes):
                    xtest.fake_input(d, X.KeyRelease, kc)
                for btn in list(state.held_buttons):
                    xtest.fake_input(d, X.ButtonRelease, btn)
                state.held_keycodes.clear()
                state.held_buttons.clear()
                if state.original:
                    # Give clients time to look up the keysyms of the last keys
                    # we typed before the scratch keycodes disappear.
                    newest = max(state.last_used.values(), default=0.0)
                    wait = state.remap_settle - (time.monotonic() - newest)
                    if wait > 0:
                        time.sleep(min(wait, 1.0))
                    # Restore only keycodes that still hold our keysym: another
                    # client (e.g. a second backend) may have bound the keycode
                    # since, and its binding must not be clobbered.
                    d.grab_server()
                    try:
                        restored = set()
                        for kc, syms in state.original.items():
                            ours = state.keycode_sym.get(kc)
                            current = tuple(int(v) for v in d.get_keyboard_mapping(kc, 1)[0])
                            if ours is not None and current[:1] == (ours,):
                                d.change_keyboard_mapping(kc, [tuple(syms)])
                                restored.add(kc)
                        marks = _scratch_marks(d)
                        if marks & restored:
                            _set_scratch_marks(d, marks - restored)
                    finally:
                        d.ungrab_server()
                    state.original.clear()
                    state.bound.clear()
                    state.keycode_sym.clear()
                d.sync()
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass
    except Exception:
        pass


#: Root-window property listing the spare keycodes locum backends (in any
#: process) have bound for unicode typing, so they leave each other's alone.
_SCRATCH_PROP = "_LOCUM_SCRATCH_KEYCODES"


def _scratch_marks(d: Any) -> frozenset:
    """Keycodes listed in :data:`_SCRATCH_PROP` (empty when unset or unreadable)."""
    xl = _xlib()
    try:
        prop = d.screen().root.get_full_property(d.intern_atom(_SCRATCH_PROP), xl.Xatom.CARDINAL)
    except xl.error.XError:
        return frozenset()
    if prop is None or prop.format != 32:
        return frozenset()
    return frozenset(int(v) for v in prop.value)


def _set_scratch_marks(d: Any, keycodes: Any) -> None:
    xl = _xlib()
    root, atom = d.screen().root, d.intern_atom(_SCRATCH_PROP)
    if keycodes:
        root.change_property(atom, xl.Xatom.CARDINAL, 32, sorted(int(k) for k in keycodes))
    else:
        root.delete_property(atom)


class _InputMixin:
    """Mouse and keyboard primitives of :class:`X11Backend` (XTEST injection, keymap handling)."""

    def _need_xtest(self) -> None:
        self._check()
        if not self._xtest:
            raise NotSupported(f"X display {self.display_name!r} lacks the XTEST extension; input injection unavailable")

    def _fake(self, event_type: int, detail: int = 0, x: int = 0, y: int = 0) -> None:
        if event_type == self._xl.X.MotionNotify:
            self._xl.xtest.fake_input(self._d, event_type, detail, root=self._root, x=x, y=y)
        else:
            self._xl.xtest.fake_input(self._d, event_type, detail)

    # ==================================================================== mouse
    @_x_errors
    def mouse_position(self) -> Point:
        self._check()
        with self._lock:
            p = self._root.query_pointer()
            return Point(int(p.root_x), int(p.root_y))

    @_x_errors
    def mouse_move(self, x: int, y: int) -> None:
        self._check()
        with self._lock:
            w, h = self._screen_size()
            cx = min(max(int(round(x)), 0), max(w - 1, 0))
            cy = min(max(int(round(y)), 0), max(h - 1, 0))
            if self._xtest:
                self._fake(self._xl.X.MotionNotify, 0, cx, cy)
            else:
                self._root.warp_pointer(cx, cy)
            self._sync()

    def _button(self, button: Any) -> int:
        return BUTTONS[MouseButton.coerce(button)]

    @_x_errors
    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._need_xtest()
        btn = self._button(button)
        with self._lock:
            self._fake(self._xl.X.ButtonPress, btn)
            self._state.held_buttons.add(btn)
            self._sync()

    @_x_errors
    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> None:
        self._need_xtest()
        btn = self._button(button)
        with self._lock:
            self._fake(self._xl.X.ButtonRelease, btn)
            self._state.held_buttons.discard(btn)
            self._sync()

    @_x_errors
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by wheel notches (``dy > 0`` up, ``dx > 0`` right) using buttons 4-7."""
        self._need_xtest()
        dx, dy = int(dx), int(dy)
        X = self._xl.X
        with self._lock:
            for count, btn in ((abs(dy), _WHEEL_UP if dy > 0 else _WHEEL_DOWN), (abs(dx), _WHEEL_RIGHT if dx > 0 else _WHEEL_LEFT)):
                for _ in range(count):
                    self._fake(X.ButtonPress, btn)
                    self._fake(X.ButtonRelease, btn)
            self._sync()

    # ================================================================= keyboard
    def _process_events(self) -> None:
        """Drain our event queue, noting keyboard/modifier mapping changes."""
        X = self._xl.X
        try:
            while self._d.pending_events():
                ev = self._d.next_event()
                if ev.type == X.MappingNotify:
                    if ev.request == X.MappingKeyboard:
                        self._keymap_dirty = True
                    elif ev.request == X.MappingModifier:
                        self._modmap_dirty = True
        except self._xl.error.ConnectionClosedError as exc:
            raise BackendError(f"Lost connection to X display {self.display_name!r}: {exc}") from exc

    def _ensure_keymap(self) -> None:
        self._process_events()
        if self._keymap_dirty or not self._keymap:
            count = self._max_kc - self._min_kc + 1
            self._keymap = [tuple(int(s) for s in syms) for syms in self._d.get_keyboard_mapping(self._min_kc, count)]
            self._keymap_dirty = False
            # Spare keycodes other locum backends have bound are not real keys:
            # they may be re-bound at any moment, so never type through them.
            self._foreign_scratch = _scratch_marks(self._d) - set(self._state.keycode_sym)
        if self._modmap_dirty:
            self._numlock_mask = 0
            try:
                for idx, keycodes in enumerate(self._d.get_modifier_mapping()):
                    for kc in keycodes:
                        if kc and self._syms(int(kc))[:1] == (_KEYSYM_NUM_LOCK,):
                            self._numlock_mask |= 1 << idx
            except Exception:
                pass
            self._modmap_dirty = False

    def _syms(self, keycode: int) -> Tuple[int, ...]:
        i = keycode - self._min_kc
        if 0 <= i < len(self._keymap):
            return self._keymap[i]
        return ()

    def _group_syms(self, keycode: int, group: int = 0) -> Tuple[int, int]:
        """(plain, shifted) keysyms of ``keycode`` in XKB group ``group`` (0 or 1).

        The core keymap of an XKB server lists group 1 in columns 0-1 and group 2
        in columns 2-3 (single-group keys repeat group 1 there).
        """
        syms = self._syms(keycode)
        c = 2 * group
        return (syms[c] if len(syms) > c else 0), (syms[c + 1] if len(syms) > c + 1 else 0)

    def _lookup(self, keysym: int, group: int = 0) -> Optional[Tuple[int, int]]:
        """Find ``(keycode, level)`` producing ``keysym`` with level 0 (plain) or 1 (shift)
        while XKB group ``group`` is active. Groups 3 and 4 cannot be read reliably
        from the core keymap, so nothing is found for them."""
        if group not in (0, 1):
            return None
        c = 2 * group
        best: Optional[Tuple[int, int]] = None
        foreign = self._foreign_scratch
        for i, syms in enumerate(self._keymap):
            kc = i + self._min_kc
            if len(syms) <= c or kc in foreign:
                continue
            if syms[c] == keysym:
                # A keycode whose second column is NoSymbol also yields keysym
                # with shift; plain is always preferred.
                return kc, 0
            if len(syms) > c + 1 and syms[c + 1] == keysym and best is None:
                best = (kc, 1)
        if best is None:
            lo, up = _lower_upper(keysym)
            if up == keysym and lo != keysym:
                # Uppercase letter on a key listing only the lowercase keysym.
                for i, syms in enumerate(self._keymap):
                    if len(syms) > c and syms[c] == lo and (len(syms) <= c + 1 or syms[c + 1] == 0):
                        if i + self._min_kc not in foreign:
                            return i + self._min_kc, 1
        return best

    def _shift_keycode(self) -> int:
        hit = self._lookup(_KEYSYM_SHIFT_L)
        if hit is not None:
            return hit[0]
        return self._scratch_bind(_KEYSYM_SHIFT_L)

    def _pool(self) -> List[int]:
        if self._scratch_pool is None:
            self._ensure_keymap()
            empty = [i + self._min_kc for i, syms in enumerate(self._keymap) if not any(syms)]
            # Keycode 8 maps to evdev KEY_RESERVED and is ignored by some
            # toolkits; use high keycodes first.
            pool = sorted((kc for kc in empty if kc > 8), reverse=True)
            self._scratch_borrow = not pool  # no spare keycode at all: borrow a real one
            if not pool:
                pool = [self._max_kc]
            if self._max_scratch is not None:
                pool = pool[: max(1, int(self._max_scratch))]
            self._scratch_pool = pool
        return self._scratch_pool

    def _scratch_bind(self, keysym: int) -> int:
        """Bind ``keysym`` to a spare keycode (re-using an existing binding) and return it.

        Other clients -- including other :class:`X11Backend` instances, in this
        process or another one -- may bind spare keycodes too. A keycode is only
        taken when the server still shows it empty (or bound by us), checked and
        changed under a server grab so two backends never share one.
        """
        st = self._state
        kc = st.bound.get(keysym)
        if kc is not None and self._syms(kc)[:1] == (keysym,):
            st.last_used[kc] = time.monotonic()
            return kc
        pool = self._pool()
        held = set(st.held_keycodes)
        free = [
            k for k in pool
            if k not in st.keycode_sym and k not in held and (self._scratch_borrow or not any(self._syms(k)))
        ]
        ours = sorted((k for k in pool if k in st.keycode_sym and k not in held), key=lambda k: st.last_used.get(k, 0.0))
        if not free and not ours:
            raise BackendError("No spare keycode available to type this character (all are in use or held down)")
        if not free:
            wait = st.remap_settle - (time.monotonic() - st.last_used.get(ours[0], 0.0))
            if wait > 0:
                time.sleep(wait)
        d = self._d
        chosen: Optional[int] = None
        current: Tuple[int, ...] = ()
        d.grab_server()
        try:
            for cand in free + ours:
                current = tuple(int(v) for v in d.get_keyboard_mapping(cand, 1)[0])
                mine = st.keycode_sym.get(cand)
                if mine is not None:
                    if current[:1] == (mine,):
                        chosen = cand
                        break
                    # Someone else re-bound our keycode: it is theirs now.
                    st.keycode_sym.pop(cand, None)
                    st.bound.pop(mine, None)
                    st.original.pop(cand, None)
                elif self._scratch_borrow or not any(current):
                    chosen = cand
                    break
                self._set_cached_syms(cand, current)  # bound by another client
            if chosen is None:
                raise BackendError("No spare keycode available to type this character (all are in use by other clients)")
            if chosen not in st.original:
                st.original[chosen] = current or (0,)
            old = st.keycode_sym.pop(chosen, None)
            if old is not None:
                st.bound.pop(old, None)
            marks = _scratch_marks(d)
            if chosen not in marks:
                _set_scratch_marks(d, marks | {chosen})
            d.change_keyboard_mapping(chosen, [(keysym, keysym)])
        finally:
            d.ungrab_server()
        self._sync()
        st.bound[keysym] = chosen
        st.keycode_sym[chosen] = keysym
        st.last_used[chosen] = time.monotonic()
        # Keep our cache coherent without waiting for MappingNotify.
        self._set_cached_syms(chosen, (keysym, keysym))
        return chosen

    def _set_cached_syms(self, keycode: int, syms: Tuple[int, ...]) -> None:
        i = keycode - self._min_kc
        if 0 <= i < len(self._keymap):
            width = max(len(self._keymap[i]), len(syms))
            self._keymap[i] = tuple(syms) + (0,) * (width - len(syms))

    def _pointer_mask(self) -> int:
        try:
            return int(self._root.query_pointer().mask)
        except Exception:
            return 0

    @staticmethod
    def _group(mask: int) -> int:
        """Active XKB group (0 = first layout) from a core event/pointer state mask."""
        return (mask >> 13) & 3

    def _resolve(self, keysym: int, *, text: bool, mask: int = 0, group: int = 0) -> Tuple[int, bool]:
        """Return ``(keycode, needs_shift)`` for ``keysym``, binding a scratch keycode if needed.

        Character keysyms are looked up in the active XKB ``group`` (the key that
        types "y" differs between layouts); other keys (modifiers, Return, F-keys)
        are the same in every group.
        """
        if not _is_char_keysym(keysym):
            group = 0
        hit = self._lookup(keysym, group)
        if hit is None:
            return self._scratch_bind(keysym), False
        kc, level = hit
        need_shift = level == 1
        col0, col1 = self._group_syms(kc, group)
        if self._numlock_mask and mask & self._numlock_mask and _is_keypad_keysym(col1) and col0 != col1 and col0 and col1:
            need_shift = not need_shift  # NumLock selects the second keypad column
        elif text and mask & self._xl.X.LockMask:
            lo, up = _lower_upper(col0)
            if lo != up and (col1 in (0, up)):
                need_shift = not need_shift  # CapsLock flips letter case
        if kc in self._state.keycode_sym:
            self._state.last_used[kc] = time.monotonic()
        return kc, need_shift

    def _press(self, kc: int) -> None:
        self._fake(self._xl.X.KeyPress, kc)
        self._state.held_keycodes[kc] = self._state.held_keycodes.get(kc, 0) + 1

    def _release(self, kc: int) -> None:
        self._fake(self._xl.X.KeyRelease, kc)
        n = self._state.held_keycodes.get(kc, 0) - 1
        if n > 0:
            self._state.held_keycodes[kc] = n
        else:
            self._state.held_keycodes.pop(kc, None)
        if kc in self._state.keycode_sym:
            self._state.last_used[kc] = time.monotonic()

    @_x_errors
    def key_down(self, key: str) -> None:
        """Press a canonical key or single character (Shift is added for shifted characters)."""
        self._need_xtest()
        keysym = key_to_keysym(key)
        with self._lock:
            held = self._held_keys.get(key)
            if held is not None:
                # Already down: send another press (auto-repeat), keep the original record.
                self._press(held[0])
                self._sync()
                return
            self._ensure_keymap()
            # A character tapped without Ctrl/Alt/Super is text: honour CapsLock so
            # key_down("A") types "A" whatever the lock state. In shortcuts the
            # keysym's own level is used (ctrl+a must not gain a Shift).
            text = len(key) == 1 and not any(k in self._held_keys for k in _COMMAND_MODIFIERS)
            char = _is_char_keysym(keysym)
            mask = self._pointer_mask() if (text or char or self._numlock_mask) else 0
            kc, need_shift = self._resolve(keysym, text=text, mask=mask, group=self._group(mask))
            shift_kc: Optional[int] = None
            if need_shift and not any(k in self._held_keys for k in ("shift", "shift_r")):
                shift_kc = self._shift_keycode()
                self._press(shift_kc)
            self._press(kc)
            self._held_keys[key] = (kc, shift_kc)
            self._sync()

    @_x_errors
    def key_up(self, key: str) -> None:
        self._need_xtest()
        keysym = key_to_keysym(key)
        with self._lock:
            rec = self._held_keys.pop(key, None)
            if rec is None:
                self._ensure_keymap()
                mask = self._pointer_mask() if _is_char_keysym(keysym) else 0
                kc, _ = self._resolve(keysym, text=False, group=self._group(mask))
                shift_kc = None
            else:
                kc, shift_kc = rec
            self._unhold(kc)
            if shift_kc is not None:
                self._unhold(shift_kc)
            self._sync()

    def _unhold(self, kc: int) -> None:
        """Drop one hold of ``kc``; release the key unless another held key still needs it.

        XTEST does not count presses, so a single release lifts the key: the
        implicit Shift of ``key_down("A")`` must stay down while ``shift`` (or
        another shifted character) is still held, and a repeated ``key_down()``
        must never leave the key stuck.
        """
        if any(kc in (rk, sk) for rk, sk in self._held_keys.values()):
            n = self._state.held_keycodes.get(kc, 0) - 1
            if n > 0:
                self._state.held_keycodes[kc] = n
            else:
                self._state.held_keycodes.pop(kc, None)
            return
        self._release(kc)
        self._state.held_keycodes.pop(kc, None)

    @_x_errors
    def release_all(self) -> None:
        """Release every key and mouse button this backend currently holds down."""
        self._need_xtest()
        X = self._xl.X
        with self._lock:
            for kc, n in list(self._state.held_keycodes.items()):
                self._fake(X.KeyRelease, kc)
            self._state.held_keycodes.clear()
            self._held_keys.clear()
            for btn in list(self._state.held_buttons):
                self._fake(X.ButtonRelease, btn)
            self._state.held_buttons.clear()
            self._sync()

    def _tap(self, kc: int, shift_kc: Optional[int]) -> None:
        if shift_kc is not None:
            self._press(shift_kc)
        self._press(kc)
        self._release(kc)
        if shift_kc is not None:
            self._release(shift_kc)

    @_x_errors
    def type_text(self, text: str) -> None:
        """Type ``text`` exactly, independent of the keyboard layout.

        Characters available on the current layout (with at most Shift) use
        their real keys; everything else (accents on a US layout, emoji, CJK...)
        is typed through temporarily bound spare keycodes. ``\\n`` presses
        Return and ``\\t`` presses Tab. CapsLock and NumLock are compensated
        (for lowercase letters typed through a spare keycode CapsLock is briefly
        switched off and restored afterwards).
        """
        self._need_xtest()
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        if not text:
            return
        with self._lock:
            self._ensure_keymap()
            mask = self._pointer_mask()
            group = self._group(mask)  # XKB group; group != 0 means a non-primary layout is active
            shift_held = any(k in self._held_keys for k in ("shift", "shift_r"))
            shift_kc: Optional[int] = None
            caps_kc: Optional[int] = None  # set while CapsLock is temporarily switched off
            try:
                for tok in split_text_tokens(text):
                    if len(tok) > 1:  # "enter" / "tab"
                        keysym = KEYSYMS[tok]
                    elif tok in _CONTROL_CHAR_KEYS:
                        keysym = KEYSYMS[_CONTROL_CHAR_KEYS[tok]]
                    elif ord(tok) < 0x20 or 0x7F <= ord(tok) < 0xA0:
                        continue  # other C0/C1 control characters cannot be typed
                    else:
                        keysym = char_to_keysym(tok)
                    if caps_kc is None and self._caps_would_uppercase(keysym, mask, group):
                        caps_kc = self._caps_lock_keycode()
                        if caps_kc is not None:
                            self._tap(caps_kc, None)  # CapsLock off
                            self._sync()
                            mask &= ~self._xl.X.LockMask
                    kc, need_shift = self._resolve(keysym, text=True, mask=mask, group=group)
                    extra = None
                    if need_shift and not shift_held:
                        if shift_kc is None:
                            shift_kc = self._shift_keycode()
                        extra = shift_kc
                    self._tap(kc, extra)
                    # Sync per character so scratch-keycode timestamps are accurate
                    # and the server never queues an unbounded burst.
                    self._sync()
            finally:
                if caps_kc is not None:
                    self._tap(caps_kc, None)  # restore CapsLock
                    self._sync()

    def _caps_would_uppercase(self, keysym: int, mask: int, group: int) -> bool:
        """True when CapsLock is on and ``keysym`` (a lowercase letter) would be typed
        on a keycode that is not a ``(lower, upper)`` letter key -- typically a
        scratch keycode bound as ``(keysym, keysym)``. Clients apply CapsLock's
        case conversion to both levels of such keys, so Shift cannot compensate."""
        if not mask & self._xl.X.LockMask:
            return False
        lo, up = _lower_upper(keysym)
        if lo == up or keysym != lo:
            return False
        hit = self._lookup(keysym, group)
        if hit is None:
            return True  # will be bound to a scratch keycode
        kc, level = hit
        col1 = self._group_syms(kc, group)[1]
        return not (level == 0 and col1 in (0, up))

    def _caps_lock_keycode(self) -> Optional[int]:
        """Keycode of a Caps_Lock key that drives the Lock modifier, if the keymap has one."""
        hit = self._lookup(_KEYSYM_CAPS_LOCK)
        if hit is None or hit[1] != 0:
            return None
        try:
            lock_codes = {int(k) for k in self._d.get_modifier_mapping()[self._xl.X.LockMapIndex] if k}
        except Exception:
            return None
        return hit[0] if hit[0] in lock_codes else None
