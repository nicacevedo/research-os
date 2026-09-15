"""Proposed science: structured, reviewable, and deliberately not yet science.

A proposal is what a bounded worker thinks the project should ask, hypothesise,
or test. A capsule object is what the project asserts and a human accepted.
Keeping those two things in different types, in different stores, with different
words for their verdicts, is the whole design:

* A proposal lives in runtime state under the Research OS state home. Deleting
  it loses a suggestion. A capsule object lives in Git inside the project.
* A proposal is never valid as a capsule object without an explicit human
  promotion, which produces a **draft** and nothing stronger.
* A proposal is assessed, not reviewed. ``ProposalAssessment`` is advisory
  analysis by a read-only worker; ``Review`` is a scientific act a human
  performs and signs. The vocabulary is kept apart on purpose, because a report
  that says "reviewed: PASS" next to a Claim is exactly the confusion that would
  let automated judgement be mistaken for human acceptance.

Two content rules are enforced by the models rather than asked for in a prompt.

**Grounding.** Every capsule id, literature work key, and analyst finding a
proposal cites must have been supplied to the worker that wrote it. A proposal
citing a Claim this project does not have, or a paper this run did not retrieve,
is refused outright.

**Basis.** Every proposed experiment says whether it is prospective -- specified
before the result is known -- or historical, reconstructed from work already
done. Promotion reads that field, and a historical experiment can never become a
preregistered one, because R0 treats ``predictions`` as an ex-ante commitment
and silently supplying them for finished work would fabricate a preregistration.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.automation.models import (
    FINDING_ID_RE,
    Confidence,
    Importance,
    utc_now,
)
from research_os.models import NonBlankStr

PROPOSAL_ID_RE = re.compile(r"^PROP-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
ITEM_ID_RE = re.compile(r"^PR-[0-9]{3}$")


class ProposalKind(StrEnum):
    """What kind of scientific object a proposed item would become.

    Deliberately a subset of the capsule types. A proposal may never propose a
    Review: a Review is a human act, and an object type an automated pipeline
    can draft is an object type it could eventually be mistaken for performing.
    """

    QUESTION = "question"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    EVIDENCE_INTERPRETATION = "evidence_interpretation"
    CLAIM = "claim"


#: The capsule type each proposal kind may be promoted into, and the only
#: status it may be promoted at.
#:
#: Every one of these is the weakest status that type has. Promotion produces a
#: starting point for a human, never a finished object, and never an accepted
#: one.
PROMOTION_TARGETS: dict[ProposalKind, tuple[str, str]] = {
    ProposalKind.QUESTION: ("question", "open"),
    ProposalKind.HYPOTHESIS: ("hypothesis", "draft"),
    ProposalKind.EXPERIMENT: ("experiment", "draft"),
    ProposalKind.CLAIM: ("claim", "draft"),
}

#: The kinds that cannot become a capsule object at all.
#:
#: An evidence interpretation is a reading of results a human already has. It
#: belongs in the report a person reads before deciding, not in a file that
#: looks like the project asserting something.
NON_PROMOTABLE_KINDS: frozenset[ProposalKind] = frozenset(
    {ProposalKind.EVIDENCE_INTERPRETATION}
)


class EvidenceBasis(StrEnum):
    """Whether a proposal was specified before or after the result was known.

    The distinction R0 cares about most. A prediction written before the data
    exists is evidence; the same sentence written afterwards is a description.
    Collapsing them is the most common way a pipeline manufactures confidence,
    so it is a required field rather than an inferred one.
    """

    PROSPECTIVE = "prospective"
    HISTORICAL = "historical"


class RuntimeClass(StrEnum):
    """Roughly how long a proposed experiment would take. An estimate, labelled."""

    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    UNKNOWN = "unknown"


class ResourceClass(StrEnum):
    """Roughly what a proposed experiment would need to run on."""

    LAPTOP = "laptop"
    WORKSTATION = "workstation"
    GPU = "gpu"
    CLUSTER = "cluster"
    UNKNOWN = "unknown"


class ActionKind(StrEnum):
    """The kind of work a recommended next action is.

    The controller dispatches only the kinds it has a worker for, and an unknown
    kind fails closed. ``HUMAN_DECISION`` is the one that always stops: it names
    a judgement the researcher has to make.
    """

    DETERMINISTIC = "deterministic"
    LITERATURE = "literature"
    ANALYSIS = "analysis"
    CODE = "code"
    EXPERIMENT = "experiment"
    INTERPRETATION = "interpretation"
    PAPER = "paper"
    HUMAN_DECISION = "human_decision"


class ProposalGrounding(BaseModel):
    """Exactly what a proposal was allowed to cite.

    Set by the controller from what it actually supplied, never by the worker.
    It is the allowlist the validator checks every citation against, which is
    what stops a proposal resting on a Claim this project does not have or a
    paper this run never retrieved.
    """

    model_config = ConfigDict(extra="forbid")

    capsule_ids: list[str] = Field(default_factory=list)
    literature_keys: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)


class SuppliedFinding(BaseModel):
    """One finding a *caller* supplied as grounding for a proposal.

    Not something a worker produced. This is the v1-level representation of
    "here is an observation, here is its identifier, cite it by that
    identifier", and it exists so that the autonomous runtime's results can
    ground a proposal auditably rather than being concatenated into the
    natural-language goal.

    Deliberately declared here rather than imported from
    :mod:`research_os.runtime.findings`. The dependency direction is
    ``runtime -> v1``, never the reverse, and ``tests/test_runtime_layering.py``
    asserts it; a proposal layer that imported the runtime would make the
    scientific layers depend on PostgreSQL. So the runtime converts its own
    finding record into this, and any other caller -- a person, a script, a
    later subsystem -- can supply one without the runtime existing.

    What it deliberately does **not** carry: importance, confidence, or any
    other judgement of its own significance. A caller that could label its
    finding "high importance" would be grading the evidence it is asking a
    person to act on.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: NonBlankStr
    kind: NonBlankStr
    statement: NonBlankStr
    """What was observed. Rendered as fenced, untrusted data, never as prose."""

    rests_on: list[str] = Field(default_factory=list)
    """Artifact ids, capsule object ids or literature keys, for the audit trail."""

    @field_validator("finding_id")
    @classmethod
    def _citable_id_shape(cls, value: str) -> str:
        """Refuse an id that cannot safely be an allowlist entry.

        The same shape the analyst's own finding ids are held to. An id
        containing whitespace or a control character is one the grounding
        allowlist and the prompt catalogue would disagree about, and a
        disagreement between those two is how an unsupplied citation passes.
        """

        if FINDING_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "a supplied finding id must be 1-32 characters of letters, "
                f"digits, '-', '_', or '.'; got {value!r}"
            )
        return value


