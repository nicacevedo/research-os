"""Typed portfolio records, and the enums the database checks against.

Same contract as :mod:`research_os.runtime.models`: every status enum here has
a matching ``check`` constraint in the SQL, the duplication is deliberate, and
``tests/test_portfolio_schema.py`` reads the constraints out of the live
catalog and asserts they hold exactly these values in both directions.

These are read models. Nothing here writes; :mod:`research_os.portfolio.store`
does that and hands back instances of these.

The class is ``PortfolioIdea`` and not ``Idea``. ``research_os.models.Idea`` is
a capsule object -- scientific state a person owns -- and a module that imports
both would have to rename one at the import site, which is exactly the kind of
ambiguity the authority model cannot afford. See
`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` §3.1a.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_os.runtime.adjudication import AdjudicationKind
from research_os.runtime.interfaces import Independence


# ------------------------------------------------------------- lifecycle --
class IdeaStatus(StrEnum):
    """Where an idea is *scientifically*.

    Orthogonal to :class:`OperationalState`. Conflating them is how a provider
    outage comes to read as a scientific rejection, which
    ``docs/RUNTIME.md`` §17 records actually happening once.
    """

    CANDIDATE = "CANDIDATE"
    PROMISING = "PROMISING"
    INVESTIGATING = "INVESTIGATING"
    REVIEW = "REVIEW"
    VALIDATED = "VALIDATED"
    HUMAN_READY = "HUMAN_READY"
    PARKED = "PARKED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


#: Statuses from which no further work is allocated without a deliberate act.
#: ``HUMAN_READY`` is here, and that is the release-critical part: its track has
#: ended, so it occupies no capacity and blocks nothing.
TERMINAL_IDEA_STATUSES: frozenset[IdeaStatus] = frozenset(
    {
        IdeaStatus.HUMAN_READY,
        IdeaStatus.PARKED,
        IdeaStatus.REJECTED,
        IdeaStatus.SUPERSEDED,
    }
)

#: Statuses an idea can never leave by an autonomous act.
CLOSED_IDEA_STATUSES: frozenset[IdeaStatus] = frozenset(
    {IdeaStatus.REJECTED, IdeaStatus.SUPERSEDED}
)


class OperationalState(StrEnum):
    """Whether anything can happen to this idea right now, and if not why."""

    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"


BLOCKED_STATES: frozenset[OperationalState] = frozenset(
    {
        OperationalState.BLOCKED_PROVIDER,
        OperationalState.BLOCKED_BUDGET,
        OperationalState.BLOCKED_EXTERNAL,
        OperationalState.BLOCKED_DEPENDENCY,
    }
)


class QualityTier(StrEnum):
    """The high-water mark of gates this idea has passed.

    Not a duplicate of :class:`IdeaStatus`. An idea rejected after reaching
    ``PROMISING`` is a different fact from one rejected as a candidate, and the
    digest is asked to report demotions.
    """

    NONE = "NONE"
    PROMISING = "PROMISING"
    VALIDATED = "VALIDATED"
    HUMAN_READY = "HUMAN_READY"


TIER_ORDER: dict[QualityTier, int] = {
    QualityTier.NONE: 0,
    QualityTier.PROMISING: 1,
    QualityTier.VALIDATED: 2,
    QualityTier.HUMAN_READY: 3,
}


class IdeaOrigin(StrEnum):
    BLIND_EXPLORER = "BLIND_EXPLORER"
    SEEDED_EXPLORER = "SEEDED_EXPLORER"
    FAILURE_MINING_EXPLORER = "FAILURE_MINING_EXPLORER"
    RESEARCHER_SEED = "RESEARCHER_SEED"
    BRANCH = "BRANCH"
    REVIVAL = "REVIVAL"
    MERGE = "MERGE"
    #: A new idea raised by a frontier request -- a result, an objection, a
    #: replication, a literature contradiction, a referee's finding.
    FOLLOW_UP = "FOLLOW_UP"
    #: A new idea the literature explorer derived from verified claims about
    #: the published record, citing them.
    LITERATURE_EXPLORER = "LITERATURE_EXPLORER"


class EdgeKind(StrEnum):
    DERIVED_FROM = "DERIVED_FROM"
    GENERALIZES = "GENERALIZES"
    SPECIALIZES = "SPECIALIZES"
    MERGED_FROM = "MERGED_FROM"
    REVIVES = "REVIVES"
    CONTRADICTS = "CONTRADICTS"
    DUPLICATE_OF = "DUPLICATE_OF"


#: The kinds that are lineage, mirroring the generated ``is_lineage`` column.
#: A traversal filters on the column; this exists so Python can answer the same
#: question without a round trip, and the schema test asserts they agree.
LINEAGE_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.DERIVED_FROM,
        EdgeKind.GENERALIZES,
        EdgeKind.SPECIALIZES,
        EdgeKind.MERGED_FROM,
        EdgeKind.REVIVES,
    }
)


#: How an idea could be settled. Controls routing, never conclusions.
#:
#: :class:`research_os.runtime.adjudication.AdjudicationKind`, reused verbatim.
#: An earlier draft defined a parallel enum -- ``MATHEMATICAL``, ``EMPIRICAL``,
#: ``LITERATURE_PRIORITY``, ``COMPUTATIONAL``, ``MIXED`` -- which was the
#: runtime's set with one member renamed, one added and one dropped. Three
#: things were wrong with that, in increasing order of seriousness:
#:
#: 1. two enums meaning the same thing eventually disagree;
#: 2. it dropped ``UNDETERMINED``, which is the member that makes the existing
#:    routing guard conservative: it refuses an action only on a *positive*
#:    determination, so a target nothing understands behaves as it did before;
#: 3. it would have been assigned by a model. ``adjudication.classify`` reads
#:    the falsification clause -- the sentence in which someone already wrote
#:    down what would settle the thing -- and its docstring is explicit that
#:    this is *reading, not inference*. Letting the generator choose its own
#:    adjudication type is letting it choose its own evidentiary bar, because
#:    every quality gate is parameterised by it.
#:
#: ``COMPUTATIONAL`` is not lost: an idea settled by running a program is
#: ``EMPIRICAL`` about that program, which is what ``DIAGNOSTIC`` already
#: means when the program is the subject.
AdjudicationType = AdjudicationKind


class EvidenceKind(StrEnum):
    LITERATURE = "literature"
    EXPERIMENT = "experiment"
    DERIVATION = "derivation"
    NUMERICAL = "numerical"
    CODE = "code"
    REPLICATION = "replication"
    INSPECTION = "inspection"


class EvidenceStrength(StrEnum):
    """What the evidence does to the idea.

    ``CONSISTENT_WITH`` is the strongest thing a numerical witness may say, and
    the database enforces that rather than trusting the caller: a numerical
    check is not a proof, and the one place that could be forgotten is the one
    place a mathematical idea reaches ``VALIDATED``.
    """

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    CONSISTENT_WITH = "CONSISTENT_WITH"
    INCONCLUSIVE = "INCONCLUSIVE"


class ReviewerRole(StrEnum):
    FALSIFIER = "falsifier"
    METHODOLOGY = "methodology_reviewer"
    NOVELTY = "novelty_reviewer"
    SKEPTIC = "skeptic_reviewer"
    REPLICATOR = "replicator"
    META = "meta_reviewer"


#: The three whose presence, live and on the current version, `VALIDATED`
#: requires. The falsifier is not among them: its job is to kill the idea
#: before this point, and a passing falsifier is not a review of the finished
#: work.
INDEPENDENT_REVIEW_ROLES: tuple[ReviewerRole, ...] = (
    ReviewerRole.METHODOLOGY,
    ReviewerRole.NOVELTY,
    ReviewerRole.SKEPTIC,
)


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    PASS_WITH_OBJECTIONS = "PASS_WITH_OBJECTIONS"
    REVISE = "REVISE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


class Severity(StrEnum):
    NONE = "NONE"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


class ObjectionTarget(StrEnum):
    """What an objection is about: the idea, or the way it proposes to settle it.

    The axis the first dogfood found missing. Severity says how bad an
    objection is; this says what it is bad *about*, and the two together are
    what make "this question is wrong" and "this question's test is wrong"
    different decisions. Before it they were the same one, and a fatal
    objection to a falsifier rejected the research question that falsifier was
    attached to.

    Reported by the model as a fact about its own objection; never a
    disposition the model chooses. ``runner.run_falsify`` routes on it in
    ordinary Python, the way ``runtime.adjudication.classify`` reads the
    adjudication type out of the falsifier rather than letting a generator
    pick its own evidentiary bar.
    """

    CLAIM = "CLAIM"
    """The idea itself does not survive: wrong, already known, or not worth it."""

    TEST = "TEST"
    """The idea may stand; what it proposes as a way to settle it does not.

    A researcher meeting this rewrites the test. So does the portfolio now --
    once, bounded by ``max_revisions_per_idea``, and with the objection left
    standing so the sharpened version has to answer it.
    """


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.CRITICAL: 3,
    Severity.FATAL: 4,
}

#: Severities that block promotion outright while unresolved.
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.CRITICAL, Severity.FATAL}
)


#: The separation actually achieved between a producer and its reviewer.
#:
#: :class:`research_os.runtime.interfaces.Independence`, reused verbatim rather
#: than a second vocabulary meaning the same thing. An earlier draft defined
#: ``DIFFERENT_FAMILY / DIFFERENT_MODEL / SAME_MODEL_CLEAN_CONTEXT``, which is
#: the runtime's enum with one member renamed -- and two enums that mean the
#: same thing eventually disagree.
#:
#: What *is* different is the question being answered, which is why the column
#: is called ``independence_vs_origin`` and not ``independence``.
#: ``model_calls.independence`` is the router's per-group value: which provider
#: family has already answered inside this independence group. This is the
#: comparison the gate needs: the review's call against the *version's origin
#: call*. Same words, different subjects, so they get different column names.
#:
#: There is deliberately no ``SAME_CALL`` member. A review whose model call is
#: the origin call is not a weak review, it is a defect, and
#: :func:`research_os.portfolio.gates.classify_independence` raises rather than
#: returning something storable.
IndependenceClass = Independence

INDEPENDENCE_ORDER: dict[Independence, int] = {
    Independence.NONE: 0,
    Independence.DIFFERENT_CONTEXT: 1,
    Independence.DIFFERENT_MODEL: 2,
    Independence.DIFFERENT_FAMILY: 3,
}


class ContextClass(StrEnum):
    FROZEN_PACKET = "FROZEN_PACKET"
    SHARED_CONTEXT = "SHARED_CONTEXT"


class Stage(StrEnum):
    """The bounded units an idea track advances through, one per invocation."""

    DEDUP = "dedup"
    NOVELTY_SCREEN = "novelty_screen"
    FALSIFY = "falsify"
    DISCOVER = "discover"
    ADJUDICATE = "adjudicate"
    EVIDENCE = "evidence"
    LITERATURE_AUDIT = "literature_audit"
    REVIEW_BOARD = "review_board"
    META_REVIEW = "meta_review"
    REPLICATE = "replicate"
    BRANCH = "branch"


class ExperimentRole(StrEnum):
    """Which measurement this is: the one that settles, or the one that checks.

    Two roles rather than a boolean, because an idea version may have exactly
    one of each and the database says so with a unique index. A third
    "another go at the same thing" is deliberately not expressible: retrying a
    failed execution reuses the row it failed on, so a host that was down for
    an hour does not leave an idea with four experiments.
    """

    PRIMARY = "PRIMARY"
    REPLICATION = "REPLICATION"


class ExperimentState(StrEnum):
    """How far the asking has got. Six states, none of them a verdict.

    The brief this closes is explicit that these must not be conflated, and
    the reason is the one the failure taxonomy already states: *experiment
    proposed*, *running* and *operationally failed* are facts about
    machinery, and only ``INTERPRETED`` is a fact about the science. A system
    with one "failed" state cannot tell a refutation from a dead node, and a
    system that reached for one would eventually report the node.
    """

    PROPOSED = "PROPOSED"
    """Designed and preregistered. Nothing has run, and nothing is owed."""

    EXECUTABLE = "EXECUTABLE"
    """The specification resolved against a declared command and a workspace
    exists for it. Separate from PROPOSED because preparing the disposable
    worktree is an irreversible act in the project repository, exactly as it
    is for :class:`research_os.experiment.models.ExecutionState.PREPARING`."""

    RUNNING = "RUNNING"
    """Submitted. Local execution passes through this state and leaves it in
    the same call; a cluster job stays here until the daemon polls it."""

    COMPLETED = "COMPLETED"
    """The executor finished and the outputs are collected. Nothing has been
    concluded from them yet."""

    OPERATIONALLY_FAILED = "OPERATIONALLY_FAILED"
    """The measurement did not happen. Never evidence about the idea, and the
    row carries the failure class that says why."""

    INTERPRETED = "INTERPRETED"
    """The frozen rule was applied to the collected outputs and a conclusion
    recorded. This is the only state that says anything scientific."""

    SUPERSEDED = "SUPERSEDED"
    """The idea version that asked for this measurement has been revised, so
    the question it answers is no longer the question being asked."""


#: States in which an experiment is still owed something.
OPEN_EXPERIMENT_STATES: frozenset[ExperimentState] = frozenset(
    {
        ExperimentState.PROPOSED,
        ExperimentState.EXECUTABLE,
        ExperimentState.RUNNING,
        ExperimentState.COMPLETED,
    }
)


class EmpiricalConclusion(StrEnum):
    """What the frozen rule said about the result. Reached by ordinary Python.

    Five values, and the fifth is the one the brief insists on:
    ``OPERATIONALLY_BLOCKED`` exists so that "the executor died" has somewhere
    to go that is not ``CONTRADICTS``. It is never stored on an evidence row,
    because there is no evidence -- it is stored on the experiment, where it
    describes the machinery.

    ``INSUFFICIENT`` is the other one worth stating plainly: the run finished,
    and what it produced does not answer the question -- a missing output, a
    metric that is not in the file, or a design that never had a
    machine-checkable rule. It is not a refutation and it must never be read
    as one.
    """

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    INCONCLUSIVE = "INCONCLUSIVE"
    INSUFFICIENT = "INSUFFICIENT"
    OPERATIONALLY_BLOCKED = "OPERATIONALLY_BLOCKED"


#: How a conclusion is stored when it becomes an evidence row.
#:
#: A table rather than a cast, because the two vocabularies are deliberately
#: different sizes: ``EvidenceStrength`` is what a *gate* reads and has no
#: member for "nothing ran", so the two conclusions that mean that have no
#: entry here and write no row at all.
EVIDENCE_STRENGTH_FOR_CONCLUSION: dict[EmpiricalConclusion, EvidenceStrength] = {
    EmpiricalConclusion.SUPPORTS: EvidenceStrength.SUPPORTS,
    EmpiricalConclusion.CONTRADICTS: EvidenceStrength.CONTRADICTS,
    EmpiricalConclusion.INCONCLUSIVE: EvidenceStrength.INCONCLUSIVE,
    EmpiricalConclusion.INSUFFICIENT: EvidenceStrength.INCONCLUSIVE,
}


class ContractKind(StrEnum):
    """Whether a scientific contract was fixed before its result or after one.

    ``PREREGISTERED`` is the only kind a gate reads as confirmatory. An
    ``EXPLORATORY`` contract exists because a post-result change of rule is
    sometimes the right scientific move -- and it must then be a new,
    labelled object that names the contract it departs from, never an edit of
    that contract. The database refuses the edit; this is where the
    alternative goes.
    """

    PREREGISTERED = "PREREGISTERED"
    EXPLORATORY = "EXPLORATORY"


class ContractState(StrEnum):
    """How far a scientific contract has been frozen."""

    ANALYSIS_FROZEN = "ANALYSIS_FROZEN"
    """The analysis is fixed and no design exists yet."""

    FROZEN = "FROZEN"
    """Analysis and design are both fixed. The contract digest covers both
    and the hypothesis, and executions are bound to it."""

    BLOCKED_CAPABILITY = "BLOCKED_CAPABILITY"
    """The analysis is fixed and no declared command can produce what it
    reads. The capability request says what would; the contract resumes from
    here when a person declares one."""

    SUPERSEDED = "SUPERSEDED"
    """No longer the contract being asked for -- its idea version was
    revised, or it was designed by a prompt this build has retired before
    anything was measured. Kept as the record."""


class ScienceObjectKind(StrEnum):
    """The three frozen objects of the science chain (`sql/0047`).

    Frozen in this order and no other: what is tested, how it is tested, and
    how that design maps onto a declared capability. See
    ``docs/SCIENCE_EXECUTION.md``.
    """

    CONTRACT = "CONTRACT"
    DESIGN = "DESIGN"
    PLAN = "PLAN"


class PrimaryOutcome(StrEnum):
    """The system-computed outcome of one determination. No model decides one.

    Three are readings of a validated result under the frozen decision rule;
    four say why there is no such reading, and none of those four is a
    finding about the idea.
    """

    SUPPORTED = "SUPPORTED"
    """The success criterion held and the failure criterion did not."""
    REFUTED = "REFUTED"
    """The failure criterion held and the success criterion did not."""
    INCONCLUSIVE = "INCONCLUSIVE"
    """The result is valid and lands in the preregistered inconclusive region:
    both criteria, or neither, or the frozen support the statistic needs is
    not in the data."""
    EXECUTION_FAILED = "EXECUTION_FAILED"
    """The execution did not complete: a crash, a timeout, a non-zero exit."""
    CAPABILITY_LIMITED = "CAPABILITY_LIMITED"
    """No declared capability can produce what the frozen contract reads, or
    the host cannot run the one that could."""
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    """An execution happened and what it produced cannot be accepted: the
    result artifact is missing, malformed or off its declared schema, or the
    trusted chain behind it does not hold."""
    BUDGET_LIMITED = "BUDGET_LIMITED"
    """A budget a person set could not cover the next step."""


#: The outcomes that read a result under the frozen rule. Only these carry a
#: complete chain, and only SUPPORTED and REFUTED are decisive.
READ_OUTCOMES: frozenset[PrimaryOutcome] = frozenset(
    {PrimaryOutcome.SUPPORTED, PrimaryOutcome.REFUTED, PrimaryOutcome.INCONCLUSIVE}
)
DECISIVE_OUTCOMES: frozenset[PrimaryOutcome] = frozenset(
    {PrimaryOutcome.SUPPORTED, PrimaryOutcome.REFUTED}
)


class RequestKind(StrEnum):
    """What a frontier request asks the portfolio for."""

    FOLLOW_UP = "FOLLOW_UP"
    """New ideas: the question this event raised, pursued as a new object."""

    LITERATURE = "LITERATURE"
    """Sources: retrieval and reading of the published record for one idea."""


class RequestBasis(StrEnum):
    """Which kind of event raised a frontier request. Recorded, never inferred."""

    RESULT = "RESULT"
    INSUFFICIENT = "INSUFFICIENT"
    ANOMALY = "ANOMALY"
    FALSIFIER_OBJECTION = "FALSIFIER_OBJECTION"
    REVIEWER_CRITICISM = "REVIEWER_CRITICISM"
    REPLICATION = "REPLICATION"
    LITERATURE = "LITERATURE"
    REFEREE_FINDING = "REFEREE_FINDING"
    EVIDENCE_GAP = "EVIDENCE_GAP"


class RequestState(StrEnum):
    OPEN = "OPEN"
    CONSUMED = "CONSUMED"
    DECLINED = "DECLINED"


class ProvenanceBasis(StrEnum):
    """Why an idea exists. One row per reason, append-only.

    Every :class:`RequestBasis` is here, because a request is one reason an
    idea can exist; so are the generators, and ``CONVERGENCE`` -- a second
    route independently arriving at a direction that already existed.
    """

    HUMAN_SEED = "HUMAN_SEED"
    BLIND_EXPLORATION = "BLIND_EXPLORATION"
    SEEDED_EXPLORATION = "SEEDED_EXPLORATION"
    FAILURE_MINING = "FAILURE_MINING"
    LITERATURE = "LITERATURE"
    RESULT = "RESULT"
    INSUFFICIENT = "INSUFFICIENT"
    ANOMALY = "ANOMALY"
    FALSIFIER_OBJECTION = "FALSIFIER_OBJECTION"
    REVIEWER_CRITICISM = "REVIEWER_CRITICISM"
    REPLICATION = "REPLICATION"
    REFEREE_FINDING = "REFEREE_FINDING"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    BRANCH = "BRANCH"
    REVIVAL = "REVIVAL"
    MERGE = "MERGE"
    CONVERGENCE = "CONVERGENCE"


#: The provenance an origin implies when nothing more specific is known.
PROVENANCE_FOR_ORIGIN: dict[str, ProvenanceBasis] = {
    "BLIND_EXPLORER": ProvenanceBasis.BLIND_EXPLORATION,
    "SEEDED_EXPLORER": ProvenanceBasis.SEEDED_EXPLORATION,
    "FAILURE_MINING_EXPLORER": ProvenanceBasis.FAILURE_MINING,
    "RESEARCHER_SEED": ProvenanceBasis.HUMAN_SEED,
    "BRANCH": ProvenanceBasis.BRANCH,
    "REVIVAL": ProvenanceBasis.REVIVAL,
    "MERGE": ProvenanceBasis.MERGE,
    "FOLLOW_UP": ProvenanceBasis.RESULT,
    "LITERATURE_EXPLORER": ProvenanceBasis.LITERATURE,
}


class ActionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class Disposition(StrEnum):
    """What a completed stage concluded should happen to the idea."""

    REJECT = "REJECT"
    PARK = "PARK"
    REVISE = "REVISE"
    DEEPEN = "DEEPEN"
    BRANCH = "BRANCH"
    PROMISING = "PROMISING"
    VALIDATED = "VALIDATED"
    HUMAN_READY = "HUMAN_READY"
    CONTINUE = "CONTINUE"
    DUPLICATE = "DUPLICATE"


#: The dispositions that are *gated*: the meta-reviewer may recommend one and
#: `gates.evaluate` decides whether it happens. Every other disposition is
#: ungated, because killing or pausing an idea needs no ceremony and that
#: asymmetry is the design.
GATED_DISPOSITIONS: frozenset[Disposition] = frozenset(
    {Disposition.PROMISING, Disposition.VALIDATED, Disposition.HUMAN_READY}
)


class PortfolioStatus(StrEnum):
    """Why the portfolio is or is not running.

    Closed, and complete. There is no ``WAIT_HUMAN``: one idea needing the
    researcher must never stop the others, and the way that is guaranteed is
    that there is no state to reach.
    """

    RUNNING = "RUNNING"
    PAUSED_BY_RESEARCHER = "PAUSED_BY_RESEARCHER"
    PAUSED_BUDGET_EXHAUSTED = "PAUSED_BUDGET_EXHAUSTED"
    PAUSED_NO_FRONTIER = "PAUSED_NO_FRONTIER"
    PAUSED_BLOCKED_EXTERNAL = "PAUSED_BLOCKED_EXTERNAL"


# ---------------------------------------------------------------- records --
class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualityDimensions(BaseModel):
    """The dimensions the scientific record keeps separate.

    §8 of the brief: *do not implement one opaque idea score*. A scalar
    scheduling utility exists and lives on ``idea_actions``; it is an
    operational number and it never replaces these.

    Every field is optional. ``None`` means "not assessed", which is different
    from 0.0 ("assessed as worthless") and the difference matters to the
    allocator: an unassessed novelty is a reason to run the novelty screen, and
    a zero is a reason not to bother.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    novelty: float | None = Field(default=None, ge=0.0, le=1.0)
    potential_impact: float | None = Field(default=None, ge=0.0, le=1.0)
    plausibility: float | None = Field(default=None, ge=0.0, le=1.0)
    falsifiability: float | None = Field(default=None, ge=0.0, le=1.0)
    tractability: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    reproducibility: float | None = Field(default=None, ge=0.0, le=1.0)
    reviewer_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    literature_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    def merged(self, other: QualityDimensions) -> QualityDimensions:
        """Overlay ``other``'s assessed dimensions onto this one.

        A stage that assesses novelty must not blank out the plausibility a
        previous stage assessed, and a stage that assesses nothing must change
        nothing.
        """

        values = self.model_dump()
        for name, value in other.model_dump().items():
            if value is not None:
                values[name] = value
        return QualityDimensions(**values)


