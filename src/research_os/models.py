"""Pydantic v2 models for Research Capsule scientific objects.

Local field semantics live here. Cross-object rules live in ``validate.py``.
Models never consult the filesystem, SQLite, or a global registry.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from research_os.ids import (
    PREFIX_TO_TYPE,
    parse_id,
    validate_id,
    validate_project_id,
)

DIGEST_VERSION = 1
DIGEST_RE = re.compile(rf"^{DIGEST_VERSION}:[0-9a-f]{{64}}$")
DIGEST_FORMAT = f"{DIGEST_VERSION}:<64 lowercase hex>"

_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_REVIEWABLE_PREFIXES = frozenset(
    {"Q", "IDEA", "HYP", "ASM", "CLAIM", "DEC", "EXP", "EVI"}
)


def _reject_blank(value: str) -> str:
    """Reject whitespace-only strings without rewriting canonical text."""

    if not value.strip():
        raise ValueError("must contain at least one non-whitespace character")
    return value


NonBlankStr = Annotated[str, AfterValidator(_reject_blank)]


def _repo_relative_path(value: str) -> str:
    """Reject pointers that cannot be a portable in-repository path.

    Format validation only: nothing is resolved, normalized, or read from
    disk. A single trailing slash is allowed because a directory pointer is
    legitimate. The value is never rewritten, because the semantic digest
    hashes the literal string.
    """

    if value.startswith(("/", "~")):
        raise ValueError("must be a repository-relative path, not absolute")
    if "\\" in value:
        raise ValueError("must use POSIX '/' separators")
    if _DRIVE_PREFIX_RE.match(value) is not None:
        raise ValueError("must not be a drive-qualified path")
    segments = value.split("/")
    if segments[-1] == "":
        segments = segments[:-1]
    if not segments:
        raise ValueError("must name at least one path segment")
    for segment in segments:
        if segment in {"", ".", ".."}:
            raise ValueError("must not contain empty, '.', or '..' path segments")
    return value


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
    title: NonBlankStr
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
    """Completed-experiment provenance pointers. Not resolved in R0.

    ``code`` and ``config`` are repository-relative POSIX paths, because
    reproducing an analysis requires the committed code and configuration.
    ``data`` is a nonblank **opaque locator**: real datasets routinely live
    outside Git and outside the project filesystem (scratch space,
    institutional or HPC storage, object stores, DOIs, dataset or table
    identifiers), so WP-A validates only that it is present. Nothing here is
    resolved, fetched, hashed, or checked for existence.
    """

    model_config = ConfigDict(extra="forbid")

    code: NonBlankStr
    config: NonBlankStr
    data: NonBlankStr
    git_commit: NonBlankStr

    @field_validator("code", "config")
    @classmethod
    def _repository_paths(cls, value: str) -> str:
        return _repo_relative_path(value)

    @field_validator("git_commit")
    @classmethod
    def _commit_hash_shape(cls, value: str) -> str:
        if _GIT_COMMIT_RE.fullmatch(value) is None:
            raise ValueError("git_commit must be 7-40 lowercase hexadecimal characters")
        return value


class Prediction(BaseModel):
    """One ex-ante predicted outcome for a hypothesis under test.

    A nested value model like ``Provenance``, not a prefixed scientific
    object type.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    predicted_outcome: NonBlankStr
    discriminates: bool

    @field_validator("hypothesis")
    @classmethod
    def _hypothesis_id(cls, value: str) -> str:
        validate_id(value)
        if parse_id(value).prefix != "HYP":
            raise ValueError("prediction hypothesis must be a HYP- id")
        return value


class Question(BaseScientificObject):
    type: Literal[ObjectType.QUESTION]
    status: QuestionStatus
    statement: NonBlankStr


