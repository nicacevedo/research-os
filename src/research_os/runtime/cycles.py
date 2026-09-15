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
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.checkpoints import DURABILITY, checkpointer
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.context import CycleContext
from research_os.runtime.db import Database
from research_os.runtime.graphs import build_cycle_graph
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.ids import thread_id_for
from research_os.runtime.interfaces import ModelProvider
from research_os.runtime.kernel import ScientificKernelAdapter
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
) -> CycleContext:
    store = RuntimeStore(db)
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


def apply_default_budgets(
    ledger: BudgetLedger, *, config: RuntimeConfig, run_id: str
) -> None:
    """Give a new cycle its own budget from the configured defaults.

    Per run, not per project, and never widened afterwards: a cycle that is
    already going has the budget it started with, so editing the configuration
    cannot retroactively authorise more spending on work in flight.
    """

    defaults = config.budget
    for dimension, limit in (
        (Dimension.MODEL_CALLS, defaults.max_model_calls),
        (Dimension.MODEL_COST_USD, defaults.max_model_cost_usd),
        (Dimension.WALL_CLOCK_SECONDS, defaults.max_wall_clock_seconds),
        (Dimension.EXTERNAL_JOBS, defaults.max_external_jobs),
        (Dimension.WORK_ITEMS, defaults.max_work_items),
    ):
        ledger.set_limit(
            scope=BudgetScope.RUN,
            scope_id=run_id,
            dimension=dimension,
            limit_value=limit,
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
    apply_default_budgets(BudgetLedger(db), config=config, run_id=run.run_id)
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
    )
    thread = run.thread_id or thread_id_for(run.run_id)
    graph_config = {"configurable": {"thread_id": thread}}

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

    with checkpointer(config.require_dsn()) as saver:
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

    _settle_wall_clock(db, run)

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


def _settle_wall_clock(db: Database, run: ResearchRun) -> None:
    """Charge the wall clock this cycle actually used.

    A declared budget that nothing ever spends is not a budget, and this one was
    exactly that until the first real end-to-end run showed
    ``wall_clock_seconds`` at zero after two and a half minutes of work. Charged
    at the end rather than sampled during, because the number that matters is
    how long the cycle took and that is only known when it stops.

    Settled directly rather than through a reservation: nothing else competes
    for wall clock, so there is no capacity to hold in advance.
    """

    started = run.started_at or run.created_at
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
            set spent = least(
                    limit_value,
                    spent + greatest(extract(epoch from (now() - %(started)s)), 0)
                ),
                updated_at = now()
            where budget_id = %(budget_id)s
            """,
            {"budget_id": budget.budget_id, "started": started},
        )


def should_continue(
    *,
    db: Database,
    config: RuntimeConfig,
    result: CycleResult,
) -> tuple[bool, str]:
    """Decide whether to open a successor cycle. Returns ``(continue?, why)``.

    Three independent bounds, all of which must permit it:

    1. the cycle's own recommendation;
    2. the lineage depth against ``max_cycles_per_objective``, measured in SQL
       so a corrupted parent chain cannot become an infinite loop;
    3. the budget, so a chain cannot continue into a cycle that cannot finish.
    """

    if result.recommendation != "START_NEXT_CYCLE":
        return False, f"the cycle recommended {result.recommendation}"

    store = RuntimeStore(db)
    depth = store.lineage_depth(result.run.run_id)
    ceiling = config.settings.max_cycles_per_objective
    if depth + 1 >= ceiling:
        return False, f"{depth + 1} cycles reached the configured ceiling of {ceiling}"

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
