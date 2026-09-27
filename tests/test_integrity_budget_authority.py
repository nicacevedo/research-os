"""INV-01 -- budget authority fails closed on unknown external spend.

The HIGH finding this file closes (final adversarial review of 37e8afe, H5):
a routed call that ran to its timeout reported no cost, and the router
*released* its reservation. The CLI had been running for ``timeout_seconds``
under ``--max-budget-usd``, i.e. it may have billed its whole cap, so every
timed-out call handed that cap back to run, project, system, idea and lineage
and the next call was authorised the full amount again: five 0.60-capped calls
under a 1.00 USD ceiling, with the ledger reporting nothing spent.

The invariant (``docs/ARCHITECTURE_INVARIANTS.md``, INV-01): no external spend
may consume authority beyond what was authorised, including when the caller
times out, crashes or loses the response, and unknown spend fails closed.

What this file holds, boundary by boundary:

- the original reproduction, through the real router, in all five scopes;
- the real adapter's two ends: a CLI killed at its timeout is *invoked* with
  no cost, a CLI that cannot be executed is *not invoked*;
- process death, with ``os._exit`` in a real child process, before the call
  is submitted, right after it is submitted, while the provider is working,
  and after the provider answered but before anything was settled;
- repeated reconciliation is idempotent;
- the delegated controllers' wrapper follows the same rule.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Role
from research_os.automation.providers import (
    ClaudeCodeProvider,
    InvocationRequest,
    InvocationResult,
)
from research_os.runtime.budgets import (
    DEFAULT_CALL_CEILING_USD,
    BudgetExhaustedError,
    BudgetLedger,
    Dimension,
    SettlementBasis,
)
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope, ReservationStatus
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.spend import BudgetedProvider, DelegatedSpendAuthority
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project
from tests.test_final_hardening_regressions import _request, _responses, _router

__all__ = ["pg_dsn", "runtime_db", "runtime_project"]

SCRIPT = Path(__file__).parent / "runtime_scripts" / "die_during_model_call.py"
IDEA, LINEAGE = "idea-x", "idea-root"
ONE = Decimal("1.00")
CAP = Decimal("0.60")


def _timeout() -> ScriptedResponse:
    # Exactly the InvocationResult shape ClaudeCodeProvider.invoke builds on
    # subprocess.TimeoutExpired: exit_code None, timed_out, an error, no cost.
    return ScriptedResponse(
        exit_code=None,
        timed_out=True,
        error="provider timed out after 600s",
        total_cost_usd=None,
    )


def _five_ceilings(db: Database, run_id: str, project_id: str) -> BudgetLedger:
    """A 1.00 USD ceiling in every scope a portfolio call counts against."""

    ledger = BudgetLedger(db)
    for scope, scope_id in (
        (BudgetScope.RUN, run_id),
        (BudgetScope.PROJECT, project_id),
        (BudgetScope.SYSTEM, "system"),
        (BudgetScope.IDEA, IDEA),
        (BudgetScope.LINEAGE, LINEAGE),
    ):
        ledger.set_limit(
            scope=scope,
            scope_id=scope_id,
            dimension=Dimension.MODEL_COST_USD,
            limit_value=ONE,
            explicit=scope is BudgetScope.PROJECT,
        )
    return ledger


def _scopes(run_id: str, project_id: str) -> tuple[tuple[BudgetScope, str], ...]:
    return (
        (BudgetScope.RUN, run_id),
        (BudgetScope.PROJECT, project_id),
        (BudgetScope.SYSTEM, "system"),
        (BudgetScope.IDEA, IDEA),
        (BudgetScope.LINEAGE, LINEAGE),
    )


def _cost(ledger: BudgetLedger, scope: BudgetScope, scope_id: str) -> Any:
    record = ledger.get(
        scope=scope, scope_id=scope_id, dimension=Dimension.MODEL_COST_USD
    )
    assert record is not None
    return record


def _attempt(router: Any, *, times: int) -> list[str]:
    outcomes: list[str] = []
    for _ in range(times):
        try:
            router.complete(
                _request(
                    max_cost=str(CAP), scopes=(("idea", IDEA), ("lineage", LINEAGE))
                )
            )
            outcomes.append("ok")
        except BudgetExhaustedError:
            outcomes.append("refused")
        except ProviderCallFailedError:
            outcomes.append("failed")
    return outcomes


# ------------------------------------------------ the original reproduction --
def test_timed_out_calls_do_not_hand_back_their_ceiling_in_any_scope(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """H5, reproduced through the real router: one call, not five.

    After one call has run to its timeout under a 0.60 cap, at most 0.40 of
    authority can honestly remain in each scope, so a second 0.60 call must
    not start. The frozen router started all five and recorded nothing spent.
    """

    provider = FakeProvider(
        name="one", family="a", responses=_responses(*[_timeout() for _ in range(5)])
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)

    outcomes = _attempt(router, times=5)

    assert len(provider.calls) == 1, outcomes
    assert outcomes == ["failed", "refused", "refused", "refused", "refused"]
    for scope, scope_id in _scopes(router._run_id, runtime_project):
        record = _cost(ledger, scope, scope_id)
        assert record.spent == CAP, f"{scope} was not charged the unknown spend"
        assert record.reserved == 0, f"{scope} still holds a reservation"
    (call,) = RuntimeStore(runtime_db).list_model_calls(limit=10)
    assert call.cost_usd is None, "what the provider reported is still recorded"
    assert "charged the whole" in (call.error or "")


def test_a_reported_failure_cost_still_settles_at_what_was_reported(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """Neighbour: a failure the provider priced is charged its price, not the cap."""

    provider = FakeProvider(
        name="one",
        family="a",
        responses=_responses(
            *[
                ScriptedResponse(exit_code=1, error="is_error", total_cost_usd=0.20)
                for _ in range(5)
            ]
        ),
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)

    outcomes = _attempt(router, times=5)

    # 0.20 charged per call, and the fourth 0.60 reservation needs 0.60 of
    # the 0.40 left: three calls ran.
    assert outcomes == ["failed", "failed", "failed", "refused", "refused"]
    for scope, scope_id in _scopes(router._run_id, runtime_project):
        assert _cost(ledger, scope, scope_id).spent == Decimal("0.60")


# ------------------------------------------------------- the real adapter --
def _fake_cli(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "claude"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _invoke(provider: ClaudeCodeProvider, tmp_path: Path) -> InvocationResult:
    return provider.invoke(
        InvocationRequest(
            role=Role.REVIEWER,
            prompt="p",
            cwd=tmp_path,
            read_only=True,
            timeout_seconds=1,
            max_budget_usd=0.60,
        )
    )


def test_the_real_adapter_says_a_timed_out_cli_was_invoked(tmp_path: Path) -> None:
    result = _invoke(
        ClaudeCodeProvider(executable=str(_fake_cli(tmp_path, "sleep 5"))), tmp_path
    )
    assert result.timed_out
    assert result.total_cost_usd is None
    assert result.invoked, "a CLI that ran to its timeout may have billed its cap"


def test_the_real_adapter_says_an_unexecutable_cli_was_not_invoked(
    tmp_path: Path,
) -> None:
    missing = _invoke(
        ClaudeCodeProvider(executable=str(tmp_path / "no-such-claude")), tmp_path
    )
    assert not missing.invoked and missing.total_cost_usd is None
    unexecutable = tmp_path / "not-executable"
    unexecutable.write_text("#!/bin/sh\necho hi\n")
    denied = _invoke(ClaudeCodeProvider(executable=str(unexecutable)), tmp_path)
    assert not denied.invoked and denied.total_cost_usd is None


def test_a_cli_that_cannot_be_executed_is_released_every_time(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The authoritative release: nothing ran, so nothing can have been billed."""

    adapter = ClaudeCodeProvider(name="one", executable=str(tmp_path / "absent"))
    router = _router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        provider=FakeProvider(name="one", family="a", responses=_responses(_timeout())),
    )
    router._adapters = {"one": adapter}
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)

    outcomes = _attempt(router, times=3)

    assert outcomes == ["failed", "failed", "failed"]
    for scope, scope_id in _scopes(router._run_id, runtime_project):
        record = _cost(ledger, scope, scope_id)
        assert record.spent == 0 and record.reserved == 0
    with runtime_db.tx() as conn:
        bases = {
            row["settlement_basis"]
            for row in conn.execute(
                "select settlement_basis from budget_reservations"
            ).fetchall()
        }
    assert bases == {str(SettlementBasis.NOT_INVOKED)}


