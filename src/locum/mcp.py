"""``python -m locum.mcp`` / ``locum-mcp``: the MCP server entry point.

The same server as ``locum mcp`` (all of its options work here too)::

    python -m locum.mcp --policy default
    locum-mcp --policy default --audit ~/.locum/mcp-audit.jsonl

The server itself lives in :mod:`locum.llm.mcp_server`.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence


#: ``locum`` options that come before the command (``locum --backend mock mcp``),
#: with whether they take a value. Here they may be given anywhere.
_GLOBAL_OPTIONS = {"--backend": True, "--timeout": True, "--pause": True, "--kill-combo": True,
                   "--humanize": False, "--no-failsafe": False, "--kill-hotkey": False,
                   "--json": False, "--debug": False, "--version": False}


def _split(args: Sequence[str]) -> "tuple[list[str], list[str]]":
    glob: list = []
    rest: list = []
    it = iter(args)
    for a in it:
        name = a.split("=", 1)[0]
        if name in _GLOBAL_OPTIONS:
            glob.append(a)
            if _GLOBAL_OPTIONS[name] and "=" not in a:
                nxt = next(it, None)
                if nxt is not None:
                    glob.append(nxt)
        else:
            rest.append(a)
    return glob, rest


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run ``locum mcp`` with ``argv`` (default: ``sys.argv[1:]``); returns the exit code."""
    from .cli import main as cli_main

    glob, rest = _split(list(sys.argv[1:] if argv is None else argv))
    return cli_main([*glob, "mcp", *rest])


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    sys.exit(main())
