"""Windows integrity levels and UIPI (User Interface Privilege Isolation).

Windows silently drops synthesized input (``SendInput``) aimed at a window of a
process running at a *higher* integrity level than the sender -- typically an
app started "as administrator" while locum runs unelevated. ``SendInput`` still
reports success, so without this check clicks and keystrokes just vanish.
:func:`uipi_block` names the blocking window so the backend can raise a clear
:class:`~locum.errors.PermissionDenied` instead.

Every function returns ``None`` when the level cannot be read (missing API,
access denied, protected process): a failed check never blocks input.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Dict, Optional, Tuple

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel
GA_ROOT = 2

#: Mandatory-label RIDs, highest first.
LEVELS = ((0x4000, "System"), (0x3000, "High"), (0x2000, "Medium"), (0x1000, "Low"), (0x0, "Untrusted"))


def level_name(rid: int) -> str:
    for base, name in LEVELS:
        if rid >= base:
            return name
    return "Untrusted"


def _have(api: Any, *names: str) -> bool:
    return all(getattr(api, n, None) is not None for n in names)


def token_integrity(api: Any, hproc: Any) -> Optional[int]:
    """Integrity RID of the process behind ``hproc`` (``None`` if unreadable)."""
    if not _have(api, "OpenProcessToken", "GetTokenInformation", "GetSidSubAuthorityCount", "GetSidSubAuthority"):
        return None
    token = ctypes.c_void_p()
    if not api.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = ctypes.c_ulong(0)
        api.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_MANDATORY_LABEL.Label.Sid
        if not sid:
            return None
        count = api.GetSidSubAuthorityCount(sid)
        n = int(count[0]) if count else 0
        if n < 1:
            return None
        return int(api.GetSidSubAuthority(sid, n - 1)[0])
    finally:
        api.CloseHandle(token)


def own_integrity(api: Any) -> Optional[int]:
    if not _have(api, "GetCurrentProcess"):
        return None
    try:
        return token_integrity(api, api.GetCurrentProcess())
    except Exception:
        return None


def process_integrity(api: Any, pid: int) -> Optional[int]:
    if not pid or not _have(api, "OpenProcess", "CloseHandle"):
        return None
    try:
        hproc = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    except Exception:
        return None
    if not hproc:
        return None
    try:
        return token_integrity(api, hproc)
    except Exception:
        return None
    finally:
        api.CloseHandle(hproc)


class UipiGuard:
    """Caches integrity lookups (a window's process does not change level)."""

    TTL = 5.0

    def __init__(self, api: Any) -> None:
        self.api = api
        self._own: Optional[int] = None
        self._own_read = False
        self._cache: Dict[Tuple[int, int], Tuple[float, Optional[int]]] = {}

    def own(self) -> Optional[int]:
        if not self._own_read:
            self._own = own_integrity(self.api)
            self._own_read = True
        return self._own

    def window_level(self, hwnd: int, pid: int) -> Optional[int]:
        key = (int(hwnd), int(pid))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self.TTL:
            return hit[1]
        level = process_integrity(self.api, pid)
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[key] = (now, level)
        return level

    def block(self, hwnd: int, pid: int) -> Optional[Tuple[int, int]]:
        """``(own, target)`` RIDs when input to ``hwnd`` would be dropped, else ``None``."""
        if not hwnd or not pid:
            return None
        own = self.own()
        if own is None:
            return None
        target = self.window_level(hwnd, pid)
        if target is None or target <= own:
            return None
        return own, target


__all__ = ["LEVELS", "UipiGuard", "level_name", "own_integrity", "process_integrity", "token_integrity"]
