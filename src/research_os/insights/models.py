"""Knowledge that was explicitly moved from one project to another.

A promoted insight is the only thing in Research OS that crosses a project
boundary, and everything about its shape is designed to stop it doing so
quietly.

**It is not a Claim.** A Claim belongs to one project, rests on that project's
Evidence, and was accepted through that project's human Review. An insight is
something a researcher decided is worth remembering *elsewhere*, and it arrives
in the new project labelled as a visitor: source project, scope, assumptions,
and the digest of what it came from all travel with it. A receiving worker is
shown those, not just the sentence, because "we found X" and "a different
project, under these assumptions, found X" are different pieces of information
and only the second one is true.

**It cannot be promoted automatically.** An agent may *nominate*; only a human
promotes. The two are different types in this module, stored in different
places, so the gap is structural rather than a rule somebody has to remember.

**It does not become a graph.** An insight links to its source and, when it is
replaced, to its successor. There is no ontology, no relation vocabulary, and no
inference over the links. A small set of human-readable facts that a person can
read and delete is worth more here than a structure nobody audits.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.automation.models import COMMIT_RE, Confidence, utc_now
from research_os.models import NonBlankStr

INSIGHT_ID_RE = re.compile(r"^INS-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
NOMINATION_ID_RE = re.compile(r"^NOM-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


class InsightStatus(StrEnum):
    """Whether an insight still applies.

    Superseded and retired insights stay visible rather than disappearing. An
    insight that turned out to be wrong is exactly the thing a researcher most
    needs to see when they reach for it again, and deleting it would let the
    same mistake be made twice in a project that never heard about the first
    time.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class PromotionType(StrEnum):
    """What kind of knowledge this is, which decides how far it travels.

    The distinction matters because these transfer very differently. A method
    that worked is often portable; a numerical result almost never is; a pitfall
    is portable precisely because it is about the tooling rather than the
    science.
    """

    METHOD = "method"
    PITFALL = "pitfall"
    TOOLING = "tooling"
    NEGATIVE_RESULT = "negative_result"
    OBSERVATION = "observation"


