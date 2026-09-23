"""Literature intelligence: the published record as a cited, persistent resource.

Both directions the mission names, through production code and a fixture
corpus (no network, no credentials):

- an idea asks the literature a precise question; retrieval runs, a reader
  answers from the packet alone, every citation and quotation is verified,
  and what survives becomes literature claims and the idea's evidence;
- a verified gap or disagreement becomes a frontier request, and that request
  becomes a new idea whose provenance names the claim -- and the literature
  explorer, blind to the bank, proposes directions from such claims alone.

And the property the table exists for: a statement about the literature that
does not rest on retrieved sources is not stored, by the reader's verifier
and again by the database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_os.portfolio import frontier, litintel, runner
from research_os.portfolio.allocation import LITERATURE_REQUEST
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaStatus,
    LiteratureClaimKind,
    OperationalState,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    RequestState,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

WORKS = {
    "openalex:W10": (
        "Safe screening for the Lasso",
        (
            "We prove that screening rules discard inactive features without "
            "loss when the dual violation ordering is monotone."
        ),
    ),
    "openalex:W11": (
        "Working sets for sparse regression",
        (
            "Working-set methods may add several coordinates per iteration and "
            "reach the same optimum by a different support path."
        ),
    ),
    "openalex:W12": (
        "Column generation for conic programs",
        "Pricing selects one column per iteration by the most negative reduced cost.",
    ),
}


class FixtureCorpus:
    """A literature source over three fixed works, recording what it was asked."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 12) -> Any:
        self.queries.append(query)
        entries = tuple(
            SimpleNamespace(
                work=SimpleNamespace(key=key, title=title, abstract=abstract),
                excerpt="",
            )
            for key, (title, abstract) in list(WORKS.items())[:limit]
        )
        return SimpleNamespace(
            query=query, entries=entries, work_keys=tuple(e.work.key for e in entries)
        )


class FakeRetriever:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str) -> dict[str, Any]:
        self.queries.append(query)
        return {"ingested": 3}


ANSWER = {
    "answer": "Screening and working-set methods differ in their support paths.",
    "claims": [
        {
            "kind": "FINDING",
            "statement": "Working-set methods can reach the same optimum by a different support path.",
            "work_keys": ["openalex:W11"],
            "excerpt": "reach the same optimum by a different support path",
            "relation_to_idea": "SUPPORTS",
        },
        {
            "kind": "METHOD",
            "statement": "Column generation prices one column per iteration.",
            "work_keys": ["openalex:W12"],
            "relation_to_idea": "CONSISTENT_WITH",
        },
    ],
    "disagreements": [],
    "gaps": [
        {
            "statement": "No retrieved work measures support paths when the ordering is not monotone.",
            "work_keys": ["openalex:W10", "openalex:W11"],
        }
    ],
}


def _context(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    router: Any,
) -> Any:
    runtime = RuntimeStore(runtime_db)
    return frontier.FrontierContext(
        config=load_config(),
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp / "artifacts", store=runtime),
        project_id=project,
        run_id=runtime.create_run(project_id=project, objective="lit").run_id,
    )


