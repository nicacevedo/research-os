"""INV-01, second round: no spend-bearing call reaches a provider without a hard cap.

The independent review of ``8e92e8c`` (frozen under
``~/.local/state/research-os-qualification/8e92e8c…/independent-review/``)
reproduced external spend above a person's ceiling through two routes the
first integrity round left uncapped:

- ``DELEGATED_OVERRUN`` -- a delegated call reserved a 0.50 USD estimate,
  reached the provider with ``max_budget_usd=None``, and settled 1.20 USD
  against 1.00 USD run, project and system ceilings;
- ``OBJECTIVE_OVERRUN`` -- an objective-cycle call that declared no ceiling
  reserved the profile's 0.05 USD estimate and did the same;
- ``UNCAPPED_LOST_SETTLEMENT`` -- two such calls whose settlement was lost
  were charged 1.00 in every scope while 2.40 had been billed.

The invariant these tests hold (``docs/ARCHITECTURE_INVARIANTS.md``, INV-01):
every spend-bearing invocation carries a provider-enforced cap no greater than
its reservation, persisted with the submission before the provider is asked;
an adapter that cannot be capped is refused with
``PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE``. What a provider was *authorised* to
spend is then never more than what was reserved, and what was reserved is
never more than any scope's limit -- including after a lost settlement.

The fake provider here honours its cap the way a capped provider must (it
stops at ``max_budget_usd`` and bills exactly that), so what it reports as
billed is the external spend these tests bound. A provider that overshoots its
own cap is a separate, recorded residual
(``test_final_hardening_regressions.py::test_a_call_the_provider_stopped_at_its_ceiling_is_charged_and_is_not_an_outage``).
"""

from __future__ import annotations

import ast
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Role
from research_os.automation.providers import (
    PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE,
    InvocationRequest,
    InvocationResult,
)
from research_os.errors import BudgetExceededError
from research_os.runtime.budgets import (
    DEFAULT_CALL_CEILING_USD,
    BudgetError,
    BudgetExhaustedError,
    BudgetLedger,
    Dimension,
)
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.models import BudgetScope, ReservationStatus
from research_os.runtime.routing import (
    BudgetCapUnavailableError,
    CallCeilingReachedError,
    ProviderCallFailedError,
)
from research_os.runtime.spend import (
    BudgetedProvider,
    DelegatedSpendAuthority,
    ProviderBudgetCapUnavailableError,
)
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project
from tests.test_final_hardening_regressions import _request, _responses, _router
from tests.test_integrity_budget_authority import (
    IDEA,
    LINEAGE,
    ONE,
    _cost,
    _five_ceilings,
    _scopes,
)

__all__ = ["pg_dsn", "runtime_db", "runtime_project"]

ROOT = Path(__file__).resolve().parents[1]
PRICE = 1.20  # what the reviewer's provider billed, against 1.00 ceilings


@dataclass
class MeteredProvider(FakeProvider):
    """A capped fake that also keeps the external meter: what it billed."""

    billed: Decimal = Decimal(0)

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        result = super().invoke(request)
        if result.total_cost_usd is not None:
            self.billed += Decimal(str(result.total_cost_usd))
        return result


def _metered(*responses: ScriptedResponse) -> MeteredProvider:
    return MeteredProvider(name="one", family="a", responses=_responses(*responses))


def _priced(cost: float = PRICE) -> ScriptedResponse:
    return ScriptedResponse(structured={"ok": True}, total_cost_usd=cost)


def _held(db: Database) -> int:
    with db.tx() as conn:
        row = conn.execute(
            "select count(*) as n from budget_reservations where status = 'HELD'"
        ).fetchone()
    return int(row["n"])


def _three_ceilings(db: Database, run_id: str, project_id: str) -> BudgetLedger:
    ledger = BudgetLedger(db)
    for scope, scope_id in (
        (BudgetScope.RUN, run_id),
        (BudgetScope.PROJECT, project_id),
        (BudgetScope.SYSTEM, "system"),
    ):
        ledger.set_limit(
            scope=scope,
            scope_id=scope_id,
            dimension=Dimension.MODEL_COST_USD,
            limit_value=ONE,
            explicit=True,
        )
    return ledger


