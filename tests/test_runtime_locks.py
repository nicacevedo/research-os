"""Advisory locks: mutual exclusion that survives the holder dying.

Repository mutation is serialised with PostgreSQL advisory locks rather than
with the ``flock`` the v1 layers use on run files. The reason is in
``runtime/locks.py``; what matters here is that the behaviour is what the rest
of the runtime assumes: one holder at a time, a non-blocking attempt that fails
fast rather than parking a worker slot, and release by the server when a
connection goes away.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from research_os.runtime.db import Database
from research_os.runtime.locks import (
    LockClass,
    RepositoryBusyError,
    advisory_lock,
    capsule_lock,
    held_locks,
    repository_lock,
)


def test_one_holder_at_a_time(runtime_db: Database, pg_dsn: str) -> None:
    with (
        repository_lock(runtime_db, "/repos/demo"),
        Database(pg_dsn) as other,
        pytest.raises(RepositoryBusyError, match="repository_mutation"),
        repository_lock(other, "/repos/demo", wait=False),
    ):
        pass


def test_different_repositories_do_not_contend(
    runtime_db: Database, pg_dsn: str
) -> None:
    with (
        repository_lock(runtime_db, "/repos/a"),
        Database(pg_dsn) as other,
        repository_lock(other, "/repos/b", wait=False),
    ):
        pass  # no exception is the assertion


def test_different_lock_classes_over_one_subject_do_not_contend(
    runtime_db: Database, pg_dsn: str
) -> None:
    """A capsule write and an index rebuild are different concerns."""

    with (
        capsule_lock(runtime_db, "demo-project"),
        Database(pg_dsn) as other,
        advisory_lock(
            other,
            lock_class=LockClass.DERIVED_INDEX,
            subject="demo-project",
            wait=False,
        ),
    ):
        pass


def test_the_lock_is_released_when_the_block_exits(
    runtime_db: Database, pg_dsn: str
) -> None:
    with repository_lock(runtime_db, "/repos/demo"):
        pass
    with Database(pg_dsn) as other, repository_lock(other, "/repos/demo", wait=False):
        pass


def test_a_lock_is_released_when_its_holder_is_killed(pg_dsn: str) -> None:
    """The property ``flock`` cannot offer across workers that hand work over.

    A worker killed mid-mutation has its session closed by the server, and the
    lock goes with it. Nothing in this system has to notice the death -- which
    matters, because nothing reliably can.

    A real child process and a real ``SIGKILL``: a simulated crash would run
    the cleanup code whose absence is the whole point.
    """

    script = Path(__file__).parent / "runtime_scripts" / "hold_repository_lock.py"
    holder = subprocess.Popen(
        [sys.executable, str(script), pg_dsn, "/repos/dying"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held", holder.stderr.read()
        with Database(pg_dsn) as watcher:
            assert held_locks(watcher), "the lock was not visible in pg_locks"
            with (
                pytest.raises(RepositoryBusyError),
                repository_lock(watcher, "/repos/dying", wait=False),
            ):
                pass
        holder.kill()
        holder.wait(timeout=30)
        # The backend exits when its socket closes; allow a moment for it.
        with Database(pg_dsn) as survivor:
            for _ in range(100):
                try:
                    with repository_lock(survivor, "/repos/dying", wait=False):
                        return
                except RepositoryBusyError:
                    time.sleep(0.1)
            pytest.fail("the lock outlived the process that held it")
    finally:
        if holder.poll() is None:  # pragma: no cover - only on an assertion failure
            holder.kill()
            holder.wait(timeout=30)


def test_held_locks_reports_only_this_runtimes_namespace(runtime_db: Database) -> None:
    before = len(held_locks(runtime_db))
    with repository_lock(runtime_db, "/repos/counted"):
        assert len(held_locks(runtime_db)) == before + 1
