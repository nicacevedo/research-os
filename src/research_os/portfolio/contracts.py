"""What each role must return, and what happens when it does not.

Every model call this layer makes has a typed output contract. A response that
does not satisfy one is a :class:`ContractError`, which the track maps to
``FailureClass.MODEL_OUTPUT_INVALID`` and the queue retries under the existing
policy. **Nothing unvalidated becomes idea content**, and that is the whole
point of the module: the failure mode this system is most exposed to is
persuasive prose arriving where structured evidence was expected, and a schema
is the cheapest place to stop it.

Three conventions hold throughout, each of which is a defect that would
otherwise be discovered later:

**Bounded.** Every list has a maximum length and every string a maximum size.
A model that returns two hundred candidate ideas has not been more helpful; it
has produced something no reviewer will read and a prompt that will become the
largest thing in the next request.

**Closed.** ``extra="forbid"`` everywhere, so a field the contract does not
know about is an error rather than silently dropped. A model inventing
``confidence_override`` should fail loudly.

**No self-assessment that a gate reads.** A role may report the *dimensions* it
assessed -- novelty, plausibility, tractability -- because those feed the
allocator, which is operational. No role returns its own quality tier, its own
independence, or its own adjudication type. Those are computed from rows and
from the falsifier, by :mod:`research_os.portfolio.gates` and
:mod:`research_os.runtime.adjudication`, precisely so that the thing being
judged does not supply the judgement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from research_os.errors import ResearchOSError
from research_os.portfolio.models import (
    Disposition,
    QualityDimensions,
    ReviewVerdict,
    Severity,
)

#: The most candidate ideas one explorer call may return.
#:
#: Six, because the portfolio's default breadth is eight active tracks and an
#: explorer that can fill the whole portfolio in one call makes the allocator's
#: diversity constraint decorative.
MAX_CANDIDATES = 6
#: The most objections one review may raise. Beyond this the reviewer has
#: written an essay rather than a review, and the gate counts objections.
MAX_OBJECTIONS = 8
#: Rows in a novelty matrix. A matrix that needs more than this is a survey.
MAX_MATRIX_ROWS = 24
#: Items in any of the short lists on an idea.
MAX_LIST_ITEMS = 12

MAX_TITLE_CHARS = 200
MAX_STATEMENT_CHARS = 2_000
MAX_SUMMARY_CHARS = 4_000


class ContractError(ResearchOSError):
    """Raised when a model's output does not satisfy its role's contract."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _bounded(value: str, limit: int, what: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{what} must not be empty")
    if len(stripped) > limit:
        raise ValueError(f"{what} must be at most {limit} characters")
    return stripped


class CandidateIdea(_Contract):
    """One proposed direction, as an explorer or a brancher returns it.

    Note what an explorer does **not** get to say: its adjudication type, its
    quality tier, or whether it is a duplicate. All three are computed
    elsewhere, and all three would be a way for the generator to set its own
    bar if they were fields here.
    """

    title: str
    research_question: str
    core_idea: str
    mechanism: str = ""
    why_it_matters: str = ""
    falsifier: str = ""
    closest_prior_work: str = ""
    claimed_difference: str = ""
    assumptions: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()
    open_uncertainties: tuple[str, ...] = ()
    next_best_action: str = ""
    dimensions: QualityDimensions = QualityDimensions()

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _bounded(value, MAX_TITLE_CHARS, "title")

    @field_validator("research_question", "core_idea")
    @classmethod
    def _required_statement(cls, value: str) -> str:
        return _bounded(value, MAX_STATEMENT_CHARS, "this field")

    @field_validator(
        "mechanism",
        "why_it_matters",
        "falsifier",
        "closest_prior_work",
        "claimed_difference",
        "next_best_action",
    )
    @classmethod
    def _optional_statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"this field must be at most {MAX_STATEMENT_CHARS} chars")
        return stripped

    @field_validator("assumptions", "alternative_explanations", "open_uncertainties")
    @classmethod
    def _bounded_list(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} items")
        return tuple(_bounded(item, MAX_STATEMENT_CHARS, "list item") for item in value)

    def as_fields(self) -> dict[str, Any]:
        """The shape :meth:`PortfolioStore.create_idea` takes."""

        return {
            "title": self.title,
            "research_question": self.research_question,
            "core_idea": self.core_idea,
            "mechanism": self.mechanism,
            "why_it_matters": self.why_it_matters,
            "falsifier": self.falsifier,
            "closest_prior_work": self.closest_prior_work,
            "claimed_difference": self.claimed_difference,
            "assumptions": list(self.assumptions),
            "alternative_explanations": list(self.alternative_explanations),
            "open_uncertainties": list(self.open_uncertainties),
            "next_best_action": self.next_best_action,
        }


