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

    def save_clipboard(self) -> Any:
        """Every item on the pasteboard with all of its types (text, images, file
        URLs, rich text), so clipboard-mode typing gives back exactly what was there."""
        items = []
        for item in self._pasteboard().pasteboardItems() or []:
            entry = []
            for typ in item.types() or []:
                data = item.dataForType_(typ)
                if data is not None:
                    entry.append((str(typ), bytes(data)))
            items.append(entry)
        return {"items": items}

    def restore_clipboard(self, saved: Any) -> None:
        if not isinstance(saved, dict):
            self.set_clipboard(saved if saved is not None else "")
            return
        appkit = self._appkit
        pb = self._pasteboard()
        pb.clearContents()
        objects = []
        for entry in saved.get("items") or []:
            item = appkit.NSPasteboardItem.alloc().init()
            for typ, data in entry:
                item.setData_forType_(appkit.NSData.dataWithBytes_length_(data, len(data)), typ)
            objects.append(item)
        if objects and not pb.writeObjects_(objects):
            raise BackendError("NSPasteboard refused the saved clipboard contents")