class Idea(BaseScientificObject):
    type: Literal[ObjectType.IDEA]
    status: IdeaStatus
    statement: NonBlankStr
    retire_reason: str | None = None
    revisit_if: str | None = None

    @field_validator("retire_reason", "revisit_if")
    @classmethod
    def _optional_text_non_empty(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty when set")
        return value

    @model_validator(mode="after")
    def _idea_retirement_rules(self) -> Self:
        if self.status is IdeaStatus.DISCARDED and (
            self.retire_reason is None or not self.retire_reason.strip()
        ):
            raise ValueError("discarded ideas require non-empty retire_reason")
        return self


_RETIRED_HYPOTHESIS_STATUSES = frozenset(
    {HypothesisStatus.REJECTED, HypothesisStatus.WITHDRAWN}
)


class Hypothesis(BaseScientificObject):
    type: Literal[ObjectType.HYPOTHESIS]
    status: HypothesisStatus
    statement: NonBlankStr
    falsification: str | None = None
    mechanism: str | None = None
    addresses: list[str] | None = None
    assumptions: list[str] | None = None
    supporting_evidence: list[str] | None = None
    contrary_evidence: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_basis: str | None = None
    retire_reason: str | None = None
    revisit_if: str | None = None

    @field_validator("retire_reason", "revisit_if")
    @classmethod
    def _optional_text_non_empty(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty when set")
        return value

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
            raise ValueError("confidence must be a numeric scalar")
        if type(value) is int:
            return float(value)
        if type(value) is float:
            return value
        raise ValueError("confidence must be a numeric scalar")

    @model_validator(mode="after")
    def _hypothesis_content_rules(self) -> Self:
        if self.status is not HypothesisStatus.DRAFT and (
            self.falsification is None or not self.falsification.strip()
        ):
            raise ValueError("non-draft hypotheses require non-empty falsification")
        if self.confidence_basis is not None and not self.confidence_basis.strip():
            raise ValueError(
                "confidence_basis must contain at least one non-whitespace character"
            )
        has_confidence = self.confidence is not None
        has_basis = (
            self.confidence_basis is not None and bool(self.confidence_basis.strip())
        )
        if has_confidence and not has_basis:
            raise ValueError("confidence requires non-empty confidence_basis")
        if self.confidence_basis is not None and not has_confidence:
            raise ValueError("confidence_basis requires confidence")
        if self.status in _RETIRED_HYPOTHESIS_STATUSES and (
            self.retire_reason is None or not self.retire_reason.strip()
        ):
            raise ValueError(
                "rejected and withdrawn hypotheses require non-empty retire_reason"
            )
        return self


class Assumption(BaseScientificObject):
    type: Literal[ObjectType.ASSUMPTION]
    status: AssumptionStatus
    statement: NonBlankStr
    scope: NonBlankStr


class Claim(BaseScientificObject):
    type: Literal[ObjectType.CLAIM]
    status: ClaimStatus
    statement: NonBlankStr
    supporting_evidence: list[str] | None = None
    contrary_evidence: list[str] | None = None
    contrary_evidence_addressed: str | None = None
    hypotheses: list[str] | None = None

    @field_validator("supporting_evidence", "contrary_evidence", "hypotheses")
    @classmethod
    def _optional_id_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            validate_id(item)
        return value

    @field_validator("contrary_evidence_addressed")
    @classmethod
    def _addressed_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("contrary_evidence_addressed must be non-empty when set")
        return value

    @model_validator(mode="after")
    def _claim_content_rules(self) -> Self:
        if self.contrary_evidence_addressed is not None and not self.contrary_evidence:
            raise ValueError("contrary_evidence_addressed requires contrary_evidence")
        addressed = self.contrary_evidence_addressed is not None and bool(
            self.contrary_evidence_addressed.strip()
        )
        if (
            self.status is ClaimStatus.ACCEPTED
            and self.contrary_evidence
            and not addressed
        ):
            raise ValueError(
                "accepted claims with contrary_evidence require non-empty "
                "contrary_evidence_addressed"
            )
        return self


class Decision(BaseScientificObject):
    type: Literal[ObjectType.DECISION]
    status: DecisionStatus
    statement: NonBlankStr
    rationale: NonBlankStr
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
            if any(not item.strip() for item in self.alternatives_considered):
                raise ValueError(
                    "alternatives_considered entries must be non-empty"
                )
        return self


_PREREGISTERED_EXPERIMENT_STATUSES = frozenset(
    {
        ExperimentStatus.SPECIFIED,
        ExperimentStatus.RUNNING,
        ExperimentStatus.COMPLETED,
        ExperimentStatus.FAILED,
        ExperimentStatus.SUPERSEDED,
    }
)


class Experiment(BaseScientificObject):
    type: Literal[ObjectType.EXPERIMENT]
    status: ExperimentStatus
    purpose: NonBlankStr
    hypotheses: list[str] | None = None
    predictions: list[Prediction] | None = None
    primary_metrics: list[str] | None = None
    decision_rule: str | None = None
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
        if any(not item.strip() for item in value):
            raise ValueError("artifact pointers must be non-empty")
        return value

    @field_validator("primary_metrics")
    @classmethod
    def _primary_metric_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value):
            raise ValueError("primary_metrics entries must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("primary_metrics must not contain duplicates")
        return value

    @field_validator("result_manifest", "decision_rule")
    @classmethod
    def _optional_text_non_empty(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty when set")
        return value

    @model_validator(mode="after")
    def _experiment_status_rules(self) -> Self:
        if self.status is not ExperimentStatus.DRAFT and not self.hypotheses:
            raise ValueError("non-draft experiments require a non-empty hypotheses list")
        if self.status is ExperimentStatus.COMPLETED and self.provenance is None:
            raise ValueError("completed experiments require provenance")
        if self.status in _PREREGISTERED_EXPERIMENT_STATUSES:
            if not self.predictions:
                raise ValueError(
                    "preregistered experiments require a non-empty predictions list"
                )
            if not self.primary_metrics:
                raise ValueError(
                    "preregistered experiments require at least one primary metric"
                )
            if self.decision_rule is None or not self.decision_rule.strip():
                raise ValueError(
                    "preregistered experiments require a non-empty decision_rule"
                )
        declared = set(self.hypotheses or [])
        for prediction in self.predictions or []:
            if prediction.hypothesis not in declared:
                raise ValueError(
                    f"prediction hypothesis {prediction.hypothesis} is not listed "
                    "in the experiment hypotheses"
                )
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
    evidence_digests: dict[str, str] | None = None

    @field_validator("subject")
    @classmethod
    def _reviewable_subject(cls, value: str) -> str:
        parsed = parse_id(value)
        if parsed.prefix not in _REVIEWABLE_PREFIXES:
            raise ValueError("reviews cannot target reviews")
        return value

    @field_validator("subject_digest")
    @classmethod
    def _versioned_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if DIGEST_RE.fullmatch(value) is None:
            raise ValueError(f"subject_digest must be {DIGEST_FORMAT!r}")
        return value

    @field_validator("evidence_digests")
    @classmethod
    def _evidence_digest_map(
        cls,
        value: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if value is None:
            return None
        for key, digest in value.items():
            validate_id(key)
            if parse_id(key).prefix != "EVI":
                raise ValueError("evidence_digests keys must be EVI- ids")
            if DIGEST_RE.fullmatch(digest) is None:
                raise ValueError(f"evidence_digests[{key}] must be {DIGEST_FORMAT!r}")
        return value

    @model_validator(mode="after")
    def _concluded_review_fields(self) -> Self:
        if self.status is ReviewStatus.CONCLUDED:
            if self.findings is None or not self.findings.strip():
                raise ValueError("concluded reviews require findings")
            if self.verdict is None:
                raise ValueError("concluded reviews require verdict")
            if self.subject_digest is None:
                raise ValueError("concluded reviews require subject_digest")
        return self

    @model_validator(mode="after")
    def _evidence_digests_target_claims(self) -> Self:
        if (
            self.evidence_digests is not None
            and parse_id(self.subject).prefix != "CLAIM"
        ):
            raise ValueError("evidence_digests is only valid on reviews of claims")
        return self


class Evidence(BaseScientificObject):
    type: Literal[ObjectType.EVIDENCE]
    status: EvidenceStatus
    statement: NonBlankStr
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
        if self.kind is EvidenceKind.LITERATURE and (
            self.citation is None or not self.citation.strip()
        ):
            raise ValueError("literature evidence requires citation")
        if self.kind is EvidenceKind.EXPERIMENT:
            if not self.experiment:
                raise ValueError("experiment evidence requires an experiment id")
        elif self.experiment is not None:
            raise ValueError(
                "experiment pointer is only valid for experiment-kind evidence"
            )
        if self.kind is EvidenceKind.OTHER:
            citation_ok = self.citation is not None and bool(self.citation.strip())
            notes_ok = self.notes is not None and bool(self.notes.strip())
            if not citation_ok and not notes_ok:
                raise ValueError("other evidence requires citation or notes")
        return self


class Project(BaseModel):
    """Project identity in ``project.yaml``. Not a prefixed scientific object."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: NonBlankStr
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
