"""The Idea store, and the seven invariants the schema is supposed to hold.

Every test here targets one invariant from
`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` §3.4, and most of them assert that
*PostgreSQL* refuses something rather than that Python does. That is the point:
a rule enforced only in the layer that wants to break it is not enforced, and
the layer that wants to break these is the one generating the ideas.
"""

from __future__ import annotations

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio.models import (
    AdjudicationType,
    EdgeKind,
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    QualityTier,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.store import (
    ActiveTrackExistsError,
    DuplicateBasisError,
    PortfolioStateError,
    PortfolioStore,
)
from research_os.runtime.db import Database, RuntimeDatabaseError
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_helpers import (
    RUNTIME_TABLES,
    pg_dsn,
    runtime_db,
    runtime_project,
    runtime_xdg,
)

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


# ------------------------------------------------------- identity and versions --
def test_an_idea_gets_both_digests_and_a_lineage_root_of_itself(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, version = seed_idea(portfolio, runtime_project)

    assert idea.idea_id.startswith("PIDEA-")
    assert idea.depth == 0
    assert idea.lineage_root == idea.idea_id
    assert idea.status is IdeaStatus.CANDIDATE
    assert idea.quality_tier is QualityTier.NONE
    assert version.content_digest.startswith("pidea-content-v1:")
    assert version.canonical_digest.startswith("pidea-canonical-v1:")


def test_a_lifecycle_only_change_does_not_move_the_content_digest(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The rule the kernel applies to a Claim, applied to an Idea.

    Changing the next action or the assessed dimensions must not stale a
    review, or the system re-reviews itself forever and the staleness signal
    stops meaning anything.
    """

    idea, first = seed_idea(portfolio, runtime_project)
    second = portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(next_best_action="Something else entirely"),
        origin_role="scientific_discovery",
    )
    assert second.version == 2
    assert second.content_digest == first.content_digest
    assert second.next_best_action != first.next_best_action


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "research_question",
        "core_idea",
        "mechanism",
        "why_it_matters",
        "falsifier",
        "closest_prior_work",
        "claimed_difference",
    ],
)
def test_changing_any_material_field_moves_the_content_digest(
    portfolio: PortfolioStore, runtime_project: str, field: str
) -> None:
    idea, first = seed_idea(portfolio, runtime_project)
    changed = portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(**{field: "materially different text"}),
        origin_role="scientific_discovery",
    )
    assert changed.content_digest != first.content_digest, (
        f"{field} is scientifically material and a review of the old value must "
        f"not survive changing it"
    )


def test_a_closed_idea_takes_no_further_versions(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="subsumed by a known theorem",
    )
    with pytest.raises(PortfolioStateError, match="REJECTED"):
        portfolio.append_version(
            idea_id=idea.idea_id, fields=idea_fields(), origin_role="x"
        )


# -------------------------------------------------------------- staleness --
def test_a_material_revision_stales_every_review(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    for role in (
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    ):
        record_review(portfolio, idea_id=idea.idea_id, version=1, role=role)
    assert len(portfolio.live_reviews(idea_id=idea.idea_id)) == 3

    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(mechanism="an entirely different mechanism"),
        origin_role="scientific_discovery",
    )
    assert portfolio.live_reviews(idea_id=idea.idea_id) == ()


def test_swapping_the_evidence_under_a_review_stales_it(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The hole the kernel already closed for Claims, closed here.

    Without ``reviewed_evidence_digest`` the sequence below leaves three
    reviews reading as current beside evidence no reviewer ever saw -- which is
    a promotion on evidence nobody checked, reached without touching a single
    word of the idea.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.DERIVATION,
        strength=EvidenceStrength.SUPPORTS,
        summary="the two orderings differ on a 2x2 instance",
        artifact_id=None,
        finding_id=None,
        literature_key="derivation:v1",
    )
    record_review(
        portfolio, idea_id=idea.idea_id, version=1, role=ReviewerRole.METHODOLOGY
    )
    assert len(portfolio.live_reviews(idea_id=idea.idea_id)) == 1

    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.DERIVATION,
        strength=EvidenceStrength.SUPPORTS,
        summary="a second, different derivation the reviewer never read",
        literature_key="derivation:v2",
    )
    assert portfolio.live_reviews(idea_id=idea.idea_id) == (), (
        "a review must not stay live when the evidence beneath it changed"
    )


def test_a_review_by_a_superseded_prompt_is_not_live(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        prompt_version="skeptic_reviewer@1",
    )
    assert len(portfolio.live_reviews(idea_id=idea.idea_id)) == 1
    assert (
        portfolio.live_reviews(
            idea_id=idea.idea_id,
            current_prompt_versions={"skeptic_reviewer": "skeptic_reviewer@2"},
        )
        == ()
    )


def test_recording_the_same_review_twice_is_one_review(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    first = record_review(
        portfolio, idea_id=idea.idea_id, version=1, role=ReviewerRole.NOVELTY
    )
    second = record_review(
        portfolio, idea_id=idea.idea_id, version=1, role=ReviewerRole.NOVELTY
    )
    assert first.review_id == second.review_id
    assert len(portfolio.list_reviews(idea_id=idea.idea_id)) == 1


# ---------------------------------------------------------------- lineage --
def test_lineage_depth_increases_and_the_root_is_inherited(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    parent, _ = seed_idea(portfolio, runtime_project)
    child, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BRANCH,
        fields=idea_fields(title="A weaker assumption"),
        parent_idea_id=parent.idea_id,
        edge_kind=EdgeKind.SPECIALIZES,
        origin_role="branch",
    )
    assert child.depth == parent.depth + 1
    assert child.lineage_root == parent.lineage_root
    assert portfolio.ancestors(child.idea_id) == (parent.idea_id,)
    assert portfolio.descendants(parent.idea_id) == (child.idea_id,)


def test_the_database_refuses_a_lineage_cycle(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Acyclicity is a check constraint, not a convention.

    The store's own ``add_edge`` reads both depths from ``ideas``, so it cannot
    produce a cycle by construction. This goes underneath it and inserts the
    back edge directly, which is what a future caller with a different idea
    about depth would do.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    child, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BRANCH,
        fields=idea_fields(title="child"),
        parent_idea_id=parent.idea_id,
        origin_role="branch",
    )
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(
            """
                insert into idea_edges
                    (parent_idea_id, child_idea_id, kind, parent_depth, child_depth)
                select c.idea_id, p.idea_id, 'DERIVED_FROM', c.depth, p.depth
                  from ideas p, ideas c
                 where p.idea_id = %s and c.idea_id = %s
                """,
            (parent.idea_id, child.idea_id),
        )


def test_the_database_refuses_an_edge_whose_depth_is_a_lie(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The composite foreign key is what makes the depth check mean anything.

    A ``check`` can only see its own row, so a writer that invents depths would
    satisfy it. The ``(idea_id, depth)`` foreign keys make an invented depth a
    referential-integrity error instead.
    """

    parent, _ = seed_idea(portfolio, runtime_project)
    child, _ = seed_idea(portfolio, runtime_project, title="unrelated root")
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(
            """
                insert into idea_edges
                    (parent_idea_id, child_idea_id, kind, parent_depth, child_depth)
                values (%s, %s, 'DERIVED_FROM', 0, 7)
                """,
            (parent.idea_id, child.idea_id),
        )


def test_an_ideas_depth_cannot_be_updated_once_an_edge_depends_on_it(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """ "``ideas.depth`` has no UPDATE path" is enforced, not asserted."""

    parent, _ = seed_idea(portfolio, runtime_project)
    portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BRANCH,
        fields=idea_fields(title="child"),
        parent_idea_id=parent.idea_id,
        origin_role="branch",
    )
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute("update ideas set depth = 9 where idea_id = %s", (parent.idea_id,))


def test_an_idea_is_a_duplicate_of_at_most_one_survivor(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    survivor, _ = seed_idea(portfolio, runtime_project, title="survivor")
    other, _ = seed_idea(portfolio, runtime_project, title="other")
    duplicate, _ = seed_idea(portfolio, runtime_project, title="duplicate")
    portfolio.add_edge(
        parent_idea_id=survivor.idea_id,
        child_idea_id=duplicate.idea_id,
        kind=EdgeKind.DUPLICATE_OF,
    )
    with pytest.raises(RuntimeDatabaseError):
        portfolio.add_edge(
            parent_idea_id=other.idea_id,
            child_idea_id=duplicate.idea_id,
            kind=EdgeKind.DUPLICATE_OF,
        )
    assert portfolio.duplicate_survivor(duplicate.idea_id) == survivor.idea_id


def test_mutual_duplication_resolves_rather_than_looping(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    left, _ = seed_idea(portfolio, runtime_project, title="left")
    right, _ = seed_idea(portfolio, runtime_project, title="right")
    portfolio.add_edge(
        parent_idea_id=right.idea_id,
        child_idea_id=left.idea_id,
        kind=EdgeKind.DUPLICATE_OF,
    )
    portfolio.add_edge(
        parent_idea_id=left.idea_id,
        child_idea_id=right.idea_id,
        kind=EdgeKind.DUPLICATE_OF,
    )
    assert portfolio.duplicate_survivor(left.idea_id) in {left.idea_id, right.idea_id}


# --------------------------------------------------------------- evidence --
def test_a_numerical_witness_cannot_be_recorded_as_proof(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """ "A numerical witness is not a proof" is a check constraint.

    This is the one place a MATHEMATICAL idea could reach VALIDATED on
    arithmetic, so the refusal is the database's rather than a reviewer's.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.NUMERICAL,
            strength=EvidenceStrength.SUPPORTS,
            summary="no counterexample found in a sweep of 10^6 instances",
            literature_key="sweep:1",
        )
    ok = portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.NUMERICAL,
        strength=EvidenceStrength.CONSISTENT_WITH,
        summary="no counterexample found in a sweep of 10^6 instances",
        literature_key="sweep:1",
    )
    assert ok.strength is EvidenceStrength.CONSISTENT_WITH


def test_literature_evidence_must_name_a_retrieved_source(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Model memory is not storable as literature, so it cannot ground novelty."""

    idea, _ = seed_idea(portfolio, runtime_project)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.CONTRADICTS,
            summary="I recall a 2019 paper that proves exactly this",
            finding_id=None,
            artifact_id=None,
            literature_key=None,
        )


def test_evidence_with_nothing_under_it_is_refused(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.INSPECTION,
            strength=EvidenceStrength.SUPPORTS,
            summary="prose with no reference",
        )


# ------------------------------------------------------------- objections --
def test_an_objection_survives_the_revision_that_claims_nothing(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Rewording does not clear a fatal objection.

    The laundering path, run end to end: raise a FATAL objection, revise the
    idea so every review goes stale, and check the objection is still standing.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    review = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.FALSIFIER,
        verdict=ReviewVerdict.REJECT,
        severity=Severity.FATAL,
        summary="Theorem 3 of Tibshirani (2013) already states this.",
    )
    portfolio.raise_objection(
        idea_id=idea.idea_id,
        review_id=review.review_id,
        raised_at_version=1,
        severity=Severity.FATAL,
        summary="Theorem 3 of Tibshirani (2013) already states this.",
    )
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(core_idea="the same thing said differently"),
        origin_role="scientific_discovery",
    )
    standing = portfolio.open_objections(
        idea_id=idea.idea_id, minimum=Severity.CRITICAL
    )
    assert len(standing) == 1
    assert standing[0].severity is Severity.FATAL


def test_an_objection_cannot_be_resolved_by_the_role_that_raised_it(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    raised_by = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=ReviewVerdict.REVISE,
        severity=Severity.CRITICAL,
        summary="the identification argument assumes what it proves",
    )
    objection, _ = portfolio.raise_objection(
        idea_id=idea.idea_id,
        review_id=raised_by.review_id,
        raised_at_version=1,
        severity=Severity.CRITICAL,
        summary="the identification argument assumes what it proves",
    )
    key = pdigests.objection_key("the identification argument assumes what it proves")
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(mechanism="a separate identification argument"),
        origin_role="scientific_discovery",
        addressed_objections=[key],
    )
    same_role = record_review(
        portfolio, idea_id=idea.idea_id, version=2, role=ReviewerRole.SKEPTIC
    )
    with pytest.raises(PortfolioStateError, match="may not resolve"):
        portfolio.resolve_objection(
            objection_id=objection.objection_id,
            addressed_at_version=2,
            response="fixed",
            resolved_by_review=same_role.review_id,
        )

    other_role = record_review(
        portfolio, idea_id=idea.idea_id, version=2, role=ReviewerRole.METHODOLOGY
    )
    resolved = portfolio.resolve_objection(
        objection_id=objection.objection_id,
        addressed_at_version=2,
        response="the argument now derives identification from A2 rather than A1",
        resolved_by_review=other_role.review_id,
    )
    assert not resolved.open


def test_an_objection_cannot_be_resolved_by_a_version_that_never_named_it(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    raised_by = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=ReviewVerdict.REVISE,
        severity=Severity.MAJOR,
        summary="the comparison is not like for like",
    )
    objection, _ = portfolio.raise_objection(
        idea_id=idea.idea_id,
        review_id=raised_by.review_id,
        raised_at_version=1,
        severity=Severity.MAJOR,
        summary="the comparison is not like for like",
    )
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(title="a different title"),
        origin_role="scientific_discovery",
    )
    resolver = record_review(
        portfolio, idea_id=idea.idea_id, version=2, role=ReviewerRole.METHODOLOGY
    )
    with pytest.raises(PortfolioStateError, match="does not claim to address"):
        portfolio.resolve_objection(
            objection_id=objection.objection_id,
            addressed_at_version=2,
            response="silently fixed",
            resolved_by_review=resolver.review_id,
        )


def test_reviving_an_idea_carries_its_unanswered_objections(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    dead, _ = seed_idea(portfolio, runtime_project)
    review = record_review(
        portfolio,
        idea_id=dead.idea_id,
        version=1,
        role=ReviewerRole.FALSIFIER,
        verdict=ReviewVerdict.REJECT,
        severity=Severity.FATAL,
        summary="the compute required exceeds any cluster by four orders of magnitude",
    )
    portfolio.raise_objection(
        idea_id=dead.idea_id,
        review_id=review.review_id,
        raised_at_version=1,
        severity=Severity.FATAL,
        summary="the compute required exceeds any cluster by four orders of magnitude",
    )
    portfolio.set_status(
        idea_id=dead.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="infeasible compute",
    )

    revived, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.REVIVAL,
        fields=idea_fields(title="the same direction, on a smaller regime"),
        parent_idea_id=dead.idea_id,
        edge_kind=EdgeKind.REVIVES,
        origin_role="failure_mining_explorer",
    )
    carried = portfolio.carry_objections_forward(
        from_idea_id=dead.idea_id,
        to_idea_id=revived.idea_id,
        review_id=review.review_id,
    )
    assert carried == 1
    assert len(portfolio.open_objections(idea_id=revived.idea_id)) == 1


# --------------------------------------------------------------- tracking --
def test_an_idea_has_at_most_one_track_in_flight(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, version = seed_idea(portfolio, runtime_project)
    portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="basis-a",
    )
    with pytest.raises(ActiveTrackExistsError):
        portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.DISCOVER,
            basis_digest="basis-b",
        )
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.ACTIVE
    )
    del version