class ParkReason(StrEnum):
    """Why the *system* parked an idea, when the reason is a structural block.

    Mirrored by ``ideas_park_reason_ck`` (`sql/0042`). A reason here is one a
    later tick can re-check mechanically -- which is the difference from the
    prose in ``revisit_if``, which nothing could read: an idea parked on its
    spend ceiling said "a person raises bounds.idea_spend_ceiling_usd", a
    person did, and the idea stayed parked (M4). An idea parked for any other
    reason -- a synthesis that parked it, a dead end -- carries none, and no
    ceiling change can revive it.
    """

    IDEA_SPEND_CEILING = "idea_spend_ceiling"
    LINEAGE_SPEND_CEILING = "lineage_spend_ceiling"
    NOVELTY_FLOOR = "novelty_floor"
    LINEAGE_ROOM = "lineage_room"
    #: Parked before reasons were structural (`sql/0045`), with nothing that
    #: establishes what would revive it: its revisit text is model-writable
    #: prose and no stage or resume status was recorded. Fails closed -- no
    #: ceiling or lineage change revives it -- and a person decides.
    LEGACY_UNKNOWN = "legacy_unknown"


#: The park reasons a raised spend ceiling can lift, and nothing else can.
BUDGET_PARK_REASONS: frozenset[ParkReason] = frozenset(
    {ParkReason.IDEA_SPEND_CEILING, ParkReason.LINEAGE_SPEND_CEILING}
)


