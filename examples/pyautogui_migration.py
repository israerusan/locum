"""Migrating from PyAutoGUI: the one-line switch, then the upgrade path.

Step 1 -- keep your script, change one import::

    import pyautogui                                   # before
    import locum.compat.pyautogui as pyautogui       # after (same functions, same semantics)

  or, without touching the script at all, run ``locum.compat.install_pyautogui_shim()``
  once before it imports ``pyautogui``.

Step 2 -- replace brittle coordinates and screenshots with what locum adds:
accessibility selectors, native set-value/invoke, waits, masked template
matching, humanized motion, RPA reports and LLM tools.

This example runs on locum's built-in demo desktop, so it is safe to run
anywhere (no real mouse/keyboard input is sent)::

    python examples/pyautogui_migration.py
"""

from __future__ import annotations

import sys

import locum.compat.pyautogui as pyautogui
from locum import Desktop
from locum.cli import DemoBackend


def legacy_pyautogui_style() -> str:
    """A typical PyAutoGUI script: hard-coded coordinates, sleeps, pixel guessing."""
    pyautogui.PAUSE = 0.0                      # module settings work as before
    pyautogui.FAILSAFE = True
    width, height = pyautogui.size()
    print(f"[legacy] screen {width}x{height}")

    pyautogui.click(370, 99)                   # "the Vendor box is at 370,99 on my machine"
    pyautogui.write("ACME Corp")
    pyautogui.press("tab")                     # tab to Invoice No
    pyautogui.write("INV-100")
    pyautogui.press("tab")
    pyautogui.write("99.90")
    pyautogui.moveTo(230, 284, duration=0.2, tween=pyautogui.easeInOutQuad)
    pyautogui.click()                          # Save

    # Image search returns PyAutoGUI's Box/Point namedtuples.
    shot = pyautogui.screenshot(region=(180, 268, 100, 32))       # the Save button
    box = pyautogui.locateOnScreen(shot, confidence=0.95, region=(0, 0, 700, 600))
    print(f"[legacy] Save button found at {tuple(box)}, center {tuple(pyautogui.center(box))}")
    return pyautogui.get_desktop().find("text#status").text


def locum_style(d: Desktop) -> str:
    """The same task, robust to window moves, resolution, DPI and theme changes."""
    form = d.window("Invoice Entry*")                   # glob / substring / regex titles
    form.activate()
    form.find("edit#vendor").set_text("Globex")         # native set-value, falls back to typing
    form.find('edit "Invoice No"').set_text("INV-101")
    form.find("edit#amount").set_text("250.00")
    d.click('button "Save"')                            # selector -> element -> click (or invoke if covered)
    status = d.wait("text#status:has-text('Saved')", timeout=5)   # explicit wait instead of sleep()
    return status.text


def main() -> int:
    demo = Desktop(DemoBackend(), sleep=lambda s: None, default_timeout=2)
    previous = pyautogui.set_desktop(demo)              # route the compat module to the demo desktop
    try:
        print("[legacy] status:", legacy_pyautogui_style())
        print("[locum] status:", locum_style(demo))
        print("invoices:", [(i["invoice_no"], i["vendor"], i["amount"]) for i in demo.backend.invoices])

        # Things PyAutoGUI cannot do:
        print(demo.tree(window="Invoice Entry*", max_depth=2, interactive_only=True).splitlines()[1])
        el = demo.element_at(370, 99)
        from locum.selectors import selector_for

        print("selector recorded for (370, 99):", selector_for(el.node))
    finally:
        pyautogui.set_desktop(previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