# ------------------------------------------- the reviewer's three routes -----
def test_rev_delegated_call_reaches_the_provider_capped_at_its_reservation(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """DELEGATED_OVERRUN: the provider is told the reservation, and stops there."""

    store = RuntimeStore(runtime_db)
    run_id = store.create_run(project_id=runtime_project, objective="delegated").run_id
    ledger = _three_ceilings(runtime_db, run_id, runtime_project)
    inner = MeteredProvider(
        name="one",
        family="a",
        responses={str(Role.REVIEWER): [_priced()]},
    )
    wrapped = BudgetedProvider(
        inner=inner,
        authority=DelegatedSpendAuthority(
            budgets=ledger,
            run_id=run_id,
            project_id=runtime_project,
            action="proposal",
        ),
    )
    result = wrapped.invoke(
        InvocationRequest(
            role=Role.REVIEWER,
            prompt="p",
            cwd=tmp_path,
            read_only=True,
            timeout_seconds=30,
        )
    )

    assert inner.calls[0].max_budget_usd == float(DEFAULT_CALL_CEILING_USD)
    assert result.budget_exhausted, "a call that needed more was stopped at its cap"
    assert inner.billed == DEFAULT_CALL_CEILING_USD
    for scope, scope_id in (
        (BudgetScope.RUN, run_id),
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.SYSTEM, "system"),
    ):
        record = _cost(ledger, scope, scope_id)
        assert record.spent == inner.billed <= record.limit_value
        assert record.reserved == 0


def test_rev_objective_call_with_no_declared_ceiling_is_capped(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """OBJECTIVE_OVERRUN: no declared ceiling is the default ceiling, capped."""

    provider = _metered(_priced())
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _three_ceilings(runtime_db, router._run_id, runtime_project)

    with pytest.raises(CallCeilingReachedError):
        router.complete(_request(max_cost=None))

    assert provider.calls[0].max_budget_usd == float(DEFAULT_CALL_CEILING_USD)
    for scope, scope_id in (
        (BudgetScope.RUN, router._run_id),
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.SYSTEM, "system"),
    ):
        record = _cost(ledger, scope, scope_id)
        assert record.spent == provider.billed == DEFAULT_CALL_CEILING_USD
        assert record.spent <= record.limit_value


