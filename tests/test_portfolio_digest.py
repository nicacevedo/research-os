"""The digest: the one artifact a returning researcher reads instead of the rest.

Two things are being protected. That it is *deterministic* -- no model writes a
word of it, and the same state renders the same text -- and that it does not
mislead: the tiers it reports carry what they rest on, and the ideas it selects
are a Pareto front with a diversity constraint rather than an ordering by the
scheduling utility.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.digest import (
    MAX_TOP_IDEAS,
    diversify,
    pareto_front,
    produce,
    render,
)
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaStatus,
    QualityDimensions,
    ReviewerRole,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def _produce(runtime_db: Database, project: str, **kwargs):
    return produce(db=runtime_db, project_id=project, config=load_config(), **kwargs)


def test_a_digest_reports_what_happened_and_what_it_cost(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    for index in range(3):
        seed_idea(portfolio, runtime_project, title=f"idea {index}")
    killed, _ = seed_idea(portfolio, runtime_project, title="killed")
    portfolio.set_status(
        idea_id=killed.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="a 2013 theorem already states it",
    )

    record = _produce(runtime_db, runtime_project)
    counts = record.payload["counts"]
    assert counts["ideas_total"] == 4
    assert counts["rejected"] == 1
    assert counts["candidate"] == 3
    assert "spend_usd" in counts

    negatives = record.payload["sections"]["negative_results"]
    assert any("2013 theorem" in item for item in negatives), (
        "what was ruled out, and why, is half of what a portfolio produces"
    )


def test_the_same_state_renders_the_same_digest(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Determinism, which is what makes "no model wrote this" checkable."""

    seed_idea(portfolio, runtime_project)
    moment = datetime.now(UTC)
    start = moment - timedelta(days=1)
    # The same *period* as well as the same state: a second digest taken from
    # where the first ended legitimately covers different time, and that
    # difference is not nondeterminism.
    first = _produce(runtime_db, runtime_project, since=start, now=moment)
    second = _produce(runtime_db, runtime_project, since=start, now=moment)
    assert render(first.payload).replace(first.digest_id, "X") == render(
        second.payload
    ).replace(second.digest_id, "X")


