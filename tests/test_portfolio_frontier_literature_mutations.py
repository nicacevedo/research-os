"""Mutation-killing regressions for the research frontier and literature intelligence.

An independent mutation review found that the suite let each of the changes
named below through: the code could be broken in that way and every test
still passed. Each test here fails under its mutation and passes on the
code as it is, and each docstring says which property it pins and why.

What is held, grouped by the module that owns it:

- **a literature answer** (``litintel``): only a claim that bears on the idea
  becomes its evidence; discovery runs first and its failure is reported,
  never fatal; every statement -- a gap and a disagreement as much as a
  claim -- cites only what was retrieved, and a quotation is found in a work
  it cites; a closed or already-reviewed idea gets claims but no evidence;
  evidence is bound to the version that asked; a replay after a crash writes
  nothing twice; and the watch is per bucket and covers live ideas only;
- **the frontier** (``frontier``): continuation leaves a VALIDATED idea
  alone and rejects only on a fatal objection to the *claim*; a follow-up
  child keeps the relation the explorer named, a duplicate child is
  recorded as convergence naming the request, a consumed request costs
  nothing, and a replay closes from what the first attempt recorded; only an
  objection that asked a question raises a request;
- **the raising sites** (``runner``): a branch records why its child exists,
  a disagreeing replication and an inconclusive primary each raise their
  request, and a meta-review that parks says when to look again and does not
  lose a second meta-review's questions;
- **allocation and the tick**: project-level work is bought one at a time,
  regardless of free idea slots, under a key that moves with each finished
  attempt; an eligible request holds off the barren-exploration pause; and a
  literature request whose work keeps failing is re-bought, then declined,
  releasing the idea that waited on it so the next request is served.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_os.portfolio import frontier, litintel, runner
from research_os.portfolio.allocation import (
    FOLLOW_UP,
    LITERATURE_REQUEST,
    SYNTHESIZE,
    Allocation,
    plan,
)
from research_os.portfolio.config import load_config
from research_os.portfolio.contracts import MetaReviewOutput
from research_os.portfolio.models import (
    Disposition,
    EdgeKind,
    EmpiricalConclusion,
    ExperimentRole,
    IdeaOrigin,
    IdeaStatus,
    ObjectionTarget,
    OperationalState,
    PortfolioStatus,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    RequestState,
    Severity,
    Stage,
)
from research_os.portfolio.stages import STAGE_ORDER
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_literature_intel import (
    ANSWER,
    WORKS,
    FakeRetriever,
    FixtureCorpus,
    _ask,
    _context,
)
from tests.test_portfolio_stages import (
    _complete_evidence,
    _complete_reviews,
    objection,
    snapshot,
)

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

CONFIG = load_config()

#: A follow-up child, in the explorer's contract shape.
CHILD = {
    "title": "Support paths under ties in the dual violation",
    "research_question": (
        "Do support paths diverge when two coordinates tie on dual violation?"
    ),
    "core_idea": "Tie-breaking decides the ordering exactly where the bound is flat.",
    "falsifier": "Exhibit a tied instance on which both methods visit one path.",
}


def _router(runtime_db: Database, **answers: Any) -> ScriptedRouter:
    return ScriptedRouter(answers=dict(answers), store=RuntimeStore(runtime_db))


def _track(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    idea_id: str,
    router: Any,
) -> runner.TrackContext:
    runtime = RuntimeStore(runtime_db)
    return runner.TrackContext(
        config=CONFIG,
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp / "artifacts", store=runtime),
        project_id=project,
        idea_id=idea_id,
        run_id=runtime.create_run(project_id=project, objective="mut").run_id,
    )


def _tick(runtime_db: Database, pg_dsn: str, tmp: Path, project: str) -> Any:
    return tick(
        db=runtime_db,
        project_id=project,
        runtime_config=make_config(pg_dsn, tmp / "artifacts"),
        portfolio_config=CONFIG,
    )


def _answer(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    request_id: str,
    answer: dict[str, Any],
    **kwargs: Any,
) -> Any:
    router = _router(runtime_db, literature_reader=answer)
    return litintel.answer_request(
        _context(portfolio, runtime_db, tmp, project, router),
        request_id,
        literature=kwargs.pop("literature", FixtureCorpus()),
        **kwargs,
    )


# ======================================================= literature answers
def test_a_claim_that_does_not_bear_on_the_idea_is_not_its_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """LI01. ``relation_to_idea: NONE`` is a claim about the record, not the idea.

    It is stored as a literature claim; it is not evidence for or against an
    idea the reader said it does not bear on. Writing it as evidence would
    hand the gates a row the reader never offered as one.
    """

    idea, request = _ask(portfolio, runtime_project)
    answer = {
        **ANSWER,
        "claims": [
            ANSWER["claims"][0],
            {**ANSWER["claims"][1], "relation_to_idea": "NONE"},
        ],
        "gaps": [],
    }
    result = _answer(
        portfolio, runtime_db, tmp_path, runtime_project, request.request_id, answer
    )
    assert result.ok, result.detail
    claims = {
        item.statement: item
        for item in portfolio.list_literature_claims(project_id=runtime_project)
    }
    assert len(claims) == 2, "both claims are stored as claims"
    unrelated = claims[ANSWER["claims"][1]["statement"]]
    related = claims[ANSWER["claims"][0]["statement"]]
    evidence = portfolio.list_evidence(idea_id=idea.idea_id)
    assert [item.claim_id for item in evidence] == [related.claim_id]
    assert unrelated.claim_id not in {item.claim_id for item in evidence}
    assert len(result.evidence) == 1


def test_a_discovery_failure_is_reported_and_the_question_is_still_answered(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """LI03. Discovery is best effort: the index may already hold the answer.

    A retriever that raises -- no credentials, a provider down -- must not
    turn a question the shared index can answer into a failed work item, and
    the resolution must say discovery did not run, so nobody reads the
    answer as resting on a fresh retrieval.
    """

    class Down:
        def retrieve(self, query: str) -> dict[str, Any]:
            raise RuntimeError("the discovery provider is down")

    _idea, request = _ask(portfolio, runtime_project)
    result = _answer(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        request.request_id,
        ANSWER,
        retriever=Down(),
    )
    assert result.ok, result.detail
    closed = portfolio.require_request(request.request_id)
    assert closed.state is RequestState.CONSUMED
    assert "discovery could not run" in (closed.resolution or "")
    assert "the discovery provider is down" in (closed.resolution or "")
    assert result.claims, "the index still answered"


def test_discovery_runs_before_the_index_is_searched(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """LI04. Retrieval ingests into the index the search then reads.

    Searching first reads the index as it was before discovery, so the works
    discovery just brought in are never in the packet -- the call would be
    paid for and ignored.
    """

    calls: list[tuple[str, str]] = []

    class Corpus(FixtureCorpus):
        def search(self, query: str, *, limit: int = 12) -> Any:
            calls.append(("search", query))
            return super().search(query, limit=limit)

    class Retriever(FakeRetriever):
        def retrieve(self, query: str) -> dict[str, Any]:
            calls.append(("retrieve", query))
            return super().retrieve(query)

    _idea, request = _ask(portfolio, runtime_project)
    result = _answer(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        request.request_id,
        ANSWER,
        literature=Corpus(),
        retriever=Retriever(),
    )
    assert result.ok, result.detail
    assert calls == [("retrieve", request.question), ("search", request.question)]


@pytest.mark.parametrize("field", ["gaps", "disagreements"])
def test_a_gap_or_disagreement_citing_an_unretrieved_work_stores_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    field: str,
) -> None:
    """LI05. Verification covers every statement, not only the claims.

    A gap or a disagreement becomes a frontier request and then a new idea
    whose provenance names it; one resting on a work nobody retrieved would
    seed new science from a model's recollection.
    """

    idea, request = _ask(portfolio, runtime_project)
    forged = {
        **ANSWER,
        "gaps": [],
        "disagreements": [],
        field: [
            {
                "statement": "A paper nobody retrieved says the question is open.",
                "work_keys": ["openalex:W999"],
            }
        ],
    }
    result = _answer(
        portfolio, runtime_db, tmp_path, runtime_project, request.request_id, forged
    )
    assert not result.ok
    assert result.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "openalex:W999" in result.detail
    assert portfolio.list_literature_claims(project_id=runtime_project) == ()
    assert portfolio.list_evidence(idea_id=idea.idea_id) == ()
    assert [
        item.request_id for item in portfolio.list_requests(project_id=runtime_project)
    ] == [request.request_id], "no question is raised from an unverified reading"


@pytest.mark.parametrize(
    "status",
    [
        IdeaStatus.REJECTED,
        IdeaStatus.SUPERSEDED,
        IdeaStatus.VALIDATED,
        IdeaStatus.HUMAN_READY,
    ],
)
def test_a_closed_or_reviewed_idea_gets_claims_but_no_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    status: IdeaStatus,
) -> None:
    """LI06. A late reading must not edit an idea's reviewed evidence set.

    A rejected or superseded idea is closed; a validated or human-ready one
    was reviewed on a fixed set of evidence, and a row appended now would
    make every one of those reviews stale without anyone asking. The claims
    are still the project's, and a gap still raises its question.
    """

    idea, request = _ask(portfolio, runtime_project)
    closed = status in {IdeaStatus.REJECTED, IdeaStatus.SUPERSEDED}
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=status,
        retire_reason=f"{status} by the test" if closed else None,
    )
    result = _answer(
        portfolio, runtime_db, tmp_path, runtime_project, request.request_id, ANSWER
    )
    assert result.ok, result.detail
    assert len(portfolio.list_literature_claims(project_id=runtime_project)) == 3
    assert portfolio.list_evidence(idea_id=idea.idea_id) == ()
    assert result.evidence == ()
    assert len(result.raised) == 1, "the gap still becomes a question"


def test_a_quotation_must_come_from_a_work_the_statement_cites(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """LI09. Quoting W11 while citing W12 attributes W11's words to W12.

    Both works were retrieved, so a check against "any supplied work" passes
    -- and stores a QUOTED claim whose quotation is not in the source it
    names.
    """

    idea, request = _ask(portfolio, runtime_project)
    misattributed = {
        **ANSWER,
        "claims": [
            {
                "kind": "FINDING",
                "statement": "Column generation reaches the optimum by another path.",
                "work_keys": ["openalex:W12"],
                "excerpt": "reach the same optimum by a different support path",
                "relation_to_idea": "SUPPORTS",
            }
        ],
        "gaps": [],
    }
    assert misattributed["claims"][0]["excerpt"] in WORKS["openalex:W11"][1]
    result = _answer(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        request.request_id,
        misattributed,
    )
    assert not result.ok
    assert "quotes" in result.detail and "openalex:W12" in result.detail
    assert portfolio.list_literature_claims(project_id=runtime_project) == ()
    assert portfolio.list_evidence(idea_id=idea.idea_id) == ()


def test_literature_evidence_is_bound_to_the_version_that_asked(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """LI14. The reader answered the asking version's question, not a later one's.

    The idea was revised while the question was in flight. Evidence written
    to the new version would be counted by the new version's gates as
    bearing on content the reader never saw.
    """

    idea, request = _ask(portfolio, runtime_project)
    assert request.source_version == 1
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(research_question="A sharpened question, asked later?"),
        origin_role="scientific_discovery",
        origin_stage=str(Stage.DISCOVER),
    )
    assert portfolio.require_idea(idea.idea_id).current_version == 2
    result = _answer(
        portfolio, runtime_db, tmp_path, runtime_project, request.request_id, ANSWER
    )
    assert result.ok, result.detail
    evidence = portfolio.list_evidence(idea_id=idea.idea_id)
    assert len(evidence) == 2
    assert {item.idea_version for item in evidence} == {1}
    assert portfolio.list_evidence(idea_id=idea.idea_id, idea_version=2) == ()


def test_a_reading_replayed_after_a_crash_writes_nothing_twice(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the writes and the close leaves the request OPEN.

    The queue retries it, the reader answers again -- with a new call id --
    and the replay must find the claims, the evidence rows and the raised
    questions the first attempt wrote rather than adding a second set.
    """

    idea, request = _ask(portfolio, runtime_project)
    real_close = PortfolioStore.close_request
    crashed: list[str] = []

    def crash_once(self: PortfolioStore, request_id: str, **kwargs: Any) -> Any:
        if kwargs.get("state") is RequestState.CONSUMED and not crashed:
            crashed.append(request_id)
            raise RuntimeError("the worker died before closing the request")
        return real_close(self, request_id, **kwargs)

    monkeypatch.setattr(PortfolioStore, "close_request", crash_once)
    with pytest.raises(RuntimeError, match="worker died"):
        _answer(
            portfolio, runtime_db, tmp_path, runtime_project, request.request_id, ANSWER
        )
    assert crashed == [request.request_id]
    assert portfolio.require_request(request.request_id).state is RequestState.OPEN
    claims = portfolio.list_literature_claims(project_id=runtime_project)
    evidence = portfolio.list_evidence(idea_id=idea.idea_id)
    requests = portfolio.list_requests(project_id=runtime_project)
    assert len(claims) == 3 and len(evidence) == 2 and len(requests) == 2

    replay = _answer(
        portfolio, runtime_db, tmp_path, runtime_project, request.request_id, ANSWER
    )
    assert replay.ok, replay.detail
    assert portfolio.require_request(request.request_id).state is RequestState.CONSUMED
    assert {
        item.claim_id
        for item in portfolio.list_literature_claims(project_id=runtime_project)
    } == {item.claim_id for item in claims}
    assert {
        item.evidence_id for item in portfolio.list_evidence(idea_id=idea.idea_id)
    } == {item.evidence_id for item in evidence}
    assert len(portfolio.list_requests(project_id=runtime_project)) == 2


