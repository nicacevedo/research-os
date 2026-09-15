"""Durable invocation tracking: how a retried side effect stops being a second one.

The failure this exists for, exactly, and reproduced in
``tests/test_runtime_idempotency.py``:

1. the runtime submits a Slurm job -- or makes a commit, or creates a worktree,
   or downloads a paper;
2. the action succeeds;
3. the worker dies before it can write down that it succeeded;
4. the lease expires and the work is retried;
5. and the naive retry submits the job a second time.

Nothing about queue design prevents this. Between "the external world changed"
and "our database knows it changed" there is a window, and a process can die in
it. What prevents it is recording the *intent* before acting, keyed by something
derived from the task rather than from the attempt, and looking for that record
before acting again.

So every side effect goes through :meth:`InvocationLedger.run`:

    claim(key) -> IN_FLIGHT row, or the existing row
        if COMPLETED: return its stored result, perform nothing
        if IN_FLIGHT and ours: perform, then complete
        if IN_FLIGHT and abandoned: hand to the caller's reconciler

**The key must not contain the attempt number.** That is the whole trick, and
getting it wrong -- keying on something that differs per attempt -- produces a
ledger that records every duplicate faithfully while preventing none of them.
:func:`idempotency_key` takes the stable identity of the *action*.

**An abandoned in-flight row is not the same as a missing one.** It means the
side effect may or may not have happened and the only honest answer is to go and
look. Callers supply a ``reconcile`` callback for actions where looking is
possible (``squeue`` knows whether a job exists; ``git log`` knows whether a
commit was made). Where it is not possible, the action is refused and escalated,
because guessing is how a cluster ends up running the same experiment twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database, jsonb
from research_os.runtime.ids import new_invocation_id
from research_os.runtime.models import InvocationStatus, ToolInvocation

LOG = logging.getLogger("research_os.runtime.idempotency")

INVOCATION_COLUMNS = (
    "invocation_id, idempotency_key, run_id, work_id, kind, request, status, result, "
    "error, attempts, owner, started_at, finished_at"
)


class IdempotencyError(ResearchOSError):
    """Raised when a side effect cannot be safely performed or reused."""


class InvocationTakenOverError(IdempotencyError):
    """Raised when a worker acts on an invocation it no longer owns.

    Not a crash: the expected outcome of being slow enough to be abandoned. The
    late worker's result is discarded, and because the action was idempotent
    whoever took over either reused it or established the truth by reconciling.
    """


class UnreconciledInvocationError(IdempotencyError):
    """Raised when a previous attempt's outcome is unknown and cannot be checked.

    Deliberately terminal. The runtime does not know whether the action
    happened, cannot find out, and must not perform it again on the chance that
    it did not.
    """


def worker_identity() -> str:
    """A stable-enough name for whoever is holding things.

    Host and pid. Not globally unique across a reboot that reuses a pid, which
    is fine: identity here is used to attribute and to debug, never as the sole
    basis for safety. Safety comes from the database's uniqueness constraints.
    """

    return f"{socket.gethostname()}/{os.getpid()}"


def idempotency_key(kind: str, *parts: object) -> str:
    """Derive a stable key for one action from its identity.

    Every part must be something that is the same on every attempt of the same
    logical action: the work item id, the target path, the spec digest, the base
    commit. Never the attempt number, never a timestamp, never a fresh uuid --
    each of those turns this into an audit log that prevents nothing.
    """

    payload = json.dumps([kind, *[str(p) for p in parts]], separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of an idempotent action.

    ``reused`` is ``True`` when a previous attempt had already done it, which is
    exactly the signal the crash-safety tests assert on.
    """

    result: dict[str, Any]
    invocation: ToolInvocation
    reused: bool