def test_rev_lost_settlements_cannot_hide_spend_beyond_any_of_five_ceilings(
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNCAPPED_LOST_SETTLEMENT, through the real router this time.

    Two calls are answered and then lose their database session before
    settlement. The provider was capped at each reservation, so what it billed
    is at most what the reconciler charges -- and in every one of the five
    scopes the charge is within the ceiling. A third call is refused before
    it reaches the provider, and reconciling again changes nothing.
    """

    provider = _metered(_priced(), _priced(), _priced())
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)
    scopes = (("idea", IDEA), ("lineage", LINEAGE))

    def lost(self: BudgetLedger, grants: Any, **kwargs: Any) -> None:
        raise RuntimeDatabaseError("the worker's session died before settlement")

    with monkeypatch.context() as patched:
        patched.setattr(BudgetLedger, "settle_all", lost)
        for _ in range(2):
            with pytest.raises(RuntimeDatabaseError):
                router.complete(_request(max_cost=None, scopes=scopes))
    assert all(
        call.max_budget_usd == float(DEFAULT_CALL_CEILING_USD)
        for call in provider.calls
    )
    assert _held(runtime_db) > 0, "the lost settlements left their reservations"

    assert ledger.reconcile_stale(older_than_seconds=-1) > 0
    assert ledger.reconcile_stale(older_than_seconds=-1) == 0, "idempotent"
    for scope, scope_id in _scopes(router._run_id, runtime_project):
        record = _cost(ledger, scope, scope_id)
        assert provider.billed <= record.spent <= record.limit_value, (scope, record)
        assert record.reserved == 0

    with pytest.raises(BudgetExhaustedError):
        router.complete(_request(max_cost=None, scopes=scopes))
    assert len(provider.calls) == 2, "the third call never reached the provider"


# ------------------------------------------------- neighbouring attacks ------
def test_two_uncertain_calls_cannot_both_be_authorised_the_same_final_ceiling(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """Two 0.60 calls under a 1.00 ceiling, in flight together: one starts.

    The first is held inside the provider while the second asks. The ledger
    has 0.40 left, the second needs its whole 0.60 cap, and it is refused
    before invocation -- so even if both outcomes were then lost, nothing
    beyond one cap was ever authorised.
    """

    gate = threading.Event()
    entered = threading.Event()

    @dataclass
    class Holding(MeteredProvider):
        def invoke(self, request: InvocationRequest) -> InvocationResult:
            entered.set()
            assert gate.wait(10)
            return super().invoke(request)

    provider = Holding(name="one", family="a", responses=_responses(_priced(0.10)))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _three_ceilings(runtime_db, router._run_id, runtime_project)
    outcome: list[str] = []

    def first() -> None:
        router.complete(_request(max_cost="0.60"))
        outcome.append("first finished")

    worker = threading.Thread(target=first, daemon=True)
    worker.start()
    assert entered.wait(10)
    with pytest.raises(BudgetExhaustedError):
        router.complete(_request(max_cost="0.60"))
    gate.set()
    worker.join(10)
    assert outcome == ["first finished"]
    assert len(provider.calls) == 1
    record = _cost(ledger, BudgetScope.PROJECT, runtime_project)
    assert record.spent == Decimal("0.10") and record.reserved == 0


@pytest.mark.parametrize("tight", ["run", "project", "system", "idea", "lineage"])
def test_each_of_the_five_scopes_alone_refuses_a_call_it_cannot_cover(
    runtime_db: Database, runtime_project: str, tmp_path: Path, tight: str
) -> None:
    """Whichever scope is short, the call never starts and nothing stays held."""

    provider = _metered(_priced(0.05))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)
    ids = {
        str(scope): scope_id
        for scope, scope_id in _scopes(router._run_id, runtime_project)
    }
    ledger.set_limit(
        scope=BudgetScope(tight),
        scope_id=ids[tight],
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("0.40"),
        explicit=True,
    )

    with pytest.raises(BudgetExhaustedError):
        router.complete(
            _request(max_cost="0.60", scopes=(("idea", IDEA), ("lineage", LINEAGE)))
        )

    assert provider.calls == []
    assert _held(runtime_db) == 0
    for scope, scope_id in _scopes(router._run_id, runtime_project):
        assert _cost(ledger, scope, scope_id).spent == 0


def test_a_timed_out_capped_call_is_charged_its_cap_and_reconciliation_is_stable(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """Unknown outcome, capped provider: the charge is an upper bound, once."""

    provider = _metered(
        ScriptedResponse(
            exit_code=None, timed_out=True, error="timed out", total_cost_usd=None
        )
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _three_ceilings(runtime_db, router._run_id, runtime_project)
    with pytest.raises(ProviderCallFailedError):
        router.complete(_request(max_cost="0.60"))
    for _ in range(3):
        assert ledger.reconcile_stale(older_than_seconds=-1) == 0
    record = _cost(ledger, BudgetScope.PROJECT, runtime_project)
    assert record.spent == Decimal("0.60") and record.reserved == 0


# -------------------------------------------- an adapter nothing can cap -----
@dataclass
class _Uncapped:
    """A provider adapter that declares no ``hard_budget_cap``."""

    name: str = "one"
    family: str = "a"
    invoked: int = 0

    def probe(self) -> Any:  # pragma: no cover - not probed here
        raise AssertionError("not probed")

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.invoked += 1
        raise AssertionError("an adapter that cannot be capped was given spend")


def test_the_router_refuses_an_adapter_that_cannot_be_capped(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    adapter = _Uncapped()
    router = _router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        provider=adapter,  # type: ignore[arg-type]
    )
    _three_ceilings(runtime_db, router._run_id, runtime_project)

    with pytest.raises(BudgetCapUnavailableError) as caught:
        router.complete(_request(max_cost="0.10"))

    assert caught.value.code == PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE
    assert PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE in str(caught.value)
    assert str(caught.value.failure_class) == "capability_denied"
    assert adapter.invoked == 0
    with runtime_db.tx() as conn:
        rows = conn.execute("select count(*) as n from budget_reservations").fetchone()
    assert int(rows["n"]) == 0, "nothing was reserved for a call that cannot be capped"


def test_the_delegated_wrapper_refuses_an_adapter_that_cannot_be_capped(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    store = RuntimeStore(runtime_db)
    run_id = store.create_run(project_id=runtime_project, objective="delegated").run_id
    ledger = _three_ceilings(runtime_db, run_id, runtime_project)
    adapter = _Uncapped()
    authority = DelegatedSpendAuthority(
        budgets=ledger, run_id=run_id, project_id=runtime_project, action="proposal"
    )
    (wrapped,) = authority.wrap({"one": adapter}).values()  # type: ignore[dict-item]
    assert wrapped.hard_budget_cap is False

    with pytest.raises(ProviderBudgetCapUnavailableError) as caught:
        wrapped.invoke(
            InvocationRequest(
                role=Role.REVIEWER,
                prompt="p",
                cwd=tmp_path,
                read_only=True,
                timeout_seconds=30,
            )
        )

    assert isinstance(caught.value, BudgetExceededError), "terminal for controllers"
    assert caught.value.code == PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE
    assert adapter.invoked == 0
    assert authority.refusals == 1
    assert _cost(ledger, BudgetScope.RUN, run_id).reserved == 0


def test_a_zero_or_non_finite_cap_is_not_a_cap() -> None:
    for bad in (0.0, -0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive finite"):
            InvocationRequest(
                role=Role.REVIEWER,
                prompt="p",
                cwd=Path("/tmp"),
                read_only=True,
                timeout_seconds=1,
                max_budget_usd=bad,
            )


# ------------------------------------------- the ledger and the database -----
def _pending(ledger: BudgetLedger, project_id: str, amount: str = "0.60") -> Any:
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=project_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=ONE,
    )
    return ledger.reserve(
        scope=BudgetScope.PROJECT,
        scope_id=project_id,
        dimension=Dimension.MODEL_COST_USD,
        amount=Decimal(amount),
        pending=True,
    )


def test_the_ledger_will_not_submit_a_cost_reservation_without_a_cap(
    runtime_db: Database, runtime_project: str
) -> None:
    ledger = BudgetLedger(runtime_db)
    grant = _pending(ledger, runtime_project)
    for cap in (None, Decimal("0.61"), Decimal(0)):
        with pytest.raises(BudgetError, match="hard cap no greater"):
            ledger.mark_submitted((grant,), provider_cap=cap)
    with pytest.raises(BudgetError, match="taken pending"):
        ledger.reserve(
            scope=BudgetScope.PROJECT,
            scope_id=runtime_project,
            dimension=Dimension.MODEL_COST_USD,
            amount=Decimal("0.10"),
        )
    ledger.mark_submitted((grant,), provider_cap=Decimal("0.60"))
    (row,) = ledger.held_reservations(budget_id=grant.budget_id)
    assert row.submitted_at is not None
    assert row.provider_cap_usd == Decimal("0.60")
    assert row.status is ReservationStatus.HELD


def test_the_database_refuses_an_uncapped_submission_whatever_the_code_does(
    runtime_db: Database, runtime_project: str
) -> None:
    """Enforced where a bypassing caller cannot argue with it (``sql/0043``)."""

    ledger = BudgetLedger(runtime_db)
    grant = _pending(ledger, runtime_project)
    uncapped = (
        "update budget_reservations set submitted_at = now() where reservation_id = %s"
    )
    over = (
        "update budget_reservations set submitted_at = now(), "
        "provider_cap_usd = 0.61 where reservation_id = %s"
    )
    born_submitted = (
        "insert into budget_reservations (reservation_id, budget_id, amount, "
        "submitted_at) values ('RSV-raw', %s, 0.10, now())"
    )
    for statement, params in (
        (uncapped, (grant.reservation_id,)),
        (over, (grant.reservation_id,)),
        (born_submitted, (grant.budget_id,)),
    ):
        with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
            conn.execute(statement, params)

    ledger.mark_submitted((grant,), provider_cap=Decimal("0.60"))
    with (
        pytest.raises(RuntimeDatabaseError, match="not rewritten"),
        runtime_db.tx() as conn,
    ):
        conn.execute(
            "update budget_reservations set provider_cap_usd = 0.30 "
            "where reservation_id = %s",
            (grant.reservation_id,),
        )


# ------------------------------------------------------ the two doors --------
def _runtime_modules() -> list[Path]:
    base = ROOT / "src" / "research_os"
    return sorted(
        path
        for package in ("runtime", "portfolio")
        for path in (base / package).rglob("*.py")
    )


def test_only_the_router_builds_a_provider_invocation_in_the_autonomous_layers() -> (
    None
):
    """The router is the one place a runtime or portfolio module asks a provider.

    Delegated controllers build their own requests inside ``automation`` and
    ``proposal``; the runtime reaches them only through wrapped adapters
    (the next test). Anything else constructing an ``InvocationRequest`` here
    would be a third door with no reservation and no cap behind it.
    """

    builders = []
    for path in _runtime_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "InvocationRequest"
            ):
                builders.append(str(path.relative_to(ROOT)))
    assert builders == ["src/research_os/runtime/routing.py"]


def test_the_delegated_actions_only_ever_hand_over_wrapped_adapters() -> None:
    """``provider_registry()`` appears in them only as ``authority.wrap``'s argument."""

    for name in ("coding.py", "proposals.py"):
        path = ROOT / "src" / "research_os" / "runtime" / "actions" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped: set[int] = set()
        registries: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "wrap"
                and isinstance(func.value, ast.Name)
                and func.value.id == "authority"
            ):
                wrapped |= {id(arg) for arg in node.args}
            if isinstance(func, ast.Name) and func.id == "provider_registry":
                registries.append(node)
        assert registries, f"{name} no longer builds a registry; update this test"
        assert all(id(call) in wrapped for call in registries), name


