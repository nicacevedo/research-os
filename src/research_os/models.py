"""Pydantic v2 models for Research Capsule scientific objects.

Local field semantics live here. Cross-object rules live in ``validate.py``.
Models never consult the filesystem, SQLite, or a global registry.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.ids import (
    PREFIX_TO_TYPE,
    parse_id,
    validate_id,
    validate_project_id,
)

_SUBJECT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEWABLE_PREFIXES = frozenset(
    {"Q", "IDEA", "HYP", "ASM", "CLAIM", "DEC", "EXP", "EVI"}
)


class ObjectType(StrEnum):
    """Canonical scientific object type names."""

    QUESTION = "question"
    IDEA = "idea"
    HYPOTHESIS = "hypothesis"
    ASSUMPTION = "assumption"
    CLAIM = "claim"
    DECISION = "decision"
    EXPERIMENT = "experiment"
    REVIEW = "review"
    EVIDENCE = "evidence"


class QuestionStatus(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class IdeaStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PROMOTED = "promoted"
    DISCARDED = "discarded"
    SUPERSEDED = "superseded"


class HypothesisStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    TESTING = "testing"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class AssumptionStatus(StrEnum):
    ACTIVE = "active"
    RELAXED = "relaxed"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ClaimStatus(StrEnum):
    DRAFT = "draft"
    EVIDENCE_LINKED = "evidence_linked"
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    SPECIFIED = "specified"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    CONCLUDED = "concluded"
    WITHDRAWN = "withdrawn"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class EvidenceKind(StrEnum):
    LITERATURE = "literature"
    EXPERIMENT = "experiment"
    OTHER = "other"


class ReviewerKind(StrEnum):
    HUMAN = "human"
    INDEPENDENT_AGENT = "independent_agent"


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class ObjectEnvelope(BaseModel):
    """Shared scientific-object envelope without supersession."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: ObjectType
    schema_version: Literal[1]
    status: str
    title: str = Field(min_length=1)
    notes: str | None = None
    created_from: list[str] = Field(default_factory=list)

    @field_validator("created_from")
    @classmethod
    def _created_from_are_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            validate_id(item)
        return value

    @model_validator(mode="after")
    def _id_matches_declared_type(self) -> Self:
        parsed = parse_id(self.id)
        expected = PREFIX_TO_TYPE[parsed.prefix]
        if expected != self.type:
            raise ValueError(
                f"id prefix {parsed.prefix!r} does not match type {self.type!r}"
            )
        return self


class BaseScientificObject(ObjectEnvelope):
    """Envelope for objects that may participate in supersession."""

    supersedes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _supersedes_are_unique_same_type(self) -> Self:
        seen: set[str] = set()
        own_prefix = parse_id(self.id).prefix
        for item in self.supersedes:
            parsed = parse_id(item)
            if item in seen:
                raise ValueError("supersedes must not contain duplicate ids")
            seen.add(item)
            if parsed.prefix != own_prefix:
                raise ValueError("supersedes must list same-type object ids")
            if item == self.id:
                raise ValueError("an object cannot supersede itself")
        return self


