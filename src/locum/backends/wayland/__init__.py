"""Native Wayland backend (experimental) built on xdg-desktop-portal and AT-SPI.

Wayland deliberately gives no client a way to inject input into, read pixels
of, or move another client's windows. The sanctioned route is the
*xdg-desktop-portal*, reached over D-Bus (``jeepney``, pure Python, installed
with ``pip install locum[wayland]``):

* **Input** -- ``org.freedesktop.portal.RemoteDesktop``: a session with
  keyboard + pointer devices and a ``ScreenCast`` monitor selection (absolute
  pointer motion is defined per screen-cast stream). The compositor shows an
  approval dialog once; the ``restore_token`` (``persist_mode=2``) is kept in
  ``$XDG_STATE_HOME/locum/wayland-portal.json`` (mode 0600) so later runs are
  approved silently until the user revokes the permission. Keys are sent as X
  keysyms (``NotifyKeyboardKeysym``, the X11 backend's keysym table), buttons
  as evdev codes, wheel notches with ``NotifyPointerAxisDiscrete``.
* **Capture** -- ``org.freedesktop.portal.Screenshot`` (non-interactive). It may
  show a permission prompt; each capture is a full-desktop PNG written to disk
  by the portal (locum loads, crops and deletes it), so it is far slower than
  X11/Windows capture.
* **Windows / elements** -- AT-SPI, shared with the X11 backend: window list,
  element tree, selectors, native actions. Window activation, moving, resizing,
  state changes and closing raise :class:`~locum.errors.NotSupported`.

Supported portals: xdg-desktop-portal-gnome (GNOME/mutter) and -kde (Plasma)
implement RemoteDesktop; xdg-desktop-portal-wlr (Sway and other wlroots
compositors) does not, so there only screenshots work.

Status: written against the portal specification and tested with a fake D-Bus;
**not yet validated on a real compositor**. ``WaylandBackend.limitations()``
(shown by ``locum info``) lists what it cannot do or verify.
"""

from __future__ import annotations

from ._portal import JeepneyBus, PortalBus, PortalClient, PortalError, SignalWatch, TokenStore, default_state_path
from .atspi import WaylandAtspiNode
from .backend import EVDEV_BUTTONS, WaylandBackend, parse_streams

__all__ = [
    "EVDEV_BUTTONS",
    "JeepneyBus",
    "PortalBus",
    "PortalClient",
    "PortalError",
    "SignalWatch",
    "TokenStore",
    "WaylandAtspiNode",
    "WaylandBackend",
    "default_state_path",
    "parse_streams",
]
