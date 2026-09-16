"""Budgets, and the concurrency property that makes them worth having.

The test that matters most is
:func:`test_two_workers_cannot_both_take_the_last_unit`. A counter incremented
after the spend passes every single-threaded test and over-spends the moment
two workers read it at once.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from research_os.runtime.budgets import (
    BudgetExhaustedError,
    BudgetLedger,
    Dimension,
    Grant,
)
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope, ReservationStatus
from research_os.runtime.store import RuntimeStore


@pytest.fixture
def ledger(runtime_db: Database) -> BudgetLedger:
    return BudgetLedger(runtime_db)


def test_an_absent_budget_means_unlimited(ledger: BudgetLedger) -> None:
    """Budgets are opt-in per dimension, and the caller's path is unconditional."""

    grant = ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id="RRUN-x",
        dimension=Dimension.MODEL_CALLS,
        amount=5,
    )
    assert grant.reservation_id == ""
    ledger.settle(grant)  # a no-op rather than a crash
    ledger.release(grant)


def test_reserving_reduces_available_before_anything_is_spent(
    ledger: BudgetLedger,
) -> None:
    """Pessimism in the crash window is the right direction of error for money."""

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=10,
    )
    ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        amount=4,
    )
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_COST_USD
    )
    assert budget is not None
    assert budget.reserved == Decimal(4)
    assert budget.spent == Decimal(0)
    assert budget.available == Decimal(6)


def test_settling_moves_the_hold_into_spend(ledger: BudgetLedger) -> None:
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=10,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        amount=4,
    )
    ledger.settle(grant, actual=Decimal("2.5"))
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_COST_USD
    )
    assert budget is not None
    assert budget.reserved == Decimal(0)
    assert budget.spent == Decimal("2.5")
    assert budget.available == Decimal("7.5")


def test_an_unreported_cost_settles_at_the_estimate(ledger: BudgetLedger) -> None:
    """A spend recorded as zero because the number was unavailable compounds."""

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=10,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        amount=3,
    )
    ledger.settle(grant, actual=None)
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_COST_USD
    )
    assert budget is not None
    assert budget.spent == Decimal(3)


def test_releasing_gives_the_capacity_back(ledger: BudgetLedger) -> None:
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=5,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS, amount=2
    )
    ledger.release(grant)
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS
    )
    assert budget is not None
    assert budget.available == Decimal(5)
    assert budget.spent == Decimal(0)


def test_exhaustion_refuses_rather_than_overdrawing(ledger: BudgetLedger) -> None:
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=2,
    )
    ledger.reserve(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS, amount=2
    )
    with pytest.raises(BudgetExhaustedError) as caught:
        ledger.reserve(
            scope=BudgetScope.RUN,
            scope_id="r1",
            dimension=Dimension.MODEL_CALLS,
            amount=1,
        )
    assert caught.value.dimension is Dimension.MODEL_CALLS
    assert caught.value.scope is BudgetScope.RUN
    assert caught.value.scope_id == "r1"


def test_two_workers_cannot_both_take_the_last_unit(ledger: BudgetLedger) -> None:
    """The property a post-hoc counter silently fails.

    Ten threads, one unit of capacity. Exactly one may succeed.
    """

    ledger.set_limit(
        scope=BudgetScope.SYSTEM,
        scope_id="system",
        dimension=Dimension.MODEL_CALLS,
        limit_value=1,
    )

    def attempt(_index: int) -> bool:
        try:
            ledger.reserve(
                scope=BudgetScope.SYSTEM,
                scope_id="system",
                dimension=Dimension.MODEL_CALLS,
                amount=1,
            )
        except BudgetExhaustedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))
    assert sum(results) == 1, f"{sum(results)} workers were told there was room for one"


def test_the_tightest_scope_wins(ledger: BudgetLedger, runtime_project: str) -> None:
    ledger.set_limit(
        scope=BudgetScope.SYSTEM,
        scope_id="system",
        dimension=Dimension.MODEL_CALLS,
        limit_value=100,
    )
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_CALLS,
        limit_value=50,
    )
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=2,
    )
    grants = ledger.reserve_all(
        dimension=Dimension.MODEL_CALLS,
        amount=2,
        run_id="r1",
        project_id=runtime_project,
    )
    assert len(grants) == 3
    with pytest.raises(BudgetExhaustedError, match="run:r1"):
        ledger.reserve_all(
            dimension=Dimension.MODEL_CALLS,
            amount=1,
            run_id="r1",
            project_id=runtime_project,
        )


def test_a_refused_multi_scope_reservation_leaks_nothing(
    ledger: BudgetLedger, runtime_project: str
) -> None:
    """Otherwise every refusal permanently consumes the wider scopes' capacity."""

    ledger.set_limit(
        scope=BudgetScope.SYSTEM,
        scope_id="system",
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    # The run budget is the one that refuses, and it is reserved last.
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=0,
    )
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve_all(
            dimension=Dimension.MODEL_CALLS,
            amount=1,
            run_id="r1",
            project_id=runtime_project,
        )
    for scope, scope_id in (
        (BudgetScope.SYSTEM, "system"),
        (BudgetScope.PROJECT, runtime_project),
    ):
        budget = ledger.get(
            scope=scope, scope_id=scope_id, dimension=Dimension.MODEL_CALLS
        )
        assert budget is not None
        assert budget.reserved == Decimal(0), f"{scope} leaked a reservation"
        assert budget.available == Decimal(10)


