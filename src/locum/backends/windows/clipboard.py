"""Clipboard support for the Windows backend: clipboard format constants,
:class:`ClipboardSnapshot` and the :class:`_ClipboardMixin`.
"""

from __future__ import annotations

import ctypes
from typing import Any, List, Tuple

from ...errors import BackendError
from ._win32api import CF_UNICODETEXT, GMEM_MOVEABLE, HWND_MESSAGE


#: Registered clipboard formats that keep text out of Windows Clipboard History
#: (Win+V), cloud clipboard sync and clipboard monitors ("Clipboard Formats",
#: Microsoft docs). Each is set alongside the text with a DWORD 0 payload.
CLIPBOARD_EXCLUSION_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
#: Clipboard formats whose data is a GDI/metafile handle or owner-drawn, not a global
#: memory block: :meth:`WindowsBackend.save_clipboard` cannot copy them (CF_BITMAP is
#: re-synthesized by Windows from the saved CF_DIB). 0x200-0x3FF: private/GDI objects.
CLIPBOARD_HANDLE_FORMATS = frozenset({2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E})
#: Formats larger than this are not kept by a clipboard snapshot.
CLIPBOARD_SNAPSHOT_MAX = 64 * 1024 * 1024



class ClipboardSnapshot:
    """Every copyable format of the clipboard, as ``[(format id, bytes)]`` (see :meth:`WindowsBackend.save_clipboard`)."""

    __slots__ = ("items",)

    def __init__(self, items: List[Tuple[int, bytes]]):
        self.items = list(items)

    def __repr__(self) -> str:
        return f"<ClipboardSnapshot formats={[f for f, _ in self.items]}>"


