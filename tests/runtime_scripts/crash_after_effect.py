"""Perform an idempotent side effect, then die before recording it.

Run as a subprocess by ``tests/test_runtime_idempotency.py``. The process is
killed with ``os._exit`` *after* the side effect and *before* the ledger is
completed, which is the window no queue design can close and the reason the
ledger exists. ``os._exit`` skips every finally block, atexit hook and buffer
flush, which is what a real SIGKILL or power loss does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from research_os.runtime.db import Database
from research_os.runtime.idempotency import InvocationLedger, idempotency_key

dsn, effects_path, work_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]


def perform() -> dict[str, object]:
    with effects_path.open("a", encoding="utf-8") as handle:
        handle.write("submitted\n")
        handle.flush()
        os.fsync(handle.fileno())
    # The external world has now changed. Nothing has recorded that it did.
    os._exit(9)


with Database(dsn) as db:
    InvocationLedger(db).run(
        key=idempotency_key("demo.submit", work_id),
        kind="demo.submit",
        work_id=work_id,
        perform=perform,
        owner="doomed-worker",
    )
