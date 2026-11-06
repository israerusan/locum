"""Backend discovery.

``get_backend()`` returns a process-wide default backend chosen from, in order:

1. the ``LOCUM_BACKEND`` environment variable (``mock``, ``x11``, ``wayland``, ``windows``, ``macos``)
2. the current platform (``win32`` -> windows, ``darwin`` -> macos, otherwise x11;
   the experimental ``wayland`` backend is never chosen automatically, see
   :func:`select_platform_backend`)

Third-party backends can be registered with :func:`register_backend`.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import Callable, Dict, Mapping, Optional, Union

from ..errors import BackendUnavailable
from .base import AccessibleNode, Backend

_REGISTRY: Dict[str, Union[str, Callable[[], Backend]]] = {
    "mock": "locum.backends.mock:MockBackend",
    "x11": "locum.backends.x11:X11Backend",
    "wayland": "locum.backends.wayland:WaylandBackend",
    "windows": "locum.backends.windows:WindowsBackend",
    "macos": "locum.backends.macos:MacOSBackend",
}

_default: Optional[Backend] = None
_lock = threading.Lock()


def register_backend(name: str, factory: Union[str, Callable[[], Backend]]) -> None:
    """Register ``factory`` (a callable or ``"module:Class"`` string) under ``name``."""
    _REGISTRY[name.lower()] = factory


def available_backends() -> list:
    return sorted(_REGISTRY)


def is_wayland_session(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the desktop session is Wayland (``XDG_SESSION_TYPE=wayland``; when that is
    unset, a ``WAYLAND_DISPLAY`` counts)."""
    env = os.environ if environ is None else environ
    kind = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if kind:
        return kind == "wayland"
    return bool(env.get("WAYLAND_DISPLAY"))


def select_platform_backend(platform: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                            jeepney_available: Optional[bool] = None) -> str:
    """The default backend name for ``platform`` (default: this one) and ``environ``.

    The ``wayland`` backend is experimental: it has not been validated on a real
    GNOME or KDE session, so it is **never chosen automatically** (since 0.3.1).
    Opt in with ``LOCUM_BACKEND=wayland``. A Wayland session with XWayland
    (``$DISPLAY`` set) gets the tested ``x11`` backend, which reaches XWayland
    clients; a Wayland session with no X server also gets ``x11``, which then
    fails with a :class:`~locum.errors.BackendUnavailable` naming the opt-in.
    ``environ`` and ``jeepney_available`` are kept for compatibility.
    """
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return "windows"
    if plat == "darwin":
        return "macos"
    return "x11"


def platform_backend_name() -> str:
    return select_platform_backend()


def create_backend(name: Optional[str] = None, **kwargs) -> Backend:
    """Instantiate a backend by name (default: env var / platform)."""
    name = (name or os.environ.get("LOCUM_BACKEND") or platform_backend_name()).lower()
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise BackendUnavailable(f"Unknown backend {name!r}; known: {', '.join(available_backends())}") from None
    if isinstance(factory, str):
        mod_name, _, attr = factory.partition(":")
        try:
            factory = getattr(importlib.import_module(mod_name), attr)
        except ImportError as exc:
            raise BackendUnavailable(f"Backend {name!r} could not be imported: {exc}") from exc
    try:
        return factory(**kwargs)
    except BackendUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - platform specific
        raise BackendUnavailable(f"Backend {name!r} failed to initialise: {exc}") from exc


def get_backend() -> Backend:
    """Return (creating on first use) the process-wide default backend."""
    global _default
    if _default is None:
        with _lock:
            if _default is None:
                _default = create_backend()
    return _default


def set_backend(backend: Union[Backend, str, None]) -> Optional[Backend]:
    """Replace the default backend (an instance, a registered name, or None to reset)."""
    global _default
    with _lock:
        _default = create_backend(backend) if isinstance(backend, str) else backend
    return _default


__all__ = [
    "AccessibleNode",
    "Backend",
    "available_backends",
    "create_backend",
    "get_backend",
    "is_wayland_session",
    "platform_backend_name",
    "register_backend",
    "select_platform_backend",
    "set_backend",
]
