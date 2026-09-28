"""A stage the allocator bought records the utility it was bought at.

The final qualification preflight (B1) found that no ``idea_actions.utility``
was ever written. The tick put the allocator's utility in the work item's
payload, and ``track.advance_idea`` -- the only production caller of
``open_action`` -- opened the action without it. Measured through the
production tick and daemon loop, 61 of 61 purchased actions had no utility,
so Q07 (a stage bought with a recorded utility) and Q26's purchase link could
never pass in a live run.

What holds instead, through the production path -- the portfolio ``tick``
decides and enqueues, ``Daemon.tick`` leases the item and runs the registered
handler, and the handler opens the action:

- the action records exactly the utility the allocator bought the stage at,
  and keeps it through the stage's lifecycle;
- it is the purchase-time number: a later tick that scores the same queued
  item differently changes nothing;
- a retry that takes over a dead attempt's action records the same number,
  and so does the dead attempt's row;
- work the allocator did not buy records none, and nothing invents one.

``tests/qualification_mutations.py`` removes the propagation and checks that
these tests notice.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import allocation, qualification, track
from research_os.portfolio.config import PortfolioConfig, load_config
from research_os.portfolio.models import ActionStatus, IdeaAction, Stage
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.daemon import Daemon
from research_os.runtime.db import Database
from research_os.runtime.models import WorkItem, WorkStatus
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_capsule, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_promotion import checkpoint_tables

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

TERMINAL = {WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.CANCELLED}


class _Killed(BaseException):
    """A worker dying mid-stage: nothing in the daemon catches it."""


class _NoModel(ScriptedRouter):
    """The cheap ladder's first rung needs no model, and none may be called."""

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        raise AssertionError(f"a model was called: {request.role}")


# --------------------------------------------------------------- helpers ----
def _config(**weights: float) -> PortfolioConfig:
    """One idea, one track, no exploration: the purchase under test is the only one."""

    base = load_config()
    return base.model_copy(
        update={
            "bounds": base.bounds.model_copy(
                update={"candidate_pool_floor": 1, "candidate_pool_ceiling": 1}
            ),
            "weights": base.weights.model_copy(update=weights),
        }
    )


def _tick(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    config: PortfolioConfig,
) -> Any:
    return tick(
        db=runtime_db,
        project_id=project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=config,
    )


def _daemon(
    runtime_db: Database, pg_dsn: str, tmp_path: Path, project: str, *, owner: str
) -> Daemon:
    """The control plane composed as ``researchd`` composes it."""

    from research_os.portfolio import extensions as portfolio_extensions

    portfolio_extensions.register()
    repo = tmp_path / "project"
    if not repo.exists():
        make_capsule(repo)
    RuntimeStore(runtime_db).upsert_project(project_id=project, repo_path=str(repo))
    return Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: _NoModel(
            answers={}, store=RuntimeStore(runtime_db)
        ),
        owner=owner,
    )


#: Two ideas no rung of the dedup ladder finds close, so it asks no model.
DISTINCT = {
    "queued": {
        "title": "Warm starts in interior-point solvers",
        "research_question": "Can a barrier method reuse a previous central path?",
        "core_idea": "Restarting from an old central path shortens the next solve.",
    },
    "direct": {
        "title": "Graph colouring by random restarts",
        "research_question": "Do random restarts help greedy colouring of sparse graphs?",
        "core_idea": "Restarting a greedy colouring escapes poor vertex orders.",
    },
}


def _purchase(report: Any, idea_id: str) -> allocation.Allocation:
    (bought,) = [
        item
        for item in report.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea_id
    ]
    return bought


def _queued(runtime_db: Database, bought: allocation.Allocation) -> WorkItem:
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select work_id from work_items where dedup_key = %s", (bought.dedup_key,)
        ).fetchone()
    assert row is not None, "the tick bought the stage and nothing was queued"
    item = WorkQueue(runtime_db).get(str(row["work_id"]))
    assert item is not None
    return item