class ScientificBasisSnapshot(BaseModel):
    """What the canonical science looked like when a proposal was made.

    Recomputed before a human is offered the promotion, so a proposal that has
    been waiting while the project moved on fails closed rather than being
    promoted onto a basis that no longer holds.

    **Why not the repository HEAD.** ``base_commit`` is already on the
    proposal, and comparing it to the current HEAD was the obvious freshness
    check and the wrong one: a proposal is about scientific objects, and
    invalidating it because someone fixed a typo in the README would train a
    researcher to ignore the warning. So this snapshots the *objects the
    proposal actually relied on* and nothing else.

    **Optional, for backward compatibility.** A proposal written before this
    existed has ``None`` here, and
    :func:`research_os.proposal.promote.basis_status` reports that honestly --
    "this proposal records no scientific basis, so staleness cannot be
    checked" -- rather than either failing it or passing it silently.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    project_id: str | None = None
    capsule_schema_versions: dict[str, int] = Field(default_factory=dict)
    """Each referenced object's ``schema_version``, so a schema migration shows."""

    referenced_object_statuses: dict[str, str] = Field(default_factory=dict)
    """Each referenced object's lifecycle status when the proposal was made.

    Recorded separately from the combined digest so a staleness report can name
    what moved -- "Q-0001 (open -> answered)" -- rather than saying that
    something did.
    """

    referenced_object_ids: list[str] = Field(default_factory=list)
    """Exactly the capsule objects the proposal's items cite, sorted."""

    referenced_object_digest: str = ""
    """A hash over ``(id, project-scoped subject digest)`` for each of them."""

    charter_digest: str | None = None
    """The project charter, when the proposal's context included one."""

    finding_packet_digest: str | None = None
    """The supplied findings, so superseded grounding is detectable."""

    literature_keys: list[str] = Field(default_factory=list)
    """Stable identifiers, sorted. Retrieval is append-only, so presence is the test."""


