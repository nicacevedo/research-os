"""The stage machine: total, deterministic, and cheapest-first.

No database. :func:`select_stage` is a pure function of a snapshot, which is
what makes "what would this idea do next" answerable by construction rather
than by setting up a portfolio -- and what makes the domain enumerable, which
is the property the architecture claims for it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    AdjudicationType,
    EvidenceKind,
    EvidenceStrength,
    IdeaEvidence,
    IdeaObjection,
    IdeaReview,
    IdeaStatus,
    IdeaVersion,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.stages import (
    STAGE_ORDER,
    TrackSnapshot,
    classify_adjudication,
    select_stage,
)
from research_os.runtime.interfaces import Independence

NOW = datetime.now(UTC)
CONFIG = load_config()


def version(**overrides: object) -> IdeaVersion:
    base: dict[str, object] = {
        "idea_id": "PIDEA-20260101T000000Z-00000000",
        "version": 1,
        "title": "t",
        "research_question": "Do the trajectories diverge?",
        "core_idea": "They diverge when the screening bound is not monotone.",
        "mechanism": "Selection by dual violation differs from selection by bound.",
        "falsifier": "Exhibit an instance where both visit identical supports.",
        "adjudication_types": (AdjudicationType.MATHEMATICAL,),
        "content_digest": "d",
        "canonical_digest": "c",
        "origin_role": "blind_explorer",
        "created_at": NOW,
    }
    base.update(overrides)
    return IdeaVersion(**base)  # type: ignore[arg-type]


def review(role: ReviewerRole) -> IdeaReview:
    return IdeaReview(
        review_id=f"IREV-{role}",
        idea_id="PIDEA-20260101T000000Z-00000000",
        idea_version=1,
        reviewer_role=role,
        verdict=ReviewVerdict.PASS,
        severity=Severity.NONE,
        summary="fine",
        reviewed_content_digest="d",
        reviewed_evidence_digest="e",
        packet_digest="p",
        prompt_version=f"{role}@1",
        provider="codex",
        provider_family="openai",
        independence_vs_origin=Independence.DIFFERENT_FAMILY,
        context_class="FROZEN_PACKET",
        created_at=NOW,
    )


def evidence(
    kind: EvidenceKind,
    *,
    strength: EvidenceStrength = EvidenceStrength.SUPPORTS,
    job_id: str | None = None,
    literature_key: str | None = None,
    index: int = 0,
) -> IdeaEvidence:
    return IdeaEvidence(
        evidence_id=f"IEVD-{kind}-{index}",
        idea_id="PIDEA-20260101T000000Z-00000000",
        idea_version=1,
        kind=kind,
        strength=strength,
        summary="s",
        job_id=job_id,
        literature_key=literature_key,
        created_at=NOW,
    )


def objection(severity: Severity) -> IdeaObjection:
    return IdeaObjection(
        objection_id=f"IOBJ-{severity}",
        idea_id="PIDEA-20260101T000000Z-00000000",
        raised_in_review="IREV-1",
        raised_at_version=1,
        objection_key=f"key-{severity}",
        severity=severity,
        summary=f"a {severity} problem",
        created_at=NOW,
    )


def snapshot(**overrides: object) -> TrackSnapshot:
    base: dict[str, object] = {
        "status": IdeaStatus.CANDIDATE,
        "version": version(),
    }
    base.update(overrides)
    return TrackSnapshot(**base)  # type: ignore[arg-type]


# ------------------------------------------------------- cheapest first --
def test_the_cheap_stages_run_before_anything_expensive() -> None:
    """The governing principle, as an ordering assertion.

    Each cheap stage is added in turn and the next one is demanded. Nothing
    that costs dollars is reachable until everything that could have stopped
    the idea for cents has run.
    """

    done: set[Stage] = set()
    for expected in (Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY):
        stage, _why = select_stage(snapshot(succeeded_stages=frozenset(done)), CONFIG)
        assert stage is expected
        done.add(expected)


def test_a_fatal_objection_ends_the_track_immediately() -> None:
    """Killing is cheap on purpose.

    The gate would refuse promotion anyway. Stopping here is what makes the
    refusal cost nothing, which is the whole difference between a portfolio
    that kills ideas and one that merely declines to promote them.
    """

    stage, why = select_stage(
        snapshot(
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY}
            ),
            open_objections=(objection(Severity.FATAL),),
        ),
        CONFIG,
    )
    assert stage is None
    assert "fatal objection" in why


def test_an_imprecise_idea_is_sharpened_before_it_is_investigated() -> None:
    stage, _why = select_stage(
        snapshot(
            version=version(mechanism="", falsifier=""),
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY}
            ),
        ),
        CONFIG,
    )
    assert stage is Stage.DISCOVER


def test_an_idea_with_no_adjudication_type_is_classified_before_evidence() -> None:
    stage, why = select_stage(
        snapshot(
            version=version(adjudication_types=()),
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY, Stage.DISCOVER}
            ),
            revision_count=1,
        ),
        CONFIG,
    )
    assert stage is Stage.ADJUDICATE
    assert "falsifier" in why


def test_an_undetermined_type_sends_the_idea_back_to_be_sharpened() -> None:
    """Conservative, and it is the behaviour `adjudication.py` was built for.

    UNDETERMINED routes nothing and blocks nothing. What it must not do is
    proceed to an evidence stage, because no kind of evidence has been
    identified that would settle the idea.
    """

    stage, why = select_stage(
        snapshot(
            version=version(adjudication_types=(AdjudicationType.UNDETERMINED,)),
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY, Stage.DISCOVER}
            ),
            revision_count=1,
        ),
        CONFIG,
    )
    assert stage is Stage.DISCOVER
    assert "does not say what kind of work" in why


# --------------------------------------------------- status gates on cost --
def test_an_unpromising_idea_does_not_reach_the_evidence_stage() -> None:
    """ "Expensive evidence stages require stronger quality gates."

    The idea below has everything it needs *except* a status, and the evidence
    stage costs two and a half dollars. It is skipped, and the track ends
    rather than spending.
    """

    ready = frozenset(
        {
            Stage.DEDUP,
            Stage.NOVELTY_SCREEN,
            Stage.FALSIFY,
            Stage.DISCOVER,
            Stage.LITERATURE_AUDIT,
        }
    )
    audited = tuple(
        evidence(EvidenceKind.LITERATURE, literature_key=f"openalex:W{i}", index=i)
        for i in range(3)
    )
    stage, _why = select_stage(
        snapshot(
            status=IdeaStatus.CANDIDATE,
            succeeded_stages=ready,
            evidence=audited,
            revision_count=1,
        ),
        CONFIG,
    )
    assert stage is not Stage.EVIDENCE

    promoted, _why = select_stage(
        snapshot(
            status=IdeaStatus.PROMISING,
            succeeded_stages=ready,
            evidence=audited,
            revision_count=1,
        ),
        CONFIG,
    )
    assert promoted is Stage.EVIDENCE


def test_replication_is_not_reachable_before_validated() -> None:
    everything = frozenset(STAGE_ORDER) - {Stage.REPLICATE, Stage.BRANCH}
    stage, _why = select_stage(
        snapshot(
            status=IdeaStatus.REVIEW,
            succeeded_stages=everything,
            basis_stages=everything,
            evidence=_complete_evidence(),
            live_reviews=_complete_reviews(),
            revision_count=1,
        ),
        CONFIG,
    )
    assert stage is not Stage.REPLICATE

    stage, _why = select_stage(
        snapshot(
            status=IdeaStatus.VALIDATED,
            succeeded_stages=everything,
            basis_stages=everything,
            evidence=_complete_evidence(),
            live_reviews=_complete_reviews(),
            revision_count=1,
        ),
        CONFIG,
    )
    assert stage is Stage.REPLICATE


def _complete_evidence() -> tuple[IdeaEvidence, ...]:
    return (
        evidence(EvidenceKind.DERIVATION),
        evidence(EvidenceKind.EXPERIMENT, job_id="XJOB-1"),
        *(
            evidence(EvidenceKind.LITERATURE, literature_key=f"openalex:W{i}", index=i)
            for i in range(3)
        ),
    )


def _complete_reviews() -> tuple[IdeaReview, ...]:
    return (
        review(ReviewerRole.METHODOLOGY),
        review(ReviewerRole.NOVELTY),
        review(ReviewerRole.SKEPTIC),
    )


# ------------------------------------------------------------- the loops --
def test_the_revision_loop_has_a_stop_condition() -> None:
    """Invariant 14, at the one place a portfolio can loop that a cycle cannot.

    "Revise until a stochastic reviewer stops objecting" is a loop. Its stop
    condition is the revision bound, and it applies to the three places a
    revision can be demanded: imprecision, an undetermined type, and a standing
    critical objection.
    """

    spent = CONFIG.bounds.max_revisions_per_idea + 1
    stage, why = select_stage(
        snapshot(
            version=version(mechanism=""),
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY}
            ),
            revision_count=spent,
        ),
        CONFIG,
    )
    assert stage is None
    assert "revision bound" in why or "rewritten" in why


def test_the_review_loop_has_a_stop_condition() -> None:
    stage, why = select_stage(
        snapshot(
            status=IdeaStatus.INVESTIGATING,
            succeeded_stages=frozenset(STAGE_ORDER) - {Stage.REVIEW_BOARD},
            basis_stages=frozenset(STAGE_ORDER) - {Stage.REVIEW_BOARD},
            evidence=_complete_evidence(),
            revision_count=1,
            review_count=CONFIG.bounds.max_reviews_per_idea,
        ),
        CONFIG,
    )
    assert stage is None
    assert "ceiling" in why


def test_deepening_on_reasoning_alone_has_a_stop_condition() -> None:
    stage, why = select_stage(
        snapshot(
            status=IdeaStatus.PROMISING,
            succeeded_stages=frozenset(
                {
                    Stage.DEDUP,
                    Stage.NOVELTY_SCREEN,
                    Stage.FALSIFY,
                    Stage.DISCOVER,
                    Stage.LITERATURE_AUDIT,
                }
            ),
            evidence=tuple(
                evidence(
                    EvidenceKind.LITERATURE, literature_key=f"openalex:W{i}", index=i
                )
                for i in range(3)
            ),
            revision_count=1,
            depth_without_evidence=CONFIG.bounds.max_depth_without_evidence + 1,
        ),
        CONFIG,
    )
    assert stage is None
    assert "without new evidence" in why


def test_branching_stops_at_the_lineage_ceiling() -> None:
    stage, why = select_stage(
        snapshot(
            status=IdeaStatus.VALIDATED,
            succeeded_stages=frozenset(STAGE_ORDER) - {Stage.BRANCH},
            basis_stages=frozenset(STAGE_ORDER) - {Stage.BRANCH},
            evidence=_complete_evidence(),
            live_reviews=_complete_reviews(),
            revision_count=1,
            lineage_active=CONFIG.bounds.max_active_per_lineage,
        ),
        CONFIG,
    )
    assert stage is None
    assert "lineage" in why


# --------------------------------------------------------------- totality --
@pytest.mark.parametrize("status", list(IdeaStatus))
def test_every_status_produces_exactly_one_answer(status: IdeaStatus) -> None:
    """Total: no reachable input leaves the machine without a decision."""

    stage, why = select_stage(snapshot(status=status), CONFIG)
    assert why
    assert stage is None or stage in STAGE_ORDER


@pytest.mark.parametrize(
    "status", [IdeaStatus.REJECTED, IdeaStatus.SUPERSEDED, IdeaStatus.PARKED]
)
def test_a_closed_or_parked_idea_gets_no_work(status: IdeaStatus) -> None:
    stage, why = select_stage(snapshot(status=status), CONFIG)
    assert stage is None
    assert str(status) in why


@pytest.mark.parametrize(
    "start",
    [
        IdeaStatus.CANDIDATE,
        IdeaStatus.PROMISING,
        IdeaStatus.INVESTIGATING,
        IdeaStatus.REVIEW,
        IdeaStatus.VALIDATED,
    ],
)
@pytest.mark.parametrize(
    "declared",
    [
        (AdjudicationType.MATHEMATICAL,),
        (AdjudicationType.EMPIRICAL,),
        (AdjudicationType.NOVELTY_OR_LITERATURE,),
        (AdjudicationType.MIXED,),
        (AdjudicationType.UNDETERMINED,),
        (),
    ],
)
def test_the_machine_terminates_from_any_starting_point(
    start: IdeaStatus, declared: tuple[AdjudicationType, ...]
) -> None:
    """Drive it to a fixed point and assert it reaches one.

    Parametrised over every status and every adjudication type, because both
    loops this property has actually caught were type- and version-dependent
    and a single fixed starting point would have missed either.

    The harness models what each stage *does*, not only that it ran. A
    ``DISCOVER`` appends a version, so it spends a revision and may legitimately
    be selected again; an ``ADJUDICATE`` writes the type. Marking either done
    and moving on -- which the first version did -- made a genuine repeat look
    like a loop, and would equally have hidden one.
    """

    done: set[Stage] = set()
    revisions = 1
    types = declared
    bound = len(STAGE_ORDER) + CONFIG.bounds.max_revisions_per_idea + 2
    for _ in range(bound):
        stage, _why = select_stage(
            snapshot(
                status=start,
                version=version(adjudication_types=types),
                succeeded_stages=frozenset(done),
                basis_stages=frozenset(done),
                evidence=_complete_evidence(),
                live_reviews=_complete_reviews(),
                revision_count=revisions,
            ),
            CONFIG,
        )
        if stage is None:
            break
        if stage is Stage.DISCOVER:
            # What discover does: append a version, spending a revision. The
            # bound on that is what makes an idea nobody can sharpen stop.
            revisions += 1
            done.add(stage)
            continue
        if stage is Stage.ADJUDICATE:
            # What adjudicate does: write the type read from the idea's own
            # text. A harness that only marked it done would see the machine
            # ask for it forever, which is a loop in the harness rather than
            # in the machine.
            types = (AdjudicationType.NOVELTY_OR_LITERATURE,)
            done.add(stage)
            continue
        assert stage not in done, f"{stage} was selected twice"
        done.add(stage)
    else:  # pragma: no cover - a loop would land here
        pytest.fail(
            f"the stage machine did not reach a fixed point from {start} "
            f"with {declared}; it ran {sorted(done)}"
        )


def test_an_idea_nobody_can_sharpen_stops_rather_than_revising_forever() -> None:
    """The stop condition the parametrised property above depends on.

    An idea whose falsifier says nothing about how it would be settled is sent
    back to be sharpened. If sharpening never helps, the revision bound is what
    ends it -- and a bound that was never reached would make the property above
    pass for the wrong reason.
    """

    stage, why = select_stage(
        snapshot(
            version=version(adjudication_types=(AdjudicationType.UNDETERMINED,)),
            succeeded_stages=frozenset(
                {Stage.DEDUP, Stage.NOVELTY_SCREEN, Stage.FALSIFY, Stage.DISCOVER}
            ),
            revision_count=CONFIG.bounds.max_revisions_per_idea + 1,
        ),
        CONFIG,
    )
    assert stage is None
    assert "revision bound is spent" in why


def test_the_selector_consults_no_model_and_no_clock() -> None:
    """Determinism, asserted by calling it twice.

    A selector that consulted anything ambient would make an idea track
    irreproducible, and a track that cannot be replayed cannot be recovered
    after a crash.
    """

    state = snapshot(
        status=IdeaStatus.PROMISING,
        succeeded_stages=frozenset({Stage.DEDUP, Stage.NOVELTY_SCREEN}),
        revision_count=1,
    )
    assert select_stage(state, CONFIG) == select_stage(state, CONFIG)


# ------------------------------------------------------ the type is read --
@pytest.mark.parametrize(
    ("falsifier", "expected"),
    [
        (
            "Exhibit a counterexample, or prove that the orderings coincide.",
            AdjudicationType.MATHEMATICAL,
        ),
        (
            "Measure the median wall-clock time over 30 repetitions on both solvers.",
            AdjudicationType.EMPIRICAL,
        ),
        (
            (
                "Find a prior publication that already reports this; search the "
                "literature for the same result under other terminology."
            ),
            AdjudicationType.NOVELTY_OR_LITERATURE,
        ),
        ("It would feel wrong.", AdjudicationType.UNDETERMINED),
    ],
)
def test_the_adjudication_type_is_read_from_the_idea(
    falsifier: str, expected: AdjudicationType
) -> None:
    """Not asked of the generator, and read from two fields rather than one.

    With a neutral question, the falsifier decides -- which is right, because
    the falsifier is the sentence that says what would settle the thing.
    """

    neutral = version(
        research_question="Is the stated relationship the one that holds?",
        core_idea="The stated relationship is the one that holds.",
        falsifier=falsifier,
    )
    assert classify_adjudication(neutral) == (expected,)


def test_a_cheaper_falsifier_adds_a_requirement_rather_than_removing_one() -> None:
    """The steering mitigation, and the honest limit around it.

    The type *is* derived from model-authored text and *is* steerable: a
    generator that wants a cheaper bar can phrase its falsifier as a literature
    question, and the `scientific_discovery` prompt tells it the sentence is
    read downstream. Reading the question as well, and unioning, means that
    steering can only add. The gate takes the union of the evidence rules, so
    this idea needs an execution *and* retrieved sources.
    """

    steered = version(
        research_question=(
            "Which solver is faster in wall-clock time over 30 repetitions?"
        ),
        core_idea="We measure runtime across seeds and report the median.",
        falsifier=("Find a prior publication reporting this; search the literature."),
    )
    declared = classify_adjudication(steered)
    assert AdjudicationType.EMPIRICAL in declared
    assert AdjudicationType.NOVELTY_OR_LITERATURE in declared

    from research_os.portfolio.gates import EVIDENCE_RULES

    assert EVIDENCE_RULES[AdjudicationType.EMPIRICAL].requires_execution


def test_an_idea_that_says_nothing_about_how_it_would_be_settled_is_undetermined() -> (
    None
):
    assert classify_adjudication(
        version(
            research_question="Is it interesting?",
            core_idea="It might be interesting.",
            falsifier="It would feel wrong.",
        )
    ) == (AdjudicationType.UNDETERMINED,)
