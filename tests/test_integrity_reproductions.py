"""The final adversarial review's reproductions, kept as permanent regressions.

The Part E review of ``37e8afe`` reproduced six HIGH and four MEDIUM
integrity defects, each as a small pytest file. Those files are frozen,
read-only evidence under
``~/.local/state/research-os-qualification/37e8afe…/evidence/60-part-e-review/``
and are not edited; each test below is one of them brought into the suite,
with its scenario and its assertions preserved. Every one failed on
``37e8afe``. The sha256 of the evidence file it came from is in its
docstring, so the provenance can be checked rather than trusted.

The neighbouring and variant cases for each defect live beside the repair,
in ``tests/test_integrity_*.py``; this file is only the original failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_os.portfolio.allocation import ADVANCE_IDEA
from research_os.portfolio.config import load_config
from research_os.portfolio.models import IdeaStatus, Stage
from research_os.portfolio.store import PortfolioStateError, PortfolioStore
from research_os.portfolio.track import advance_idea
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.queue import WorkQueue
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_final_hardening_regressions import (
    _budget,
    _ceiling,
    _request,
    _responses,
)
from tests.test_final_hardening_regressions import (
    _router as _billing_router,
)
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TwoPathRouter,
    _Entry,
    _matrix,
    _Packet,
    _why,
    _Work,
    checkpoint_tables,
)
from tests.test_portfolio_promotion import (
    _router as _portfolio_router,
)
from tests.test_portfolio_tick import _tick

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]


# ------------------------------------------------------------------ H5 -----
def _timeout() -> ScriptedResponse:
    # Exactly the InvocationResult shape ClaudeCodeProvider.invoke builds on
    # subprocess.TimeoutExpired: exit_code None, timed_out, an error, no cost.
    return ScriptedResponse(
        exit_code=None,
        timed_out=True,
        error="provider timed out after 600s",
        total_cost_usd=None,
    )


def test_h5_timed_out_calls_are_not_each_authorised_the_whole_ceiling_again(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """H5. ``_review_budget_timeout.py``, sha256
    8c96a1040485f0aed902d24e3257e0007bfd0f09549f0c5ef7b629a71bb856e5.

    A 1.00 ceiling in every scope; each call is capped at 0.60 by the CLI.
    After one call has run to its timeout under a 0.60 cap, at most 0.40 of
    authority can honestly remain, so a second 0.60 call must not start. The
    frozen router started all five: up to 3.00 USD against a 1.00 ceiling,
    with the ledger reporting nothing spent in any scope.
    """

    idea, lineage = "idea-x", "idea-root"
    ledger = _ceiling(runtime_db, BudgetScope.PROJECT, runtime_project, "1.00")
    _ceiling(runtime_db, BudgetScope.IDEA, idea, "1.00")
    _ceiling(runtime_db, BudgetScope.LINEAGE, lineage, "1.00")
    provider = FakeProvider(
        name="one", family="a", responses=_responses(*[_timeout() for _ in range(5)])
    )
    router = _billing_router(
        runtime_db, tmp_path, project_id=runtime_project, provider=provider
    )
    scopes = (("idea", idea), ("lineage", lineage))

    for _ in range(5):
        try:
            router.complete(_request(max_cost="0.60", scopes=scopes))
        except ProviderCallFailedError:
            pass  # PROVIDER_TIMEOUT: the queue would retry it
        except Exception:  # noqa: BLE001 - a refusal is the correct outcome
            break

    for scope, scope_id in (
        (BudgetScope.PROJECT, runtime_project),
        (BudgetScope.IDEA, idea),
        (BudgetScope.LINEAGE, lineage),
    ):
        record = _budget(ledger, scope, scope_id)
        assert record.reserved == 0
    calls = RuntimeStore(runtime_db).list_model_calls(limit=10)
    assert len(calls) == len(provider.calls)
    assert len(provider.calls) <= 1, (
        f"{len(provider.calls)} calls, each capped at 0.60 and each run to its "
        f"timeout, were authorised under a 1.00 ceiling; the ledger records "
        f"{_budget(ledger, BudgetScope.PROJECT, runtime_project).spent} spent"
    )
    assert isinstance(BudgetLedger(runtime_db), BudgetLedger)


# ------------------------------------------------------------------ H3 -----
SIX = tuple(f"openalex:W{index}" for index in range(1, 7))


class SameSixIndex:
    """An index that returns the same six works whatever it is asked.

    Against it no second terminology path can find anything the first search
    did not already retrieve.
    """

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in SIX[:limit]
            ),
        )


class SlowFirstScout(TwoPathRouter):
    """The first audit's call outlives the grace period while a tick runs.

    The first scout cites three of the six works it was shown; every later
    scout cites four. While the first call is in flight the portfolio's tick
    runs (the call is made to look older than the grace period, exactly as the
    reconciliation tests age an action).
    """

    scout_calls: int = 0
    reclaimed_while_running: int = -1

    def __init__(
        self,
        *,
        db: Database,
        pg_dsn: str,
        tmp_path: Path,
        project: str,
        idea_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._db, self._dsn, self._tmp = db, pg_dsn, tmp_path
        self._project, self._idea = project, idea_id

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            type(self).scout_calls += 1
            keys = SIX[:3] if type(self).scout_calls == 1 else SIX[:4]
            if type(self).scout_calls == 1:
                with self._db.tx() as conn:
                    conn.execute(
                        "update idea_actions set updated_at = now() - "
                        "interval '20 minutes' where idea_id = %s "
                        "and status = 'ACTIVE'",
                        (self._idea,),
                    )
                report = _tick(self._db, self._dsn, self._tmp, self._project)
                type(self).reclaimed_while_running = report.stale_actions_reclaimed
            self.answers = {**self.answers, "literature_scout": _matrix(keys)}
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


def _run_audit_retry(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> tuple[list[str], bool]:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _portfolio_router(runtime_db)
    SlowFirstScout.scout_calls = 0
    SlowFirstScout.reclaimed_while_running = -1
    router = SlowFirstScout(
        db=runtime_db,
        pg_dsn=pg_dsn,
        tmp_path=tmp_path,
        project=runtime_project,
        idea_id=idea.idea_id,
        answers=base.answers,
        store=RuntimeStore(runtime_db),
    )
    queue = WorkQueue(runtime_db)
    index = SameSixIndex()

    trace: list[str] = []
    human_ready_before_replicate = False
    for step in range(30):
        # Each stage is run the way the daemon runs it: from a leased item.
        queue.enqueue(
            project_id=runtime_project,
            kind=ADVANCE_IDEA,
            payload={"idea_id": idea.idea_id},
            dedup_key=f"review-probe:{step}",
        )
        (item,) = queue.claim(owner="worker", lease_seconds=3600, kinds=(ADVANCE_IDEA,))
        try:
            result = advance_idea(
                runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
                portfolio_config=load_config(),
                db=runtime_db,
                project_id=runtime_project,
                idea_id=idea.idea_id,
                models=router,
                literature=index,
                work_id=item.work_id,
            )
        except PortfolioStateError as exc:
            # The worker finished the stage and found its action already
            # FAILED by the reconciler; the queue retries the item.
            trace.append(f"(worker could not complete its action: {str(exc)[:70]})")
            continue
        version = portfolio.require_version(idea.idea_id)
        succeeded = portfolio.succeeded_stages_for_version(
            idea_id=idea.idea_id, idea_version=version.version
        )
        status = portfolio.require_idea(idea.idea_id).status
        trace.append(
            f"{result.stage} ok={result.ok} -> {status}"
            f" {(result.detail or result.reason)[:70]}"
        )
        if status is IdeaStatus.HUMAN_READY and Stage.REPLICATE not in succeeded:
            human_ready_before_replicate = True
            break
        if result.stage is None or not result.ok:
            break
    trace.append(
        f"live stage reclaimed by the tick={SlowFirstScout.reclaimed_while_running}"
        f" scout calls={SlowFirstScout.scout_calls}"
    )
    return trace, human_ready_before_replicate


def test_h3_a_live_stage_with_a_leased_work_item_is_not_reclaimed_as_a_crash(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H3. ``_review_revision_audit_retry.py``, sha256
    ce281e4720a1b758c990b32f4ae9f04ac76ea6d13f8c5a69cafaf1b48e9dfddb.

    ``advance_idea`` accepted the work item's id and never gave it to
    ``open_action``, so every ``idea_actions.work_id`` was NULL and any stage
    older than ``stale_action_grace_seconds`` was reclaimed as
    ``worker_crash`` while its worker ran.
    """

    trace, _ = _run_audit_retry(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project
    )
    assert SlowFirstScout.reclaimed_while_running == 0, (
        "the tick reclaimed a live stage (leased work item, worker running) as "
        "worker_crash" + _why(trace)
    )
