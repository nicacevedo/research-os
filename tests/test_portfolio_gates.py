"""The quality gates: what the rows permit, and what no amount of prose does.

Every test here is built on one fixture -- an idea assembled so that it
*passes* every gate -- and then removes exactly one thing. That shape is
deliberate. A suite of gate tests that only ever checks refusals passes
perfectly against a gate that refuses everything, so
``test_a_complete_idea_reaches_human_ready`` is the positive control that makes
every other test in this file capable of failing.
"""

from __future__ import annotations

import pytest

from research_os.portfolio.config import PortfolioConfig, load_config
from research_os.portfolio.gates import (
    SelfReviewError,
    board_independence,
    classify_independence,
    evaluate,
    permit,
)
from research_os.portfolio.models import (
    AdjudicationType,
    Disposition,
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaVersion,
    QualityTier,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.interfaces import Independence
from research_os.runtime.models import ModelCallStatus
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

#: Every stage the gates consult as "this ran". Passed wholesale except where a
#: test is about one of them being absent.
ALL_STAGES = frozenset(Stage)


def _config() -> PortfolioConfig:
    return load_config()


def replicator_call(runtime: RuntimeStore):
    """A model call standing in for a replicator's own invocation.

    Replication requires a *different* call from the one that produced the
    original, so a test that wants replication to count has to supply one. That
    the fixture has to do this is the point: "a second opinion from the same
    source" is exactly what the rule refuses.
    """

    return runtime.record_model_call(
        provider="codex",
        role="replicator",
        status=ModelCallStatus.OK,
        model="gpt-x",
    )


class Built:
    """One fully-assembled idea, and the handles a test needs to take it apart."""

    def __init__(self, store: PortfolioStore, idea_id: str, version: int) -> None:
        self.store = store
        self.idea_id = idea_id
        self.version = version

    def gate(
        self,
        *,
        stages: frozenset[Stage] = ALL_STAGES,
        requested: QualityTier = QualityTier.HUMAN_READY,
        config: PortfolioConfig | None = None,
    ):
        head = self.store.require_version(self.idea_id, self.version)
        return evaluate(
            version=head,
            live_reviews=self.store.live_reviews(idea_id=self.idea_id),
            objections=self.store.open_objections(idea_id=self.idea_id),
            evidence=self.store.list_evidence(
                idea_id=self.idea_id, idea_version=self.version
            ),
            succeeded_stages=stages,
            config=config or _config(),
            requested=requested,
        )


def _build(
    store: PortfolioStore,
    runtime_db: Database,
    project_id: str,
    *,
    adjudication: list[AdjudicationType] | None = None,
    with_evidence: bool = True,
    with_reviews: bool = True,
    with_replication: bool = True,
    replication_from_origin: bool = False,
    literature_keys: int = 3,
    review_verdict: ReviewVerdict = ReviewVerdict.PASS,
    families: tuple[str, ...] = ("openai", "google", "anthropic"),
) -> Built:
    """Assemble an idea that passes every gate, then let a test remove one thing."""

    runtime = RuntimeStore(runtime_db)
    # The call that produced the idea. Recorded rather than left null, because
    # "the replication came from a different worker" is only checkable against
    # a worker that is named.
    origin_call = runtime.record_model_call(
        provider="claude",
        role="blind_explorer",
        status=ModelCallStatus.OK,
        model="opus",
    )
    idea, _ = store.create_idea(
        project_id=project_id,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            adjudication_types=adjudication or [AdjudicationType.MATHEMATICAL]
        ),
        origin_role="blind_explorer",
        origin_call_id=origin_call.call_id,
    )
    built = Built(store, idea.idea_id, 1)

    if with_evidence:
        job = runtime.create_external_job(
            project_id=project_id,
            executor="local",
            spec_digest="spec-counterexample-search",
            run_dir="/tmp/search",
        )
        store.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.DERIVATION,
            strength=EvidenceStrength.SUPPORTS,
            summary="the two selection orderings differ on a 2x2 instance",
            literature_key="derivation:1",
            source_call_id=None,
        )
        store.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.EXPERIMENT,
            strength=EvidenceStrength.SUPPORTS,
            summary="an executed search exhibited the divergent instance",
            job_id=job.job_id,
        )
        for index in range(literature_keys):
            store.add_evidence(
                idea_id=idea.idea_id,
                idea_version=1,
                kind=EvidenceKind.LITERATURE,
                strength=EvidenceStrength.CONSISTENT_WITH,
                summary=f"retrieved source {index}",
                literature_key=f"openalex:W{index}",
            )
    if with_replication:
        second = runtime.create_external_job(
            project_id=project_id,
            executor="local",
            spec_digest="spec-second-parameterisation",
            run_dir="/tmp/search2",
        )
        store.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.REPLICATION,
            strength=EvidenceStrength.SUPPORTS,
            summary="a second search under a different parameterisation agreed",
            job_id=second.job_id,
            source_call_id=(
                origin_call.call_id
                if replication_from_origin
                else replicator_call(runtime).call_id
            ),
        )
    if with_reviews:
        for role, family in zip(
            (ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY, ReviewerRole.SKEPTIC),
            families,
            strict=False,
        ):
            record_review(
                store,
                idea_id=idea.idea_id,
                version=1,
                role=role,
                verdict=review_verdict,
                family=family,
                model=f"model-{family}",
            )
    return built


