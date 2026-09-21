"""The portfolio tick, and the property this whole layer exists to provide.

The release-critical one has a test of its own and a name that says what it
means: an idea waiting for the researcher does not stop the portfolio. The rest
are the bounds -- capacity, lineage, spend -- and the two kinds of pause, the
one a person chooses and the ones that are conditions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research_os.portfolio import allocation
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ActionStatus,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    PortfolioStatus,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import ensure_schedule, tick
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def _tick(runtime_db: Database, pg_dsn: str, tmp_path, project: str, **kwargs):
    return tick(
        db=runtime_db,
        project_id=project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        **kwargs,
    )


# --------------------------------------------------- the whole point --
def test_an_idea_waiting_for_the_researcher_does_not_stop_the_portfolio(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The scenario the brief asks to be tested explicitly.

    Idea A reaches HUMAN_READY. Idea B is mid-investigation. Idea C is in
    review. The portfolio must keep allocating to B and C and must still have
    room to explore -- and no state anywhere may become "waiting for a human".

    The mechanism is one line in ``active_count``: capacity counts ideas whose
    *operational* state is ACTIVE, and a HUMAN_READY idea has no track. There
    is deliberately no portfolio state that one waiting idea can reach.
    """

    waiting, _ = seed_idea(portfolio, runtime_project, title="A: waiting for a person")
    investigating, _ = seed_idea(
        portfolio, runtime_project, title="B: mid-investigation"
    )
    reviewing, _ = seed_idea(portfolio, runtime_project, title="C: in review")

    portfolio.set_status(idea_id=waiting.idea_id, status=IdeaStatus.HUMAN_READY)
    portfolio.set_status(idea_id=investigating.idea_id, status=IdeaStatus.INVESTIGATING)
    portfolio.set_status(idea_id=reviewing.idea_id, status=IdeaStatus.REVIEW)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert report.status is PortfolioStatus.RUNNING
    assert report.active_tracks == 0
    assert report.free_slots == load_config().bounds.max_active_tracks

    allocated = {item.idea_id for item in report.allocations if item.idea_id}
    assert investigating.idea_id in allocated or reviewing.idea_id in allocated
    assert waiting.idea_id not in allocated, (
        "a HUMAN_READY idea must attract no further spend"
    )
    assert any(item.kind == allocation.EXPLORE for item in report.allocations), (
        "and the portfolio must still be generating"
    )
    assert not portfolio.require_idea(waiting.idea_id).occupies_capacity
    assert [item for item in PortfolioStatus if "HUMAN" in str(item)] == []


def test_the_portfolio_has_no_state_meaning_waiting_for_a_human() -> None:
    """Asserted against the enum, because the guarantee is an absence.

    A test that only drove the tick would pass on a build that had such a
    state and merely had not reached it yet.
    """

    assert {str(item) for item in PortfolioStatus} == {
        "RUNNING",
        "PAUSED_BY_RESEARCHER",
        "PAUSED_BUDGET_EXHAUSTED",
        "PAUSED_NO_FRONTIER",
        "PAUSED_BLOCKED_EXTERNAL",
    }