def test_a_watch_is_one_request_per_idea_per_bucket(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """LI07. The bucket is what makes a pass idempotent *and* periodic.

    Without it in the key, the first watch's request is found again by every
    later pass, so the record is read once per idea for the project's life.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    first = litintel.watch(portfolio, project_id=runtime_project, bucket="20260923")
    again = litintel.watch(portfolio, project_id=runtime_project, bucket="20260923")
    later = litintel.watch(portfolio, project_id=runtime_project, bucket="20260924")
    assert first == again and len(first) == 1
    assert len(later) == 1 and later != first
    assert len(portfolio.list_requests(project_id=runtime_project)) == 2
    assert "20260924" in portfolio.require_request(later[0]).source_ref


def test_a_watch_covers_live_ideas_only(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """LI08. A watch is a paid reading; only ideas still being worked earn one.

    A candidate has not passed the cheap screens, and a rejected or parked
    idea is not being worked -- reading the record for them spends the
    literature budget on questions nobody is asking.
    """

    statuses = {
        IdeaStatus.CANDIDATE: None,
        IdeaStatus.REJECTED: "refuted",
        IdeaStatus.PARKED: "parked",
        IdeaStatus.PROMISING: None,
    }
    ideas: dict[IdeaStatus, str] = {}
    for index, (status, reason) in enumerate(statuses.items()):
        idea, _ = seed_idea(
            portfolio,
            runtime_project,
            title=f"idea {index}",
            research_question=f"question {index}?",
        )
        if status is not IdeaStatus.CANDIDATE:
            portfolio.set_status(
                idea_id=idea.idea_id,
                status=status,
                retire_reason=reason,
                revisit_if="never" if status is IdeaStatus.PARKED else None,
            )
        ideas[status] = idea.idea_id
    raised = litintel.watch(portfolio, project_id=runtime_project, bucket="20260923")
    assert [portfolio.require_request(item).source_idea_id for item in raised] == [
        ideas[IdeaStatus.PROMISING]
    ]


# ============================================================ continuation
def test_continuation_leaves_a_validated_idea_with_nothing_left_alone(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """FR01. VALIDATED is closed for synthesis, not in limbo.

    Parking it would take a validated result out of what the writer reads
    and out of the human-ready route, for no reason but that every stage it
    called for has run.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    done = frozenset(STAGE_ORDER)
    finished = snapshot(
        status=IdeaStatus.VALIDATED,
        succeeded_stages=done,
        basis_stages=done,
        evidence=_complete_evidence(),
        live_reviews=_complete_reviews(),
        revision_count=1,
    )
    from research_os.portfolio.stages import select_stage

    assert select_stage(finished, CONFIG)[0] is None
    assert (
        frontier.settle(
            portfolio, portfolio.require_idea(idea.idea_id), finished, CONFIG
        )
        is None
    )
    after = portfolio.require_idea(idea.idea_id)
    assert after.status is IdeaStatus.VALIDATED
    assert after.retire_reason is None and after.revisit_if is None


def test_a_fatal_objection_to_the_test_parks_rather_than_rejects(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """FR02. Only a fatal objection to the *claim* is a rejection.

    One to the test says the idea's way of settling itself is broken; with
    the revision bound spent the track ends, but the claim has not been
    shown wrong, and rejecting it would record a finding nobody made.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    cheap = frozenset({Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY})
    stuck = snapshot(
        status=IdeaStatus.PROMISING,
        succeeded_stages=cheap,
        open_objections=(objection(Severity.FATAL, ObjectionTarget.TEST),),
        revision_count=CONFIG.bounds.max_revisions_per_idea,
    )
    applied = frontier.settle(
        portfolio, portfolio.require_idea(idea.idea_id), stuck, CONFIG
    )
    assert applied == str(IdeaStatus.PARKED)
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.PARKED

    # The control: the same objection against the claim does reject.
    other, _ = seed_idea(
        portfolio, runtime_project, title="other", research_question="other?"
    )
    portfolio.set_status(idea_id=other.idea_id, status=IdeaStatus.PROMISING)
    killed = snapshot(
        status=IdeaStatus.PROMISING,
        succeeded_stages=cheap,
        open_objections=(objection(Severity.FATAL, ObjectionTarget.CLAIM),),
    )
    assert frontier.settle(
        portfolio, portfolio.require_idea(other.idea_id), killed, CONFIG
    ) == str(IdeaStatus.REJECTED)


# ============================================================== follow-ups
def _frontier_context(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    router: Any,
) -> frontier.FrontierContext:
    runtime = RuntimeStore(runtime_db)
    return frontier.FrontierContext(
        config=CONFIG,
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp / "artifacts", store=runtime),
        project_id=project,
        run_id=runtime.create_run(project_id=project, objective="f").run_id,
    )


def _request(portfolio: PortfolioStore, project: str, parent: Any) -> Any:
    return frontier.raise_request(
        portfolio,
        project_id=project,
        basis=RequestBasis.RESULT,
        source_ref="PEXP-20260101T000000Z-0000000a",
        question="What does the result open next?",
        source_idea_id=parent.idea_id,
        source_version=1,
    )


def test_a_follow_up_child_keeps_the_relation_the_explorer_named(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR04. SPECIALIZES is a claim about the lineage, and it is recorded.

    Forcing every follow-up edge to DERIVED_FROM erases whether the child
    narrows, widens or merely follows its parent -- which is what the
    lineage view and the diversity bound read.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    request = _request(portfolio, runtime_project, parent)
    router = _router(
        runtime_db,
        follow_up_explorer={"children": [CHILD], "relations": ["SPECIALIZES"]},
    )
    result = frontier.run_follow_up(
        _frontier_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
    )
    assert result.ok, result.detail
    (child,) = result.created
    (edge,) = [
        item
        for item in portfolio.edges_of(child)
        if item.child_idea_id == child and item.parent_idea_id == parent.idea_id
    ]
    assert edge.kind is EdgeKind.SPECIALIZES


def test_a_duplicate_follow_up_child_is_convergence_naming_the_request(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR09/FR17. A second route to an existing idea is a fact, not a discard.

    The existing idea gains a CONVERGENCE provenance row naming the request,
    by column and as its source, so the chain from the event to the idea it
    re-derived is a join; and no second idea is minted.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    existing, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=dict(CHILD),
        origin_role="blind_explorer",
    )
    request = _request(portfolio, runtime_project, parent)
    router = _router(
        runtime_db,
        follow_up_explorer={"children": [CHILD], "relations": ["DERIVED_FROM"]},
    )
    before = {item.idea_id for item in portfolio.list_ideas(project_id=runtime_project)}
    result = frontier.run_follow_up(
        _frontier_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
    )
    assert result.ok, result.detail
    assert result.created == () and result.converged == (existing.idea_id,)
    after = {item.idea_id for item in portfolio.list_ideas(project_id=runtime_project)}
    assert after == before, "a duplicate child is not a new idea"
    reasons = portfolio.provenance_of(existing.idea_id)
    assert [item.basis for item in reasons] == [
        ProvenanceBasis.BLIND_EXPLORATION,
        ProvenanceBasis.CONVERGENCE,
    ]
    convergence = reasons[1]
    assert convergence.request_id == request.request_id
    assert convergence.source_ref == request.request_id
    assert convergence.call_id is not None
    closed = portfolio.require_request(request.request_id)
    assert closed.state is RequestState.CONSUMED
    assert "convergence" in (closed.resolution or "")


def test_a_consumed_request_creates_nothing_and_costs_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR23. A request is answered once, however often its item is replayed."""

    parent, _ = seed_idea(portfolio, runtime_project)
    request = _request(portfolio, runtime_project, parent)
    portfolio.close_request(
        request.request_id, state=RequestState.CONSUMED, resolution="answered earlier"
    )
    router = _router(
        runtime_db,
        follow_up_explorer={"children": [CHILD], "relations": ["DERIVED_FROM"]},
    )
    result = frontier.run_follow_up(
        _frontier_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
    )
    assert result.ok and result.created == () and result.model_calls == 0
    assert router.requests == []
    assert [
        item.idea_id for item in portfolio.list_ideas(project_id=runtime_project)
    ] == [parent.idea_id]
    assert (
        portfolio.require_request(request.request_id).resolution == "answered earlier"
    )


def test_a_follow_up_replayed_after_its_children_closes_without_asking_again(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The crash between the children and the close.

    The children and a convergence are already recorded against the request.
    Asking the explorer again would pay twice and, from a nondeterministic
    model, add a second set of children; the replay closes from the record.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    request = _request(portfolio, runtime_project, parent)
    child, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.FOLLOW_UP,
        fields=dict(CHILD),
        parent_idea_id=parent.idea_id,
        origin_role="follow_up_explorer",
        provenance=(ProvenanceBasis.RESULT, request.source_ref, request.request_id),
    )
    elsewhere, _ = seed_idea(
        portfolio, runtime_project, title="elsewhere", research_question="where?"
    )
    portfolio.record_provenance(
        idea_id=elsewhere.idea_id,
        basis=ProvenanceBasis.CONVERGENCE,
        source_ref=request.request_id,
        request_id=request.request_id,
    )
    router = _router(
        runtime_db,
        follow_up_explorer={
            "children": [{**CHILD, "title": "a second, different child"}],
            "relations": ["DERIVED_FROM"],
        },
    )
    result = frontier.run_follow_up(
        _frontier_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
    )
    assert result.ok and "replay" in result.detail
    assert result.created == (child.idea_id,)
    assert result.converged == (elsewhere.idea_id,)
    assert router.requests == [], "the replay makes no model call"
    assert len(portfolio.list_ideas(project_id=runtime_project)) == 3
    assert portfolio.require_request(request.request_id).state is RequestState.CONSUMED


def test_only_an_objection_that_asked_a_question_raises_a_request(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """FR15. The reviewer decides whether a criticism is also a question.

    Turning every objection into a paid follow-up makes each review a
    fan-out; one with no question would be recorded as "(no question
    stated)" and bought anyway.
    """

    idea, version = seed_idea(portfolio, runtime_project)
    raised = frontier.requests_from_objections(
        portfolio,
        project_id=runtime_project,
        idea_id=idea.idea_id,
        version=version.version,
        basis=RequestBasis.REVIEWER_CRITICISM,
        objections=[
            ("IOBJ-silent", "the timing is confounded", ""),
            (
                "IOBJ-asks",
                "the ordering is assumed",
                "Does the ordering hold under ties?",
            ),
            ("IOBJ-blank", "the sample is small", "   "),
        ],
    )
    assert raised == 1
    (request,) = portfolio.list_requests(project_id=runtime_project)
    assert request.source_ref == "IOBJ-asks"
    assert request.question == "Does the ordering hold under ties?"


# ========================================================= raising sites
def test_a_branch_records_why_its_child_exists(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR10. Every idea has a reason, and a branch child's reason is its parent.

    Provenance is what the lineage view and convergence read; a child with
    none is an idea nobody can say the origin of.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=parent.idea_id, status=IdeaStatus.VALIDATED)
    router = _router(
        runtime_db,
        brancher={"children": [CHILD], "relations": ["GENERALIZES"]},
    )
    context = _track(
        portfolio, runtime_db, tmp_path, runtime_project, parent.idea_id, router
    )
    outcome = runner.run_branch(context, runner.build_snapshot(context))
    assert outcome.ok, outcome.detail
    (child,) = outcome.data["children"]
    assert portfolio.require_idea(child).origin is IdeaOrigin.BRANCH
    (reason,) = portfolio.provenance_of(child)
    assert reason.basis is ProvenanceBasis.BRANCH
    assert reason.source_ref == parent.idea_id
    assert reason.request_id is None


def _measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    conclusion: EmpiricalConclusion,
    *,
    role: ExperimentRole,
) -> tuple[Any, str]:
    idea, _ = seed_idea(portfolio, project)
    context = _track(portfolio, runtime_db, tmp, project, idea.idea_id, None)
    experiment_id = f"PEXP-20260101T000000Z-{role.value[:8].lower():0>8}"
    step = SimpleNamespace(
        experiment=SimpleNamespace(experiment_id=experiment_id),
        conclusion=conclusion,
        detail=f"read as {conclusion}",
    )
    runner._raise_from_measurement(
        context, runner.build_snapshot(context), step, role=role
    )
    return idea, experiment_id


@pytest.mark.parametrize(
    ("replicated", "raises"),
    [
        (EmpiricalConclusion.CONTRADICTS, True),
        (EmpiricalConclusion.SUPPORTS, False),
    ],
)
def test_a_replication_that_disagrees_raises_a_replication_request(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
    replicated: EmpiricalConclusion,
    raises: bool,
) -> None:
    """FR13. A primary and its replication disagreeing is a question.

    Decided from two stored conclusions, not from anybody's opinion: the
    primary said SUPPORTS. A replication that agrees raises nothing.
    """

    primary = SimpleNamespace(conclusion=EmpiricalConclusion.SUPPORTS)
    monkeypatch.setattr(
        PortfolioStore,
        "get_experiment",
        lambda self, **kwargs: (
            primary if kwargs.get("role") is ExperimentRole.PRIMARY else None
        ),
    )
    idea, experiment_id = _measured(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        replicated,
        role=ExperimentRole.REPLICATION,
    )
    requests = portfolio.list_requests(project_id=runtime_project)
    if not raises:
        assert requests == ()
        return
    (request,) = requests
    assert request.basis is RequestBasis.REPLICATION
    assert request.source_ref == experiment_id
    assert request.source_idea_id == idea.idea_id and request.source_version == 1


@pytest.mark.parametrize(
    ("conclusion", "basis"),
    [
        (EmpiricalConclusion.INCONCLUSIVE, RequestBasis.ANOMALY),
        (EmpiricalConclusion.INSUFFICIENT, RequestBasis.INSUFFICIENT),
        (EmpiricalConclusion.SUPPORTS, None),
    ],
)
def test_an_inconclusive_primary_raises_an_anomaly_request(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    conclusion: EmpiricalConclusion,
    basis: RequestBasis | None,
) -> None:
    """FR14. A value between the prespecified conditions is an anomaly.

    It left the prediction neither met nor failed, and asking why is new
    work. A SUPPORTS primary raises nothing here; its track continues.
    """

    idea, experiment_id = _measured(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        conclusion,
        role=ExperimentRole.PRIMARY,
    )
    requests = portfolio.list_requests(project_id=runtime_project)
    if basis is None:
        assert requests == ()
        return
    (request,) = requests
    assert request.basis is basis
    assert request.source_ref == experiment_id
    assert request.source_idea_id == idea.idea_id


def test_a_meta_review_that_parks_says_when_and_loses_no_later_questions(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR16. PARK is a state with a revisit condition, and questions are per call.

    A parked idea with no revisit condition is one nothing will reopen. And
    a second meta-review of the same version -- after the evidence changed
    -- must raise its own questions rather than colliding with the first
    one's key and being dropped.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    context = _track(
        portfolio, runtime_db, tmp_path, runtime_project, idea.idea_id, None
    )
    current = runner.build_snapshot(context)
    first = MetaReviewOutput(
        recommendation=Disposition.PARK,
        summary="the reviewers disagree on whether the effect is real",
        unresolved_disagreements=("whether the tie-breaking rule drives it",),
        follow_up_questions=("Does the effect survive a fixed tie-breaking rule?",),
    )
    runner._continue_after_meta_review(
        context, current, first, Disposition.PARK, call_id="MCALL-first"
    )
    parked = portfolio.require_idea(idea.idea_id)
    assert parked.status is IdeaStatus.PARKED
    assert parked.revisit_if == "whether the tie-breaking rule drives it"
    assert "parked it" in (parked.retire_reason or "")

    second = MetaReviewOutput(
        recommendation=Disposition.PARK,
        summary="with the new evidence the disagreement narrows",
        follow_up_questions=("Is the narrowing itself a property of the solver?",),
    )
    runner._continue_after_meta_review(
        context, current, second, Disposition.PARK, call_id="MCALL-second"
    )
    requests = portfolio.list_requests(project_id=runtime_project)
    assert len(requests) == 2, "the second meta-review's question was dropped"
    by_call = {
        call: [item for item in requests if call in item.source_ref]
        for call in ("MCALL-first", "MCALL-second")
    }
    assert [item.question for item in by_call["MCALL-first"]] == [
        "Does the effect survive a fixed tie-breaking rule?"
    ]
    assert [item.question for item in by_call["MCALL-second"]] == [
        "Is the narrowing itself a property of the solver?"
    ]
    assert {item.basis for item in requests} == {RequestBasis.REVIEWER_CRITICISM}


# ============================================================== allocation
def _plan(**overrides: Any) -> tuple[Allocation, ...]:
    arguments: dict[str, Any] = {
        "candidates": (),
        "config": CONFIG,
        "free_slots": 4,
        "lineage_in_flight": {},
        # At the ceiling, so no explorer is bought and every allocation below
        # is the project-level work under test.
        "candidate_pool": CONFIG.bounds.candidate_pool_ceiling,
        "pending_seeds": 0,
        "origin_counts": {},
        "minable_failures": 0,
        "tick_bucket": "20260923T0000",
    }
    arguments.update(overrides)
    return plan(**arguments)


def _kinds(allocations: tuple[Allocation, ...], kind: str) -> list[Allocation]:
    return [item for item in allocations if item.kind == kind]


@pytest.mark.parametrize(
    ("kind", "requests_arg", "in_flight_arg"),
    [
        (FOLLOW_UP, "open_requests", "follow_ups_in_flight"),
        (LITERATURE_REQUEST, "literature_requests", "literature_in_flight"),
    ],
)
def test_plan_buys_one_request_at_a_time(
    kind: str, requests_arg: str, in_flight_arg: str
) -> None:
    """FR11/FR12. One of each project-level kind per project, oldest first.

    That is the bound on these generators (invariant 14): two open requests
    are two ticks' work, and one already in flight means none this tick.
    """

    pending = [("FREQ-old", 0, "RESULT"), ("FREQ-new", 0, "RESULT")]
    bought = _kinds(_plan(**{requests_arg: pending}), kind)
    assert len(bought) == 1
    assert bought[0].payload["request_id"] == "FREQ-old"
    assert _kinds(_plan(**{requests_arg: pending, in_flight_arg: 1}), kind) == []


def test_plan_buys_project_work_with_no_free_idea_slot() -> None:
    """FR32. A follow-up, a synthesis and a literature answer hold no idea slot.

    Gating them on one starved the literature answers blocked ideas were
    waiting on whenever every track was busy measuring.
    """

    allocations = _plan(
        free_slots=0,
        open_requests=[("FREQ-f", 0, "RESULT")],
        literature_requests=[("FREQ-l", 0, "LITERATURE")],
        synthesis_basis=("basis-digest", 0),
    )
    assert sorted(item.kind for item in allocations) == sorted(
        [FOLLOW_UP, LITERATURE_REQUEST, SYNTHESIZE]
    )
    assert (
        _kinds(
            _plan(free_slots=0, synthesis_basis=("b", 0), syntheses_in_flight=1),
            SYNTHESIZE,
        )
        == []
    )


@pytest.mark.parametrize("kind", [FOLLOW_UP, LITERATURE_REQUEST, SYNTHESIZE])
def test_a_project_work_key_moves_with_its_generation(kind: str) -> None:
    """The generation is in the key, or a finished attempt spends it forever.

    ``work_items.dedup_key`` is permanently unique and ``enqueue`` is
    ``on conflict do nothing``: a key that does not move after a failure is
    a retry the queue refuses silently, and one that moves within a
    generation is two items for one attempt.
    """

    def key(generation: int) -> str:
        payload: dict[str, object] = {"generation": generation}
        payload["basis" if kind == SYNTHESIZE else "request_id"] = "FREQ-x"
        return Allocation(kind=kind, reason="r", payload=payload).dedup_key

    assert key(0) == key(0)
    assert key(0) != key(1) != key(2)
    if kind == SYNTHESIZE:
        through_plan = [
            _kinds(_plan(synthesis_basis=("FREQ-x", g)), kind)[0].dedup_key
            for g in (0, 1)
        ]
    else:
        argument = "open_requests" if kind == FOLLOW_UP else "literature_requests"
        through_plan = [
            _kinds(_plan(**{argument: [("FREQ-x", g, "RESULT")]}), kind)[0].dedup_key
            for g in (0, 1)
        ]
    assert through_plan == [key(0), key(1)]


# ==================================================================== tick
def _barren(runtime_db: Database, project: str) -> None:
    """Enough successful explorer runs after the newest idea to be barren."""

    with runtime_db.tx() as conn:
        for index in range(CONFIG.bounds.max_barren_explorations):
            conn.execute(
                """
                insert into work_items
                       (work_id, project_id, kind, payload, status, dedup_key)
                values (%(work_id)s, %(project_id)s, 'portfolio_explore',
                        '{}'::jsonb, 'SUCCEEDED', %(work_id)s)
                """,
                {"work_id": f"WORK-barren-{index}", "project_id": project},
            )


@pytest.mark.parametrize("kind", [RequestKind.FOLLOW_UP, RequestKind.LITERATURE])
def test_an_eligible_request_holds_off_the_barren_pause(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
    kind: RequestKind,
) -> None:
    """FR19. An open request is information the explorers did not have.

    Exactly as a seed is: pausing as "no frontier" while a question the
    portfolio can buy is waiting would stop the one route that is not the
    explorers.
    """

    idea, version = seed_idea(portfolio, runtime_project)
    if kind is RequestKind.FOLLOW_UP:
        _request(portfolio, runtime_project, idea)
        bought = FOLLOW_UP
    else:
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
        litintel.ask(
            portfolio,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            version=version.version,
            question="What published work bears on this?",
            source_ref=f"test:{idea.idea_id}",
            wait=True,
        )
        bought = LITERATURE_REQUEST
    _barren(runtime_db, runtime_project)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.RUNNING, report.notes
    assert [item.kind for item in report.allocations if item.kind == bought] == [bought]


def test_a_deferred_request_does_not_hold_off_the_barren_pause(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """FR19's control. A request the tick may not buy now is not a frontier.

    Its lineage holds its ceiling of live ideas, so ``_servable_requests``
    skips it; counting it anyway would keep a barren portfolio RUNNING on a
    question it cannot act on.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    for index in range(CONFIG.bounds.max_active_per_lineage - 1):
        portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.BRANCH,
            fields=idea_fields(
                title=f"sibling {index}", research_question=f"s{index}?"
            ),
            parent_idea_id=parent.idea_id,
            origin_role="brancher",
        )
    _request(portfolio, runtime_project, parent)
    _barren(runtime_db, runtime_project)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_NO_FRONTIER


def _literature_items(runtime_db: Database, request_id: str) -> list[dict[str, Any]]:
    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select dedup_key, status from work_items "
            "where kind = %s and payload->>'request_id' = %s order by created_at",
            (LITERATURE_REQUEST, request_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _fail_literature_items(runtime_db: Database) -> None:
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set status = 'FAILED' "
            "where kind = %s and status in ('PENDING', 'LEASED', 'WAITING')",
            (LITERATURE_REQUEST,),
        )


def test_a_failing_literature_request_is_rebought_then_declined_and_releases(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The generation, the ceiling and the release, all read from the queue.

    A work item that failed -- a provider outage, a crash, anything the
    handler did not record -- must not spend the request's key: the next
    tick buys it again under the next generation. After
    ``max_stage_failures`` finished-and-failed items the request is declined,
    the idea blocked on it returns to IDLE (a declined question is not a
    finding about the idea), and the next open request is served instead of
    the head of the queue holding it forever.
    """

    first_idea, first = _ask(portfolio, runtime_project, prefix="first")
    second_idea, second = _ask(portfolio, runtime_project, prefix="second")
    ceiling = CONFIG.bounds.max_stage_failures

    for generation in range(ceiling):
        report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
        bought = _kinds(report.allocations, LITERATURE_REQUEST)
        assert [item.payload["request_id"] for item in bought] == [first.request_id]
        assert bought[0].payload["generation"] == generation
        items = _literature_items(runtime_db, first.request_id)
        assert len(items) == generation + 1, "the re-buy was refused by the queue"
        assert items[-1]["status"] == "PENDING"
        assert items[-1]["dedup_key"].endswith(f":{generation}")
        _fail_literature_items(runtime_db)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    declined = portfolio.require_request(first.request_id)
    assert declined.state is RequestState.DECLINED
    assert str(ceiling) in (declined.resolution or "")
    assert (
        portfolio.require_idea(first_idea.idea_id).operational_state
        is OperationalState.IDLE
    ), "the idea waiting on a declined question is released"
    assert (
        portfolio.require_idea(second_idea.idea_id).operational_state
        is OperationalState.BLOCKED_DEPENDENCY
    )
    bought = _kinds(report.allocations, LITERATURE_REQUEST)
    assert [item.payload["request_id"] for item in bought] == [second.request_id]
    assert len(_literature_items(runtime_db, second.request_id)) == 1
    assert len(_literature_items(runtime_db, first.request_id)) == ceiling