def test_a_capped_provider_is_chosen_over_an_uncapped_one_that_could_also_serve(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """The routing filter, not only the refusal behind it.

    Two providers can serve the request; the uncappable one sorts first (same
    tier, cheaper estimate). The router must route to the capped one -- an
    adapter that cannot be capped is never a candidate for spend -- rather
    than pick the uncappable one and refuse the call.
    """

    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.routing import ModelRouter, ProviderProfile

    capped = MeteredProvider(
        name="two", family="b", responses=_responses(_priced(0.05))
    )
    uncapped = _Uncapped(name="one", family="a")
    store = RuntimeStore(runtime_db)
    router = ModelRouter(
        adapters={"one": uncapped, "two": capped},  # type: ignore[dict-item]
        profiles=(
            ProviderProfile(name="one", family="a", tier=3, estimated_cost_usd=0.01),
            ProviderProfile(name="two", family="b", tier=3, estimated_cost_usd=0.05),
        ),
        store=store,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        budgets=BudgetLedger(runtime_db),
        run_id=store.create_run(project_id=runtime_project, objective="route").run_id,
        project_id=runtime_project,
        failure_threshold=1_000,
    )

    response = router.complete(_request(max_cost="0.10"))

    assert response.provider == "two"
    assert uncapped.invoked == 0
    assert capped.calls[0].max_budget_usd == pytest.approx(0.10)
