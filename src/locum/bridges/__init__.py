"""Bridges to enterprise UI technologies that Windows UI Automation cannot see into.

* :mod:`locum.bridges.sap` -- SAP GUI for Windows through the SAP GUI
  Scripting API (COM; ``pip install locum[sap]``).
* :mod:`locum.bridges.java` -- Swing/AWT applications through the Java Access
  Bridge (``WindowsAccessBridge-64.dll`` / ``-32.dll``, ctypes only).
* :mod:`locum.bridges.remote` -- an image-only session scoped to one remote
  desktop window (Citrix Workspace, RDP, VMware Horizon): template matching and
  OCR inside that window, with input bound to it.

Nothing here is imported by ``import locum``; each bridge imports its platform
dependencies lazily and raises :class:`~locum.errors.NotSupported` with an
install/enable hint when they are missing. Acting calls go through the same
:class:`~locum.llm.safety.Policy` as :class:`~locum.llm.safety.GuardedDesktop`
when a policy is configured (pass ``policy=`` or a ``GuardedDesktop``).
See ``docs/BRIDGES.md``.
"""

from __future__ import annotations

__all__ = ["sap", "java", "remote"]
