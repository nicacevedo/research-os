"""Running one bounded cycle, and resuming one that stopped.

This is the seam between the durable runtime and the reasoning graph. It owns
four things the graph deliberately does not:

**Building the context.** The graph receives live services; assembling them from
a DSN and a repository path happens here, once, so no node has to know how.

**Narrowing the permitted actions.** The policy table says what authority each
action needs; the autonomy setting says how much this deployment grants. The
intersection is computed here and passed in, so a node cannot widen its own
permissions -- it can only choose from a list it was handed.

**Deciding the run's status from where the graph stopped.** A graph that ends at
``await_decision`` has not failed; it is ``WAITING_HUMAN``. One that ends at
``conclude`` with ``BUDGET_EXHAUSTED`` has not failed either. Translating a
stopping point into an operational status is this module's job, and getting it
wrong is how a system reports that it is stuck when it is waiting for a person.

**Opening the successor cycle.** A cycle recommends; this decides, against the
lineage depth and the budget. There is no path by which a cycle extends itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.checkpoints import DURABILITY, checkpointer
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.context import CycleContext
from research_os.runtime.db import Database
from research_os.runtime.executors import build_executors
from research_os.runtime.graphs import build_cycle_graph
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.ids import thread_id_for
from research_os.runtime.interfaces import ModelProvider
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.locks import research_run_lock
from research_os.runtime.models import (
    Autonomy,
    BudgetScope,
    ResearchRun,
    RunStatus,
    TerminalState,
)
from research_os.runtime.policy import ACTIONS, AutonomyLevel, granted_permissions
from research_os.runtime.queue import WorkQueue
from research_os.runtime.registry import ACTION_HANDLERS
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.runtime.cycles")


class CycleError(ResearchOSError):
    """Raised when a cycle cannot be started or resumed."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Where a cycle stopped, and what should happen next."""

    run: ResearchRun
    status: RunStatus
    terminal_state: TerminalState | None
    #: Present when the graph stopped at a human gate.
    pending_approval_id: str | None
    recommendation: str
    notes: tuple[str, ...]
    state: dict[str, Any]

    @property
    def waiting_for_human(self) -> bool:
        return self.status is RunStatus.WAITING_HUMAN


def permitted_actions(autonomy: str) -> tuple[str, ...]:
    """The actions a cycle at this autonomy setting may choose from.

    `A2` actions are included, and they need no handler. That is not an
    oversight: planning an `A2` action is *how the researcher gets asked*, and
    every `A2` action in this build is one the person performs themselves. The
    cycle's job is to notice the decision is due, prepare the packet, stop, and
    -- once answered -- record the decision and hand over the command. A
    handler would be a way to perform it, and there deliberately is none.

    An `A0`/`A1` action with no handler is excluded, because there the handler
    *is* the action and listing it would let a planner pick something this build
    cannot do.
    """

    available = granted_permissions(autonomy)
    allowed: list[str] = []
    for action, policy in ACTIONS.items():
        if policy.level is AutonomyLevel.A2:
            if policy.human_executes or action in ACTION_HANDLERS:
                allowed.append(str(action))
            continue
        if action not in ACTION_HANDLERS:
            continue
        if policy.permissions - available:
            continue
        allowed.append(str(action))
    return tuple(sorted(allowed))


