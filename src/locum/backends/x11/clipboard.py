"""Clipboard part of the X11 backend: ``xclip`` / ``xsel`` / ``wl-clipboard``
helpers or the native selection owner, as a mixin of :class:`X11Backend`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from ...errors import BackendError, BackendUnavailable, NotSupported
from ._xcore import _x_errors


class _ClipboardMixin:
    """Clipboard access of :class:`X11Backend` (helper tools or native selection)."""

    # ================================================================ clipboard
    def _detect_clipboard(self, tool: Optional[str]) -> Optional[Tuple[List[str], List[str]]]:
        tools = {
            "xclip": (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]),
            "xsel": (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]),
            "wl-clipboard": (["wl-paste", "--no-newline"], ["wl-copy"]),
        }
        # "native" = python-xlib selection handling (used automatically when no helper is installed)
        self._clip_mode = "native"
        if tool is not None:
            t = tool.lower()
            if t in ("none", ""):
                self._clip_mode = "none"
                return None
            if t in ("native", "xlib"):
                return None
            if t in ("wl-copy", "wl-paste", "wayland"):
                t = "wl-clipboard"
            if t not in tools:
                raise ValueError(f"Unknown clipboard tool {tool!r}; expected one of {', '.join(tools)} or 'none'")
            get, put = tools[t]
            if shutil.which(get[0]) is None or shutil.which(put[0]) is None:
                raise BackendUnavailable(f"Clipboard tool {tool!r} is not installed")
            return list(get), list(put)
        order = ["xclip", "xsel"]
        if os.environ.get("WAYLAND_DISPLAY"):
            order.insert(0, "wl-clipboard")
        for name in order:
            get, put = tools[name]
            if shutil.which(get[0]) and shutil.which(put[0]):
                return list(get), list(put)
        return None

    def _native_clipboard(self) -> Any:
        if self._clip_mode == "none":
            raise NotSupported("Clipboard support was disabled (clipboard_tool='none')")
        with self._lock:
            self._check()
            if self._state.clipboard is None:
                from .._x11clip import X11Clipboard

                # Kept in the finalizer state so it is closed (connection and
                # serving thread) even when the backend is never closed.
                self._state.clipboard = X11Clipboard(self.display_name)
            return self._state.clipboard

    def _clip_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display_name
        return env

    @_x_errors
    def get_clipboard(self) -> str:
        """Clipboard text (empty string when the clipboard is empty or holds no text)."""
        self._check()
        if self._clip_cmds is None:
            return self._native_clipboard().get()
        try:
            proc = subprocess.run(
                self._clip_cmds[0], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._clip_env(), timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"Clipboard read timed out ({' '.join(self._clip_cmds[0])})") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run clipboard helper: {exc}") from exc
        if proc.returncode != 0:
            return ""  # helpers exit non-zero when there is no text selection owner
        return proc.stdout.decode("utf-8", "replace")

    @_x_errors
    def set_clipboard(self, text: str) -> None:
        self._check()
        if not isinstance(text, str):
            raise TypeError(f"text must be str, not {type(text).__name__}")
        if self._clip_cmds is None:
            self._native_clipboard().set(text)
            return
        try:
            # The helpers fork a daemon that owns the selection; never capture
            # its output or run() would wait for the daemon to exit.
            proc = subprocess.run(
                self._clip_cmds[1], input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._clip_env(), timeout=5, start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"Clipboard write timed out ({' '.join(self._clip_cmds[1])})") from exc
        except OSError as exc:
            raise BackendError(f"Cannot run clipboard helper: {exc}") from exc
        if proc.returncode != 0:
            raise BackendError(f"Clipboard helper {self._clip_cmds[1][0]!r} failed with exit code {proc.returncode}")

    def wait_clipboard_read(self, since: float, timeout: float) -> Optional[bool]:
        """With the native clipboard: wait until an application fetched the text we own
        (see :meth:`Backend.wait_clipboard_read`). Helper tools own the selection in
        another process, so they cannot tell (None)."""
        if self._clip_cmds is not None or self._clip_mode == "none":
            return None
        with self._lock:
            clip = self._state.clipboard
        if clip is None:
            return None
        return clip.wait_read(since, timeout)