class ExplorerOutput(_Contract):
    """What any of the three explorers returns."""

    candidates: tuple[CandidateIdea, ...] = ()
    #: Why the explorer produced nothing, when it produced nothing. An empty
    #: result is a legitimate answer -- a failure-mining explorer looking at a
    #: portfolio with no failures has nothing to mine -- and is different from
    #: a call that went wrong.
    nothing_to_propose: str = ""

    @field_validator("candidates")
    @classmethod
    def _bounded_candidates(
        cls, value: tuple[CandidateIdea, ...]
    ) -> tuple[CandidateIdea, ...]:
        if len(value) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} candidates per call")
        return value


class DiscoveryOutput(_Contract):
    """What ``scientific_discovery`` returns.

    ``can_be_made_precise=False`` is a *useful* answer and ends the track. A
    role whose only successful outcome is producing something would produce
    something for every input, which is how a portfolio fills with ideas that
    were never going to be answerable.
    """

    can_be_made_precise: bool
    #: Required when it can. The sharpened idea replaces the candidate's text.
    refined: CandidateIdea | None = None
    #: Required when it cannot.
    obstacle: str = ""
    #: The smallest thing that would move this forward. Prose; the allocator
    #: does not parse it, a person reads it.
    minimum_decisive_action: str = ""

    @field_validator("obstacle", "minimum_decisive_action")
    @classmethod
    def _statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    def check(self) -> None:
        if self.can_be_made_precise and self.refined is None:
            raise ContractError(
                "scientific_discovery said the idea can be made precise and did "
                "not supply the precise version"
            )
        if not self.can_be_made_precise and not self.obstacle:
            raise ContractError(
                "scientific_discovery said the idea cannot be made precise and "
                "did not say what stops it"
            )


class Objection(_Contract):
    """One thing wrong with an idea, at a severity the gate reads."""

    severity: Severity
    summary: str

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "an objection")

    @field_validator("severity")
    @classmethod
    def _real_severity(cls, value: Severity) -> Severity:
        if value is Severity.NONE:
            raise ValueError("an objection with no severity is not an objection")
        return value


class FalsifierOutput(_Contract):
    """What the adversarial falsifier returns.

    Its job is to kill the idea, so an empty ``objections`` list with
    ``pass_to_investigation`` true is the *unusual* outcome and is recorded as
    such. The brief's four dispositions map onto the severity of the worst
    objection rather than being a separate field, so a FATAL disposition
    without a fatal objection is unrepresentable.
    """

    objections: tuple[Objection, ...] = ()
    summary: str
    #: What, specifically, was searched for and not found. Distinguishes "I
    #: looked for a counterexample and there isn't an obvious one" from "I did
    #: not look".
    attempted: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "the falsifier's summary")

    @field_validator("objections")
    @classmethod
    def _bounded_objections(cls, value: tuple[Objection, ...]) -> tuple[Objection, ...]:
        if len(value) > MAX_OBJECTIONS:
            raise ValueError(f"at most {MAX_OBJECTIONS} objections")
        return value

    @property
    def worst(self) -> Severity:
        from research_os.portfolio.models import SEVERITY_ORDER

        if not self.objections:
            return Severity.NONE
        return max(
            (item.severity for item in self.objections),
            key=lambda item: SEVERITY_ORDER[item],
        )


