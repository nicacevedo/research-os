"""The deterministic quality gates, and the independence classifier.

This module is the reason a meta-reviewer cannot promote an idea by being
persuasive. It reads rows -- evidence, reviews, objections -- and returns what
those rows permit. It never reads prose and never asks a model anything.

What it is *not*, stated first because the distinction is the whole honesty of
this layer: **nothing here checks whether an idea is true.** Every requirement
below is a check that the right *kinds* of evidence exist, that enough separate
readings happened, and that nothing is standing unanswered. A system of this
shape can be satisfied by work that is thorough and wrong. That is why the top
tier is called ``HUMAN_READY`` and not ``CORRECT``, and why §12 of the
architecture makes the Curator stamp every rendered page with what was actually
executed, retrieved and reviewed rather than with the tier alone.

Three things it *does* make impossible, and each one is a row-level check
rather than a prompt:

- promotion on model memory. A literature evidence row with no retrieved source
  key is refused by the database, so "the deep audit" cannot be satisfied by
  recollection;
- promotion of a universally quantified claim on arithmetic. A numerical
  witness is storable only as ``CONSISTENT_WITH``, and this module refuses to
  let ``CONSISTENT_WITH`` alone satisfy "substantive evidence" or replication;
- an idea choosing its own evidentiary bar. The requirements below are keyed by
  :class:`~research_os.runtime.adjudication.AdjudicationKind`, which is computed
  from the falsification clause by ordinary Python.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from research_os.errors import ResearchOSError
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.models import (
    BLOCKING_SEVERITIES,
    INDEPENDENT_REVIEW_ROLES,
    SEVERITY_ORDER,
    TIER_ORDER,
    AdjudicationType,
    Disposition,
    EvidenceKind,
    EvidenceStrength,
    IdeaEvidence,
    IdeaObjection,
    IdeaReview,
    IdeaVersion,
    QualityTier,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.runtime.interfaces import Independence


class SelfReviewError(ResearchOSError):
    """Raised when a review's model call *is* the call that produced the work.

    Not a degradation, and deliberately not representable as one. Every other
    shortfall in independence is something a deployment can genuinely be short
    of -- one provider family, one model. This one can only arise from a code
    path that handed the origin's own invocation to the gate, and a defect that
    degrades quietly is a defect that ships.
    """


#: Verdicts that do not, on their own, block promotion.
#:
#: ``PASS_WITH_OBJECTIONS`` is here and ``REVISE``/``REJECT``/``INCONCLUSIVE``
#: are not. An objection is handled by the objection machinery -- it stands
#: until somebody else agrees it was answered -- so a reviewer who passes while
#: recording one has said something the gate can act on. A reviewer who says
#: "revise" or "I could not tell" has not endorsed anything, and counting it
#: towards "three reviews" would make the tier mean "three reviewers looked"
#: rather than "three reviewers did not object".
NON_NEGATIVE_VERDICTS: frozenset[ReviewVerdict] = frozenset(
    {ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_OBJECTIONS}
)


@dataclass(frozen=True, slots=True)
class EvidenceRule:
    """What ``VALIDATED`` requires of the evidence, for one adjudication type."""

    description: str
    #: At least one supporting or contradicting row of one of these kinds.
    kinds: frozenset[EvidenceKind]
    #: At least one row naming an ``external_jobs`` id. This is what "machine
    #: checkable" means concretely: something was executed and its execution is
    #: addressable, rather than a model having written a derivation.
    requires_execution: bool = False
    #: Which kinds of evidence may carry that execution. Separate from
    #: :attr:`kinds`, because for a mathematical idea they are *different*
    #: kinds: the substance is a derivation and the executed check is a search.
    #: Collapsing the two would have demanded an executed derivation, which is
    #: not a thing, and the positive-control test found exactly that.
    execution_kinds: frozenset[EvidenceKind] = frozenset()
    #: Distinct retrieved literature keys required.
    literature_keys: int = 0


@dataclass(frozen=True, slots=True)
class ReplicationRule:
    """What ``HUMAN_READY`` requires as second-line verification."""

    description: str
    kinds: frozenset[EvidenceKind]
    requires_execution: bool = False
    #: The replicating evidence must come from a different model call than the
    #: one that produced the original. "A second opinion from the same source"
    #: is what this word is usually misused for.
    requires_distinct_source: bool = True
    #: Literature keys the replication must add that the original did not have.
    new_literature_keys: int = 0


#: What each adjudication type must show before ``VALIDATED``.
#:
#: The union over every type an idea declares applies, so declaring a second
#: type adds requirements and never removes them. That closes the route where a
#: generator classifies an empirical claim as a literature question and routes
#: past execution entirely.
EVIDENCE_RULES: dict[AdjudicationType, EvidenceRule] = {
    AdjudicationType.MATHEMATICAL: EvidenceRule(
        description=(
            "a derivation, plus something a machine actually ran: an executed "
            "counterexample search with its own job. A model's prose derivation "
            "is a document, not a check."
        ),
        kinds=frozenset({EvidenceKind.DERIVATION}),
        requires_execution=True,
        execution_kinds=frozenset(
            {EvidenceKind.NUMERICAL, EvidenceKind.EXPERIMENT, EvidenceKind.CODE}
        ),
    ),
    AdjudicationType.EMPIRICAL: EvidenceRule(
        description=(
            "an experiment naming the execution it is of, interpreted against "
            "criteria fixed before the result"
        ),
        kinds=frozenset({EvidenceKind.EXPERIMENT}),
        requires_execution=True,
        execution_kinds=frozenset({EvidenceKind.EXPERIMENT}),
    ),
    AdjudicationType.NOVELTY_OR_LITERATURE: EvidenceRule(
        description="retrieved primary sources, not recollection of them",
        kinds=frozenset({EvidenceKind.LITERATURE}),
        literature_keys=1,
    ),
    AdjudicationType.DIAGNOSTIC: EvidenceRule(
        description=(
            "an observation of this implementation, addressable as an artifact. "
            "It is a fact about the program and never on its own a reason to "
            "close a scientific target."
        ),
        kinds=frozenset({EvidenceKind.INSPECTION, EvidenceKind.CODE}),
    ),
    AdjudicationType.MIXED: EvidenceRule(
        description="the union of the requirements of its components",
        kinds=frozenset(),
    ),
    AdjudicationType.UNDETERMINED: EvidenceRule(
        description=(
            "nothing, because an idea whose falsifier says nothing about how it "
            "would be settled cannot be validated. Sharpen the falsifier."
        ),
        kinds=frozenset(),
    ),
}

REPLICATION_RULES: dict[AdjudicationType, ReplicationRule] = {
    AdjudicationType.MATHEMATICAL: ReplicationRule(
        description=(
            "an independent derivation that did not read the first, or a second "
            "executed search under a different parameterisation"
        ),
        kinds=frozenset({EvidenceKind.REPLICATION}),
        requires_execution=True,
    ),
    AdjudicationType.EMPIRICAL: ReplicationRule(
        description=(
            "a second execution with its own specification digest, differing in "
            "seed or in implementation"
        ),
        kinds=frozenset({EvidenceKind.REPLICATION}),
        requires_execution=True,
    ),
    AdjudicationType.NOVELTY_OR_LITERATURE: ReplicationRule(
        description=(
            "a second terminology path: a retrieval by a different call that "
            "finds sources the first did not"
        ),
        kinds=frozenset({EvidenceKind.LITERATURE, EvidenceKind.REPLICATION}),
        new_literature_keys=1,
    ),
    AdjudicationType.DIAGNOSTIC: ReplicationRule(
        description="an independent checker or an alternate implementation",
        kinds=frozenset({EvidenceKind.REPLICATION, EvidenceKind.CODE}),
    ),
    AdjudicationType.MIXED: ReplicationRule(
        description="the union of the requirements of its components",
        kinds=frozenset(),
    ),
    AdjudicationType.UNDETERMINED: ReplicationRule(
        description="unreachable; this type cannot reach VALIDATED",
        kinds=frozenset(),
    ),
}


@dataclass(frozen=True, slots=True)
class GateResult:
    """What the rows permit, and what is missing if they permit less."""

    #: The highest tier this idea's rows support right now.
    tier: QualityTier
    #: The tier that was asked about.
    requested: QualityTier
    #: Every requirement of ``requested`` that is not met, in the researcher's
    #: words. Empty exactly when ``passed``.
    unmet: tuple[str, ...] = ()
    #: How many distinct ``(provider family, model)`` pairs produced the live
    #: independent reviews. **One means the three reviews are three samples
    #: from one model**, and no rendered output may call that independent.
    board_independence: int = 0
    #: Things a person should know that do not block anything.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.unmet

    def at_least(self, tier: QualityTier) -> bool:
        return TIER_ORDER[self.tier] >= TIER_ORDER[tier]


# ------------------------------------------------------------ independence --
def classify_independence(
    *,
    origin_call_id: str | None,
    review_call_id: str | None,
    origin_provider_family: str | None,
    review_provider_family: str,
    origin_model: str | None,
    review_model: str | None,
    frozen_packet: bool,
) -> Independence:
    """The separation achieved between a producer and one of its reviewers.

    Reuses :class:`research_os.runtime.interfaces.Independence` rather than
    inventing a second vocabulary for the same idea. The question is different
    from ``model_calls.independence`` -- that one is per independence group,
    this one is against the *version's origin call* -- which is why the column
    storing it is named ``independence_vs_origin``.

    Raises rather than returning for the one case that cannot be a deployment
    limitation.
    """

    if review_call_id is not None and review_call_id == origin_call_id:
        raise SelfReviewError(
            f"model call {review_call_id} both produced this idea version and "
            f"reviewed it. That is not a weak review; it is a defect in whatever "
            f"assembled the review request."
        )
    if origin_provider_family is None and origin_model is None:
        # The origin call could not be found, so nothing about the separation
        # is known. ``NONE`` rather than ``DIFFERENT_CONTEXT``: an unknown
        # origin is not a weaker separation, it is no evidence of one -- and
        # this value is printed into the researcher's repository next to the
        # word VALIDATED. `routing.py` records what false independence
        # provenance cost the last time it happened.
        return Independence.NONE
    if origin_provider_family and review_provider_family != origin_provider_family:
        return Independence.DIFFERENT_FAMILY
    if origin_model and review_model and review_model != origin_model:
        return Independence.DIFFERENT_MODEL
    return Independence.DIFFERENT_CONTEXT if frozen_packet else Independence.NONE


def board_independence(reviews: Iterable[IdeaReview]) -> int:
    """Distinct ``(provider family, model)`` pairs across the review board.

    The number the architecture's §10 exists to surface. Three reviewer *roles*
    are not three independent reviewers: three samples from one model reading
    one packet are correlated in exactly the dimension that matters, which is
    susceptibility to confident prose. Counting the pairs is the only honest
    thing to report, and reporting it is what stops the word "independent"
    being used where it is not true.
    """

    return len(
        {
            (review.provider_family, review.model)
            for review in reviews
            if review.reviewer_role in INDEPENDENT_REVIEW_ROLES
        }
    )


# ------------------------------------------------------------------ gates --
def _distinct_literature_keys(evidence: Sequence[IdeaEvidence]) -> set[str]:
    return {
        item.literature_key
        for item in evidence
        if item.kind is EvidenceKind.LITERATURE and item.literature_key
    }


def _substantive(
    evidence: Sequence[IdeaEvidence], kinds: frozenset[EvidenceKind]
) -> bool:
    """Whether any evidence of these kinds actually bears on the proposition.

    ``CONSISTENT_WITH`` does not count here, and that single exclusion is what
    stops a universally quantified claim reaching the top tier on two finite
    computations. A witness that failed to find a counterexample is worth
    recording and is not a reason to believe the theorem.
    """

    return any(
        item.kind in kinds
        and item.strength in {EvidenceStrength.SUPPORTS, EvidenceStrength.CONTRADICTS}
        for item in evidence
    )


def _executed(evidence: Sequence[IdeaEvidence], kinds: frozenset[EvidenceKind]) -> bool:
    return any(item.kind in kinds and item.job_id for item in evidence)


def _rules_for(
    version: IdeaVersion,
) -> tuple[
    tuple[AdjudicationType, ...], tuple[EvidenceRule, ...], tuple[ReplicationRule, ...]
]:
    """The union of rules over every declared adjudication type.

    ``MIXED`` and ``UNDETERMINED`` contribute no rules of their own;
    ``UNDETERMINED`` is handled separately, because it is the one type that
    makes ``VALIDATED`` unreachable rather than merely demanding.
    """

    declared = tuple(version.adjudication_types) or (AdjudicationType.UNDETERMINED,)
    concrete = tuple(
        item
        for item in declared
        if item not in {AdjudicationType.MIXED, AdjudicationType.UNDETERMINED}
    )
    return (
        declared,
        tuple(EVIDENCE_RULES[item] for item in concrete),
        tuple(REPLICATION_RULES[item] for item in concrete),
    )


def _promising_unmet(
    version: IdeaVersion,
    objections: Sequence[IdeaObjection],
    succeeded_stages: frozenset[Stage],
) -> list[str]:
    unmet: list[str] = []
    if not version.research_question.strip():
        unmet.append("no research question")
    if not version.mechanism.strip():
        unmet.append("no explicit mechanism")
    if not version.falsifier.strip():
        unmet.append("no falsifier: nothing is stated that would settle this")
    if Stage.NOVELTY_SCREEN not in succeeded_stages:
        unmet.append("the cheap novelty screen has not run")
    fatal = [
        item for item in objections if item.open and item.severity is Severity.FATAL
    ]
    if fatal:
        unmet.append(
            f"{len(fatal)} unanswered fatal objection(s): {fatal[0].summary[:120]}"
        )
    return unmet


def _validated_unmet(
    version: IdeaVersion,
    reviews: Sequence[IdeaReview],
    objections: Sequence[IdeaObjection],
    evidence: Sequence[IdeaEvidence],
    succeeded_stages: frozenset[Stage],
    config: PortfolioConfig,
) -> list[str]:
    unmet: list[str] = []
    _declared, evidence_rules, _ = _rules_for(version)

    if not evidence_rules:
        # No declared type carries an evidence rule, so nothing would satisfy
        # one. UNDETERMINED means the falsifier said nothing; MIXED means it
        # said "several kinds", which is a decomposition rather than a route.
        # Either way there is no evidence that would settle this idea as
        # stated, and saying so is more useful than listing what is missing.
        unmet.append(
            "the falsifier does not name one kind of work that would settle "
            "this, so no evidence would. Sharpen it, or split the idea into "
            "components that each have their own."
        )

    for role in INDEPENDENT_REVIEW_ROLES:
        found = [item for item in reviews if item.reviewer_role is role]
        if not found:
            unmet.append(f"no live {role} review of the current version")
        elif not any(item.verdict in NON_NEGATIVE_VERDICTS for item in found):
            verdicts = ", ".join(sorted({str(item.verdict) for item in found}))
            unmet.append(f"the {role} did not endorse this ({verdicts})")

    for rule in evidence_rules:
        if rule.kinds and not _substantive(evidence, rule.kinds):
            kinds = ", ".join(sorted(str(item) for item in rule.kinds))
            unmet.append(f"no substantive {kinds} evidence: {rule.description}")
        if rule.requires_execution and not _executed(evidence, rule.execution_kinds):
            unmet.append(
                f"nothing was executed: {rule.description}. A derivation or a "
                f"design is not a check."
            )

    keys = _distinct_literature_keys(evidence)
    needed = config.thresholds.novelty_min_sources
    if Stage.LITERATURE_AUDIT not in succeeded_stages:
        unmet.append("the deep literature audit has not run")
    if len(keys) < needed:
        unmet.append(
            f"the novelty case rests on {len(keys)} retrieved source(s); "
            f"{needed} are required. Whether something is already known is a "
            f"question about the published record."
        )

    blocking = [
        item
        for item in objections
        if item.open and item.severity in BLOCKING_SEVERITIES
    ]
    if blocking:
        worst = max(blocking, key=lambda item: SEVERITY_ORDER[item.severity])
        unmet.append(
            f"{len(blocking)} unanswered objection(s) at CRITICAL or above: "
            f"{worst.summary[:120]}"
        )
    return unmet


def _human_ready_unmet(
    version: IdeaVersion,
    reviews: Sequence[IdeaReview],
    objections: Sequence[IdeaObjection],
    evidence: Sequence[IdeaEvidence],
) -> list[str]:
    unmet: list[str] = []
    _, _, replication_rules = _rules_for(version)

    major = [
        item
        for item in objections
        if item.open and SEVERITY_ORDER[item.severity] >= SEVERITY_ORDER[Severity.MAJOR]
    ]
    if major:
        unmet.append(
            f"{len(major)} objection(s) at MAJOR or above are unanswered: "
            f"{major[0].summary[:120]}"
        )

    # Whose work the replication must be independent of. Nulls are dropped:
    # an unknown call cannot make anything distinct from it, and the check
    # below separately requires the *replication* to name its own call.
    origin_calls = {
        call
        for call in (
            {version.origin_call_id}
            | {
                item.source_call_id
                for item in evidence
                if item.kind is not EvidenceKind.REPLICATION
            }
        )
        if call
    }
    for rule in replication_rules:
        if not _replication_met(rule, evidence, origin_calls):
            unmet.append(f"no second-line verification: {rule.description}")
    if not replication_rules:
        unmet.append(
            "no adjudication type with a defined replication rule, so there is "
            "nothing that would count as verifying this a second way"
        )

    if not version.open_uncertainties:
        unmet.append(
            "no limitations are stated. An idea offered for a researcher's "
            "attention with nothing open about it has not been examined."
        )
    if not version.why_it_matters.strip():
        unmet.append("no scientific significance is stated")
    del reviews
    return unmet


def _replication_met(
    rule: ReplicationRule,
    evidence: Sequence[IdeaEvidence],
    origin_calls: set[str | None],
) -> bool:
    """Whether the second-line verification this type requires actually exists.

    Two shapes, because "a second time" means different things:

    **A second execution or an independent reconstruction.** A ``REPLICATION``
    row from a call that is not among the calls that produced the original
    work, with a job when the type demands one.

    **A second terminology path**, for a literature-adjudicated idea. A
    novelty claim is a claim about absence, and the way to verify an absence is
    to look again with different words. So the test is over the *literature*
    rows: at least two distinct retrieval calls, and the later ones finding
    keys the first did not.

    The first version of this function applied the execution shape to both, and
    the literature case was unsatisfiable by construction: every literature
    row's own call went into ``origin_calls``, so every literature row was
    filtered out of the candidates, so a second audit could never count. An
    independent test audit found it -- and found that no test could have,
    because the suite's only passing gate control used an adjudication type
    this build cannot produce evidence for.
    """

    if rule.new_literature_keys:
        return _second_terminology_path(evidence, rule.new_literature_keys)

    candidates = [item for item in evidence if item.kind in rule.kinds]
    if rule.requires_distinct_source:
        # A replication with no recorded call cannot demonstrate that it is
        # independent of anything, so it does not count. Conservative in the
        # direction that matters: the cost of being wrong here is one more
        # verification run, and the cost of being wrong the other way is
        # calling a worker agreeing with itself a replication.
        candidates = [
            item
            for item in candidates
            if item.source_call_id and item.source_call_id not in origin_calls
        ]
    if rule.requires_execution:
        candidates = [item for item in candidates if item.job_id]
    return bool(candidates)


def _second_terminology_path(
    evidence: Sequence[IdeaEvidence], minimum_new_keys: int
) -> bool:
    """Whether a later retrieval found sources the first one did not.

    Grouped by the call that produced each row and ordered by when it was
    written, so "the first search" is a fact about the record rather than a
    label somebody applied.
    """

    literature = sorted(
        (
            item
            for item in evidence
            if item.kind is EvidenceKind.LITERATURE
            and item.literature_key
            and item.source_call_id
        ),
        key=lambda item: (item.created_at, item.evidence_id),
    )
    if not literature:
        return False
    calls: list[str] = []
    for item in literature:
        if item.source_call_id not in calls:
            calls.append(str(item.source_call_id))
    if len(calls) < 2:
        return False
    first = calls[0]
    original = {
        item.literature_key for item in literature if item.source_call_id == first
    }
    later = {item.literature_key for item in literature if item.source_call_id != first}
    return len(later - original) >= minimum_new_keys


def evaluate(
    *,
    version: IdeaVersion,
    live_reviews: Sequence[IdeaReview],
    objections: Sequence[IdeaObjection],
    evidence: Sequence[IdeaEvidence],
    succeeded_stages: frozenset[Stage],
    config: PortfolioConfig,
    requested: QualityTier = QualityTier.HUMAN_READY,
) -> GateResult:
    """What this idea's rows permit.

    Cumulative: ``VALIDATED`` includes ``PROMISING``'s requirements and
    ``HUMAN_READY`` includes both. A failure at a lower tier is reported at
    every higher one, so ``unmet`` reads as the complete list of what is
    missing rather than as the first thing checked.
    """

    promising = _promising_unmet(version, objections, succeeded_stages)
    validated = promising + _validated_unmet(
        version, live_reviews, objections, evidence, succeeded_stages, config
    )
    human_ready = validated + _human_ready_unmet(
        version, live_reviews, objections, evidence
    )

    if not human_ready:
        tier = QualityTier.HUMAN_READY
    elif not validated:
        tier = QualityTier.VALIDATED
    elif not promising:
        tier = QualityTier.PROMISING
    else:
        tier = QualityTier.NONE

    unmet_for = {
        QualityTier.NONE: [],
        QualityTier.PROMISING: promising,
        QualityTier.VALIDATED: validated,
        QualityTier.HUMAN_READY: human_ready,
    }[requested]

    independence = board_independence(live_reviews)
    notes: list[str] = []
    if independence <= 1 and live_reviews:
        notes.append(
            "every review of this idea was produced by one model. That is not "
            "independent review, and nothing rendered from this may call it so."
        )
    if any(
        item.kind is EvidenceKind.NUMERICAL
        and item.strength is EvidenceStrength.CONSISTENT_WITH
        for item in evidence
    ):
        notes.append(
            "a numerical witness is recorded. It is consistent with the "
            "proposition; it does not establish it."
        )
    return GateResult(
        tier=tier,
        requested=requested,
        unmet=tuple(dict.fromkeys(unmet_for)),
        board_independence=independence,
        notes=tuple(notes),
    )


#: What a meta-reviewer recommendation maps onto, when the gate allows it.
_TIER_FOR_DISPOSITION: Mapping[Disposition, QualityTier] = {
    Disposition.PROMISING: QualityTier.PROMISING,
    Disposition.VALIDATED: QualityTier.VALIDATED,
    Disposition.HUMAN_READY: QualityTier.HUMAN_READY,
}


def permit(recommendation: Disposition, result: GateResult) -> Disposition:
    """Take the minimum of what was recommended and what the rows allow.

    The one function that makes the meta-reviewer advisory about promotion. An
    ungated disposition -- REJECT, PARK, REVISE, DEEPEN, BRANCH, DUPLICATE,
    CONTINUE -- passes through unchanged: killing or pausing an idea needs no
    ceremony, and that asymmetry is deliberate.

    A gated one is lowered to the highest tier the gate permits, or to
    ``CONTINUE`` when it permits none. It is never raised: a meta-reviewer that
    recommends PROMISING for something already VALIDATED does not demote it,
    because demotion is a change of status and this function decides a
    *disposition*.
    """

    wanted = _TIER_FOR_DISPOSITION.get(recommendation)
    if wanted is None:
        return recommendation
    if TIER_ORDER[result.tier] >= TIER_ORDER[wanted]:
        return recommendation
    for disposition, tier in (
        (Disposition.HUMAN_READY, QualityTier.HUMAN_READY),
        (Disposition.VALIDATED, QualityTier.VALIDATED),
        (Disposition.PROMISING, QualityTier.PROMISING),
    ):
        if (
            TIER_ORDER[tier] <= TIER_ORDER[result.tier]
            and TIER_ORDER[tier] < TIER_ORDER[wanted]
        ):
            return disposition
    return Disposition.CONTINUE