# ------------------------------------------------------------ the control --
def test_a_complete_idea_reaches_human_ready(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The positive control.

    Without it, every refusal test below would pass against a gate that refuses
    everything, and the suite would be green and worthless.
    """

    built = _build(portfolio, runtime_db, runtime_project)
    result = built.gate()
    assert result.unmet == (), result.unmet
    assert result.tier is QualityTier.HUMAN_READY
    assert result.passed


# --------------------------------------------------------------- promising --
def test_an_idea_without_a_falsifier_is_not_promising(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=__import__(
            "research_os.portfolio.models", fromlist=["IdeaOrigin"]
        ).IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(falsifier=""),
        origin_role="blind_explorer",
    )
    built = Built(portfolio, idea.idea_id, 1)
    result = built.gate(requested=QualityTier.PROMISING)
    assert not result.passed
    assert any("falsifier" in item for item in result.unmet)


def test_a_standing_fatal_objection_blocks_promising(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project)
    assert built.gate(requested=QualityTier.PROMISING).passed

    killer = record_review(
        portfolio,
        idea_id=built.idea_id,
        version=1,
        role=ReviewerRole.FALSIFIER,
        verdict=ReviewVerdict.REJECT,
        severity=Severity.FATAL,
        summary="Theorem 3 of a 2013 paper already states exactly this",
    )
    portfolio.raise_objection(
        idea_id=built.idea_id,
        review_id=killer.review_id,
        raised_at_version=1,
        severity=Severity.FATAL,
        summary="Theorem 3 of a 2013 paper already states exactly this",
    )
    result = built.gate(requested=QualityTier.PROMISING)
    assert not result.passed
    assert any("fatal objection" in item for item in result.unmet)


def test_the_cheap_screen_must_have_run(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project)
    result = built.gate(stages=ALL_STAGES - {Stage.NOVELTY_SCREEN})
    assert any("novelty screen" in item for item in result.unmet)


# --------------------------------------------------------------- validated --
def test_two_reviews_are_not_three(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project, with_reviews=False)
    for role in (ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY):
        record_review(portfolio, idea_id=built.idea_id, version=1, role=role)
    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("skeptic_reviewer review" in item for item in result.unmet)


def test_three_reviewers_who_all_say_revise_do_not_validate(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """ "Three reviews exist" is not "three reviewers endorsed this".

    The gate reads the verdict. A tier that counted rows would mean "three
    reviewers looked at it", which is a different and much weaker claim than
    the word VALIDATED makes.
    """

    built = _build(
        portfolio, runtime_db, runtime_project, review_verdict=ReviewVerdict.REVISE
    )
    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert sum("did not endorse" in item for item in result.unmet) == 3


def test_a_mathematical_idea_needs_something_a_machine_ran(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """A model's prose derivation is a document, not a check."""

    idea, _ = seed_idea(
        portfolio, runtime_project, adjudication_types=[AdjudicationType.MATHEMATICAL]
    )
    built = Built(portfolio, idea.idea_id, 1)
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.DERIVATION,
        strength=EvidenceStrength.SUPPORTS,
        summary="a complete and confident derivation",
        literature_key="derivation:1",
    )
    for index in range(3):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.CONSISTENT_WITH,
            summary=f"source {index}",
            literature_key=f"openalex:W{index}",
        )
    for role in (
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    ):
        record_review(portfolio, idea_id=idea.idea_id, version=1, role=role)

    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("nothing was executed" in item for item in result.unmet)