class ProposedItem(BaseModel):
    """One proposed scientific object. Runtime state, never capsule truth."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: ProposalKind
    title: NonBlankStr
    statement: NonBlankStr
    rationale: NonBlankStr
    basis: EvidenceBasis
    addresses: list[str] = Field(default_factory=list)
    """Existing capsule objects this item bears on, by id."""

    grounded_in_literature: list[str] = Field(default_factory=list)
    """Retrieved works this item rests on, by literature work key."""

    grounded_in_findings: list[str] = Field(default_factory=list)
    """Analyst findings this item rests on, by finding id."""

    falsification: str | None = None
    expected_direction: str | None = None
    primary_metrics: list[str] = Field(default_factory=list)
    decision_rule: str | None = None
    required_inputs: list[str] = Field(default_factory=list)
    required_code: list[str] = Field(default_factory=list)
    runtime_class: RuntimeClass = RuntimeClass.UNKNOWN
    resource_class: ResourceClass = ResourceClass.UNKNOWN
    risks: list[str] = Field(default_factory=list)
    importance: Importance = Importance.MEDIUM
    confidence: Confidence = Confidence.LOW

    @field_validator("item_id")
    @classmethod
    def _item_id_shape(cls, value: str) -> str:
        if ITEM_ID_RE.fullmatch(value) is None:
            raise ValueError("item_id must look like PR-001")
        return value

    @model_validator(mode="after")
    def _kind_specific_rules(self) -> Self:
        """Refuse a proposal that is incomplete for what it claims to be.

        A hypothesis with no falsification is a belief. An experiment with no
        decision rule cannot settle anything, because whatever comes out of it
        can be read as agreement. Both are required here rather than at
        promotion, so the worker is told what a usable proposal is while it can
        still produce one.
        """

        if (
            self.kind is ProposalKind.HYPOTHESIS
            and not (self.falsification or "").strip()
        ):
            raise ValueError(
                f"{self.item_id} proposes a hypothesis with no falsification; a "
                "hypothesis that nothing could contradict is not a hypothesis"
            )
        if self.kind is ProposalKind.EXPERIMENT:
            if not (self.decision_rule or "").strip():
                raise ValueError(
                    f"{self.item_id} proposes an experiment with no decision rule; "
                    "without one, any outcome can be read as confirmation"
                )
            if not self.primary_metrics:
                raise ValueError(
                    f"{self.item_id} proposes an experiment with no primary metric"
                )
            if (
                self.basis is EvidenceBasis.PROSPECTIVE
                and not (self.expected_direction or "").strip()
            ):
                raise ValueError(
                    f"{self.item_id} is a prospective experiment with no expected "
                    "direction; a prediction made after the fact is not a prediction"
                )
        if self.kind is ProposalKind.CLAIM and not self.addresses:
            raise ValueError(
                f"{self.item_id} proposes a claim that addresses nothing; a claim "
                "must name the hypotheses or questions it answers"
            )
        return self

    @property
    def promotable(self) -> bool:
        return self.kind not in NON_PROMOTABLE_KINDS


class OpenUncertainty(BaseModel):
    """Something the proposal could not settle, and what would settle it."""

    model_config = ConfigDict(extra="forbid")

    statement: NonBlankStr
    what_would_settle_it: NonBlankStr
    blocks: list[str] = Field(default_factory=list)


class NextAction(BaseModel):
    """One recommended next step, typed so a controller can dispatch it.

    A recommendation and nothing else. Naming an action here does not schedule
    it, authorise it, or budget for it; the controller decides what to dispatch,
    and it dispatches only kinds it has a worker for.
    """

    model_config = ConfigDict(extra="forbid")

    action: NonBlankStr
    kind: ActionKind
    rationale: NonBlankStr
    addresses_items: list[str] = Field(default_factory=list)
    requires_human: bool = False

    @model_validator(mode="after")
    def _human_decisions_require_a_human(self) -> Self:
        if self.kind is ActionKind.HUMAN_DECISION and not self.requires_human:
            raise ValueError("an action of kind human_decision must set requires_human")
        return self


class ResearchProposal(BaseModel):
    """A complete, validated, and explicitly non-canonical scientific proposal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    proposal_id: str
    project_id: str | None = None
    project_path: NonBlankStr
    base_commit: str | None = None
    goal: NonBlankStr
    created_at: str = Field(default_factory=utc_now)
    summary: NonBlankStr
    grounding: ProposalGrounding = Field(default_factory=ProposalGrounding)
    supplied_findings: list[SuppliedFinding] = Field(default_factory=list)
    """The findings the caller grounded this proposal in, quoted for audit.

    The *allowlist* is ``grounding.finding_ids``; this is the text behind
    each id, kept so a person reading the proposal months later does not
    have to go to the operational database to find out what ``FIND-...``
    said. A validator below requires the two to agree, because a proposal
    that cites an id whose text it does not carry is unauditable.
    """

    scientific_basis: ScientificBasisSnapshot | None = None
    """What the canonical science looked like. ``None`` on older proposals."""

    items: list[ProposedItem] = Field(default_factory=list)
    uncertainties: list[OpenUncertainty] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    provider: NonBlankStr = "unknown"
    model: str | None = None
    invocation_id: str | None = None
    raw_output_path: str | None = None

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_shape(cls, value: str) -> str:
        if PROPOSAL_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "proposal_id must look like PROP-20260912T101500Z-0a1b2c3d"
            )
        return value

    @model_validator(mode="after")
    def _everything_cited_was_supplied(self) -> Self:
        """Refuse a proposal that rests on something this run did not have.

        The same rule the literature analyst is held to, applied to the wider
        set: a capsule id, a retrieved work, an analyst finding. A proposal that
        cites a Claim this project does not hold is not a proposal about this
        project, and one that cites a paper this run did not retrieve has
        reached outside its own evidence.
        """

        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("proposed item ids must not repeat")

        capsule = set(self.grounding.capsule_ids)
        literature = set(self.grounding.literature_keys)
        findings = set(self.grounding.finding_ids)

        # A quoted finding must be one the allowlist permits. One direction
        # only, deliberately: a caller may also ground a proposal through
        # ``analysis_data``, where the finding *text* travels in its own fenced
        # block and only the ids reach ``grounding.finding_ids``. So "cited but
        # not quoted here" is a legitimate shape, and "quoted but not allowed"
        # is not -- the second means the prompt showed a worker more than the
        # validator would accept, which is how an unsupplied citation gets
        # written in the first place.
        quoted = {item.finding_id for item in self.supplied_findings}
        if len(quoted) != len(self.supplied_findings):
            raise ValueError("a proposal quotes the same supplied finding twice")
        if quoted - findings:
            raise ValueError(
                "a proposal quotes finding(s) that are not in its grounding "
                "allowlist: " + ", ".join(sorted(quoted - findings))
            )
        for item in self.items:
            _reject_unsupplied(item.item_id, "capsule object", item.addresses, capsule)
            _reject_unsupplied(
                item.item_id,
                "retrieved work",
                item.grounded_in_literature,
                literature,
            )
            _reject_unsupplied(
                item.item_id, "analyst finding", item.grounded_in_findings, findings
            )
        known = set(ids)
        for action in self.next_actions:
            unknown = sorted(set(action.addresses_items) - known)
            if unknown:
                raise ValueError(
                    "a recommended action refers to proposed items that are not "
                    "in this proposal: " + ", ".join(unknown)
                )
        for uncertainty in self.uncertainties:
            unknown = sorted(set(uncertainty.blocks) - known)
            if unknown:
                raise ValueError(
                    "an uncertainty blocks proposed items that are not in this "
                    "proposal: " + ", ".join(unknown)
                )
        return self

    def item(self, item_id: str) -> ProposedItem:
        for entry in self.items:
            if entry.item_id == item_id:
                return entry
        raise KeyError(item_id)

    @property
    def human_checkpoints(self) -> list[NextAction]:
        return [item for item in self.next_actions if item.requires_human]