class _Raises:
    name = "one"
    family = "a"
    hard_budget_cap = True

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def probe(self) -> object:  # pragma: no cover
        raise AssertionError

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.calls += 1
        raise self.exc


def test_an_adapter_that_raises_after_it_was_handed_the_call_is_charged(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """Neighbour: an exception is not evidence that nothing was spent."""

    router = _router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        provider=FakeProvider(name="one", family="a", responses=_responses(_timeout())),
    )
    raising = _Raises(OSError("broken pipe while reading the envelope"))
    router._adapters = {"one": raising}
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)

    assert _attempt(router, times=3) == ["failed", "refused", "refused"]
    assert raising.calls == 1
    assert _cost(ledger, BudgetScope.LINEAGE, LINEAGE).spent == CAP


def test_an_adapter_with_no_implementation_is_released(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    router = _router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        provider=FakeProvider(name="one", family="a", responses=_responses(_timeout())),
    )
    router._adapters = {"one": _Raises(NotImplementedError("no adapter"))}
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)
    assert _attempt(router, times=2) == ["failed", "failed"]
    assert _cost(ledger, BudgetScope.PROJECT, runtime_project).spent == 0


# ------------------------------------------------------- process death ------
def _die(
    boundary: str, *, runtime_db: Database, pg_dsn: str, project: str, tmp_path: Path
) -> tuple[BudgetLedger, str, Path]:
    run_id = (
        RuntimeStore(runtime_db)
        .create_run(project_id=project, objective="idea-track")
        .run_id
    )
    ledger = _five_ceilings(runtime_db, run_id, project)
    marker = tmp_path / f"{boundary}.started"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            pg_dsn,
            project,
            run_id,
            str(tmp_path / "artifacts"),
            boundary,
            IDEA,
            LINEAGE,
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 9, completed.stderr
    with runtime_db.tx() as conn:
        held = conn.execute(
            "select count(*) as n from budget_reservations where status = 'HELD'"
        ).fetchone()
        # Old enough for the reconciler, which the daemon runs.
        conn.execute(
            "update budget_reservations set created_at = now() - interval '3 hours'"
        )
    # One MODEL_COST_USD reservation per scope; MODEL_CALLS has no ceiling
    # here, and an absent budget is a zero-amount grant with no row.
    assert int(held["n"]) == 5, "the crash left a reservation unaccounted for"
    return ledger, run_id, marker