def test_a_demotion_is_reported_rather_than_forgotten(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """ "Ideas demoted" needs a high-water mark, which is what `quality_tier` is.

    Without it, an idea rejected after reaching PROMISING and one rejected as
    a candidate are the same row, and the more interesting of the two
    disappears.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="the falsifier found a counterexample",
    )
    record = _produce(runtime_db, runtime_project)
    assert record.payload["counts"]["demoted_after_reaching_a_tier"] == 1
    assert any(
        "reached PROMISING" in item for item in record.payload["sections"]["demoted"]
    )


def test_every_top_idea_carries_whether_its_reviews_were_independent(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Repeated per idea, not stated once at the top.

    A reader who skips to one entry must not miss it, and on a one-provider
    machine the honest answer is that the three reviews came from one model.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    for role in (
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    ):
        record_review(
            portfolio,
            idea_id=idea.idea_id,
            version=1,
            role=role,
            provider="claude",
            family="anthropic",
            model="opus",
        )
    record = _produce(runtime_db, runtime_project)
    entries = record.payload["top_ideas"]
    assert entries
    assert entries[0]["board_independence"] == 1
    assert "not independent review" in entries[0]["independence_note"]
    assert "not independent review" in render(record.payload)


def test_a_top_idea_reports_its_strongest_objection(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    review = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=__import__(
            "research_os.portfolio.models", fromlist=["ReviewVerdict"]
        ).ReviewVerdict.PASS_WITH_OBJECTIONS,
        severity=__import__(
            "research_os.portfolio.models", fromlist=["Severity"]
        ).Severity.MAJOR,
        summary="the baseline is not tuned to the same budget",
    )
    portfolio.raise_objection(
        idea_id=idea.idea_id,
        review_id=review.review_id,
        raised_at_version=1,
        severity=__import__(
            "research_os.portfolio.models", fromlist=["Severity"]
        ).Severity.MAJOR,
        summary="the baseline is not tuned to the same budget",
    )
    record = _produce(runtime_db, runtime_project)
    entry = record.payload["top_ideas"][0]
    assert "not tuned" in entry["strongest_objection"]
    assert "[MAJOR]" in entry["strongest_objection"]


def test_the_top_ideas_are_a_front_and_not_an_ordering(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """§8: do not implement one opaque idea score.

    The idea that is best on novelty and worst on literature confidence, and
    the one that is the reverse, are both on the front. An ordering by any
    single number would drop one of them.
    """

    high_novelty, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(title="novel, thinly searched"),
        origin_role="blind_explorer",
        dimensions=QualityDimensions(novelty=0.95, literature_confidence=0.2),
    )
    well_searched, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            title="thoroughly searched, less novel",
            research_question="A completely different question about amplifiers.",
            core_idea="Substrate conduction dominates radiative loading.",
        ),
        origin_role="blind_explorer",
        dimensions=QualityDimensions(novelty=0.2, literature_confidence=0.95),
    )
    dominated, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            title="worse at everything",
            research_question="A third, unrelated question about scheduling.",
            core_idea="Queue depth predicts tail latency.",
        ),
        origin_role="blind_explorer",
        dimensions=QualityDimensions(novelty=0.1, literature_confidence=0.1),
    )
    for idea in (high_novelty, well_searched, dominated):
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    record = _produce(runtime_db, runtime_project)
    chosen = {entry["idea_id"] for entry in record.payload["top_ideas"]}
    assert high_novelty.idea_id in chosen
    assert well_searched.idea_id in chosen
    assert dominated.idea_id not in chosen


def test_an_idea_cannot_reach_the_top_by_scoring_itself(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The front ranks only what something deterministic wrote.

    An explorer returning 1.0 on every dimension it is allowed to report must
    not thereby dominate an idea with actual evidence. Four of the nine
    dimensions come only from model output, and an earlier version ranked on
    all four -- so the cheapest way to the top of "worth your attention" was
    confidence.
    """

    boastful, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(title="scores itself perfectly"),
        origin_role="blind_explorer",
        dimensions=QualityDimensions(
            potential_impact=1.0,
            plausibility=1.0,
            falsifiability=1.0,
            tractability=1.0,
            reproducibility=1.0,
            reviewer_confidence=1.0,
            evidence_strength=1.0,
        ),
    )
    grounded, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            title="has actual evidence",
            research_question="A different question about amplifiers.",
            core_idea="Substrate conduction dominates radiative loading.",
        ),
        origin_role="blind_explorer",
        dimensions=QualityDimensions(),
    )
    for index in range(4):
        portfolio.add_evidence(
            idea_id=grounded.idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.SUPPORTS,
            summary=f"retrieved source {index}",
            literature_key=f"openalex:W{index}",
        )
    for idea in (boastful, grounded):
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)

    record = _produce(runtime_db, runtime_project)
    chosen = {entry["idea_id"] for entry in record.payload["top_ideas"]}
    assert grounded.idea_id in chosen
    # The boastful one is not excluded -- nothing here judges it -- but it must
    # not have *dominated* the grounded one, which is what would have happened
    # when the front read four dimensions only it had filled in.
    assert not _dominates_by_self_report(record, boastful.idea_id, grounded.idea_id)


def _dominates_by_self_report(record, winner: str, loser: str) -> bool:
    entries = [item["idea_id"] for item in record.payload["top_ideas"]]
    return winner in entries and loser not in entries