class ProposalVerdict(StrEnum):
    """The verdicts a read-only proposal assessment may return.

    Deliberately unrelated to the scientific :class:`~research_os.models.Verdict`
    enum, and deliberately the same three words the automation reviewer uses: it
    assesses proposed work, not science, and it must never be mistakable for a
    human Review.
    """

    PASS = "PASS"
    PASS_WITH_REPAIR = "PASS_WITH_REPAIR"
    FAIL = "FAIL"


class AssessmentFinding(BaseModel):
    """One thing an assessment says about one proposed item. Advisory."""

    model_config = ConfigDict(extra="forbid")

    item_id: str | None = None
    severity: NonBlankStr
    message: NonBlankStr

    @field_validator("item_id")
    @classmethod
    def _item_id_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if ITEM_ID_RE.fullmatch(value) is None:
            raise ValueError("item_id must look like PR-001")
        return value


class ProposalAssessment(BaseModel):
    """A read-only worker's structured judgement of a proposal.

    Never written into a capsule, never called a Review, and never sufficient
    for anything to become accepted science. Its verdict decides whether the
    controller offers the proposal to the human as it stands or flags it; the
    human decides everything after that.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    verdict: ProposalVerdict
    summary: NonBlankStr
    findings: list[AssessmentFinding] = Field(default_factory=list)
    provider: NonBlankStr
    model: str | None = None
    independence: str = "unknown"
    independence_note: str = ""
    invocation_id: str | None = None
    raw_output_path: str | None = None
    assessed_at: str = Field(default_factory=utc_now)

    @property
    def blocking(self) -> list[AssessmentFinding]:
        return [
            item
            for item in self.findings
            if item.severity.strip().lower() in {"blocker", "major"}
        ]


class PromotionRecord(BaseModel):
    """The record that a human turned one proposed item into a draft object.

    Written to the proposal store, not to the capsule: the capsule gets the
    object, and this says who asked for it, from which proposal, and what it
    became. It exists so a draft in a project can be traced back to the run that
    suggested it.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    item_id: str
    object_id: str
    object_type: str
    object_status: str
    project_path: str
    written_path: str
    promoted_at: str = Field(default_factory=utc_now)
    promoted_by: str = "human"
    basis: EvidenceBasis = EvidenceBasis.PROSPECTIVE
    note: str = ""


def _reject_unsupplied(
    item_id: str, label: str, cited: list[str], supplied: set[str]
) -> None:
    unknown = sorted(set(cited) - supplied)
    if unknown:
        raise ValueError(
            f"{item_id} cites {label}(s) that were not supplied to this "
            "proposal: " + ", ".join(unknown) + ". A proposal may only rest on "
            "what this run actually had in front of it"
        )
