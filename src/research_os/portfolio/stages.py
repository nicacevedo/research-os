"""What happens to an idea next, decided by ordinary Python.

One function, :func:`select_stage`, and it is the reason the track graph is
bounded. It reads the idea's rows and returns the single stage that should run
next, or ``None`` when nothing should. It is **total** -- every reachable
combination of inputs maps to exactly one answer -- and it asks no model, which
is what makes an idea track reproducible enough to test at all.

The order below is the governing principle as control flow:

```text
explore broadly       (the explorers, elsewhere -- the portfolio's job)
kill cheaply          dedup, screen, falsify        cents
sharpen               discover, adjudicate          cents
deepen selectively    evidence, literature audit    dollars
verify independently  review board, meta review     dollars
replicate consequentially                           the most expensive
```

Nothing expensive runs until everything cheap that could have stopped it has
run. A stage that could kill the idea for ten cents is always tried before a
stage that would cost two dollars to confirm it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from research_os.portfolio.config import STAGE_MINIMUM_STATUS, PortfolioConfig
from research_os.portfolio.gates import EVIDENCE_RULES, _executed, _substantive
from research_os.portfolio.models import (
    SEVERITY_ORDER,
    AdjudicationType,
    ExperimentRole,
    ExperimentState,
    IdeaEvidence,
    IdeaExperiment,
    IdeaObjection,
    IdeaReview,
    IdeaStatus,
    IdeaVersion,
    ObjectionTarget,
    ReviewerRole,
    Severity,
    Stage,
)

#: Which statuses a stage may run against, in the order statuses progress. A
#: stage whose minimum the idea has not reached is skipped, which is where
#: "expensive evidence stages require stronger quality gates" is applied.
_STATUS_ORDER: dict[IdeaStatus, int] = {
    IdeaStatus.CANDIDATE: 0,
    IdeaStatus.PROMISING: 1,
    IdeaStatus.INVESTIGATING: 2,
    IdeaStatus.REVIEW: 3,
    IdeaStatus.VALIDATED: 4,
    IdeaStatus.HUMAN_READY: 5,
    # The three that are not on the progression at all. Nothing runs on them.
    IdeaStatus.PARKED: -1,
    IdeaStatus.REJECTED: -1,
    IdeaStatus.SUPERSEDED: -1,
}


@dataclass(frozen=True, slots=True)
class TrackSnapshot:
    """Everything :func:`select_stage` reads, as a value.

    A snapshot rather than a store handle, so the decision is a pure function
    of data a test can construct. The cost is one assembly step in the caller;
    the benefit is that "what would this idea do next" is answerable without a
    database, which is what makes the stage machine's domain enumerable.
    """

    status: IdeaStatus
    version: IdeaVersion
    #: Stages that have succeeded against this *version*, at any basis.
    succeeded_stages: frozenset[Stage] = frozenset()
    #: Stages that have succeeded against the version's *current basis* --
    #: this content, this evidence set, these live reviews.
    #:
    #: Both are needed and they answer different questions. "Has the falsifier
    #: run on this version" decides whether to run it at all; "has the
    #: meta-review run on *this* basis" decides whether the synthesis is still
    #: a synthesis of what exists. Without the second, replication satisfied
    #: the last HUMAN_READY requirement and nothing ever re-evaluated the gate,
    #: so an idea that had earned the top tier sat at VALIDATED forever.
    basis_stages: frozenset[Stage] = frozenset()
    evidence: tuple[IdeaEvidence, ...] = ()
    live_reviews: tuple[IdeaReview, ...] = ()
    open_objections: tuple[IdeaObjection, ...] = ()
    #: How many versions this idea has had. Bounds REVISE.
    revision_count: int = 1
    #: How many reviews it has attracted across every version. Bounds the
    #: revise/review loop in reviewer spend as well as in versions.
    review_count: int = 0
    #: Live descendants in this lineage family. Bounds BRANCH.
    lineage_active: int = 0
    #: Whether the idea's lineage has produced new evidence recently enough to
    #: justify going deeper. Bounds depth-without-evidence.
    depth_without_evidence: int = 0
    #: The measurements this *version* asked for, in whatever state they are.
    #: Read by the evidence branch to decide when the empirical route has been
    #: exhausted, which is the eighth way a portfolio can loop: an experiment
    #: that ran and concluded INCONCLUSIVE leaves the evidence requirement
    #: unmet forever, and without this the stage would be selected again every
    #: tick and do nothing every time.
    experiments: tuple[IdeaExperiment, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking_objections(self) -> tuple[IdeaObjection, ...]:
        return tuple(
            item
            for item in self.open_objections
            if SEVERITY_ORDER[item.severity] >= SEVERITY_ORDER[Severity.CRITICAL]
        )

    @property
    def fatal_objections(self) -> tuple[IdeaObjection, ...]:
        """Standing FATAL objections **to the idea**, which end a track.

        A FATAL objection targeting the *test* is deliberately not here. It is
        fatal to the way the idea proposes to settle itself, and the answer to
        that is to sharpen the idea, not to bury it -- see
        :attr:`fatal_test_objections` and the first dogfood's §R.2.
        """

        return tuple(
            item
            for item in self.open_objections
            if item.severity is Severity.FATAL and item.target is ObjectionTarget.CLAIM
        )

    @property
    def fatal_test_objections(self) -> tuple[IdeaObjection, ...]:
        """Standing FATAL objections to the idea's *test* rather than its claim.

        These do not end a track; they make sharpening the next thing that
        has to happen, and they stay standing until a role other than the one
        that raised them agrees the sharpened version answers them.
        """

        return tuple(
            item
            for item in self.open_objections
            if item.severity is Severity.FATAL and item.target is ObjectionTarget.TEST
        )

    @property
    def is_precise(self) -> bool:
        """Whether the idea says what it is and what would settle it.

        Not a judgement: three non-empty fields. An idea can be precise and
        wrong, which is the good case -- it can then be shown to be wrong.
        """

        return bool(
            self.version.research_question.strip()
            and self.version.mechanism.strip()
            and self.version.falsifier.strip()
        )

    @property
    def missing_review_roles(self) -> tuple[ReviewerRole, ...]:
        present = {item.reviewer_role for item in self.live_reviews}
        return tuple(
            role
            for role in (
                ReviewerRole.METHODOLOGY,
                ReviewerRole.NOVELTY,
                ReviewerRole.SKEPTIC,
            )
            if role not in present
        )

    @property
    def settled_measurement(self) -> IdeaExperiment | None:
        """The primary experiment of this version, once it has been read.

        ``INTERPRETED`` and nothing weaker. An experiment that is still
        running, or that failed operationally, is one the track should come
        back to; one that has been read is one whose answer is in, whatever
        that answer was.
        """

        for item in self.experiments:
            if (
                item.role is ExperimentRole.PRIMARY
                and item.state is ExperimentState.INTERPRETED
            ):
                return item
        return None

    def literature_keys(self) -> set[str]:
        from research_os.portfolio.models import EvidenceKind

        return {
            item.literature_key
            for item in self.evidence
            if item.kind is EvidenceKind.LITERATURE and item.literature_key
        }


def snapshot_for(
    store: Any,
    idea: Any,
    *,
    version: IdeaVersion | None = None,
    max_review_age_seconds: int | None = None,
) -> TrackSnapshot | None:
    """Read everything :func:`select_stage` needs for one idea, in one place.

    **One builder, because two were one too many.** The allocator assembled
    this inline and `researchctl ideas show` assembled it again, and the
    second copy omitted ``experiments``, ``lineage_active`` and
    ``depth_without_evidence``. The consequence was measured on 2026-09-23:
    an idea whose composed experiment had run, been read and concluded
    ``INSUFFICIENT`` was shown as ``next: evidence -- get the evidence this
    kind of idea would be settled by``, while the allocator had correctly
    ended its track. A reader would have waited for work that had already
    happened.

    The allocator's own comment already named the hazard -- "a field present
    in one and absent from the other is two stage machines wearing one name"
    -- and the fix it implies is not a third field on the copy. It is that
    there is no copy.

    Returns ``None`` when the idea has no current version, which is the one
    state in which there is nothing to decide.
    """

    from research_os.portfolio import digests as pdigests

    current = version or store.get_version(idea.idea_id)
    if current is None:
        return None
    # `live_reviews`' prompt-version rule is this build's and is never
    # passed. Its *age* is the caller's, because the sentinel default
    # resolves it from the process-global `load_config()` while a track
    # carries its own -- and two definitions of liveness in one function is
    # the livelock `live_reviews`' own docstring records an independent
    # audit finding: the basis counted a stale review that `select_stage`
    # counted missing, so a track re-ran a stage whose result it refused to
    # see. Passed through, so the basis below and the selector above it
    # cannot disagree.
    reviews = (
        store.live_reviews(idea_id=idea.idea_id)
        if max_review_age_seconds is None
        else store.live_reviews(
            idea_id=idea.idea_id, max_age_seconds=max_review_age_seconds
        )
    )
    evidence = store.list_evidence(idea_id=idea.idea_id, idea_version=current.version)
    # The meta-review's basis, and the only stage `select_stage` reads
    # `basis_stages` for. Computed exactly as `runner` computed it, because
    # it *was* computed there and nowhere else: the allocator's snapshot had
    # no `basis_stages` at all, so `Stage.META_REVIEW not in frozenset()` was
    # always true and the tick would re-buy a meta-review that had already
    # run on this basis, every tick, until the review ceiling. The runner
    # then chose something else, which is the two-stage-machines problem in
    # its most expensive form.
    basis = pdigests.basis_digest(
        content=current.content_digest,
        evidence_ids=[item.evidence_id for item in evidence],
        review_ids=[item.review_id for item in reviews],
        stage_inputs={"stage": str(Stage.META_REVIEW)},
    )
    # The status read now, with everything else, and not the caller's copy:
    # the tick lists its ideas before it snapshots each one, and a worker
    # that finished a stage in between left a snapshot that paired the old
    # status with the new rows -- which selected a stage the idea had moved
    # past and, with nothing permitted for the old status, parked it.
    fresh = store.get_idea(idea.idea_id) or idea
    return TrackSnapshot(
        status=fresh.status,
        version=current,
        succeeded_stages=store.succeeded_stages_for_version(
            idea_id=idea.idea_id, idea_version=current.version
        ),
        basis_stages=store.completed_stages(
            idea_id=idea.idea_id,
            idea_version=current.version,
            basis_digest=basis,
        ),
        evidence=evidence,
        live_reviews=reviews,
        open_objections=store.open_objections(idea_id=idea.idea_id),
        revision_count=store.revision_count(idea.idea_id),
        review_count=store.review_count(idea.idea_id),
        lineage_active=store.lineage_active_counts(idea.project_id).get(
            idea.lineage_root, 0
        ),
        depth_without_evidence=store.depth_without_evidence(idea.idea_id),
        experiments=store.list_experiments(
            idea_id=idea.idea_id, idea_version=current.version
        ),
    )


def _concrete_types(version: IdeaVersion) -> tuple[AdjudicationType, ...]:
    """The declared types that carry an evidence rule of their own.

    ``MIXED`` and ``UNDETERMINED`` carry none, on purpose: the first means "more
    than one kind of work", which is a decomposition rather than a route, and
    the second means "nothing here says". An idea with only these has no
    evidence that would settle it, and must be sharpened rather than
    investigated.
    """

    return tuple(
        item
        for item in version.adjudication_types
        if item not in {AdjudicationType.MIXED, AdjudicationType.UNDETERMINED}
    )


def _evidence_sufficient(snapshot: TrackSnapshot) -> bool:
    """Whether the declared adjudication types' evidence requirements are met.

    Shares :func:`research_os.portfolio.gates._substantive` and ``_executed``
    with the gate rather than restating them. Two definitions of "enough
    evidence" would eventually disagree, and the disagreement would look like
    a track that runs the evidence stage forever against a gate that is already
    satisfied.
    """

    concrete = _concrete_types(snapshot.version)
    if not concrete:
        return False
    for kind in concrete:
        rule = EVIDENCE_RULES[kind]
        if rule.kinds and not _substantive(snapshot.evidence, rule.kinds):
            return False
        if rule.requires_execution and not _executed(
            snapshot.evidence, rule.execution_kinds
        ):
            return False
    return True


def _permits(status: IdeaStatus, stage: Stage) -> bool:
    minimum = STAGE_MINIMUM_STATUS.get(stage)
    if minimum is None:
        return True
    return _STATUS_ORDER[status] >= _STATUS_ORDER[IdeaStatus(minimum)]


def select_stage(
    snapshot: TrackSnapshot, config: PortfolioConfig
) -> tuple[Stage | None, str]:
    """The next stage for this idea, and one line saying why.

    Returns ``(None, reason)`` when the track should end. The reason is not
    decoration: it is what ``researchctl ideas show`` prints under "current
    next action", and a track that stops for a reason nobody can read is a
    track a researcher has to reverse-engineer from a log.
    """

    if _STATUS_ORDER[snapshot.status] < 0:
        return None, f"the idea is {snapshot.status} and takes no further work"

    # --- kill cheaply -----------------------------------------------------
    if Stage.DEDUP not in snapshot.succeeded_stages:
        return Stage.DEDUP, "check whether this idea is already in the portfolio"
    if Stage.NOVELTY_SCREEN not in snapshot.succeeded_stages:
        return Stage.NOVELTY_SCREEN, "check cheaply whether this is already known"
    if Stage.FALSIFY not in snapshot.succeeded_stages:
        return Stage.FALSIFY, "try to kill the idea before anything is spent on it"

    # A fatal objection ends the track here rather than at the gate. The gate
    # would refuse promotion anyway; stopping at this point is what makes the
    # refusal *cheap*, which is the entire difference between a portfolio that
    # kills ideas and one that merely declines to promote them.
    if snapshot.fatal_objections:
        return None, (
            f"a fatal objection stands: {snapshot.fatal_objections[0].summary[:120]}"
        )

    # A fatal objection to the *test* is the one kind that does not end the
    # track. It makes sharpening the next thing that must happen, and it is
    # bounded by the same revision ceiling everything else is -- past it, an
    # idea whose test nobody can fix has no route left and stops here rather
    # than being sharpened forever.
    if snapshot.fatal_test_objections:
        if snapshot.revision_count >= config.bounds.max_revisions_per_idea:
            return None, (
                "the falsifier is fatal to this idea's test, and the revision "
                f"bound of {config.bounds.max_revisions_per_idea} is spent: "
                f"{snapshot.fatal_test_objections[0].summary[:120]}"
            )
        return Stage.DISCOVER, (
            "the question survives but its test does not; rewrite the test: "
            f"{snapshot.fatal_test_objections[0].summary[:120]}"
        )

    # --- sharpen ----------------------------------------------------------
    #
    # The test is "is it precise", plus "has it ever been sharpened", and
    # deliberately *not* "has discover run against this version". The version
    # form is what an earlier draft had, and it loops: discover appends a
    # version, the new version has no succeeded stages, so discover is selected
    # again, forever -- three cheap calls and one expensive one per pass until
    # the revision bound runs out. The integration test caught it on its first
    # run.
    #
    # `revision_count` counts versions produced by discover, so it is zero
    # exactly until the idea has been sharpened once.
    if not snapshot.is_precise or snapshot.revision_count == 0:
        if snapshot.revision_count > config.bounds.max_revisions_per_idea:
            return None, (
                f"this idea has been rewritten {snapshot.revision_count} times and "
                f"is still not precise; the revision bound is "
                f"{config.bounds.max_revisions_per_idea}"
            )
        return Stage.DISCOVER, "make the idea precise enough to be settled"
    if not snapshot.version.adjudication_types:
        return Stage.ADJUDICATE, "read the falsifier to decide what would settle this"
    # An idea whose only declared types are UNDETERMINED or MIXED has no
    # *concrete* evidence rule, so no amount of evidence would ever satisfy
    # one. The first version of this function let both fall through to the
    # evidence stage, where `_evidence_sufficient` was permanently false and
    # the stage would have been selected forever -- a loop that each individual
    # rule looked like progress through. The termination test found it.
    #
    # MIXED is split rather than sharpened: the architecture's §8 says a mixed
    # idea becomes component sub-ideas with SPECIALIZES edges, and the role
    # that can do that is `scientific_discovery`.
    if not _concrete_types(snapshot.version):
        if snapshot.revision_count > config.bounds.max_revisions_per_idea:
            return None, (
                "the falsifier still does not say how this would be settled, and "
                "the revision bound is spent"
            )
        if AdjudicationType.MIXED in snapshot.version.adjudication_types:
            return Stage.DISCOVER, (
                "this is settled by more than one kind of work; split it into "
                "components that each have their own falsifier"
            )
        return Stage.DISCOVER, (
            "the falsifier does not say what kind of work would settle this; sharpen it"
        )

    # --- deepen selectively ----------------------------------------------
    # The audit before the evidence stage, and not only because it is
    # cheaper. For a literature-adjudicated idea the audit *is* the evidence,
    # so running the evidence stage first would either duplicate it under
    # another name or refuse. For every other type the audit is required for
    # VALIDATED anyway, and doing the $1.50 stage before the $2.50 one is the
    # same cheapest-first rule the rest of this function follows.
    if _permits(snapshot.status, Stage.LITERATURE_AUDIT):
        keys = len(snapshot.literature_keys())
        needed = config.thresholds.novelty_min_sources
        if Stage.LITERATURE_AUDIT not in snapshot.succeeded_stages:
            return Stage.LITERATURE_AUDIT, (
                "establish what is already known, from retrieved sources rather "
                "than from recollection"
            )
        if keys < needed:
            # The audit ran and the corpus did not have enough in it. Running
            # it again asks the same question of the same index and gets the
            # same answer, so this stops -- at $1.50 a time, unattended, the
            # first version would have asked forever. Found by the first test
            # that drove an idea all the way through.
            return None, (
                f"the novelty case rests on {keys} retrieved source(s) and "
                f"{needed} are required; this index does not have them. The idea "
                f"is not refuted -- its novelty cannot be established here."
            )

    if not _evidence_sufficient(snapshot) and _permits(snapshot.status, Stage.EVIDENCE):
        if snapshot.depth_without_evidence > config.bounds.max_depth_without_evidence:
            return None, (
                "this lineage has gone "
                f"{snapshot.depth_without_evidence} levels deep without new "
                "evidence; deepening on reasoning alone stops here"
            )
        settled = snapshot.settled_measurement
        if settled is not None:
            # The measurement was taken and read, and what it produced does
            # not meet this idea's evidence requirement. Selecting the stage
            # again would find the same interpreted experiment and conclude
            # the same thing -- at no cost, forever, while the portfolio
            # reports the idea as active. The eighth loop, and the reason
            # `TrackSnapshot.experiments` exists.
            #
            # Not a refutation. An INCONCLUSIVE or INSUFFICIENT measurement
            # leaves the question open, and the reason says so rather than
            # retiring the idea, because `select_stage` decides what runs
            # next and never what an idea *is*.
            return None, (
                f"the experiment this idea asked for ran and was read "
                f"({settled.conclusion}); it does not meet the evidence this "
                f"kind of idea requires, and re-running the same measurement "
                f"is not a different answer"
            )
        return Stage.EVIDENCE, "get the evidence this kind of idea would be settled by"

    # --- verify independently --------------------------------------------
    if snapshot.missing_review_roles and _permits(snapshot.status, Stage.REVIEW_BOARD):
        if snapshot.review_count >= config.bounds.max_reviews_per_idea:
            return None, (
                f"this idea has attracted {snapshot.review_count} reviews, which is "
                f"the ceiling; a revise/review loop stops here"
            )
        missing = ", ".join(str(role) for role in snapshot.missing_review_roles)
        return Stage.REVIEW_BOARD, f"obtain the missing reviews: {missing}"

    if snapshot.blocking_objections:
        if snapshot.revision_count > config.bounds.max_revisions_per_idea:
            return None, (
                "an objection at CRITICAL stands and the revision bound is spent"
            )
        return Stage.DISCOVER, (
            f"answer a standing objection: "
            f"{snapshot.blocking_objections[0].summary[:120]}"
        )

    if Stage.META_REVIEW not in snapshot.basis_stages and _permits(
        snapshot.status, Stage.META_REVIEW
    ):
        if snapshot.review_count >= config.bounds.max_reviews_per_idea:
            return None, (
                f"this idea has attracted {snapshot.review_count} reviews, which "
                f"is the ceiling"
            )
        return Stage.META_REVIEW, (
            "synthesise what is now on the record into a disposition"
            if Stage.META_REVIEW in snapshot.succeeded_stages
            else "synthesise the reviews into a disposition"
        )

    # --- replicate consequentially ---------------------------------------
    if Stage.REPLICATE not in snapshot.succeeded_stages and _permits(
        snapshot.status, Stage.REPLICATE
    ):
        return Stage.REPLICATE, "verify the result a second way before a person sees it"

    # --- branch -----------------------------------------------------------
    if Stage.BRANCH not in snapshot.succeeded_stages and _permits(
        snapshot.status, Stage.BRANCH
    ):
        if snapshot.lineage_active >= config.bounds.max_active_per_lineage:
            return None, (
                f"this lineage already has {snapshot.lineage_active} live ideas, "
                f"which is the ceiling"
            )
        return Stage.BRANCH, "open the child directions this result suggests"

    return None, "every stage this idea's state calls for has run"


def waits_only_for_lineage_room(
    snapshot: TrackSnapshot, config: PortfolioConfig
) -> bool:
    """Whether the one thing between this idea and its next stage is the ceiling.

    Asked by construction rather than by reading the reason string: the same
    snapshot with an empty lineage would branch. The lineage ceiling is a
    bound on how much runs at once, not a finding about the idea, so an idea
    it stops is owed the stage when the lineage has room again.
    """

    if snapshot.lineage_active < config.bounds.max_active_per_lineage:
        return False
    stage, _ = select_stage(replace(snapshot, lineage_active=0), config)
    return stage is Stage.BRANCH


#: The stages that may run at all, in the order :func:`select_stage` considers
#: them. Exported so the test that enumerates the machine's domain reads the
#: same order the function uses, rather than a copy of it.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.DEDUP,
    Stage.NOVELTY_SCREEN,
    Stage.FALSIFY,
    Stage.DISCOVER,
    Stage.ADJUDICATE,
    Stage.EVIDENCE,
    Stage.LITERATURE_AUDIT,
    Stage.REVIEW_BOARD,
    Stage.META_REVIEW,
    Stage.REPLICATE,
    Stage.BRANCH,
)


def classify_adjudication(version: IdeaVersion) -> tuple[AdjudicationType, ...]:
    """What kind of work would settle this idea. Read, not asked.

    Delegates to :func:`research_os.runtime.adjudication.classify`, which
    weights the falsification clause above the statement because *the falsifier
    is the sentence in which someone already wrote down what would settle the
    thing*.

    **And it is read twice, from two different fields, and the answers are
    unioned.** That is the part an independent security review argued for, and
    it is worth being precise about what it does and does not buy.

    It does not make the type un-steerable. The falsifier is written by a
    model, the classifier is keyword scoring, and the ``scientific_discovery``
    prompt tells the model outright that what kind of work would settle the
    idea is read from that sentence. A generator that wants a cheaper bar can
    phrase its falsifier as a literature question, and an earlier version of
    this function -- which read the falsifier alone and returned a
    single-element tuple -- would have given it one.

    What the union buys is that steering can only ever *add* requirements. The
    gates take the union of the evidence rules over every declared type
    (:func:`research_os.portfolio.gates._rules_for`), so an idea whose question
    reads as empirical and whose falsifier reads as a literature search must
    satisfy both -- it needs an execution *and* retrieved sources. Making the
    falsifier cheaper no longer removes what the question demands.

    ``UNDETERMINED`` is dropped when anything concrete survives, and kept when
    nothing does: a target this module does not understand behaves exactly as
    it did before the module existed, which is what
    ``runtime/adjudication.py`` means by conservative.
    """

    from research_os.runtime.adjudication import classify

    from_falsifier = classify(falsification=version.falsifier).kind
    from_statement = classify(
        statement=f"{version.research_question}\n{version.core_idea}"
    ).kind
    declared = {
        AdjudicationType(item.value) for item in (from_falsifier, from_statement)
    }
    concrete = declared - {AdjudicationType.UNDETERMINED}
    if not concrete:
        return (AdjudicationType.UNDETERMINED,)
    # MIXED alongside a concrete type says nothing the concrete one does not.
    if len(concrete) > 1:
        concrete -= {AdjudicationType.MIXED}
    return tuple(sorted(concrete, key=lambda item: item.value))


#: The old name, kept for one release because the report and two documents
#: cite it. It reads only the falsifier, which is what made the type steerable.
classify_from_falsifier = classify_adjudication


def stage_reason_lines(
    snapshot: TrackSnapshot, config: PortfolioConfig
) -> Sequence[str]:
    """A short human-readable account of where this idea is. For the CLI."""

    stage, why = select_stage(snapshot, config)
    lines = [f"next: {stage or 'nothing'} -- {why}"]
    if snapshot.open_objections:
        lines.append(
            f"standing objections: {len(snapshot.open_objections)} "
            f"({len(snapshot.blocking_objections)} blocking)"
        )
    lines.append(
        f"reviews live on this version: {len(snapshot.live_reviews)} of 3; "
        f"evidence rows: {len(snapshot.evidence)}"
    )
    return lines
