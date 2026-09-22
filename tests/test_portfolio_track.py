"""One idea, driven stage by stage, against a scripted provider.

This is where the pieces meet: the selector picks a stage, the handler calls a
model, the contract validates it, the store records rows, and the gate decides
what those rows permit. Each test drives the *real* graph -- with a real
PostgreSQL checkpointer -- and asserts on database state rather than on return
values, because what a researcher reads later is the rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ActionStatus,
    AdjudicationType,
    Disposition,
    EdgeKind,
    EvidenceKind,
    IdeaStatus,
    OperationalState,
    ReviewerRole,
    Stage,
)
from research_os.portfolio.store import DuplicateBasisError, PortfolioStore
from research_os.portfolio.track import advance_idea, build_track_graph
from research_os.runtime.db import Database
from research_os.runtime.models import RunKind, RunStatus
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


# ------------------------------------------------------------- doubles --
@dataclass(frozen=True, slots=True)
class _Work:
    key: str
    title: str
    abstract: str


@dataclass(frozen=True, slots=True)
class _Entry:
    work: _Work
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class _Packet:
    query: str
    entries: tuple[_Entry, ...]

    @property
    def work_keys(self) -> tuple[str, ...]:
        return tuple(item.work.key for item in self.entries)


def _next_stage(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project_id: str,
    idea_id: str,
    literature: Any = None,
) -> Stage | None:
    """What the track would do next, without doing it.

    A read of the selector, not a dry run of the track. An earlier version
    "peeked" by calling ``advance_idea`` with a provider that refused
    everything -- which opened an action, failed a stage and left the idea in
    BLOCKED_PROVIDER, so the peek changed exactly the state it was asking
    about.
    """

    from research_os.portfolio.config import load_config as _load
    from research_os.portfolio.runner import TrackContext, build_snapshot
    from research_os.portfolio.stages import select_stage
    from research_os.runtime.artifacts import FilesystemArtifactStore

    runtime = RuntimeStore(runtime_db)
    probe = TrackContext(
        config=_load(),
        portfolio=portfolio,
        runtime=runtime,
        models=None,  # type: ignore[arg-type]
        artifacts=FilesystemArtifactStore(Path("/tmp"), store=runtime),
        project_id=project_id,
        idea_id=idea_id,
        run_id="peek",
        literature=literature,
    )
    stage, _why = select_stage(build_snapshot(probe), _load())
    return stage


class FakeLiterature:
    """A literature source with a fixed, addressable corpus.

    Fixed on purpose: the property under test is that a novelty matrix may only
    cite keys that were supplied, so the supplied set has to be something a
    test can name.
    """

    def __init__(
        self, keys: tuple[str, ...] = ("openalex:W1", "openalex:W2", "openalex:W3")
    ):
        self.keys = keys
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        self.queries.append(query)
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in self.keys[:limit]
            ),
        )


#: The falsifier the scripted `scientific_discovery` writes into the sharpened
#: version, and therefore what the idea's adjudication type becomes.
#:
#: It matters more than it looks: the adjudication type is read from the
#: falsifier, and it decides which evidence route the track takes. A test that
#: wants to reach the review board needs a route this host can actually
#: complete, which -- with no executor and no derivation path wired -- is the
#: literature one.
LITERATURE_FALSIFIER = (
    "Find a prior publication that already reports this; search the literature "
    "for the same result under other terminology."
)
MATHEMATICAL_FALSIFIER = (
    "Exhibit a counterexample, or prove that the orderings coincide."
)
EMPIRICAL_FALSIFIER = (
    "Measure the median wall-clock time over 30 repetitions on both solvers."
)


def discovery_answer(falsifier: str = LITERATURE_FALSIFIER) -> dict[str, Any]:
    return {
        "can_be_made_precise": True,
        "minimum_decisive_action": "search for the closest prior result",
        "refined": {
            **{
                key: value
                for key, value in idea_fields().items()
                if key != "adjudication_types"
            },
            "falsifier": falsifier,
        },
    }


def _answers(**overrides: Any) -> dict[str, dict[str, Any]]:
    """A scripted answer per role, all of which are 'this is fine'."""

    def review(who: str) -> dict[str, Any]:
        # Distinct summaries, because the property under test is that no
        # reviewer sees another's. Identical text would make the assertion
        # vacuous.
        return {
            "verdict": "PASS",
            "summary": f"{who} found nothing to object to",
            "objections": [],
        }

    base: dict[str, dict[str, Any]] = {
        "duplicate_adjudicator": {
            "verdict": "distinct",
            "rationale": "different question",
        },
        "literature_scout": {
            "rows": [
                {
                    "proposed_component": "the trajectory comparison",
                    "closest_known_result": "screening rules",
                    "relation": "different",
                    "precise_difference": "we compare trajectories, not optima",
                    "source_key": "openalex:W1",
                    "confidence": 0.8,
                },
                {
                    "proposed_component": "the divergence claim",
                    "closest_known_result": "working-set convergence",
                    "relation": "partial",
                    "precise_difference": "convergence is shown, trajectories are not",
                    "source_key": "openalex:W2",
                    "confidence": 0.6,
                },
                {
                    "proposed_component": "the monotonicity condition",
                    "closest_known_result": "safe screening bounds",
                    "relation": "different",
                    "precise_difference": "the bound's monotonicity is not studied",
                    "source_key": "openalex:W3",
                    "confidence": 0.7,
                },
            ],
            "queries": ["column generation working set", "support trajectory lasso"],
            "summary": "no prior result compares trajectories",
        },
        "falsifier": {
            "summary": "no cheap kill found",
            "objections": [],
            "attempted": ["a two-variable counterexample", "a subsuming theorem"],
        },
        "scientific_discovery": discovery_answer(),
        "methodology_reviewer": review("the methodologist"),
        "novelty_reviewer": review("the novelty reviewer"),
        "skeptic_reviewer": review("the skeptic"),
        "meta_reviewer": {
            "recommendation": "HUMAN_READY",
            "summary": "all three reviewers were satisfied",
            "unresolved_disagreements": [],
        },
        "replicator": review("the replicator"),
        "brancher": {"children": [], "relations": []},
        "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def checkpoint_tables(pg_dsn: str) -> str:
    """LangGraph's own tables, created once per test database.

    Separate from this runtime's migrations on purpose -- those tables belong
    to LangGraph -- so a test that drives a real graph has to ask for them, the
    way ``researchd`` does at startup.
    """

    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    return pg_dsn


def _router(runtime_db: Database, **kwargs: Any) -> ScriptedRouter:
    return ScriptedRouter(
        answers=_answers(**kwargs.pop("answers", {})),
        store=RuntimeStore(runtime_db),
        **kwargs,
    )


def _advance(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project_id: str,
    idea_id: str,
    router: ScriptedRouter,
    literature: FakeLiterature | None = None,
    can_execute: bool = False,
    repo_path: Path | None = None,
    executors: dict[str, object] | None = None,
):
    """Advance one idea by one stage, with this host's capabilities supplied.

    ``can_execute`` is a *test* convenience and is no longer a parameter of
    `advance_idea`: whether this host can measure anything is derived from
    whether there is an executor and a repository, because passing it as a
    flag is how the composition root came to pass ``False`` on a host that
    could. A test that wants the capability supplies a stub executor; one
    that wants its absence supplies nothing, which is what most of these
    want.
    """

    return advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=project_id,
        idea_id=idea_id,
        models=router,
        literature=literature,
        charter="Understand sparse regression solvers.",
        problem="Do CG and working sets coincide?",
        repo_path=repo_path,
        executors=(
            executors
            if executors is not None
            else ({"local": object()} if can_execute else {})
        ),
    )


def _drive_to(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project_id: str,
    idea_id: str,
    router: ScriptedRouter,
    target: Stage | None,
    *,
    literature: FakeLiterature | None = None,
    can_execute: bool = False,
    repo_path: Path | None = None,
    executors: dict[str, object] | None = None,
    max_steps: int = 24,
    stop_before: bool = False,
):
    """Advance until ``target`` is the stage that ran, or the track stops.

    A bounded loop with the trace in the failure message, rather than a fixed
    repeat count. The count form is what the first version of this file used,
    and it made every "the track never got there" failure look identical
    whether the cause was one extra stage or an infinite loop.
    """

    trace: list[str] = []
    result = None
    for _ in range(max_steps):
        if (
            stop_before
            and _next_stage(portfolio, runtime_db, project_id, idea_id, literature)
            is target
        ):
            return result, trace
        result = _advance(
            portfolio,
            runtime_db,
            pg_dsn,
            tmp_path,
            project_id,
            idea_id,
            router,
            literature=literature,
            can_execute=can_execute,
            repo_path=repo_path,
            executors=executors,
        )
        trace.append(f"{result.stage} ok={result.ok} {result.detail[:70]}")
        if result.stage is target or result.stage is None:
            return result, trace
        if not result.ok:
            return result, trace
    raise AssertionError(
        f"the track did not reach {target} in {max_steps} steps:\n  "
        + "\n  ".join(trace)
    )


# ------------------------------------------------------------ the graph --
def test_the_graph_is_bounded_and_ends() -> None:
    """One stage per invocation, and no edge back to the selector.

    The shape the brief asks for: "then END the graph invocation". A cycle in
    this graph would be a track that never stops, which is the failure mode a
    bounded graph exists to prevent.
    """

    graph = build_track_graph()
    compiled = graph.compile()
    drawn = compiled.get_graph()
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    assert ("conclude", "__end__") in edges
    assert not any(
        target == "hydrate_idea" for source, target in edges if source != "__start__"
    )


# ------------------------------------------------------ stage by stage --
def test_a_candidate_advances_one_stage_per_entry(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Exactly one stage, and the cheapest one available, each time."""

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(runtime_db)
    seen: list[Stage] = []
    for _ in range(3):
        result = _advance(
            portfolio,
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea.idea_id,
            router,
        )
        assert result.ok, result.detail
        seen.append(result.stage)
    assert seen == [Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY]