def _ask(portfolio: PortfolioStore, project: str) -> tuple[Any, Any]:
    idea, version = seed_idea(portfolio, project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    request = litintel.ask(
        portfolio,
        project_id=project,
        idea_id=idea.idea_id,
        version=version.version,
        question="Do screening and working-set methods share a support path?",
        source_ref=f"test:{idea.idea_id}",
        wait=True,
    )
    return idea, request


# =============================================== idea -> verified evidence --
def test_an_idea_asks_and_gets_verified_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, request = _ask(portfolio, runtime_project)
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.BLOCKED_DEPENDENCY
    ), "an idea waiting on its question is an operational wait, not a verdict"
    corpus, retriever = FixtureCorpus(), FakeRetriever()
    router = ScriptedRouter(
        answers={"literature_reader": ANSWER}, store=RuntimeStore(runtime_db)
    )

    result = litintel.answer_request(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=corpus,
        retriever=retriever,
    )

    assert result.ok, result.detail
    assert retriever.queries == [request.question], "discovery runs before reading"
    assert corpus.queries == [request.question]
    claims = portfolio.list_literature_claims(project_id=runtime_project)
    assert {item.kind for item in claims} == {
        LiteratureClaimKind.FINDING,
        LiteratureClaimKind.METHOD,
        LiteratureClaimKind.GAP,
    }
    quoted = next(item for item in claims if item.kind is LiteratureClaimKind.FINDING)
    assert quoted.verification.name == "QUOTED" and quoted.work_keys == (
        "openalex:W11",
    )
    assert all(item.work_keys for item in claims)
    evidence = portfolio.list_evidence(idea_id=idea.idea_id)
    assert {(item.kind, item.strength, item.literature_key) for item in evidence} == {
        (EvidenceKind.LITERATURE, EvidenceStrength.SUPPORTS, "openalex:W11"),
        (EvidenceKind.LITERATURE, EvidenceStrength.CONSISTENT_WITH, "openalex:W12"),
    }
    assert all(item.summary.startswith("claim PLCL-") for item in evidence)
    assert portfolio.require_request(request.request_id).state is RequestState.CONSUMED
    assert (
        portfolio.require_idea(idea.idea_id).operational_state is OperationalState.IDLE
    )


def test_a_reading_that_cites_an_unretrieved_work_stores_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, request = _ask(portfolio, runtime_project)
    forged = {
        **ANSWER,
        "claims": [
            {**ANSWER["claims"][0]},
            {
                "kind": "FINDING",
                "statement": "A paper nobody retrieved settles this.",
                "work_keys": ["openalex:W999"],
                "relation_to_idea": "CONTRADICTS",
            },
        ],
    }
    router = ScriptedRouter(
        answers={"literature_reader": forged}, store=RuntimeStore(runtime_db)
    )
    result = litintel.answer_request(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=FixtureCorpus(),
    )
    assert not result.ok
    assert result.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "openalex:W999" in result.detail
    assert portfolio.list_literature_claims(project_id=runtime_project) == ()
    assert portfolio.list_evidence(idea_id=idea.idea_id) == (), (
        "fail-closed: the grounded claim is not kept beside the invented one"
    )


def test_a_quotation_that_is_not_in_the_source_stores_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    _idea, request = _ask(portfolio, runtime_project)
    misquoted = {
        **ANSWER,
        "claims": [
            {
                **ANSWER["claims"][0],
                "excerpt": "working sets are strictly faster in all regimes",
            }
        ],
    }
    router = ScriptedRouter(
        answers={"literature_reader": misquoted}, store=RuntimeStore(runtime_db)
    )
    result = litintel.answer_request(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=FixtureCorpus(),
    )
    assert not result.ok and "quotes" in result.detail
    assert portfolio.list_literature_claims(project_id=runtime_project) == ()