class PortfolioIdea(_Record):
    """One candidate research direction. Not a capsule object."""

    idea_id: str
    project_id: str
    depth: int
    lineage_root: str
    origin: IdeaOrigin
    current_version: int
    status: IdeaStatus
    operational_state: OperationalState
    quality_tier: QualityTier
    curated_digest: str | None = None
    curated_at: datetime | None = None
    #: Why this idea was retired, and what would bring it back. Required by the
    #: schema on REJECTED, PARKED and SUPERSEDED, for the reason
    #: ``docs/CAPSULE.md`` gives for a discarded capsule Idea: so the project
    #: can tell "we ruled this out" from "we forgot about it".
    retire_reason: str | None = None
    revisit_if: str | None = None
    created_at: datetime
    updated_at: datetime
    #: A PARKED idea's structural block (`sql/0042`): what parked it, the
    #: stage it was waiting to run, and the status it resumes as when the
    #: block lifts. ``None`` unless the system parked it for such a reason.
    park_reason: ParkReason | None = None
    park_stage: str | None = None
    resume_status: IdeaStatus | None = None

    @property
    def allocatable(self) -> bool:
        """Whether the allocator may spend on this idea at all."""

        return (
            self.status not in TERMINAL_IDEA_STATUSES
            and self.operational_state is OperationalState.IDLE
        )

    @property
    def occupies_capacity(self) -> bool:
        """Whether this idea counts against the active-track ceiling.

        A ``HUMAN_READY`` idea does not, and that single line is how the
        portfolio keeps running while one idea waits for the researcher.
        """

        return self.operational_state is OperationalState.ACTIVE


