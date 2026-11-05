"""Backend discovery.

``get_backend()`` returns a process-wide default backend chosen from, in order:

1. the ``PYOILOT_BACKEND`` environment variable (``mock``, ``x11``, ``windows``, ``macos``)
2. the current platform (``win32`` -> windows, ``darwin`` -> macos, otherwise x11)

Third-party backends can be registered with :func:`register_backend`.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import Callable, Dict, Optional, Union

from ..errors import BackendUnavailable
from .base import AccessibleNode, Backend

_REGISTRY: Dict[str, Union[str, Callable[[], Backend]]] = {
    "mock": "pyoilot.backends.mock:MockBackend",
    "x11": "pyoilot.backends.x11:X11Backend",
    "windows": "pyoilot.backends.windows:WindowsBackend",
    "macos": "pyoilot.backends.macos:MacOSBackend",
}

_default: Optional[Backend] = None
_lock = threading.Lock()


def register_backend(name: str, factory: Union[str, Callable[[], Backend]]) -> None:
    """Register ``factory`` (a callable or ``"module:Class"`` string) under ``name``."""
    _REGISTRY[name.lower()] = factory


def available_backends() -> list:
    return sorted(_REGISTRY)


def platform_backend_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "x11"


def create_backend(name: Optional[str] = None, **kwargs) -> Backend:
    """Instantiate a backend by name (default: env var / platform)."""
    name = (name or os.environ.get("PYOILOT_BACKEND") or platform_backend_name()).lower()
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
    "platform_backend_name",
    "register_backend",
    "set_backend",
]
