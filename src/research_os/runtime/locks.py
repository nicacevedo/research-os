"""Serialising the operations that can mutate one repository.

The v1 layers already lock a *run* with ``flock`` on a file under the state home
(:mod:`research_os.runlock`), and that is the right mechanism for what it
protects: one JSON file replaced whole by one process on one machine. It is the
wrong mechanism for this, for two reasons. A worker holding a repository lock may
not be the process that will release it -- work is claimed, leased and possibly
taken over -- and ``flock`` releases on process exit, which is precisely when
this lock most needs to be reasoned about rather than silently dropped.

PostgreSQL advisory locks fit better. They are held by a *session*, the server
decides who has one, and a worker that dies has its session closed by the
server, which releases the lock without anything needing to detect the death.

Two scopes, and the distinction matters for throughput:

**A repository mutation lock.** Anything that changes the canonical checkout --
adding a worktree, committing, integrating a candidate branch, writing a capsule
file -- takes it. Held for the duration of the mutation and no longer.

**No lock at all for reading.** Literature search, repository inspection,
diagnostics, hypothesis generation, review of a frozen packet: none of these
change the repository, so none of them queue behind one that does. This is
where the runtime gets its parallelism, and it is safe because the things that
do not take the lock genuinely cannot corrupt anything.

Advisory locks are taken with a *session*-scoped ``pg_advisory_lock``, which
means a dedicated connection is held for the lifetime of the ``with`` block.
That is the one place this runtime holds a connection across work, and it is
acceptable because the alternative -- a transaction-scoped lock -- would require
holding a transaction open instead, which is worse.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import IntEnum

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database
from research_os.runtime.migrations import ADVISORY_NAMESPACE

LOG = logging.getLogger("research_os.runtime.locks")


class LockClass(IntEnum):
    """The distinct kinds of advisory lock, so two purposes cannot collide.

    Mixed into the key alongside the hashed subject, because the subject alone
    is a hash and two different kinds of subject could in principle hash alike.
    """

    REPOSITORY_MUTATION = 1
    PROJECT_CAPSULE = 2
    DERIVED_INDEX = 3
    #: One cycle at a time per run. Two workers entering one LangGraph thread
    #: duplicates model spend and writes concurrently to one checkpointed
    #: thread; nothing else in the design prevented it, because the run's
    #: status transition is not a guard (RUNNING -> RUNNING succeeds).
    RESEARCH_RUN = 4


class RepositoryBusyError(ResearchOSError):
    """Raised when another worker is already mutating this repository."""


def _key(lock_class: LockClass, subject: str) -> int:
    """Map a subject onto the 32-bit second half of a two-int advisory key.

    The top bit is cleared so the value is always a positive signed int, which
    is what PostgreSQL's two-argument form takes.
    """

    digest = hashlib.sha256(f"{int(lock_class)}:{subject}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


@contextmanager
def advisory_lock(
    db: Database,
    *,
    lock_class: LockClass,
    subject: str,
    wait: bool = False,
) -> Iterator[None]:
    """Hold one advisory lock for the body of the block.

    ``wait=False`` is the default, and it was not at first. Every caller took
    the blocking form, which meant ``pg_advisory_lock`` with no
    ``lock_timeout`` -- an unbounded wait. Because ``Daemon.tick`` runs work
    inline on one thread, a single stuck holder stopped lease reclamation,
    approval notification and job polling for that whole process, while the
    blocked worker's own lease keeper kept its lease alive so nothing recovered
    it either. Every ``RepositoryBusyError`` handler in the tree was
    unreachable.

    So the default refuses fast. A worker that cannot get the lock puts its work
    item back and picks up something else, which is what a worker should do.
    ``wait=True`` remains available for a caller that genuinely wants to queue,
    and there is currently none.

    Releasing is best-effort and cannot mask the body's exception: an unlock
    that itself fails is logged, because raising from the ``finally`` would hide
    whatever actually went wrong inside the block.
    """

    key = _key(lock_class, subject)
    with db.autocommit() as conn:
        if wait:
            conn.execute("select pg_advisory_lock(%s, %s)", (ADVISORY_NAMESPACE, key))
            acquired = True
        else:
            row = conn.execute(
                "select pg_try_advisory_lock(%s, %s) as got", (ADVISORY_NAMESPACE, key)
            ).fetchone()
            acquired = bool(row and row["got"])
        if not acquired:
            raise RepositoryBusyError(
                f"another worker is holding the {lock_class.name.lower()} lock for {subject}"
            )
        LOG.debug("acquired %s lock for %s", lock_class.name, subject)
        try:
            yield
        finally:
            try:
                conn.execute(
                    "select pg_advisory_unlock(%s, %s)", (ADVISORY_NAMESPACE, key)
                )
            except Exception as exc:  # noqa: BLE001 - must not mask the body's error
                LOG.error(
                    "could not release the %s lock for %s: %s",
                    lock_class.name,
                    subject,
                    exc,
                )
            else:
                LOG.debug("released %s lock for %s", lock_class.name, subject)


def repository_lock(db: Database, repo_path: str, *, wait: bool = False):
    """Serialise mutations of one canonical checkout."""

    return advisory_lock(
        db, lock_class=LockClass.REPOSITORY_MUTATION, subject=repo_path, wait=wait
    )


def capsule_lock(db: Database, project_id: str, *, wait: bool = False):
    """Serialise writes to one project's capsule."""

    return advisory_lock(
        db, lock_class=LockClass.PROJECT_CAPSULE, subject=project_id, wait=wait
    )


def derived_index_lock(db: Database, name: str, *, wait: bool = False):
    """Serialise rebuilds of one derived, rebuildable index."""

    return advisory_lock(
        db, lock_class=LockClass.DERIVED_INDEX, subject=name, wait=wait
    )


def research_run_lock(db: Database, run_id: str, *, wait: bool = False):
    """Serialise execution of one bounded cycle.

    Two workers entering one LangGraph thread duplicates model spend and writes
    concurrently to one checkpoint stream. Nothing else prevented it: the run's
    status transition is not a guard, because RUNNING -> RUNNING succeeds.
    """

    return advisory_lock(
        db, lock_class=LockClass.RESEARCH_RUN, subject=run_id, wait=wait
    )


def held_locks(db: Database) -> tuple[tuple[int, int], ...]:
    """Every advisory lock currently held in this runtime's namespace.

    For ``researchctl runtime status``: a lock nobody can explain is a much
    easier thing to debug when it can be listed.
    """

    with db.tx() as conn:
        rows = conn.execute(
            "select classid, objid from pg_locks "
            "where locktype = 'advisory' and classid = %s and granted",
            (ADVISORY_NAMESPACE,),
        ).fetchall()
    return tuple((int(row["classid"]), int(row["objid"])) for row in rows)