class InvocationLedger:
    """The durable record of side effects, and the gate in front of them."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> ToolInvocation | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {INVOCATION_COLUMNS} from tool_invocations where idempotency_key = %s",
                (key,),
            ).fetchone()
        return ToolInvocation.model_validate(row) if row else None

    def _claim(
        self,
        *,
        key: str,
        kind: str,
        request: dict[str, Any],
        run_id: str | None,
        work_id: str | None,
        owner: str,
    ) -> tuple[ToolInvocation, bool]:
        """Insert an IN_FLIGHT row, or return the existing one.

        Returns ``(invocation, claimed_by_us)``. The unique index on
        ``idempotency_key`` decides the race; two workers cannot both be told
        they claimed it.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into tool_invocations
                    (invocation_id, idempotency_key, run_id, work_id, kind, request, owner)
                values (%(invocation_id)s, %(key)s, %(run_id)s, %(work_id)s, %(kind)s,
                        %(request)s, %(owner)s)
                on conflict (idempotency_key) do nothing
                returning {INVOCATION_COLUMNS}
                """,
                {
                    "invocation_id": new_invocation_id(),
                    "key": key,
                    "run_id": run_id,
                    "work_id": work_id,
                    "kind": kind,
                    "request": jsonb(request),
                    "owner": owner,
                },
            ).fetchone()
            if row is not None:
                return ToolInvocation.model_validate(row), True
            existing = conn.execute(
                f"select {INVOCATION_COLUMNS} from tool_invocations where idempotency_key = %s",
                (key,),
            ).fetchone()
        if existing is None:  # pragma: no cover
            raise IdempotencyError(f"invocation {key!r} could not be claimed or found")
        return ToolInvocation.model_validate(existing), False

    def _retake(self, invocation_id: str, *, owner: str) -> bool:
        """Take over an abandoned row whose outcome has just been established.

        Only reached after a reconciler has looked at the world and reported
        that the side effect did *not* happen. The row goes back to
        ``IN_FLIGHT`` under the new owner, so a third worker arriving now is
        told the action is in flight rather than performing it as well.

        Guarded on ``status = 'ABANDONED'`` so that exactly one of several
        concurrent reconcilers wins the row: the losers find the status already
        moved and re-read it.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                update tool_invocations
                set owner = %(owner)s,
                    attempts = attempts + 1,
                    status = 'IN_FLIGHT',
                    error = null,
                    finished_at = null
                where invocation_id = %(invocation_id)s and status = 'ABANDONED'
                returning invocation_id
                """,
                {"invocation_id": invocation_id, "owner": owner},
            ).fetchone()
        return row is not None

    def complete(
        self, invocation_id: str, *, result: dict[str, Any], owner: str | None = None
    ) -> ToolInvocation:
        """Record that the side effect took hold.

        Guarded on ownership when an owner is given, exactly as
        :meth:`WorkQueue.succeed` is. Without the guard, a worker whose
        invocation had been abandoned and taken over could return late and
        overwrite the new owner's record -- and if it landed ``mark_failed``
        last, the row would read FAILED for an action that had in fact
        completed, which the retry path would then read as permission to do it
        again.

        ``owner=None`` is the *reconciliation* case and is deliberately
        unguarded: a reconciler has looked at the world and established that the
        action took hold, which is authoritative over any worker's opinion and
        over any earlier status. Hence the status set includes ABANDONED and
        FAILED -- the two states a reconciler exists to correct.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update tool_invocations
                set status = 'COMPLETED', result = %(result)s, error = null, finished_at = now()
                where invocation_id = %(invocation_id)s
                  and status <> 'COMPLETED'
                  and (%(owner)s::text is null or
                       (status = 'IN_FLIGHT' and owner = %(owner)s))
                returning {INVOCATION_COLUMNS}
                """,
                {
                    "invocation_id": invocation_id,
                    "result": jsonb(result),
                    "owner": owner,
                },
            ).fetchone()
        if row is None:
            current = self.get_by_id(invocation_id)
            if current is None:
                raise IdempotencyError(f"no such invocation: {invocation_id}")
            raise InvocationTakenOverError(
                f"invocation {invocation_id} is {current.status} and owned by "
                f"{current.owner}; this worker's result is discarded"
            )
        return ToolInvocation.model_validate(row)

    def mark_failed(
        self, invocation_id: str, *, error: str, owner: str | None = None
    ) -> ToolInvocation:
        """Record that the action definitely did not happen.

        Distinct from abandoning. A failure recorded here means the side effect
        was attempted and did not take effect, so a retry is safe. The row is
        deleted rather than kept as FAILED when the action is safe to retry
        under the same key -- see :meth:`run`.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update tool_invocations
                set status = 'FAILED', error = %(error)s, finished_at = now()
                where invocation_id = %(invocation_id)s
                  and status = 'IN_FLIGHT'
                  and (%(owner)s::text is null or owner = %(owner)s)
                returning {INVOCATION_COLUMNS}
                """,
                {"invocation_id": invocation_id, "error": error[:4000], "owner": owner},
            ).fetchone()
        if row is None:
            current = self.get_by_id(invocation_id)
            if current is None:
                raise IdempotencyError(f"no such invocation: {invocation_id}")
            raise InvocationTakenOverError(
                f"invocation {invocation_id} is {current.status} and owned by "
                f"{current.owner}; this worker's failure is not recorded over it"
            )
        return ToolInvocation.model_validate(row)

    def get_by_id(self, invocation_id: str) -> ToolInvocation | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {INVOCATION_COLUMNS} from tool_invocations where invocation_id = %s",
                (invocation_id,),
            ).fetchone()
        return ToolInvocation.model_validate(row) if row else None

    def reopen(self, key: str) -> None:
        """Allow a definitively-failed action to be attempted again.

        Only ever called for a row this process just marked ``FAILED``, where
        "the side effect did not happen" is known rather than assumed.
        """

        with self._db.tx() as conn:
            conn.execute(
                "delete from tool_invocations where idempotency_key = %s and status = 'FAILED'",
                (key,),
            )

    def abandon_stale(self, *, older_than_seconds: int) -> tuple[ToolInvocation, ...]:
        """Mark long-running IN_FLIGHT rows as ABANDONED.

        Called by the daemon. An abandoned row is a flag for a human or a
        reconciler, never something the runtime silently retries.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                update tool_invocations
                set status = 'ABANDONED', finished_at = now(),
                    error = coalesce(error, 'in flight with no owner reporting')
                where status = 'IN_FLIGHT'
                  and started_at < now() - make_interval(secs => %(age)s)
                returning {INVOCATION_COLUMNS}
                """,
                {"age": float(older_than_seconds)},
            ).fetchall()
        return tuple(ToolInvocation.model_validate(row) for row in rows)

    def run(
        self,
        *,
        key: str,
        kind: str,
        perform: Callable[[], dict[str, Any]],
        request: dict[str, Any] | None = None,
        run_id: str | None = None,
        work_id: str | None = None,
        owner: str | None = None,
        reconcile: Callable[[ToolInvocation], dict[str, Any] | None] | None = None,
        retry_on_failure: bool = True,
    ) -> Outcome:
        """Perform ``perform`` at most once for ``key``, ever.

        The ordering is the contract:

        - the intent is durable *before* ``perform`` runs, so a crash during it
          leaves evidence;
        - a ``COMPLETED`` row short-circuits, and ``perform`` is not called;
        - an ``ABANDONED`` row -- someone died mid-action -- is offered to
          ``reconcile``, which either returns the real outcome or ``None`` to
          say "it did not happen"; with no reconciler the action is refused.
        """

        who = owner or worker_identity()
        invocation, claimed = self._claim(
            key=key,
            kind=kind,
            request=request or {},
            run_id=run_id,
            work_id=work_id,
            owner=who,
        )

        if invocation.status is InvocationStatus.COMPLETED:
            LOG.info(
                "reusing completed invocation %s (%s)", invocation.invocation_id, kind
            )
            return Outcome(
                result=invocation.result or {}, invocation=invocation, reused=True
            )

        if invocation.status is InvocationStatus.FAILED:
            if not retry_on_failure:
                raise IdempotencyError(
                    f"{kind} previously failed ({invocation.error}) and is not retryable"
                )
            # FAILED means "the performer raised", which is *not* the same as
            # "the effect did not happen". `perform` routinely raises after the
            # external world has already changed -- an `sbatch` that succeeded
            # and then a database blip while recording the job id. The first
            # version deleted the row and re-performed, which submitted the same
            # experiment to a cluster twice. So the reconciler gets the first
            # word here too, exactly as it does for ABANDONED.
            if reconcile is not None:
                found = reconcile(invocation)
                if found is not None:
                    LOG.warning(
                        "invocation %s (%s) recorded a failure but the action had "
                        "taken hold; reusing it",
                        invocation.invocation_id,
                        kind,
                    )
                    recovered = self.complete(invocation.invocation_id, result=found)
                    return Outcome(result=found, invocation=recovered, reused=True)
            self.reopen(key)
            invocation, claimed = self._claim(
                key=key,
                kind=kind,
                request=request or {},
                run_id=run_id,
                work_id=work_id,
                owner=who,
            )

        if invocation.status is InvocationStatus.ABANDONED:
            if reconcile is None:
                raise UnreconciledInvocationError(
                    f"{kind} was started by {invocation.owner} and never finished. Its "
                    f"outcome is unknown and no reconciler is available, so it will not "
                    f"be performed again. Inspect invocation {invocation.invocation_id}."
                )
            found = reconcile(invocation)
            if found is not None:
                completed = self.complete(invocation.invocation_id, result=found)
                LOG.warning(
                    "reconciled abandoned invocation %s (%s): the action had happened",
                    invocation.invocation_id,
                    kind,
                )
                return Outcome(result=found, invocation=completed, reused=True)
            if not self._retake(invocation.invocation_id, owner=who):
                current = self.get(key)
                if current is not None and current.status is InvocationStatus.COMPLETED:
                    return Outcome(
                        result=current.result or {}, invocation=current, reused=True
                    )
                raise IdempotencyError(f"could not take over invocation for {kind}")
            claimed = True

        if not claimed and invocation.status is InvocationStatus.IN_FLIGHT:
            # Someone else holds it and is presumably still alive. Not an error:
            # this worker simply does not do it.
            raise IdempotencyError(
                f"{kind} is already in flight, held by {invocation.owner} since "
                f"{invocation.started_at.isoformat()}"
            )

        try:
            result = perform()
        except Exception as exc:
            self.mark_failed(
                invocation.invocation_id, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        completed = self.complete(invocation.invocation_id, result=result)
        return Outcome(result=result, invocation=completed, reused=False)

    def list_for_run(
        self, run_id: str, *, limit: int = 200
    ) -> tuple[ToolInvocation, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {INVOCATION_COLUMNS} from tool_invocations where run_id = %s "
                "order by started_at desc limit %s",
                (run_id, limit),
            ).fetchall()
        return tuple(ToolInvocation.model_validate(row) for row in rows)
