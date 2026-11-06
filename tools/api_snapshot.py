"""Snapshot of locum's public API (see docs/API.md).

    python tools/api_snapshot.py            # print the snapshot
    python tools/api_snapshot.py --write    # update tests/data/public_api.txt
    python tools/api_snapshot.py --check    # exit 1 and show a diff if it changed

The snapshot lists every name in the ``__all__`` of each public module, with the
call signature of functions and classes and the signatures of the public
methods locum's own classes define. ``tests/test_public_api.py`` fails when the
snapshot no longer matches, so an API change is always a deliberate edit of the
snapshot file that shows up in review next to its CHANGELOG entry.
"""

from __future__ import annotations

import argparse
import difflib
import enum
import importlib
import inspect
import os
import re
import sys
from typing import Any, Iterator, List

#: Modules whose ``__all__`` is the public, semver-covered API. Keep in sync with docs/API.md.
PUBLIC_MODULES = [
    "locum",
    "locum.errors",
    "locum.selectors",
    "locum.rpa",
    "locum.llm",
    "locum.browser",
    "locum.trace",
    "locum.heal",
    "locum.a11y",
    "locum.compat.pyautogui",
]
#: Optional modules: snapshotted when they exist (added by later releases).
OPTIONAL_MODULES = [
    "locum.bridges.sap",
    "locum.bridges.java",
    "locum.bridges.remote",
    "locum.rpa.credentials",
]

#: Experimental modules (docs/API.md, "Experimental"): snapshotted so a change still shows up
#: in review, under a marked header, but they carry no compatibility promise.
EXPERIMENTAL_MODULES = {"locum.bridges.sap", "locum.bridges.java"}

SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "data", "public_api.txt")
_ADDR = re.compile(r" at 0x[0-9A-Fa-f]+")


class _Shown:
    """A default value rendered by a fixed text (its repr would be unstable)."""

    def __init__(self, text: str):
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _stable_default(value: Any) -> Any:
    # Sets print in hash order (random per process); long keyword/pattern tables are
    # data, not signature: show the container type only, so tuning them is not an API change.
    if isinstance(value, (set, frozenset, tuple, list, dict)) and (isinstance(value, (set, frozenset)) or len(value) > 4):
        return _Shown(f"<{type(value).__name__}>")
    return value


def _sig(obj: Any) -> str:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return "(...)"
    params = [p.replace(default=_stable_default(p.default)) if p.default is not p.empty else p
              for p in sig.parameters.values()]
    return _ADDR.sub("", str(sig.replace(parameters=params)))


def _own_methods(cls: type) -> Iterator[str]:
    for name, member in sorted(vars(cls).items()):
        if name.startswith("_") and name != "__init__":
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if isinstance(member, property):
            yield f"    .{name} property"
        elif inspect.isfunction(member) and name != "__init__":
            yield f"    .{name}{_sig(member)}"


def _describe(modname: str, name: str, obj: Any) -> List[str]:
    if inspect.isclass(obj):
        if issubclass(obj, BaseException):
            bases = ", ".join(b.__name__ for b in obj.__bases__)
            return [f"{modname}.{name} exception({bases})"]
        if issubclass(obj, enum.Enum):
            return [f"{modname}.{name} enum[{', '.join(m.name for m in obj)}]"]
        lines = [f"{modname}.{name} class{_sig(obj)}"]
        if (obj.__module__ or "").startswith("locum"):
            lines.extend(_own_methods(obj))
        return lines
    if callable(obj):
        return [f"{modname}.{name} def{_sig(obj)}"]
    if inspect.ismodule(obj):
        return [f"{modname}.{name} module"]
    if name == "__version__":
        return [f"{modname}.{name} str"]
    return [f"{modname}.{name} {type(obj).__name__}"]


def snapshot() -> str:
    out: List[str] = []
    for modname in PUBLIC_MODULES + OPTIONAL_MODULES:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            if modname in OPTIONAL_MODULES:
                continue
            raise
        names = getattr(mod, "__all__", None)
        if names is None:
            raise SystemExit(f"{modname} has no __all__: every public module must declare one")
        out.append(f"# {modname} (experimental: no compatibility promise)" if modname in EXPERIMENTAL_MODULES
                   else f"# {modname}")
        for name in sorted(names):
            out.extend(_describe(modname, name, getattr(mod, name)))
        out.append("")
    return "\n".join(out)


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    now = snapshot()
    if args.write:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(now)
        print(f"wrote {SNAPSHOT}")
        return 0
    if args.check:
        with open(SNAPSHOT, encoding="utf-8") as fh:
            old = fh.read()
        if old == now:
            return 0
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(True), now.splitlines(True), "tests/data/public_api.txt", "current API"))
        return 1
    print(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