def build_context(
    *,
    config: RuntimeConfig,
    db: Database,
    repo_path: Path,
    models: ModelProvider,
    autonomy: str,
    executors: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> CycleContext:
    """Assemble the live services one cycle runs against.

    ``executors`` defaults to whatever this machine can actually run work on,
    rather than to nothing. It used to default to ``{}``, and nothing in the
    control plane ever supplied one -- so a daemon-driven cycle that planned
    ``run_local_experiment`` was refused with "no local executor is available on
    this machine", on a machine that can obviously run a local command. The
    experiment path was reachable only from a test that built the executor
    itself.

    Building them here also applies the containment policy in one place:
    ``build_executors`` reads the autonomy setting and gives a high-autonomy
    cycle a local executor that *requires* a sandbox.
    """

    store = RuntimeStore(db)
    if executors is None:
        executors = dict(
            build_executors(config, project_id=project_id, autonomy=autonomy)
        )
    return CycleContext(
        config=config,
        db=db,
        store=store,
        queue=WorkQueue(db),
        ledger=InvocationLedger(db),
        budgets=BudgetLedger(db),
        artifacts=FilesystemArtifactStore(config.artifacts_root, store=store),
        kernel=ScientificKernelAdapter(repo_path),
        models=models,
        permitted_actions=permitted_actions(autonomy),
        executors=dict(executors or {}),
    )


#: Every dimension a run budget covers, in one place, so `apply_default_budgets`
#: and the inheritance it performs cannot drift apart.
_BUDGET_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.MODEL_CALLS,
    Dimension.MODEL_COST_USD,
    Dimension.WALL_CLOCK_SECONDS,
    Dimension.EXTERNAL_JOBS,
    Dimension.WORK_ITEMS,
)


def apply_default_budgets(
    ledger: BudgetLedger,
    *,
    config: RuntimeConfig,
    run_id: str,
    project_id: str | None = None,
    inherit_from_run_id: str | None = None,
) -> None:
    """Give a new cycle its budget, and its project a standing ceiling.

    The per-run budget is never widened afterwards: a cycle that is already
    going has the budget it started with, so editing the configuration cannot
    retroactively authorise more spending on work in flight.

    **``inherit_from_run_id`` is what makes ``--max-cost-usd`` mean anything
    past the first cycle**, and its absence was a real and measured defect.

    ``researchctl runtime start --max-cost-usd 6`` sets a run-scope limit on the
    run it creates. A successor cycle is a *different* run, and this function
    used to overwrite its limits with the configuration defaults
    unconditionally. So an objective a researcher capped at 6 USD ran its first
    cycle at 6 and every cycle after it at the default 25 -- exposure of
    ``6 + 11 x 25 = 281 USD`` against a number the researcher had typed as 6,
    with nothing reporting the discrepancy. Observed on a real objective on
    2026-09-17: cycle 0 held ``model_cost_usd`` limit 6 and cycle 1 held 25.

    A successor continues the same objective under the same authorisation, so
    it inherits the root run's limits rather than the defaults. Where the
    parent has no limit for a dimension -- it cannot, today, but a future
    dimension could be added -- the default fills in.

    **``set_limit`` overwrites**, which is why the existing-limit check below is
    not decoration: without it, calling this twice on one run (a resume, a
    reconciler) would silently reset a researcher's explicit cap to the
    default. Only *raising* a limit is refused; the configuration may still
    lower one.

    The per-*project* cost ceiling exists because the run budget alone does not
    bound an objective. ``should_continue`` deliberately ignores run scope when
    deciding to continue -- the successor gets a fresh run budget -- so without
    this, one objective's exposure was ``max_cycles_per_objective`` times the
    per-run cost. It is now derived from the *effective* per-cycle cost limit
    rather than from the default, so a 6 USD objective gets a 72 USD project
    ceiling instead of 300, and it is set only if absent, so a researcher who
    has chosen their own ceiling keeps it.

    **What is still true and is not hidden:** ``--max-cost-usd X`` bounds one
    cycle, and an objective may run up to ``max_cycles_per_objective`` of them.
    The exposure is ``X * max_cycles_per_objective``, and both numbers are
    printed by ``researchctl runtime run``. Making the cap bound the whole
    objective means one budget shared by every cycle in the chain, which is a
    larger change to the reservation path and is recorded as remaining work
    rather than smuggled in here.
    """

    defaults = config.budget
    inherited: dict[Dimension, Decimal] = {}
    if inherit_from_run_id:
        for dimension in _BUDGET_DIMENSIONS:
            record = ledger.get(
                scope=BudgetScope.RUN,
                scope_id=inherit_from_run_id,
                dimension=dimension,
            )
            if record is not None:
                inherited[dimension] = Decimal(record.limit_value)

    def limit_for(dimension: Dimension, fallback: float) -> Decimal:
        return inherited.get(dimension, Decimal(str(fallback)))

    effective_cost = limit_for(Dimension.MODEL_COST_USD, defaults.max_model_cost_usd)
    if project_id is not None:
        existing = ledger.get(
            scope=BudgetScope.PROJECT,
            scope_id=project_id,
            dimension=Dimension.MODEL_COST_USD,
        )
        if existing is None:
            ledger.set_limit(
                scope=BudgetScope.PROJECT,
                scope_id=project_id,
                dimension=Dimension.MODEL_COST_USD,
                limit_value=effective_cost
                * Decimal(config.settings.max_cycles_per_objective),
            )
    for dimension, fallback in (
        (Dimension.MODEL_CALLS, defaults.max_model_calls),
        (Dimension.MODEL_COST_USD, defaults.max_model_cost_usd),
        (Dimension.WALL_CLOCK_SECONDS, defaults.max_wall_clock_seconds),
        (Dimension.EXTERNAL_JOBS, defaults.max_external_jobs),
        (Dimension.WORK_ITEMS, defaults.max_work_items),
    ):
        wanted = limit_for(dimension, fallback)
        current = ledger.get(
            scope=BudgetScope.RUN, scope_id=run_id, dimension=dimension
        )
        if current is not None and Decimal(current.limit_value) < wanted:
            # A tighter limit already exists on this run. Raising it here would
            # be retroactive authorisation, which the first paragraph forbids.
            continue
        ledger.set_limit(
            scope=BudgetScope.RUN,
            scope_id=run_id,
            dimension=dimension,
            limit_value=wanted,
        )