class IdeaVersion(_Record):
    """Immutable scientific content. Nothing updates one of these."""

    idea_id: str
    version: int
    title: str
    research_question: str
    core_idea: str
    mechanism: str = ""
    why_it_matters: str = ""
    falsifier: str = ""
    adjudication_types: tuple[AdjudicationType, ...] = ()
    closest_prior_work: str = ""
    claimed_difference: str = ""
    assumptions: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()
    open_uncertainties: tuple[str, ...] = ()
    next_best_action: str = ""
    dimensions: QualityDimensions = QualityDimensions()
    addressed_objections: tuple[str, ...] = ()
    content_digest: str
    canonical_digest: str
    origin_call_id: str | None = None
    origin_role: str
    origin_stage: str | None = None
    created_at: datetime


class IdeaEdge(_Record):
    parent_idea_id: str
    child_idea_id: str
    kind: EdgeKind
    parent_depth: int
    child_depth: int
    detail: str = ""
    created_at: datetime

    @property
    def is_lineage(self) -> bool:
        return self.kind in LINEAGE_KINDS


class IdeaEvidence(_Record):
    evidence_id: str
    idea_id: str
    idea_version: int
    kind: EvidenceKind
    strength: EvidenceStrength
    summary: str
    artifact_id: str | None = None
    finding_id: str | None = None
    job_id: str | None = None
    literature_key: str | None = None
    source_call_id: str | None = None
    #: The verified literature claim this row rests on, when it rests on one.
    claim_id: str | None = None
    created_at: datetime
    #: The executed search whose results this row cites (`sql/0039`). A
    #: literature row without one cannot count as a retrieval path: nothing
    #: records which search produced it (INV-05).
    retrieval_id: str | None = None


