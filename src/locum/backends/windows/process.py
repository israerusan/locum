"""Program launching for the Windows backend: ``ShellExecuteEx`` flags,
:func:`shell_launch_target`, :class:`ShellProcess` and the :class:`_ProcessMixin`.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
from typing import Any, Optional, Tuple

from ._win32api import DWORD, SHELLEXECUTEINFOW


# ShellExecuteEx flags
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
_PROGRAM_EXTS = (".exe", ".com", ".bat", ".cmd")


def shell_launch_target(command: str) -> Optional[Tuple[str, str]]:
    """``(file, parameters)`` when ``command`` must be started with ``ShellExecuteEx``, else None.

    ``CreateProcess`` (``subprocess.Popen``) only finds programs on ``PATH``, in
    System32 and the current folder. Start > Run also uses the *App Paths*
    registry (``excel``, ``winword``, ``outlook`` are registered there, not on
    ``PATH``) and file associations (``Q3.xlsx``, ``https://...``). A command
    whose program is an existing ``.exe``/``.bat`` path, or is found on
    ``PATH``, keeps using ``Popen`` (exact process, exact arguments).
    """
    cmd = command.strip()
    if not cmd:
        return None
    if os.path.isfile(cmd):  # an unquoted path, possibly with spaces
        return None if cmd.lower().endswith(_PROGRAM_EXTS) else (cmd, "")
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end < 0:
            return None
        file, params = cmd[1:end], cmd[end + 1:].strip()
    else:
        # Like CreateProcess with an unquoted path: try each whitespace-delimited
        # prefix ("C:\Program", "C:\Program Files\app.exe", ...) as the program.
        # With spaces, the whole command (no arguments) is the last candidate:
        # "C:\Program Files\App\app" -> app.exe, as CreateProcess does.
        splits = [(m.start(), m.end()) for m in re.finditer(r"\s+", cmd)]
        if splits:
            splits.append((len(cmd), len(cmd)))
        for start, end in splits:
            prefix = cmd[:start]
            if os.path.isfile(prefix) or os.path.isfile(prefix + ".exe"):
                if not os.path.isfile(prefix) or prefix.lower().endswith(_PROGRAM_EXTS):
                    return None
                return prefix, cmd[end:]
        parts = cmd.split(None, 1)
        file, params = parts[0], (parts[1] if len(parts) > 1 else "")
    if not file:
        return None
    if os.path.isfile(file):
        return None if file.lower().endswith(_PROGRAM_EXTS) else (file, params)
    if not os.path.splitext(file)[1] and os.path.isfile(file + ".exe"):
        return None  # CreateProcess appends ".exe" to an extension-less program path
    # (shutil.which only does that for full paths on Windows with Python >= 3.12)
    if shutil.which(file) is not None:
        return None
    return file, params


class ShellProcess:
    """What :meth:`WindowsBackend.launch` returns for a ``ShellExecuteEx`` launch.

    A small ``subprocess.Popen``-like object: ``args``, ``pid`` (None when the
    request was handed to an already-running instance, e.g. a second workbook
    opened in the running Excel), ``returncode``, ``poll()`` and ``wait()``.
    """

    def __init__(self, api: Any, args: str, handle: Any) -> None:
        self._api = api
        self.args = args
        self._handle = handle or None
        self.pid: Optional[int] = None
        self.returncode: Optional[int] = None
        if self._handle is not None:
            try:
                self.pid = int(api.GetProcessId(self._handle)) or None
            except Exception:
                self.pid = None
        else:
            self.returncode = 0  # no process of our own: the shell (or a running instance) handled it

    def poll(self) -> Optional[int]:
        return self._wait(0)

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        code = self._wait(0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000)))
        if code is None:  # timed out (Popen.wait raises for any finite timeout, 0 included)
            import subprocess

            raise subprocess.TimeoutExpired(self.args, timeout)
        return code

    def _wait(self, ms: int) -> Optional[int]:
        """Wait up to ``ms`` milliseconds; the exit code, or None if still running."""
        if self.returncode is not None or self._handle is None:
            return self.returncode
        if int(self._api.WaitForSingleObject(self._handle, ms)) != WAIT_OBJECT_0:
            return None
        code = DWORD(0)
        if self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            self.returncode = int(code.value)
        else:
            self.returncode = 0
        self._close()
        return self.returncode

    def _close(self) -> None:
        h, self._handle = self._handle, None
        if h is not None:
            try:
                self._api.CloseHandle(h)
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - finalizer
        self._close()

    def __repr__(self) -> str:
        return f"<ShellProcess {self.args!r} pid={self.pid} returncode={self.returncode}>"


class _ProcessMixin:
    """Program launching of :class:`WindowsBackend` (``ShellExecuteEx`` / ``Popen``)."""

    def launch(self, command: Any, **kwargs: Any) -> Any:
        """Start an application, a document or a URL the way Start > Run does.

        A command string whose program is a bare name not on ``PATH`` (``excel``,
        ``winword`` -- found through the *App Paths* registry), a document
        (``C:\\Reports\\Q3.xlsx``) or a URL is opened with ``ShellExecuteExW``
        and returns a :class:`ShellProcess`. Explicit program paths, commands
        found on ``PATH``, argument lists and ``subprocess.Popen`` keyword options
        (other than ``cwd``) use ``subprocess.Popen``.
        """
        target = shell_launch_target(command) if isinstance(command, str) and set(kwargs) <= {"cwd"} else None
        shell = getattr(self._api, "ShellExecuteExW", None)
        if target is None or shell is None:
            return super().launch(command, **kwargs)
        file, params = target
        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
        info.lpVerb = None  # the default verb ("open" for documents)
        info.lpFile = file
        info.lpParameters = params or None
        cwd = kwargs.get("cwd")
        info.lpDirectory = os.fspath(cwd) if cwd is not None else None
        info.nShow = SW_SHOWNORMAL
        co_init = getattr(self._api, "CoInitializeEx", None)
        hr = None
        if co_init is not None:  # shell extensions may need COM (STA, no OLE1 DDE)
            try:
                hr = int(co_init(None, 0x2 | 0x4))
            except Exception:
                hr = None
        try:
            ok = shell(ctypes.byref(info))
            err = self._api.get_last_error() if not ok else 0
        finally:
            if hr is not None and hr >= 0 and getattr(self._api, "CoUninitialize", None) is not None:
                self._api.CoUninitialize()
        if not ok:
            msg = f"Cannot launch {command!r}: {self._api.format_error(err)}"
            if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND, like Popen
                raise FileNotFoundError(2, msg, file)
            raise OSError(err, msg)
        return ShellProcess(self._api, command, info.hProcess)
