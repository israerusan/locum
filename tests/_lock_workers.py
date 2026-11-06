"""Child-process entry points for the desktop input lock tests (run with ``python -c``)."""

from __future__ import annotations

import os
import sys
import time


class _Named:
    """Stands in for a backend that drives a real desktop: it opts into a named lock."""

    def __init__(self, name: str) -> None:
        self.desktop_lock_name = name


def hold_lock(name: str, ready_path: str, seconds: float, abandon: bool = False) -> None:
    from locum import _inputlock

    lock = _inputlock._lock_for(_Named(name))
    if not lock.acquire(10.0):
        sys.exit(3)
    with open(ready_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    time.sleep(seconds)
    if abandon:
        os._exit(0)  # die holding it: the OS must hand it to the next waiter
    lock.release()


if __name__ == "__main__":  # pragma: no cover - driven by the tests
    hold_lock(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1")