class Provenance(BaseModel):
    """Completed-experiment provenance pointers. Not resolved in R0."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    config: str = Field(min_length=1)
    data: str = Field(min_length=1)
    git_commit: str = Field(min_length=1)


class Question(BaseScientificObject):
    type: Literal[ObjectType.QUESTION]
    status: QuestionStatus
    statement: str = Field(min_length=1)


class Idea(BaseScientificObject):
    type: Literal[ObjectType.IDEA]
    status: IdeaStatus
    statement: str = Field(min_length=1)


class Hypothesis(BaseScientificObject):
    type: Literal[ObjectType.HYPOTHESIS]
    status: HypothesisStatus
    statement: str = Field(min_length=1)
    falsification: str | None = None
    mechanism: str | None = None
    addresses: list[str] | None = None
    assumptions: list[str] | None = None
    supporting_evidence: list[str] | None = None
    contrary_evidence: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_basis: str | None = None

    @field_validator(
        "addresses",
        "assumptions",
        "supporting_evidence",
        "contrary_evidence",
    )
    @classmethod
    def _optional_id_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            validate_id(item)
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_is_number(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is bool:
            raise ValueError("confidence must be a number between 0 and 1")
        if type(value) is int:
            return float(value)
        return value

    @model_validator(mode="after")
    def _hypothesis_content_rules(self) -> Self:
        if self.status is not HypothesisStatus.DRAFT and not self.falsification:
            raise ValueError("non-draft hypotheses require non-empty falsification")
        has_confidence = self.confidence is not None
        has_basis = bool(self.confidence_basis)
        if has_confidence and not has_basis:
            raise ValueError("confidence requires non-empty confidence_basis")
        if self.confidence_basis is not None and not has_confidence:
            raise ValueError("confidence_basis requires confidence")
        return self


class Assumption(BaseScientificObject):
    type: Literal[ObjectType.ASSUMPTION]
    status: AssumptionStatus
    statement: str = Field(min_length=1)
    scope: str = Field(min_length=1)


class Claim(BaseScientificObject):
    type: Literal[ObjectType.CLAIM]
    status: ClaimStatus
    statement: str = Field(min_length=1)
    evidence: list[str] | None = None
    hypotheses: list[str] | None = None

    @field_validator("evidence", "hypotheses")
    @classmethod
    def _optional_id_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            validate_id(item)
        return value


class Decision(BaseScientificObject):
    type: Literal[ObjectType.DECISION]
    status: DecisionStatus
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    alternatives_considered: list[str] | None = None
    related: list[str] | None = None

    @field_validator("related")
    @classmethod
    def _related_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            validate_id(item)
        return value

    @model_validator(mode="after")
    def _accepted_requires_alternatives(self) -> Self:
        if self.status is DecisionStatus.ACCEPTED:
            if not self.alternatives_considered:
                raise ValueError(
                    "accepted decisions require non-empty alternatives_considered"
                )
            if any(not item for item in self.alternatives_considered):
                raise ValueError(
                    "alternatives_considered entries must be non-empty"
                )
        return self


class Experiment(BaseScientificObject):
    type: Literal[ObjectType.EXPERIMENT]
    status: ExperimentStatus
    purpose: str = Field(min_length=1)
    hypotheses: list[str] | None = None
    provenance: Provenance | None = None
    result_manifest: str | None = None
    artifacts: list[str] | None = None

    @field_validator("hypotheses")
    @classmethod
    def _hypothesis_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            validate_id(item)
        return value

    @field_validator("artifacts")
    @classmethod
    def _artifact_pointers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item for item in value):
            raise ValueError("artifact pointers must be non-empty")
        return value

    @field_validator("result_manifest")
    @classmethod
    def _result_manifest_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("result_manifest must be non-empty when set")
        return value

    @model_validator(mode="after")
    def _experiment_status_rules(self) -> Self:
        if self.status is not ExperimentStatus.DRAFT and not self.hypotheses:
            raise ValueError("non-draft experiments require a non-empty hypotheses list")
        if self.status is ExperimentStatus.COMPLETED and self.provenance is None:
            raise ValueError("completed experiments require provenance")
        return self


class Review(ObjectEnvelope):
    """Reviews do not use ``supersedes`` in R0."""

    type: Literal[ObjectType.REVIEW]
    status: ReviewStatus
    subject: str
    reviewer_kind: ReviewerKind
    findings: str | None = None
    verdict: Verdict | None = None
    subject_digest: str | None = None

    @field_validator("subject")
    @classmethod
    def _reviewable_subject(cls, value: str) -> str:
        parsed = parse_id(value)
        if parsed.prefix not in _REVIEWABLE_PREFIXES:
            raise ValueError("reviews cannot target reviews")
        return value

    @field_validator("subject_digest")
    @classmethod
    def _lowercase_hex_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _SUBJECT_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("subject_digest must be 64-char lowercase hex SHA-256")
        return value

    @model_validator(mode="after")
    def _concluded_review_fields(self) -> Self:
        if self.status is ReviewStatus.CONCLUDED:
            if not self.findings:
                raise ValueError("concluded reviews require findings")
            if self.verdict is None:
                raise ValueError("concluded reviews require verdict")
            if self.subject_digest is None:
                raise ValueError("concluded reviews require subject_digest")
        return self


class Evidence(BaseScientificObject):
    type: Literal[ObjectType.EVIDENCE]
    status: EvidenceStatus
    statement: str = Field(min_length=1)
    kind: EvidenceKind
    citation: str | None = None
    global_ref: str | None = None
    locator: str | None = None
    experiment: str | None = None

    @field_validator("experiment")
    @classmethod
    def _experiment_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_id(value)
        return value

    @model_validator(mode="after")
    def _kind_conditioned_fields(self) -> Self:
        if self.kind is EvidenceKind.LITERATURE and not self.citation:
            raise ValueError("literature evidence requires citation")
        if self.kind is EvidenceKind.EXPERIMENT and not self.experiment:
            raise ValueError("experiment evidence requires an experiment id")
        if self.kind is EvidenceKind.OTHER and not self.citation and not self.notes:
            raise ValueError("other evidence requires citation or notes")
        return self


class Project(BaseModel):
    """Project identity in ``project.yaml``. Not a prefixed scientific object."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1)
    capsule_version: Literal[1]
    status: ProjectStatus
    description: str | None = None

    @field_validator("id")
    @classmethod
    def _project_slug(cls, value: str) -> str:
        return validate_project_id(value)


ScientificObject = (
    Question
    | Idea
    | Hypothesis
    | Assumption
    | Claim
    | Decision
    | Experiment
    | Review
    | Evidence
)

Reviewable = (
    Question | Idea | Hypothesis | Assumption | Claim | Decision | Experiment | Evidence
)

TYPE_TO_MODEL: dict[str, type[ScientificObject]] = {
    ObjectType.QUESTION: Question,
    ObjectType.IDEA: Idea,
    ObjectType.HYPOTHESIS: Hypothesis,
    ObjectType.ASSUMPTION: Assumption,
    ObjectType.CLAIM: Claim,
    ObjectType.DECISION: Decision,
    ObjectType.EXPERIMENT: Experiment,
    ObjectType.REVIEW: Review,
    ObjectType.EVIDENCE: Evidence,
}


def parse_object(data: dict[str, Any]) -> ScientificObject:
    """Parse a mapping into a typed scientific object.

    Does not load YAML, walk capsules, or consult a registry.
    """

    raw_type = data.get("type")
    model = TYPE_TO_MODEL.get(raw_type) if isinstance(raw_type, str) else None
    if model is None:
        raise ValueError(f"unknown object type: {raw_type!r}")
    return model.model_validate(data)
