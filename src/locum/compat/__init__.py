"""Compatibility layers that let existing automation scripts run on locum.

* :mod:`locum.compat.pyautogui` -- a drop-in subset of the PyAutoGUI API
  (``moveTo``, ``click``, ``write``, ``hotkey``, ``locateOnScreen``...).

Use it explicitly (``import locum.compat.pyautogui as pyautogui``) or call
:func:`install_pyautogui_shim` once so that unmodified scripts doing
``import pyautogui`` transparently get locum.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

__all__ = ["install_pyautogui_shim", "uninstall_pyautogui_shim"]


def install_pyautogui_shim(name: str = "pyautogui", *, force: bool = False) -> ModuleType:
    """Register :mod:`locum.compat.pyautogui` in ``sys.modules`` under ``name``.

    If a module is already registered under ``name`` (e.g. the real PyAutoGUI
    was imported) it is left alone unless ``force=True``. Returns the module
    now registered under ``name``.
    """
    existing = sys.modules.get(name)
    shim = importlib.import_module("locum.compat.pyautogui")
    if existing is not None and existing is not shim and not force:
        return existing
    sys.modules[name] = shim
    return shim


def uninstall_pyautogui_shim(name: str = "pyautogui") -> bool:
    """Remove the shim registered by :func:`install_pyautogui_shim`. Returns True if it was removed."""
    shim = sys.modules.get("locum.compat.pyautogui")
    if shim is not None and sys.modules.get(name) is shim:
        del sys.modules[name]
        return True
    return False