def test_a_crashed_workers_reservation_is_released_not_charged(
    ledger: BudgetLedger, runtime_db: Database
) -> None:
    """The spend is unknown. Charging for work that may not have happened is worse.

    What was actually spent lives in ``model_calls``; this is only the capacity
    reservation catching up with a worker that never came back.
    """

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=10,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        amount=4,
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update budget_reservations set created_at = now() - interval '2 hours' "
            "where reservation_id = %s",
            (grant.reservation_id,),
        )
    assert ledger.reconcile_stale(older_than_seconds=3600) == 1
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_COST_USD
    )
    assert budget is not None
    assert budget.reserved == Decimal(0)
    assert budget.spent == Decimal(0)
    held = ledger.held_reservations(budget_id=grant.budget_id)
    assert held == ()


def test_settling_twice_charges_once(ledger: BudgetLedger) -> None:
    """A replayed node must not double-charge."""

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS, amount=1
    )
    ledger.settle(grant)
    ledger.settle(grant)
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS
    )
    assert budget is not None
    assert budget.spent == Decimal(1)


def test_releasing_a_settled_grant_does_not_refund_it(ledger: BudgetLedger) -> None:
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS, amount=1
    )
    ledger.settle(grant)
    ledger.release(grant)
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS
    )
    assert budget is not None
    assert budget.spent == Decimal(1)
    assert budget.reserved == Decimal(0)


def test_exhausted_dimensions_names_what_ran_out(
    ledger: BudgetLedger, runtime_project: str
) -> None:
    """The first thing a researcher asks is which budget it was."""

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_CALLS,
        limit_value=1,
    )
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.EXTERNAL_JOBS,
        limit_value=0,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_CALLS, amount=1
    )
    ledger.settle(grant)
    found = ledger.exhausted_dimensions(run_id="r1", project_id=runtime_project)
    assert (BudgetScope.RUN, "r1", Dimension.MODEL_CALLS) in found
    assert (BudgetScope.PROJECT, runtime_project, Dimension.EXTERNAL_JOBS) in found


def test_a_reservation_row_records_which_work_held_it(
    ledger: BudgetLedger, runtime_db: Database, runtime_project: str
) -> None:
    from research_os.runtime.queue import WorkQueue

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    work = (
        WorkQueue(runtime_db)
        .enqueue(project_id=runtime_project, kind="demo", run_id=run.run_id)
        .item
    )
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=5,
    )
    grant = ledger.reserve(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_CALLS,
        amount=1,
        work_id=work.work_id,
    )
    held = ledger.held_reservations(budget_id=grant.budget_id)
    assert len(held) == 1
    assert held[0].work_id == work.work_id
    assert held[0].status is ReservationStatus.HELD


def test_an_empty_grant_is_safe_to_pass_around() -> None:
    """The unlimited case must not need a branch at every call site."""

    grant = Grant(
        reservation_id="",
        budget_id="",
        amount=Decimal(1),
        dimension=Dimension.MODEL_CALLS,
        scope=BudgetScope.RUN,
        scope_id="r",
    )
    assert grant.reservation_id == ""


# -- what a delegated controller spent must reach this ledger ---------------
def test_a_delegated_spend_is_recorded_even_past_the_limit(
    runtime_db: Database,
) -> None:
    """The money is already gone; refusing to record it is the worse error.

    `propose_capsule_change` and the coding action call v1 controllers that own
    their own providers and never touch this ledger, so a cycle that made four
    model calls reported one and a run started with `--max-cost-usd 6` reported
    a tenth of what it had spent. Found by reading a pilot's run report next to
    its provider invocations; nothing asserted the two agreed.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="spend", repo_path="/tmp/spend")
    run = store.create_run(project_id="spend", objective="cost me something")
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("1.00"),
    )

    under = ledger.charge_all(
        dimension=Dimension.MODEL_COST_USD,
        amount=Decimal("0.40"),
        run_id=run.run_id,
        project_id="spend",
    )
    assert under == (), "0.40 of a 1.00 budget is not an overrun"
    budget = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert budget is not None and budget.spent == Decimal("0.40")

    # And past the limit it records the spend and *reports* the overrun.
    over = ledger.charge_all(
        dimension=Dimension.MODEL_COST_USD,
        amount=Decimal("0.90"),
        run_id=run.run_id,
        project_id="spend",
    )
    assert over, "an overrun must be reported, not swallowed"
    budget = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert budget is not None and budget.spent == Decimal("1.30")

    # The next reservation sees it, which is where the cap bites.
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve(
            scope=BudgetScope.RUN,
            scope_id=run.run_id,
            dimension=Dimension.MODEL_COST_USD,
            amount=Decimal("0.10"),
        )


def test_charging_a_dimension_with_no_budget_is_a_no_op(runtime_db: Database) -> None:
    """An absent budget means unlimited, here as everywhere else in this file."""

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="unbounded", repo_path="/tmp/unbounded")
    run = store.create_run(project_id="unbounded", objective="no limits set")
    ledger = BudgetLedger(runtime_db)

    assert (
        ledger.charge_all(
            dimension=Dimension.MODEL_CALLS,
            amount=7,
            run_id=run.run_id,
            project_id="unbounded",
        )
        == ()
    )
    assert (
        ledger.charge_all(
            dimension=Dimension.MODEL_CALLS,
            amount=0,
            run_id=run.run_id,
            project_id="unbounded",
        )
        == ()
    )