def test_the_same_basis_runs_once_but_a_failure_does_not_poison_it(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The partial index, and why it is partial.

    A succeeded stage owns its basis forever -- that is the idempotency the
    invariant promises. A *failed* one must not, or a provider outage would
    take the idea out of the portfolio permanently and nothing would say so.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    first = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="basis-a",
    )
    portfolio.complete_action(
        action_id=first.action_id,
        status=__import__(
            "research_os.portfolio.models", fromlist=["ActionStatus"]
        ).ActionStatus.FAILED,
        failure_class="PROVIDER_UNAVAILABLE",
        operational_state=OperationalState.BLOCKED_PROVIDER,
    )
    retry = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="basis-a",
    )
    assert retry.action_id != first.action_id

    portfolio.complete_action(
        action_id=retry.action_id,
        status=__import__(
            "research_os.portfolio.models", fromlist=["ActionStatus"]
        ).ActionStatus.SUCCEEDED,
    )
    with pytest.raises(DuplicateBasisError):
        portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.FALSIFY,
            basis_digest="basis-a",
        )


def test_a_human_ready_idea_frees_its_capacity_slot(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The release-critical property, at the level it is actually decided.

    Capacity counts ideas whose *operational* state is ACTIVE. A HUMAN_READY
    idea has no track, so it counts for nothing, and the portfolio's other
    ideas keep their slots. There is no portfolio state that one waiting idea
    can put the project into.
    """

    from research_os.portfolio.models import ActionStatus

    waiting, _ = seed_idea(portfolio, runtime_project, title="A")
    working, _ = seed_idea(portfolio, runtime_project, title="B")

    action = portfolio.open_action(
        idea_id=waiting.idea_id,
        idea_version=1,
        stage=Stage.META_REVIEW,
        basis_digest="b1",
    )
    portfolio.open_action(
        idea_id=working.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="b2",
    )
    assert portfolio.active_count(runtime_project) == 2

    portfolio.complete_action(action_id=action.action_id, status=ActionStatus.SUCCEEDED)
    portfolio.set_status(idea_id=waiting.idea_id, status=IdeaStatus.HUMAN_READY)

    assert portfolio.active_count(runtime_project) == 1
    assert not portfolio.require_idea(waiting.idea_id).occupies_capacity
    assert portfolio.require_idea(working.idea_id).occupies_capacity


# ------------------------------------------------------------- retirement --
@pytest.mark.parametrize(
    "status", [IdeaStatus.REJECTED, IdeaStatus.PARKED, IdeaStatus.SUPERSEDED]
)
def test_retiring_an_idea_requires_a_reason(
    portfolio: PortfolioStore, runtime_project: str, status: IdeaStatus
) -> None:
    """`docs/CAPSULE.md`'s retirement-memory rule, applied to candidates.

    Without it, `researchctl ideas rejected` cannot tell "we ruled this out"
    from "we forgot about it", and the failure-mining explorer -- whose entire
    input is why things died -- is handed a list of nulls.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.set_status(idea_id=idea.idea_id, status=status)


def test_parking_an_idea_requires_what_would_bring_it_back(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.set_status(
            idea_id=idea.idea_id,
            status=IdeaStatus.PARKED,
            retire_reason="not now",
        )
    parked = portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="the cluster this needs is unavailable",
        revisit_if="the cluster becomes available, or a smaller regime is found",
    )
    assert parked.status is IdeaStatus.PARKED


def test_the_quality_tier_is_a_high_water_mark(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """A rejection after PROMISING is a different fact from a rejection before it."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    rejected = portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="the falsifier found a counterexample",
    )
    assert rejected.status is IdeaStatus.REJECTED
    assert rejected.quality_tier is QualityTier.PROMISING


# ------------------------------------------------------------ bookkeeping --
def test_the_uncurated_count_is_the_stated_exposure(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    assert portfolio.uncurated_count(runtime_project) == 1
    portfolio.mark_curated(idea_ids=[idea.idea_id], snapshot_digest="snap-1")
    assert portfolio.uncurated_count(runtime_project) == 0
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    assert portfolio.uncurated_count(runtime_project) == 1, (
        "a state change the Curator has not written is uncurated again"
    )


def test_every_table_is_truncated_between_tests(runtime_db: Database) -> None:
    """The fixture's table list must cover the live schema.

    Backwards, deliberately: a new table that nobody adds to ``RUNTIME_TABLES``
    leaks rows into the next test, and the failure that produces is somebody
    else's test failing for a reason that is not about them.
    """

    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' and table_type = 'BASE TABLE'"
        ).fetchall()
    live = {str(row["table_name"]) for row in rows} - {"schema_migrations"}
    assert live - set(RUNTIME_TABLES) == set()


def test_the_adjudication_vocabulary_is_the_runtimes_own() -> None:
    """The portfolio must not invent a second classification scheme.

    ``research_os.runtime.adjudication`` already classifies a scientific target
    deterministically from its falsification clause, and its docstring says why
    that matters: *not from a model*. A parallel enum here would be a second
    answer to one question, and the second one would be a model's.
    """

    from research_os.runtime.adjudication import AdjudicationKind

    assert {item.value for item in AdjudicationType} == {
        item.value for item in AdjudicationKind
    }