def open_cycle(
    *,
    config: RuntimeConfig,
    db: Database,
    project_id: str,
    repo_path: Path,
    objective: str,
    autonomy: Autonomy = Autonomy.HIGH,
    parent_run_id: str | None = None,
    cycle_index: int = 0,
) -> ResearchRun:
    """Create the run row, its budgets and its request event. Executes nothing.

    Separate from execution because the run id has to exist *before* anything
    that records provenance against it can be built. The first version of this
    passed a placeholder id to the model router, and the router duly recorded
    model calls against a run named "pending" -- which the foreign key rejected,
    failing every continuation. Found by running three real cycles in a row.
    """

    store = RuntimeStore(db)
    store.upsert_project(project_id=project_id, repo_path=str(repo_path))
    run = store.create_run(
        project_id=project_id,
        objective=objective,
        autonomy=autonomy,
        parent_run_id=parent_run_id,
        cycle_index=cycle_index,
    )
    apply_default_budgets(
        BudgetLedger(db),
        config=config,
        run_id=run.run_id,
        project_id=project_id,
        # A successor continues the same objective under the same
        # authorisation, so it inherits the parent's limits rather than the
        # configuration defaults. Without this a `--max-cost-usd 6` objective
        # ran cycle 0 at 6 and every cycle after it at 25.
        inherit_from_run_id=parent_run_id,
    )
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id=project_id,
        run_id=run.run_id,
        payload={"objective": objective, "cycle_index": cycle_index},
        dedup_key=f"requested:{run.run_id}",
    )
    return run


def start_cycle(
    *,
    config: RuntimeConfig,
    db: Database,
    project_id: str,
    repo_path: Path,
    objective: str,
    models: ModelProvider | Callable[[str], ModelProvider],
    autonomy: Autonomy = Autonomy.HIGH,
    parent_run_id: str | None = None,
    cycle_index: int = 0,
    executors: dict[str, Any] | None = None,
) -> CycleResult:
    """Open one bounded cycle and run it until it stops.

    ``models`` may be a provider or a callable taking the new run's id. The
    callable form exists because provenance is recorded per run and the router
    therefore cannot be built until the run exists -- see :func:`open_cycle`.
    """

    run = open_cycle(
        config=config,
        db=db,
        project_id=project_id,
        repo_path=repo_path,
        objective=objective,
        autonomy=autonomy,
        parent_run_id=parent_run_id,
        cycle_index=cycle_index,
    )
    return _execute(
        config=config,
        db=db,
        run=run,
        repo_path=repo_path,
        models=_provider_for(models, run.run_id),
        executors=executors,
        resume=False,
    )


