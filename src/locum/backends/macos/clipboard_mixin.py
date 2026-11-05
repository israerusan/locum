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