def test_each_stage_gets_its_own_run_row_invisible_to_objective_continuation(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """A track borrows a run for budgets and provenance, and nothing else.

    `parked_objectives` drives `advance_objective`, which opens *successor
    cycles*. A finished idea-track run appearing there would mean the portfolio
    and objective continuation were both deciding what runs next.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(runtime_db)
    result = _advance(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router
    )
    runtime = RuntimeStore(runtime_db)
    run = runtime.require_run(result.run_id)
    assert run.run_kind is RunKind.IDEA_TRACK
    assert run.status is RunStatus.SUCCEEDED
    assert runtime.parked_objectives(project_id=runtime_project) == ()
    assert runtime.stranded_runs(grace_seconds=0) == ()


def test_a_fatal_objection_rejects_the_idea_and_ends_the_track(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Killing cheaply, end to end.

    The falsifier's success is the idea's death, the status carries the reason,
    and nothing further is spent.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(
        runtime_db,
        answers={
            "falsifier": {
                "summary": "already known",
                "objections": [
                    {
                        "severity": "FATAL",
                        "summary": "Theorem 3 of a 2013 paper already states this",
                    }
                ],
                "attempted": ["a literature check"],
            }
        },
    )
    for _ in range(3):
        result = _advance(
            portfolio,
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea.idea_id,
            router,
        )
    assert result.stage is Stage.FALSIFY
    assert result.disposition is Disposition.REJECT

    rejected = portfolio.require_idea(idea.idea_id)
    assert rejected.status is IdeaStatus.REJECTED
    assert "Theorem 3" in (rejected.retire_reason or "")

    # And nothing further happens: the next entry finds no stage.
    after = _advance(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router
    )
    assert after.stage is None
    assert "REJECTED" in after.reason


def test_a_duplicate_becomes_a_reference_rather_than_disappearing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    survivor, _ = seed_idea(portfolio, runtime_project)
    twin, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=__import__(
            "research_os.portfolio.models", fromlist=["IdeaOrigin"]
        ).IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(title="found again, worded differently"),
        origin_role="blind_explorer",
    )
    router = _router(runtime_db)
    result = _advance(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, twin.idea_id, router
    )
    assert result.disposition is Disposition.DUPLICATE
    assert portfolio.require_idea(twin.idea_id).status is IdeaStatus.SUPERSEDED
    assert portfolio.duplicate_survivor(twin.idea_id) == survivor.idea_id
    assert portfolio.require_idea(twin.idea_id).retire_reason


def test_the_adjudication_type_is_written_without_a_model_call(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The stage that costs nothing, and decides which gate applies."""

    idea, _ = seed_idea(portfolio, runtime_project, adjudication_types=[])
    router = _router(
        runtime_db,
        answers={"scientific_discovery": discovery_answer(MATHEMATICAL_FALSIFIER)},
    )
    result, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        Stage.ADJUDICATE,
    )
    assert result.stage is Stage.ADJUDICATE, trace

    assert result.model_calls == 0
    assert result.cost_usd == 0
    head = portfolio.require_version(idea.idea_id)
    assert AdjudicationType.MATHEMATICAL in head.adjudication_types


