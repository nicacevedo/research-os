"""LangGraph checkpoint persistence, and the retention that keeps it bounded.

Checkpoints are what make a bounded cycle resumable across process death, and
they are also the part of this system most likely to grow without anyone
noticing. Three decisions keep that from happening.

**One thread is one bounded cycle.** Not one per project and certainly not one
per objective. A thread that lives as long as a project accumulates every
superstep the project ever took, and resuming it means deserialising all of it.
``ids.thread_id_for(run_id)`` is derived from the run, and a run ends.

**State carries references, never bytes.** Enforced by
``tests/test_runtime_graph.py``, which measures a real checkpoint's size. A
checkpoint holding a PDF is a checkpoint table that grows by megabytes per
literature fetch.

**Retention is implemented, not aspirational.** `PostgresSaver.delete_thread`
exists on the pinned version, and :func:`prune` uses it for threads whose runs
finished long enough ago. Without this the table is append-only forever, and
"we will clean it up later" becomes a table nobody dares touch.

**Durability is `sync`.** LangGraph offers `sync`, `async` and `exit`. The
default is cheaper and would lose the most recent superstep on a hard kill,
which is the exact scenario the checkpointing exists for, so this runtime always
passes ``durability="sync"``. Slower per node; correct when the power goes out.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.checkpoint.postgres import PostgresSaver

LOG = logging.getLogger("research_os.runtime.checkpoints")

#: Passed to every ``invoke``. See the module docstring: the cheaper modes lose
#: the superstep that a hard kill happens during, which is the one that matters.
DURABILITY = "sync"

#: Checkpoint tables LangGraph owns. Listed so `researchctl runtime status` can
#: report their size without importing LangGraph.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


class CheckpointError(ResearchOSError):
    """Raised when checkpoint storage cannot be prepared or pruned."""


def _saver_class() -> type[PostgresSaver]:
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ModuleNotFoundError as exc:  # pragma: no cover - install-time path
        raise CheckpointError(
            "Bounded resumable cycles need LangGraph. Install the runtime extra: "
            "`uv sync --extra runtime`."
        ) from exc
    return PostgresSaver


@contextmanager
def checkpointer(dsn: str, *, setup: bool = False) -> Iterator[PostgresSaver]:
    """Open a checkpointer for the duration of one graph execution.

    Scoped to the execution rather than held for the daemon's lifetime, because
    the saver holds one autocommit connection and a worker that is between work
    items should not be holding one. ``setup`` creates LangGraph's own tables and
    is idempotent; it is called once at startup rather than per execution, since
    it issues DDL.
    """

    saver_class = _saver_class()
    with saver_class.from_conn_string(dsn) as saver:
        if setup:
            saver.setup()
        yield saver


def ensure_tables(dsn: str) -> None:
    """Create LangGraph's checkpoint tables if they are absent.

    Separate from this runtime's own migrations on purpose: those tables belong
    to LangGraph, their shape is its business, and putting its DDL in our
    migration files would make an upgrade of the library a schema conflict.
    """

    with checkpointer(dsn, setup=True):
        LOG.debug("langgraph checkpoint tables are present")


def thread_ids(db: Database) -> tuple[str, ...]:
    with db.tx() as conn:
        rows = conn.execute(
            "select distinct thread_id from checkpoints order by thread_id"
        ).fetchall()
    return tuple(str(row["thread_id"]) for row in rows)


def prune(db: Database, dsn: str, *, retention_days: int) -> tuple[str, ...]:
    """Delete checkpoints for cycles that finished more than ``retention_days`` ago.

    Driven from ``research_runs`` rather than from the checkpoint tables' own
    timestamps: the question is "is this cycle over and old", and this runtime
    is the thing that knows. A thread with no matching run row is left alone --
    it was not created by this runtime and deleting other people's data on a
    guess is not tidying.
    """

    with db.tx() as conn:
        rows = conn.execute(
            """
            select thread_id from research_runs
            where thread_id is not null
              and status in ('SUCCEEDED','FAILED','CANCELLED')
              and finished_at is not null
              and finished_at < now() - make_interval(days => %(days)s)
            """,
            {"days": retention_days},
        ).fetchall()
    stale = [str(row["thread_id"]) for row in rows]
    if not stale:
        return ()
    live = set(thread_ids(db))
    deleted: list[str] = []
    with checkpointer(dsn) as saver:
        for thread in stale:
            if thread not in live:
                continue
            saver.delete_thread(thread)
            deleted.append(thread)
    if deleted:
        LOG.info("pruned checkpoints for %d finished cycle(s)", len(deleted))
    return tuple(deleted)


def checkpoint_sizes(db: Database) -> dict[str, int]:
    """On-disk bytes per checkpoint table, for the status command.

    Unbounded checkpoint growth is the failure this runtime is most likely to
    develop quietly, so it is a number a person can look at rather than
    something to discover from a full disk.
    """

    sizes: dict[str, int] = {}
    with db.tx() as conn:
        for table in CHECKPOINT_TABLES:
            row = conn.execute(
                "select coalesce(pg_total_relation_size(to_regclass(%s)), 0) as bytes",
                (f"public.{table}",),
            ).fetchone()
            sizes[table] = int(row["bytes"]) if row else 0
    return sizes


def state_size_bytes(state: Any) -> int:
    """How large one graph state is, serialised the way LangGraph serialises it.

    Used by the test that asserts state holds references rather than bytes. Kept
    here so the test measures what the checkpointer would actually write rather
    than what ``json.dumps`` would.
    """

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    _kind, blob = JsonPlusSerializer().dumps_typed(state)
    return len(blob)