def test_a_numerical_witness_alone_is_not_substantive_evidence(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Two finite computations do not establish a universally quantified claim."""

    runtime = RuntimeStore(runtime_db)
    idea, _ = seed_idea(
        portfolio, runtime_project, adjudication_types=[AdjudicationType.MATHEMATICAL]
    )
    built = Built(portfolio, idea.idea_id, 1)
    job = runtime.create_external_job(
        project_id=runtime_project,
        executor="local",
        spec_digest="sweep",
        run_dir="/tmp/sweep",
    )
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.NUMERICAL,
        strength=EvidenceStrength.CONSISTENT_WITH,
        summary="no counterexample in 10^6 sampled instances",
        job_id=job.job_id,
    )
    for role in (
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    ):
        record_review(portfolio, idea_id=idea.idea_id, version=1, role=role)

    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("substantive derivation evidence" in item for item in result.unmet)
    assert any("does not establish it" in note for note in result.notes)


def test_the_novelty_case_needs_more_than_one_retrieved_source(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project, literature_keys=1)
    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("retrieved source" in item for item in result.unmet)


def test_declaring_a_second_type_adds_requirements_and_removes_none(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The generator cannot choose a cheaper bar by classifying differently.

    An idea that is mathematical *and* empirical must satisfy both, so adding
    ``novelty_or_literature`` to an empirical claim cannot route it past
    execution.
    """

    idea, _ = seed_idea(
        portfolio,
        runtime_project,
        adjudication_types=[
            AdjudicationType.EMPIRICAL,
            AdjudicationType.NOVELTY_OR_LITERATURE,
        ],
    )
    built = Built(portfolio, idea.idea_id, 1)
    for index in range(4):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.SUPPORTS,
            summary=f"source {index}",
            literature_key=f"openalex:W{index}",
        )
    for role in (
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    ):
        record_review(portfolio, idea_id=idea.idea_id, version=1, role=role)

    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("experiment" in item for item in result.unmet)