class ScreenOutput(_Contract):
    """The cheap novelty screen. Kills obvious duplicates; establishes nothing.

    ``likely_known`` is a *screen*, and the name says so. It is allowed to be
    wrong in both directions and it can never satisfy a gate: the deep audit
    is what ``VALIDATED`` requires, and that one must cite retrieved sources.
    """

    likely_known: bool
    nearest_known_work: str = ""
    rationale: str = ""

    @field_validator("nearest_known_work", "rationale")
    @classmethod
    def _statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped


class NoveltyRow(_Contract):
    """One row of the novelty matrix.

    ``source_key`` is required and is checked against the keys that were
    actually supplied in the literature packet, by the existing fail-closed
    citation check. A row citing a work nobody retrieved invalidates the whole
    report rather than being dropped, because the statement it supported was
    reached some other way.
    """

    proposed_component: str
    closest_known_result: str
    relation: str = Field(pattern="^(same|partial|different)$")
    precise_difference: str = ""
    source_key: str
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("proposed_component", "closest_known_result", "source_key")
    @classmethod
    def _required(cls, value: str) -> str:
        return _bounded(value, MAX_STATEMENT_CHARS, "this field")


class NoveltyAuditOutput(_Contract):
    """The deep audit: a structured matrix, and the queries that produced it.

    ``queries`` is not decoration. A novelty claim is a claim about *absence*,
    and an absence found by one query is worth much less than one found by
    six. Recording them is what lets a person check the search rather than the
    conclusion, and what lets the replication rule demand a second terminology
    path that is actually different.
    """

    rows: tuple[NoveltyRow, ...] = ()
    queries: tuple[str, ...] = ()
    summary: str = ""

    @field_validator("rows")
    @classmethod
    def _bounded_rows(cls, value: tuple[NoveltyRow, ...]) -> tuple[NoveltyRow, ...]:
        if len(value) > MAX_MATRIX_ROWS:
            raise ValueError(f"at most {MAX_MATRIX_ROWS} rows")
        return value

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row.source_key for row in self.rows))


class ReviewOutput(_Contract):
    """What each of the three independent reviewers returns.

    ``verdict`` and ``objections`` must agree: ``PASS`` with an objection is a
    contradiction, and it is the shape a reviewer that wants to be agreeable
    produces. The schema refuses it here and the database refuses it again.
    """

    verdict: ReviewVerdict
    summary: str
    objections: tuple[Objection, ...] = ()
    dimensions: QualityDimensions = QualityDimensions()

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "the review summary")

    @field_validator("objections")
    @classmethod
    def _bounded_objections(cls, value: tuple[Objection, ...]) -> tuple[Objection, ...]:
        if len(value) > MAX_OBJECTIONS:
            raise ValueError(f"at most {MAX_OBJECTIONS} objections")
        return value

    def check(self) -> None:
        if self.verdict is ReviewVerdict.PASS and self.objections:
            raise ContractError(
                "a review that passes with an objection standing is a "
                "contradiction; use PASS_WITH_OBJECTIONS"
            )
        if self.verdict is ReviewVerdict.REJECT and not self.objections:
            raise ContractError("a rejection must say what is wrong")

    @property
    def severity(self) -> Severity:
        from research_os.portfolio.models import SEVERITY_ORDER

        if not self.objections:
            return Severity.NONE
        return max(
            (item.severity for item in self.objections),
            key=lambda item: SEVERITY_ORDER[item],
        )


class MetaReviewOutput(_Contract):
    """The synthesis. A recommendation, which the gate may lower.

    ``unresolved_disagreements`` is required to be honest rather than empty: a
    meta-reviewer whose job is to synthesise has an incentive to produce
    consensus, and the brief is explicit that it must preserve disagreement.
    Recording it as a field means a synthesis that erased one is visible.
    """

    recommendation: Disposition
    summary: str
    unresolved_disagreements: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "the meta-review summary")

    @field_validator("unresolved_disagreements")
    @classmethod
    def _bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} disagreements")
        return value


