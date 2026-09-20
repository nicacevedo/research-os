"""The durable work queue.

What this queue promises is **at-least-once delivery**, and it is worth saying
plainly because the alternative is a comfortable lie. Exactly-once delivery of a
side effect across a machine that can lose power is not available: between "the
external action succeeded" and "the database knows it succeeded" there is a
window, and no amount of queue design closes it. What closes it is making the
side effect idempotent, which is :mod:`research_os.runtime.idempotency`'s job.
This module's job is to make sure work is never *lost*, and to make sure a
worker that dies does not take its work with it.

Four decisions carry that.

**Claiming increments the attempt counter.** Not completing, not failing --
claiming. A task whose work segfaults the worker would otherwise be claimed
forever by a succession of victims, each dying before it could record anything.
Charging the attempt at claim time means a task that kills three workers is
marked ``FAILED`` with ``failure_class='worker_crash'`` instead of killing a
fourth.

**Every deadline is computed by PostgreSQL.** ``now() + make_interval(...)``,
never a Python timestamp. Two workers with skewed clocks must not disagree about
whether a lease is dead.

**A lease cannot be renewed once it has expired.** The renew statement requires
``lease_expires_at > now()``. A worker that was paused past its deadline has
already, possibly, been replaced; letting it renew would hand one work item to
two owners. It is told the lease is gone and must stop.

**Completion is guarded by ownership.** ``where lease_owner = %(owner)s``. A
worker that lost its lease cannot write a result over the work of whoever took
over. It discovers this as a returned ``None`` rather than as silence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database, jsonb
from research_os.runtime.failures import FailureClass, is_retryable, retry_delay_seconds
from research_os.runtime.ids import new_work_id
from research_os.runtime.models import WorkItem, WorkStatus

LOG = logging.getLogger("research_os.runtime.queue")

WORK_COLUMNS = (
    "work_id, run_id, project_id, kind, payload, status, priority, scheduled_at, "
    "attempts, max_attempts, lease_owner, lease_expires_at, dedup_key, "
    "failure_class, last_error, result, created_at, updated_at"
)


class QueueError(ResearchOSError):
    """Raised when a queue operation is refused or impossible."""


class LeaseLostError(QueueError):
    """Raised when a worker acts on a work item it no longer owns.

    Not a crash: an expected outcome of being slow. The worker stops touching
    the item and lets whoever holds the lease now finish it. Because side
    effects are idempotent, the new owner reuses whatever this one managed to
    do.
    """


def _row_to_item(row: dict[str, Any]) -> WorkItem:
    return WorkItem.model_validate(row)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    item: WorkItem
    created: bool


class WorkQueue:
    """Queue operations against one database."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------- producing --
    def enqueue(
        self,
        *,
        project_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        priority: int = 100,
        delay_seconds: float = 0.0,
        max_attempts: int = 3,
        dedup_key: str | None = None,
    ) -> EnqueueResult:
        """Add one work item, or return the existing one with the same dedup key.

        ``dedup_key`` is the only protection against a producer that runs twice
        -- a retried event handler, a schedule that fired during a restart. It
        is a unique index, so the race is resolved by PostgreSQL rather than by
        a check-then-insert that two processes can both pass.
        """

        work_id = new_work_id()
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into work_items
                    (work_id, run_id, project_id, kind, payload, priority,
                     scheduled_at, max_attempts, dedup_key)
                values
                    (%(work_id)s, %(run_id)s, %(project_id)s, %(kind)s, %(payload)s,
                     %(priority)s, now() + make_interval(secs => %(delay)s),
                     %(max_attempts)s, %(dedup_key)s)
                on conflict (dedup_key) do nothing
                returning {WORK_COLUMNS}
                """,
                {
                    "work_id": work_id,
                    "run_id": run_id,
                    "project_id": project_id,
                    "kind": kind,
                    "payload": jsonb(payload or {}),
                    "priority": priority,
                    "delay": float(delay_seconds),
                    "max_attempts": max_attempts,
                    "dedup_key": dedup_key,
                },
            ).fetchone()
            if row is not None:
                return EnqueueResult(item=_row_to_item(row), created=True)
            existing = conn.execute(
                f"select {WORK_COLUMNS} from work_items where dedup_key = %s",
                (dedup_key,),
            ).fetchone()
        if existing is None:  # pragma: no cover - only if the row vanished
            raise QueueError(
                f"work item with dedup key {dedup_key!r} could not be created or found"
            )
        return EnqueueResult(item=_row_to_item(existing), created=False)

    # -------------------------------------------------------------- claiming --
    def claim(
        self,
        *,
        owner: str,
        lease_seconds: int,
        limit: int = 1,
        kinds: tuple[str, ...] | None = None,
    ) -> tuple[WorkItem, ...]:
        """Claim up to ``limit`` due items, transactionally.

        ``for update skip locked`` is what makes several workers safe against
        one another: each skips rows another has locked rather than queueing
        behind them, so N workers claim N distinct items in one round trip
        instead of serialising.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                with candidate as (
                    select work_id
                    from work_items
                    where status = 'PENDING'
                      and scheduled_at <= now()
                      and (%(kinds)s::text[] is null or kind = any(%(kinds)s::text[]))
                    order by priority asc, scheduled_at asc, work_id asc
                    for update skip locked
                    limit %(limit)s
                )
                update work_items w
                set status = 'LEASED',
                    lease_owner = %(owner)s,
                    lease_expires_at = now() + make_interval(secs => %(lease)s),
                    attempts = w.attempts + 1,
                    updated_at = now()
                from candidate c
                where w.work_id = c.work_id
                returning {", ".join("w." + c.strip() for c in WORK_COLUMNS.split(","))}
                """,
                {
                    "owner": owner,
                    "lease": float(lease_seconds),
                    "limit": limit,
                    "kinds": list(kinds) if kinds else None,
                },
            ).fetchall()
        # **Sorted here, because `returning` is not ordered.**
        #
        # The CTE's `order by` picks the right *set* of rows -- the N most
        # urgent -- and that is all it does. `UPDATE ... RETURNING` emits rows
        # in whatever order the update visited them, which PostgreSQL does not
        # specify and which in practice follows the heap. So a claim of two
        # items came back in priority order most of the time and in physical
        # order the rest, and a worker taking `limit > 1` processed the
        # cheapest item first whenever the layout happened to differ.
        #
        # Invisible for a long time because the default `limit` is 1 and
        # because a freshly truncated table lays rows down in insertion order.
        # It surfaced in a full-suite run after unrelated tests changed how
        # much churn the shared session database had seen -- which is the
        # ordinary way a latent ordering assumption announces itself.
        items = [_row_to_item(row) for row in rows]
        items.sort(key=lambda item: (item.priority, item.scheduled_at, item.work_id))
        return tuple(items)

    def renew(self, work_id: str, *, owner: str, lease_seconds: int) -> None:
        """Extend the lease, or raise :class:`LeaseLostError`.

        Refuses to renew an expired lease. Once the deadline passes the item may
        already belong to someone else, and a renewal here would be this
        worker declaring itself the owner of work it no longer owns.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                update work_items
                set lease_expires_at = now() + make_interval(secs => %(lease)s),
                    updated_at = now()
                where work_id = %(work_id)s
                  and status = 'LEASED'
                  and lease_owner = %(owner)s
                  and lease_expires_at > now()
                returning work_id
                """,
                {"work_id": work_id, "owner": owner, "lease": float(lease_seconds)},
            ).fetchone()
        if row is None:
            raise LeaseLostError(
                f"lease on {work_id} is no longer held by {owner}; stopping work on it"
            )

    # ------------------------------------------------------------ finishing --
    def succeed(
        self, work_id: str, *, owner: str, result: dict[str, Any] | None = None
    ) -> WorkItem:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update work_items
                set status = 'SUCCEEDED', result = %(result)s, lease_owner = null,
                    lease_expires_at = null, failure_class = null, last_error = null,
                    updated_at = now()
                where work_id = %(work_id)s and status = 'LEASED' and lease_owner = %(owner)s
                returning {WORK_COLUMNS}
                """,
                {"work_id": work_id, "owner": owner, "result": jsonb(result or {})},
            ).fetchone()
        if row is None:
            raise LeaseLostError(f"cannot complete {work_id}: not held by {owner}")
        return _row_to_item(row)

    def fail(
        self,
        work_id: str,
        *,
        owner: str,
        failure_class: FailureClass,
        error: str,
        force_terminal: bool = False,
        not_before: datetime | None = None,
    ) -> WorkItem:
        """Record a failure and apply the retry policy for its class.

        The policy lives in :mod:`research_os.runtime.failures` and is applied
        here so that "should this be retried" is answered once, from the class
        of the failure, rather than by each worker's opinion. A scientifically
        negative result never reaches this method at all -- it is a success.

        ``not_before`` is a floor under the next attempt, supplied by a caller
        that knows when the blocking condition lifts -- in practice a provider
        breaker's ``cooldown_until``. The backoff still applies; the schedule
        is the later of the two.

        Without it the retry schedule and the breaker were two independent
        clocks, and on 2026-09-19 they disagreed by three and a half minutes.
        A provider failure opened a 300-second cooldown; the item's three
        attempts were spent 30 and 60 seconds apart, every one of them inside
        a window where routing was guaranteed to refuse. The item was
        exhausted before the provider was well, and a transient failure became
        permanent by arithmetic.

        Note that the floor is applied *without* extending the attempt budget.
        Raising ``max_attempts`` until the numbers happened to overlap would
        have hidden the same defect behind a different set of magic numbers,
        and would break again the first time either setting changed.
        """

        retryable = (not force_terminal) and is_retryable(failure_class)
        delay = retry_delay_seconds(failure_class, attempt=1)
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update work_items
                set status = case
                        when %(retryable)s and attempts < max_attempts then 'PENDING'
                        else 'FAILED' end,
                    scheduled_at = case
                        when %(retryable)s and attempts < max_attempts
                        then greatest(
                            now() + make_interval(
                                secs => %(delay)s * greatest(attempts, 1)),
                            coalesce(%(not_before)s, to_timestamp(0)))
                        else scheduled_at end,
                    lease_owner = null,
                    lease_expires_at = null,
                    failure_class = %(failure_class)s,
                    last_error = %(error)s,
                    updated_at = now()
                where work_id = %(work_id)s and status = 'LEASED' and lease_owner = %(owner)s
                returning {WORK_COLUMNS}
                """,
                {
                    "work_id": work_id,
                    "owner": owner,
                    "retryable": retryable,
                    "delay": float(delay),
                    "not_before": not_before,
                    "failure_class": str(failure_class),
                    "error": error[:4000],
                },
            ).fetchone()
        if row is None:
            raise LeaseLostError(f"cannot fail {work_id}: not held by {owner}")
        return _row_to_item(row)

    def wait_for_external(
        self,
        work_id: str,
        *,
        owner: str,
        detail: str,
        retry_after_seconds: float,
        max_parks: int = 240,
        failure_class: FailureClass | None = None,
        not_before: datetime | None = None,
    ) -> WorkItem:
        """Park an item until an external dependency is expected to have moved.

        Distinct from failing: nothing went wrong, and the attempt is refunded
        so that waiting for a cluster does not consume the retry budget that
        exists for things that break.

        ``not_before`` is a known deadline for the thing being waited on -- a
        provider breaker's ``cooldown_until``. Supplied as a timestamp rather
        than converted to a delay by the caller, because the caller's clock is
        not the database's: this module's rule is that every deadline is
        computed by PostgreSQL, and the first version of the provider-cooldown
        path broke it by subtracting a ``Clock.now()`` from a database
        timestamp. Under the frozen clock the tests inject, that arithmetic
        produced a nine-month deferral which the assertions -- written as
        "at or after the cooldown" -- accepted.

        Bounded, though. Refunding the attempt means ``attempts`` never
        approaches ``max_attempts``, so an item that always parks would be
        re-claimed forever -- an unbounded loop that invariant 14 forbids, and
        which an independent review found before any caller existed to trigger
        it. Parks are counted separately in the payload, and past ``max_parks``
        the item fails as ``SCHEDULER_UNAVAILABLE`` so the wait ends in a named
        state rather than in silence. At the default 60-second poll interval,
        240 parks is four hours.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update work_items
                set status = case
                        when coalesce((payload ->> 'parks')::int, 0) + 1 > %(max_parks)s
                        then 'FAILED' else 'PENDING' end,
                    scheduled_at = greatest(
                        now() + make_interval(secs => %(delay)s),
                        coalesce(%(not_before)s, to_timestamp(0))),
                    attempts = greatest(attempts - 1, 0),
                    payload = jsonb_set(
                        payload, '{{parks}}',
                        to_jsonb(coalesce((payload ->> 'parks')::int, 0) + 1)
                    ),
                    failure_class = case
                        when coalesce((payload ->> 'parks')::int, 0) + 1 > %(max_parks)s
                        then %(exhausted_class)s else %(waiting_class)s end,
                    lease_owner = null,
                    lease_expires_at = null,
                    last_error = %(detail)s,
                    updated_at = now()
                where work_id = %(work_id)s and status = 'LEASED' and lease_owner = %(owner)s
                returning {WORK_COLUMNS}
                """,
                {
                    "work_id": work_id,
                    "owner": owner,
                    "delay": float(retry_after_seconds),
                    "not_before": not_before,
                    "detail": detail[:4000],
                    "max_parks": max_parks,
                    # What it is waiting for, kept on the row while it waits.
                    # This used to be nulled on every park, so `runtime status`
                    # showed an item with a reason in `last_error` and no class
                    # beside it -- readable as "something went wrong and we do
                    # not know what". A parked item knows exactly what it is
                    # waiting for.
                    "waiting_class": (
                        str(failure_class) if failure_class is not None else None
                    ),
                    "exhausted_class": str(
                        failure_class or FailureClass.SCHEDULER_UNAVAILABLE
                    ),
                },
            ).fetchone()
        if row is None:
            raise LeaseLostError(f"cannot park {work_id}: not held by {owner}")
        return _row_to_item(row)

    # ------------------------------------------------------------ recovering --
    def reclaim_expired(self, *, limit: int = 100) -> tuple[WorkItem, ...]:
        """Return expired leases to the queue, or fail them if out of attempts.

        This is the entire crash-recovery story for workers. A worker that is
        killed, loses power, or is disconnected stops renewing; its deadline
        passes; this puts the item back. Nothing needs to detect that the
        process died, which is good, because nothing reliably can.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                with expired as (
                    select work_id from work_items
                    where status = 'LEASED' and lease_expires_at <= now()
                    order by lease_expires_at
                    for update skip locked
                    limit %(limit)s
                )
                update work_items w
                set status = case when w.attempts >= w.max_attempts then 'FAILED' else 'PENDING' end,
                    lease_owner = null,
                    lease_expires_at = null,
                    failure_class = case
                        when w.attempts >= w.max_attempts then %(failure_class)s
                        else w.failure_class end,
                    -- Only overwrite the message when this *is* the diagnosis.
                    -- Requeueing used to replace a diagnosed failure's text
                    -- with "lease expired" while keeping its class, which reads
                    -- as a contradiction in `runtime status`.
                    last_error = case
                        when w.attempts >= w.max_attempts or w.failure_class is null
                        then %(error)s else w.last_error end,
                    updated_at = now()
                from expired e
                where w.work_id = e.work_id
                returning {", ".join("w." + c.strip() for c in WORK_COLUMNS.split(","))}
                """,
                {
                    "limit": limit,
                    "failure_class": str(FailureClass.WORKER_CRASH),
                    "error": "lease expired; the worker holding this item stopped reporting",
                },
            ).fetchall()
        if rows:
            LOG.warning("reclaimed %d expired lease(s)", len(rows))
        return tuple(_row_to_item(row) for row in rows)

    # -------------------------------------------------------------- reading --
    def get(self, work_id: str) -> WorkItem | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {WORK_COLUMNS} from work_items where work_id = %s", (work_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def list_for_run(self, run_id: str, *, limit: int = 500) -> tuple[WorkItem, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {WORK_COLUMNS} from work_items where run_id = %s "
                "order by created_at, work_id limit %s",
                (run_id, limit),
            ).fetchall()
        return tuple(_row_to_item(row) for row in rows)

    def counts_by_status(self, *, run_id: str | None = None) -> dict[WorkStatus, int]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "select status, count(*) as n from work_items "
                "where (%(run_id)s::text is null or run_id = %(run_id)s) group by status",
                {"run_id": run_id},
            ).fetchall()
        return {WorkStatus(row["status"]): int(row["n"]) for row in rows}

    def cancel_run_work(self, run_id: str) -> int:
        """Cancel every not-yet-finished item of one run.

        Leased items are cancelled too. Their workers will discover it when they
        try to complete and are told they no longer hold the lease -- which is
        exactly the behaviour a cancel needs, because the alternative is waiting
        for a task that may be an hour long.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                update work_items
                set status = 'CANCELLED', lease_owner = null, lease_expires_at = null,
                    updated_at = now()
                where run_id = %s and status in ('PENDING','LEASED','WAITING','BLOCKED')
                returning work_id
                """,
                (run_id,),
            ).fetchall()
        return len(row)