class RetrievalPurpose(StrEnum):
    """Why the system executed a literature search. Mirrored by
    ``literature_retrievals_purpose_ck``.

    Set by the code that runs the search, never by a model, and it is what a
    gate reads to tell the first terminology path from the second.
    """

    #: The cheap screen's search of the research question.
    NOVELTY_SCREEN = "novelty_screen"
    #: The deep audit's search of the research question -- the first path,
    #: and every retry of it.
    AUDIT = "audit"
    #: ``REPLICATE``'s search on the core idea and the claimed difference: the
    #: only purpose that can be a distinct executed retrieval path (INV-05).
    SECOND_PATH = "second_path"
    #: A literature request's search, answered by a verified reading.
    READING = "reading"


class RetrievalStatus(StrEnum):
    """Mirrored by ``literature_retrievals_status_ck``."""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExecutionReceipt(_Record):
    """One execution, as Research OS's own runner observed it (`sql/0046`).

    Written by :func:`research_os.portfolio.provenance.write_receipt` from the
    argument vector and environment the runner delivered, the inputs it
    materialised, the code identity and the output digests it hashed when the
    process exited -- never from anything a program printed. The document
    itself is the content-addressed artifact ``receipt_artifact_id``; these
    columns are its digests. Immutable (INV-02, INV-07).
    """

    receipt_id: str
    job_id: str
    experiment_id: str
    idea_id: str
    idea_version: int
    role: ExperimentRole
    action_id: str | None = None
    run_id: str | None = None
    work_id: str | None = None
    command: str
    command_digest: str
    spec_digest: str
    base_commit: str
    delivered_digest: str
    inputs_digest: str
    outputs_digest: str
    exit_code: int | None = None
    manifest_artifact_id: str | None = None
    manifest_digest: str | None = None
    parent_receipt_id: str | None = None
    receipt_artifact_id: str
    created_at: datetime
    #: The frozen execution plan this execution realised (`sql/0047`), or
    #: ``None`` for an execution no plan governs.
    plan_digest: str | None = None


class ReplicationAssessment(_Record):
    """What one replication reading established, three findings kept apart.

    ``configuration_independent`` is proved from two trusted receipts: a
    separate execution was delivered a configuration that differs from its
    parent's in what the frozen manifest says it varies.
    ``perturbation_attested`` is never proved: it records that the researcher's
    declaration, frozen in the manifest, attests the computation uses a varied
    input. ``agrees``/``identical_values`` compare the scientific readings.
    A ``legacy`` row is pre-`sql/0046` evidence kept as history, and neither.
    """

    assessment_id: str
    evidence_id: str
    experiment_id: str | None = None
    idea_id: str
    idea_version: int
    legacy: bool = False
    receipt_id: str | None = None
    parent_receipt_id: str | None = None
    parent_experiment_id: str | None = None
    parent_evidence_id: str | None = None
    parent_analysis_artifact_id: str | None = None
    manifest_artifact_id: str | None = None
    manifest_digest: str | None = None
    analysis_artifact_id: str | None = None
    configuration_independent: bool
    perturbation_attested: bool
    varied: tuple[str, ...] = ()
    attested: tuple[str, ...] = ()
    unattested: tuple[str, ...] = ()
    agrees: bool | None = None
    identical_values: bool | None = None
    basis: str
    created_at: datetime


