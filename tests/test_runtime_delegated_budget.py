"""`--max-cost-usd X` must not knowingly authorise spending above X.

The runtime's own router has always been reserve → execute → reconcile, with
the reservation's ``where`` clause as the check, so two workers cannot both be
told there is room for the last call. It makes none of the calls inside a
delegated action: `propose_capsule_change` hands the work to
`ProposalController` and the coding action to `AutomationController`, both of
which own their own providers and had never touched the runtime's ledger.

The previous release measured the consequence on a real pilot. Twelve model
calls, 2.3888 USD, of which five rows totalling 1.6478 USD were invisible -- a
3.2x under-report on an ordinary cycle with no failures -- and a run started
with `--max-cost-usd 6` reporting a tenth of what it had spent. The answer then
was `BudgetLedger.charge_all`: record it afterwards, past the limit when it
must. That makes the ledger true and it is not a budget. A cap that bites on
the call *after* the overrun is a cap that authorised the overrun.

`research_os.runtime.spend` wraps the provider adapters instead, which is the
one chokepoint both delegated controllers already pass through, so the
reservation happens before the call and neither controller changes.

These tests are the four properties §4.4 of the brief asks for: the cap
refuses before the money is spent, one authority governs both delegated paths,
concurrent workers cannot spend the same final budget, and a crash leaves
conservative recoverable accounting.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Role
from research_os.automation.providers import InvocationRequest, InvocationResult
from research_os.errors import BudgetExceededError
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope, ReservationStatus
from research_os.runtime.spend import (
    DEFAULT_PER_CALL_CEILING_USD,
    BudgetedProvider,
    DelegatedSpendAuthority,
)
from research_os.runtime.store import RuntimeStore


@dataclass
class PricedProvider:
    """A provider that bills a fixed amount and counts how often it was asked."""

    name: str = "fake"
    family: str = "fake"
    price: float | None = 0.10
    calls: int = 0
    raises: bool = False

    def probe(self) -> Any:  # pragma: no cover - not exercised here
        raise AssertionError("probe must not be charged for")

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.calls += 1
        if self.raises:
            raise RuntimeError("the provider fell over")
        return InvocationResult(
            argv=("fake",),
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            text="{}",
            total_cost_usd=self.price,
        )


def request() -> InvocationRequest:
    return InvocationRequest(
        role=Role.PLANNER,
        prompt="p",
        cwd=Path("/tmp"),
        read_only=True,
        timeout_seconds=1,
    )


@pytest.fixture
def budgeted(runtime_db: Database) -> dict[str, Any]:
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="capped", repo_path="/tmp/capped")
    run = store.create_run(project_id="capped", objective="spend carefully")
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("1.00"),
    )
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    return {"db": runtime_db, "store": store, "run": run, "ledger": ledger}


def authority(budgeted: dict[str, Any], **kwargs: Any) -> DelegatedSpendAuthority:
    return DelegatedSpendAuthority(
        budgets=budgeted["ledger"],
        run_id=budgeted["run"].run_id,
        project_id="capped",
        action="propose_capsule_change",
        **kwargs,
    )


def budget(budgeted: dict[str, Any], dimension: Dimension) -> Any:
    return budgeted["ledger"].get(
        scope=BudgetScope.RUN, scope_id=budgeted["run"].run_id, dimension=dimension
    )


# -- the cap refuses before the money is spent --------------------------------


def test_the_cap_refuses_the_call_before_the_provider_is_asked(
    budgeted: dict[str, Any],
) -> None:
    """The whole difference between a budget and a report.

    A 1.00 USD limit and a 0.50 per-call ceiling authorises two calls. The
    third is refused, and the assertion that matters is `provider.calls == 2`:
    the money was not spent and then noticed.
    """

    provider = PricedProvider(price=0.50)
    wrapped = BudgetedProvider(inner=provider, authority=authority(budgeted))

    wrapped.invoke(request())
    wrapped.invoke(request())
    with pytest.raises(BudgetExceededError, match="refused a delegated"):
        wrapped.invoke(request())

    assert provider.calls == 2, (
        "the third call reached the provider, so the budget was a report and not a cap"
    )
    assert Decimal(budget(budgeted, Dimension.MODEL_COST_USD).spent) == Decimal("1.00")


def test_a_refusal_raises_what_the_controllers_already_handle(
    budgeted: dict[str, Any],
) -> None:
    """`BudgetExceededError` is an `AutomationError`, and that is load-bearing.

    `AutomationController._execute` catches `AutomationError` to fail the run
    terminally and clean up. A refusal raised as anything else would escape it
    and leave the run EXECUTING forever with a worktree nobody removes -- the
    same shape of defect the `SandboxError` clause in that handler exists to
    close.
    """

    from research_os.errors import AutomationError

    ledger = budgeted["ledger"]
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=budgeted["run"].run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(0),
    )
    wrapped = BudgetedProvider(inner=PricedProvider(), authority=authority(budgeted))
    with pytest.raises(AutomationError):
        wrapped.invoke(request())


def test_a_refusal_leaks_no_capacity(budgeted: dict[str, Any]) -> None:
    """The call reservation must not survive a refused cost reservation.

    Taking one dimension and failing the other is how a cost-exhausted run
    silently exhausts its call budget too and then misreports which one ran
    out. The runtime router learned it; this follows it and proves it.
    """

    ledger = budgeted["ledger"]
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=budgeted["run"].run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(0),
    )
    wrapped = BudgetedProvider(inner=PricedProvider(), authority=authority(budgeted))
    with pytest.raises(BudgetExceededError):
        wrapped.invoke(request())

    calls = budget(budgeted, Dimension.MODEL_CALLS)
    assert Decimal(calls.reserved) == 0
    assert Decimal(calls.spent) == 0
    held = ledger.held_reservations(budget_id=calls.budget_id)
    assert held == ()


def test_an_unused_reservation_is_released(budgeted: dict[str, Any]) -> None:
    """A cheap call must not consume the ceiling reserved for it."""

    wrapped = BudgetedProvider(
        inner=PricedProvider(price=0.01), authority=authority(budgeted)
    )
    wrapped.invoke(request())

    cost = budget(budgeted, Dimension.MODEL_COST_USD)
    assert Decimal(cost.spent) == Decimal("0.01")
    assert Decimal(cost.reserved) == 0, "the unused part of the hold was not released"


def test_a_provider_that_reports_no_cost_is_charged_for_the_call_only(
    budgeted: dict[str, Any],
) -> None:
    """A spend recorded as zero because the number was unavailable compounds."""

    wrapped = BudgetedProvider(
        inner=PricedProvider(price=None), authority=authority(budgeted)
    )
    wrapped.invoke(request())

    assert Decimal(budget(budgeted, Dimension.MODEL_CALLS).spent) == 1
    cost = budget(budgeted, Dimension.MODEL_COST_USD)
    assert Decimal(cost.spent) == 0
    assert Decimal(cost.reserved) == 0


def test_the_ceiling_ratchets_to_the_largest_observed_call(
    budgeted: dict[str, Any],
) -> None:
    """No provider quotes a price, so the first expensive call cannot be refused.

    What can be bounded is the *second*: a call that cost 0.90 raises the
    reservation for the next one from the 0.50 default to 0.90, so the overrun
    is bounded by one call rather than repeated for the length of the run.
    """

    auth = authority(budgeted)
    provider = PricedProvider(price=0.90)
    wrapped = BudgetedProvider(inner=provider, authority=auth)

    assert auth.largest_call_usd == 0
    wrapped.invoke(request())
    assert auth.largest_call_usd == Decimal("0.90")
    assert auth.largest_call_usd > DEFAULT_PER_CALL_CEILING_USD

    # 1.00 limit, 0.90 already spent, and the next reservation now asks for
    # 0.90 rather than 0.50 -- so it is refused instead of authorising a second
    # call that would take the run to 1.80.
    with pytest.raises(BudgetExceededError):
        wrapped.invoke(request())
    assert provider.calls == 1


def test_probing_is_never_charged(budgeted: dict[str, Any]) -> None:
    """`runtime doctor` asks every provider whether it exists. That is free."""

    calls: list[str] = []

    @dataclass
    class ProbeOnly:
        name: str = "fake"
        family: str = "fake"

        def probe(self) -> str:
            calls.append("probed")
            return "ok"

        def invoke(self, request: InvocationRequest) -> InvocationResult:
            raise AssertionError("not called")

    wrapped = BudgetedProvider(inner=ProbeOnly(), authority=authority(budgeted))
    assert wrapped.probe() == "ok"
    assert calls == ["probed"]
    assert Decimal(budget(budgeted, Dimension.MODEL_CALLS).spent) == 0


# -- concurrency ---------------------------------------------------------------


def test_two_delegated_workers_cannot_spend_the_same_final_budget(
    budgeted: dict[str, Any],
) -> None:
    """Separate authorities, separate connections, one budget.

    This is the property a counter incremented after the spend cannot have, and
    the reason the reservation's `where` clause is the check. Each worker gets
    its own `DelegatedSpendAuthority`, exactly as two concurrent cycles would.
    """

    ledger = budgeted["ledger"]
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=budgeted["run"].run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("0.50"),
    )
    providers = [PricedProvider(price=0.50) for _ in range(6)]

    def attempt(provider: PricedProvider) -> str:
        wrapped = BudgetedProvider(inner=provider, authority=authority(budgeted))
        try:
            wrapped.invoke(request())
        except BudgetExceededError:
            return "refused"
        return "spent"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, providers))

    assert outcomes.count("spent") == 1, outcomes
    assert sum(item.calls for item in providers) == 1, (
        "more than one worker reached a provider with 0.50 left and a 0.50 ceiling"
    )
    assert Decimal(budget(budgeted, Dimension.MODEL_COST_USD).spent) == Decimal("0.50")


# -- crash behaviour ------------------------------------------------------------


def test_a_worker_that_dies_mid_call_leaves_the_capacity_held(
    budgeted: dict[str, Any],
) -> None:
    """Pessimism in the crash window is the right direction of error for money.

    A reservation taken and never settled keeps `available` low, so a second
    worker does not spend money the first may already have spent. It is
    recoverable rather than lost: `reconcile_stale` releases it once it is old
    enough to be certainly abandoned.
    """

    auth = authority(budgeted)
    grant = auth.authorize()
    del grant  # the process dies here, holding it

    cost = budget(budgeted, Dimension.MODEL_COST_USD)
    assert Decimal(cost.reserved) == DEFAULT_PER_CALL_CEILING_USD
    assert Decimal(cost.spent) == 0

    released = budgeted["ledger"].reconcile_stale(older_than_seconds=-1)
    assert released == 2, "both dimensions' holds should be reclaimed"
    after = budget(budgeted, Dimension.MODEL_COST_USD)
    assert Decimal(after.reserved) == 0
    assert Decimal(after.spent) == 0, (
        "an abandoned reservation must be released, not charged: assuming the "
        "spend happened would bill for work that may never have been done"
    )


def test_a_provider_that_raises_counts_the_call_and_invents_no_cost(
    budgeted: dict[str, Any],
) -> None:
    """Exactly what `ModelRouter.complete` does on the same event."""

    auth = authority(budgeted)
    wrapped = BudgetedProvider(inner=PricedProvider(raises=True), authority=auth)
    with pytest.raises(RuntimeError, match="fell over"):
        wrapped.invoke(request())

    assert Decimal(budget(budgeted, Dimension.MODEL_CALLS).spent) == 1
    cost = budget(budgeted, Dimension.MODEL_COST_USD)
    assert Decimal(cost.spent) == 0
    assert Decimal(cost.reserved) == 0
    assert auth.settled_calls == 1
    assert auth.settled_usd == 0


def test_every_reservation_ends_settled_or_released(
    budgeted: dict[str, Any],
) -> None:
    """Never neither. A HELD row with no owner is capacity nobody can use."""

    auth = authority(budgeted)
    wrapped = BudgetedProvider(inner=PricedProvider(price=0.10), authority=auth)
    wrapped.invoke(request())
    wrapped.invoke(request())

    for dimension in (Dimension.MODEL_CALLS, Dimension.MODEL_COST_USD):
        record = budget(budgeted, dimension)
        assert budgeted["ledger"].held_reservations(budget_id=record.budget_id) == ()
        with budgeted["db"].tx() as conn:
            statuses = conn.execute(
                "select distinct status from budget_reservations where budget_id = %s",
                (record.budget_id,),
            ).fetchall()
        assert {row["status"] for row in statuses} <= {
            str(ReservationStatus.SETTLED),
            str(ReservationStatus.RELEASED),
        }


# -- the reconciliation does not double-count ----------------------------------


def test_the_backstop_charges_only_what_the_authority_missed(
    budgeted: dict[str, Any], tmp_path: Path
) -> None:
    """Two mechanisms over one spend must not both charge it.

    `charge_delegated_spend` was the whole of delegated accounting and is now
    the reconciliation for calls the authority never saw. If it still charged
    the total, every delegated call would be billed twice and a 6 USD run would
    stop at 3.
    """

    from research_os.runtime.actions.base import charge_delegated_spend
    from tests.runtime_graph_helpers import make_context, make_router

    auth = authority(budgeted)
    wrapped = BudgetedProvider(inner=PricedProvider(price=0.10), authority=auth)
    wrapped.invoke(request())
    wrapped.invoke(request())
    assert auth.settled_calls == 2
    assert auth.settled_usd == Decimal("0.20")

    context = make_context(
        db=budgeted["db"],
        repo=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        dsn="",
        models=make_router(
            db=budgeted["db"],
            artifacts_root=tmp_path / "artifacts",
            run_id=budgeted["run"].run_id,
            project_id="capped",
            answers={},
        ),
        permitted=(),
    )

    @dataclass
    class Invocation:
        provider: str = "fake"
        role: str = "planner"
        ok: bool = True
        model: str = "fake-1"
        total_cost_usd: float = 0.10

    charge_delegated_spend(
        {
            "run_id": budgeted["run"].run_id,
            "project_id": "capped",
        },
        context,
        [Invocation(), Invocation()],
        action="propose_capsule_change",
        authority=auth,
    )

    assert Decimal(budget(budgeted, Dimension.MODEL_COST_USD).spent) == Decimal("0.20")
    assert Decimal(budget(budgeted, Dimension.MODEL_CALLS).spent) == 2
    # And the provenance rows are still written, which is the job the
    # reconciliation kept.
    rows = budgeted["store"].list_model_calls(run_id=budgeted["run"].run_id)
    assert len(rows) == 2
    assert {row.prompt_version for row in rows} == {"delegated:propose_capsule_change"}


def test_a_call_the_authority_never_saw_is_still_charged(
    budgeted: dict[str, Any], tmp_path: Path
) -> None:
    """The backstop is not decoration.

    A controller that invokes a provider it did not get from the wrapped
    registry produces exactly this, and it must not be free.
    """

    from research_os.runtime.actions.base import charge_delegated_spend
    from tests.runtime_graph_helpers import make_context, make_router

    auth = authority(budgeted)
    context = make_context(
        db=budgeted["db"],
        repo=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        dsn="",
        models=make_router(
            db=budgeted["db"],
            artifacts_root=tmp_path / "artifacts",
            run_id=budgeted["run"].run_id,
            project_id="capped",
            answers={},
        ),
        permitted=(),
    )

    @dataclass
    class Invocation:
        provider: str = "fake"
        role: str = "planner"
        ok: bool = True
        model: str = "fake-1"
        total_cost_usd: float = 0.30

    charge_delegated_spend(
        {"run_id": budgeted["run"].run_id, "project_id": "capped"},
        context,
        [Invocation()],
        action="propose_capsule_change",
        authority=auth,
    )
    assert Decimal(budget(budgeted, Dimension.MODEL_COST_USD).spent) == Decimal("0.30")


# -- the objective's cap, across the cycles of one objective -------------------


def test_a_successor_cycle_inherits_the_objectives_explicit_cap(
    runtime_db: Database, tmp_path: Any
) -> None:
    """Measured on a real objective before it was a test.

    `researchctl runtime start --max-cost-usd 6` set a run-scope limit on the
    run it created. A successor cycle is a different run, and
    `apply_default_budgets` overwrote its limits with the configuration
    defaults unconditionally -- so cycle 0 held 6 and cycle 1 held 25. Over
    `max_cycles_per_objective = 12` that is an exposure of
    `6 + 11 x 25 = 281 USD` against a number the researcher typed as 6.

    Observed on 2026-09-17 on `cg-sparse-regression`:
    `run:RRUN-...-6e916d0b model_cost_usd available 5.80` and
    `run:RRUN-...-42c57607 model_cost_usd available 24.39`.
    """

    from research_os.runtime.cycles import apply_default_budgets
    from tests.runtime_graph_helpers import make_config

    config = make_config(dsn="", artifacts_root=tmp_path / "artifacts")
    ledger = BudgetLedger(runtime_db)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="capped-objective", repo_path=str(tmp_path))
    parent = store.create_run(project_id="capped-objective", objective="o")

    # What the CLI does: the explicit cap, then the defaults which must not
    # raise it.
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=parent.run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(6),
    )
    apply_default_budgets(
        ledger,
        config=config,
        run_id=parent.run_id,
        project_id="capped-objective",
        inherit_from_run_id=parent.run_id,
    )
    parent_limit = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=parent.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert parent_limit is not None
    assert Decimal(parent_limit.limit_value) == Decimal(6), (
        "the defaults overwrote the researcher's explicit cap"
    )

    # And the successor.
    child = store.create_run(
        project_id="capped-objective",
        objective="o",
        parent_run_id=parent.run_id,
        cycle_index=1,
    )
    apply_default_budgets(
        ledger,
        config=config,
        run_id=child.run_id,
        project_id="capped-objective",
        inherit_from_run_id=parent.run_id,
    )
    child_limit = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=child.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert child_limit is not None
    assert Decimal(child_limit.limit_value) == Decimal(6), (
        f"the successor got {child_limit.limit_value} rather than the objective's 6"
    )


def test_the_project_ceiling_follows_the_objectives_cap(
    runtime_db: Database, tmp_path: Any
) -> None:
    """A 6 USD objective must not buy a 300 USD project ceiling.

    The ceiling exists to bound an objective, and deriving it from the
    configuration default rather than from the researcher's number made it
    bound the wrong thing.
    """

    from research_os.runtime.cycles import apply_default_budgets
    from tests.runtime_graph_helpers import make_config

    config = make_config(dsn="", artifacts_root=tmp_path / "artifacts")
    ledger = BudgetLedger(runtime_db)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="ceiling-project", repo_path=str(tmp_path))
    run = store.create_run(project_id="ceiling-project", objective="o")

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(6),
    )
    apply_default_budgets(
        ledger,
        config=config,
        run_id=run.run_id,
        project_id="ceiling-project",
        inherit_from_run_id=run.run_id,
    )
    ceiling = ledger.get(
        scope=BudgetScope.PROJECT,
        scope_id="ceiling-project",
        dimension=Dimension.MODEL_COST_USD,
    )
    assert ceiling is not None
    expected = Decimal(6) * Decimal(config.settings.max_cycles_per_objective)
    assert Decimal(ceiling.limit_value) == expected
    assert Decimal(ceiling.limit_value) < Decimal(300)


def test_applying_the_defaults_twice_does_not_reset_a_cap(
    runtime_db: Database, tmp_path: Any
) -> None:
    """`set_limit` is an upsert, so a second call would silently widen a run.

    Reachable on any path that opens a run and then re-applies budgets -- a
    resume, a reconciler, a future caller. The check that prevents it is not
    decoration.
    """

    from research_os.runtime.cycles import apply_default_budgets
    from tests.runtime_graph_helpers import make_config

    config = make_config(dsn="", artifacts_root=tmp_path / "artifacts")
    ledger = BudgetLedger(runtime_db)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="twice-project", repo_path=str(tmp_path))
    run = store.create_run(project_id="twice-project", objective="o")

    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(2),
    )
    for _ in range(3):
        apply_default_budgets(
            ledger, config=config, run_id=run.run_id, project_id="twice-project"
        )
    limit = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert limit is not None
    assert Decimal(limit.limit_value) == Decimal(2)


def test_an_unconstrained_objective_still_gets_the_defaults(
    runtime_db: Database, tmp_path: Any
) -> None:
    """The fix must not turn "no cap given" into "no budget"."""

    from research_os.runtime.cycles import apply_default_budgets
    from tests.runtime_graph_helpers import make_config

    config = make_config(dsn="", artifacts_root=tmp_path / "artifacts")
    ledger = BudgetLedger(runtime_db)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="default-project", repo_path=str(tmp_path))
    run = store.create_run(project_id="default-project", objective="o")
    apply_default_budgets(
        ledger,
        config=config,
        run_id=run.run_id,
        project_id="default-project",
        inherit_from_run_id=run.run_id,
    )
    limit = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert limit is not None
    assert Decimal(limit.limit_value) == Decimal(str(config.budget.max_model_cost_usd))
