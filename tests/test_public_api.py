"""The public API is frozen by a snapshot (docs/API.md).

If this fails you changed something public. If that was deliberate:

    python tools/api_snapshot.py --write

then commit tests/data/public_api.txt together with a CHANGELOG entry
(under "Breaking" if a name, parameter or default was removed or changed).
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    spec = importlib.util.spec_from_file_location("api_snapshot", os.path.join(ROOT, "tools", "api_snapshot.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_public_api_matches_the_snapshot():
    tool = _tool()
    with open(tool.SNAPSHOT, encoding="utf-8") as fh:
        frozen = fh.read()
    current = tool.snapshot()
    if current != frozen:
        import difflib

        diff = "".join(difflib.unified_diff(frozen.splitlines(True), current.splitlines(True),
                                            "tests/data/public_api.txt", "current API", n=1))
        raise AssertionError("The public API changed (see docs/API.md). If deliberate, run "
                             "`python tools/api_snapshot.py --write` and add a CHANGELOG entry.\n" + diff)


def test_every_public_module_declares_all():
    tool = _tool()
    for name in tool.PUBLIC_MODULES:
        mod = importlib.import_module(name)
        assert isinstance(getattr(mod, "__all__", None), (list, tuple)), name
        for attr in mod.__all__:
            assert hasattr(mod, attr), f"{name}.__all__ lists missing name {attr!r}"
