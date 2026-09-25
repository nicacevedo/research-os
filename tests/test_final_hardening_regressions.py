"""The final micro-hardening before unattended qualification, and what holds instead.

Each section is one residue the pre-qualification review reproduced and
deliberately left for this pass, run through production code, with the
assertion turned round and a control beside it so the repair cannot pass by
over-correcting.

- **A1. A budget is an authority boundary.** A call reserved the provider
  profile's 0.05 estimate whatever ceiling it declared, so a project with 0.06
  left could start a 2.50 call; and a lineage one cent under its ceiling was
  sold a stage per free slot in one tick, each judged against the same
  pre-tick sum. A call now reserves its whole declared ceiling against run,
  project, system, idea and lineage before it starts, and the provider is told
  to stop at the same number; the allocator charges each sale as it makes it.
- **A4. A remainder that cannot authorise a call is spent.** A portfolio with
  less left than its cheapest call reported RUNNING and bought work the ledger
  then refused.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Role
from research_os.automation.providers import InvocationRequest, InvocationResult
from research_os.portfolio import allocation
from research_os.portfolio.config import Bounds, load_config
from research_os.portfolio.models import (
    ActionStatus,
    IdeaStatus,
    OperationalState,
    PortfolioStatus,
    QualityDimensions,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.track import advance_idea
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetExhaustedError, BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.interfaces import (
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelRole,
)
from research_os.runtime.models import BudgetScope, ModelCallStatus
from research_os.runtime.routing import (
    CallCeilingReachedError,
    ModelRouter,
    ProviderProfile,
)
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_prequalification_regressions import _succeed
from tests.test_portfolio_tick import _tick
from tests.test_portfolio_track import checkpoint_tables

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

ROLES = ("planner", "reviewer", "analyst", "coder", "literature")


# =============================================================================
# A1. No call starts without its whole ceiling reserved in every scope.
# =============================================================================
class ObservingProvider(FakeProvider):
    """A fake that records what the ledger held at the moment it was invoked.

    "Reserved before the call starts" is a claim about ordering, and the only
    way to test an ordering is to look from inside the call.
    """

    def __init__(self, db: Database, project_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._db = db
        self._project_id = project_id
        self.held_at_invoke: list[Decimal] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        record = BudgetLedger(self._db).get(
            scope=BudgetScope.PROJECT,
            scope_id=self._project_id,
            dimension=Dimension.MODEL_COST_USD,
        )
        self.held_at_invoke.append(Decimal(record.reserved) if record else Decimal(-1))
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the call"
        return super().invoke(request)


def _responses(*items: ScriptedResponse) -> dict[str, list[ScriptedResponse]]:
    return {role: list(items) for role in ROLES}


def _router(
    db: Database,
    tmp_path: Path,
    *,
    project_id: str,
    provider: FakeProvider,
) -> ModelRouter:
    store = RuntimeStore(db)
    return ModelRouter(
        adapters={provider.name: provider},  # type: ignore[dict-item]
        # The default 0.05 estimate `profiles_from_adapters` builds: the number
        # the router used to reserve whatever the request declared.
        profiles=(ProviderProfile(name=provider.name, family="a", tier=3),),
        store=store,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        budgets=BudgetLedger(db),
        run_id=store.create_run(project_id=project_id, objective="idea-track").run_id,
        project_id=project_id,
        failure_threshold=1_000,
    )


def _request(
    *, max_cost: str | None, scopes: tuple[tuple[str, str], ...] = ()
) -> ModelRequest:
    return ModelRequest(
        role=ModelRole.FALSIFIER,
        capability=Capability.CRITIQUE,
        prompt="try to kill this",
        prompt_version="falsifier@1",
        criticality=Criticality.NORMAL,
        independence=Independence.DIFFERENT_CONTEXT,
        json_schema={"type": "object"},
        max_cost_usd=float(max_cost) if max_cost is not None else None,
        budget_scopes=scopes,
    )


def _ceiling(
    db: Database,
    scope: BudgetScope,
    scope_id: str,
    usd: str,
    *,
    spent: str = "0",
) -> BudgetLedger:
    ledger = BudgetLedger(db)
    ledger.set_limit(
        scope=scope,
        scope_id=scope_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(usd),
        explicit=scope is BudgetScope.PROJECT,
        opening_spent=Decimal(spent),
    )
    return ledger


def _budget(ledger: BudgetLedger, scope: BudgetScope, scope_id: str) -> Any:
    record = ledger.get(
        scope=scope, scope_id=scope_id, dimension=Dimension.MODEL_COST_USD
    )
    assert record is not None
    return record


def _ok(cost: float) -> ScriptedResponse:
    return ScriptedResponse(structured={"ok": True}, total_cost_usd=cost)


def test_a_call_whose_ceiling_exceeds_the_remaining_authority_never_starts(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """0.20 left under an explicit ceiling; the call declares 0.40.

    The router reserved 0.05 here, the 0.05 fitted, and the provider was
    invoked -- for a call allowed to cost eight times what was left.
    """

    ledger = _ceiling(
        runtime_db, BudgetScope.PROJECT, runtime_project, "1.00", spent="0.80"
    )
    provider = FakeProvider(name="one", family="a", responses=_responses(_ok(0.10)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    with pytest.raises(BudgetExhaustedError):
        router.complete(_request(max_cost="0.40"))

    assert provider.calls == [], "the provider was invoked without the authority"
    budget = _budget(ledger, BudgetScope.PROJECT, runtime_project)
    assert budget.spent == Decimal("0.80")
    assert budget.reserved == 0
    # Nothing happened, so nothing is recorded as having happened.
    assert RuntimeStore(runtime_db).list_model_calls(limit=10) == ()


def test_a_call_that_fits_the_remaining_authority_runs(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The control: the same remainder authorises a call that fits in it."""

    ledger = _ceiling(
        runtime_db, BudgetScope.PROJECT, runtime_project, "1.00", spent="0.80"
    )
    provider = FakeProvider(name="one", family="a", responses=_responses(_ok(0.10)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    response = router.complete(_request(max_cost="0.20"))

    assert response.ok
    assert len(provider.calls) == 1
    assert _budget(ledger, BudgetScope.PROJECT, runtime_project).spent == Decimal(
        "0.90"
    )


def test_the_whole_ceiling_is_held_while_the_call_runs_and_the_provider_is_capped(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """Reserved before it starts, and the provider told the same number."""

    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "5.00")
    provider = ObservingProvider(
        runtime_db,
        runtime_project,
        name="one",
        family="a",
        responses=_responses(_ok(0.13)),
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    router.complete(_request(max_cost="0.40"))

    assert provider.held_at_invoke == [Decimal("0.40")]
    assert provider.calls[0].max_budget_usd == pytest.approx(0.40)
    # And settled at what it cost, not at what it was allowed to cost.
    budget = _budget(ledger, BudgetScope.PROJECT, runtime_project)
    assert budget.spent == Decimal("0.13")
    assert budget.reserved == 0


def test_a_request_that_declares_no_ceiling_is_not_capped(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The documented residual, pinned: no declared ceiling, no invented one."""

    provider = FakeProvider(name="one", family="a", responses=_responses(_ok(0.02)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    router.complete(_request(max_cost=None))

    assert provider.calls[0].max_budget_usd is None


def test_a_call_the_provider_stopped_at_its_ceiling_is_charged_and_is_not_an_outage(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """`error_max_budget_usd`: billed, terminal, and not the provider's fault.

    Read as an outage it would be retried -- spending the ceiling again to stop
    at the same place -- and would cool a healthy provider down for everyone.
    """

    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "5.00")
    provider = FakeProvider(
        name="one",
        family="a",
        responses=_responses(
            ScriptedResponse(
                structured=None,
                exit_code=1,
                error="error_max_budget_usd",
                total_cost_usd=0.43,
                budget_exhausted=True,
            )
        ),
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    with pytest.raises(CallCeilingReachedError) as caught:
        router.complete(_request(max_cost="0.40"))

    assert isinstance(caught.value, BudgetExhaustedError)
    budget = _budget(ledger, BudgetScope.PROJECT, runtime_project)
    # The overrun of one response is recorded, not hidden.
    assert budget.spent == Decimal("0.43")
    assert budget.reserved == 0
    (call,) = RuntimeStore(runtime_db).list_model_calls(limit=10)
    assert call.status is ModelCallStatus.FAILED
    assert Decimal(str(call.cost_usd)) == Decimal("0.43")
    health = {
        item.provider: item for item in RuntimeStore(runtime_db).provider_health()
    }
    assert "one" not in health or health["one"].consecutive_failures == 0


def test_a_refused_call_leaves_no_spend_and_no_hold_in_any_scope(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The lineage refuses; run, project, system and idea must all be untouched."""

    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "5.00")
    _ceiling(runtime_db, BudgetScope.SYSTEM, "system", "50.00")
    _ceiling(runtime_db, BudgetScope.IDEA, "PIDEA-x", "8.00")
    _ceiling(runtime_db, BudgetScope.LINEAGE, "PIDEA-root", "0.30")
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_CALLS,
        limit_value=10,
    )
    provider = FakeProvider(name="one", family="a", responses=_responses(_ok(0.10)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    with pytest.raises(BudgetExhaustedError) as caught:
        router.complete(
            _request(
                max_cost="0.40",
                scopes=(("idea", "PIDEA-x"), ("lineage", "PIDEA-root")),
            )
        )

    assert caught.value.scope is BudgetScope.LINEAGE
    assert provider.calls == []
    for scope, scope_id in (
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.SYSTEM, "system"),
        (BudgetScope.IDEA, "PIDEA-x"),
        (BudgetScope.LINEAGE, "PIDEA-root"),
    ):
        record = _budget(ledger, scope, scope_id)
        assert (record.spent, record.reserved) == (0, 0), scope
    calls = ledger.get(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_CALLS,
    )
    assert calls is not None and (calls.spent, calls.reserved) == (0, 0)


def test_the_reported_cost_is_what_every_scope_is_charged(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "5.00")
    _ceiling(runtime_db, BudgetScope.IDEA, "PIDEA-x", "8.00")
    _ceiling(runtime_db, BudgetScope.LINEAGE, "PIDEA-root", "40.00")
    provider = FakeProvider(name="one", family="a", responses=_responses(_ok(0.137)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )

    router.complete(
        _request(
            max_cost="0.40",
            scopes=(("idea", "PIDEA-x"), ("lineage", "PIDEA-root")),
        )
    )

    for scope, scope_id in (
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.IDEA, "PIDEA-x"),
        (BudgetScope.LINEAGE, "PIDEA-root"),
    ):
        record = _budget(ledger, scope, scope_id)
        assert record.spent == Decimal("0.137"), scope
        assert record.reserved == 0, scope


def test_concurrent_calls_cannot_oversubscribe_one_lineage(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """Two stages of one lineage, 1.00 left, each call allowed 0.60.

    The first is held inside the provider while the second asks. The ledger's
    ``where`` clause is the check, so the second is refused rather than told
    the remainder is also its own.
    """

    ledger = _ceiling(runtime_db, BudgetScope.LINEAGE, "PIDEA-root", "1.00")
    scopes = (("lineage", "PIDEA-root"),)
    first = ObservingProvider(
        runtime_db,
        runtime_project,
        name="one",
        family="a",
        responses=_responses(_ok(0.5)),
    )
    first.gate = threading.Event()
    second = FakeProvider(name="one", family="a", responses=_responses(_ok(0.5)))
    router_a = _router(runtime_db, tmp_path, project_id=runtime_project, provider=first)
    router_b = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=second
    )

    errors: list[BaseException] = []

    def run_first() -> None:
        try:
            router_a.complete(_request(max_cost="0.60", scopes=scopes))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert first.entered.wait(10)
    try:
        with pytest.raises(BudgetExhaustedError):
            router_b.complete(_request(max_cost="0.60", scopes=scopes))
    finally:
        first.gate.set()
        worker.join(10)

    assert errors == []
    assert second.calls == []
    record = _budget(ledger, BudgetScope.LINEAGE, "PIDEA-root")
    assert record.spent == Decimal("0.5")
    assert record.reserved == 0


# -------------------------------------------------------- the allocator --
def _candidate(
    store: PortfolioStore,
    project: str,
    *,
    lineage: str,
    stage: Stage = Stage.FALSIFY,
    spent: str = "0",
    lineage_spent: str = "0",
    title: str,
) -> allocation.Candidate:
    idea, _version = seed_idea(store, project, title=title)
    shared = idea.model_copy(update={"lineage_root": lineage})
    return allocation.Candidate(
        idea=shared,
        dimensions=QualityDimensions(),
        diversity=allocation.diversity_key(
            lineage_root=lineage, adjudication=[], research_question=title
        ),
        stage=stage,
        reason="test",
        expected_cost=Decimal("0.40"),
        idle_seconds=0.0,
        open_objections=0,
        spent=Decimal(spent),
        lineage_spent=Decimal(lineage_spent),
    )


def _plan(
    candidates: list[allocation.Candidate],
    *,
    lineage_ceiling: str = "40.00",
    authority: allocation.SaleAuthority | None = None,
) -> tuple[allocation.Allocation, ...]:
    config = load_config()
    config = config.model_copy(
        update={
            "bounds": Bounds(
                lineage_spend_ceiling_usd=Decimal(lineage_ceiling),
                max_active_per_lineage=10,
            )
        }
    )
    return allocation.plan(
        candidates=candidates,
        config=config,
        free_slots=len(candidates),
        lineage_in_flight={},
        candidate_pool=100,
        pending_seeds=0,
        origin_counts={},
        minable_failures=0,
        tick_bucket="t",
        may_explore=False,
        authority=authority,
    )


def test_one_tick_cannot_sell_a_lineage_past_its_ceiling(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """0.50 committed of 1.00; three falsifications at 0.40 a call.

    Each was tested against the same 0.50 and all three were sold -- 1.70 of
    authority for a lineage with 0.50 left. One fits.
    """

    candidates = [
        _candidate(
            portfolio, runtime_project, lineage="L", lineage_spent="0.50", title=name
        )
        for name in ("first", "second", "third")
    ]

    sold = _plan(candidates, lineage_ceiling="1.00")

    assert len([item for item in sold if item.kind == allocation.ADVANCE_IDEA]) == 1


def test_a_lineage_with_room_is_sold_what_fits(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The control: the same three, with room for all of them."""

    candidates = [
        _candidate(portfolio, runtime_project, lineage="L", title=name)
        for name in ("first", "second", "third")
    ]

    assert len(_plan(candidates, lineage_ceiling="1.20")) == 3
    assert len(_plan(candidates, lineage_ceiling="1.19")) == 2


def test_one_tick_cannot_sell_past_the_projects_remaining_authority(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    candidates = [
        _candidate(portfolio, runtime_project, lineage=name, title=name)
        for name in ("first", "second", "third")
    ]

    tight = _plan(
        candidates, authority=allocation.SaleAuthority(cost_usd=Decimal("0.90"))
    )
    roomy = _plan(
        candidates, authority=allocation.SaleAuthority(cost_usd=Decimal("1.20"))
    )
    calls = _plan(candidates, authority=allocation.SaleAuthority(calls=1))

    assert len(tight) == 2
    assert len(roomy) == 3
    assert len(calls) == 1


def test_an_idea_whose_next_call_does_not_fit_its_own_ceiling_is_not_sold(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """7.70 of 8.00 spent; its next stage may cost 0.40 a call."""

    near = _candidate(
        portfolio, runtime_project, lineage="A", spent="7.70", title="near"
    )
    free = _candidate(
        portfolio,
        runtime_project,
        lineage="B",
        spent="7.70",
        stage=Stage.ADJUDICATE,
        title="free",
    )

    sold = _plan([near, free])

    assert [item.idea_id for item in sold] == [free.idea.idea_id]


# ------------------------------------------------------ a stage, end to end --
def _screen_router(
    db: Database, tmp_path: Path, project_id: str
) -> tuple[ModelRouter, FakeProvider]:
    provider = FakeProvider(
        name="one",
        family="a",
        responses=_responses(
            ScriptedResponse(
                structured={"likely_known": False, "rationale": "nothing close"},
                total_cost_usd=0.02,
            )
        ),
    )
    return _router(db, tmp_path, project_id=project_id, provider=provider), provider


def _advance_with(
    store: PortfolioStore,
    db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
    router: ModelRouter,
) -> Any:
    return advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=db,
        project_id=project,
        idea_id=idea_id,
        models=router,
        charter="Understand sparse regression solvers.",
        problem="Do CG and working sets coincide?",
    )


def _screen_next(store: PortfolioStore, project: str) -> str:
    idea, version = seed_idea(store, project)
    _succeed(store, idea.idea_id, version.version, Stage.DEDUP)
    return idea.idea_id


def test_a_stage_its_lineage_cannot_cover_blocks_on_budget_and_calls_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
    checkpoint_tables: str,
) -> None:
    """The lineage has 0.10 of authority; the novelty screen may cost 0.25.

    It used to reach the provider, and a refusal from the ledger -- had there
    been one -- fell to the catch-all as UNKNOWN and left the idea IDLE for
    the next tick to buy the same refused stage again.
    """

    idea_id = _screen_next(portfolio, runtime_project)
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"lineage_spend_ceiling_usd": "0.10"}
    )
    router, provider = _screen_router(runtime_db, tmp_path, runtime_project)

    with pytest.raises(BudgetExhaustedError):
        _advance_with(
            portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router
        )

    assert provider.calls == []
    idea = portfolio.require_idea(idea_id)
    assert idea.operational_state is OperationalState.BLOCKED_BUDGET
    assert idea.status is IdeaStatus.CANDIDATE, "a budget is not a verdict"
    (action,) = [
        item
        for item in portfolio.list_actions(idea_id=idea_id)
        if item.stage is Stage.NOVELTY_SCREEN
    ]
    assert action.status is ActionStatus.FAILED
    assert action.failure_class == "budget_exhausted"
    lineage = _budget(BudgetLedger(runtime_db), BudgetScope.LINEAGE, idea.lineage_root)
    assert lineage.limit_value == Decimal("0.10")
    assert (lineage.spent, lineage.reserved) == (0, 0)


def test_a_stage_its_lineage_can_cover_runs_and_is_charged_to_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
    checkpoint_tables: str,
) -> None:
    """The control, and the other half of the claim: the spend lands on both."""

    idea_id = _screen_next(portfolio, runtime_project)
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"lineage_spend_ceiling_usd": "1.00"}
    )
    router, provider = _screen_router(runtime_db, tmp_path, runtime_project)

    result = _advance_with(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router
    )

    assert result.ok, result.detail
    assert len(provider.calls) == 1
    idea = portfolio.require_idea(idea_id)
    ledger = BudgetLedger(runtime_db)
    for scope, scope_id in (
        (BudgetScope.IDEA, idea_id),
        (BudgetScope.LINEAGE, idea.lineage_root),
    ):
        record = _budget(ledger, scope, scope_id)
        assert record.spent == Decimal("0.02"), scope
        assert record.reserved == 0, scope


def test_a_lineage_that_spent_before_it_had_a_row_opens_with_that_spend(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
    checkpoint_tables: str,
) -> None:
    """Spend recorded on actions before `sql/0036` is not handed back."""

    idea_id = _screen_next(portfolio, runtime_project)
    version = portfolio.require_version(idea_id).version
    old = portfolio.open_action(
        idea_id=idea_id,
        idea_version=version,
        stage=Stage.FALSIFY,
        basis_digest="legacy-basis",
    )
    portfolio.complete_action(
        action_id=old.action_id,
        status=ActionStatus.SUCCEEDED,
        cost_usd=Decimal("0.95"),
    )
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"lineage_spend_ceiling_usd": "1.00"}
    )
    router, provider = _screen_router(runtime_db, tmp_path, runtime_project)

    with pytest.raises(BudgetExhaustedError):
        _advance_with(
            portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router
        )

    assert provider.calls == []


# =============================================================================
# A4. A remainder that cannot authorise the cheapest call is spent.
# =============================================================================
def _project_spent(db: Database, project: str, *, limit: str, spent: str) -> None:
    _ceiling(db, BudgetScope.PROJECT, project, limit, spent=spent)


def test_a_remainder_below_the_cheapest_call_pauses_the_portfolio(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """0.10 left, and nothing this portfolio calls may cost less than 0.25.

    It reported RUNNING and bought work every tick for the ledger to refuse.
    """

    seed_idea(portfolio, runtime_project)
    _project_spent(runtime_db, runtime_project, limit="1.00", spent="0.90")

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert report.status is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED
    assert report.work_enqueued == 0
    assert "cheapest call" in " ".join(report.notes)
    state = portfolio.get_state(runtime_project)
    assert state is not None
    assert state.status is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED


def test_a_remainder_that_covers_the_cheapest_call_keeps_it_running(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    seed_idea(portfolio, runtime_project)
    _project_spent(runtime_db, runtime_project, limit="1.00", spent="0.70")

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert report.status is PortfolioStatus.RUNNING
    assert report.work_enqueued >= 1


def test_the_cheapest_call_is_the_smallest_declared_ceiling() -> None:
    config = load_config()
    expected = min(
        [value for value in config.stage_cost_usd.values() if value > 0]
        + [config.explorer_cost_usd]
    )
    assert allocation.cheapest_call(config) == expected
    assert (
        allocation.call_ceiling(allocation.ADVANCE_IDEA, Stage.ADJUDICATE, config) == 0
    )


def test_a_tick_does_not_sell_what_the_queue_has_already_bought(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Bought, not started: nothing is held for it yet, and it still counts.

    0.90 left. The first tick buys an explorer (0.60) and a screen (0.25).
    Before either starts, a second tick sees the same ledger -- nothing is
    reserved until a call begins -- and would buy a second screen against
    authority the first tick had already committed.
    """

    seed_idea(portfolio, runtime_project)
    _project_spent(runtime_db, runtime_project, limit="2.00", spent="1.10")

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert sorted(item.kind for item in first.allocations) == [
        allocation.ADVANCE_IDEA,
        allocation.EXPLORE,
    ]
    assert first.work_enqueued == 2

    later, _ = seed_idea(portfolio, runtime_project, title="another direction")
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert later.idea_id not in {item.idea_id for item in second.allocations}


def test_a_tick_with_room_for_both_rounds_buys_both(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The control: with authority for the second screen, it is bought."""

    seed_idea(portfolio, runtime_project)
    _project_spent(runtime_db, runtime_project, limit="2.00", spent="0.80")

    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    later, _ = seed_idea(portfolio, runtime_project, title="another direction")
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert later.idea_id in {item.idea_id for item in second.allocations}


def test_the_adapter_passes_the_ceiling_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--max-budget-usd``, spelled from the reservation, never in exponent form."""

    import subprocess

    from research_os.automation.providers import ClaudeCodeProvider

    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv,
            1,
            '{"is_error": true, "subtype": "error_max_budget_usd", '
            '"total_cost_usd": 0.41, "usage": {}}',
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = ClaudeCodeProvider().invoke(
        InvocationRequest(
            role=Role.REVIEWER,
            prompt="p",
            cwd=Path("/tmp"),
            read_only=True,
            timeout_seconds=5,
            max_budget_usd=0.4,
        )
    )

    argv = seen[0]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.4"
    assert result.budget_exhausted is True
    assert result.total_cost_usd == pytest.approx(0.41)
    assert not result.ok


# =============================================================================
# A2. No scientific reader follows a link out of the directory it reads.
# =============================================================================
SECRET = '{"host_secret_key": 1, "another_private_field": 2}'


def _host_secret(tmp_path: Path) -> Path:
    outside = tmp_path / "host"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.json"
    secret.write_text(SECRET, encoding="utf-8")
    return secret


@pytest.fixture
def box(tmp_path: Path) -> Path:
    """A directory a run could write, with one file it did write."""

    root = tmp_path / "box"
    (root / "results").mkdir(parents=True)
    (root / "results" / "run.json").write_text('{"metric": 0.5}', encoding="utf-8")
    return root


def test_a_file_written_in_the_directory_is_read(box: Path) -> None:
    from research_os.automation.filescope import contained_file, open_contained

    handle = open_contained(box, "results/run.json")
    assert handle is not None
    with handle:
        assert handle.read() == b'{"metric": 0.5}'
    assert contained_file(box, "results/run.json") == box / "results" / "run.json"


@pytest.mark.parametrize(
    "shape",
    ["sibling", "outside", "chain", "directory", "dangling", "dotdot", "absolute"],
)
def test_a_link_or_escape_at_any_component_is_refused(
    box: Path, tmp_path: Path, shape: str
) -> None:
    from research_os.automation.filescope import contained_file, open_contained

    secret = _host_secret(tmp_path)
    relative = "results/out.json"
    if shape == "sibling":
        (box / "results" / "out.json").symlink_to(box / "results" / "run.json")
    elif shape == "outside":
        (box / "results" / "out.json").symlink_to(secret)
    elif shape == "chain":
        (box / "hop").symlink_to(secret)
        (box / "results" / "out.json").symlink_to(box / "hop")
    elif shape == "directory":
        (box / "linked").symlink_to(secret.parent)
        relative = "linked/secret.json"
    elif shape == "dangling":
        (box / "results" / "out.json").symlink_to(tmp_path / "nothing-yet")
    elif shape == "dotdot":
        relative = "../host/secret.json"
    else:
        relative = str(secret)

    assert open_contained(box, relative) is None
    assert contained_file(box, relative) is None


def test_a_fifo_where_a_log_should_be_is_refused_without_blocking(box: Path) -> None:
    """A blocking open of a FIFO waits for a writer that never comes."""

    import os

    from research_os.automation.filescope import open_contained

    os.mkfifo(box / "results" / "stdout.txt")
    assert open_contained(box, "results/stdout.txt") is None


def _log_context(tmp_path: Path) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        artifacts=FilesystemArtifactStore(tmp_path / "store"), run_id="RUN-x"
    )


def _experiment() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(experiment_id="PEXP-x", command="sweep")


@pytest.mark.parametrize("shape", ["file", "directory"])
def test_a_run_cannot_turn_its_log_into_a_host_file(tmp_path: Path, shape: str) -> None:
    """The run directory is bound writable into the sandbox.

    So the program can replace its own stdout -- or ``logs/`` -- with a link,
    and the reader stored the host file it pointed at as the run's log.
    """

    from research_os.portfolio.empirical import _store_logs

    secret = _host_secret(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "stderr.txt").write_text("warning: ok\n", encoding="utf-8")
    if shape == "file":
        (run_dir / "logs" / "stdout.txt").symlink_to(secret)
    else:
        (run_dir / "logs" / "stdout.txt").unlink(missing_ok=True)
        real = run_dir / "logs"
        moved = run_dir / "logs-real"
        real.rename(moved)
        (run_dir / "logs").symlink_to(secret.parent)

    context = _log_context(tmp_path)
    stored = _store_logs(context, _experiment(), run_dir=run_dir)

    for item in stored:
        assert context.artifacts.get_bytes(item["artifact_id"]) != SECRET.encode()
    assert "logs/stdout.txt" not in {item["path"] for item in stored}


def test_a_log_the_run_wrote_is_stored(tmp_path: Path) -> None:
    from research_os.portfolio.empirical import _store_logs

    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "stdout.txt").write_text("measured\n", encoding="utf-8")
    context = _log_context(tmp_path)

    (item,) = _store_logs(context, _experiment(), run_dir=run_dir)

    assert item["path"] == "logs/stdout.txt"
    assert context.artifacts.get_bytes(item["artifact_id"]) == b"measured\n"


def _schema_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    (repo / "results").mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)


def _schema(repo: Path) -> str:
    from types import SimpleNamespace

    from research_os.portfolio.empirical import output_schema_lines

    commands = {"sweep": SimpleNamespace(outputs=("results/run.json",))}
    return "\n".join(output_schema_lines(commands, repository=repo))


def test_a_committed_link_at_a_declared_output_does_not_reach_the_designer(
    tmp_path: Path,
) -> None:
    """`ls-files` lists a committed link; following it prompted the host's keys."""

    secret = _host_secret(tmp_path)
    repo = _schema_repo(tmp_path)
    (repo / "results" / "run.json").symlink_to(secret)
    _commit(repo)

    assert "host_secret_key" not in _schema(repo)


def test_a_linked_directory_on_a_declared_output_does_not_reach_the_designer(
    tmp_path: Path,
) -> None:
    """The index still names `results/run.json` after `results/` becomes a link."""

    secret = _host_secret(tmp_path)
    repo = _schema_repo(tmp_path)
    (repo / "results" / "run.json").write_text('{"metric": 1}', encoding="utf-8")
    _commit(repo)
    (repo / "results" / "run.json").unlink()
    (repo / "results").rmdir()
    (repo / "results").symlink_to(secret.parent)
    (secret.parent / "run.json").write_text(SECRET, encoding="utf-8")

    assert "host_secret_key" not in _schema(repo)


def test_a_committed_specimen_still_shows_its_shape(tmp_path: Path) -> None:
    """The control: a real committed output is still described."""

    repo = _schema_repo(tmp_path)
    (repo / "results" / "run.json").write_text(
        '{"portability": {"R": 0.2}}', encoding="utf-8"
    )
    _commit(repo)

    assert "portability.R" in _schema(repo)


def test_the_runtime_route_does_not_store_a_linked_output_or_log(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from research_os.runtime.actions.experiments import _collect_outputs

    secret = _host_secret(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "out.json").symlink_to(secret)
    (run_dir / "logs" / "stdout.txt").symlink_to(secret)
    (run_dir / "real.json").write_text('{"metric": 2}', encoding="utf-8")
    store = FilesystemArtifactStore(tmp_path / "store")
    context = SimpleNamespace(artifacts=store)
    spec = SimpleNamespace(outputs=("out.json", "real.json"))

    refs = _collect_outputs(context, spec, {"finished": True, "run_dir": str(run_dir)})

    assert [ref.role for ref in refs] == ["result:real.json"]
    assert all(store.get_bytes(ref.artifact_id) != SECRET.encode() for ref in refs)


def test_a_linked_document_root_is_not_inventoried(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from research_os.runtime.actions.inspect import _scientific_documents

    secret = _host_secret(tmp_path)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "notes.md").write_text("# ours\n", encoding="utf-8")
    (repo / "reports").symlink_to(secret.parent)
    (repo / "docs" / "leak.json").symlink_to(secret)
    context = SimpleNamespace(
        artifacts=FilesystemArtifactStore(tmp_path / "store"),
        kernel=SimpleNamespace(objects=lambda: ()),
    )

    shown, total, _refs = _scientific_documents(repo, context)

    assert [entry["path"] for entry in shown] == ["docs/notes.md"]
    assert total == 1


def test_the_v1_experiment_route_does_not_hash_through_a_directory_link(
    tmp_path: Path,
) -> None:
    from research_os.experiment.ingest import collect_artifacts

    worktree = tmp_path / "wt"
    (worktree / "specimens").mkdir(parents=True)
    (worktree / "specimens" / "run.json").write_text('{"m": 1}', encoding="utf-8")
    (worktree / "results").symlink_to(worktree / "specimens")
    (worktree / "own.json").write_text('{"m": 2}', encoding="utf-8")

    records = collect_artifacts(worktree, declared=["results/run.json", "own.json"])

    assert [record.path for record in records] == ["own.json"]


def test_bytes_replaced_after_they_were_hashed_are_not_what_is_read(
    box: Path, tmp_path: Path
) -> None:
    """Validation, then replacement, then the read.

    `_collect` hashed ``results/run.json``; then the file is replaced -- by
    other bytes, or by a link through a swapped directory -- before the
    conclusion reads it. The read must be of the recorded bytes or of none.
    """

    import shutil

    from research_os.portfolio.empirical import _collect, _read_recorded

    ((relative, digest, _size),) = _collect(box, ["results/run.json"])
    assert isinstance(
        _read_recorded(box, relative, digest=digest, max_bytes=1024), bytes
    )

    (box / "results" / "run.json").write_text('{"metric": 9.9}', encoding="utf-8")
    changed = _read_recorded(box, relative, digest=digest, max_bytes=1024)
    assert isinstance(changed, str) and "changed after" in changed

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "run.json").write_text('{"metric": 0.5}', encoding="utf-8")
    shutil.rmtree(box / "results")
    (box / "results").symlink_to(elsewhere)
    linked = _read_recorded(box, relative, digest=digest, max_bytes=1024)
    assert isinstance(linked, str) and "not a file this run wrote" in linked


# =============================================================================
# A3. A committed specimen is recognised by its Git object, not a filter's view.
# =============================================================================
def _attr_repo(tmp_path: Path) -> Path:
    """A repository whose attributes change how Git hashes and checks out.

    ``crlf.json`` is committed with CRLF endings *before* ``text=auto``
    exists, so its blob keeps them and the clean filter would normalise them
    away; ``smudged.json`` is committed with LF under ``eol=crlf``, so
    checkout writes CRLF bytes that are not its blob.
    """

    import subprocess

    repo = tmp_path / "attrs"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["config", "core.autocrlf", "false"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "results").mkdir()
    (repo / "results" / "crlf.json").write_bytes(b'{"R": 0.216}\r\n')
    _commit(repo)
    (repo / ".gitattributes").write_text(
        "* text=auto\nresults/smudged.json eol=crlf\n", encoding="utf-8"
    )
    (repo / "results" / "smudged.json").write_bytes(b'{"R": 0.5}\n')
    _commit(repo)
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(workspace), "HEAD"], cwd=repo, check=True
    )
    return workspace


def test_a_crlf_specimen_under_text_auto_is_recognised_as_committed(
    tmp_path: Path,
) -> None:
    """The defect: `hash-object` normalised it, and it looked like a new result."""

    from research_os.portfolio.empirical import _was_already_in_the_checkout

    workspace = _attr_repo(tmp_path)

    assert _was_already_in_the_checkout(workspace, "results/crlf.json") is not None


def test_a_specimen_checkout_rewrote_is_recognised_as_committed(
    tmp_path: Path,
) -> None:
    """`eol=crlf`: `--no-filters` alone would call the untouched file new."""

    from research_os.portfolio.empirical import _was_already_in_the_checkout

    workspace = _attr_repo(tmp_path)
    assert (workspace / "results" / "smudged.json").read_bytes().endswith(b"\r\n")

    assert _was_already_in_the_checkout(workspace, "results/smudged.json") is not None


def test_rewriting_gitattributes_in_the_workspace_does_not_launder_a_specimen(
    tmp_path: Path,
) -> None:
    """Attributes are read from HEAD, not from a file the run may edit."""

    from research_os.portfolio.empirical import _was_already_in_the_checkout

    workspace = _attr_repo(tmp_path)
    (workspace / ".gitattributes").write_text("", encoding="utf-8")

    assert _was_already_in_the_checkout(workspace, "results/smudged.json") is not None


@pytest.mark.parametrize(
    ("relative", "written", "committed"),
    [
        # Different content: the run's own output.
        ("results/crlf.json", b'{"R": 3.1}\r\n', False),
        ("results/smudged.json", b'{"R": 7.0}\r\n', False),
        # The same content with other line endings: bytes neither the blob
        # nor checkout's rendering of it, however a clean filter would
        # normalise them -- the run wrote them.
        ("results/crlf.json", b'{"R": 0.216}\n', False),
        # Exactly the committed blob's bytes, though checkout wrote CRLF:
        # that is the Git object itself, and it is not a measurement.
        ("results/smudged.json", b'{"R": 0.5}\n', True),
    ],
)
def test_bytes_the_run_wrote_are_judged_by_the_git_object(
    tmp_path: Path, relative: str, written: bytes, committed: bool
) -> None:
    from research_os.portfolio.empirical import _was_already_in_the_checkout

    workspace = _attr_repo(tmp_path)
    (workspace / relative).write_bytes(written)

    assert (_was_already_in_the_checkout(workspace, relative) is not None) is committed


def test_a_file_git_never_had_is_never_stale(tmp_path: Path) -> None:
    from research_os.portfolio.empirical import _was_already_in_the_checkout

    workspace = _attr_repo(tmp_path)
    (workspace / "results" / "new.json").write_bytes(b"{}\n")

    assert _was_already_in_the_checkout(workspace, "results/new.json") is None


# ------------------------------------------- a refusal ends the run it met --
@pytest.mark.parametrize("refused", [True, False])
def test_an_explorer_run_a_budget_refuses_ends_budget_exhausted(
    runtime_db: Database, tmp_path: Path, runtime_project: str, refused: bool
) -> None:
    """Every call reserves its whole ceiling now, so refusals are reachable.

    An explorer whose call the ledger refused left its run RUNNING for ever:
    only idea stages recorded their own ending, and no reconciler looks at an
    idea-track run. The control -- an unexpected error -- ends FATAL.
    """

    from types import SimpleNamespace

    from research_os.portfolio import extensions
    from research_os.runtime.models import RunStatus, TerminalState

    class Refusing:
        def complete(self, request: ModelRequest) -> Any:
            if refused:
                raise BudgetExhaustedError(
                    "0.10 left; 0.60 was requested",
                    dimension=Dimension.MODEL_COST_USD,
                    scope=BudgetScope.PROJECT,
                    scope_id=runtime_project,
                )
            raise RuntimeError("something else broke")

    store = RuntimeStore(runtime_db)
    context = SimpleNamespace(
        config=make_config(store.db.dsn, tmp_path / "artifacts"),
        db=runtime_db,
        item=SimpleNamespace(
            project_id=runtime_project,
            work_id=None,
            payload={"explorer": "blind_explorer"},
        ),
        models=lambda *_args: Refusing(),
        repo_path=None,
    )

    with pytest.raises(BudgetExhaustedError if refused else RuntimeError):
        extensions.run_explore(context)  # type: ignore[arg-type]

    (run,) = [
        item
        for item in store.list_runs(project_id=runtime_project, limit=10)
        if item.objective.startswith("portfolio-explore")
    ]
    assert run.status is RunStatus.FAILED
    assert run.terminal_state is (
        TerminalState.BUDGET_EXHAUSTED
        if refused
        else TerminalState.FATAL_INFRASTRUCTURE_ERROR
    )