# ------------------------------------------------------------- bounds --
def test_the_active_track_ceiling_is_respected(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    config = load_config()
    for index in range(config.bounds.max_active_tracks + 3):
        idea, _ = seed_idea(portfolio, runtime_project, title=f"idea {index}")
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    advances = [
        item for item in report.allocations if item.kind == allocation.ADVANCE_IDEA
    ]
    assert len(advances) <= config.bounds.max_active_tracks


def test_one_lineage_cannot_take_the_whole_portfolio(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Diversity as a hard cap, not only as a penalty.

    Without it an early lineage that happens to score well takes every slot,
    which is the failure the brief's §25 describes: one direction becomes the
    portfolio.
    """

    config = load_config()
    root, _ = seed_idea(portfolio, runtime_project, title="root")
    portfolio.set_status(idea_id=root.idea_id, status=IdeaStatus.PROMISING)
    for index in range(6):
        child, _ = portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.BRANCH,
            fields=idea_fields(title=f"child {index}"),
            parent_idea_id=root.idea_id,
            origin_role="brancher",
        )
        portfolio.set_status(idea_id=child.idea_id, status=IdeaStatus.PROMISING)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    advances = [
        item for item in report.allocations if item.kind == allocation.ADVANCE_IDEA
    ]
    assert len(advances) <= config.bounds.max_active_per_lineage


def test_an_idea_past_its_spend_ceiling_is_not_allocated(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    config = load_config()
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="spent",
    )
    portfolio.complete_action(
        action_id=action.action_id,
        status=ActionStatus.SUCCEEDED,
        cost_usd=config.bounds.idea_spend_ceiling_usd + Decimal(1),
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert idea.idea_id not in {item.idea_id for item in report.allocations}


# ------------------------------------------------------ reconciliation --
def test_a_track_whose_worker_died_is_freed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The portfolio's `stranded_runs`, and why it has to exist.

    An idea left ACTIVE by a dead worker is not rejected, not parked, not
    blocked, and not in the digest as any of those. It is simply never chosen
    again -- which is the worst kind of failure, because nothing reports it.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="held-by-a-dead-worker",
        work_id="WORK-20260101T000000Z-deadbeef",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update idea_actions set updated_at = now() - interval '2 hours' "
            "where action_id = %s",
            (action.action_id,),
        )
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.ACTIVE
    )

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.stale_actions_reclaimed == 1
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.IDLE
    )
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.CANDIDATE, (
        "reclaiming a dead track is not a scientific verdict"
    )


# --------------------------------------------------------------- pauses --
def test_the_researchers_pause_stops_everything_and_says_so(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    seed_idea(portfolio, runtime_project)
    portfolio.upsert_state(project_id=runtime_project)
    portfolio.set_portfolio_status(
        project_id=runtime_project,
        status=PortfolioStatus.PAUSED_BY_RESEARCHER,
        detail="not now",
        paused_by="researcher",
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_BY_RESEARCHER
    assert report.allocations == ()
    assert report.work_enqueued == 0


def test_an_exhausted_budget_pauses_the_portfolio_and_rejects_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """ "Budget exhaustion parks work truthfully; it does not convert it into a
    scientific rejection."

    The idea is still PROMISING afterwards, has no retirement reason, and the
    pause names the command that lifts it.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(0),
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED
    assert "researchctl runtime budget" in " ".join(report.notes)

    after = portfolio.require_idea(idea.idea_id)
    assert after.status is IdeaStatus.PROMISING
    assert after.retire_reason is None


def test_a_pause_whose_cause_has_cleared_resumes_without_a_person(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Every pause below RESEARCHER is a condition, not a decision.

    A system that needed a person to notice the budget had been raised would
    have turned a resource question into an attention question.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(0),
    )
    assert (
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project).status
        is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED
    )
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(25),
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.RUNNING
    assert "resumed" in report.notes


# --------------------------------------------------------- determinism --
def test_the_same_state_produces_the_same_plan(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """ADR-0004's claim, asserted.

    It is what makes every other test in this file able to assert anything at
    all: a plan that varied run to run could not be checked for fairness,
    diversity or bound enforcement.
    """

    for index in range(4):
        idea, _ = seed_idea(portfolio, runtime_project, title=f"idea {index}")
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    # Enough CANDIDATEs that the pool is at its floor, so no explorer is
    # bought and the two ticks really do read the same state. With the pool
    # empty the first tick queues an explorer and the second legitimately
    # plans differently -- which is the bound below, not nondeterminism.
    for index in range(load_config().bounds.candidate_pool_floor):
        seed_idea(portfolio, runtime_project, title=f"candidate {index}")
    moment = datetime.now(UTC)
    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    assert [
        (item.kind, item.idea_id, str(item.stage), str(item.utility))
        for item in first.allocations
    ] == [
        (item.kind, item.idea_id, str(item.stage), str(item.utility))
        for item in second.allocations
    ]


def test_a_tick_enqueues_each_allocation_once(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    moment = datetime.now(UTC)
    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    assert first.work_enqueued >= 1
    assert second.work_enqueued == 0, (
        "the same tick decided twice must buy the work once"
    )
    queue = WorkQueue(runtime_db)
    # Curation and the digest are enqueued too, and are counted on their own
    # fields rather than in `work_enqueued`, which is about *allocations*.
    expected = (
        first.work_enqueued + int(first.curation_enqueued) + int(first.digest_enqueued)
    )
    assert sum(queue.counts_by_status().values()) == expected


def test_an_empty_portfolio_explores_rather_than_idling(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    portfolio.upsert_state(project_id=runtime_project)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert any(item.kind == allocation.EXPLORE for item in report.allocations)
    assert report.status is PortfolioStatus.RUNNING


def test_a_pending_seed_is_taken_before_blind_exploration(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The one input the researcher gives is not left sitting.

    A portfolio exploring on its own while a seed goes unconsumed is a
    portfolio ignoring the thing it was asked to look at.
    """

    portfolio.upsert_state(project_id=runtime_project)
    portfolio.add_seed(
        project_id=runtime_project,
        text="Compare column generation with fully-corrective Frank-Wolfe.",
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    explores = [item for item in report.allocations if item.kind == allocation.EXPLORE]
    assert explores and explores[0].explorer == "seeded_explorer"


def test_the_tick_is_on_the_runtimes_own_schedule_table(
    runtime_db: Database, runtime_project: str
) -> None:
    """Through `schedules`, so a tick passes through the same machinery as
    anything a person asked for.

    ``sql/0001_runtime.sql`` states the reason the table exists, and a second
    pass in the daemon's own ordering would have been a second execution path
    with none of the provenance, budget, locking or failure handling.
    """

    schedule_id = ensure_schedule(
        db=runtime_db, project_id=runtime_project, config=load_config()
    )
    assert schedule_id
    again = ensure_schedule(
        db=runtime_db, project_id=runtime_project, config=load_config()
    )
    assert again == schedule_id

    schedules = RuntimeStore(runtime_db).list_schedules()
    assert any(item.kind == "PORTFOLIO_TICK_DUE" for item in schedules)


def test_a_stale_tick_does_not_resurrect_a_rejected_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="subsumed by a known theorem",
    )
    report = _tick(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        now=datetime.now(UTC) + timedelta(days=30),
    )
    assert idea.idea_id not in {item.idea_id for item in report.allocations}


@pytest.mark.parametrize("blocked", list(OperationalState))
def test_no_operational_state_makes_an_idea_allocatable_while_in_flight(
    portfolio: PortfolioStore, runtime_project: str, blocked: OperationalState
) -> None:
    """Only IDLE is allocatable, and that is one predicate in one place."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_operational_state(idea_id=idea.idea_id, state=blocked)
    refreshed = portfolio.require_idea(idea.idea_id)
    assert allocation.allocatable(refreshed) is (blocked is OperationalState.IDLE)


def test_a_block_is_not_cleared_while_no_provider_is_healthy(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The control for `_clear_blocks`, which is deliberately coarse.

    It clears ``BLOCKED_PROVIDER`` for every idea when *any* provider is
    healthy, because nothing records which provider blocked which idea -- and
    on a one-provider machine that distinction does not exist. What it must
    not do is clear a block when nothing has recovered, which would put the
    idea straight back into the allocator to fail again.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_operational_state(
        idea_id=idea.idea_id, state=OperationalState.BLOCKED_PROVIDER
    )
    RuntimeStore(runtime_db).record_provider_result(
        provider="claude", ok=False, error="down", threshold=1, cooldown=600
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.blocks_cleared == 0
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.BLOCKED_PROVIDER
    )


def test_the_explorer_rotates_towards_what_is_under_represented(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Failure mining is chosen when there are failures and little of it.

    Rotation rather than a fixed ratio: the counts decide, so a portfolio that
    has drifted towards one origin corrects itself without anybody tuning a
    weight. Only the seeded branch had a test.
    """

    for index in range(4):
        idea, _ = seed_idea(portfolio, runtime_project, title=f"blind {index}")
        portfolio.set_status(
            idea_id=idea.idea_id,
            status=IdeaStatus.REJECTED,
            retire_reason="the falsifier killed it",
        )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    explores = [item for item in report.allocations if item.kind == allocation.EXPLORE]
    assert explores and explores[0].explorer == "failure_mining_explorer", [
        item.reason for item in explores
    ]


def test_a_second_tick_does_not_buy_a_second_explorer(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """One explorer in flight at a time, across ticks and not only within one.

    `Allocation.dedup_key` buckets by the minute, which stops two explorers in
    one tick and nothing at all across ticks: with a 120 s cadence and an
    explorer that takes longer, every tick added another at full price. Found
    by running two ticks against a real project and watching the queue grow.
    """

    portfolio.add_seed(project_id=runtime_project, text="somewhere to look")
    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert [item.kind for item in first.allocations] == [allocation.EXPLORE]

    # A minute later, so the dedup bucket is a different one and the queue
    # would happily take a second item.
    later = datetime.now(UTC) + timedelta(minutes=1)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=later)
    assert [item.kind for item in second.allocations] == []

    with runtime_db.tx() as conn:
        queued = conn.execute(
            "select count(*) as n from work_items where kind = 'portfolio_explore'"
        ).fetchone()
    assert queued is not None and queued["n"] == 1


def test_an_explorer_that_finished_does_not_block_the_next(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The control. The bound is on work in flight, not on work ever done."""

    portfolio.add_seed(project_id=runtime_project, text="somewhere to look")
    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    with runtime_db.tx() as conn:
        conn.execute("update work_items set status = 'SUCCEEDED'")

    later = datetime.now(UTC) + timedelta(minutes=1)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=later)
    assert [item.kind for item in second.allocations] == [allocation.EXPLORE]


def test_exploration_that_produces_nothing_stops(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The sixth loop: every explorer succeeds, no idea appears, forever.

    The budget ceiling did stop this, after spending all of it, and reported
    it as `PAUSED_BUDGET_EXHAUSTED` -- the wrong diagnosis for a project whose
    idea space the available explorers have exhausted.
    """

    bound = load_config().bounds.max_barren_explorations
    with runtime_db.tx() as conn:
        for index in range(bound):
            conn.execute(
                """
                insert into work_items
                       (work_id, project_id, kind, payload, status, dedup_key)
                values (%(work_id)s, %(project_id)s, 'portfolio_explore',
                        '{}'::jsonb, 'SUCCEEDED', %(work_id)s)
                """,
                {"work_id": f"WORK-barren-{index}", "project_id": runtime_project},
            )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_NO_FRONTIER
    assert "idea space" in " ".join(report.notes)
    assert not report.allocations


def test_a_seed_is_what_restarts_exhausted_exploration(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Its control, and the reason the pause is safe to enter automatically.

    A pause nothing can leave is a stop, and this one is left by the one
    action that supplies what the count says is missing: somewhere new to
    look.
    """

    bound = load_config().bounds.max_barren_explorations
    with runtime_db.tx() as conn:
        for index in range(bound):
            conn.execute(
                """
                insert into work_items
                       (work_id, project_id, kind, payload, status, dedup_key)
                values (%(work_id)s, %(project_id)s, 'portfolio_explore',
                        '{}'::jsonb, 'SUCCEEDED', %(work_id)s)
                """,
                {"work_id": f"WORK-barren-{index}", "project_id": runtime_project},
            )
    assert (
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project).status
        is PortfolioStatus.PAUSED_NO_FRONTIER
    )

    portfolio.add_seed(project_id=runtime_project, text="a direction nobody tried")
    later = datetime.now(UTC) + timedelta(minutes=1)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=later)
    assert report.status is PortfolioStatus.RUNNING
    assert [item.kind for item in report.allocations] == [allocation.EXPLORE]


def test_an_explorer_in_flight_is_not_an_empty_frontier(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The bound above must not turn waiting into stopping.

    Buying one explorer and then declining to buy a second leaves a tick with
    no allocations and no ideas, which is exactly the shape of `no frontier`.
    Reporting that would tell the researcher to seed a project that is at that
    moment generating, which is advice to do the thing already in progress.
    """

    portfolio.add_seed(project_id=runtime_project, text="somewhere to look")
    assert _tick(runtime_db, pg_dsn, tmp_path, runtime_project).status is (
        PortfolioStatus.RUNNING
    )
    later = datetime.now(UTC) + timedelta(minutes=2)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=later)
    assert second.status is PortfolioStatus.RUNNING
    assert not second.allocations