def test_an_undetermined_idea_cannot_be_validated(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(
        portfolio,
        runtime_db,
        runtime_project,
        adjudication=[AdjudicationType.UNDETERMINED],
    )
    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("Sharpen the falsifier" in item for item in result.unmet)


def test_a_standing_critical_objection_blocks_validated(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project)
    assert built.gate(requested=QualityTier.VALIDATED).passed

    raiser = record_review(
        portfolio,
        idea_id=built.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=ReviewVerdict.PASS_WITH_OBJECTIONS,
        severity=Severity.CRITICAL,
        summary="the comparison holds only under an assumption never stated",
    )
    portfolio.raise_objection(
        idea_id=built.idea_id,
        review_id=raiser.review_id,
        raised_at_version=1,
        severity=Severity.CRITICAL,
        summary="the comparison holds only under an assumption never stated",
    )
    result = built.gate(requested=QualityTier.VALIDATED)
    assert not result.passed
    assert any("CRITICAL or above" in item for item in result.unmet)


# ------------------------------------------------------------ human ready --
def test_replication_by_the_same_source_does_not_count(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """ "A second opinion from the same source" is not replication.

    The replicating evidence must come from a different model call than the
    one that produced the original, or the check is the same worker agreeing
    with itself.
    """

    built = _build(
        portfolio,
        runtime_db,
        runtime_project,
        replication_from_origin=True,
    )
    result = built.gate(requested=QualityTier.HUMAN_READY)
    assert result.tier is QualityTier.VALIDATED
    assert any("second-line verification" in item for item in result.unmet)


def test_an_unanswered_major_objection_blocks_human_ready_only(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project)
    raiser = record_review(
        portfolio,
        idea_id=built.idea_id,
        version=1,
        role=ReviewerRole.METHODOLOGY,
        verdict=ReviewVerdict.PASS_WITH_OBJECTIONS,
        severity=Severity.MAJOR,
        summary="the baseline is not tuned to the same budget",
    )
    portfolio.raise_objection(
        idea_id=built.idea_id,
        review_id=raiser.review_id,
        raised_at_version=1,
        severity=Severity.MAJOR,
        summary="the baseline is not tuned to the same budget",
    )
    result = built.gate()
    assert result.tier is QualityTier.VALIDATED
    assert any("MAJOR or above" in item for item in result.unmet)


def test_an_idea_with_no_stated_limitations_is_not_human_ready(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(
        portfolio,
        runtime_project,
        open_uncertainties=[],
        adjudication_types=[AdjudicationType.MATHEMATICAL],
    )
    built = Built(portfolio, idea.idea_id, 1)
    result = built.gate()
    assert any("limitations" in item for item in result.unmet)


# ------------------------------------------------------------ the min rule --
def test_a_meta_reviewer_cannot_promote_an_idea_the_rows_do_not_support(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The stub that always recommends the top tier.

    This is the failure mode the whole gate apparatus exists for: a
    meta-reviewer that writes a confident paragraph and recommends
    HUMAN_READY. It gets CONTINUE, because the idea has no reviews, no
    evidence and no audit.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    built = Built(portfolio, idea.idea_id, 1)
    result = built.gate(stages=frozenset())

    assert result.tier is QualityTier.NONE
    assert permit(Disposition.HUMAN_READY, result) is Disposition.CONTINUE
    assert permit(Disposition.VALIDATED, result) is Disposition.CONTINUE
    assert permit(Disposition.PROMISING, result) is Disposition.CONTINUE


def test_the_min_rule_lowers_rather_than_refusing(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project, with_replication=False)
    result = built.gate()
    assert result.tier is QualityTier.VALIDATED
    assert permit(Disposition.HUMAN_READY, result) is Disposition.VALIDATED


@pytest.mark.parametrize(
    "disposition",
    [
        Disposition.REJECT,
        Disposition.PARK,
        Disposition.REVISE,
        Disposition.DEEPEN,
        Disposition.BRANCH,
        Disposition.DUPLICATE,
    ],
)
def test_ungated_dispositions_pass_through(
    portfolio: PortfolioStore, runtime_project: str, disposition: Disposition
) -> None:
    """Killing or pausing an idea needs no ceremony. That asymmetry is the design."""

    idea, _ = seed_idea(portfolio, runtime_project)
    result = Built(portfolio, idea.idea_id, 1).gate(stages=frozenset())
    assert permit(disposition, result) is disposition


# ----------------------------------------------------------- independence --
def test_a_review_by_the_call_that_produced_the_work_raises(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    with pytest.raises(SelfReviewError):
        classify_independence(
            origin_call_id="MCALL-1",
            review_call_id="MCALL-1",
            origin_provider_family="anthropic",
            review_provider_family="anthropic",
            origin_model="opus",
            review_model="opus",
            frozen_packet=True,
        )
    del portfolio, runtime_project


@pytest.mark.parametrize(
    ("origin_family", "review_family", "origin_model", "review_model", "expected"),
    [
        ("anthropic", "openai", "opus", "gpt", Independence.DIFFERENT_FAMILY),
        ("anthropic", "anthropic", "opus", "sonnet", Independence.DIFFERENT_MODEL),
        ("anthropic", "anthropic", "opus", "opus", Independence.DIFFERENT_CONTEXT),
    ],
)
def test_independence_is_classified_against_the_versions_origin(
    origin_family: str,
    review_family: str,
    origin_model: str,
    review_model: str,
    expected: Independence,
) -> None:
    assert (
        classify_independence(
            origin_call_id="MCALL-origin",
            review_call_id="MCALL-review",
            origin_provider_family=origin_family,
            review_provider_family=review_family,
            origin_model=origin_model,
            review_model=review_model,
            frozen_packet=True,
        )
        is expected
    )


def test_one_model_reviewing_three_times_is_reported_as_one(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The honest limit, surfaced as a number rather than implied away.

    Three reviewer *roles* are not three independent reviewers. On a
    one-provider host the board's independence is 1, the gate says so in a
    note, and §10 of the architecture forbids any rendered output from calling
    that independent.
    """

    built = _build(
        portfolio,
        runtime_db,
        runtime_project,
        families=("anthropic", "anthropic", "anthropic"),
    )
    result = built.gate()
    assert result.board_independence == 1
    assert any("not independent review" in note for note in result.notes)
    assert result.passed, (
        "a one-family board is reported, not refused -- refusing would mean no "
        "review ever happens on a single-provider machine"
    )


def test_three_families_are_counted_as_three(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    built = _build(portfolio, runtime_db, runtime_project)
    result = built.gate()
    assert result.board_independence == 3
    assert not any("not independent review" in note for note in result.notes)


def test_board_independence_ignores_the_falsifier_and_the_meta_reviewer(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Only the three independent roles count towards the board.

    A falsifier from a fourth family would otherwise make a one-family board
    look like a two-family one, and the falsifier is not a review of the
    finished work -- its job was to kill the idea before this point.
    """

    built = _build(
        portfolio,
        runtime_db,
        runtime_project,
        families=("anthropic", "anthropic", "anthropic"),
    )
    record_review(
        portfolio,
        idea_id=built.idea_id,
        version=1,
        role=ReviewerRole.FALSIFIER,
        family="openai",
        model="gpt",
    )
    assert board_independence(portfolio.live_reviews(idea_id=built.idea_id)) == 1


def test_a_gate_evaluated_on_a_version_object_needs_no_database(
    runtime_project: str,
) -> None:
    """`evaluate` is a pure function of rows, which is what makes it testable."""

    version = IdeaVersion(
        idea_id="PIDEA-20260101T000000Z-00000000",
        version=1,
        title="t",
        research_question="q",
        core_idea="c",
        mechanism="m",
        falsifier="f",
        content_digest="d",
        canonical_digest="c",
        origin_role="blind_explorer",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    result = evaluate(
        version=version,
        live_reviews=(),
        objections=(),
        evidence=(),
        succeeded_stages=frozenset({Stage.NOVELTY_SCREEN}),
        config=_config(),
        requested=QualityTier.PROMISING,
    )
    assert result.passed
    assert result.tier is QualityTier.PROMISING
    del runtime_project
