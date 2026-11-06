"""Browser page automation over the Chrome DevTools Protocol (Edge, Chrome, Chromium).

See :mod:`locum.browser.cdp`. Standard library only.
"""

from .cdp import BROWSERS, Browser, CDPError, Page, PageInfo, discover_ports, find_executable, managed_profile_dir

__all__ = ["BROWSERS", "Browser", "CDPError", "Page", "PageInfo", "discover_ports", "find_executable", "managed_profile_dir"]