def _after_reconciliation(
    ledger: BudgetLedger, run_id: str, project: str
) -> dict[BudgetScope, Decimal]:
    assert ledger.reconcile_stale(older_than_seconds=3600) == 5
    assert ledger.reconcile_stale(older_than_seconds=3600) == 0, (
        "reconciliation is not idempotent"
    )
    spent: dict[BudgetScope, Decimal] = {}
    for scope, scope_id in _scopes(run_id, project):
        record = _cost(ledger, scope, scope_id)
        assert record.reserved == 0
        spent[scope] = record.spent
    return spent


def test_death_before_submission_is_released(
    runtime_db: Database, pg_dsn: str, runtime_project: str, tmp_path: Path
) -> None:
    ledger, run_id, marker = _die(
        "before_submission",
        runtime_db=runtime_db,
        pg_dsn=pg_dsn,
        project=runtime_project,
        tmp_path=tmp_path,
    )
    assert not marker.exists(), "the provider was reached"
    with ledger._db.tx() as conn:
        unsubmitted = conn.execute(
            "select count(*) as n from budget_reservations where submitted_at is null"
        ).fetchone()
    assert int(unsubmitted["n"]) == 5
    spent = _after_reconciliation(ledger, run_id, runtime_project)
    assert set(spent.values()) == {Decimal(0)}, spent


