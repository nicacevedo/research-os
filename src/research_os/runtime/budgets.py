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
``HELD`` reservation, and `available` already excludes it. Pessimism in the
crash window is the right direction of error for money.

**Unknown spend fails closed** (``docs/ARCHITECTURE_INVARIANTS.md``, INV-01).
Whether a reservation may be handed back without a reported cost depends on
one fact, recorded durably *before* the external call starts: whether it was
ever submitted. A caller that knows the window exists reserves ``pending``
and calls :meth:`BudgetLedger.mark_submitted` immediately before it hands the
work over; every other reservation is submitted from the moment it exists.

Hence:

```text
reserve(amount, pending=True)  -> HELD, not submitted; `available` drops now
mark_submitted(provider_cap)   -> HELD, submitted: the outcome is now unknown
settle(actual)                 -> SETTLED at a reported cost
settle_unknown()               -> SETTLED at the whole amount: no report exists
release()                      -> RELEASED, only on evidence nothing was spent
```

and the reconciler follows the same rule for a worker that never came back:
a reservation that was never submitted is released, and one that was is
settled at its whole amount, because its outcome is exactly what nobody knows.

**Charging the whole reservation is only a bound if the provider was bound
too** (INV-01). A worker that dies after the provider answered is charged its
reservation; if the provider had been allowed to spend more than that, the
ledger would record less than was billed and the ceiling would silently stop
binding. So a ``model_cost_usd`` reservation cannot be marked submitted without
the provider-enforced cap it was handed, no greater than its amount --
recorded on the row (``provider_cap_usd``, ``sql/0043``) and refused by the
database otherwise. The sum of what providers were *authorised* to spend is
then never more than what was reserved, and what was reserved is never more
than any scope's limit.

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
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database
from research_os.runtime.ids import new_budget_id, new_reservation_id
from research_os.runtime.models import (
    BudgetRecord,
    BudgetScope,
    Reservation,
    SettlementBasis,
)

LOG = logging.getLogger("research_os.runtime.budgets")

BUDGET_COLUMNS = (
    "budget_id, scope, scope_id, dimension, limit_value, reserved, spent, "
    "explicit, created_at, updated_at"
)
RESERVATION_COLUMNS = (
    "reservation_id, budget_id, work_id, amount, status, created_at, settled_at, "
    "submitted_at, settlement_basis, provider_cap_usd"
)

#: What one model call may cost when its caller declared no ceiling of its own.
#:
#: Not an estimate: it is reserved against every applicable scope *and* handed
#: to the provider as its hard cap, so it is exactly what the call is
#: authorised to spend. Chosen above the pilots' observed mean (0.13 USD over
#: twelve calls) by enough that ordinary calls fit, and low enough that a 6 USD
#: run authorises about a dozen rather than two. A call that needs more is
#: stopped by the provider at this number and fails ``BUDGET_EXHAUSTED``.
DEFAULT_CALL_CEILING_USD = Decimal("0.50")


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
    EXTERNAL_CALLS = "external_calls"


