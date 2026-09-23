"""The portfolio tick: bounded, deterministic, and it ends.

The node names are the brief's §26 -- hydrate, inspect capacity, reconcile,
replenish, allocate, launch, collect, update the bank, schedule the digest --
and the pipeline is bounded and ends, with ``researchd`` invoking the next one.
What it is *not* is a LangGraph, and that is a recorded deviation rather than
an oversight.

`ARCHITECTURE.md` §12a adopted LangGraph for one measured property: durable
checkpointing across interrupt and process death. A tick makes no model call,
has no interrupt, completes in milliseconds, is idempotent, and reconstructs
its whole input from the database on the next pass. There is nothing to
checkpoint, and writing checkpoint rows every ``tick_seconds`` per project
forever would be a cost buying no property -- which is the shape of adoption
§12 exists to prevent. See docs/adr/0004.

**The release-critical property lives here**, in ``inspect_capacity``: capacity
counts ideas whose operational state is ACTIVE. A ``HUMAN_READY`` idea has no
track, so it holds no slot, so one idea waiting for the researcher does not
stop the portfolio. There is deliberately no ``WAIT_HUMAN`` state for this
function to reach.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from research_os.portfolio import allocation
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.models import (
    ActionStatus,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    PortfolioStatus,
    RequestKind,
    RequestState,
)
from research_os.portfolio.stages import select_stage, snapshot_for
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.portfolio.tick")


@dataclass
class TickReport:
    """What one pass of the portfolio did.

    Returned rather than logged-and-forgotten, so the loop can be driven by a
    test and asserted on -- the same reason ``daemon.TickReport`` exists.
    """

    project_id: str
    status: PortfolioStatus = PortfolioStatus.RUNNING
    active_tracks: int = 0
    free_slots: int = 0
    candidate_pool: int = 0
    stale_actions_reclaimed: int = 0
    blocks_cleared: int = 0
    #: Ideas with nothing left to run that this pass gave an explicit state.
    settled: int = 0
    #: Capability-blocked ideas released because the declared commands changed.
    capability_unblocked: int = 0
    open_requests: int = 0
    allocations: tuple[allocation.Allocation, ...] = ()
    work_enqueued: int = 0
    #: Allocations the queue already had an item for, so nothing was enqueued.
    #: One or two of these is a tick racing itself and is normal; every
    #: allocation in a pass refused, pass after pass, is the portfolio
    #: deciding and not doing -- which is what the first dogfood spent an hour
    #: doing while reporting RUNNING.
    work_refused: int = 0
    uncurated: int = 0
    curation_enqueued: bool = False
    digest_enqueued: bool = False
    notes: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "status": str(self.status),
            "active_tracks": self.active_tracks,
            "free_slots": self.free_slots,
            "candidate_pool": self.candidate_pool,
            "stale_actions_reclaimed": self.stale_actions_reclaimed,
            "blocks_cleared": self.blocks_cleared,
            "settled": self.settled,
            "capability_unblocked": self.capability_unblocked,
            "open_requests": self.open_requests,
            "work_enqueued": self.work_enqueued,
            "work_refused": self.work_refused,
            "uncurated": self.uncurated,
            "curation_enqueued": self.curation_enqueued,
            "digest_enqueued": self.digest_enqueued,
            "allocations": [
                {
                    "kind": item.kind,
                    "idea_id": item.idea_id,
                    "stage": str(item.stage) if item.stage else None,
                    "explorer": item.explorer,
                    "utility": str(item.utility),
                    "reason": item.reason,
                }
                for item in self.allocations
            ],
            "notes": list(self.notes),
        }


def tick(
    *,
    db: Database,
    project_id: str,
    runtime_config: RuntimeConfig,
    portfolio_config: PortfolioConfig,
    now: datetime | None = None,
) -> TickReport:
    """One bounded pass. Reads the portfolio, decides, enqueues, ends."""

    moment = now or datetime.now(UTC)
    store = PortfolioStore(db)
    runtime = RuntimeStore(db)
    queue = WorkQueue(db)
    budgets = BudgetLedger(db)

    # --- hydrate ---------------------------------------------------------
    state = store.get_state(project_id) or store.upsert_state(project_id=project_id)
    config = portfolio_config.with_overrides(state.bounds)
    report = TickReport(project_id=project_id, status=state.status)

    if state.status is PortfolioStatus.PAUSED_BY_RESEARCHER:
        report.notes.append("paused by the researcher; nothing is allocated")
        store.touch_tick(project_id)
        return report

    # --- reconcile -------------------------------------------------------
    report.stale_actions_reclaimed = _reclaim_stale(store, project_id, config)
    report.blocks_cleared = _clear_blocks(store, runtime, project_id)
    report.capability_unblocked = _observe_capability(store, report, state)

    # --- inspect capacity ------------------------------------------------
    report.active_tracks = store.active_count(project_id)
    report.free_slots = max(0, config.bounds.max_active_tracks - report.active_tracks)
    counts = store.counts_by_status(project_id)
    report.candidate_pool = counts.get(IdeaStatus.CANDIDATE, 0)

    pending_seeds = len(store.pending_seeds(project_id=project_id))
    requests = store.list_requests(
        project_id=project_id,
        states=(RequestState.OPEN,),
        kinds=(RequestKind.FOLLOW_UP,),
        limit=50,
    )
    literature_requests = store.list_requests(
        project_id=project_id,
        states=(RequestState.OPEN,),
        kinds=(RequestKind.LITERATURE,),
        limit=50,
    )
    report.open_requests = len(requests) + len(literature_requests)

    # --- the four reasons a portfolio may stop ---------------------------
    # Read the project and system ceilings directly rather than through
    # `exhausted_dimensions`, which takes a run id: a tick is not a run, and
    # inventing one to ask the question would put a row in `research_runs` that
    # never runs anything.
    spent_out = [
        (scope, dimension)
        for scope, scope_id in (
            (BudgetScope.PROJECT, project_id),
            (BudgetScope.SYSTEM, "system"),
        )
        for dimension in (Dimension.MODEL_COST_USD, Dimension.MODEL_CALLS)
        if (record := budgets.get(scope=scope, scope_id=scope_id, dimension=dimension))
        is not None
        and record.exhausted
    ]
    if spent_out:
        return _pause(
            store,
            report,
            PortfolioStatus.PAUSED_BUDGET_EXHAUSTED,
            f"{spent_out[0][0]} budget for {spent_out[0][1]} is spent. Raise it "
            f"with `researchctl runtime budget --max-cost-usd` and the next tick "
            f"resumes; nothing here converts that into a scientific rejection.",
        )

    in_flight = store.explorations_in_flight(project_id=project_id)

    # Exploration that produces nothing is the sixth way a portfolio can loop,
    # and before this the budget ceiling was the only thing that stopped it --
    # which worked, and reported the wrong cause. A new seed clears the pause,
    # because a seed is exactly the information the count says is missing.
    barren = store.barren_explorations(project_id=project_id)
    # An open request is information the explorers did not have, exactly as
    # a seed is, so it holds off the pause for the same reason.
    if (
        barren >= config.bounds.max_barren_explorations
        and not pending_seeds
        and not requests
        and not literature_requests
    ):
        return _pause(
            store,
            report,
            PortfolioStatus.PAUSED_NO_FRONTIER,
            f"{barren} explorer run(s) in a row produced no new idea: this "
            f"project's idea space looks exhausted to the explorers available "
            f"here. `researchctl seed add` gives them somewhere else to look.",
        )

    candidates, report.settled = _candidates(store, project_id, config, moment)
    ideas = store.list_ideas(project_id=project_id, limit=500)
    blocked_externally = [
        item
        for item in ideas
        if item.operational_state
        in {OperationalState.BLOCKED_EXTERNAL, OperationalState.BLOCKED_PROVIDER}
    ]
    live = [
        item
        for item in ideas
        if item.status
        not in {IdeaStatus.REJECTED, IdeaStatus.SUPERSEDED, IdeaStatus.HUMAN_READY}
    ]

    # --- replenish and allocate ------------------------------------------
    allocations = allocation.plan(
        candidates=candidates,
        config=config,
        free_slots=report.free_slots,
        lineage_active=store.lineage_active_counts(project_id),
        candidate_pool=report.candidate_pool,
        pending_seeds=pending_seeds,
        origin_counts=_origin_counts(ideas),
        minable_failures=counts.get(IdeaStatus.REJECTED, 0)
        + counts.get(IdeaStatus.PARKED, 0),
        tick_bucket=moment.strftime("%Y%m%dT%H%M"),
        explorers_in_flight=in_flight,
        open_requests=[
            (item.request_id, item.attempts, str(item.basis)) for item in requests
        ],
        follow_ups_in_flight=store.work_in_flight(
            project_id=project_id, kind=allocation.FOLLOW_UP
        ),
        literature_requests=[
            (item.request_id, item.attempts, str(item.basis))
            for item in literature_requests
        ],
        literature_in_flight=store.work_in_flight(
            project_id=project_id, kind=allocation.LITERATURE_REQUEST
        ),
        frontier_claims=len(
            store.list_literature_claims(
                project_id=project_id, frontier_only=True, limit=50
            )
        ),
    )
    report.allocations = allocations

    # There was a second branch here -- `PAUSED_NO_FRONTIER` when nothing was
    # allocatable and the pool was empty -- and it could not fire. An empty
    # pool with a free slot is exactly the state in which the allocator buys a
    # blind explorer, so `allocations` was never empty while the rest of the
    # condition held. The status it reached for is real; what detects it is
    # `barren_explorations` above, which asks whether generating has *worked*
    # rather than whether it is possible.
    #
    # `PAUSED_BLOCKED_EXTERNAL` had the same shape of defect and it survived
    # the fix above: the condition was `not allocations`, and an explorer is an
    # allocation. A portfolio whose every live idea is blocked still has a free
    # slot and a pool under its ceiling, so it still buys an explorer, so
    # `allocations` was never empty and this branch could not fire either.
    #
    # `§4.3` says the condition is "every *allocatable idea* is blocked
    # externally", which is what this now asks. Exploring past it is not
    # harmless: the new ideas meet the same blocked stage, and spending $0.60 a
    # cadence to keep producing them reports the eventual stop as
    # `PAUSED_BUDGET_EXHAUSTED` -- the wrong cause, which is precisely the
    # mistake §N.8 was about.
    idea_allocations = [
        item for item in allocations if item.kind == allocation.ADVANCE_IDEA
    ]
    if (
        not idea_allocations
        and not any(
            item.kind in {allocation.FOLLOW_UP, allocation.LITERATURE_REQUEST}
            for item in allocations
        )
        and report.active_tracks == 0
        and live
        and blocked_externally
        and len(blocked_externally) >= len(live)
    ):
        return _pause(
            store,
            report,
            PortfolioStatus.PAUSED_BLOCKED_EXTERNAL,
            f"every one of {len(live)} live idea(s) is waiting on something "
            f"outside this machine: "
            + ", ".join(
                sorted({str(item.operational_state) for item in blocked_externally})
            )
            + ". `researchctl portfolio status` lists the failures that got "
            "them there.",
        )

    # --- launch ----------------------------------------------------------
    for item in allocations:
        enqueued = queue.enqueue(
            project_id=project_id,
            kind=item.kind,
            payload={
                "idea_id": item.idea_id,
                "stage": str(item.stage) if item.stage else None,
                # Provenance, and the key `failed_stage_counts` groups by, so
                # the count it returns and the key the allocator builds are
                # derived from the same three things.
                "idea_version": (
                    str(item.idea_version) if item.idea_version is not None else None
                ),
                "explorer": item.explorer,
                "reason": item.reason,
                "utility": str(item.utility),
                **(
                    {"request_id": item.payload.get("request_id")}
                    if item.kind
                    in {allocation.FOLLOW_UP, allocation.LITERATURE_REQUEST}
                    else {}
                ),
            },
            dedup_key=item.dedup_key,
            priority=100,
        )
        if enqueued.created:
            report.work_enqueued += 1
        else:
            report.work_refused += 1

    if report.work_refused and not report.work_enqueued and allocations:
        # Not a note for the sake of one. A pass that decided on N things and
        # bought none of them is indistinguishable, in every other field of
        # this report, from a pass with nothing to do.
        report.notes.append(
            f"allocated {len(allocations)} item(s) and enqueued none: the queue "
            f"already holds an item for each. If this repeats, those items are "
            f"not being worked -- `researchctl portfolio status` lists failures."
        )

    # --- update the bank -------------------------------------------------
    report.uncurated = store.uncurated_count(project_id)
    if report.uncurated:
        enqueued = queue.enqueue(
            project_id=project_id,
            kind=allocation.CURATE,
            payload={"uncurated": report.uncurated},
            dedup_key=f"{allocation.CURATE}:{project_id}:{moment.strftime('%Y%m%dT%H%M')}",
        )
        report.curation_enqueued = enqueued.created

    # --- schedule the digest ---------------------------------------------
    due = state.last_digest_at is None or (
        (moment - state.last_digest_at).total_seconds() >= config.cadence.digest_seconds
    )
    if due:
        enqueued = queue.enqueue(
            project_id=project_id,
            kind=allocation.DIGEST,
            payload={
                "since": state.last_digest_at.isoformat()
                if state.last_digest_at
                else None
            },
            dedup_key=f"{allocation.DIGEST}:{project_id}:{moment.strftime('%Y%m%d')}",
        )
        report.digest_enqueued = enqueued.created

    if state.status is not PortfolioStatus.RUNNING:
        # A pause whose cause has cleared. Resumed by the tick rather than by a
        # person, because every pause below RESEARCHER is a condition and not a
        # decision -- and a system that needed a human to notice the budget was
        # raised would have made a resource question into an attention question.
        store.set_portfolio_status(
            project_id=project_id,
            status=PortfolioStatus.RUNNING,
            detail="the condition that paused this portfolio has cleared",
        )
        report.notes.append("resumed")
    report.status = PortfolioStatus.RUNNING
    store.touch_tick(project_id)
    return report


def _pause(
    store: PortfolioStore,
    report: TickReport,
    status: PortfolioStatus,
    detail: str,
) -> TickReport:
    store.set_portfolio_status(
        project_id=report.project_id, status=status, detail=detail
    )
    store.touch_tick(report.project_id)
    report.status = status
    # A paused pass bought nothing, so it must not report allocations. The
    # blocked-external pause is computed *after* the plan, and leaving the
    # plan in the report made a pass that enqueued zero items list one.
    report.allocations = ()
    report.notes.append(detail)
    return report


def _reclaim_stale(
    store: PortfolioStore, project_id: str, config: PortfolioConfig
) -> int:
    """Free ideas whose track died without saying so.

    The portfolio's ``stranded_runs``, and it exists for the same reason: a
    worker killed between claiming a stage and completing it leaves an idea
    ACTIVE forever, which removes it from allocation silently. Silently is the
    part that matters -- the idea is not rejected, not parked, not blocked, and
    not in the digest as any of those. It is simply never chosen again.
    """

    reclaimed = 0
    for action in store.stale_actions(
        project_id=project_id,
        older_than_seconds=config.cadence.stale_action_grace_seconds,
    ):
        store.complete_action(
            action_id=action.action_id,
            status=ActionStatus.FAILED,
            detail="the worker holding this stage is gone",
            failure_class="worker_crash",
            operational_state=OperationalState.IDLE,
        )
        reclaimed += 1
    return reclaimed


def _clear_blocks(store: PortfolioStore, runtime: RuntimeStore, project_id: str) -> int:
    """Return ideas to IDLE when what blocked them has recovered.

    Only ``BLOCKED_PROVIDER`` is cleared automatically, and only against the
    provider health the router maintains. ``BLOCKED_BUDGET`` clears when a
    person raises a ceiling, ``BLOCKED_EXTERNAL`` when the capability appears;
    neither is something this function can observe, so neither is guessed at.
    """

    healthy = {item.provider for item in runtime.provider_health() if item.healthy}
    if not healthy:
        return 0
    cleared = 0
    for idea in store.list_ideas(
        project_id=project_id,
        operational=[OperationalState.BLOCKED_PROVIDER],
        limit=200,
    ):
        store.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)
        cleared += 1
    return cleared


def _observe_capability(store: PortfolioStore, report: TickReport, state: Any) -> int:
    """Release ideas blocked on a capability when the declared commands change.

    ``_clear_blocks`` says a capability arriving is not a fact the tick can
    read. For the empirical route it now is: the declared commands live in
    ``experiments.yaml``, outside every worktree, so a change to them is a
    *person's* act -- exactly what ``portfolio resume`` stands for -- and the
    tick can observe it by digest. A capability-blocked contract records the
    command set it was judged against; when that differs from today's, its
    idea is released and the stage-failure watermark moves, so the refusal
    that blocked it is not counted against the retry. The idea resumes from
    its frozen analysis: nothing scientific is re-decided.

    The first observation only records the digest. Nothing had changed.
    """

    from research_os.portfolio.models import ContractState
    from research_os.portfolio.scicontract import (
        command_set_digest,
        declared_command_set,
    )

    current = command_set_digest(declared_command_set(report.project_id))
    previous = state.command_set_digest
    if previous == current:
        return 0
    store.set_command_set_digest(project_id=report.project_id, digest=current)
    if previous is None:
        return 0
    released = 0
    for contract in store.list_contracts(
        project_id=report.project_id, states=[ContractState.BLOCKED_CAPABILITY]
    ):
        if contract.command_set_digest == current:
            continue
        idea = store.get_idea(contract.idea_id)
        if (
            idea is None
            or idea.operational_state is not OperationalState.BLOCKED_EXTERNAL
        ):
            continue
        store.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)
        released += 1
    if released:
        store.forgive_stage_failures(project_id=report.project_id)
        report.notes.append(
            f"the declared experiment commands changed; {released} idea(s) "
            f"blocked on a capability were released to resume from their "
            f"frozen analysis"
        )
    return released


def _origin_counts(ideas: Sequence[Any]) -> dict[IdeaOrigin, int]:
    counts: dict[IdeaOrigin, int] = {}
    for idea in ideas:
        counts[idea.origin] = counts.get(idea.origin, 0) + 1
    return counts


def _candidates(
    store: PortfolioStore,
    project_id: str,
    config: PortfolioConfig,
    moment: datetime,
) -> tuple[tuple[allocation.Candidate, ...], int]:
    """Every idea the allocator may choose from, with its next stage.

    The next stage is computed here rather than by the handler, because an
    idea with no next stage is not a candidate at all -- allocating work to it
    would enqueue an item that does nothing.
    """

    from research_os.portfolio import frontier

    found: list[allocation.Candidate] = []
    settled = 0
    failures = store.stage_failures(project_id)
    for idea in store.list_ideas(project_id=project_id, limit=500):
        if not allocation.allocatable(idea):
            continue
        # The allocator's snapshot, `advance_idea`'s and `ideas show`'s must
        # agree about what runs next, and the only way to be sure of that is
        # for there to be one of them. A field present in one and absent from
        # another is two stage machines wearing one name -- which is exactly
        # what `ideas show` turned out to be until `snapshot_for` existed.
        snapshot = snapshot_for(
            store,
            idea,
            max_review_age_seconds=config.thresholds.review_max_age_seconds,
        )
        if snapshot is None:
            continue
        version = snapshot.version
        stage, reason = select_stage(snapshot, config)
        if stage is None:
            # Nothing left to run and not settled: give it an explicit state
            # rather than leaving it unallocatable in limbo. The track does
            # this when a stage ends it; this catches an idea that reached
            # the same place any other way -- including every one a build
            # before `frontier.settle` existed left there.
            if frontier.settle(store, idea, snapshot, config):
                settled += 1
            continue
        failed_attempts, recent_failures, refusals = failures.get(
            (idea.idea_id, str(stage), str(version.version)), (0, 0, 0)
        )
        # Three numbers, three readers. The dedup key reads the all-time
        # count, because it is built from it against a permanently unique
        # index and a key that repeats is a retry `enqueue` refuses silently.
        # The ceiling reads the *recent* count, so a stage that failed while
        # a capability was missing is retryable once `portfolio resume` says
        # it arrived. And a *refusal* counts once: "no declared command can
        # test this idea" is the same answer next time, and each attempt is
        # a paid frontier call. See `PortfolioStore.stage_failures`.
        if refusals >= 1 or recent_failures >= config.bounds.max_stage_failures:
            # Retried to its ceiling and still failing, so this is not a
            # transient. `BLOCKED_EXTERNAL` rather than a status change,
            # because the same rule that governs an exhausted budget governs
            # this: an infrastructure failure must not be written down as a
            # scientific decision about the idea. What is missing is a person
            # fixing something, which is what that state already means and why
            # `_clear_blocks` deliberately does not guess when it lifts.
            #
            # Silently dropping the candidate instead -- which is what an
            # early draft of this fix did -- reproduces the defect it is here
            # to close, one layer up: the idea disappears from the allocator
            # and nothing anywhere says why.
            if idea.operational_state is not OperationalState.BLOCKED_EXTERNAL:
                store.set_operational_state(
                    idea_id=idea.idea_id,
                    state=OperationalState.BLOCKED_EXTERNAL,
                )
            continue
        found.append(
            allocation.Candidate(
                idea=idea,
                dimensions=version.dimensions,
                diversity=allocation.diversity_key(
                    lineage_root=idea.lineage_root,
                    adjudication=[str(item) for item in version.adjudication_types],
                    research_question=version.research_question,
                ),
                stage=stage,
                reason=reason,
                expected_cost=config.cost_for(stage),
                idle_seconds=allocation.idle_seconds(idea, now=moment),
                open_objections=len(snapshot.open_objections),
                spent=store.spend_for_idea(idea.idea_id),
                lineage_spent=store.spend_for_lineage(idea.lineage_root),
                failed_attempts=failed_attempts,
            )
        )
    return tuple(found), settled


def ensure_schedule(
    *, db: Database, project_id: str, config: PortfolioConfig
) -> str | None:
    """Make sure this project's tick is on the runtime's schedule table.

    Through ``schedules`` rather than a new pass in the daemon's ordering,
    because ``sql/0001_runtime.sql`` says why that table exists: *a schedule
    produces events; it never performs work directly, so a scheduled action
    passes through exactly the same provenance, budget, locking and failure
    handling as one a person asked for.*
    """

    store = RuntimeStore(db)
    for existing in store.list_schedules():
        if existing.project_id == project_id and existing.kind == "PORTFOLIO_TICK_DUE":
            return existing.schedule_id
    created = store.create_schedule(
        project_id=project_id,
        kind="PORTFOLIO_TICK_DUE",
        interval_seconds=config.cadence.tick_seconds,
        payload={"project_id": project_id},
    )
    return created.schedule_id


__all__ = ["TickReport", "ensure_schedule", "tick"]