class SourceReference(BaseModel):
    """Where an insight came from, precisely enough to go and look.

    The digest is the point. A Claim's wording can change after an insight was
    drawn from it, and an insight that still quotes the old wording while
    pointing at the new object would be silently wrong. Recording the digest
    means a reader can tell whether the source still says what it said.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_path: str = ""
    object_id: str | None = None
    object_type: str | None = None
    object_digest: str | None = None
    commit: str | None = None
    run_id: str | None = None
    detail: str = ""

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        if PROJECT_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{value!r} is not a valid project id")
        return value

    @field_validator("commit")
    @classmethod
    def _commit_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("commit must be a full 40-character commit sha")
        return value


class PromotedInsight(BaseModel):
    """One piece of knowledge a human moved out of the project that produced it.

    ``scope`` and ``assumptions`` are required and non-empty, because an insight
    without them is a sentence that will be applied somewhere it does not hold.
    Making them mandatory is the cheapest available protection against the
    failure mode this whole feature risks: a true-in-one-place finding becoming
    a general belief by being repeated in enough prompts.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    insight_id: str
    title: NonBlankStr
    statement: NonBlankStr
    promotion_type: PromotionType
    source: SourceReference
    scope: NonBlankStr
    """Where this holds. The boundary the statement is true inside."""

    assumptions: list[NonBlankStr] = Field(min_length=1)
    """What had to be true for it. An insight with none is not scoped."""

    applicability: NonBlankStr
    """When another project should reach for this, and when it should not."""

    confidence: Confidence = Confidence.LOW
    status: InsightStatus = InsightStatus.ACTIVE
    promoted_by: NonBlankStr = "human"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    superseded_by: str | None = None
    retire_reason: str | None = None
    keywords: list[str] = Field(default_factory=list)
    nomination_id: str | None = None

    @field_validator("insight_id")
    @classmethod
    def _insight_id_shape(cls, value: str) -> str:
        if INSIGHT_ID_RE.fullmatch(value) is None:
            raise ValueError("insight_id must look like INS-20260912T101500Z-0a1b2c3d")
        return value

    @field_validator("superseded_by")
    @classmethod
    def _successor_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if INSIGHT_ID_RE.fullmatch(value) is None:
            raise ValueError("superseded_by must be an insight id")
        return value

    @model_validator(mode="after")
    def _retirement_is_explained(self) -> Self:
        """Refuse a retirement with no reason, or a successor with no supersession.

        Both directions matter. An insight retired without a reason tells a
        future reader nothing about why, which is the only genuinely useful part
        of a retirement. A successor recorded on an active insight would mean
        two insights are simultaneously in force about the same thing.
        """

        if (
            self.status is InsightStatus.RETIRED
            and not (self.retire_reason or "").strip()
        ):
            raise ValueError(
                "a retired insight must say why. An insight that turned out to "
                "be wrong is exactly what a future reader needs, and the reason "
                "is the useful part"
            )
        if self.status is InsightStatus.SUPERSEDED and not self.superseded_by:
            raise ValueError("a superseded insight must name what replaced it")
        if self.superseded_by and self.status is InsightStatus.ACTIVE:
            raise ValueError(
                "an active insight must not name a successor; two insights "
                "cannot both be in force about the same thing"
            )
        if self.superseded_by == self.insight_id:
            raise ValueError("an insight cannot supersede itself")
        return self

    @property
    def live(self) -> bool:
        return self.status is InsightStatus.ACTIVE

    def search_text(self) -> str:
        """Return everything this insight is matched on, as one string."""

        return " ".join(
            [
                self.title,
                self.statement,
                self.scope,
                self.applicability,
                " ".join(self.assumptions),
                " ".join(self.keywords),
                self.source.project_id,
            ]
        )


class InsightNomination(BaseModel):
    """A candidate an agent put forward. Runtime state, not knowledge.

    Deliberately a different type in a different store. An agent may notice that
    something is worth remembering elsewhere and say so; what it may never do is
    put that thing where another project's workers will read it. Promotion is a
    human act, and making nomination a separate object means the gap cannot be
    closed by setting a field.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    nomination_id: str
    title: NonBlankStr
    statement: NonBlankStr
    promotion_type: PromotionType
    source: SourceReference
    scope: str = ""
    assumptions: list[str] = Field(default_factory=list)
    applicability: str = ""
    confidence: Confidence = Confidence.LOW
    rationale: NonBlankStr
    nominated_by: NonBlankStr
    created_at: str = Field(default_factory=utc_now)
    promoted_insight_id: str | None = None
    declined_reason: str | None = None

    @field_validator("nomination_id")
    @classmethod
    def _nomination_id_shape(cls, value: str) -> str:
        if NOMINATION_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "nomination_id must look like NOM-20260912T101500Z-0a1b2c3d"
            )
        return value

    @property
    def pending(self) -> bool:
        return self.promoted_insight_id is None and self.declined_reason is None

    @property
    def missing_for_promotion(self) -> list[str]:
        """Return what a human would still have to supply to promote this.

        A nomination may arrive incomplete, and that is fine: an agent noticing
        something is useful even when it cannot say where the finding holds. The
        human filling in the scope is the human deciding it transfers.
        """

        missing: list[str] = []
        if not self.scope.strip():
            missing.append("scope")
        if not [item for item in self.assumptions if item.strip()]:
            missing.append("assumptions")
        if not self.applicability.strip():
            missing.append("applicability")
        return missing


class InsightMatch(BaseModel):
    """One retrieved insight and why it matched. Ranked deterministically."""

    model_config = ConfigDict(extra="forbid")

    insight: PromotedInsight
    score: float
    matched_terms: list[str] = Field(default_factory=list)
