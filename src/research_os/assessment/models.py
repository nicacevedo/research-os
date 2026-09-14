"""A grounded technical assessment: what a repository is, without pretending it is science.

A great many research repositories have no Research Capsule. A solver, a
simulation code, a data pipeline: real work, under version control, with no
Question or Claim objects in it and no reason to have them yet. v1.0.0 could not
say anything useful about one. Asked to assess such a repository, the proposal
worker wrote a scientific proposal, cited ``PR-002`` and ``PR-003`` as if the
project held them, and the grounding validator refused it -- correctly.

The refusal was right and the request was reasonable. What was wrong was the
**universe**: the worker was asked for scientific proposals about a project that
has no scientific objects, so the only citations available to it were ones it
had to invent.

A :class:`TechnicalAssessment` is what that worker should have been asked for.
It is a reading of a repository at a pinned commit, grounded in things that
actually exist there -- files, symbols, deterministic checks -- plus whatever
literature the run retrieved. Four properties keep it honest.

**It is not a proposal and not science.** It has no Question, no Hypothesis, no
Claim, no Evidence. Nothing in it is promotable into a capsule object, there is
no promotion path from it, and it is never written under ``.research/``. The
type is separate from :class:`~research_os.proposal.models.ResearchProposal` so
that no future code can confuse the two by accident.

**Its grounding is deterministic.** A file reference must name a path that is
tracked at the assessment's own base commit. Not "looks plausible": present.

**Scientific identifiers fail closed.** The field that could carry one is
validated against a supplied allowlist which, in repository-assessment mode, the
controller always leaves empty. A worker that writes ``CLAIM-0001`` or
``PR-002`` is refused, and no amount of confidence on its part changes that.

**It says what it could not establish.** An assessment that reports no
uncertainty is asserting there is none, and about an unfamiliar repository that
is almost never true.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.automation.models import Confidence, Importance, utc_now
from research_os.models import NonBlankStr

ASSESSMENT_ID_RE = re.compile(r"^TA-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
OBSERVATION_ID_RE = re.compile(r"^OB-[0-9]{3}$")

#: Characters a repository path may not contain in a reference.
#:
#: Control characters have no business in a path a person will read or a tool
#: will open, and a reference carrying one is either a mangled answer or an
#: attempt to break a display. Either way it is not a path in this repository.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: A symbol reference: a function, class, method, or module path.
#:
#: Deliberately permissive about language and strict about shape. ``solve``,
#: ``Solver.step``, ``cuPDLP::restart`` and ``Module.fn!`` are all things a
#: reader can find; anything with whitespace or a control character is not a
#: symbol, it is a sentence.
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:!?<>-]{0,127}$")


class AssessmentMode(StrEnum):
    """Which grounding universe produced this assessment.

    Recorded on the object rather than inferred from what it happens to cite, so
    a reader can tell at a glance that this was a repository reading and not a
    scientific one -- including when the repository later gains a capsule.
    """

    REPOSITORY_ASSESSMENT = "repository_assessment"


class ActionKind(StrEnum):
    """The kind of work a recommended next action is.

    The same closed vocabulary the proposal layer uses, minus the kinds that
    have no meaning without a capsule. ``HUMAN_DECISION`` always stops.
    """

    DETERMINISTIC = "deterministic"
    LITERATURE = "literature"
    ANALYSIS = "analysis"
    CODE = "code"
    EXPERIMENT = "experiment"
    INITIALISE_CAPSULE = "initialise_capsule"
    """Suggest the researcher start a Research Capsule here.

    A suggestion and nothing more. Research OS never creates a capsule on its
    own: that is a decision about whether a repository is a scientific project,
    and it belongs to the person whose project it is.
    """

    HUMAN_DECISION = "human_decision"


class AssessmentGrounding(BaseModel):
    """Exactly what an assessment was allowed to cite.

    Built by the controller from what it actually supplied -- the tracked files
    at the base commit, the literature keys this run retrieved, the check ids
    this project has profiles for -- and never by the worker. ``capsule_ids`` is
    present and empty in repository-assessment mode, which is what makes an
    invented ``CLAIM-0001`` a validation failure rather than an unmodelled case.
    """

    model_config = ConfigDict(extra="forbid")

    repository_files: list[str] = Field(default_factory=list)
    literature_keys: list[str] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    capsule_ids: list[str] = Field(default_factory=list)
    base_commit: str | None = None


class FileRef(BaseModel):
    """One repository-relative reference, with an optional symbol.

    Path plus symbol rather than path plus line number, deliberately. A line
    number is correct for about as long as nobody edits the file, and an
    assessment whose references rot on the next commit is an assessment nobody
    will trust a month later. A path is stable, a symbol is nearly so, and both
    are checkable.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    symbol: str | None = None
    note: str = ""

    @field_validator("path")
    @classmethod
    def _repository_relative(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("a file reference must name a path")
        if _CONTROL.search(cleaned):
            raise ValueError(
                f"{value!r} contains a control character; a repository path does not"
            )
        if cleaned.startswith("/"):
            raise ValueError(
                f"{cleaned!r} is absolute; a file reference is relative to the "
                "repository root"
            )
        if "\\" in cleaned:
            raise ValueError(
                f"{cleaned!r} uses backslashes; repository paths are POSIX-style"
            )
        segments = cleaned.split("/")
        if any(part in {"", ".", ".."} for part in segments):
            raise ValueError(
                f"{cleaned!r} is not a normalised repository path; '.', '..' and "
                "empty segments cannot address a file in this repository"
            )
        return cleaned

    @field_validator("symbol")
    @classmethod
    def _symbol_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if _SYMBOL.fullmatch(cleaned) is None:
            raise ValueError(
                f"{value!r} is not a symbol reference. A symbol is a name such as "
                "'solve' or 'Solver.step'; put prose in the note instead."
            )
        return cleaned


class Observation(BaseModel):
    """One thing this assessment establishes about the repository.

    Every observation must rest on something: a file in the repository, a
    retrieved work, or a deterministic check. An observation resting on nothing
    is an opinion, and an opinion presented beside grounded findings is worse
    than no finding at all.
    """

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    statement: NonBlankStr
    rationale: NonBlankStr
    file_refs: list[FileRef] = Field(default_factory=list)
    literature_keys: list[str] = Field(default_factory=list)
    check_ids: list[str] = Field(default_factory=list)
    related_capsule_ids: list[str] = Field(default_factory=list)
    """Scientific objects this observation bears on, by id.

    Present so the fail-closed rule has somewhere to live. In
    repository-assessment mode the controller supplies no capsule ids at all, so
    any value here is unsupplied and the assessment is refused.
    """

    importance: Importance = Importance.MEDIUM
    confidence: Confidence = Confidence.LOW

    @field_validator("observation_id")
    @classmethod
    def _observation_id_shape(cls, value: str) -> str:
        if OBSERVATION_ID_RE.fullmatch(value) is None:
            raise ValueError("observation_id must look like OB-001")
        return value

    @model_validator(mode="after")
    def _rests_on_something(self) -> Self:
        if not (self.file_refs or self.literature_keys or self.check_ids):
            raise ValueError(
                f"{self.observation_id} rests on nothing: name the repository "
                "file, the retrieved work, or the deterministic check it comes "
                "from. An ungrounded observation is an opinion."
            )
        return self


class Uncertainty(BaseModel):
    """Something the assessment could not settle, and what would settle it."""

    model_config = ConfigDict(extra="forbid")

    statement: NonBlankStr
    what_would_settle_it: NonBlankStr
    blocks: list[str] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    """The highest-value unresolved question this repository poses.

    One, not a list. Asked for in the singular because the value of this field
    is the ranking: a worker that returns five questions has not done the part
    that was hard.
    """

    model_config = ConfigDict(extra="forbid")

    question: NonBlankStr
    why_it_matters: NonBlankStr
    what_would_answer_it: NonBlankStr
    blocked_by_evidence: bool = False
    """Whether answering it needs evidence this run could not obtain.

    True is a legitimate and useful answer. A grounded statement that the
    decisive evidence is missing changes what a researcher does next; a
    confident answer built on the evidence that happened to be available does
    not.
    """


class NextAction(BaseModel):
    """One recommended next step, typed so a controller could dispatch it."""

    model_config = ConfigDict(extra="forbid")

    action: NonBlankStr
    kind: ActionKind
    rationale: NonBlankStr
    addresses_observations: list[str] = Field(default_factory=list)
    requires_human: bool = False

    @model_validator(mode="after")
    def _human_decisions_require_a_human(self) -> Self:
        if self.kind is ActionKind.HUMAN_DECISION and not self.requires_human:
            raise ValueError("an action of kind human_decision must set requires_human")
        return self


class TechnicalAssessment(BaseModel):
    """A complete, validated, explicitly non-scientific reading of a repository."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    assessment_id: str
    mode: AssessmentMode = AssessmentMode.REPOSITORY_ASSESSMENT
    project_id: str | None = None
    project_path: NonBlankStr
    base_commit: str | None = None
    goal: NonBlankStr
    created_at: str = Field(default_factory=utc_now)
    summary: NonBlankStr
    grounding: AssessmentGrounding = Field(default_factory=AssessmentGrounding)
    observations: list[Observation] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    open_question: OpenQuestion | None = None
    next_actions: list[NextAction] = Field(default_factory=list)
    capsule_suggestion: str = ""
    """Why a Research Capsule might be worth starting here. Advice, never an act."""

    provider: NonBlankStr = "unknown"
    model: str | None = None
    invocation_id: str | None = None
    raw_output_path: str | None = None

    @field_validator("assessment_id")
    @classmethod
    def _assessment_id_shape(cls, value: str) -> str:
        if ASSESSMENT_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "assessment_id must look like TA-20260912T101500Z-0a1b2c3d"
            )
        return value

    @model_validator(mode="after")
    def _everything_cited_was_supplied(self) -> Self:
        """Refuse an assessment resting on anything this run did not have.

        The same rule the proposal layer enforces, against a different universe.
        A file that is not tracked at the base commit is not a file this
        assessment may cite: it may have been deleted, renamed, or never existed,
        and a reference a reader cannot open is indistinguishable from an
        invented one.
        """

        ids = [item.observation_id for item in self.observations]
        if len(set(ids)) != len(ids):
            raise ValueError("observation ids must not repeat")
        for index, observation in enumerate(self.observations, start=1):
            expected = f"OB-{index:03d}"
            if observation.observation_id != expected:
                raise ValueError(
                    f"observation {index} has id {observation.observation_id}; "
                    "ids must be sequential from OB-001"
                )

        files = set(self.grounding.repository_files)
        literature = set(self.grounding.literature_keys)
        checks = set(self.grounding.check_ids)
        capsule = set(self.grounding.capsule_ids)
        for observation in self.observations:
            _reject_unsupplied(
                observation.observation_id,
                "repository file",
                [item.path for item in observation.file_refs],
                files,
            )
            _reject_unsupplied(
                observation.observation_id,
                "retrieved work",
                observation.literature_keys,
                literature,
            )
            _reject_unsupplied(
                observation.observation_id,
                "deterministic check",
                observation.check_ids,
                checks,
            )
            _reject_unsupplied(
                observation.observation_id,
                "scientific object",
                observation.related_capsule_ids,
                capsule,
            )

        known = set(ids)
        for action in self.next_actions:
            unknown = sorted(set(action.addresses_observations) - known)
            if unknown:
                raise ValueError(
                    "a recommended action refers to observations that are not in "
                    "this assessment: " + ", ".join(unknown)
                )
        for uncertainty in self.uncertainties:
            unknown = sorted(set(uncertainty.blocks) - known)
            if unknown:
                raise ValueError(
                    "an uncertainty blocks observations that are not in this "
                    "assessment: " + ", ".join(unknown)
                )
        return self

    @property
    def human_actions(self) -> list[NextAction]:
        return [item for item in self.next_actions if item.requires_human]

    @property
    def cited_files(self) -> list[str]:
        found = {
            reference.path
            for observation in self.observations
            for reference in observation.file_refs
        }
        return sorted(found)


def _reject_unsupplied(
    observation_id: str, label: str, cited: list[str], supplied: set[str]
) -> None:
    """Raise unless every citation in ``cited`` was supplied by the controller."""

    unknown = sorted(set(cited) - supplied)
    if not unknown:
        return
    message = (
        f"{observation_id} cites {label}(s) this run did not supply: "
        + ", ".join(unknown)
        + ". An assessment may rest only on what the controller actually gave it."
    )
    if label == "scientific object":
        message += (
            " This assessment was made in repository-assessment mode, which "
            "supplies no scientific object identifiers at all, so there is "
            "nothing of that kind to cite."
        )
    raise ValueError(message)
