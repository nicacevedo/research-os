"""Take the repository mutation lock, announce it, and block until killed.

Used by ``tests/test_runtime_locks.py`` to assert the property that motivated
using advisory locks rather than ``flock``: the server releases the lock when
the holder's connection goes away, so a worker killed mid-mutation does not
leave a repository permanently locked.
"""

from __future__ import annotations

import sys
import time

from research_os.runtime.db import Database
from research_os.runtime.locks import repository_lock

dsn, subject = sys.argv[1], sys.argv[2]

with Database(dsn) as db, repository_lock(db, subject):
    print("held", flush=True)
    while True:
        time.sleep(3600)