def _provider_for(
    models: ModelProvider | Callable[[str], ModelProvider], run_id: str
) -> ModelProvider:
    """Resolve the two accepted forms of ``models`` to a provider.

    A :class:`ModelProvider` has ``complete``; a factory does not. Checked by
    attribute rather than by ``callable()``, because a provider could
    legitimately define ``__call__`` and the failure would be silent.
    """

    if hasattr(models, "complete"):
        return models  # type: ignore[return-value]
    return models(run_id)  # type: ignore[operator]


def resume_cycle(
    *,
    config: RuntimeConfig,
    db: Database,
    run_id: str,
    repo_path: Path,
    models: ModelProvider,
    resume_value: Any = None,
    executors: dict[str, Any] | None = None,
) -> CycleResult:
    """Continue a cycle that crashed, or answer the gate it stopped at.

    One entry point for both, because from the graph's point of view they are
    the same operation: re-enter the thread and carry on. ``resume_value`` is
    ``None`` for a crash -- pick up where the last checkpoint left off -- and
    the human's answer for a gate.
    """

    store = RuntimeStore(db)
    run = store.require_run(run_id)
    if run.terminal:
        raise CycleError(
            f"{run_id} is already {run.status} ({run.terminal_state}); nothing to resume"
        )
    return _execute(
        config=config,
        db=db,
        run=run,
        repo_path=repo_path,
        models=models,
        executors=executors,
        resume=True,
        resume_value=resume_value,
    )


def _thread_has_checkpoint(app: Any, graph_config: dict[str, Any]) -> bool:
    """Whether this thread has ever executed a superstep.

    ``get_state`` on an unknown thread returns a snapshot with no values and no
    next step rather than raising, so both are checked: a thread that stopped at
    an interrupt has a ``next``, and one that finished has values.
    """

    snapshot = app.get_state(graph_config)
    return bool(snapshot.values) or bool(snapshot.next)


