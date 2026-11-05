"""Worker entry points for the multi-process WorkQueue tests.

They live in their own module (not a ``test_*`` file) so ``spawn`` children can
import them by name without collecting or re-running any test.
"""

from __future__ import annotations

import os
import time


def drain(path: str, worker_name: str, log_dir: str, start, lease_duration: float = 60.0) -> None:
    """Process the queue at ``path`` until it is empty, logging each handler run to ``<log_dir>/<worker>.log``."""
    from pypilot.rpa.queue import WorkQueue

    start.wait(60)
    q = WorkQueue.open(path, worker_id=worker_name, lease_duration=lease_duration)
    with open(os.path.join(log_dir, worker_name + ".log"), "a", encoding="utf-8") as log:

        def handler(item):
            log.write(item.id + "\n")
            log.flush()
            time.sleep(0.001)
            return {"by": worker_name}

        q.process(handler, retries=0)


def claim_one(path: str, out_path: str, lease_duration: float) -> None:
    """Claim one item and exit without finishing it (a worker that crashes mid-transaction)."""
    from pypilot.rpa.queue import WorkQueue

    q = WorkQueue.open(path, worker_id="crasher", lease_duration=lease_duration)
    item = q.next()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(item.id if item is not None else "")