# ------------------------------------------------------- the literature --
def test_a_novelty_matrix_citing_an_unretrieved_work_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Fail-closed, like the v1 analyst, and for the same reason.

    The row is not dropped and the report is not salvaged: the statement it
    supported was reached some other way, and keeping the statement while
    removing its only stated ground leaves an ungrounded claim looking
    grounded.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    router = _router(
        runtime_db,
        answers={
            "literature_scout": {
                "rows": [
                    {
                        "proposed_component": "the claim",
                        "closest_known_result": "a paper I remember",
                        "relation": "same",
                        "source_key": "openalex:INVENTED",
                        "confidence": 0.9,
                    }
                ],
                "queries": ["x"],
            }
        },
    )
    literature = FakeLiterature()
    result, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        Stage.LITERATURE_AUDIT,
        literature=literature,
    )
    assert result.stage is Stage.LITERATURE_AUDIT, trace
    assert not result.ok
    assert "not retrieved" in result.detail
    assert portfolio.list_evidence(idea_id=idea.idea_id) == ()


def test_a_grounded_audit_records_one_evidence_row_per_matrix_row(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(
        portfolio,
        runtime_project,
        falsifier=LITERATURE_FALSIFIER,
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    router = _router(runtime_db)
    literature = FakeLiterature()
    result, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        Stage.LITERATURE_AUDIT,
        literature=literature,
    )
    assert result.ok, (result.detail, trace)
    evidence = portfolio.list_evidence(idea_id=idea.idea_id)
    assert len(evidence) == 3
    assert all(item.kind is EvidenceKind.LITERATURE for item in evidence)
    assert {item.literature_key for item in evidence} == set(literature.keys)


def test_an_idea_settled_by_measurement_stops_where_nothing_can_measure(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The honest refusal.

    An empirical idea on a host with no executor stops below VALIDATED and
    says so, rather than being concluded from reasoning about what the
    measurement would have shown.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=EMPIRICAL_FALSIFIER)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    router = _router(
        runtime_db,
        answers={"scientific_discovery": discovery_answer(EMPIRICAL_FALSIFIER)},
    )
    result, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        Stage.EVIDENCE,
        literature=FakeLiterature(),
    )
    assert result.stage is Stage.EVIDENCE, trace
    assert not result.ok
    assert "execute" in result.detail
    assert (
        portfolio.require_idea(idea.idea_id).operational_state
        is OperationalState.BLOCKED_EXTERNAL
    )
    assert portfolio.require_idea(idea.idea_id).status not in {
        IdeaStatus.REJECTED,
        IdeaStatus.PARKED,
        IdeaStatus.SUPERSEDED,
    }, "a missing capability is not a scientific verdict"
    assert portfolio.require_idea(idea.idea_id).retire_reason is None


