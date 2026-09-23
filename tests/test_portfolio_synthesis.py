"""The writer and the referee: from reviewed evidence, and back to the frontier.

Held here:

- the writer is given rows with identifiers and nothing else, and a draft is
  stored only if every citation was supplied, every finding cites evidence
  that bears on it, every novelty claim cites the literature and every number
  appears in what it cites -- each refusal tested by the draft that would
  have slipped through;
- a gap the writer names becomes a frontier request;
- the referee's findings become frontier requests (and missing literature a
  literature request), a follow-up explorer turns one into a new idea, and
  the referee approves nothing: no idea status moves and no Review exists;
- the tick buys a synthesis when the reviewed evidence changes, once per basis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import frontier, synthesis
from research_os.portfolio.allocation import SYNTHESIZE
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    SynthesisState,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def _record(
    portfolio: PortfolioStore, runtime_db: Database, tmp: Path, project: str
) -> dict[str, Any]:
    artifacts = FilesystemArtifactStore(
        tmp / "artifacts", store=RuntimeStore(runtime_db)
    )
    ref = artifacts.put_text("analysis", role="test", producer="test")
    idea, _ = seed_idea(portfolio, project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    measured = portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.INSPECTION,
        strength=EvidenceStrength.SUPPORTS,
        summary="interaction = 0.5 over 6 cells; prespecified support > 0.25",
        artifact_id=ref.artifact_id,
    )
    consistent = portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.CONSISTENT_WITH,
        summary="claim: working sets reach the optimum by another path",
        literature_key="openalex:W11",
    )
    claim = portfolio.record_literature_claim(
        project_id=project,
        kind="FINDING",
        statement="Working-set methods reach the same optimum by a different path.",
        work_keys=("openalex:W11",),
        excerpt="",
        verification="CITED",
        query="q",
        digest="pclaim-v1:w11",
        idea_id=idea.idea_id,
    )
    return {
        "idea": idea,
        "measured": measured,
        "consistent": consistent,
        "claim": claim,
    }


def _draft(record: dict[str, Any], **override: Any) -> dict[str, Any]:
    statements = [
        {
            "statement_id": "S1",
            "kind": "FINDING",
            "text": "The size-by-difficulty interaction was 0.5, above the prespecified bar.",
            "cites": [record["measured"].evidence_id],
            "ideas": [record["idea"].idea_id],
        },
        {
            "statement_id": "S2",
            "kind": "NOVELTY",
            "text": "Prior work compares optima, not support paths.",
            "cites": [record["claim"].claim_id],
            "ideas": [record["idea"].idea_id],
        },
        {
            "statement_id": "S3",
            "kind": "LIMITATION",
            "text": "One model family reviewed this; that is not independent review.",
            "cites": [],
            "ideas": [record["idea"].idea_id],
        },
    ]
    payload: dict[str, Any] = {
        "title": "What the reviewed evidence establishes",
        "statements": statements,
        "evidence_requests": [
            {
                "idea_id": record["idea"].idea_id,
                "kind": "measurement",
                "question": "Does the interaction survive a second instance family?",
                "reason": "only one family was measured",
            }
        ],
    }
    payload.update(override)
    return payload


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
        run_id=runtime.create_run(project_id=project, objective="synth").run_id,
    )


REFEREE = {
    "verdict": "MAJOR_REVISION",
    "summary": "the finding rests on one instance family",
    "findings": [
        {
            "finding_id": "F1",
            "kind": "MISSING_CONTROL",
            "severity": "MAJOR",
            "statement_ids": ["S1"],
            "summary": "no control for instance generator bias",
            "follow_up_question": "Is the interaction an artefact of the instance generator?",
        },
        {
            "finding_id": "F2",
            "kind": "MISSING_LITERATURE",
            "severity": "MINOR",
            "statement_ids": ["S2"],
            "summary": "screening-rule literature was not consulted",
        },
    ],
}


def test_a_grounded_draft_is_written_refereed_and_sent_back_to_the_frontier(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    record = _record(portfolio, runtime_db, tmp_path, runtime_project)
    child = {
        "title": "Instance generator bias",
        "research_question": "Is the interaction an artefact of the instance generator?",
        "core_idea": "One generator may couple size and difficulty.",
        "falsifier": "Measure the interaction under a second generator.",
    }
    router = ScriptedRouter(
        answers={
            "synthesizer": _draft(record),
            "referee": REFEREE,
            "follow_up_explorer": {"children": [child], "relations": ["DERIVED_FROM"]},
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(portfolio, runtime_db, tmp_path, runtime_project, router)
    before = portfolio.require_idea(record["idea"].idea_id)

    result = synthesis.synthesize(context)

    assert result.ok, result.detail
    (made,) = portfolio.list_syntheses(project_id=runtime_project)
    assert (
        made.state is SynthesisState.REFEREED
        and made.referee_verdict == "MAJOR_REVISION"
    )
    requests = {
        item.source_ref: item
        for item in portfolio.list_requests(project_id=runtime_project)
    }
    gap = requests[f"{made.synthesis_id}:gap:0"]
    assert gap.basis is RequestBasis.EVIDENCE_GAP and gap.kind is RequestKind.FOLLOW_UP
    control = requests[f"{made.synthesis_id}:F1"]
    assert control.basis is RequestBasis.REFEREE_FINDING
    assert control.source_idea_id == record["idea"].idea_id
    literature = requests[f"{made.synthesis_id}:F2"]
    assert literature.kind is RequestKind.LITERATURE

    # The referee approved nothing.
    after = portfolio.require_idea(record["idea"].idea_id)
    assert (after.status, after.current_version) == (
        before.status,
        before.current_version,
    )
    with runtime_db.tx() as conn:
        reviews = conn.execute("select count(*) as n from idea_reviews").fetchone()
    assert reviews["n"] == 0

    # And its finding returns to the frontier as a new idea.
    followed = frontier.run_follow_up(context, control.request_id)
    (created,) = followed.created
    assert portfolio.require_idea(created).depth == 1
    assert portfolio.provenance_of(created)[0].basis is ProvenanceBasis.REFEREE_FINDING


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda s, r: s[0].update(cites=["IEVD-20260101T000000Z-00000000"]),
            "not supplied",
        ),
        (lambda s, r: s[0].update(cites=[r["claim"].claim_id]), "cites no evidence"),
        (
            lambda s, r: s[0].update(cites=[r["consistent"].evidence_id]),
            "cites no evidence",
        ),
        (
            lambda s, r: s[1].update(cites=[r["measured"].evidence_id]),
            "cites no literature",
        ),
        (
            lambda s, r: s[0].update(text="The interaction was 0.73 across 42 cells."),
            "appear in nothing",
        ),
    ],
    ids=[
        "invented",
        "claim-as-finding",
        "consistent-as-finding",
        "novelty-no-lit",
        "number",
    ],
)
def test_an_ungrounded_draft_is_refused_whole(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    mutate: Any,
    reason: str,
) -> None:
    record = _record(portfolio, runtime_db, tmp_path, runtime_project)
    draft = _draft(record)
    mutate(draft["statements"], record)
    router = ScriptedRouter(
        answers={"synthesizer": draft}, store=RuntimeStore(runtime_db)
    )
    result = synthesis.synthesize(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router)
    )
    assert not result.ok
    assert result.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert reason in result.detail
    assert portfolio.list_syntheses(project_id=runtime_project) == ()
    assert portfolio.list_requests(project_id=runtime_project) == (), (
        "a refused draft raises none of its requests"
    )


def test_nothing_is_written_from_unreviewed_work(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    seed_idea(portfolio, runtime_project)  # a CANDIDATE: not reviewed evidence
    router = ScriptedRouter(answers={}, store=RuntimeStore(runtime_db))
    result = synthesis.synthesize(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router)
    )
    assert result.ok and "no reviewed evidence" in result.detail
    assert router.requests == []


def test_the_tick_buys_one_synthesis_per_basis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    record = _record(portfolio, runtime_db, tmp_path, runtime_project)

    def bought() -> list[Any]:
        report = tick(
            db=runtime_db,
            project_id=runtime_project,
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
        )
        return [item for item in report.allocations if item.kind == SYNTHESIZE]

    first = bought()
    assert len(first) == 1
    router = ScriptedRouter(
        answers={"synthesizer": _draft(record), "referee": REFEREE},
        store=RuntimeStore(runtime_db),
    )
    synthesis.synthesize(
        _context(portfolio, runtime_db, tmp_path, runtime_project, router)
    )
    with runtime_db.tx() as conn:
        conn.execute("update work_items set status = 'SUCCEEDED'")
    assert bought() == [], "an unchanged, refereed basis is not synthesised again"