class ReplicationProvenance(_Record):
    """A replication assessment as a readiness gate reads it (INV-07, INV-08).

    Built by :func:`research_os.portfolio.provenance.replication_provenance`,
    which re-verifies the whole chain against what is stored *now* -- every
    digest re-hashed, the evidence, both receipts, the manifest and the
    current experiment rows bound to one another -- and says what, if
    anything, broke. The gate itself decides admissibility from these fields.
    """

    evidence_id: str
    assessment_id: str
    legacy: bool
    configuration_independent: bool
    perturbation_attested: bool
    chain_intact: bool
    problems: tuple[str, ...] = ()

    @property
    def admissible(self) -> bool:
        return (
            not self.legacy
            and self.chain_intact
            and self.configuration_independent
            and self.perturbation_attested
        )


class ScienceObject(_Record):
    """One frozen contract, design or plan (`sql/0047`). Content-addressed.

    ``object_digest`` is ``<kind prefix>:<artifact_id>``: the artifact holds
    the canonical bytes of the object and its id is their sha256, so the row
    and the bytes cannot disagree without one of them failing to re-hash.
    """

    object_digest: str
    kind: ScienceObjectKind
    project_id: str
    idea_id: str
    idea_version: int
    parent_digest: str | None = None
    artifact_id: str
    capability_ref: str | None = None
    capability_digest: str | None = None
    spec_digest: str | None = None
    frozen_at: datetime


class ScienceOutcome(_Record):
    """One system-computed outcome, bound to its chain (`sql/0047`). Immutable."""

    outcome_id: str
    project_id: str
    idea_id: str
    idea_version: int
    role: ExperimentRole
    state: PrimaryOutcome
    reason: str
    contract_id: str | None = None
    experiment_id: str | None = None
    contract_digest: str | None = None
    design_digest: str | None = None
    plan_digest: str | None = None
    capability_ref: str | None = None
    capability_digest: str | None = None
    receipt_id: str | None = None
    result_sha256: str | None = None
    estimate: float | None = None
    record_artifact_id: str
    created_at: datetime


class ScienceChain(_Record):
    """One evidence row's science chain as a readiness gate reads it.

    Built by :func:`research_os.portfolio.sciencechain.science_chains`, which
    re-verifies everything behind the row *now*: the frozen contract, design
    and plan re-hash and name one another, the plan is the experiment's and
    binds a declared capability, the receipt is the runner's for that
    execution of that plan, the recorded outcome names all of it, and the
    experiment still names this reading. The gate decides from these fields.
    """

    evidence_id: str
    experiment_id: str | None = None
    role: ExperimentRole
    outcome: PrimaryOutcome | None = None
    capability_ref: str | None = None
    chain_intact: bool
    #: Replication only: whether its outcome is the same state as the
    #: primary's, under the capability's frozen comparison rule.
    agrees_with_primary: bool | None = None
    problems: tuple[str, ...] = ()

    @property
    def admissible(self) -> bool:
        return (
            self.chain_intact
            and self.capability_ref is not None
            and self.outcome in DECISIVE_OUTCOMES
        )


class LiteratureRetrieval(_Record):
    """One literature search the system actually executed (`sql/0039`)."""

    retrieval_id: str
    project_id: str
    idea_id: str | None = None
    idea_version: int | None = None
    action_id: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    work_id: str | None = None
    purpose: RetrievalPurpose
    query: str
    query_digest: str
    backend: str
    result_limit: int
    status: RetrievalStatus
    result_keys: tuple[str, ...] = ()
    result_digest: str | None = None
    error: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class IdeaReview(_Record):
    review_id: str
    idea_id: str
    idea_version: int
    reviewer_role: ReviewerRole
    verdict: ReviewVerdict
    severity: Severity
    summary: str
    recommendation: Disposition | None = None
    detail_artifact_id: str | None = None
    #: The two digests this review is bound to. Both, not one: the content is
    #: what it read, and the evidence set is what it read it against. A
    #: revision stales it through the first; swapping the evidence beneath a
    #: standing approval stales it through the second.
    reviewed_content_digest: str
    reviewed_evidence_digest: str
    packet_digest: str
    #: ``role@version``. A review produced by a prompt this build has since
    #: superseded answered a question no longer being asked.
    prompt_version: str
    call_id: str | None = None
    provider: str
    model: str | None = None
    provider_family: str
    independence_vs_origin: Independence
    context_class: ContextClass
    independence_note: str = ""
    created_at: datetime
    #: A review is an event of the call that produced it (`sql/0040`, INV-04).
    #: The action that bought the call; which attempt this is at its binding;
    #: the earlier review of the same binding it follows, if any; and a digest
    #: of the response it records.
    action_id: str | None = None
    attempt: int = 1
    supersedes_review_id: str | None = None
    response_digest: str | None = None
    #: How many objections this review event raised (`sql/0044`, INV-04). The
    #: review and exactly that many objection rows commit together or not at
    #: all. ``None`` only on a legacy row whose completeness cannot be
    #: established, and such a review is never live.
    objection_count: int | None = None


class IdeaObjection(_Record):
    objection_id: str
    idea_id: str
    raised_in_review: str
    raised_at_version: int
    objection_key: str
    severity: Severity
    #: Whether the objection is to the idea or to the way it proposes to
    #: settle itself. Stored, because `stages.select_stage` and
    #: `runner.run_falsify` both route on it and both read stored rows.
    target: ObjectionTarget = ObjectionTarget.CLAIM
    summary: str
    addressed_at_version: int | None = None
    response: str | None = None
    #: The review that established the answer. Never the producing side, and
    #: never the role that raised the objection: an objection is answered to
    #: somebody else's satisfaction or it is not answered.
    resolved_by_review: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    #: Its position in the review event that raised it (`sql/0044`). ``None``
    #: on a legacy row, which stays standing like any other.
    ordinal: int | None = None

    @property
    def open(self) -> bool:
        return self.resolved_at is None

    @property
    def blocking(self) -> bool:
        return self.open and self.severity in BLOCKING_SEVERITIES