def _execute(
    *,
    config: RuntimeConfig,
    db: Database,
    run: ResearchRun,
    repo_path: Path,
    models: ModelProvider,
    executors: dict[str, Any] | None,
    resume: bool,
    resume_value: Any = None,
) -> CycleResult:
    from langgraph.types import Command

    store = RuntimeStore(db)
    context = build_context(
        config=config,
        db=db,
        repo_path=repo_path,
        models=models,
        autonomy=str(run.autonomy),
        executors=executors,
        project_id=run.project_id,
    )
    thread = run.thread_id or thread_id_for(run.run_id)
    graph_config = {"configurable": {"thread_id": thread}}
    # When *this* entry began, for the wall-clock charge. Not the run's
    # `started_at`, which never advances.
    entered_at = datetime.now(UTC)

    if (
        run.status is RunStatus.CREATED
        or resume
        and run.status in {RunStatus.WAITING_HUMAN, RunStatus.WAITING_EXTERNAL}
    ):
        run = store.set_run_status(run.run_id, RunStatus.RUNNING)

    initial: dict[str, Any] = {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "repo_path": str(repo_path),
        "objective": run.objective,
        "autonomy": str(run.autonomy),
        "cycle_index": run.cycle_index,
        "artifacts": [],
        "notes": [],
    }

    # One worker in one thread. Two entering the same LangGraph thread
    # duplicates model spend and writes concurrently to one checkpoint stream,
    # and the run's status transition is not a guard -- RUNNING -> RUNNING
    # succeeds. Refuses fast rather than queueing: the caller puts the work item
    # back and picks up something else.
    with (
        research_run_lock(db, run.run_id),
        checkpointer(config.require_dsn()) as saver,
    ):
        app = build_cycle_graph().compile(checkpointer=saver)

        # What to hand LangGraph depends on the thread, not on the caller's
        # intention. "Resume" and "start" are the same operation from the
        # graph's point of view -- re-enter the thread and carry on -- and the
        # only thing that decides the payload is whether the thread has a
        # checkpoint yet.
        #
        # Getting this wrong raised `EmptyInputError`: the control plane records
        # a run row, emits an event, and the work item that picks it up arrives
        # at a thread that has never executed. Passing `None` there is "continue
        # from the last checkpoint" and there is no last checkpoint.
        payload: Any
        if resume_value is not None:
            payload = Command(resume=resume_value)
        elif _thread_has_checkpoint(app, graph_config):
            payload = None
        else:
            payload = initial

        app.invoke(payload, graph_config, context=context, durability=DURABILITY)
        snapshot = app.get_state(graph_config)

    final = dict(snapshot.values or {})
    pending = [item for item in (snapshot.interrupts or ())]
    notes = tuple(final.get("notes", []))

    # Charged before the gate branch, not after it. An earlier version settled
    # only on the non-pending path, so a cycle that stopped for a human
    # decision charged nothing for the time it spent getting there -- and a run
    # that gated and resumed repeatedly never accrued wall clock at all.
    _settle_wall_clock(db, run, entered_at=entered_at)

    if pending:
        approval_id = ""
        value = pending[0].value
        if isinstance(value, dict):
            approval_id = str(value.get("approval_id", ""))
        run = store.set_run_status(
            run.run_id,
            RunStatus.WAITING_HUMAN,
            terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
            detail=f"awaiting a scientific decision ({approval_id or 'unrecorded'})",
        )
        store.record_event(
            kind="SCIENTIFIC_DECISION_REQUIRED",
            project_id=run.project_id,
            run_id=run.run_id,
            payload={"approval_id": approval_id},
            dedup_key=f"decision-required:{approval_id or run.run_id}",
        )
        return CycleResult(
            run=run,
            status=RunStatus.WAITING_HUMAN,
            terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
            pending_approval_id=approval_id or None,
            recommendation="WAIT_HUMAN",
            notes=notes,
            state=final,
        )

    digest = str(final.get("frontier_digest") or "")
    if digest:
        store.set_frontier_digest(run.run_id, digest)

    raw_terminal = str(final.get("terminal_state") or TerminalState.DONE_FOR_NOW)
    terminal = TerminalState(raw_terminal)
    status = (
        RunStatus.FAILED
        if terminal is TerminalState.FATAL_INFRASTRUCTURE_ERROR
        else RunStatus.SUCCEEDED
    )
    run = store.set_run_status(
        run.run_id, status, terminal_state=terminal, detail=notes[-1] if notes else None
    )
    store.record_event(
        kind="RESEARCH_CYCLE_FINISHED",
        project_id=run.project_id,
        run_id=run.run_id,
        payload={
            "terminal_state": str(terminal),
            "recommendation": final.get("next_recommendation", ""),
        },
        dedup_key=f"cycle-finished:{run.run_id}",
    )
    return CycleResult(
        run=run,
        status=status,
        terminal_state=terminal,
        pending_approval_id=None,
        recommendation=str(final.get("next_recommendation") or "DONE_FOR_NOW"),
        notes=notes,
        state=final,
    )


def _settle_wall_clock(db: Database, run: ResearchRun, *, entered_at: datetime) -> None:
    """Charge the wall clock this *entry* used.

    ``entered_at`` rather than ``run.started_at``, because ``started_at`` is
    stamped once and never advances -- so charging from it billed the full
    elapsed time since the run first began, again, on every resume. A run
    resumed three times charged roughly 1x + 2x + 3x the real time, and the
    old ``least(limit_value, ...)`` clamp then guaranteed convergence to exactly
    the limit, so ``wall_clock_seconds`` reported itself exhausted regardless of
    time actually spent -- and ``should_continue`` reads that.

    No clamp now either. A budget that cannot record an overrun cannot tell
    "used exactly the budget" from "used ten times it", and the second is the
    one worth knowing.
    """

    ledger = BudgetLedger(db)
    budget = ledger.get(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.WALL_CLOCK_SECONDS,
    )
    if budget is None:
        return
    with db.tx() as conn:
        conn.execute(
            """
            update budgets
            set spent = spent
                + greatest(extract(epoch from (now() - %(entered)s)), 0),
                updated_at = now()
            where budget_id = %(budget_id)s
            """,
            {"budget_id": budget.budget_id, "entered": entered_at},
        )


