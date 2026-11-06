"""Drive Windows Calculator with accessibility selectors -- no coordinates, no images.

Every button is found by its UI Automation id (``num7Button``, ``plusButton``...)
or, failing that, by its accessible name ("Seven", "Plus", "Equals"), and the
answer is read back from the ``CalculatorResults`` element ("Display is 15").

Real Windows 11 desktop (launches Calculator, clicks its buttons):

    python examples/calculator_selectors.py --expr "12+30"

Built-in mock Calculator (no real input, safe anywhere, used by the tests):

    python examples/calculator_selectors.py --backend mock

The real run starts Calculator in Standard mode, which evaluates left to right
("2+3*4" gives 20); the mock Calculator does the same.

Fail-safe: move the mouse into any screen corner to abort a real run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from locum import Desktop

# Key -> (automation id, accessible name) as exposed by Windows 11 Calculator.
BUTTONS = {
    **{str(i): (f"num{i}Button", name) for i, name in enumerate(
        ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"])},
    "+": ("plusButton", "Plus"),
    "-": ("minusButton", "Minus"),
    "*": ("multiplyButton", "Multiply by"),
    "/": ("divideButton", "Divide by"),
    "=": ("equalButton", "Equals"),
    "C": ("clearButton", "Clear"),
}

# The same selector text works on the real app and on the mock below.
RESULT = "#CalculatorResults"

StepHook = Optional[Callable[[Desktop, str], None]]


def button_selector(key: str) -> str:
    """Automation id first (locale-independent), accessible name as a fallback."""
    aid, name = BUTTONS[key]
    return f'button#{aid}, button "{name}"'


def read_display(window) -> str:
    """'Display is 1,234.5' -> '1234.5' (the element's accessible name holds the number)."""
    name = window.find(RESULT).name
    return re.sub(r"^\s*Display is\s*", "", name).replace(",", "").strip()


def run(d: Desktop, expr: str, *, on_step: StepHook = None) -> str:
    """Launch Calculator, key in ``expr`` (digits and + - * /), press Equals, return the result."""
    step = on_step or (lambda desk, label: None)
    already_open = {w.handle for w in d.windows("Calculator")}
    calc = d.launch("calc.exe", wait_window="Calculator", timeout=15)
    if calc.handle in already_open:  # never drive a Calculator you already had open
        raise SystemExit("Calculator did not open a new window; close yours and retry")
    calc.activate()
    step(d, "Calculator is open")

    calc.find(button_selector("C")).click()
    for key in expr.replace(" ", "") + "=":
        if key not in BUTTONS:
            raise SystemExit(f"unsupported key {key!r}: use digits and + - * /")
        calc.find(button_selector(key)).click()
        step(d, f"clicked {BUTTONS[key][1]!r}")

    result = read_display(calc)
    step(d, f"result {result}")
    return result  # Calculator is left open: it may be a window you already had


# ------------------------------------------------------------------------------ mock Calculator


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal(1))) if value == value.to_integral() else format(value.normalize(), "f")


def build_mock_desktop(width: int = 800, height: int = 600) -> Desktop:
    """A mock desktop where ``launch("calc.exe")`` opens a working, Calculator-shaped window."""
    from locum.backends.mock import MockBackend

    class CalcBackend(MockBackend):
        def launch(self, command, **kwargs):
            super().launch(command, **kwargs)
            _add_calculator(self, width, height)
            return None

    return Desktop(CalcBackend(width=width, height=height), sleep=lambda s: None, default_timeout=2)


def _add_calculator(be, width: int, height: int) -> None:
    win = be.add_window("Calculator", ((width - 320) // 2, (height - 440) // 2, 320, 440), app="CalculatorApp")
    state = {"acc": Decimal(0), "entry": "0", "op": None, "fresh": True}
    display = win.add_text("0", (16, 40, 288, 60), automation_id="CalculatorResults", fill=(250, 250, 250))

    def show(text: str) -> None:
        display.name, display.value = f"Display is {text}", text

    def apply(a: Decimal, op: str, b: Decimal) -> Decimal:
        if op == "/" and b == 0:
            raise ZeroDivisionError
        return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else a}[op]

    def press(key: str) -> None:
        s = state
        try:
            if key.isdigit():
                s["entry"] = key if s["fresh"] or s["entry"] == "0" else s["entry"] + key
                s["fresh"] = False
                show(s["entry"])
            elif key == "C":
                s.update(acc=Decimal(0), entry="0", op=None, fresh=True)
                show("0")
            else:  # an operator or "=": evaluate left to right, like Standard mode
                entry = Decimal(s["entry"])
                s["acc"] = apply(s["acc"], s["op"], entry) if s["op"] and not s["fresh"] else (
                    s["acc"] if s["op"] else entry)
                s["op"] = None if key == "=" else key
                s["fresh"] = True
                s["entry"] = _fmt(s["acc"])
                show(s["entry"])
        except (ZeroDivisionError, InvalidOperation):
            s.update(acc=Decimal(0), entry="0", op=None, fresh=True)
            show("Cannot divide by zero")

    layout = ["C", None, None, "/", "7", "8", "9", "*", "4", "5", "6", "-", "1", "2", "3", "+", None, "0", None, "="]
    for i, key in enumerate(layout):
        if key is None:
            continue
        aid, name = BUTTONS[key]
        row, col = divmod(i, 4)
        win.add_button(name, (16 + col * 74, 116 + row * 62, 66, 54), automation_id=aid,
                       on_click=lambda w, k=key: press(k))
    show("0")


# ------------------------------------------------------------------------------ entry point


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--backend", choices=["auto", "mock", "windows"], default=None,
                   help="mock = built-in fake Calculator; default: $LOCUM_BACKEND, else the real desktop")
    p.add_argument("--expr", default="7+8", help="digits and + - * / (default: 7+8)")
    args = p.parse_args(argv)
    backend = (args.backend or os.environ.get("LOCUM_BACKEND") or "auto").lower()
    mock = backend in ("mock", "demo")

    if mock:
        d = build_mock_desktop()
    elif sys.platform != "win32":
        print("The real-desktop run needs Windows Calculator; try --backend mock", file=sys.stderr)
        return 3
    else:
        print("Driving the real Calculator. Move the mouse into a screen corner to abort.")
        d = Desktop()  # fail-safe is on by default

    with d:
        result = run(d, args.expr)
    print(f"{args.expr} = {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