def _work(daemon: Daemon, runtime_db: Database, work_id: str) -> WorkItem:
    """Run the daemon's own passes until the item is finished."""

    queue = WorkQueue(runtime_db)
    for _ in range(20):
        daemon.tick()
        item = queue.get(work_id)
        assert item is not None
        if item.status in TERMINAL:
            return item
    raise AssertionError(f"{work_id} never finished")


def _actions_of(portfolio: PortfolioStore, idea_id: str, work_id: str) -> list[Any]:
    return [
        action
        for action in portfolio.list_actions(idea_id=idea_id)
        if action.work_id == work_id
    ]


def _durable_utility(runtime_db: Database, action_id: str) -> Decimal | None:
    """The column itself, not the model's reading of it."""

    with runtime_db.tx() as conn:
        row = conn.execute(
            "select utility from idea_actions where action_id = %s", (action_id,)
        ).fetchone()
    assert row is not None
    return row["utility"]


# ------------------------------------------------------------ the defect ----
def test_a_stage_the_allocator_bought_records_the_utility_it_was_bought_at(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    daemon = _daemon(runtime_db, pg_dsn, tmp_path, runtime_project, owner="worker-a")
    idea, _ = seed_idea(portfolio, runtime_project)

    bought = _purchase(
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project, _config()), idea.idea_id
    )
    assert bought.stage is Stage.DEDUP
    assert bought.utility != 0, "a zero utility would not show it was carried"
    queued = _queued(runtime_db, bought)
    assert Decimal(str(queued.payload["utility"])) == bought.utility

    done = _work(daemon, runtime_db, queued.work_id)
    assert done.status is WorkStatus.SUCCEEDED, done.last_error

    (action,) = _actions_of(portfolio, idea.idea_id, queued.work_id)
    assert action.stage is Stage.DEDUP
    assert action.status is ActionStatus.SUCCEEDED, "kept through its lifecycle"
    assert (action.attempt, action.lease_owner) == (1, "worker-a")
    assert action.utility == bought.utility
    assert _durable_utility(runtime_db, action.action_id) == bought.utility

    # What the qualification reads. The same tick bought the bank's curation,
    # so once the daemon has run it Q07 holds as a whole -- and Q26's
    # purchase link holds for this idea.
    for _ in range(20):
        if not daemon.tick().did_something:
            break
    ctx = qualification._Ctx(
        db=runtime_db, project_id=runtime_project, artifacts=None, repo_path=None
    )
    assert qualification._bought(ctx) == 1
    assert qualification._bought(ctx, idea.idea_id) == 1
    held, detail = qualification.CHECKS["curation_prioritization"](ctx)
    assert held and detail.endswith("; 1 stage(s) bought with a utility"), detail


def test_the_recorded_utility_is_the_purchase_and_not_a_later_rescoring(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """A second tick scores the queued stage anew; the purchase does not change."""

    daemon = _daemon(runtime_db, pg_dsn, tmp_path, runtime_project, owner="worker-a")
    idea, _ = seed_idea(portfolio, runtime_project)
    first = _purchase(
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project, _config()), idea.idea_id
    )
    queued = _queued(runtime_db, first)

    later = _tick(
        runtime_db, pg_dsn, tmp_path, runtime_project, _config(decisiveness=3.0)
    )
    rescored = _purchase(later, idea.idea_id)
    assert rescored.dedup_key == first.dedup_key
    assert rescored.utility != first.utility, "the rescoring must differ to show this"
    assert later.work_enqueued == 0 and later.work_refused >= 1
    assert _queued(runtime_db, rescored).work_id == queued.work_id

    assert _work(daemon, runtime_db, queued.work_id).status is WorkStatus.SUCCEEDED
    (action,) = _actions_of(portfolio, idea.idea_id, queued.work_id)
    assert action.utility == first.utility
    assert action.utility != rescored.utility