def should_continue(
    *,
    db: Database,
    config: RuntimeConfig,
    result: CycleResult,
    observed_frontier: str | None = None,
) -> tuple[bool, str]:
    """Decide whether to open a successor cycle. Returns ``(continue?, why)``.

    Three independent bounds, all of which must permit it:

    1. the cycle's own recommendation;
    2. the lineage depth against ``max_cycles_per_objective``, measured in SQL
       so a corrupted parent chain cannot become an infinite loop;
    3. the budget, so a chain cannot continue into a cycle that cannot finish.

    ``observed_frontier`` is the frontier **as of now**, supplied by a caller
    that has just measured it. It changes only which two digests the
    progress check compares, and getting that wrong is what a real pilot
    caught:

    - a cycle that has *just concluded* is its own "now", so the comparison is
      its digest against its parent's. That is the default, and it is the
      seven-cycle stop from ``docs/RUNTIME.md`` §16.
    - a cycle that concluded some time ago and is being advanced *because a
      person changed the science* is not its own "now". Comparing its digest
      against its parent's asks "did that old cycle learn anything", and the
      answer was no -- which is exactly why it stopped and waited. It refused
      the successor at the precise moment the wait had ended.

    So the caller that measured the current frontier passes it, and the
    comparison becomes "has the frontier moved since this cycle recorded one",
    which is the question both callers actually mean.
    """

    if result.recommendation != "START_NEXT_CYCLE":
        return False, f"the cycle recommended {result.recommendation}"

    store = RuntimeStore(db)
    depth = store.lineage_depth(result.run.run_id)
    ceiling = config.settings.max_cycles_per_objective
    if depth + 1 >= ceiling:
        return False, f"{depth + 1} cycles reached the configured ceiling of {ceiling}"

    # Did this cycle change anything? The first real pilot ran seven chained
    # cycles over an identical frontier, each concluding START_NEXT_CYCLE,
    # stopping only at the ceiling -- fifteen model calls for one assessment's
    # worth of information.
    #
    # The cause is structural, not a planner mistake: the runtime cannot write a
    # capsule, so it cannot retire a hypothesis, record an experiment, or move a
    # claim. Its own work never changes the frontier that the frontier is
    # derived from. So progress has to be *checked* rather than assumed, and
    # when there is none the honest recommendation is to stop and say why.
    if observed_frontier is not None:
        # A caller that has measured the frontier now. Compare against what
        # this cycle recorded, which is the only comparison that answers "has
        # anything changed since".
        #
        # An *empty* measurement is not a change. It means the capsule could
        # not be read -- mid-edit, a checkout in progress -- and treating
        # unknown as changed opened a successor cycle over an unchanged
        # frontier, which the first closed-loop pilot did.
        if not observed_frontier:
            return False, (
                "the current frontier could not be computed, so whether "
                "anything changed is unknown. Unknown is not changed: no "
                "successor is opened, and the next observation will decide."
            )
        if observed_frontier == result.run.frontier_digest:
            return False, (
                "the canonical science changed but the unresolved frontier did "
                "not, so a successor cycle would recompute the same work and "
                "add no information."
            )
    else:
        digest = result.run.frontier_digest
        if digest and result.run.parent_run_id:
            parent = store.get_run(result.run.parent_run_id)
            if parent is not None and parent.frontier_digest == digest:
                return False, (
                    "the frontier is unchanged from the previous cycle. The "
                    "runtime cannot alter canonical scientific state, so "
                    "repeating the cycle would repeat its cost without adding "
                    "information. What is outstanding needs a person: see "
                    f"`researchctl runtime run {result.run.run_id}`."
                )

    exhausted = BudgetLedger(db).exhausted_dimensions(
        run_id=result.run.run_id, project_id=result.run.project_id
    )
    project_or_system = [
        (scope, dimension)
        for scope, _scope_id, dimension in exhausted
        if scope is not BudgetScope.RUN
    ]
    if project_or_system:
        names = ", ".join(
            f"{scope}:{dimension}" for scope, dimension in project_or_system
        )
        return False, f"budget exhausted beyond this cycle ({names})"
    return True, f"cycle {depth + 1} of at most {ceiling}"
