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
from research_os.runtime.failures import FailureClass
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
    # Exactly the cap, not "at most": this assertion read `<=` and passed on
    # an allocator that bought *nothing* for a full lineage -- the defect that
    # stopped the second live qualification. At most is the diversity bound;
    # at least is the portfolio working at all.
    assert len(advances) == config.bounds.max_active_per_lineage


def test_a_portfolio_whose_every_lineage_is_full_still_works_its_ideas(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The state the live qualification wedged in, rebuilt from rows.

    On 2026-09-24 every one of 15 lineages held exactly
    ``max_active_per_lineage`` live ideas -- children are admitted until a
    lineage is full, so full is where lineages settle -- and not one idea was
    running. The allocator read "live ideas in this lineage" as "work in
    flight in this lineage", skipped every candidate, and, with the pool over
    its ceiling so no explorer either, bought nothing on every tick for as
    long as anyone let it run: RUNNING, eight free slots, no failure anywhere,
    and $9 of budget that could not be spent.

    The population bound is right where members are *created* and is
    untouched. What the allocator bounds is concurrency, so it counts tracks.
    """

    config = load_config()
    ceiling = config.bounds.max_active_per_lineage
    lineages = 5
    for family in range(lineages):
        root, _ = seed_idea(portfolio, runtime_project, title=f"root {family}")
        portfolio.set_status(idea_id=root.idea_id, status=IdeaStatus.PROMISING)
        for index in range(ceiling - 1):
            portfolio.create_idea(
                project_id=runtime_project,
                origin=IdeaOrigin.FOLLOW_UP,
                fields=idea_fields(title=f"child {family}.{index}"),
                parent_idea_id=root.idea_id,
                origin_role="follow_up_explorer",
            )
    counts = portfolio.lineage_active_counts(runtime_project)
    assert len(counts) == lineages and set(counts.values()) == {ceiling}
    assert portfolio.active_count(runtime_project) == 0

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    advances = [
        item for item in report.allocations if item.kind == allocation.ADVANCE_IDEA
    ]
    assert len(advances) == min(config.bounds.max_active_tracks, lineages * ceiling)
    per_lineage: dict[str, int] = {}
    for item in advances:
        root = portfolio.require_idea(item.idea_id).lineage_root
        per_lineage[root] = per_lineage.get(root, 0) + 1
    assert max(per_lineage.values()) <= ceiling
    assert report.work_enqueued >= len(advances)


def test_the_lineage_cap_still_bounds_the_work_in_flight(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """A lineage with a track already running gets only what is left of its cap."""

    config = load_config()
    root, _ = seed_idea(portfolio, runtime_project, title="root")
    portfolio.set_status(idea_id=root.idea_id, status=IdeaStatus.PROMISING)
    for index in range(4):
        child, _ = portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.BRANCH,
            fields=idea_fields(title=f"child {index}"),
            parent_idea_id=root.idea_id,
            origin_role="brancher",
        )
        portfolio.set_status(idea_id=child.idea_id, status=IdeaStatus.PROMISING)
    running = child
    portfolio.set_operational_state(
        idea_id=running.idea_id, state=OperationalState.ACTIVE
    )

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    advances = [
        item for item in report.allocations if item.kind == allocation.ADVANCE_IDEA
    ]
    assert len(advances) == config.bounds.max_active_per_lineage - 1
    assert running.idea_id not in {item.idea_id for item in advances}


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
    # And not silently: skipped for good, it must say so and stop counting
    # against its lineage and the pool.
    parked = portfolio.require_idea(idea.idea_id)
    assert parked.status is IdeaStatus.PARKED
    assert "idea ceiling" in (parked.retire_reason or "")
    assert "idea_spend_ceiling_usd" in (parked.revisit_if or "")


def test_a_candidate_below_the_novelty_floor_is_parked_not_left_to_fill_the_pool(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The final hostile review's second wedge, in miniature.

    A novelty screen that finds an idea "likely known" records novelty 0.2,
    under the default floor of 0.25. The stage machine still had a next stage
    for it, so nothing settled it; the allocator skipped it with a silent
    `continue`; and it went on counting as a CANDIDATE -- against the pool
    ceiling that stops exploration, and against its lineage. Forty of them
    froze a portfolio with no note at all.
    """

    from research_os.portfolio.models import QualityDimensions
    from research_os.runtime.db import jsonb

    config = load_config()
    below = config.thresholds.novelty_floor - 0.05
    ideas = []
    for n in range(3):
        idea, _ = seed_idea(portfolio, runtime_project, title=f"likely known {n}")
        for stage in (Stage.DEDUP, Stage.NOVELTY_SCREEN):
            action = portfolio.open_action(
                idea_id=idea.idea_id,
                idea_version=1,
                stage=stage,
                basis_digest=f"{stage}-{n}",
            )
            portfolio.complete_action(
                action_id=action.action_id, status=ActionStatus.SUCCEEDED
            )
        with runtime_db.tx() as conn:
            conn.execute(
                "update idea_versions set dimensions = %s where idea_id = %s",
                (jsonb(QualityDimensions(novelty=below).model_dump()), idea.idea_id),
            )
        ideas.append(idea)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.settled >= len(ideas)
    for idea in ideas:
        after = portfolio.require_idea(idea.idea_id)
        assert after.status is IdeaStatus.PARKED, after.status
        assert "below the floor" in (after.retire_reason or "")
    assert report.candidate_pool == 0 or all(
        portfolio.require_idea(idea.idea_id).status is not IdeaStatus.CANDIDATE
        for idea in ideas
    )
    # The pool they no longer occupy is the room the explorer needs: one is
    # bought across these ticks (the first, or the next once nothing is in
    # flight), and the parked ideas are not.
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    bought = [*report.allocations, *second.allocations]
    assert any(item.kind == allocation.EXPLORE for item in bought)
    assert not {idea.idea_id for idea in ideas} & {item.idea_id for item in bought}


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


def test_barren_exploration_stops_exploring_not_the_ideas_that_have_work(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The pause used to be taken before the candidates were read.

    Six barren explorer runs then froze every idea that still had a stage to
    run, under a status saying the idea space looked exhausted -- the final
    hostile review showed ten ideas one cheap screen from their next step,
    PAUSED_NO_FRONTIER with eight free slots. What is exhausted is exploring.
    """

    # The ideas first: barren runs are counted from the newest idea.
    ideas = [
        seed_idea(portfolio, runtime_project, title=f"still has work {n}")[0]
        for n in range(3)
    ]
    bound = load_config().bounds.max_barren_explorations
    with runtime_db.tx() as conn:
        for index in range(bound):
            conn.execute(
                """
                insert into work_items
                       (work_id, project_id, kind, payload, status, dedup_key,
                        created_at)
                values (%(work_id)s, %(project_id)s, 'portfolio_explore',
                        '{}'::jsonb, 'SUCCEEDED', %(work_id)s,
                        now() + interval '1 second')
                """,
                {"work_id": f"WORK-barren-{index}", "project_id": runtime_project},
            )
    assert portfolio.barren_explorations(project_id=runtime_project) == bound

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.RUNNING
    advanced = {
        item.idea_id
        for item in report.allocations
        if item.kind == allocation.ADVANCE_IDEA
    }
    assert advanced == {idea.idea_id for idea in ideas}
    assert not any(item.kind == allocation.EXPLORE for item in report.allocations), [
        (item.kind, item.reason) for item in report.allocations
    ]
    assert any("no explorer is bought" in note for note in report.notes)


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


# ----------------------------------- the seventh loop, found by the dogfood --
def _fail_the_queued_advance(
    runtime_db: Database, project: str, idea_id: str
) -> str | None:
    """Fail the advance work item the last tick enqueued, terminally.

    Through the real queue rather than by writing rows, because the thing
    under test is what the queue's permanent ``dedup_key`` index does to the
    next tick. The stage is returned so a caller can assert which one died.
    """

    queue = WorkQueue(runtime_db)
    owner = "worker-under-test"
    while True:
        claimed = queue.claim(owner=owner, lease_seconds=60, limit=1)
        if not claimed:
            return None
        item = claimed[0]
        if (
            item.kind == allocation.ADVANCE_IDEA
            and item.payload.get("idea_id") == idea_id
        ):
            queue.fail(
                item.work_id,
                owner=owner,
                failure_class=FailureClass.CODE_EXCEPTION,
                error="KeyError: <ModelRole.NOVELTY_SCREENER: 'novelty_screener'>",
                force_terminal=True,
            )
            return str(item.payload.get("stage") or "")
        queue.succeed(item.work_id, owner=owner, result={})


def test_a_failed_stage_can_be_bought_again(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The dedup key must not outlive the work item it deduplicates.

    ``work_items.dedup_key`` is a permanent unique index and ``enqueue`` is
    ``on conflict do nothing``, so a key naming only the idea and the stage is
    spent the moment that pair first fails. The first dogfood ran an hour of
    ticks that each allocated the same eight advances and enqueued none of
    them, reporting RUNNING the whole time -- and fixing the routing defect
    underneath did not recover it, because the keys were still spent.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert first.work_enqueued >= 1
    stage = _fail_the_queued_advance(runtime_db, runtime_project, idea.idea_id)
    assert stage, "nothing was enqueued for this idea, so this test proves nothing"
    portfolio.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)

    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    again = [
        item
        for item in second.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ]
    assert again, "the allocator stopped choosing an idea it had not finished"
    assert second.work_enqueued >= 1, (
        "the stage was allocated again and the queue refused it: the dedup key "
        "outlived the failed work item"
    )
    # The mechanism, named. The retry is a *different* key from the one the
    # failed item holds forever, and the generation is what makes it different.
    retried = next(item for item in again if item.stage is not None)
    assert retried.generation == 1
    assert retried.dedup_key.endswith(":1")
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select status from work_items where dedup_key = %s",
            (retried.dedup_key,),
        ).fetchone()
    assert row is not None, "the generation-1 key bought nothing"
    assert str(row["status"]) != "FAILED"


def test_retrying_a_failing_stage_has_a_ceiling(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Invariant 14. A retry with no bound is the loop, wearing a new name.

    Past the ceiling the idea is ``BLOCKED_EXTERNAL`` -- what is missing is a
    person fixing something -- and *not* REJECTED or PARKED, because those
    would write an infrastructure failure down as a decision about the
    science. Same rule as an exhausted budget.
    """

    config = load_config()
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    for _ in range(config.bounds.max_stage_failures):
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
        assert _fail_the_queued_advance(runtime_db, runtime_project, idea.idea_id)
        portfolio.set_operational_state(
            idea_id=idea.idea_id, state=OperationalState.IDLE
        )

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert not [
        item
        for item in report.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ]
    blocked = portfolio.get_idea(idea.idea_id)
    assert blocked is not None
    assert blocked.operational_state is OperationalState.BLOCKED_EXTERNAL
    assert blocked.status is IdeaStatus.PROMISING, (
        "a stage that keeps failing is not a scientific verdict on the idea"
    )


def test_a_pass_that_buys_nothing_it_decided_on_says_so(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Deciding and not doing must not look like having nothing to do.

    Every other field of the tick report is identical between the two.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    moment = datetime.now(UTC)

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    assert first.work_enqueued >= 1
    assert first.work_refused == 0
    assert not any("enqueued none" in note for note in first.notes)

    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    assert second.work_enqueued == 0
    assert second.work_refused == len(
        [item for item in second.allocations if item.kind == allocation.ADVANCE_IDEA]
    )
    assert any("enqueued none" in note for note in second.notes)


def test_a_portfolio_whose_every_idea_is_blocked_pauses_and_stops_exploring(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """``PAUSED_BLOCKED_EXTERNAL`` had a condition that could not hold.

    The same shape as the dead ``PAUSED_NO_FRONTIER`` branch §N.9 removed: it
    required ``not allocations``, and a portfolio with a free slot and a pool
    under its ceiling always allocates an explorer -- so the status was
    reachable in the enum, the CLI and §4.3, and not in the code.

    It matters more than a tidy enum. Exploring past this spends $0.60 a
    cadence producing ideas that meet the same blocked stage, and the eventual
    stop is then reported as ``PAUSED_BUDGET_EXHAUSTED``: the wrong cause,
    which is the mistake §N.8 is about.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    portfolio.set_operational_state(
        idea_id=idea.idea_id, state=OperationalState.BLOCKED_EXTERNAL
    )

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    assert report.status is PortfolioStatus.PAUSED_BLOCKED_EXTERNAL
    assert not any(item.kind == allocation.EXPLORE for item in report.allocations)
    assert report.work_enqueued == 0
    assert "BLOCKED_EXTERNAL" in (portfolio.get_state(runtime_project).detail or "")


def test_a_stage_that_succeeded_on_its_second_attempt_is_not_bought_again(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """A retry *inside* a work item is not a failure of that work item.

    The soak caught this against the first version of the generation counter,
    which counted FAILED ``idea_actions`` rows. A falsifier call returned
    ``error_max_structured_output_retries``; the queue retried the same item
    on its own backoff and the second attempt succeeded -- which is the R5
    retry machinery working exactly as designed. But the first attempt had
    already written a FAILED action row, so the generation advanced, the key
    changed, and the tick bought a *second* work item for a stage that had
    just succeeded.

    Counting terminally failed work items instead makes the two cases
    different, which they are: the queue exhausting its attempts is a failure,
    and the queue succeeding on attempt two is not.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert first.work_enqueued >= 1

    # One failed attempt, then success -- on the same work item, the way the
    # queue's own backoff does it.
    queue = WorkQueue(runtime_db)
    owner = "worker-under-test"
    item = next(
        claimed
        for claimed in queue.claim(owner=owner, lease_seconds=60, limit=5)
        if claimed.kind == allocation.ADVANCE_IDEA
        and claimed.payload.get("idea_id") == idea.idea_id
    )
    stage = str(item.payload.get("stage") or "")
    queue.fail(
        item.work_id,
        owner=owner,
        failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        error="claude did not answer: error_max_structured_output_retries",
    )
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage(stage),
        basis_digest="attempt-one",
    )
    portfolio.complete_action(
        action_id=action.action_id,
        status=ActionStatus.FAILED,
        failure_class="provider_unavailable",
    )
    # A retryable failure reschedules rather than failing the item, so the
    # count must already be zero here -- before the retry has even run.
    assert (
        portfolio.failed_stage_counts(runtime_project).get((idea.idea_id, stage), 0)
        == 0
    ), "an item that is going to be retried was counted as failed"

    # Now let the retry happen and succeed, as the soak's falsifier did.
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set scheduled_at = now() - interval '1 minute' "
            "where work_id = %s",
            (item.work_id,),
        )
    retried = next(
        claimed
        for claimed in queue.claim(owner=owner, lease_seconds=60, limit=5)
        if claimed.work_id == item.work_id
    )
    assert retried.attempts == 2
    queue.succeed(retried.work_id, owner=owner, result={})

    counts = portfolio.failed_stage_counts(runtime_project)
    assert counts.get((idea.idea_id, stage), 0) == 0, (
        "a work item that succeeded on its second attempt was counted as a "
        "failure, so the next tick will buy the same stage again"
    )


def test_an_advance_that_ran_no_stage_does_not_spend_the_next_ones_key(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """A worker killed mid-stage, the documented retry, and the idea after it.

    The final hostile review's reproduction, through the real queue, handler
    and tick. The retry meets the dead worker's still-open action, runs no
    stage ("another pass is already doing this"), and is recorded SUCCEEDED.
    The generation used to count only *failed* items, so when the reconciler
    freed the action and the allocator chose the stage again it built the
    same key, and the queue refused it on every tick after -- an idea that
    looked IDLE and unblocked and was never worked again.
    """

    from research_os.portfolio import extensions as portfolio_extensions
    from research_os.portfolio import track
    from research_os.runtime.daemon import Daemon, TickReport
    from research_os.runtime.models import WorkStatus
    from tests.runtime_graph_helpers import ScriptedRouter, make_capsule

    portfolio_extensions.register()
    repo = make_capsule(tmp_path / "project")
    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(repo)
    )
    daemon = Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: ScriptedRouter(
            answers={}, store=RuntimeStore(runtime_db)
        ),
        owner="restarted-worker",
    )
    queue = WorkQueue(runtime_db)
    idea, _ = seed_idea(portfolio, runtime_project)
    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert any(
        item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
        for item in first.allocations
    )

    # A worker takes it, opens the idea's action, and is killed.
    (dead,) = queue.claim(
        owner="dead-worker", lease_seconds=120, kinds=(allocation.ADVANCE_IDEA,)
    )
    portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.DEDUP,
        basis_digest=track._basis_for(
            portfolio, idea_id=idea.idea_id, version=1, stage=Stage.DEDUP
        ),
        thread_id="thread-of-the-dead-worker",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set lease_expires_at = now() - interval '1 second' "
            "where work_id = %s",
            (dead.work_id,),
        )

    # The restarted daemon reclaims the lease and runs the retry, which finds
    # the action held and does nothing.
    assert [item.work_id for item in queue.reclaim_expired()] == [dead.work_id]
    (retry,) = queue.claim(
        owner=daemon.owner, lease_seconds=120, kinds=(allocation.ADVANCE_IDEA,)
    )
    daemon._run_item(retry, TickReport())
    assert queue.get(dead.work_id).status is WorkStatus.SUCCEEDED

    # The reconciler frees the orphaned action.
    with runtime_db.tx() as conn:
        conn.execute(
            "update idea_actions set updated_at = now() - interval '2 hours' "
            "where idea_id = %s",
            (idea.idea_id,),
        )
    freed = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert freed.stale_actions_reclaimed == 1

    # And the stage is bought again, under a new key.
    mine = [
        item
        for item in freed.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ] or [
        item
        for item in _tick(runtime_db, pg_dsn, tmp_path, runtime_project).allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ]
    assert mine, "the allocator stopped choosing an idea nobody worked"
    assert mine[0].dedup_key != dead.dedup_key
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select status from work_items where dedup_key = %s", (mine[0].dedup_key,)
        ).fetchone()
    assert row is not None and str(row["status"]) == "PENDING", (
        "the stage was chosen again and the queue refused it"
    )