def test_a_retry_that_takes_over_a_dead_attempt_records_the_same_purchase(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker dies after opening its action; the next attempt takes it over."""

    idea, _ = seed_idea(portfolio, runtime_project)
    bought = _purchase(
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project, _config()), idea.idea_id
    )
    queued = _queued(runtime_db, bought)

    def killed(**_kwargs: Any) -> Any:
        raise _Killed

    real = track._run_owned_stage
    monkeypatch.setattr(track, "_run_owned_stage", killed)
    dying = _daemon(runtime_db, pg_dsn, tmp_path, runtime_project, owner="worker-a")
    with pytest.raises(_Killed):
        _work(dying, runtime_db, queued.work_id)
    (dead,) = _actions_of(portfolio, idea.idea_id, queued.work_id)
    assert dead.status is ActionStatus.ACTIVE and dead.attempt == 1
    assert dead.utility == bought.utility

    # The lease runs out; a restarted daemon requeues the item and runs it.
    monkeypatch.setattr(track, "_run_owned_stage", real)
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set lease_expires_at = now() - interval '1 second' "
            "where work_id = %s",
            (queued.work_id,),
        )
    restarted = _daemon(runtime_db, pg_dsn, tmp_path, runtime_project, owner="worker-b")
    done = _work(restarted, runtime_db, queued.work_id)
    assert done.status is WorkStatus.SUCCEEDED and done.attempts == 2, done.last_error

    by_attempt = {
        a.attempt: a for a in _actions_of(portfolio, idea.idea_id, done.work_id)
    }
    assert set(by_attempt) == {1, 2}
    assert by_attempt[1].status is ActionStatus.FAILED
    assert by_attempt[2].status is ActionStatus.SUCCEEDED
    assert by_attempt[2].lease_owner == "worker-b"
    for action in by_attempt.values():
        assert action.utility == bought.utility
        assert _durable_utility(runtime_db, action.action_id) == bought.utility


def test_work_the_allocator_did_not_buy_records_no_utility(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """An item with no purchase in it, and a stage run with no item at all."""

    daemon = _daemon(runtime_db, pg_dsn, tmp_path, runtime_project, owner="worker-a")
    queue = WorkQueue(runtime_db)

    unbought, _ = seed_idea(portfolio, runtime_project, **DISTINCT["queued"])
    item = queue.enqueue(
        project_id=runtime_project,
        kind=allocation.ADVANCE_IDEA,
        payload={"idea_id": unbought.idea_id},
        dedup_key="enqueued-by-hand",
    ).item
    assert _work(daemon, runtime_db, item.work_id).status is WorkStatus.SUCCEEDED
    (action,) = _actions_of(portfolio, unbought.idea_id, item.work_id)
    assert action.utility is None

    direct, _ = seed_idea(portfolio, runtime_project, **DISTINCT["direct"])
    result = track.advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=runtime_project,
        idea_id=direct.idea_id,
        models=_NoModel(answers={}, store=RuntimeStore(runtime_db)),
    )
    assert result.ok and result.action_id is not None, result.detail
    actions: list[IdeaAction] = portfolio.list_actions(idea_id=direct.idea_id)
    assert [a.utility for a in actions] == [None]
    ctx = qualification._Ctx(
        db=runtime_db, project_id=runtime_project, artifacts=None, repo_path=None
    )
    assert qualification._bought(ctx) == 0


def test_every_purchase_mutant_still_applies_and_names_real_tests() -> None:
    """The harness in ``tests/qualification_mutations.py`` cannot rot silently."""

    import ast

    from tests.qualification_mutations import MUTANTS

    root = Path(__file__).resolve().parents[1]
    assert MUTANTS
    for mutant in MUTANTS:
        source = (root / mutant.file).read_text(encoding="utf-8")
        assert source.count(mutant.find) == 1, f"{mutant.id} no longer applies"
        for node in mutant.tests:
            path, _, name = node.partition("::")
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            names = {
                item.name for item in tree.body if isinstance(item, ast.FunctionDef)
            }
            assert name in names, f"{mutant.id}: {node}"