class IdeaExperiment(_Record):
    """One measurement one idea version asked for, and how far it has got.

    The row that did not exist, and whose absence is why every empirical idea
    stopped at the evidence stage. It carries no interpretation of its own:
    :attr:`conclusion` is what the frozen rule in :attr:`decision_rule` said
    when ordinary Python applied it to the collected outputs, and
    :attr:`analysis_artifact_id` is the document that shows the arithmetic.
    """

    experiment_id: str
    idea_id: str
    idea_version: int
    project_id: str
    role: ExperimentRole
    state: ExperimentState
    command: str
    spec_digest: str
    #: The execution's identity with the workspace path removed. What a
    #: replication has to differ in; see
    #: :func:`research_os.portfolio.empirical.variation_digest`.
    variation_digest: str
    workspace_path: str
    preregistration_artifact_id: str | None = None
    decision_rule: dict[str, Any] | None = None
    no_rule_reason: str | None = None
    job_id: str | None = None
    analysis_artifact_id: str | None = None
    conclusion: EmpiricalConclusion | None = None
    evidence_id: str | None = None
    failure_class: str | None = None
    detail: str | None = None
    attempts: int = 0
    origin_call_id: str | None = None
    #: ``role@version`` of the prompt that designed it. Part of liveness, for
    #: the reason ``idea_reviews.prompt_version`` is: a design produced by a
    #: prompt this build has superseded is a commitment to a question no
    #: longer being asked, and resubmitting it forever is how an idea wedges.
    prompt_version: str = ""
    #: The scientific contract this execution realises. ``None`` for every
    #: experiment written before contracts existed, whose design carried its
    #: own rule in :attr:`decision_rule`.
    contract_id: str | None = None
    created_at: datetime
    updated_at: datetime
    #: A replication's execution manifest (`sql/0041`), frozen before it ran:
    #: parent, code identity, inputs, environment, the intended independence
    #: variables and the execution. See ``empirical.freeze_replication_manifest``.
    execution_manifest_artifact_id: str | None = None
    #: The frozen execution plan (`sql/0047`) this execution realises: the
    #: capability binding, the code commit, the inputs and the specification.
    #: ``None`` for an execution no capability manifest governs.
    plan_digest: str | None = None

    @property
    def open(self) -> bool:
        return self.state in OPEN_EXPERIMENT_STATES

    @property
    def preregistered(self) -> bool:
        """Whether a machine-checkable rule was fixed before the result.

        ``False`` is a legitimate and recorded outcome -- some questions do
        not have one -- and it is why :attr:`no_rule_reason` is required when
        this is false. What it costs is the top of the scale: without a rule
        the conclusion can only ever be ``INSUFFICIENT``.
        """

        return self.decision_rule is not None


class ScientificContract(_Record):
    """Hypothesis + analysis + design, frozen before any result. See ``sql/0031``.

    Read model only. The two halves are digests and artifact ids here; the
    content lives in the content-addressed store and is re-verified against
    these digests by :func:`research_os.portfolio.scicontract.verify` every
    time anything is executed or interpreted under the contract.
    """

    contract_id: str
    project_id: str
    idea_id: str
    idea_version: int
    role: ExperimentRole
    kind: ContractKind
    state: ContractState
    hypothesis_digest: str
    analysable: bool
    analysis_digest: str
    analysis_artifact_id: str
    analysis_prompt: str = ""
    analysis_call_id: str | None = None
    design_digest: str | None = None
    design_artifact_id: str | None = None
    design_prompt: str | None = None
    design_call_id: str | None = None
    contract_digest: str | None = None
    contract_artifact_id: str | None = None
    parent_contract_id: str | None = None
    capability_request: dict[str, Any] | None = None
    command_set_digest: str | None = None
    detail: str | None = None
    created_at: datetime
    updated_at: datetime
    frozen_at: datetime | None = None

    @property
    def frozen(self) -> bool:
        return self.state is ContractState.FROZEN


class FrontierRequest(_Record):
    """One question the portfolio owes an idea. See ``sql/0032``."""

    request_id: str
    project_id: str
    kind: RequestKind
    basis: RequestBasis
    source_idea_id: str | None = None
    source_version: int | None = None
    source_ref: str
    question: str
    detail: str | None = None
    state: RequestState
    attempts: int = 0
    resolution: str | None = None
    resolved_by: str | None = None
    created_at: datetime
    updated_at: datetime


class IdeaProvenance(_Record):
    """One reason an idea exists."""

    provenance_id: str
    idea_id: str
    basis: ProvenanceBasis
    source_ref: str | None = None
    request_id: str | None = None
    call_id: str | None = None
    detail: str = ""
    created_at: datetime


class LiteratureClaimKind(StrEnum):
    FINDING = "FINDING"
    METHOD = "METHOD"
    DATASET = "DATASET"
    LIMITATION = "LIMITATION"
    DISAGREEMENT = "DISAGREEMENT"
    GAP = "GAP"
    OPEN_QUESTION = "OPEN_QUESTION"


#: The kinds that point at the edge of the published record: what the
#: literature explorer reads to propose directions.
FRONTIER_CLAIM_KINDS: frozenset[LiteratureClaimKind] = frozenset(
    {
        LiteratureClaimKind.LIMITATION,
        LiteratureClaimKind.DISAGREEMENT,
        LiteratureClaimKind.GAP,
        LiteratureClaimKind.OPEN_QUESTION,
    }
)


class ClaimVerification(StrEnum):
    CITED = "CITED"
    QUOTED = "QUOTED"


class LiteratureClaim(_Record):
    """One verified statement about the published record. See ``sql/0033``."""

    claim_id: str
    project_id: str
    kind: LiteratureClaimKind
    statement: str
    work_keys: tuple[str, ...]
    excerpt: str = ""
    verification: ClaimVerification
    query: str
    request_id: str | None = None
    idea_id: str | None = None
    source_call_id: str | None = None
    artifact_id: str | None = None
    digest: str
    created_at: datetime


class SynthesisState(StrEnum):
    """How far a synthesis has got. Never further than REFEREED.

    There is deliberately no ACCEPTED: the referee grants nothing, and
    accepting a claim is a person's act on a capsule object.
    """

    DRAFTED = "DRAFTED"
    REFEREED = "REFEREED"
    SUPERSEDED = "SUPERSEDED"


class Synthesis(_Record):
    """One evidence synthesis and its referee. See ``sql/0034``."""

    synthesis_id: str
    project_id: str
    state: SynthesisState
    basis_digest: str
    document_artifact_id: str
    referee_artifact_id: str | None = None
    writer_call_id: str | None = None
    referee_call_id: str | None = None
    statements: int = 0
    findings: int = 0
    referee_verdict: str | None = None
    detail: str | None = None
    created_at: datetime
    updated_at: datetime


class IdeaAction(_Record):
    action_id: str
    idea_id: str
    idea_version: int
    stage: Stage
    basis_digest: str
    status: ActionStatus
    work_id: str | None = None
    thread_id: str | None = None
    utility: Decimal | None = None
    disposition: Disposition | None = None
    detail: str | None = None
    failure_class: str | None = None
    cost_usd: Decimal
    model_calls: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    #: The execution that owns this action while it is ACTIVE (`sql/0038`):
    #: the run whose session lock its process holds, and the work item
    #: attempt and lease owner that bought it. See
    #: ``docs/ARCHITECTURE_INVARIANTS.md`` INV-03.
    run_id: str | None = None
    attempt: int | None = None
    lease_owner: str | None = None
    executor: str | None = None