def test_a_revision_can_re_run_the_cheap_ladder(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """The defect that actually cost the first soak its deep stages.

    `succeeded_stages_for_version` is version-scoped, so after a revision the
    stage machine correctly asks for dedup, the novelty screen and the
    falsifier again -- against content that is now different. The dedup key
    named only the idea and the stage, so those keys had been spent by the
    *previous* version's runs, which SUCCEEDED, and `on conflict do nothing`
    refused them forever.

    Every idea that reached PROMISING and was then sharpened was therefore
    wedged permanently at the bottom of its own re-run ladder. The first
    soak's report blamed throughput for never reaching the literature audit
    or the review board. Throughput was real; this was the cause. It became
    visible the moment `work_refused` existed to show a tick allocating eight
    items and buying none of them.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    v1 = [
        item
        for item in first.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ]
    assert v1, "nothing was allocated, so this test proves nothing"
    stage = v1[0].stage
    assert v1[0].dedup_key.endswith(":v1:0")

    # Run it to success on version 1, exactly as the real ladder does.
    queue = WorkQueue(runtime_db)
    owner = "worker-under-test"
    for item in queue.claim(owner=owner, lease_seconds=60, limit=5):
        queue.succeed(item.work_id, owner=owner, result={})
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=stage,
        basis_digest="v1",
    )
    portfolio.complete_action(action_id=action.action_id, status=ActionStatus.SUCCEEDED)

    # Now sharpen it. A new version, and the cheap ladder is owed again.
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(research_question="a sharper question entirely"),
        origin_call_id=None,
        origin_role="scientific_discovery",
        origin_stage=str(Stage.DISCOVER),
    )
    portfolio.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)

    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    v2 = [
        item
        for item in second.allocations
        if item.kind == allocation.ADVANCE_IDEA and item.idea_id == idea.idea_id
    ]
    assert v2, "the stage machine stopped asking for the re-run"
    assert v2[0].stage is stage
    assert v2[0].dedup_key.endswith(":v2:0"), (
        f"the key does not name the version: {v2[0].dedup_key}"
    )
    assert second.work_enqueued >= 1, (
        "the cheap ladder was owed again for version 2 and the queue refused "
        "it: the dedup key outlived the version it was built for"
    )
    # Any refusal left is the explorer sharing this tick's minute bucket,
    # which is the key doing its job. The advance is what must get through.
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select count(*) as n from work_items where dedup_key = %s",
            (v2[0].dedup_key,),
        ).fetchone()
    assert int(row["n"]) == 1


def test_resume_lets_a_stage_that_hit_its_ceiling_be_tried_again(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """A ceiling that never decays is a wedge, not a bound.

    `max_stage_failures` stops the allocator choosing a stage that will never
    succeed, and `BLOCKED_EXTERNAL` says what is missing is a person fixing
    something. What had no answer is *after* they fix it: the count is over
    failed work items and never decays, so `portfolio resume` returned the
    ideas to IDLE and the next tick read the same historical count and
    blocked them again.

    Measured on 2026-09-22 on the three empirical ideas of
    `cg-sparse-regression`, whose evidence stage had failed while the
    experiment route did not exist and which could not be retried once it
    did.

    The all-time count must keep moving -- `work_items.dedup_key` is built
    from it against a permanently unique index -- so this asserts both: the
    ceiling forgives and the key does not repeat.
    """

    from research_os.portfolio.allocation import ADVANCE_IDEA
    from research_os.runtime.models import WorkStatus
    from research_os.runtime.queue import WorkQueue

    idea, version = seed_idea(portfolio, runtime_project)
    portfolio.upsert_state(project_id=runtime_project)
    queue = WorkQueue(runtime_db)
    config = load_config()

    key = (idea.idea_id, "falsify", str(version.version))
    for index in range(config.bounds.max_stage_failures):
        item = queue.enqueue(
            project_id=runtime_project,
            kind=ADVANCE_IDEA,
            payload={
                "idea_id": idea.idea_id,
                "stage": "falsify",
                "idea_version": version.version,
            },
            dedup_key=f"{ADVANCE_IDEA}:{idea.idea_id}:falsify:v1:{index}",
        )
        assert item.created
        with runtime_db.tx() as conn:
            conn.execute(
                "update work_items set status = %s, updated_at = now() "
                "where work_id = %s",
                (str(WorkStatus.FAILED), item.item.work_id),
            )

    ceiling = config.bounds.max_stage_failures
    assert portfolio.stage_failures(runtime_project)[key] == (ceiling, ceiling, 0)

    portfolio.forgive_stage_failures(project_id=runtime_project)

    total, recent, refusals = portfolio.stage_failures(runtime_project)[key]
    assert total == ceiling, (
        "the all-time count moved, so a retry would re-use a spent dedup key"
    )
    assert recent == 0, "resume did not forgive the ceiling"
    assert refusals == 0, "these failures carried no class, so none is a refusal"


def test_a_refusal_blocks_the_idea_once_rather_than_three_times(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Buying the same "no" three times is not a retry policy.

    `max_stage_failures` exists so a *transient* is retried and then stops.
    A refusal is not transient: "no declared command can test this idea" is
    the same answer next time and the time after, and each attempt is a paid
    frontier call. Six identical refusals were bought across two sessions
    before anyone counted them.

    The classes here are the ones the failure taxonomy already calls
    terminal, so this is the allocator agreeing with the policy table rather
    than a new policy. `portfolio resume` is what reconsiders it, which is
    what the blocked state has always meant.
    """

    from research_os.portfolio.allocation import ADVANCE_IDEA
    from research_os.portfolio.store import REFUSAL_CLASSES
    from research_os.runtime.failures import FailureClass, Response, response_for
    from research_os.runtime.models import WorkStatus
    from research_os.runtime.queue import WorkQueue

    for item in REFUSAL_CLASSES:
        assert response_for(item) in {
            Response.FAIL_PERMANENTLY,
            Response.INTERRUPT_FOR_HUMAN,
        }, f"{item} is not terminal in the taxonomy, so one must not block"

    idea, version = seed_idea(portfolio, runtime_project)
    portfolio.upsert_state(project_id=runtime_project)
    queue = WorkQueue(runtime_db)

    result = queue.enqueue(
        project_id=runtime_project,
        kind=ADVANCE_IDEA,
        payload={
            "idea_id": idea.idea_id,
            "stage": "evidence",
            "idea_version": version.version,
        },
        dedup_key=f"{ADVANCE_IDEA}:{idea.idea_id}:evidence:v1:0",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set status = %s, failure_class = %s, "
            "updated_at = now() where work_id = %s",
            (
                str(WorkStatus.FAILED),
                str(FailureClass.CAPABILITY_DENIED),
                result.item.work_id,
            ),
        )

    total, recent, refusals = portfolio.stage_failures(runtime_project)[
        (idea.idea_id, "evidence", str(version.version))
    ]
    assert (total, recent, refusals) == (1, 1, 1)

    # And a transient does not count as one, or the ceiling would collapse
    # to a single retry for everything.
    other = queue.enqueue(
        project_id=runtime_project,
        kind=ADVANCE_IDEA,
        payload={
            "idea_id": idea.idea_id,
            "stage": "falsify",
            "idea_version": version.version,
        },
        dedup_key=f"{ADVANCE_IDEA}:{idea.idea_id}:falsify:v1:0",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set status = %s, failure_class = %s, "
            "updated_at = now() where work_id = %s",
            (
                str(WorkStatus.FAILED),
                str(FailureClass.PROVIDER_UNAVAILABLE),
                other.item.work_id,
            ),
        )
    assert portfolio.stage_failures(runtime_project)[
        (idea.idea_id, "falsify", str(version.version))
    ] == (1, 1, 0)


def test_a_portfolio_stage_failure_reaches_the_queue_with_its_class(
    runtime_project: str,
) -> None:
    """Twenty-seven failures were recorded as `unknown`, which is three losses.

    `Daemon._classify` believes an exception that states its class and falls
    through to `UNKNOWN` otherwise, and the portfolio raised a bare
    `ResearchOSError` for everything that was not a provider failure. So
    `portfolio status` could not say why anything failed, the retry policy
    could not tell an outage from a policy answer, and the allocator could
    not tell a refusal from a transient -- which is what made the rule above
    unimplementable before it.
    """

    from research_os.portfolio import extensions
    from research_os.runtime.failures import FailureClass

    for item in (
        FailureClass.CAPABILITY_DENIED,
        FailureClass.POLICY_REFUSED,
        FailureClass.MODEL_OUTPUT_INVALID,
        FailureClass.EXECUTOR_FAILED,
        FailureClass.BUDGET_EXHAUSTED,
    ):
        error = extensions._as_error(item, "a detail")
        assert getattr(error, "failure_class", None) is item, (
            f"{item} reaches the queue as unknown"
        )

    # A provider failure still raises what the router raises, so the queue
    # schedules against the breaker's cooldown rather than a stopwatch.
    from research_os.runtime.routing import ProviderCallFailedError

    outage = extensions._as_error(FailureClass.PROVIDER_UNAVAILABLE, "no provider")
    assert isinstance(outage, ProviderCallFailedError)
    assert outage.failure_class is FailureClass.PROVIDER_UNAVAILABLE
