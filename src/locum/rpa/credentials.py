"""Credential vault: usernames and passwords kept in the operating system's credential store.

::

    from locum.rpa.credentials import get_credential, set_credential

    set_credential("erp", "robot01", "s3cret")      # once (or: locum credential set erp)
    cred = get_credential("erp")
    cred.username                                  # 'robot01'
    cred.password                                  # Secret('***')
    d.find('edit "Password"').type(cred.password)  # typed; never shown anywhere

Stores:

* **Windows**: Windows Credential Manager (``CredReadW`` / ``CredWriteW`` /
  ``CredDeleteW`` through ctypes, no dependency): generic credentials whose
  target name is ``locum:NAME`` -- visible under *Windows Credentials* in the
  Credential Manager control panel and to ``cmdkey /list:locum:*``. They are
  stored for the current user (persist: local machine).
* **macOS / Linux**: the optional `keyring <https://pypi.org/project/keyring/>`_
  package (``pip install locum[credentials]``): the macOS Keychain, the Secret
  Service (GNOME Keyring, KWallet)... Each credential is one item with service
  ``locum:NAME`` (account ``locum``) holding the username and password.
* **Environment variables** -- only when enabled explicitly
  (``LOCUM_CREDENTIALS_ENV=1``, ``get_credential(..., env=True)`` or
  :func:`enable_env_fallback`): ``LOCUM_CREDENTIAL_<NAME>_USERNAME`` and
  ``LOCUM_CREDENTIAL_<NAME>_PASSWORD`` (``NAME`` upper-cased, other characters
  than letters and digits as ``_``) for a credential the store does not have,
  or when there is no store (a CI runner). Read-only.

The password is a :class:`~locum._secret.Secret`: ``str``/``repr``/f-strings show
``***`` and its value is scrubbed from traces, the safety audit log, flow run
reports and logs, and CLI errors. Typing APIs (``Desktop.type``,
``Element.type``/``set_text``, ``clipboard_set``) accept it directly. In flows:
``${credential:erp.username}`` / ``${credential:erp.password}``.

CLI: ``locum credential set NAME`` (asks for the password without echo),
``locum credential delete NAME``, ``locum credential list`` (names only).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Tuple

from .._secret import Secret, reveal
from ..errors import BackendUnavailable, LocumError

__all__ = [
    "Credential",
    "CredentialNotFound",
    "CredentialStoreUnavailable",
    "CredentialStore",
    "Secret",
    "get_credential",
    "set_credential",
    "delete_credential",
    "list_credentials",
    "enable_env_fallback",
    "get_store",
    "set_store",
    "TARGET_PREFIX",
    "ENV_FALLBACK_VAR",
]

#: Prefix of every target name (Windows) / service name (keyring) locum writes.
TARGET_PREFIX = "locum:"
#: Set to ``1`` to let :func:`get_credential` fall back to environment variables.
ENV_FALLBACK_VAR = "LOCUM_CREDENTIALS_ENV"
_KEYRING_ACCOUNT = "locum"
_KEYRING_INDEX = ("locum", "credential-index")
_MAX_NAME = 200


class CredentialNotFound(LocumError, LookupError):
    """No credential of that name in the store (nor in the environment, when enabled)."""


class CredentialStoreUnavailable(BackendUnavailable):
    """No credential store on this system (install ``keyring``: ``pip install locum[credentials]``)."""


@dataclass(frozen=True)
class Credential:
    """A username and its password (a :class:`Secret`: ``repr`` never shows it)."""

    name: str
    username: str
    password: Secret
    #: Where it came from: ``"wincred"``, ``"keyring"``, ``"env"`` or a custom store's name.
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.password, Secret):
            object.__setattr__(self, "password", Secret(str(self.password)))


class CredentialStore(Protocol):
    """What a store implements (see :func:`set_store`)."""

    name: str

    def get(self, name: str) -> Optional[Tuple[str, str]]:  # (username, password) or None
        ...

    def set(self, name: str, username: str, password: str) -> None:
        ...

    def delete(self, name: str) -> bool:  # False: there was none
        ...

    def names(self) -> List[str]:
        ...


def _check_name(name: Any) -> str:
    if not isinstance(name, str):
        raise TypeError(f"credential name must be a str, not {type(name).__name__}")
    n = name.strip()
    if not n:
        raise ValueError("credential name must not be empty")
    if len(n) > _MAX_NAME:
        raise ValueError(f"credential name is longer than {_MAX_NAME} characters")
    if any(ord(c) < 32 for c in n) or "*" in n:
        raise ValueError(f"credential name {n!r} contains a control character or '*'")
    return n


# =============================================================================== Windows

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512

_DWORD = ctypes.c_uint32


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class CREDENTIALW(ctypes.Structure):
    """``CREDENTIALW`` (wincred.h)."""

    _fields_ = [
        ("Flags", _DWORD),
        ("Type", _DWORD),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", _DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", _DWORD),
        ("AttributeCount", _DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


_PCRED = ctypes.POINTER(CREDENTIALW)


class WindowsCredentialStore:
    """Windows Credential Manager through ``advapi32`` (generic credentials ``locum:NAME``).

    ``dll`` / ``last_error`` replace ``advapi32`` and ``ctypes.get_last_error`` (tests).
    """

    name = "wincred"

    def __init__(self, dll: Any = None, last_error: Any = None) -> None:
        if dll is None:
            dll = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
            last_error = ctypes.get_last_error  # type: ignore[attr-defined]
            dll.CredReadW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.POINTER(_PCRED)]
            dll.CredReadW.restype = ctypes.c_int
            dll.CredWriteW.argtypes = [_PCRED, _DWORD]
            dll.CredWriteW.restype = ctypes.c_int
            dll.CredDeleteW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD]
            dll.CredDeleteW.restype = ctypes.c_int
            dll.CredEnumerateW.argtypes = [ctypes.c_wchar_p, _DWORD, ctypes.POINTER(_DWORD),
                                           ctypes.POINTER(ctypes.POINTER(_PCRED))]
            dll.CredEnumerateW.restype = ctypes.c_int
            dll.CredFree.argtypes = [ctypes.c_void_p]
            dll.CredFree.restype = None
        self._dll = dll
        self._last_error = last_error or (lambda: 0)

    def _fail(self, what: str) -> OSError:
        code = int(self._last_error() or 0)
        return OSError(code, f"{what} failed (Windows error {code})")

    def get(self, name: str) -> Optional[Tuple[str, str]]:
        pcred = _PCRED()
        if not self._dll.CredReadW(TARGET_PREFIX + name, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            if int(self._last_error() or 0) == ERROR_NOT_FOUND:
                return None
            raise self._fail("CredReadW")
        try:
            cred = pcred.contents
            size = int(cred.CredentialBlobSize)
            blob = ctypes.string_at(cred.CredentialBlob, size) if size and cred.CredentialBlob else b""
            return (cred.UserName or "", blob.decode("utf-16-le", errors="replace"))
        finally:
            self._dll.CredFree(pcred)

    def set(self, name: str, username: str, password: str) -> None:
        blob = password.encode("utf-16-le")
        if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise ValueError(f"password is too long for Windows Credential Manager "
                             f"({len(blob)} > {CRED_MAX_CREDENTIAL_BLOB_SIZE} bytes)")
        buf = ctypes.create_string_buffer(blob, len(blob) or 1)
        try:
            cred = CREDENTIALW()
            cred.Flags = 0
            cred.Type = CRED_TYPE_GENERIC
            cred.TargetName = TARGET_PREFIX + name
            cred.Comment = "locum credential"
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = username
            if not self._dll.CredWriteW(ctypes.byref(cred), 0):
                raise self._fail("CredWriteW")
        finally:
            ctypes.memset(buf, 0, len(buf))  # no copy of the password left in this process's buffer

    def delete(self, name: str) -> bool:
        if self._dll.CredDeleteW(TARGET_PREFIX + name, CRED_TYPE_GENERIC, 0):
            return True
        if int(self._last_error() or 0) == ERROR_NOT_FOUND:
            return False
        raise self._fail("CredDeleteW")

    def names(self) -> List[str]:
        count = _DWORD(0)
        pcreds = ctypes.POINTER(_PCRED)()
        if not self._dll.CredEnumerateW(TARGET_PREFIX + "*", 0, ctypes.byref(count), ctypes.byref(pcreds)):
            if int(self._last_error() or 0) == ERROR_NOT_FOUND:
                return []
            raise self._fail("CredEnumerateW")
        try:
            out = []
            for i in range(int(count.value)):
                c = pcreds[i].contents
                target = c.TargetName or ""
                if int(c.Type) == CRED_TYPE_GENERIC and target.startswith(TARGET_PREFIX):
                    out.append(target[len(TARGET_PREFIX):])
            return sorted(out)
        finally:
            self._dll.CredFree(pcreds)


# =============================================================================== keyring


class KeyringCredentialStore:
    """The ``keyring`` package (macOS Keychain, Secret Service, KWallet...)."""

    name = "keyring"

    def __init__(self, keyring_module: Any = None) -> None:
        if keyring_module is None:
            import keyring as keyring_module  # noqa: PLC0415 - optional dependency
        self._kr = keyring_module
        self._lock = threading.Lock()

    def _index(self) -> List[str]:
        raw = self._kr.get_password(*_KEYRING_INDEX)
        try:
            names = json.loads(raw) if raw else []
        except ValueError:
            names = []
        return [n for n in names if isinstance(n, str)]

    def _save_index(self, names: List[str]) -> None:
        self._kr.set_password(*_KEYRING_INDEX, json.dumps(sorted(set(names))))

    def get(self, name: str) -> Optional[Tuple[str, str]]:
        raw = self._kr.get_password(TARGET_PREFIX + name, _KEYRING_ACCOUNT)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return (str(data.get("username", "")), str(data["password"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LocumError(f"credential {name!r} in the keyring is not a locum credential") from None

    def set(self, name: str, username: str, password: str) -> None:
        self._kr.set_password(TARGET_PREFIX + name, _KEYRING_ACCOUNT,
                              json.dumps({"username": username, "password": password}))
        with self._lock:
            names = self._index()
            if name not in names:
                self._save_index(names + [name])

    def delete(self, name: str) -> bool:
        errors = getattr(self._kr, "errors", None)
        delete_error = getattr(errors, "PasswordDeleteError", None) if errors is not None else None
        try:
            if self._kr.get_password(TARGET_PREFIX + name, _KEYRING_ACCOUNT) is None:
                found = False
            else:
                self._kr.delete_password(TARGET_PREFIX + name, _KEYRING_ACCOUNT)
                found = True
        except Exception as exc:
            if delete_error is not None and isinstance(exc, delete_error):
                found = False
            else:
                raise
        with self._lock:
            names = self._index()
            if name in names:
                self._save_index([n for n in names if n != name])
        return found

    def names(self) -> List[str]:
        return sorted(self._index())


# =============================================================================== environment


def _env_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def _from_env(name: str) -> Optional[Tuple[str, str]]:
    key = _env_key(name)
    password = os.environ.get(f"LOCUM_CREDENTIAL_{key}_PASSWORD")
    if password is None:
        return None
    return (os.environ.get(f"LOCUM_CREDENTIAL_{key}_USERNAME", ""), password)


# =============================================================================== API

_store_lock = threading.Lock()
_store: Any = None
_env_enabled: Optional[bool] = None


def _default_store() -> Any:
    if sys.platform == "win32":
        return WindowsCredentialStore()
    try:
        return KeyringCredentialStore()
    except ImportError:
        raise CredentialStoreUnavailable(
            "no credential store: install keyring for the macOS Keychain / Secret Service "
            "(pip install locum[credentials]), or enable the environment fallback "
            f"({ENV_FALLBACK_VAR}=1)") from None


def get_store() -> Any:
    """The credential store in use (created on first use). Raises :class:`CredentialStoreUnavailable`."""
    global _store
    with _store_lock:
        if _store is None:
            _store = _default_store()
        return _store


def set_store(store: Any) -> Any:
    """Use ``store`` (a :class:`CredentialStore`; ``None`` = the platform default, chosen lazily).
    Returns the previous one."""
    global _store
    with _store_lock:
        old, _store = _store, store
    return old


def enable_env_fallback(enabled: Optional[bool] = True) -> None:
    """Let :func:`get_credential` read ``LOCUM_CREDENTIAL_<NAME>_*`` environment variables
    (``None``: back to ``$LOCUM_CREDENTIALS_ENV``)."""
    global _env_enabled
    _env_enabled = enabled


def _env_allowed(env: Optional[bool]) -> bool:
    if env is not None:
        return bool(env)
    if _env_enabled is not None:
        return _env_enabled
    return os.environ.get(ENV_FALLBACK_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def get_credential(name: str, *, env: Optional[bool] = None) -> Credential:
    """The credential ``name`` from the OS store.

    Args:
        name: The credential's name (``locum credential set NAME``).
        env: Fall back to ``LOCUM_CREDENTIAL_<NAME>_USERNAME`` / ``_PASSWORD`` environment
            variables when the store has no such credential, or there is no store
            (default: only when enabled with :func:`enable_env_fallback` or
            ``LOCUM_CREDENTIALS_ENV=1``).

    Raises:
        CredentialNotFound: Neither the store nor (when enabled) the environment has it.
        CredentialStoreUnavailable: No store, and the environment fallback is off.
    """
    n = _check_name(name)
    use_env = _env_allowed(env)
    try:
        store = get_store()
    except CredentialStoreUnavailable:
        if not use_env:
            raise
        store = None
    if store is not None:
        found = store.get(n)
        if found is not None:
            return Credential(n, found[0], Secret(found[1]), getattr(store, "name", type(store).__name__))
    if use_env:
        found = _from_env(n)
        if found is not None:
            return Credential(n, found[0], Secret(found[1]), "env")
    where = getattr(store, "name", "store") if store is not None else "the environment"
    hint = (f"; also looked for LOCUM_CREDENTIAL_{_env_key(n)}_PASSWORD" if use_env and store is not None else "")
    raise CredentialNotFound(f"no credential named {n!r} in {where}{hint} (store one with: "
                             f"locum credential set {n})")


def set_credential(name: str, username: str, password: Any) -> None:
    """Store (or replace) credential ``name`` in the OS store. ``password``: a str or a :class:`Secret`."""
    n = _check_name(name)
    if not isinstance(username, str):
        raise TypeError(f"username must be a str, not {type(username).__name__}")
    pw = reveal(password)
    if not isinstance(pw, str):
        raise TypeError("password must be a str or a Secret")
    Secret(pw)  # registered: scrubbed from anything that sees it from now on
    get_store().set(n, username, pw)


def delete_credential(name: str, *, missing_ok: bool = False) -> bool:
    """Remove credential ``name``; ``True`` if it existed. Raises :class:`CredentialNotFound`
    for a missing one unless ``missing_ok``."""
    n = _check_name(name)
    if get_store().delete(n):
        return True
    if missing_ok:
        return False
    raise CredentialNotFound(f"no credential named {n!r} to delete")


def list_credentials() -> List[str]:
    """Names of the stored locum credentials (never their values)."""
    return list(get_store().names())