class PortfolioState(_Record):
    project_id: str
    status: PortfolioStatus
    charter_digest: str | None = None
    detail: str | None = None
    paused_at: datetime | None = None
    paused_by: str | None = None
    last_tick_at: datetime | None = None
    last_digest_at: datetime | None = None
    bounds: dict[str, Any] = Field(default_factory=dict)
    #: The Curator's watermark: the commit it last wrote to the autonomous
    #: branch, and the snapshot digest that commit carries. See
    #: ``sql/0024_bank_watermark.sql``.
    bank_commit: str | None = None
    bank_digest: str | None = None
    bank_written_at: datetime | None = None
    #: When a person last said "look again" with `portfolio resume`. The
    #: stage-failure ceiling counts only failures after it; the dedup key
    #: still counts all of them. See ``sql/0029_failures_forgiven.sql``.
    failures_forgiven_at: datetime | None = None
    #: The declared experiment capability this portfolio last saw, by digest.
    #: See ``sql/0031_scientific_contracts.sql``.
    command_set_digest: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def running(self) -> bool:
        return self.status is PortfolioStatus.RUNNING


class PortfolioSeed(_Record):
    seed_id: str
    project_id: str
    text: str
    note: str = ""
    consumed_at: datetime | None = None
    consumed_by: str | None = None
    created_at: datetime


class PortfolioDigestRecord(_Record):
    digest_id: str
    project_id: str
    period_start: datetime
    period_end: datetime
    payload: dict[str, Any]
    artifact_id: str | None = None
    created_at: datetime


#: Every ``check`` constraint in the portfolio schema that mirrors an enum
#: above. Read by ``tests/test_portfolio_schema.py`` in both directions: every
#: constraint named here allows exactly the enum's values, and every value-list
#: constraint on a portfolio table appears here.
ENUM_CONSTRAINTS: dict[str, frozenset[str]] = {
    "ideas_status_ck": frozenset(s.value for s in IdeaStatus),
    "ideas_operational_ck": frozenset(s.value for s in OperationalState),
    "ideas_quality_tier_ck": frozenset(s.value for s in QualityTier),
    "ideas_origin_ck": frozenset(s.value for s in IdeaOrigin),
    "idea_versions_adjudication_ck": frozenset(s.value for s in AdjudicationType),
    "idea_edges_kind_ck": frozenset(s.value for s in EdgeKind),
    "idea_evidence_kind_ck": frozenset(s.value for s in EvidenceKind),
    "idea_evidence_strength_ck": frozenset(s.value for s in EvidenceStrength),
    "idea_reviews_role_ck": frozenset(s.value for s in ReviewerRole),
    "idea_reviews_verdict_ck": frozenset(s.value for s in ReviewVerdict),
    "idea_reviews_severity_ck": frozenset(s.value for s in Severity),
    "idea_reviews_independence_ck": frozenset(s.value for s in Independence),
    "idea_reviews_context_ck": frozenset(s.value for s in ContextClass),
    # NONE is not storable on an objection: an objection with no severity is
    # not an objection. The enum is shared with reviews, where NONE means "this
    # reviewer raised nothing".
    "idea_objections_severity_ck": frozenset(
        s.value for s in Severity if s is not Severity.NONE
    ),
    "idea_objections_target_ck": frozenset(s.value for s in ObjectionTarget),
    "idea_experiments_role_ck": frozenset(s.value for s in ExperimentRole),
    "execution_receipts_role_ck": frozenset(s.value for s in ExperimentRole),
    "idea_experiments_state_ck": frozenset(s.value for s in ExperimentState),
    "idea_experiments_conclusion_ck": frozenset(s.value for s in EmpiricalConclusion),
    "scientific_contracts_role_ck": frozenset(s.value for s in ExperimentRole),
    "scientific_contracts_kind_ck": frozenset(s.value for s in ContractKind),
    "scientific_contracts_state_ck": frozenset(s.value for s in ContractState),
    "frontier_requests_kind_ck": frozenset(s.value for s in RequestKind),
    "frontier_requests_basis_ck": frozenset(s.value for s in RequestBasis),
    "frontier_requests_state_ck": frozenset(s.value for s in RequestState),
    "idea_provenance_basis_ck": frozenset(s.value for s in ProvenanceBasis),
    "literature_claims_kind_ck": frozenset(s.value for s in LiteratureClaimKind),
    "syntheses_state_ck": frozenset(s.value for s in SynthesisState),
    "syntheses_verdict_ck": frozenset({"SOUND", "MAJOR_REVISION", "UNSOUND"}),
    "literature_claims_verification_ck": frozenset(s.value for s in ClaimVerification),
    "idea_actions_status_ck": frozenset(s.value for s in ActionStatus),
    "idea_actions_stage_ck": frozenset(s.value for s in Stage),
    "idea_actions_disposition_ck": frozenset(s.value for s in Disposition),
    "portfolio_state_status_ck": frozenset(s.value for s in PortfolioStatus),
    "literature_retrievals_purpose_ck": frozenset(s.value for s in RetrievalPurpose),
    "literature_retrievals_status_ck": frozenset(s.value for s in RetrievalStatus),
    "ideas_park_reason_ck": frozenset(s.value for s in ParkReason),
    "science_objects_kind_ck": frozenset(s.value for s in ScienceObjectKind),
    "science_outcomes_role_ck": frozenset(s.value for s in ExperimentRole),
    "science_outcomes_state_ck": frozenset(s.value for s in PrimaryOutcome),
}


#: The one value-list check constraint on a portfolio table that is *policy*
#: rather than an enum mirror, with the rule it expresses.
#:
#: The schema-agreement test scans the live database for any constraint that
#: enumerates string literals and demands a Python enum for it, which is the
#: right default -- it is how two constraints escaped once before. These three
#: enumerate values while expressing a relationship between two columns, so
#: there is no closed set for an enum to be. Naming them here is the
#: acknowledgement; a new one still fails the test until somebody decides which
#: list it belongs in.
POLICY_CONSTRAINTS: dict[str, str] = {
    "idea_evidence_numerical_ck": (
        "a numerical witness may be CONSISTENT_WITH, CONTRADICTS or "
        "INCONCLUSIVE, and never SUPPORTS. A finite computation does not "
        "establish a universally quantified proposition, and this is the one "
        "place a MATHEMATICAL idea could reach VALIDATED on arithmetic."
    ),
}
