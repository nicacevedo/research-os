"""Budgets: reserve, execute, reconcile.

The naive design is a counter that is incremented after each spend. It is wrong
in two ways that both matter for a system meant to run unattended overnight.

**It over-spends under concurrency.** Two workers read "58 of 60 calls used",
both conclude there is room, and both spend. With four workers and a cheap
check that is a budget overrun of a few percent; with an expensive model it is
real money. So capacity is taken *before* the spend, in one SQL statement whose
``where`` clause is the check, and the statement either reserves or reports that
it could not.

**It under-counts after a crash.** A worker that spends and dies before
incrementing leaves the budget believing the money is still there. So the
reservation is durable and separate from the settlement: a crash leaves a
``HELD`` reservation, and `available` already excludes it. The daemon later
settles or releases it. Pessimism in the crash window is the right direction of
error for money.

Hence:

```text
reserve(amount)   -> HELD, and `available` drops immediately
  ...spend...
settle(actual)    -> HELD becomes SETTLED, `spent` += actual, `reserved` -= held
  or release()    -> HELD becomes RELEASED, `reserved` -= held, nothing spent
```

**Exhaustion is a destination, not an error to retry.** A budget that retries is
not a budget. ``FailureClass.BUDGET_EXHAUSTED`` is terminal, and a run whose
budget is gone ends in ``TerminalState.BUDGET_EXHAUSTED`` with what it did
manage to do intact -- not in a loop that keeps asking.

Scopes nest, and the *tightest* one wins. A run that asks for capacity is
checked against its own budget, its project's, and the system's; if any refuses,
the answer is no. That ordering means editing the system budget can never widen
a run that is already going.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database
from research_os.runtime.ids import new_budget_id, new_reservation_id
from research_os.runtime.models import BudgetRecord, BudgetScope, Reservation

LOG = logging.getLogger("research_os.runtime.budgets")

BUDGET_COLUMNS = (
    "budget_id, scope, scope_id, dimension, limit_value, reserved, spent, "
    "created_at, updated_at"
)
RESERVATION_COLUMNS = (
    "reservation_id, budget_id, work_id, amount, status, created_at, settled_at"
)


class Dimension(StrEnum):
    """What is being counted.

    Separate dimensions rather than one currency, because they are not
    interchangeable: running out of GPU hours and running out of dollars call
    for different responses, and a run that converted one into the other would
    be making a decision that is not its to make.
    """

    MODEL_CALLS = "model_calls"
    MODEL_COST_USD = "model_cost_usd"
    WALL_CLOCK_SECONDS = "wall_clock_seconds"
    EXTERNAL_JOBS = "external_jobs"
    WORK_ITEMS = "work_items"
    STORAGE_BYTES = "storage_bytes"
    EXTERNAL_CALLS = "external_calls"


class BudgetError(ResearchOSError):
    """Raised when a budget cannot be read or changed."""


class BudgetExhaustedError(BudgetError):
    """Raised when there is not enough capacity left to proceed.

    Carries the dimension and the scope so the terminal state can say which
    budget ran out, which is the first thing a researcher asks.
    """

    def __init__(
        self, message: str, *, dimension: Dimension, scope: BudgetScope, scope_id: str
    ) -> None:
        super().__init__(message)
        self.dimension = dimension
        self.scope = scope
        self.scope_id = scope_id


@dataclass(frozen=True, slots=True)
class Grant:
    """A held reservation. Settle it or release it; never neither."""

    reservation_id: str
    budget_id: str
    amount: Decimal
    dimension: Dimension
    scope: BudgetScope
    scope_id: str


class BudgetLedger:
    """Budget reads and writes against one database."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    # -------------------------------------------------------------- defining --
    def set_limit(
        self,
        *,
        scope: BudgetScope,
        scope_id: str,
        dimension: Dimension,
        limit_value: Decimal | float,
    ) -> BudgetRecord:
        """Create or raise/lower a limit, preserving what has been spent."""

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into budgets (budget_id, scope, scope_id, dimension, limit_value)
                values (%(budget_id)s, %(scope)s, %(scope_id)s, %(dimension)s, %(limit_value)s)
                on conflict (scope, scope_id, dimension) do update
                    set limit_value = excluded.limit_value, updated_at = now()
                returning {BUDGET_COLUMNS}
                """,
                {
                    "budget_id": new_budget_id(),
                    "scope": str(scope),
                    "scope_id": scope_id,
                    "dimension": str(dimension),
                    "limit_value": Decimal(str(limit_value)),
                },
            ).fetchone()
        return BudgetRecord.model_validate(row)

    def get(
        self, *, scope: BudgetScope, scope_id: str, dimension: Dimension
    ) -> BudgetRecord | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {BUDGET_COLUMNS} from budgets "
                "where scope = %s and scope_id = %s and dimension = %s",
                (str(scope), scope_id, str(dimension)),
            ).fetchone()
        return BudgetRecord.model_validate(row) if row else None

    def list_for(
        self, *, scope: BudgetScope, scope_id: str
    ) -> tuple[BudgetRecord, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {BUDGET_COLUMNS} from budgets where scope = %s and scope_id = %s "
                "order by dimension",
                (str(scope), scope_id),
            ).fetchall()
        return tuple(BudgetRecord.model_validate(row) for row in rows)

    # ------------------------------------------------------------- reserving --
    def reserve(
        self,
        *,
        scope: BudgetScope,
        scope_id: str,
        dimension: Dimension,
        amount: Decimal | float,
        work_id: str | None = None,
    ) -> Grant:
        """Take capacity now, before spending it.

        The ``where`` clause *is* the check, which is what makes this safe under
        concurrency: PostgreSQL evaluates it against the row it is about to
        lock, so two workers cannot both pass. An absent budget means
        unlimited -- budgets are opt-in per dimension -- and that is recorded as
        a zero-amount grant so the caller's settle/release path is unconditional.
        """

        wanted = Decimal(str(amount))
        if wanted < 0:
            raise BudgetError(f"cannot reserve a negative amount ({wanted})")
        with self._db.tx() as conn:
            budget = conn.execute(
                f"select {BUDGET_COLUMNS} from budgets "
                "where scope = %s and scope_id = %s and dimension = %s for update",
                (str(scope), scope_id, str(dimension)),
            ).fetchone()
            if budget is None:
                return Grant(
                    reservation_id="",
                    budget_id="",
                    amount=wanted,
                    dimension=dimension,
                    scope=scope,
                    scope_id=scope_id,
                )
            updated = conn.execute(
                """
                update budgets
                set reserved = reserved + %(amount)s, updated_at = now()
                where budget_id = %(budget_id)s
                  and limit_value - reserved - spent >= %(amount)s
                returning budget_id, limit_value - reserved - spent as remaining
                """,
                {"budget_id": budget["budget_id"], "amount": wanted},
            ).fetchone()
            if updated is None:
                available = (
                    Decimal(budget["limit_value"])
                    - Decimal(budget["reserved"])
                    - Decimal(budget["spent"])
                )
                raise BudgetExhaustedError(
                    f"{dimension} budget for {scope}:{scope_id} has {available} left; "
                    f"{wanted} was requested",
                    dimension=dimension,
                    scope=scope,
                    scope_id=scope_id,
                )
            reservation_id = new_reservation_id()
            conn.execute(
                """
                insert into budget_reservations (reservation_id, budget_id, work_id, amount)
                values (%(reservation_id)s, %(budget_id)s, %(work_id)s, %(amount)s)
                """,
                {
                    "reservation_id": reservation_id,
                    "budget_id": budget["budget_id"],
                    "work_id": work_id,
                    "amount": wanted,
                },
            )
        return Grant(
            reservation_id=reservation_id,
            budget_id=str(budget["budget_id"]),
            amount=wanted,
            dimension=dimension,
            scope=scope,
            scope_id=scope_id,
        )

    def reserve_all(
        self,
        *,
        dimension: Dimension,
        amount: Decimal | float,
        run_id: str,
        project_id: str,
        work_id: str | None = None,
    ) -> tuple[Grant, ...]:
        """Reserve against run, project and system, or reserve against none.

        The tightest scope wins, and "wins" has to mean that nothing is left
        held when one of them refuses. So a refusal releases whatever was
        already taken before re-raising: a half-reserved spend that never
        happens would leak capacity on every refusal until the run looked broke.
        """

        scopes = (
            (BudgetScope.RUN, run_id),
            (BudgetScope.PROJECT, project_id),
            (BudgetScope.SYSTEM, "system"),
        )
        taken: list[Grant] = []
        try:
            for scope, scope_id in scopes:
                taken.append(
                    self.reserve(
                        scope=scope,
                        scope_id=scope_id,
                        dimension=dimension,
                        amount=amount,
                        work_id=work_id,
                    )
                )
        except BaseException:
            # Any failure, not only exhaustion. Each `reserve` is its own
            # transaction, so a transient database error or a constraint
            # violation on the second or third leaked the grants already taken --
            # and the docstring above promised the opposite. Release is
            # best-effort so a failing release cannot mask the original error.
            for grant in taken:
                try:
                    self.release(grant)
                except Exception as exc:  # noqa: BLE001 - must not mask the cause
                    LOG.warning(
                        "could not release %s while rolling back a reservation: %s",
                        grant.reservation_id,
                        exc,
                    )
            raise
        return tuple(taken)

    # ------------------------------------------------------------- settling --
    def settle(self, grant: Grant, *, actual: Decimal | float | None = None) -> None:
        """Convert a held reservation into a recorded spend.

        ``actual`` may differ from the reservation: a model call is reserved at
        an estimate and settled at the reported cost. Where the provider reports
        nothing, the estimate stands -- a spend recorded as zero because the
        number was unavailable is the one accounting error that compounds.
        """

        if not grant.reservation_id:
            return
        spent = Decimal(str(actual)) if actual is not None else grant.amount
        with self._db.tx() as conn:
            row = conn.execute(
                """
                update budget_reservations set status = 'SETTLED', settled_at = now()
                where reservation_id = %s and status = 'HELD'
                returning budget_id, amount
                """,
                (grant.reservation_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                update budgets
                set reserved = greatest(reserved - %(held)s, 0),
                    spent = spent + %(spent)s,
                    updated_at = now()
                where budget_id = %(budget_id)s
                """,
                {
                    "budget_id": row["budget_id"],
                    "held": Decimal(row["amount"]),
                    "spent": spent,
                },
            )

    def release(self, grant: Grant) -> None:
        """Give capacity back because the spend did not happen."""

        if not grant.reservation_id:
            return
        with self._db.tx() as conn:
            row = conn.execute(
                """
                update budget_reservations set status = 'RELEASED', settled_at = now()
                where reservation_id = %s and status = 'HELD'
                returning budget_id, amount
                """,
                (grant.reservation_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "update budgets set reserved = greatest(reserved - %s, 0), updated_at = now() "
                "where budget_id = %s",
                (Decimal(row["amount"]), row["budget_id"]),
            )

    def settle_all(
        self, grants: tuple[Grant, ...], *, actual: Decimal | float | None = None
    ) -> None:
        for grant in grants:
            self.settle(grant, actual=actual)

    def release_all(self, grants: tuple[Grant, ...]) -> None:
        for grant in grants:
            self.release(grant)

    # ----------------------------------------------------------- reconciling --
    def reconcile_stale(self, *, older_than_seconds: int, limit: int = 500) -> int:
        """Release reservations whose worker never came back.

        Called by the daemon. Deliberately releases rather than settles: the
        spend is *unknown*, and assuming it happened would charge for work that
        may never have been done. The model-call provenance table is the record
        of what was actually spent; this is only the capacity reservation
        catching up.

        Aggregated in SQL, ordered by ``budget_id``, and bounded. The first
        version issued one ``update budgets`` per stale row from a Python loop
        in arbitrary order, which two daemons could deadlock against each other
        -- and since this is the only thing that releases leaked capacity,
        starving it starves the recovery.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                with stale as (
                    select reservation_id, budget_id, amount
                    from budget_reservations
                    where status = 'HELD'
                      and created_at < now() - make_interval(secs => %(age)s)
                    order by reservation_id
                    for update skip locked
                    limit %(limit)s
                ), released as (
                    update budget_reservations r
                    set status = 'RELEASED', settled_at = now()
                    from stale s where r.reservation_id = s.reservation_id
                    returning r.budget_id, r.amount
                ), totals as (
                    select budget_id, sum(amount) as amount
                    from released group by budget_id
                ), applied as (
                    update budgets b
                    set reserved = greatest(b.reserved - t.amount, 0),
                        updated_at = now()
                    from (select * from totals order by budget_id) t
                    where b.budget_id = t.budget_id
                    returning 1
                )
                select (select count(*) from released) as n
                """,
                {"age": float(older_than_seconds), "limit": limit},
            ).fetchone()
        count = int(row["n"]) if row else 0
        if count:
            LOG.warning("released %d stale budget reservation(s)", count)
        return count

    def held_reservations(self, *, budget_id: str) -> tuple[Reservation, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {RESERVATION_COLUMNS} from budget_reservations "
                "where budget_id = %s and status = 'HELD' order by created_at",
                (budget_id,),
            ).fetchall()
        return tuple(Reservation.model_validate(row) for row in rows)

    def exhausted_dimensions(
        self, *, run_id: str, project_id: str
    ) -> tuple[tuple[BudgetScope, str, Dimension], ...]:
        """Every budget with nothing left, across the three scopes.

        Used by the continuation policy to decide ``BUDGET_EXHAUSTED`` rather
        than starting a cycle that cannot finish.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select scope, scope_id, dimension from budgets
                where limit_value - reserved - spent <= 0
                  and ((scope = 'run' and scope_id = %(run_id)s)
                       or (scope = 'project' and scope_id = %(project_id)s)
                       or (scope = 'system' and scope_id = 'system'))
                order by scope, dimension
                """,
                {"run_id": run_id, "project_id": project_id},
            ).fetchall()
        return tuple(
            (
                BudgetScope(row["scope"]),
                str(row["scope_id"]),
                Dimension(row["dimension"]),
            )
            for row in rows
        )