def provider_cap(amount: Decimal) -> float:
    """``amount`` as the float a provider adapter carries, never above it.

    ``InvocationRequest.max_budget_usd`` is a float, and a float's decimal
    rendering can sit a hair above the decimal it came from. A cap that is
    one ulp over its reservation is a cap over its reservation, so the value
    is stepped down until its rendering is not.
    """

    value = float(amount)
    while value > 0 and Decimal(str(value)) > amount:
        value = math.nextafter(value, 0.0)
    return value


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
        explicit: bool = False,
        opening_spent: Decimal | float = 0,
    ) -> BudgetRecord:
        """Create or raise/lower a limit, preserving what has been spent.

        ``explicit`` records that a *person* set this number, and the default
        is false because almost every caller here is the runtime deriving one
        from configuration. Only ``researchctl runtime budget`` passes true.

        Once true it stays true: a derived write over an explicit ceiling
        must not quietly demote it back, or the protection would last until
        the next time anything touched the row.

        ``opening_spent`` is what the scope had already spent before it had a
        row, and it is read only when the row is *created*. An idea or a
        lineage that ran stages before `sql/0036` has its spend recorded on
        `idea_actions` and nowhere in this table, and a row opening at zero
        would hand it its whole ceiling a second time.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into budgets
                    (budget_id, scope, scope_id, dimension, limit_value, explicit,
                     spent)
                values (%(budget_id)s, %(scope)s, %(scope_id)s, %(dimension)s,
                        %(limit_value)s, %(explicit)s, %(opening_spent)s)
                on conflict (scope, scope_id, dimension) do update
                    set limit_value = excluded.limit_value,
                        explicit = budgets.explicit or excluded.explicit,
                        updated_at = now()
                returning {BUDGET_COLUMNS}
                """,
                {
                    "budget_id": new_budget_id(),
                    "scope": str(scope),
                    "scope_id": scope_id,
                    "dimension": str(dimension),
                    "limit_value": Decimal(str(limit_value)),
                    "explicit": explicit,
                    "opening_spent": max(Decimal(str(opening_spent)), Decimal(0)),
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
        pending: bool = False,
    ) -> Grant:
        """Take capacity now, before spending it.

        The ``where`` clause *is* the check, which is what makes this safe under
        concurrency: PostgreSQL evaluates it against the row it is about to
        lock, so two workers cannot both pass. An absent budget means
        unlimited -- budgets are opt-in per dimension -- and that is recorded as
        a zero-amount grant so the caller's settle/release path is unconditional.

        ``pending`` says the spend has not been handed to anything yet, and
        obliges the caller to :meth:`mark_submitted` before it is. Without
        it the reservation is submitted from the moment it exists, which is
        the conservative reading for a caller that never said otherwise: its
        outcome is unknown, and the reconciler will charge it rather than
        hand it back.
        """

        wanted = Decimal(str(amount))
        if wanted < 0:
            raise BudgetError(f"cannot reserve a negative amount ({wanted})")
        if dimension is Dimension.MODEL_COST_USD and not pending:
            # Money is handed to a provider only through `mark_submitted`,
            # with the cap the provider is given (INV-01). A cost reservation
            # that is "submitted from the moment it exists" would have no cap
            # on record, and the database refuses it (`sql/0043`); saying so
            # here names the rule instead of a trigger.
            raise BudgetError(
                "a model_cost_usd reservation is taken pending and submitted "
                "with the provider cap it authorises; it cannot be submitted "
                "at creation"
            )
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
                insert into budget_reservations
                    (reservation_id, budget_id, work_id, amount, submitted_at)
                values (%(reservation_id)s, %(budget_id)s, %(work_id)s, %(amount)s,
                        case when %(pending)s then null else now() end)
                """,
                {
                    "reservation_id": reservation_id,
                    "budget_id": budget["budget_id"],
                    "work_id": work_id,
                    "amount": wanted,
                    "pending": pending,
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
        extra: tuple[tuple[BudgetScope, str], ...] = (),
        pending: bool = False,
    ) -> tuple[Grant, ...]:
        """Reserve against run, project and system, or reserve against none.

        The tightest scope wins, and "wins" has to mean that nothing is left
        held when one of them refuses. So a refusal releases whatever was
        already taken before re-raising: a half-reserved spend that never
        happens would leak capacity on every refusal until the run looked broke.

        ``extra`` names further scopes the same spend counts against -- the
        discovery portfolio's idea and lineage (`sql/0036`) -- reserved in the
        same all-or-nothing pass, so a lineage's ceiling is checked by the
        same ``where`` clause as a project's and cannot be oversubscribed by
        two concurrent stages either.
        """

        scopes = (
            (BudgetScope.RUN, run_id),
            (BudgetScope.PROJECT, project_id),
            (BudgetScope.SYSTEM, "system"),
            *extra,
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
                        pending=pending,
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
                    self._rollback(grant)
                except Exception as exc:  # noqa: BLE001 - must not mask the cause
                    LOG.warning(
                        "could not release %s while rolling back a reservation: %s",
                        grant.reservation_id,
                        exc,
                    )
            raise
        return tuple(taken)

    # ------------------------------------------------------------ submitting --
    def mark_submitted(
        self,
        grants: tuple[Grant, ...],
        *,
        provider_cap: Decimal | float | None = None,
    ) -> None:
        """Record, durably, that these reservations' work is being handed over.

        Called immediately before the external call and never after it: from
        this statement on, the reservation's outcome is unknown until the
        provider reports, and nothing may release it without a report that
        says it was not spent. A crash *before* it leaves a reservation the
        reconciler may hand back, because the work provably never started.

        ``provider_cap`` is the hard limit the provider is being handed, and a
        ``model_cost_usd`` reservation is refused submission without one no
        greater than its own amount (INV-01) -- here, before anything is
        written, and again by the database (``sql/0043``). That is what makes
        charging the reservation on an unknown outcome an upper bound on what
        was billed, rather than a guess.
        """

        cap = Decimal(str(provider_cap)) if provider_cap is not None else None
        for grant in grants:
            if grant.dimension is not Dimension.MODEL_COST_USD:
                continue
            if cap is None or not cap.is_finite() or cap <= 0 or cap > grant.amount:
                raise BudgetError(
                    f"refusing to submit a {grant.amount} USD reservation for "
                    f"{grant.scope}:{grant.scope_id} with provider cap {cap}: "
                    f"a spend-bearing call is handed to a provider only with a "
                    f"hard cap no greater than what was reserved for it"
                )
        ids = [grant.reservation_id for grant in grants if grant.reservation_id]
        if not ids:
            return
        capped = [
            grant.reservation_id
            for grant in grants
            if grant.reservation_id and grant.dimension is Dimension.MODEL_COST_USD
        ]
        with self._db.tx() as conn:
            conn.execute(
                "update budget_reservations "
                "set submitted_at = now(), "
                "    provider_cap_usd = case when reservation_id = any(%(capped)s) "
                "                            then %(cap)s else provider_cap_usd end "
                "where reservation_id = any(%(ids)s) and status = 'HELD' "
                "and submitted_at is null",
                {"ids": ids, "capped": capped, "cap": cap},
            )

    # ------------------------------------------------------------- settling --
    def settle(
        self,
        grant: Grant,
        *,
        actual: Decimal | float | None = None,
        basis: SettlementBasis | None = None,
    ) -> None:
        """Convert a held reservation into a recorded spend.

        ``actual`` may differ from the reservation: a model call is reserved at
        an estimate and settled at the reported cost. Where the provider reports
        nothing, the estimate stands -- a spend recorded as zero because the
        number was unavailable is the one accounting error that compounds.

        Idempotent: only a HELD reservation is settled, so a second settlement
        of the same grant -- a retried handler, the reconciler racing the
        worker -- changes nothing.
        """

        if not grant.reservation_id:
            return
        spent = Decimal(str(actual)) if actual is not None else grant.amount
        recorded = basis or (
            SettlementBasis.ESTIMATE
            if actual is None
            else SettlementBasis.REPORTED_OVER_RESERVATION
            if spent > grant.amount
            else SettlementBasis.REPORTED
        )
        if recorded is SettlementBasis.REPORTED_OVER_RESERVATION:
            # The provider billed more than the cap it was handed, which was
            # this reservation. Recorded at what it said -- the money is gone
            # -- and marked, so a provider that does not honour its cap is a
            # row a person can find rather than a number that looks ordinary.
            LOG.warning(
                "%s:%s settled at %s against a %s reservation: the provider "
                "reported more than the hard cap it was given",
                grant.scope,
                grant.scope_id,
                spent,
                grant.amount,
            )
        with self._db.tx() as conn:
            row = conn.execute(
                """
                update budget_reservations
                   set status = 'SETTLED', settled_at = now(),
                       settlement_basis = %s,
                       submitted_at = coalesce(submitted_at, now())
                where reservation_id = %s and status = 'HELD'
                returning budget_id, amount
                """,
                (str(recorded), grant.reservation_id),
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

    def settle_unknown(self, grants: tuple[Grant, ...]) -> None:
        """Charge the whole reservation: the work was submitted, its cost is unknown.

        The fail-closed half of INV-01. A provider killed at its timeout, an
        adapter that raised after starting a process, a response lost before
        it was read -- none of them says what was billed, and every one of
        them may have billed up to the ceiling it was capped at. Handing that
        ceiling back is how five calls capped at 0.60 USD each ran under a
        1.00 USD ceiling; charging it is how the sixth is refused.
        """

        for grant in grants:
            self.settle(grant, basis=SettlementBasis.UNKNOWN_OUTCOME)

    def release(self, grant: Grant, *, basis: SettlementBasis | None = None) -> None:
        """Give capacity back because the spend did not happen.

        Only on evidence. ``basis`` names it; a caller with none should not be
        here, and :meth:`settle_unknown` is where it belongs instead.
        """

        if not grant.reservation_id:
            return
        with self._db.tx() as conn:
            row = conn.execute(
                """
                update budget_reservations
                   set status = 'RELEASED', settled_at = now(),
                       settlement_basis = %s
                where reservation_id = %s and status = 'HELD'
                returning budget_id, amount
                """,
                (str(basis or SettlementBasis.NOT_INVOKED), grant.reservation_id),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "update budgets set reserved = greatest(reserved - %s, 0), updated_at = now() "
                "where budget_id = %s",
                (Decimal(row["amount"]), row["budget_id"]),
            )

    def _rollback(self, grant: Grant) -> None:
        """Undo a reservation this ledger took and nothing has used yet."""

        self.release(grant, basis=SettlementBasis.REFUSED)

    def charge_all(
        self,
        *,
        dimension: Dimension,
        amount: Decimal | float,
        run_id: str,
        project_id: str,
        work_id: str | None = None,
    ) -> tuple[str, ...]:
        """Record a spend that has **already happened**, past the limit if need be.

        ``work_id`` is accepted and recorded in the log line only. There is no
        work-item budget scope in use, and an earlier version took the argument
        and never referenced it at all, which made the signature promise an
        attribution that did not exist.

        Returns the ids of any budgets this pushed over their limit.

        Every other path here is reserve-then-settle, and that is the right
        shape for a spend this process is about to make: the ``where`` clause is
        the check, so two workers cannot both be told there is room for the last
        call. This is for the case that shape cannot express -- a spend made by
        a subsystem the runtime *delegated to*, discovered after the fact.

        `propose_capsule_change` and the coding action call v1 controllers,
        which own their own providers and their own per-action call ceilings and
        never touch this ledger. So a cycle started with ``--max-cost-usd 6``
        could report having spent 0.10 while a delegated proposal worker had
        made three calls, and `runtime run` showed one model call for a cycle
        that made four. The budget was not wrong about what it had reserved; it
        was silent about what the run had cost.

        Refusing to record an overrun is not an option, because the money is
        already gone. So this records it and *reports* which budgets it broke,
        and the next `reserve` sees the spend -- the cap bites on the following
        call rather than the one that overran. That is weaker than a
        reservation and it is the strongest thing that is true.
        """

        spend = Decimal(str(amount))
        if spend <= 0:
            return ()
        over: list[str] = []
        scopes = (
            (BudgetScope.RUN, run_id),
            (BudgetScope.PROJECT, project_id),
            (BudgetScope.SYSTEM, "system"),
        )
        with self._db.tx() as conn:
            for scope, scope_id in scopes:
                row = conn.execute(
                    """
                    update budgets
                    set spent = spent + %(amount)s, updated_at = now()
                    where scope = %(scope)s and scope_id = %(scope_id)s
                      and dimension = %(dimension)s
                    returning budget_id, spent, reserved, limit_value
                    """,
                    {
                        "amount": spend,
                        "scope": str(scope),
                        "scope_id": scope_id,
                        "dimension": str(dimension),
                    },
                ).fetchone()
                if row is None:
                    # No budget at this scope means unlimited at this scope,
                    # which is what an absent row means everywhere here.
                    continue
                # `spent` alone, not `spent + reserved`. A reservation is
                # money *promised* and another worker's in-flight HELD row can
                # be released, so including it warned about budgets that were
                # not overspent and might never be. What this reports is the
                # fact it has: this charge took recorded spend past the limit.
                if Decimal(row["spent"]) > Decimal(row["limit_value"]):
                    over.append(str(row["budget_id"]))
        if over:
            LOG.warning(
                "a delegated %s spend of %s%s pushed %d budget(s) past their limit: %s",
                dimension,
                spend,
                f" (work {work_id})" if work_id else "",
                len(over),
                ", ".join(over),
            )
        return tuple(over)

    def settle_all(
        self,
        grants: tuple[Grant, ...],
        *,
        actual: Decimal | float | None = None,
        basis: SettlementBasis | None = None,
    ) -> None:
        for grant in grants:
            self.settle(grant, actual=actual, basis=basis)

    def release_all(
        self, grants: tuple[Grant, ...], *, basis: SettlementBasis | None = None
    ) -> None:
        for grant in grants:
            self.release(grant, basis=basis)

    # ----------------------------------------------------------- reconciling --
    def reconcile_stale(self, *, older_than_seconds: int, limit: int = 500) -> int:
        """Close reservations whose worker never came back, failing closed.

        Called by the daemon. Two cases, told apart by the one fact that
        decides them (`sql/0037`):

        - **never submitted** -- the worker died after reserving and before
          it handed anything to a provider. Nothing can have been spent, so
          the capacity is released;
        - **submitted** -- the worker may have started the call and died
          before it recorded what the call cost. Nobody knows, and this used
          to *release* it on exactly that ground, which hands a person's
          ceiling back for work that may have been billed in full. It is
          settled at its whole amount instead: an over-count here is visible
          and recoverable by a person, an under-count is money that is gone
          and a ceiling that has silently stopped binding.

        Aggregated in SQL and bounded. What makes it deadlock-safe is the ``for
        update skip locked`` on the reservations: two daemons never contend for
        the same rows, so there is no wait cycle. The ``order by budget_id`` is a
        hint, not a guarantee -- PostgreSQL does not promise lock-acquisition
        order from a subquery's ordering, and an earlier version of this
        paragraph said it did. The first
        version issued one ``update budgets`` per stale row from a Python loop
        in arbitrary order, which two daemons could deadlock against each other
        -- and since this is the only thing that closes leaked reservations,
        starving it starves the recovery.

        Idempotent: a reservation leaves ``HELD`` exactly once, so a second
        pass over the same rows finds nothing.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                with stale as (
                    select reservation_id, budget_id, amount,
                           submitted_at is not null as submitted
                    from budget_reservations
                    where status = 'HELD'
                      and created_at < now() - make_interval(secs => %(age)s)
                    order by reservation_id
                    for update skip locked
                    limit %(limit)s
                ), closed as (
                    update budget_reservations r
                    set status = case when s.submitted then 'SETTLED'
                                      else 'RELEASED' end,
                        settlement_basis = case when s.submitted
                                                then %(unknown)s
                                                else %(unsubmitted)s end,
                        settled_at = now()
                    from stale s where r.reservation_id = s.reservation_id
                    returning r.budget_id, r.amount, s.submitted
                ), totals as (
                    select budget_id,
                           sum(amount) as held,
                           sum(case when submitted then amount else 0 end) as spent
                    from closed group by budget_id
                ), applied as (
                    update budgets b
                    set reserved = greatest(b.reserved - t.held, 0),
                        spent = b.spent + t.spent,
                        updated_at = now()
                    from (select * from totals order by budget_id) t
                    where b.budget_id = t.budget_id
                    returning 1
                )
                select (select count(*) from closed) as n,
                       (select count(*) from closed where submitted) as charged
                """,
                {
                    "age": float(older_than_seconds),
                    "limit": limit,
                    "unknown": str(SettlementBasis.STALE_UNKNOWN_OUTCOME),
                    "unsubmitted": str(SettlementBasis.STALE_NOT_SUBMITTED),
                },
            ).fetchone()
        count = int(row["n"]) if row else 0
        charged = int(row["charged"]) if row else 0
        if count:
            LOG.warning(
                "reconciled %d stale budget reservation(s): %d released as never "
                "submitted, %d charged in full because their outcome is unknown",
                count,
                count - charged,
                charged,
            )
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
