"""Shared error, finding, and result primitives for Research OS."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

E_DUP_ID = "E_DUP_ID"
E_DANGLING_REF = "E_DANGLING_REF"
E_WRONG_REF_TYPE = "E_WRONG_REF_TYPE"
E_CREATED_FROM_CYCLE = "E_CREATED_FROM_CYCLE"
E_SUPERSESSION_CYCLE = "E_SUPERSESSION_CYCLE"
E_SUPERSEDED_WITHOUT_SUCCESSOR = "E_SUPERSEDED_WITHOUT_SUCCESSOR"
E_WITHDRAWN_SUPERSEDED = "E_WITHDRAWN_SUPERSEDED"
E_NONQUALIFYING_EVIDENCE = "E_NONQUALIFYING_EVIDENCE"
E_ACCEPTED_WITHOUT_EVIDENCE = "E_ACCEPTED_WITHOUT_EVIDENCE"
E_ACCEPTED_WITHOUT_HUMAN_REVIEW = "E_ACCEPTED_WITHOUT_HUMAN_REVIEW"
E_STALE_REVIEW_DIGEST = "E_STALE_REVIEW_DIGEST"
E_REVIEW_OF_REVIEW = "E_REVIEW_OF_REVIEW"

W_STALE_SUBJECT_DIGEST = "W_STALE_SUBJECT_DIGEST"
W_PROMOTED_WITHOUT_HYPOTHESIS = "W_PROMOTED_WITHOUT_HYPOTHESIS"


class ResearchOSError(Exception):
    """Base exception for Research OS kernel failures."""


class InvalidIdError(ResearchOSError, ValueError):
    """Raised when a scientific or project identifier is malformed."""


class Severity(StrEnum):
    """Finding severity for deterministic validation reports."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One deterministic validation finding.

    M3 formats these; M2 only produces structured data.
    """

    severity: Severity
    code: str
    message: str
    object_id: str | None = None
    field: str | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Deterministic collection of validation findings."""

    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.INFO)

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.findings)