class DuplicateAdjudication(_Contract):
    """Layer 4 of deduplication. An opinion, recorded as an edge.

    ``of_idea_id`` must be one of the neighbours that were supplied. A model
    naming an idea nobody showed it has not adjudicated anything, and the
    caller checks it against the list -- the same fail-closed shape the
    literature analyst uses for citations.
    """

    verdict: str = Field(pattern="^(duplicate|merge|distinct)$")
    of_idea_id: str = ""
    rationale: str = ""

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    def check(self, supplied: tuple[str, ...]) -> None:
        if self.verdict == "distinct":
            return
        if not self.of_idea_id:
            raise ContractError(f"a {self.verdict!r} verdict must name an idea")
        if self.of_idea_id not in supplied:
            raise ContractError(
                f"the adjudicator named {self.of_idea_id}, which was not among the "
                f"ideas it was shown. A verdict about something nobody supplied is "
                f"not a verdict."
            )


class BranchOutput(_Contract):
    """Child directions a strong idea suggests.

    Each child names how it relates to its parent, and the relation must be a
    *lineage* kind -- a branch that produced a CONTRADICTS edge would be
    claiming its parent is wrong, which is a review's job and not a brancher's.
    """

    children: tuple[CandidateIdea, ...] = ()
    relations: tuple[str, ...] = ()

    @field_validator("relations")
    @classmethod
    def _lineage_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"DERIVED_FROM", "GENERALIZES", "SPECIALIZES"}
        bad = [item for item in value if item not in allowed]
        if bad:
            raise ValueError(f"not lineage relations a branch may create: {bad}")
        return value

    def check(self, *, maximum: int) -> None:
        if len(self.children) > maximum:
            raise ContractError(
                f"the branching bound is {maximum} children and "
                f"{len(self.children)} were proposed"
            )
        if len(self.relations) != len(self.children):
            raise ContractError("every child must name how it relates to its parent")


def parse[ContractModel: BaseModel](
    model: type[ContractModel],
    *,
    structured: Mapping[str, Any] | None,
    text: str | None,
    role: str,
) -> ContractModel:
    """Validate one model response against its role's contract.

    Fail-closed, like ``research_os.literature.analyst.parse_literature_report``
    and for the same reason: a response that is partly valid was reached by
    reasoning this system cannot inspect, and salvaging the valid half leaves
    an unsupported conclusion looking supported.
    """

    payload: Any = structured
    if payload is None:
        payload = _extract_json_object(text or "")
    if payload is None:
        raise ContractError(
            f"{role} returned no JSON object; its contract requires one"
        )
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ContractError(
            f"{role} output does not satisfy its contract: {exc}"
        ) from None


def _extract_json_object(text: str) -> Any | None:
    """The first balanced JSON object in ``text``, or ``None``.

    Providers sometimes wrap a JSON body in prose or a fence even when asked
    not to. Scanning for the first balanced object is what the v1 layers
    already do; this is deliberately not a repair -- a body that does not parse
    is refused, not patched.
    """

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


#: Every contract, by the role that returns it. Read by the prompt registry so
#: a template and its output model cannot drift apart, and by the test that
#: asserts every portfolio role has both.
CONTRACTS: dict[str, type[BaseModel]] = {
    "blind_explorer": ExplorerOutput,
    "seeded_explorer": ExplorerOutput,
    "failure_mining_explorer": ExplorerOutput,
    "scientific_discovery": DiscoveryOutput,
    "duplicate_adjudicator": DuplicateAdjudication,
    "novelty_screen": ScreenOutput,
    "literature_scout": NoveltyAuditOutput,
    "falsifier": FalsifierOutput,
    "methodology_reviewer": ReviewOutput,
    "novelty_reviewer": ReviewOutput,
    "skeptic_reviewer": ReviewOutput,
    "replicator": ReviewOutput,
    "meta_reviewer": MetaReviewOutput,
    "brancher": BranchOutput,
}
