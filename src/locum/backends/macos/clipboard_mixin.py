"""``MacOSBackend`` clipboard (NSPasteboard).
"""

from __future__ import annotations

from typing import Any

from ...errors import BackendError, NotSupported
from .constants import _INSTALL_HINT


class _ClipboardMixin:
    # ------------------------------------------------------------------- clipboard
    def _pasteboard(self) -> Any:
        if self._appkit is None:
            raise NotSupported(f"Clipboard needs pyobjc-framework-Cocoa; {_INSTALL_HINT}")
        return self._appkit.NSPasteboard.generalPasteboard()

    def _string_type(self) -> Any:
        return getattr(self._appkit, "NSPasteboardTypeString", "public.utf8-plain-text")

    def get_clipboard(self) -> str:
        value = self._pasteboard().stringForType_(self._string_type())
        return str(value) if value is not None else ""

    def set_clipboard(self, text: str) -> None:
        pb = self._pasteboard()
        pb.clearContents()
        if not pb.setString_forType_(str(text), self._string_type()):
            raise BackendError("NSPasteboard refused the clipboard text")

    #: Largest clipboard snapshot (all items and types together) clipboard-mode typing will
    #: hold in memory; a bigger clipboard (a copied video, a large design export) is refused.
    CLIPBOARD_SNAPSHOT_MAX = 64 * 1024 * 1024
    #: Attempts to read a consistent snapshot while another app keeps changing the clipboard.
    CLIPBOARD_SNAPSHOT_ATTEMPTS = 3

    def save_clipboard(self) -> Any:
        """Every item on the pasteboard with all of its types (text, images, file
        URLs, rich text), so clipboard-mode typing gives back exactly what was there.

        Fails closed: if the snapshot cannot be exact -- a type's data is unavailable,
        the clipboard is larger than :attr:`CLIPBOARD_SNAPSHOT_MAX`, or it keeps changing
        while it is read (another app, a clipboard manager, Universal Clipboard) -- it
        raises :class:`BackendError`. Clipboard-mode typing then stops *before* it
        overwrites the clipboard, instead of later "restoring" a partial or mixed copy
        over the user's data.
        """
        pb = self._pasteboard()
        change_count = getattr(pb, "changeCount", None)
        for _attempt in range(max(1, int(self.CLIPBOARD_SNAPSHOT_ATTEMPTS))):
            before = change_count() if callable(change_count) else None
            items, total = [], 0
            for item in pb.pasteboardItems() or []:
                entry = []
                for typ in item.types() or []:
                    data = item.dataForType_(typ)
                    if data is None:
                        raise BackendError(
                            f"the clipboard's {str(typ)!r} data is unavailable, so it cannot be saved exactly; "
                            "refusing to overwrite it (use another typing mode)")
                    total += len(data)
                    if total > self.CLIPBOARD_SNAPSHOT_MAX:
                        raise BackendError(
                            f"the clipboard holds more than {self.CLIPBOARD_SNAPSHOT_MAX // (1024 * 1024)} MB; "
                            "refusing to overwrite it (use another typing mode)")
                    entry.append((str(typ), bytes(data)))
                if entry:
                    items.append(entry)
            if before is None or change_count() == before:
                return {"items": items}
        raise BackendError("the clipboard kept changing while it was saved; refusing to overwrite it")

    def restore_clipboard(self, saved: Any) -> None:
        """Put back a :meth:`save_clipboard` snapshot. Every item is rebuilt and checked
        *before* the pasteboard is touched; a refused type or write raises
        :class:`BackendError` instead of leaving a silently partial clipboard."""
        if not isinstance(saved, dict):
            self.set_clipboard(saved if saved is not None else "")
            return
        appkit = self._appkit
        pb = self._pasteboard()
        objects = []
        for entry in saved.get("items") or []:
            item = appkit.NSPasteboardItem.alloc().init()
            for typ, data in entry:
                if not item.setData_forType_(appkit.NSData.dataWithBytes_length_(data, len(data)), typ):
                    raise BackendError(f"could not rebuild the saved clipboard's {typ!r} data")
            objects.append(item)
        pb.clearContents()
        if objects and not pb.writeObjects_(objects):
            raise BackendError("NSPasteboard refused the saved clipboard contents")