# ------------------------------------------------------- the review board --
def _to_review_board(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    router: ScriptedRouter,
    literature: FakeLiterature,
) -> str:
    """Drive one idea to the point where the review board is next."""

    idea, _ = seed_idea(
        portfolio,
        project,
        falsifier=LITERATURE_FALSIFIER,
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    _, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        project,
        idea.idea_id,
        router,
        Stage.REVIEW_BOARD,
        literature=literature,
        stop_before=True,
    )
    assert trace
    return idea.idea_id


def test_the_board_records_three_reviews_none_of_which_saw_the_others(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    router = _router(runtime_db)
    idea_id = _to_review_board(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        router,
        FakeLiterature(),
    )
    result = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert result.stage is Stage.REVIEW_BOARD
    assert result.ok, result.detail
    assert result.model_calls == 3

    roles = {item.reviewer_role for item in portfolio.live_reviews(idea_id=idea_id)}
    assert roles == {
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    }
    # No reviewer's prompt contains another reviewer's summary.
    #
    # Asserted against the *summaries the reviewers actually wrote* rather than
    # against the word "PASS", which appears in every reviewer's instructions
    # and made the first version of this assertion vacuous in the other
    # direction -- it failed on the prompt telling the reviewer which verdicts
    # exist.
    summaries = {item.summary for item in portfolio.live_reviews(idea_id=idea_id)}
    for request in router.requests:
        if "reviewer" not in str(request.role):
            continue
        for summary in summaries:
            if summary in request.prompt:
                raise AssertionError(
                    f"{request.role} was shown another reviewer's verdict: {summary}"
                )


def test_one_model_reviewing_three_times_is_not_called_independent(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The honest limit, measured from the rows the board actually wrote."""

    from research_os.portfolio.gates import board_independence

    router = _router(runtime_db)
    idea_id = _to_review_board(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        router,
        FakeLiterature(),
    )
    _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert board_independence(portfolio.live_reviews(idea_id=idea_id)) == 1


def test_a_board_across_three_families_is_counted_as_three(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    from research_os.portfolio.gates import board_independence

    router = _router(
        runtime_db,
        providers={
            "methodology_reviewer": "claude",
            "novelty_reviewer": "codex",
            "skeptic_reviewer": "gemini",
        },
        models_by_role={
            "methodology_reviewer": "opus",
            "novelty_reviewer": "gpt",
            "skeptic_reviewer": "gemini-pro",
        },
    )
    idea_id = _to_review_board(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        router,
        FakeLiterature(),
    )
    _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert board_independence(portfolio.live_reviews(idea_id=idea_id)) == 3


def test_a_provider_outage_mid_board_does_not_repeat_the_reviews_that_landed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The one measured reason the track is a graph.

    The board is three nodes. An outage at the third leaves the first two
    recorded, the idea in BLOCKED_PROVIDER rather than rejected, and the retry
    pays for one call instead of three.
    """

    router = _router(runtime_db)
    idea_id = _to_review_board(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        router,
        FakeLiterature(),
    )
    router.unavailable_roles = {"skeptic_reviewer"}
    result = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert not result.ok
    assert result.model_calls == 2
    assert (
        portfolio.require_idea(idea_id).operational_state
        is OperationalState.BLOCKED_PROVIDER
    )
    assert portfolio.require_idea(idea_id).status is not IdeaStatus.REJECTED

    landed = {item.reviewer_role for item in portfolio.live_reviews(idea_id=idea_id)}
    assert landed == {ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY}

    router.unavailable_roles = set()
    before = len(router.requests)
    retry = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert retry.ok, retry.detail
    reviewer_calls = [
        request
        for request in router.requests[before:]
        if "reviewer" in str(request.role)
    ]
    assert len(reviewer_calls) == 1, (
        "the two reviews that landed must not be paid for twice"
    )


# ------------------------------------------------------- the meta review --
def test_a_meta_reviewer_recommending_the_top_tier_gets_what_the_rows_allow(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The scripted meta-reviewer always says HUMAN_READY. It never gets it.

    The idea below has three passing reviews and a grounded novelty audit, and
    is still missing replication. The gate lowers the recommendation, records
    what was missing, and the idea's status follows the gate rather than the
    recommendation.
    """

    router = _router(runtime_db)
    idea_id = _to_review_board(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        router,
        FakeLiterature(),
    )
    _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    result = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        router,
        literature=FakeLiterature(),
    )
    assert result.stage is Stage.META_REVIEW
    assert result.disposition is not Disposition.HUMAN_READY
    assert portfolio.require_idea(idea_id).status is not IdeaStatus.HUMAN_READY

    actions = [
        item
        for item in portfolio.list_actions(idea_id=idea_id)
        if item.stage is Stage.META_REVIEW
    ]
    assert actions and actions[-1].status is ActionStatus.SUCCEEDED


# ------------------------------------------------------------ idempotency --
def test_the_same_basis_is_not_paid_for_twice(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Two ticks reaching the same conclusion cost one call, not two.

    Simulated by opening the action out from under the track: the second entry
    finds the basis already claimed and declines, which is what a replayed
    event or a duplicated tick produces.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(runtime_db)
    first = _advance(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router
    )
    assert first.stage is Stage.DEDUP and first.ok

    with pytest.raises(DuplicateBasisError):
        portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.DEDUP,
            basis_digest=portfolio.list_actions(idea_id=idea.idea_id)[0].basis_digest,
        )


def test_an_idea_with_a_track_in_flight_is_not_started_again(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="held-by-somebody-else",
    )
    result = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        _router(runtime_db),
    )
    assert result.ok
    assert result.action_id is None
    assert "another pass" in result.detail


def test_a_branch_opens_children_with_recorded_lineage(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    parent, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=parent.idea_id, status=IdeaStatus.VALIDATED)
    router = _router(
        runtime_db,
        answers={
            "brancher": {
                "children": [
                    {
                        "title": "the same result under weaker assumptions",
                        "research_question": "Does the divergence survive rank deficiency?",
                        "core_idea": "Drop the full-rank assumption and check.",
                        "falsifier": "Exhibit a rank-deficient instance where it fails.",
                    }
                ],
                "relations": ["GENERALIZES"],
            }
        },
    )
    result, trace = _drive_to(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        parent.idea_id,
        router,
        Stage.BRANCH,
        literature=FakeLiterature(),
    )
    assert result.stage is Stage.BRANCH, trace
    children = portfolio.descendants(parent.idea_id)
    assert len(children) == 1
    child = portfolio.require_idea(children[0])
    assert child.depth == parent.depth + 1
    assert child.lineage_root == parent.lineage_root
    kinds = {edge.kind for edge in portfolio.edges_of(child.idea_id)}
    assert EdgeKind.GENERALIZES in kinds


def test_every_call_a_track_makes_is_attributed_to_its_own_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Cost attribution, which is what `sql/0015` exists to keep.

    ``record_model_call`` derives ``project_id`` by looking the run up. A
    router built before the run exists therefore carries a run id naming
    nothing, and the whole track's spend lands on a row with no project --
    silently, because the foreign key on ``model_calls.run_id`` was dropped
    precisely so effect rows could outlive their runs.

    Driven through the factory form, which is what the daemon's handler
    passes.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(runtime_db)
    seen: list[str] = []

    def factory(run_id: str):
        seen.append(run_id)
        return router

    result = advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=runtime_project,
        idea_id=idea.idea_id,
        models=factory,
    )
    assert result.run_id
    assert seen == [result.run_id], seen

    runtime = RuntimeStore(runtime_db)
    assert runtime.get_run(result.run_id) is not None


@pytest.mark.parametrize("verdict", ["duplicate", "merge"])
def test_an_adjudicated_duplicate_is_recorded_without_violating_the_schema(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
    verdict: str,
) -> None:
    """Both adjudicator verdicts must be storable. `merge` was not.

    `is_lineage` is `kind not in ('CONTRADICTS','DUPLICATE_OF')`, so
    `MERGED_FROM` is a lineage edge and `idea_edges_acyclic_ck` demands
    `child_depth > parent_depth`. Deduplication compares *siblings* -- two
    ideas two explorers produced independently, both at depth 0 -- so every
    merge it tried to record violated the constraint with

        new row for relation "idea_edges" violates check constraint
        "idea_edges_acyclic_ck"

    Nobody found out because the branch was unreachable: the similarity
    threshold was set so high that layer four had never once been consulted.
    Recalibrating it on measured data made the branch live, and it failed on
    the first real merge.

    §7 of the architecture says a semantic duplicate is "given a
    `DUPLICATE_OF` edge to the survivor" and §4.1's disposition table says the
    same; the code disagreed with both.
    """

    survivor, _ = seed_idea(portfolio, runtime_project)
    candidate, _ = seed_idea(
        portfolio,
        runtime_project,
        research_question="a near-identical question about the same thing",
    )
    assert survivor.depth == candidate.depth == 0, (
        "the point of this test is two ideas at the same depth"
    )

    router = _router(
        runtime_db,
        answers={
            "duplicate_adjudicator": {
                "verdict": verdict,
                "of_idea_id": survivor.idea_id,
                "rationale": "the same direction in different words",
            }
        },
    )
    # Force layer four: identical-enough text that the screen escalates.
    outcome = _advance(
        portfolio,
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        candidate.idea_id,
        router,
    )
    assert outcome.ok, outcome.detail

    stored = portfolio.get_idea(candidate.idea_id)
    assert stored is not None
    # Strict, not conditional: a test that silently skips its own subject is
    # the failure mode §H of the build report is about.
    assert stored.status is IdeaStatus.SUPERSEDED, (
        "the adjudicator was never reached, so this test proves nothing; "
        f"the candidate is {stored.status}"
    )
    kinds = {edge.kind for edge in portfolio.edges_of(candidate.idea_id)}
    assert EdgeKind.MERGED_FROM not in kinds, (
        "a sibling merge cannot be a lineage edge; it violates idea_edges_acyclic_ck"
    )
    assert EdgeKind.DUPLICATE_OF in kinds