class _ClipboardMixin:
    """Clipboard access of :class:`WindowsBackend`."""

    # ================================================================ clipboard
    def _open_clipboard(self, owner: Any = None) -> None:
        a = self._api
        for attempt in range(self._clip_retries):
            if a.OpenClipboard(owner):
                return
            if attempt + 1 < self._clip_retries:
                self._sleep(self._clip_delay)
        raise self._error("OpenClipboard (held by another process)")

    def get_clipboard(self) -> str:
        """Unicode text on the clipboard ('' when it holds no text)."""
        self._check()
        a = self._api
        with self._lock:
            self._open_clipboard(None)
            try:
                if not a.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                h = a.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                ptr = a.GlobalLock(h)
                if not ptr:
                    raise self._error("GlobalLock")
                try:
                    size = int(a.GlobalSize(h))
                    raw = ctypes.string_at(ptr, size - (size % 2)) if size > 0 else b""
                finally:
                    a.GlobalUnlock(h)
            finally:
                a.CloseClipboard()
        text = raw.decode("utf-16-le", "replace")
        nul = text.find("\x00")
        return text if nul < 0 else text[:nul]

    def save_clipboard(self) -> Any:
        """Snapshot every format on the clipboard (text, an image, a file list, an Excel range...).

        Clipboard-mode typing restores the user's clipboard from this with
        :meth:`restore_clipboard`; saving only its text would replace a copied
        image or Excel range with plain or empty text. Formats that are not
        global memory blocks (:data:`CLIPBOARD_HANDLE_FORMATS`) or larger than
        :data:`CLIPBOARD_SNAPSHOT_MAX` are skipped.
        """
        enum = getattr(self._api, "EnumClipboardFormats", None)
        if enum is None:
            return self.get_clipboard()
        self._check()
        a = self._api
        items: List[Tuple[int, bytes]] = []
        with self._lock:
            self._open_clipboard(None)
            try:
                fmt = 0
                while True:
                    fmt = int(enum(fmt) or 0)
                    if not fmt:
                        break
                    if fmt in CLIPBOARD_HANDLE_FORMATS or 0x200 <= fmt <= 0x3FF:
                        continue
                    h = a.GetClipboardData(fmt)
                    if not h:
                        continue
                    size = int(a.GlobalSize(h) or 0)
                    if size > CLIPBOARD_SNAPSHOT_MAX:
                        continue
                    ptr = a.GlobalLock(h)
                    if not ptr:
                        continue
                    try:
                        items.append((fmt, ctypes.string_at(ptr, size) if size > 0 else b""))
                    finally:
                        a.GlobalUnlock(h)
            finally:
                a.CloseClipboard()
        return ClipboardSnapshot(items)

    def restore_clipboard(self, saved: Any) -> None:
        """Put back a :meth:`save_clipboard` snapshot (an empty one empties the clipboard)."""
        if not isinstance(saved, ClipboardSnapshot):
            self.set_clipboard(saved if saved is not None else "")
            return
        self._check()
        a = self._api
        with self._lock:
            owner = a.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
            try:
                self._open_clipboard(owner)
                try:
                    if not a.EmptyClipboard():
                        raise self._error("EmptyClipboard")
                    for fmt, data in saved.items:
                        try:
                            hmem = self._global_bytes(data or b"\x00")
                        except BackendError:
                            continue
                        try:
                            ok = a.SetClipboardData(fmt, hmem)
                        except Exception:
                            ok = None
                        if not ok:
                            a.GlobalFree(hmem)
                finally:
                    a.CloseClipboard()
            finally:
                if owner:
                    a.DestroyWindow(owner)

    def _global_bytes(self, data: bytes) -> Any:
        """A GMEM_MOVEABLE global memory block holding ``data`` (caller frees it unless the clipboard owns it)."""
        a = self._api
        hmem = a.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hmem:
            raise self._error("GlobalAlloc")
        try:
            ptr = a.GlobalLock(hmem)
            if not ptr:
                raise self._error("GlobalLock")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                a.GlobalUnlock(hmem)
        except BaseException:
            a.GlobalFree(hmem)
            raise
        return hmem

    def _clipboard_exclusion_formats(self) -> List[int]:
        """Registered ids of :data:`CLIPBOARD_EXCLUSION_FORMATS` (empty when unavailable)."""
        if self._clip_exclusions is None:
            ids: List[int] = []
            register = getattr(self._api, "RegisterClipboardFormatW", None)
            if register is not None:
                for fmt in CLIPBOARD_EXCLUSION_FORMATS:
                    try:
                        fid = int(register(fmt) or 0)
                    except Exception:
                        fid = 0
                    if fid:
                        ids.append(fid)
            self._clip_exclusions = ids
        return self._clip_exclusions

    def set_clipboard(self, text: str) -> None:
        """Replace the clipboard with ``text`` (``CF_UNICODETEXT``).

        Unless the backend was created with ``clipboard_history=True``, the text
        is marked as excluded from Windows Clipboard History (Win+V), cloud
        clipboard sync and clipboard monitors, so passwords or bank data typed
        through clipboard paste do not linger there.
        """
        self._check()
        a = self._api
        data = (str(text) + "\x00").encode("utf-16-le", "surrogatepass")
        with self._lock:
            exclusions = [] if self._clip_history else self._clipboard_exclusion_formats()
            hmem = self._global_bytes(data)
            owned_by_system = False
            try:
                # A (message-only) owner window: EmptyClipboard with a NULL owner can
                # make SetClipboardData fail on some systems.
                owner = a.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
                try:
                    self._open_clipboard(owner)
                    try:
                        if not a.EmptyClipboard():
                            raise self._error("EmptyClipboard")
                        if not a.SetClipboardData(CF_UNICODETEXT, hmem):
                            raise self._error("SetClipboardData")
                        owned_by_system = True
                        for fmt in exclusions:
                            self._set_dword_format(fmt, 0)
                    finally:
                        a.CloseClipboard()
                finally:
                    if owner:
                        a.DestroyWindow(owner)
            finally:
                if not owned_by_system:
                    a.GlobalFree(hmem)

    def _set_dword_format(self, fmt: int, value: int) -> None:
        """Best effort: put a DWORD under clipboard format ``fmt`` (clipboard already open)."""
        try:
            h = self._global_bytes(int(value).to_bytes(4, "little"))
        except BackendError:
            return
        try:
            ok = self._api.SetClipboardData(fmt, h)
        except Exception:
            ok = None
        if not ok:
            self._api.GlobalFree(h)
