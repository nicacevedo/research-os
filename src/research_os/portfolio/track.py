"""The IdeaTrackGraph: one bounded stage of one idea, then END.

A LangGraph, and the reason is one measured property rather than consistency
for its own sake. `ARCHITECTURE.md` §12a adopted LangGraph for *durable
checkpointing across process death*, and the place that pays for itself here is
the review board: three sequential expensive calls, where a crash after the
second must resume at the third rather than paying for all three again. Every
other stage is one node, where checkpointing buys nothing -- and that is said
here rather than argued away.

What the graph does **not** do is keep an invocation alive. One entry advances
one idea through exactly one stage, records what happened, and ends. `DEEPEN`
and `BRANCH` create rows; the portfolio decides when they run. There is no way
to express "keep going".

The stage is chosen by :func:`research_os.portfolio.stages.select_stage`, which
is deterministic and asks no model, so a resumed track makes the same choice
the crashed one did.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from research_os.errors import ResearchOSError
from research_os.portfolio import runner, stages
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.ids import track_thread_id
from research_os.portfolio.models import (
    ActionStatus,
    Disposition,
    OperationalState,
    ReviewerRole,
    Stage,
)
from research_os.portfolio.store import (
    ActiveTrackExistsError,
    DuplicateBasisError,
    PortfolioStore,
)
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.checkpoints import DURABILITY, checkpointer
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.interfaces import ModelProvider
from research_os.runtime.locks import research_run_lock
from research_os.runtime.models import Autonomy, RunKind, RunStatus, TerminalState
from research_os.runtime.routing import IndependenceUnavailableError
from research_os.runtime.store import RuntimeStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.graph import StateGraph
    from langgraph.runtime import Runtime

LOG = logging.getLogger("research_os.portfolio.track")


class _UnusedProvider:
    """Stands in where a provider is required and must not be reached.

    `advance_idea` selects the stage before it opens a run, and selection is
    deterministic. Handing it a real provider would be harmless and handing it
    one that raises says something true: nothing on that path may ask a model.
    """

    def complete(self, request: object) -> object:  # pragma: no cover
        raise TrackError(
            "stage selection consults no model; something on the probe path "
            "tried to make a call"
        )


_UNUSED_PROVIDER = _UnusedProvider()


class TrackError(ResearchOSError):
    """Raised when an idea track cannot be opened or advanced."""


class TrackState(TypedDict, total=False):
    """What one bounded stage carries between its nodes.

    Small and structured, for the reason ``graphs/state.py`` gives: LangGraph
    persists state at every superstep, and state that carries bytes is a
    checkpoint table that grows by megabytes. Everything large is already in
    the artifact store or in a row.
    """

    idea_id: str
    project_id: str
    run_id: str
    stage: str
    reason: str
    outcome: dict[str, Any]
    disposition: str
    detail: str
    cost_usd: str
    model_calls: int
    failure_class: str
    notes: list[str]


@dataclass(frozen=True, slots=True)
class TrackResult:
    """What one entry into the track concluded."""

    idea_id: str
    run_id: str
    action_id: str | None
    stage: Stage | None
    reason: str
    ok: bool
    detail: str
    disposition: Disposition | None = None
    cost_usd: Decimal = Decimal(0)
    model_calls: int = 0
    failure_class: FailureClass | None = None


# ----------------------------------------------------------------- nodes --
def hydrate_idea(
    state: TrackState, runtime: Runtime[runner.TrackContext]
) -> dict[str, Any]:
    context = runtime.context
    snapshot = runner.build_snapshot(context)
    stage, reason = stages.select_stage(snapshot, context.config)
    return {
        "idea_id": context.idea_id,
        "project_id": context.project_id,
        "run_id": context.run_id,
        "stage": str(stage) if stage else "",
        "reason": reason,
        "notes": [f"selected {stage or 'nothing'}: {reason}"],
    }


def _perform(
    state: TrackState, runtime: Runtime[runner.TrackContext], stage: Stage
) -> dict[str, Any]:
    context = runtime.context
    snapshot = runner.build_snapshot(context)
    handler = runner.STAGE_HANDLERS[stage]
    outcome = handler(context, snapshot)
    return _outcome_update(outcome)


def _outcome_update(outcome: runner.StageOutcome) -> dict[str, Any]:
    return {
        "detail": outcome.detail,
        "disposition": str(outcome.disposition) if outcome.disposition else "",
        "cost_usd": str(outcome.cost_usd),
        "model_calls": outcome.model_calls,
        "failure_class": str(outcome.failure_class) if outcome.failure_class else "",
        "outcome": dict(outcome.data),
    }


def _accumulate(state: TrackState, outcome: runner.StageOutcome) -> dict[str, Any]:
    """Fold one reviewer's cost into what the board has spent so far.

    Needed only by the review board, which is three nodes. Every other stage
    sets these once.
    """

    update = _outcome_update(outcome)
    update["cost_usd"] = str(Decimal(state.get("cost_usd", "0")) + outcome.cost_usd)
    update["model_calls"] = int(state.get("model_calls", 0)) + outcome.model_calls
    return update


def _reviewer_node(role: ReviewerRole):
    def node(
        state: TrackState, runtime: Runtime[runner.TrackContext]
    ) -> dict[str, Any]:
        context = runtime.context
        snapshot = runner.build_snapshot(context)
        if role not in snapshot.missing_review_roles:
            # Already recorded, by this attempt or a previous one. Not an
            # error: it is precisely what makes a resumed board free.
            return {
                "notes": [f"{role} had already reviewed this version"],
            }
        outcome = runner.run_one_review(context, snapshot, role)
        update = _accumulate(state, outcome)
        update["notes"] = [outcome.detail]
        return update

    node.__name__ = f"review_{role.name.lower()}"
    return node


def finish_board(
    state: TrackState, runtime: Runtime[runner.TrackContext]
) -> dict[str, Any]:
    context = runtime.context
    snapshot = runner.build_snapshot(context)
    runner.finish_review_board(context, snapshot)
    return {"disposition": str(Disposition.CONTINUE)}


def conclude(
    state: TrackState, runtime: Runtime[runner.TrackContext]
) -> dict[str, Any]:
    """Grant the one tier a gate can grant alone, and stop.

    The stage handlers already wrote what they found. What happens here is the
    ``CANDIDATE -> PROMISING`` transition, evaluated from rows: its
    requirements involve no reviewer, so there is nothing for a meta-review to
    synthesise, and every stage past the falsifier is gated on the idea having
    reached it. Nothing performed that transition before, so an idea that
    survived the falsifier simply stopped -- which is what trying to write a
    test that drives a promotion end to end surfaced.

    A node rather than something in the caller, because a graph whose terminal
    behaviour lives outside it is a graph whose behaviour changes when somebody
    writes a second caller.
    """

    notes = [state.get("detail", "")]
    if not state.get("failure_class"):
        promoted = runner.promote_if_earned(runtime.context)
        if promoted:
            notes.append(f"the rows now support {promoted}")
        # Continuation -- `frontier.settle` -- is deliberately *not* here.
        # This node runs while the stage's action is still ACTIVE, so the
        # stage that just ran is not yet among the succeeded ones and the
        # snapshot selects it again: the settle did nothing for every stage
        # but one, and on the literature branch `complete_action` would then
        # have overwritten the BLOCKED_DEPENDENCY it set with IDLE. The next
        # tick settles from committed rows, with a compare-and-set.
    return {"notes": notes}


def _after_hydrate(state: TrackState) -> str:
    stage = state.get("stage", "")
    if not stage:
        return "conclude"
    if stage == str(Stage.REVIEW_BOARD):
        return "review_methodology"
    return "perform"


def build_track_graph() -> StateGraph:
    """One stage, then END. The shape of §16, bounded.

    The fan-out is deliberately narrow: ``perform`` dispatches every
    single-node stage through the closed handler table, and only the review
    board gets its own nodes, because only there does a checkpoint between
    steps save a call.
    """

    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(TrackState, context_schema=runner.TrackContext)
    graph.add_node("hydrate_idea", hydrate_idea)
    graph.add_node(
        "perform",
        lambda state, runtime: _perform(state, runtime, Stage(state["stage"])),
    )
    graph.add_node("review_methodology", _reviewer_node(ReviewerRole.METHODOLOGY))
    graph.add_node("review_novelty", _reviewer_node(ReviewerRole.NOVELTY))
    graph.add_node("review_skeptic", _reviewer_node(ReviewerRole.SKEPTIC))
    graph.add_node("finish_board", finish_board)
    graph.add_node("conclude", conclude)

    graph.add_edge(START, "hydrate_idea")
    graph.add_conditional_edges(
        "hydrate_idea",
        _after_hydrate,
        {
            "perform": "perform",
            "review_methodology": "review_methodology",
            "conclude": "conclude",
        },
    )
    graph.add_edge("perform", "conclude")
    graph.add_edge("review_methodology", "review_novelty")
    graph.add_edge("review_novelty", "review_skeptic")
    graph.add_edge("review_skeptic", "finish_board")
    graph.add_edge("finish_board", "conclude")
    graph.add_edge("conclude", END)
    return graph


# ------------------------------------------------------------ the entry --
def advance_idea(
    *,
    runtime_config: RuntimeConfig,
    portfolio_config: PortfolioConfig,
    db: Database,
    project_id: str,
    idea_id: str,
    models: ModelProvider | Callable[[str], ModelProvider],
    repo_path: Path | None = None,
    literature: runner.LiteratureSource | None = None,
    charter: str = "",
    problem: str = "",
    established_facts: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
    executors: Mapping[str, Any] | None = None,
    work_id: str | None = None,
    run_id: str | None = None,
) -> TrackResult:
    """Advance one idea by exactly one stage.

    Four things happen in order, and the order is what makes a crash anywhere
    in it recoverable:

    1. the stage is selected deterministically, so a retry selects the same one;
    2. an ``idea_actions`` row claims the idea's single active slot and the
       stage's scientific basis, so two ticks cannot both run it;
    3. a ``research_runs`` row of kind ``idea_track`` carries the budget and
       the provenance, and is invisible to objective continuation;
    4. the graph runs under the run's advisory lock, so two workers cannot
       enter one LangGraph thread.

    A lost race at step 2 returns a result saying so rather than raising: two
    portfolio ticks reaching the same conclusion is ordinary, not a defect.

    **What survives a crash, and by what mechanism.** LangGraph's checkpointing
    resumes this entry's graph between nodes *within* one call. It does not
    carry across calls: a worker that dies leaves an ACTIVE action that the
    portfolio tick's reconciler frees, and the retry is a fresh run with a
    fresh thread. What makes the retry cheap is the database, not the
    checkpointer -- each reviewer node asks whether its review is already
    recorded against this version and skips if it is. That is stronger than a
    checkpoint, because it survives a process change, a machine change and a
    code change; it is stated here because the alternative is a docstring
    claiming a property the checkpointer does not provide.
    """

    store = PortfolioStore(db)
    runtime_store = RuntimeStore(db)
    store.require_idea(idea_id)
    version = store.require_version(idea_id)

    context_probe = runner.TrackContext(
        config=portfolio_config,
        portfolio=store,
        runtime=runtime_store,
        # Never used: the probe asks `select_stage`, which consults no model.
        # It is here because the type requires one, and a factory has nothing
        # to build against until a run exists.
        models=_UNUSED_PROVIDER,
        artifacts=FilesystemArtifactStore(
            root=runtime_config.artifacts_root, store=runtime_store
        ),
        project_id=project_id,
        idea_id=idea_id,
        run_id="pending",
        charter=charter,
        problem=problem,
        established_facts=established_facts,
        constraints=constraints,
        literature=literature,
        repo_path=repo_path,
        executors=dict(executors or {}),
        ledger=InvocationLedger(db),
        budgets=BudgetLedger(db),
    )
    snapshot = runner.build_snapshot(context_probe)
    stage, reason = stages.select_stage(snapshot, portfolio_config)
    if stage is None:
        return TrackResult(
            idea_id=idea_id,
            run_id="",
            action_id=None,
            stage=None,
            reason=reason,
            ok=True,
            detail=reason,
        )

    basis = _basis_for(store, idea_id=idea_id, version=version.version, stage=stage)
    run = runtime_store.create_run(
        project_id=project_id,
        objective=f"idea-track:{idea_id}:{stage}",
        autonomy=Autonomy(runtime_config.autonomy),
        run_kind=RunKind.IDEA_TRACK,
    )
    runtime_store.set_thread_id(run.run_id, track_thread_id(run.run_id))
    run = runtime_store.require_run(run.run_id)
    try:
        action = store.open_action(
            idea_id=idea_id,
            idea_version=version.version,
            stage=stage,
            basis_digest=basis,
            thread_id=run.thread_id,
        )
    except (ActiveTrackExistsError, DuplicateBasisError) as exc:
        runtime_store.set_run_status(
            run.run_id,
            RunStatus.CANCELLED,
            terminal_state=TerminalState.CANCELLED,
            detail=str(exc),
        )
        return TrackResult(
            idea_id=idea_id,
            run_id=run.run_id,
            action_id=None,
            stage=stage,
            reason=reason,
            ok=True,
            detail=f"another pass is already doing this: {exc}",
        )

    context = runner.TrackContext(
        config=portfolio_config,
        portfolio=store,
        runtime=runtime_store,
        # Built *after* the run exists, so every call this track makes is
        # recorded against it. A router constructed earlier would carry a run
        # id naming nothing, and `record_model_call` derives `project_id` by
        # looking the run up -- so the cost of the whole track would land on a
        # row with no project, which is the attribution `sql/0015` exists to
        # keep.
        models=models(run.run_id) if callable(models) else models,
        artifacts=context_probe.artifacts,
        project_id=project_id,
        idea_id=idea_id,
        run_id=run.run_id,
        work_id=work_id,
        charter=charter,
        problem=problem,
        established_facts=established_facts,
        constraints=constraints,
        literature=literature,
        # An experiment runs in a disposable worktree *of* this repository and
        # its canonical state is fingerprinted before and after. The path was
        # accepted and then discarded by this function for two releases, which
        # is why an empirical idea could not be measured: the stage that would
        # have done it had no repository to make a workspace from.
        repo_path=repo_path,
        executors=dict(executors or {}),
        ledger=InvocationLedger(db),
        budgets=BudgetLedger(db),
    )
    runtime_store.set_run_status(run.run_id, RunStatus.RUNNING)

    try:
        with (
            research_run_lock(db, run.run_id),
            checkpointer(runtime_config.require_dsn()) as saver,
        ):
            app = build_track_graph().compile(checkpointer=saver)
            graph_config = {"configurable": {"thread_id": run.thread_id}}
            app.invoke(
                {
                    "idea_id": idea_id,
                    "project_id": project_id,
                    "run_id": run.run_id,
                    "notes": [],
                    "cost_usd": "0",
                    "model_calls": 0,
                },
                graph_config,
                context=context,
                durability=DURABILITY,
            )
            final = dict(app.get_state(graph_config).values or {})
    except IndependenceUnavailableError as exc:
        # A deployment fact, not a defect, and the taxonomy has a member for
        # it. `IndependenceUnavailableError` is a sibling of
        # `ProviderCallFailedError` rather than a subclass, so no stage
        # handler caught it and it reached the catch-all below as UNKNOWN --
        # which `_terminal_for` maps to FATAL_INFRASTRUCTURE_ERROR and
        # `_operational_for` to IDLE. That sends a researcher looking for a
        # broken system when what is missing is a second provider family
        # they install. §10 of the architecture already says the answer is
        # WAITING_FOR_EXTERNAL_DEPENDENCY; the objective cycle does this at
        # `graphs/cycle.py:893` and this layer did not.
        store.complete_action(
            action_id=action.action_id,
            status=ActionStatus.FAILED,
            detail=f"required review independence is unavailable here: {exc}",
            failure_class=str(FailureClass.CAPABILITY_DENIED),
            operational_state=_operational_for(str(FailureClass.CAPABILITY_DENIED)),
        )
        runtime_store.set_run_status(
            run.run_id,
            RunStatus.FAILED,
            terminal_state=TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY,
            detail=str(exc)[:500],
        )
        raise
    except Exception as exc:
        store.complete_action(
            action_id=action.action_id,
            status=ActionStatus.FAILED,
            detail=str(exc),
            failure_class=str(FailureClass.UNKNOWN),
            operational_state=OperationalState.IDLE,
        )
        runtime_store.set_run_status(
            run.run_id,
            RunStatus.FAILED,
            terminal_state=TerminalState.FATAL_INFRASTRUCTURE_ERROR,
            detail=str(exc)[:500],
        )
        raise

    cost = Decimal(final.get("cost_usd", "0"))
    calls = int(final.get("model_calls", 0))
    failure = final.get("failure_class") or ""
    disposition = final.get("disposition") or ""
    detail = final.get("detail") or reason

    ok = not failure
    store.complete_action(
        action_id=action.action_id,
        status=ActionStatus.SUCCEEDED if ok else ActionStatus.FAILED,
        disposition=Disposition(disposition) if disposition else None,
        detail=detail,
        failure_class=failure or None,
        cost_usd=cost,
        model_calls=calls,
        operational_state=_operational_for(failure),
    )
    # The run row is the operational mirror and stays terse on purpose: the
    # durable account of what a stage decided is the action, and the bank
    # page rendered from it. Widening telemetry is R5's call, not this
    # module's.
    runtime_store.set_run_status(
        run.run_id,
        RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
        terminal_state=(TerminalState.DONE_FOR_NOW if ok else _terminal_for(failure)),
        detail=detail[:500],
    )
    return TrackResult(
        idea_id=idea_id,
        run_id=run.run_id,
        action_id=action.action_id,
        stage=stage,
        reason=reason,
        ok=ok,
        detail=detail,
        disposition=Disposition(disposition) if disposition else None,
        cost_usd=cost,
        model_calls=calls,
        failure_class=FailureClass(failure) if failure else None,
    )


def _basis_for(
    store: PortfolioStore, *, idea_id: str, version: int, stage: Stage
) -> str:
    """The scientific basis this stage would run against.

    Content, evidence and live reviews. Two requests to run one stage against
    one basis are one action, which is what makes a replayed event, a reclaimed
    lease and a duplicated portfolio tick all harmless.
    """

    from research_os.portfolio import digests as pdigests

    head = store.require_version(idea_id, version)
    evidence = store.list_evidence(idea_id=idea_id, idea_version=version)
    reviews = store.live_reviews(idea_id=idea_id)
    return pdigests.basis_digest(
        content=head.content_digest,
        evidence_ids=[item.evidence_id for item in evidence],
        review_ids=[item.review_id for item in reviews],
        stage_inputs={"stage": str(stage)},
    )


#: Which operational state a failed stage leaves the idea in.
#:
#: The distinction that matters: a provider outage is not a scientific verdict
#: and must not read as one. ``docs/RUNTIME.md`` §17 records what happened the
#: one time it did.
_OPERATIONAL_FOR_FAILURE: dict[str, OperationalState] = {
    str(FailureClass.PROVIDER_UNAVAILABLE): OperationalState.BLOCKED_PROVIDER,
    str(FailureClass.PROVIDER_TIMEOUT): OperationalState.BLOCKED_PROVIDER,
    str(FailureClass.BUDGET_EXHAUSTED): OperationalState.BLOCKED_BUDGET,
    str(FailureClass.CAPABILITY_DENIED): OperationalState.BLOCKED_EXTERNAL,
}


def _operational_for(failure: str) -> OperationalState:
    if not failure:
        return OperationalState.IDLE
    return _OPERATIONAL_FOR_FAILURE.get(failure, OperationalState.IDLE)


def _terminal_for(failure: str) -> TerminalState:
    if failure == str(FailureClass.BUDGET_EXHAUSTED):
        return TerminalState.BUDGET_EXHAUSTED
    if failure in {
        str(FailureClass.PROVIDER_UNAVAILABLE),
        str(FailureClass.PROVIDER_TIMEOUT),
        str(FailureClass.CAPABILITY_DENIED),
    }:
        return TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY
    return TerminalState.FATAL_INFRASTRUCTURE_ERROR


__all__ = [
    "TrackError",
    "TrackResult",
    "TrackState",
    "advance_idea",
    "build_track_graph",
]