@pytest.mark.parametrize(
    "boundary", ["after_submission", "while_processing", "after_response"]
)
def test_death_after_submission_is_charged_in_full(
    boundary: str,
    runtime_db: Database,
    pg_dsn: str,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """Every boundary past submission leaves an unknown outcome: charged the cap.

    ``after_response`` included: the provider reported 0.10, but the report
    died with the process, and nothing durable says what the call cost. The
    over-count is visible and a person can correct it; the under-count this
    replaces was invisible.
    """

    ledger, run_id, marker = _die(
        boundary,
        runtime_db=runtime_db,
        pg_dsn=pg_dsn,
        project=runtime_project,
        tmp_path=tmp_path,
    )
    assert marker.exists(), "the provider was never reached"
    spent = _after_reconciliation(ledger, run_id, runtime_project)
    assert set(spent.values()) == {CAP}, spent
    with runtime_db.tx() as conn:
        statuses = conn.execute(
            "select distinct status, settlement_basis from budget_reservations"
        ).fetchall()
    assert {(row["status"], row["settlement_basis"]) for row in statuses} == {
        (str(ReservationStatus.SETTLED), str(SettlementBasis.STALE_UNKNOWN_OUTCOME))
    }


def test_after_a_charged_crash_the_next_call_is_refused(
    runtime_db: Database, pg_dsn: str, runtime_project: str, tmp_path: Path
) -> None:
    """The point of charging: the authority the dead call may have used is gone."""

    ledger, run_id, _ = _die(
        "while_processing",
        runtime_db=runtime_db,
        pg_dsn=pg_dsn,
        project=runtime_project,
        tmp_path=tmp_path,
    )
    _after_reconciliation(ledger, run_id, runtime_project)
    provider = FakeProvider(
        name="one",
        family="a",
        responses=_responses(ScriptedResponse(structured={}, total_cost_usd=0.01)),
    )
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    assert _attempt(router, times=1) == ["refused"]
    assert provider.calls == []


def test_settlement_is_idempotent(runtime_db: Database, runtime_project: str) -> None:
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=ONE,
    )
    grant = ledger.reserve(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        amount=CAP,
        pending=True,
    )
    ledger.mark_submitted((grant,), provider_cap=CAP)
    ledger.settle_unknown((grant,))
    ledger.settle_unknown((grant,))
    ledger.settle(grant, actual=Decimal("0.05"))
    ledger.release(grant)
    record = _cost(ledger, BudgetScope.PROJECT, runtime_project)
    assert record.spent == CAP and record.reserved == 0


# ------------------------------------------------- delegated controllers ----
class _TimedOut:
    name = "claude"
    family = "anthropic"
    hard_budget_cap = True

    def __init__(self, *, invoked: bool = True) -> None:
        self.invoked = invoked

    def probe(self) -> object:  # pragma: no cover
        raise AssertionError

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            argv=("claude",),
            exit_code=None,
            timed_out=self.invoked,
            stdout="",
            stderr="",
            error="provider timed out after 600s" if self.invoked else "not found",
            invoked=self.invoked,
        )


def _delegated(
    runtime_db: Database, project: str, *, invoked: bool
) -> tuple[BudgetLedger, str, BudgetedProvider, DelegatedSpendAuthority]:
    run_id = (
        RuntimeStore(runtime_db).create_run(project_id=project, objective="o").run_id
    )
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=ONE,
        explicit=True,
    )
    authority = DelegatedSpendAuthority(
        budgets=ledger,
        run_id=run_id,
        project_id=project,
        action="proposal",
        per_call_ceiling_usd=CAP,
    )
    return (
        ledger,
        run_id,
        BudgetedProvider(inner=_TimedOut(invoked=invoked), authority=authority),
        authority,
    )  # type: ignore[arg-type]


def _delegated_request(tmp_path: Path) -> InvocationRequest:
    return InvocationRequest(
        role=Role.PLANNER, prompt="p", cwd=tmp_path, read_only=True, timeout_seconds=1
    )


def test_a_delegated_call_that_timed_out_is_charged_and_the_next_refused(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    from research_os.errors import BudgetExceededError

    ledger, run_id, wrapped, authority = _delegated(
        runtime_db, runtime_project, invoked=True
    )
    wrapped.invoke(_delegated_request(tmp_path))
    assert _cost(ledger, BudgetScope.RUN, run_id).spent == CAP
    assert authority.unknown_usd == CAP and authority.settled_usd == 0
    with pytest.raises(BudgetExceededError):
        wrapped.invoke(_delegated_request(tmp_path))


def test_a_delegated_call_that_never_started_is_released(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    ledger, run_id, wrapped, _ = _delegated(runtime_db, runtime_project, invoked=False)
    for _ in range(3):
        wrapped.invoke(_delegated_request(tmp_path))
    record = _cost(ledger, BudgetScope.RUN, run_id)
    assert record.spent == 0 and record.reserved == 0


def test_an_undeclared_timeout_is_charged_the_default_ceiling_it_was_capped_at(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """A call that declared no ceiling is charged the reservation it did take.

    That reservation is the runtime's default per-call ceiling, and the
    provider was handed the same number as its hard cap -- no longer a 0.05
    profile *estimate* with no cap behind it (INV-01).
    """

    provider = FakeProvider(name="one", family="a", responses=_responses(_timeout()))
    router = _router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    ledger = _five_ceilings(runtime_db, router._run_id, runtime_project)
    with pytest.raises(ProviderCallFailedError):
        router.complete(_request(max_cost=None))
    assert provider.calls[0].max_budget_usd == float(DEFAULT_CALL_CEILING_USD)
    assert (
        _cost(ledger, BudgetScope.PROJECT, runtime_project).spent
        == DEFAULT_CALL_CEILING_USD
    )
