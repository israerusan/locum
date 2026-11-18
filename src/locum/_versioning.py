"""Resolve ``locum.__version__`` (kept out of ``__init__`` so importing locum stays cheap).

``_version.py`` is written by the build (hatch-vcs) from the git tag. In a source
checkout -- an editable install (``pip install -e .``) or ``PYTHONPATH=src`` -- that
file and the editable install's metadata were frozen when the install ran: after a
``git pull`` / a new tag they still name the old version (and a ``PYTHONPATH=src``
checkout without a ``_version.py`` reports whatever *other* locum is installed). So,
in order:

1. A git checkout of locum itself (``<root>/src/locum`` with ``<root>/.git`` and
   locum's ``pyproject.toml``): ``git describe``, formatted the way hatch-vcs
   (``guess-next-dev`` / ``node-and-date``) builds it from a clean tree.
2. ``_version.py`` next to this file (the build's own record for *this* tree).
3. The installed distribution's metadata (``importlib.metadata``).
4. ``"0.0.0+unknown"``.

The result is computed once, on first access of ``locum.__version__`` (never on
``import locum``). Uncommitted changes are not marked: the version names the commit.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Dict, Optional

UNKNOWN = "0.0.0+unknown"
_DESCRIBE_RE = re.compile(r"^v?(?P<tag>\d+(?:\.\d+)*)-(?P<distance>\d+)-g(?P<node>[0-9a-f]+)$")
_NAME_RE = re.compile(r"""^\s*name\s*=\s*["']locum["']\s*$""", re.MULTILINE)


def _source_root() -> Optional[pathlib.Path]:
    """The repository root when this package runs from a git checkout of locum, else ``None``."""
    here = pathlib.Path(__file__).resolve().parent  # .../src/locum
    if here.parent.name != "src":
        return None
    root = here.parent.parent
    try:
        if not (root / ".git").exists():
            return None
        # A copy of locum vendored into another project's src/ must not report that project's tags.
        if not _NAME_RE.search((root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")):
            return None
    except OSError:
        return None
    return root


def format_describe(describe: str) -> Optional[str]:
    """``git describe --tags --long`` output -> the version hatch-vcs builds (clean tree).

    ``v0.2.0-0-g5fe7b56`` -> ``0.2.0``; ``v0.2.0-3-g1a2b3c4`` -> ``0.2.1.dev3+g1a2b3c4``.
    ``None`` for a tag that is not a plain dotted number.
    """
    m = _DESCRIBE_RE.match(describe.strip())
    if not m:
        return None
    tag, distance, node = m.group("tag"), int(m.group("distance")), m.group("node")
    if distance == 0:
        return tag
    parts = tag.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return f"{'.'.join(parts)}.dev{distance}+g{node}"


def _git_version(root: pathlib.Path) -> Optional[str]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")  # never take the index lock of a busy checkout
    kw: Dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash in GUI hosts
    try:
        out = subprocess.run(
            # --abbrev=9: the node length hatch-vcs writes into _version.py
            ["git", "describe", "--tags", "--long", "--abbrev=9", "--match", "v[0-9]*"],
            cwd=str(root), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, **kw,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0:
        return None
    return format_describe(out.stdout)


def resolve_version() -> str:
    """The version of this locum (see the module docstring for the order)."""
    root = _source_root()
    if root is not None:
        v = _git_version(root)
        if v:
            return v
    try:
        from ._version import __version__ as built  # type: ignore[import-not-found]

        if built:
            return str(built)
    except Exception:
        pass
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("locum")
    except Exception:
        return UNKNOWN