def test_the_database_refuses_an_unsourced_or_edited_claim(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    with pytest.raises(RuntimeDatabaseError):
        portfolio.record_literature_claim(
            project_id=runtime_project,
            kind="FINDING",
            statement="recollection, not retrieval",
            work_keys=(),
            excerpt="",
            verification="CITED",
            query="q",
            digest="pclaim-v1:none",
        )
    claim = portfolio.record_literature_claim(
        project_id=runtime_project,
        kind="FINDING",
        statement="a sourced statement",
        work_keys=("openalex:W10",),
        excerpt="",
        verification="CITED",
        query="q",
        digest="pclaim-v1:x",
    )
    with (
        pytest.raises(RuntimeDatabaseError, match="immutable"),
        runtime_db.tx() as conn,
    ):
        conn.execute(
            "update literature_claims set statement = 'reworded' where claim_id = %s",
            (claim.claim_id,),
        )


def test_no_literature_source_is_a_reported_capability_not_an_answer(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, request = _ask(portfolio, runtime_project)
    router = ScriptedRouter(answers={}, store=RuntimeStore(runtime_db))
    result = litintel.answer_request(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=None,
    )
    assert result.ok and "no literature source" in result.detail
    assert router.requests == [], "nothing is asked of a model with no sources to read"
    assert (
        portfolio.require_idea(idea.idea_id).operational_state is OperationalState.IDLE
    )


# ============================================ verified gap -> new idea ----
def test_a_verified_gap_becomes_a_new_idea_that_cites_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, request = _ask(portfolio, runtime_project)
    child = {
        "title": "Support paths under non-monotone orderings",
        "research_question": "Do support paths diverge when the ordering is not monotone?",
        "core_idea": "The literature measures only the monotone case.",
        "falsifier": "Search for a published measurement of the non-monotone case.",
    }
    router = ScriptedRouter(
        answers={
            "literature_reader": ANSWER,
            "follow_up_explorer": {"children": [child], "relations": ["SPECIALIZES"]},
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(portfolio, runtime_db, tmp_path, runtime_project, router)
    answered = litintel.answer_request(
        context, request.request_id, literature=FixtureCorpus()
    )
    (raised,) = answered.raised
    gap = portfolio.require_request(raised)
    assert gap.kind is RequestKind.FOLLOW_UP and gap.basis is RequestBasis.LITERATURE
    claim = portfolio.get_literature_claim(gap.source_ref)
    assert claim is not None and claim.kind is LiteratureClaimKind.GAP

    followed = frontier.run_follow_up(context, raised)
    assert followed.ok, followed.detail
    (created,) = followed.created
    new = portfolio.require_idea(created)
    assert new.origin is IdeaOrigin.FOLLOW_UP and new.depth == idea.depth + 1
    (reason,) = portfolio.provenance_of(created)
    assert reason.basis is ProvenanceBasis.LITERATURE
    assert reason.source_ref == claim.claim_id


def test_the_literature_explorer_proposes_from_claims_alone(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Literature-driven discovery with no parent idea and no view of the bank."""

    seed_idea(portfolio, runtime_project, title="BANKTEXT an existing idea")
    claim = portfolio.record_literature_claim(
        project_id=runtime_project,
        kind="DISAGREEMENT",
        statement="Two works disagree on whether screening preserves the support path.",
        work_keys=("openalex:W10", "openalex:W11"),
        excerpt="",
        verification="CITED",
        query="support path",
        digest="pclaim-v1:disagreement",
    )
    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(
        answers={
            "literature_explorer": {
                "candidates": [
                    {
                        "title": "Resolve the screening disagreement",
                        "research_question": "Does screening preserve the support path?",
                        "core_idea": "The two works assume different orderings.",
                        "falsifier": "Find a published analysis of both orderings.",
                        "derived_from": [claim.claim_id],
                    }
                ]
            }
        },
        store=runtime,
    )
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        "literature_explorer",
    )
    assert outcome.ok, outcome.detail
    (prompt,) = [
        r.prompt for r in router.requests if str(r.role) == "literature_explorer"
    ]
    assert claim.claim_id in prompt and "BANKTEXT" not in prompt
    (created,) = outcome.data["created"]
    idea = portfolio.require_idea(created)
    assert idea.origin is IdeaOrigin.LITERATURE_EXPLORER and idea.depth == 0
    (reason,) = portfolio.provenance_of(created)
    assert reason.basis is ProvenanceBasis.LITERATURE
    assert reason.source_ref == claim.claim_id


# ================================================ scheduling & the tick ---
def test_the_tick_buys_an_answer_for_an_open_literature_request(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    _idea, request = _ask(portfolio, runtime_project)
    report = tick(
        db=runtime_db,
        project_id=runtime_project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
    )
    bought = [item for item in report.allocations if item.kind == LITERATURE_REQUEST]
    assert bought and bought[0].payload["request_id"] == request.request_id


def test_a_watch_is_scheduled_through_the_existing_machinery_and_is_idempotent(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    schedule = litintel.ensure_watch(
        runtime_db, project_id=runtime_project, interval_seconds=86_400
    )
    assert schedule == litintel.ensure_watch(
        runtime_db, project_id=runtime_project, interval_seconds=86_400
    )
    first = litintel.watch(portfolio, project_id=runtime_project, bucket="20260923")
    again = litintel.watch(portfolio, project_id=runtime_project, bucket="20260923")
    assert first == again and len(first) == 1
    request = portfolio.require_request(first[0])
    assert (
        request.kind is RequestKind.LITERATURE
        and request.source_idea_id == idea.idea_id
    )