def test_the_front_is_thinned_by_diversity(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Eight versions of one idea is not eight ideas.

    A researcher reading the digest should not find their attention spent on
    one lineage restated.
    """

    root, _ = seed_idea(portfolio, runtime_project, title="root")
    portfolio.set_status(idea_id=root.idea_id, status=IdeaStatus.PROMISING)
    for index in range(6):
        child, _ = portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.BRANCH,
            fields=idea_fields(title=f"child {index}"),
            parent_idea_id=root.idea_id,
            origin_role="brancher",
            dimensions=QualityDimensions(novelty=0.9, potential_impact=0.9),
        )
        portfolio.set_status(idea_id=child.idea_id, status=IdeaStatus.PROMISING)

    record = _produce(runtime_db, runtime_project)
    shown = record.payload["top_ideas"]
    # Both bounds: thinning must remove some and must not remove everything.
    assert shown, "thinning removed the whole family"
    assert len(shown) < 7


def test_the_digest_body_is_assembled_from_fields(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Every line traceable to a column, which is the whole claim.

    The question and the falsifier below are the exact strings stored on the
    version; if a model had written the prose, they would not appear verbatim.
    """

    idea, version = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    record = _produce(runtime_db, runtime_project)
    text = render(record.payload)
    assert version.research_question in text
    assert version.falsifier in text
    assert "No human has evaluated any of it" in text


def test_a_digest_with_nothing_to_report_says_so(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    portfolio.upsert_state(project_id=runtime_project)
    record = _produce(runtime_db, runtime_project)
    assert record.payload["top_ideas"] == []
    assert "Nothing has reached a tier worth surfacing yet." in render(record.payload)


def test_the_period_starts_where_the_last_digest_ended(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    portfolio.upsert_state(project_id=runtime_project)
    first = _produce(runtime_db, runtime_project, now=datetime.now(UTC))
    second = _produce(
        runtime_db, runtime_project, now=datetime.now(UTC) + timedelta(hours=1)
    )
    assert second.period_start >= first.period_end - timedelta(seconds=1)


@pytest.mark.parametrize("count", [0, 1, 12])
def test_diversify_is_bounded_and_deterministic(count: int) -> None:
    from research_os.portfolio.allocation import DiversityKey

    entries = [
        (
            f"idea-{index}",
            None,
            DiversityKey(
                lineage_root=f"root-{index}",
                adjudication=("mathematical",),
                subproblem=frozenset({f"token{index}"}),
            ),
        )
        for index in range(count)
    ]
    kept = diversify(entries, limit=MAX_TOP_IDEAS)  # type: ignore[arg-type]
    assert len(kept) == min(count, MAX_TOP_IDEAS), (
        "every entry here occupies a distinct coordinate, so nothing should be "
        "thinned except by the limit"
    )
    assert kept == diversify(entries, limit=MAX_TOP_IDEAS)  # type: ignore[arg-type]


def test_an_empty_front_is_expressible() -> None:
    assert pareto_front([]) == []


def test_unassessed_dimensions_do_not_rank_an_idea_below_a_measured_one() -> None:
    """A candidate nobody has assessed must not be dominated for that alone.

    Otherwise the front is always the ideas that have been worked on, and a new
    direction can never appear in it -- which is the state every new direction
    starts in.
    """

    from research_os.portfolio.models import PortfolioIdea

    def _idea(name: str) -> PortfolioIdea:
        now = datetime.now(UTC)
        return PortfolioIdea(
            idea_id=name,
            project_id="p",
            depth=0,
            lineage_root=name,
            origin=IdeaOrigin.BLIND_EXPLORER,
            current_version=1,
            status=IdeaStatus.CANDIDATE,
            operational_state=__import__(
                "research_os.portfolio.models", fromlist=["OperationalState"]
            ).OperationalState.IDLE,
            quality_tier=__import__(
                "research_os.portfolio.models", fromlist=["QualityTier"]
            ).QualityTier.NONE,
            created_at=now,
            updated_at=now,
        )

    unassessed = (_idea("PIDEA-a"), QualityDimensions())
    middling = (_idea("PIDEA-b"), QualityDimensions(novelty=0.5, potential_impact=0.5))
    front = pareto_front([unassessed, middling])
    assert len(front) == 2


def test_evidence_appears_under_the_idea_it_supports(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="the closest retrieved work is different",
        literature_key="openalex:W1",
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    record = _produce(runtime_db, runtime_project)
    entry = record.payload["top_ideas"][0]
    assert any("closest retrieved work" in item for item in entry["evidence"])
