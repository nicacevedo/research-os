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

And every string bound is **declared in the schema the role is given**, by
:func:`_shown`. A ``field_validator`` contributes nothing to
``model_json_schema()``, which is what a prompt template's ``output_schema``
carries, so these limits were enforced and never stated -- and a response
that exceeded one by a few hundred characters was discarded whole.

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
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from research_os.errors import ResearchOSError
from research_os.portfolio.models import (
    Disposition,
    ObjectionTarget,
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

#: The longest a *scalar* command-parameter value may be. One argv token.
MAX_SCALAR_PARAMETER_CHARS = 512

#: The absolute ceiling on a composed document, independent of any
#: declaration. The declaration's own ``max_bytes`` is what normally bounds
#: one, and is checked against canonical bytes rather than against `str()`.
MAX_GENERATED_BYTES = 1024 * 1024

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


def _shown(chars: int, **field: Any) -> Any:
    """Declare a bound where the role that has to respect it can see it.

    The validators below are the enforcement and are unchanged. What this
    adds is that the number reaches the *model*: a prompt template's
    ``output_schema`` is ``model_json_schema()``, a ``field_validator`` puts
    nothing into it, and so every one of these limits was checked against and
    never stated.

    Measured rather than anticipated, twice in ten minutes on one real
    traversal. ``scientific_discovery`` returned a 2,000-character
    ``obstacle`` on one idea and a 201-character ``refined.title`` on
    another; both whole responses were discarded as
    ``MODEL_OUTPUT_INVALID``, and three of those wedge an idea at
    ``BLOCKED_EXTERNAL`` until a person runs ``researchctl portfolio
    resume``. :meth:`ExperimentDesign._explanation` already records the same
    accident in the same words -- "a limit nothing had told it about" -- and
    answered it by clipping three fields on one contract. This is the general
    form of that answer.

    **Schema-only, deliberately.** The validators measure the *stripped*
    value and a JSON Schema ``maxLength`` would not, so declaring these as
    ``max_length`` constraints would refuse strings the validators accept.
    Which outputs are acceptable does not change here; only whether the model
    was told.
    """

    return Field(json_schema_extra={"maxLength": chars}, **field)


def _shown_list(
    *, count: int, items: int | None = None, choices: tuple[str, ...] = (), **field: Any
) -> Any:
    """The same disclosure for a bounded list of strings.

    `_shown` covered scalars and an architecture review pointed out that the
    module docstring then claimed the job was done while every *list* bound
    stayed enforced and unstated -- so a thirteenth `assumptions` entry, or
    a 2,001-character one, still discards the whole response and still tells
    the model nothing. The incident `_shown` records is reachable through
    exactly that door.

    A callable rather than a dict, because a dict `json_schema_extra`
    replaces `items` wholesale and would erase the type Pydantic generated
    for the element.
    """

    def annotate(schema: dict[str, Any]) -> None:
        schema["maxItems"] = count
        element = schema.get("items")
        if not isinstance(element, dict):
            return
        if choices:
            # A closed set says strictly more than a length does.
            element["enum"] = list(choices)
        elif items is not None:
            element["maxLength"] = items

    return Field(json_schema_extra=annotate, **field)


class CandidateIdea(_Contract):
    """One proposed direction, as an explorer or a brancher returns it.

    Note what an explorer does **not** get to say: its adjudication type, its
    quality tier, or whether it is a duplicate. All three are computed
    elsewhere, and all three would be a way for the generator to set its own
    bar if they were fields here.
    """

    title: str = _shown(MAX_TITLE_CHARS)
    research_question: str = _shown(MAX_STATEMENT_CHARS)
    core_idea: str = _shown(MAX_STATEMENT_CHARS)
    mechanism: str = _shown(MAX_STATEMENT_CHARS, default="")
    why_it_matters: str = _shown(MAX_STATEMENT_CHARS, default="")
    falsifier: str = _shown(MAX_STATEMENT_CHARS, default="")
    closest_prior_work: str = _shown(MAX_STATEMENT_CHARS, default="")
    claimed_difference: str = _shown(MAX_STATEMENT_CHARS, default="")
    assumptions: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    alternative_explanations: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    open_uncertainties: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    next_best_action: str = _shown(MAX_STATEMENT_CHARS, default="")
    dimensions: QualityDimensions = QualityDimensions()
    #: The identifiers of the records this direction was derived from --
    #: a rejected idea a failure-mining explorer was shown, a literature
    #: claim a literature explorer was shown. Checked by ordinary code
    #: against what was actually supplied, fail-closed, exactly as a
    #: citation is: an identifier nobody showed the model is not a source.
    derived_from: tuple[str, ...] = _shown_list(items=64, count=3, default=())

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _bounded(value, MAX_TITLE_CHARS, "title")

    @field_validator("derived_from")
    @classmethod
    def _derived(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 3:
            raise ValueError("at most 3 derived_from identifiers")
        return tuple(_bounded(item, 64, "a derived_from identifier") for item in value)

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
    obstacle: str = _shown(MAX_STATEMENT_CHARS, default="")
    #: The smallest thing that would move this forward. Prose; the allocator
    #: does not parse it, a person reads it.
    minimum_decisive_action: str = _shown(MAX_STATEMENT_CHARS, default="")

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
    summary: str = _shown(MAX_SUMMARY_CHARS)
    #: Whether this is wrong with the *idea* or with the way the idea proposes
    #: to settle itself. See :class:`ObjectionTarget`.
    #:
    #: Defaults to ``CLAIM``, which is today's behaviour, so the softer route
    #: is reachable only when a model states plainly that the question
    #: survives its own objection. A reviewer that says nothing is read as
    #: objecting to the idea, which is the reading that kills -- erring
    #: towards the cheap outcome rather than towards keeping work alive.
    target: ObjectionTarget = ObjectionTarget.CLAIM
    #: A new research question this objection raises, when it raises one --
    #: "the argmin sits at the grid edge for every design" raises "is the
    #: cold-start protocol monotone in lambda?". Optional, and never a
    #: rewrite of the idea under review: it becomes a *new* idea, with its
    #: own contract, through a frontier request.
    follow_up_question: str = _shown(
        MAX_STATEMENT_CHARS,
        default="",
        description=(
            "Optional. A NEW research question this objection raises, worth "
            "pursuing as its own idea. Not a revision of the idea under review."
        ),
    )

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "an objection")

    @field_validator("follow_up_question")
    @classmethod
    def _follow_up(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

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
    summary: str = _shown(MAX_SUMMARY_CHARS)
    #: What, specifically, was searched for and not found. Distinguishes "I
    #: looked for a counterexample and there isn't an obvious one" from "I did
    #: not look".
    attempted: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=MAX_LIST_ITEMS, default=()
    )

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

    @field_validator("attempted")
    @classmethod
    def _bounded_attempts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} attempts")
        return tuple(
            _bounded(item, MAX_STATEMENT_CHARS, "an attempt") for item in value
        )

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
    nearest_known_work: str = _shown(MAX_STATEMENT_CHARS, default="")
    rationale: str = _shown(MAX_STATEMENT_CHARS, default="")

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

    proposed_component: str = _shown(MAX_STATEMENT_CHARS)
    closest_known_result: str = _shown(MAX_STATEMENT_CHARS)
    relation: str = Field(pattern="^(same|partial|different)$")
    precise_difference: str = _shown(MAX_STATEMENT_CHARS, default="")
    source_key: str = _shown(MAX_STATEMENT_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("proposed_component", "closest_known_result", "source_key")
    @classmethod
    def _required(cls, value: str) -> str:
        return _bounded(value, MAX_STATEMENT_CHARS, "this field")

    @field_validator("precise_difference")
    @classmethod
    def _bounded_difference(cls, value: str) -> str:
        # Bounded because it is interpolated into an evidence row's summary,
        # which the Curator commits into the researcher's repository. An
        # independent security review found six fields this module's own
        # "every string has a maximum size" did not cover; this is the one
        # that reaches Git.
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped


class NoveltyAuditOutput(_Contract):
    """The deep audit: a structured matrix, and the queries that produced it.

    ``queries`` is not decoration. A novelty claim is a claim about *absence*,
    and an absence found by one query is worth much less than one found by
    six. Recording them is what lets a person check the search rather than the
    conclusion, and what lets the replication rule demand a second terminology
    path that is actually different.
    """

    rows: tuple[NoveltyRow, ...] = ()
    queries: tuple[str, ...] = _shown_list(
        items=MAX_TITLE_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    summary: str = _shown(MAX_SUMMARY_CHARS, default="")

    @field_validator("queries")
    @classmethod
    def _bounded_queries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # The queries travel in `StageOutcome.data` into the LangGraph
        # checkpoint, whose own docstring says state carrying bytes is a table
        # that grows by megabytes.
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} queries")
        return tuple(_bounded(item, MAX_TITLE_CHARS, "a query") for item in value)

    @field_validator("summary")
    @classmethod
    def _bounded_summary(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_SUMMARY_CHARS:
            raise ValueError(f"at most {MAX_SUMMARY_CHARS} characters")
        return stripped

    @field_validator("rows")
    @classmethod
    def _bounded_rows(cls, value: tuple[NoveltyRow, ...]) -> tuple[NoveltyRow, ...]:
        if len(value) > MAX_MATRIX_ROWS:
            raise ValueError(f"at most {MAX_MATRIX_ROWS} rows")
        return value

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row.source_key for row in self.rows))


#: The comparators a frozen decision rule may use.
#:
#: Six, all of them total orders on a float, and deliberately no expression
#: language. A decision rule the model could *write* rather than *fill in*
#: would be model-authored code deciding what a result means, which is the
#: thing this whole layer is arranged to prevent -- and it would be evaluated
#: after the result exists, which is the thing preregistration is for.
COMPARATORS: frozenset[str] = frozenset({"<", "<=", ">", ">=", "==", "!="})

#: How far into a JSON document a metric may be addressed.
MAX_METRIC_DEPTH = 8

#: The resource settings an executor actually reads.
#:
#: Exactly the six `SlurmExecutor.build_script` turns into an `#SBATCH`
#: directive. Anything else a design names is inert -- it reaches no
#: executor and changes nothing about what runs -- so it is dropped rather
#: than carried into the specification digest.
RESOURCE_KEYS: frozenset[str] = frozenset(
    {"partition", "time_limit", "account", "cpus", "memory", "gres"}
)


def _checked_seeds(value: tuple[int, ...]) -> tuple[int, ...]:
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"at most {MAX_LIST_ITEMS} seeds")
    for seed in value:
        if not 0 <= seed <= 2**31 - 1:
            raise ValueError("a seed must fit in a non-negative 32-bit integer")
    return value


def _checked_parameters(value: dict[str, Any]) -> dict[str, Any]:
    """Scalars stay short; a composed document is bounded where it is frozen.

    See :meth:`ExperimentDesign._bounded_parameters` for why the two limits
    differ.
    """

    if len(value) > 32:
        raise ValueError("at most 32 command parameters")
    for name, supplied in value.items():
        if len(str(name)) > 64:
            raise ValueError(f"parameter {name!r} has too long a name")
        limit = (
            MAX_GENERATED_BYTES
            if isinstance(supplied, dict | list)
            else MAX_SCALAR_PARAMETER_CHARS
        )
        if len(str(supplied)) > limit:
            raise ValueError(f"parameter {name!r} is too long to be a value")
    return value


def _checked_resources(value: dict[str, str]) -> dict[str, str]:
    """Keep the settings an executor reads, and drop the rest.

    See :meth:`ExperimentDesign._executor_resources`.
    """

    kept = {
        str(name): str(supplied)[:128]
        for name, supplied in value.items()
        if str(name) in RESOURCE_KEYS
    }
    if len(kept) > len(RESOURCE_KEYS):  # pragma: no cover - unreachable
        raise ValueError("more resource settings than there are resources")
    return kept


class DecisionPredicate(_Contract):
    """One half of a frozen decision rule: a comparison against a threshold."""

    comparator: str = Field(pattern=r"^(<=|>=|==|!=|<|>)$")
    #: Finite, because a model writes it. A NaN threshold makes every
    #: comparison false forever -- a rule that can never be met and never be
    #: refuted, reported as a permanent INCONCLUSIVE with nothing saying
    #: why -- and an infinite one is a rule that is always met or never.
    #: `allow_inf_nan=False` also keeps the token out of the preregistration
    #: artifact, which is written before the row and must be readable by
    #: something other than Python.
    threshold: float = Field(allow_inf_nan=False)

    def holds(self, value: float) -> bool:
        """Apply the comparison. Ordinary Python, and the only thing that is.

        ``==`` and ``!=`` on floats are exact, and that is intended: a rule
        that says "exactly zero errors" means exactly zero, and a tolerance
        invented here would be a threshold nobody preregistered.
        """

        return {
            "<": value < self.threshold,
            "<=": value <= self.threshold,
            ">": value > self.threshold,
            ">=": value >= self.threshold,
            "==": value == self.threshold,
            "!=": value != self.threshold,
        }[self.comparator]

    def rendered(self) -> str:
        return f"{self.comparator} {self.threshold}"


class DecisionRule(_Contract):
    """The prespecified rule that decides what the result means.

    **Two predicates, not one, and that is the design.** A single "success"
    predicate makes every result that is not a success a refutation, which is
    false -- a measurement can miss both. Requiring the failure condition to
    be stated separately makes "neither" expressible, and makes a rule whose
    success condition is everything detectable: both predicates hold, and the
    conclusion is ``INCONCLUSIVE`` rather than ``SUPPORTS``.

    ``metric_path`` addresses a number inside a JSON document the experiment
    declared it would write. Dotted, with integer segments indexing lists.
    There is no expression language and there will not be one: the rule is
    *filled in*, never written, so what it can say is fixed before any idea
    exists.
    """

    output_path: str = _shown(512)
    metric_path: str = _shown(256)
    success: DecisionPredicate
    failure: DecisionPredicate
    #: What the number is, in the researcher's words. Not used by the
    #: comparison; printed beside it, because a threshold with no units is a
    #: number nobody can check.
    metric_description: str = _shown(MAX_STATEMENT_CHARS, default="")

    @field_validator("output_path")
    @classmethod
    def _relative_output(cls, value: str) -> str:
        stripped = _bounded(value, 512, "the decision rule's output path")
        if stripped.startswith(("/", "~")) or "\\" in stripped:
            raise ValueError("the output path must be a relative POSIX path")
        if any(part in {"", ".", ".."} for part in stripped.split("/")):
            raise ValueError("the output path must not contain '.' or '..' segments")
        return stripped

    @field_validator("metric_path")
    @classmethod
    def _addressable_metric(cls, value: str) -> str:
        stripped = _bounded(value, 256, "the decision rule's metric path")
        parts = stripped.split(".")
        if len(parts) > MAX_METRIC_DEPTH:
            raise ValueError(f"a metric path may have at most {MAX_METRIC_DEPTH} parts")
        if any(not part for part in parts):
            raise ValueError("a metric path must not contain an empty segment")
        return stripped

    @field_validator("metric_description")
    @classmethod
    def _bounded_description(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    def rendered(self) -> str:
        return (
            f"{self.metric_path} in {self.output_path}: "
            f"supports when {self.success.rendered()}, "
            f"contradicts when {self.failure.rendered()}"
        )


class ExperimentDesign(_Contract):
    """One experiment over one declared command, preregistered.

    Three things the model may *not* supply, each of which would be it
    choosing its own bar:

    - an argument vector. It names a command the researcher declared in
      ``experiments.yaml`` -- a file outside every worktree -- and fills in
      the parameters that command declares. Nothing else is runnable.
    - a verdict. The design fixes the rule; ordinary Python applies it after
      the result exists, and the model is never asked what the numbers mean.
    - whether the experiment was preregistered. Either a
      :class:`DecisionRule` is here or ``no_decision_rule_reason`` says why
      there is none, and the second costs the idea the top of the scale.
    """

    testable: bool
    untestable_reason: str = _shown(MAX_SUMMARY_CHARS, default="")
    command: str = _shown(64, default="")
    command_parameters: dict[str, Any] = Field(default_factory=dict)
    seeds: tuple[int, ...] = ()
    resources: dict[str, str] = Field(default_factory=dict)
    primary_endpoint: str = _shown(MAX_STATEMENT_CHARS, default="")
    secondary_endpoints: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    dataset_identity: str = _shown(MAX_STATEMENT_CHARS, default="")
    #: Which prediction of the idea this measurement would falsify. Quoted
    #: from the idea's own falsifier by the model, so a design that tests
    #: something else is visible rather than inferred.
    falsification_criterion: str = _shown(MAX_STATEMENT_CHARS, default="")
    decision_rule: DecisionRule | None = None
    no_decision_rule_reason: str = _shown(MAX_SUMMARY_CHARS, default="")
    #: What this replication varies, and how. Empty on a primary design; the
    #: replication template requires it, and ordinary Python separately checks
    #: that the resulting specification really is different.
    variation_kind: str = _shown(MAX_STATEMENT_CHARS, default="")
    variation_detail: str = _shown(MAX_SUMMARY_CHARS, default="")

    @field_validator("command")
    @classmethod
    def _command_name(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 64:
            raise ValueError("a command name is at most 64 characters")
        return stripped

    @field_validator(
        "primary_endpoint",
        "dataset_identity",
        "falsification_criterion",
        "variation_kind",
    )
    @classmethod
    def _statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    @field_validator("untestable_reason", "no_decision_rule_reason", "variation_detail")
    @classmethod
    def _explanation(cls, value: str) -> str:
        """Clipped rather than refused, and only these three fields.

        The dogfood found why. Asked to design an experiment for an idea no
        declared command can test, the designer answered ``testable: false``
        with a careful 2,400-character account of why -- which is the *right*
        answer and the most useful one it could give -- and the contract
        threw the whole response away for being 400 characters over a limit
        nothing had told it about. The retry produced the same answer and
        failed identically, which is the loop ``_parameter_contract`` already
        records paying for once.

        These three fields are explanation and nothing reads them as
        evidence: they reach a work item's error, an action's detail and a
        person. Clipping one loses a paragraph; refusing it loses the
        answer. Every field that *is* content -- the command, its parameters,
        the endpoint, the decision rule -- still refuses, because salvaging
        half of one of those is how an unsupported conclusion comes to look
        supported.
        """

        stripped = value.strip()
        if len(stripped) <= MAX_SUMMARY_CHARS:
            return stripped
        return stripped[: MAX_SUMMARY_CHARS - 14].rstrip() + " [clipped]"

    @field_validator("secondary_endpoints")
    @classmethod
    def _bounded_endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} secondary endpoints")
        return tuple(
            _bounded(item, MAX_STATEMENT_CHARS, "a secondary endpoint")
            for item in value
        )

    @field_validator("seeds")
    @classmethod
    def _bounded_seeds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _checked_seeds(value)

    @field_validator("command_parameters")
    @classmethod
    def _bounded_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Scalars stay short; a composed document is bounded where it is frozen.

        A value for a ``generated`` parameter is a whole JSON document and is
        legitimately larger than an argv token, so the 512-character rule
        cannot apply to it -- that rule exists because a scalar parameter
        becomes one argument. What bounds a composed document is
        ``ParameterSpec.max_bytes``, checked against its *canonical* bytes in
        :func:`research_os.experiment.generated.freeze`, which is the only
        place that knows which declaration it is being measured against.

        The ceiling here is a second, declaration-independent one, so a
        design cannot make a prompt or a database row enormous by composing a
        document for a command that turns out not to declare a generated
        parameter at all -- in which case ``resolve_command`` refuses it, but
        only after this model has already been constructed.
        """

        return _checked_parameters(value)

    @field_validator("resources")
    @classmethod
    def _executor_resources(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep the settings an executor reads, and drop the rest.

        The second thing the dogfood found, and the same shape as the first.
        Asked for the resources its experiment needs, the designer wrote
        ``machine: "a 2026 laptop; the thesis's hardware and Gurobi 12 are
        not ..."`` -- a *note*, in a field for values -- and a 128-character
        bound threw away an otherwise valid design. The retry wrote a note
        again.

        `RESOURCE_KEYS` is what `SlurmExecutor.build_script` actually turns
        into a directive. Everything else is inert: it reaches no executor,
        changes nothing about what runs, and its only effect is to enter the
        specification digest -- so carrying a paragraph of prose there would
        make the frozen identity of an experiment partly a model's commentary
        on it.

        Dropped rather than refused, and clipped rather than truncated
        silently, because neither the note nor its length is a fact about the
        experiment that anything downstream reads.
        """

        return _checked_resources(value)

    def check(self) -> None:
        """Refuse a design that contradicts itself.

        Checked here rather than in the handler because every one of these is
        a property of the answer alone, and a contract that needs a caller to
        finish validating it is a contract two callers will finish
        differently.
        """

        if not self.testable:
            if not self.untestable_reason:
                raise ContractError(
                    "a design that says the idea is not testable must say why; "
                    "'no' with no reason cannot be acted on or disagreed with"
                )
            return
        if not self.command:
            raise ContractError("a testable design must name a declared command")
        if not self.primary_endpoint:
            raise ContractError(
                "a testable design must name its primary endpoint before the "
                "result exists. That is what preregistration is."
            )
        if self.decision_rule is None and not self.no_decision_rule_reason:
            raise ContractError(
                "a design must carry a machine-checkable decision rule, or say "
                "why this question does not admit one. An endpoint with neither "
                "is one that can be read whichever way suits once the numbers "
                "are in."
            )
        if self.decision_rule is not None and self.no_decision_rule_reason:
            raise ContractError(
                "a design carries a decision rule or a reason there is none, never both"
            )


# ---------------------------------------------------- the scientific contract --
#
# An experiment used to be preregistered by the same call that chose its
# design: one model composed the grid *and* fixed the threshold, which is the
# co-design the discovery report's §AB.5 demonstrated arithmetically -- with
# `lambda_ratios: [0.5, 0.5]` the live statistic's denominator is 1.0 by
# construction, so a "preregistered" SUPPORTS is reachable by choosing the
# grid. And the only analysis the route could express was "read one number
# out of one file", so an idea whose falsifier asked for a regression
# coefficient was INSUFFICIENT before anything ran (§AB.3): a missing
# *analysis* was still human-owned.
#
# The contract below splits the two acts and gives the second a language:
#
#   AnalysisSpec          authored FIRST, by its own role, blind to any design:
#                         the estimand, the raw observables, the inclusion
#                         rules, a closed set of reductions, the primary
#                         statistic, its uncertainty, the two-predicate
#                         decision, what the data must exhibit before the
#                         statistic means anything, and what happens when
#                         something is missing.
#   DesignSpecification   authored SECOND, by the experiment designer, against
#                         the frozen analysis -- whose thresholds it is not
#                         shown -- choosing the declared command and the grid.
#
# Both are *filled in*, never written: every operation is a member of a closed
# set that ordinary Python in `research_os.portfolio.analysis` evaluates, so
# no model-authored code ever decides what a result means.

#: A name the analysis gives an observable or a reduction. Lower-case, short,
#: and unambiguous inside a prompt or a JSON document.
ANALYSIS_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,47}$"

#: A field of a record, addressed by dotted path inside the record: at most
#: :data:`MAX_FIELD_CHARS` characters and :data:`MAX_FIELD_DEPTH` segments.
FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z0-9_\-]+)*$"
MAX_FIELD_CHARS = 128
MAX_FIELD_DEPTH = 6

#: Everything a frozen analysis may compute. A closed set, and deliberately
#: no expression language: see the section comment above.
ANALYSIS_OPERATIONS: tuple[str, ...] = (
    "value",
    "count",
    "fraction",
    "mean",
    "median",
    "std",
    "min",
    "max",
    "sum",
    "quantile",
    "difference",
    "ratio",
    "correlation",
    "ols_coefficient",
)

#: Operations over one field of a records observable.
FIELD_OPERATIONS: frozenset[str] = frozenset(
    {"mean", "median", "std", "min", "max", "sum", "quantile"}
)

MAX_OBSERVABLES = 8
MAX_REDUCTIONS = 24
MAX_CONDITIONS = 8
MAX_SUPPORT_RULES = 8
MAX_OLS_TERMS = 6
MAX_DESIGN_VARIABLES = 12
MAX_LEVELS = 64


def _relative_path(value: str, what: str) -> str:
    stripped = _bounded(value, 512, what)
    if stripped.startswith(("/", "~")) or "\\" in stripped:
        raise ValueError(f"{what} must be a relative POSIX path")
    if any(part in {"", ".", ".."} for part in stripped.split("/")):
        raise ValueError(f"{what} must not contain '.' or '..' segments")
    return stripped


def _field_name(value: str, what: str) -> str:
    stripped = value.strip()
    if len(stripped) > MAX_FIELD_CHARS or len(stripped.split(".")) > MAX_FIELD_DEPTH:
        raise ValueError(
            f"{what} is at most {MAX_FIELD_CHARS} characters and "
            f"{MAX_FIELD_DEPTH} dotted segments"
        )
    if not re.fullmatch(FIELD_PATTERN, stripped):
        raise ValueError(
            f"{what} {value!r} is not a field name: letters, digits, '_' and '-', "
            f"optionally dotted into a nested record"
        )
    return stripped


class Condition(_Contract):
    """One inclusion rule: a comparison a record must satisfy to be analysed.

    Numeric comparisons on numbers, and ``==`` / ``!=`` on strings too, so a
    categorical field can select a regime ("solver == 'cold'"). Nothing else:
    no pattern, no arithmetic, no reference to another field.
    """

    field: str = _shown(128)
    comparator: str = Field(pattern=r"^(<=|>=|==|!=|<|>)$")
    value: float | int | str | bool

    @field_validator("field")
    @classmethod
    def _field(cls, value: str) -> str:
        return _field_name(value, "a condition's field")

    @field_validator("value")
    @classmethod
    def _value(cls, value: float | str | bool) -> float | int | str | bool:
        if isinstance(value, str) and len(value) > 128:
            raise ValueError("a condition's value is at most 128 characters")
        if isinstance(value, float) and math.isnan(value):
            raise ValueError("a condition's value must be a number, not NaN")
        return value

    def rendered(self) -> str:
        return f"{self.field} {self.comparator} {self.value!r}"


class Observable(_Contract):
    """One raw output the analysis reads, and how.

    ``scalar`` addresses one number by dotted path, which is what the route
    could always express. ``records`` addresses a list of records -- a JSON
    array of objects at ``path``, or every row of a CSV file -- and is what
    lets a reduction, a regression or an uncertainty exist at all.
    """

    name: str = Field(pattern=ANALYSIS_NAME_PATTERN)
    #: The file the run must write. Relative to the experiment's workspace.
    source: str = _shown(512)
    kind: Literal["scalar", "records"]
    #: Dotted path inside the document. For ``records``, empty means the
    #: document itself is the list (and is the only value a CSV accepts).
    path: str = _shown(256, default="")
    #: Fields every analysed record must carry.
    fields: tuple[str, ...] = _shown_list(
        items=128, count=MAX_CONDITIONS * 2, default=()
    )
    #: The inclusion rules. A record failing any is excluded, and counted.
    include: tuple[Condition, ...] = ()
    #: What happens to a record missing a required field or holding a value
    #: that is not a finite number where one is required. Fixed before the
    #: run, like everything else here, so "drop the awkward rows" is a
    #: decision made in advance or not at all.
    incomplete_records: Literal["insufficient", "exclude"] = "insufficient"
    description: str = _shown(MAX_STATEMENT_CHARS, default="")

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        return _relative_path(value, "an observable's source")

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 256:
            raise ValueError("an observable's path is at most 256 characters")
        if stripped and any(not part for part in stripped.split(".")):
            raise ValueError("an observable's path must not contain an empty segment")
        if len(stripped.split(".")) > MAX_METRIC_DEPTH:
            raise ValueError(
                f"an observable's path has at most {MAX_METRIC_DEPTH} parts"
            )
        return stripped

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_CONDITIONS * 2:
            raise ValueError(f"at most {MAX_CONDITIONS * 2} fields")
        return tuple(_field_name(item, "an observable's field") for item in value)

    @field_validator("include")
    @classmethod
    def _include(cls, value: tuple[Condition, ...]) -> tuple[Condition, ...]:
        if len(value) > MAX_CONDITIONS:
            raise ValueError(f"at most {MAX_CONDITIONS} inclusion rules")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped


class Reduction(_Contract):
    """One named quantity, computed by a member of a closed set of operations.

    Reductions are evaluated in the order given and may only refer to
    reductions named *earlier*, so the analysis is a straight line and cannot
    express a loop. Which arguments each operation takes is checked by
    :meth:`AnalysisSpec.check`, because it is a relationship between fields.
    """

    name: str = Field(pattern=ANALYSIS_NAME_PATTERN)
    op: Literal[
        "value",
        "count",
        "fraction",
        "mean",
        "median",
        "std",
        "min",
        "max",
        "sum",
        "quantile",
        "difference",
        "ratio",
        "correlation",
        "ols_coefficient",
    ]
    #: The observable this reads, for every operation but difference/ratio.
    observable: str = Field(default="", pattern=r"^([a-z][a-z0-9_]{0,47})?$")
    #: The field a field operation aggregates; the first of a correlation.
    field: str = _shown(128, default="")
    #: The second field of a correlation.
    other_field: str = _shown(128, default="")
    #: Extra selection for this reduction only (``fraction`` counts them).
    where: tuple[Condition, ...] = ()
    #: For ``quantile``: which one, strictly between 0 and 1.
    q: float | None = Field(default=None, gt=0.0, lt=1.0)
    #: For ``difference`` / ``ratio``: exactly two earlier reductions, in order.
    of: tuple[str, ...] = _shown_list(items=48, count=2, default=())
    #: For ``ols_coefficient``: the response field, the predictor terms (a
    #: field, or two fields joined by ':' for their product) and which term's
    #: coefficient is the quantity. An intercept is always included.
    response: str = _shown(128, default="")
    terms: tuple[str, ...] = _shown_list(items=260, count=MAX_OLS_TERMS, default=())
    coefficient: str = _shown(260, default="")

    @field_validator("field", "other_field", "response")
    @classmethod
    def _optional_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            return ""
        return _field_name(stripped, "a reduction's field")

    @field_validator("terms")
    @classmethod
    def _terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_OLS_TERMS:
            raise ValueError(f"at most {MAX_OLS_TERMS} regression terms")
        checked: list[str] = []
        for term in value:
            parts = term.strip().split(":")
            if not 1 <= len(parts) <= 2:
                raise ValueError(
                    f"a term is a field or two fields joined by ':': {term!r}"
                )
            checked.append(
                ":".join(_field_name(part, "a term's field") for part in parts)
            )
        return tuple(checked)

    @field_validator("coefficient")
    @classmethod
    def _coefficient(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 260:
            raise ValueError("a coefficient name is at most 260 characters")
        return stripped

    @field_validator("where")
    @classmethod
    def _where(cls, value: tuple[Condition, ...]) -> tuple[Condition, ...]:
        if len(value) > MAX_CONDITIONS:
            raise ValueError(f"at most {MAX_CONDITIONS} conditions")
        return value

    @field_validator("of")
    @classmethod
    def _of(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 2:
            raise ValueError("difference and ratio take exactly two reductions")
        for item in value:
            if not re.fullmatch(ANALYSIS_NAME_PATTERN, item):
                raise ValueError(f"{item!r} is not a reduction name")
        return value

    def fields_read(self) -> tuple[str, ...]:
        """Every record field this reduction reads, for the required set."""

        found = [self.field, self.other_field, self.response]
        for term in self.terms:
            found.extend(term.split(":"))
        found.extend(item.field for item in self.where)
        return tuple(dict.fromkeys(item for item in found if item))


class Uncertainty(_Contract):
    """How the primary statistic's uncertainty is computed, fixed in advance.

    One method: a percentile bootstrap over the analysed records, with the
    seed written down, so the interval is a deterministic function of the
    data and the contract rather than of when it was computed.
    """

    method: Literal["bootstrap_percentile"] = "bootstrap_percentile"
    level: float = Field(default=0.95, ge=0.5, le=0.999)
    resamples: int = Field(default=1000, ge=100, le=5000)
    seed: int = Field(default=20260915, ge=0, le=2**31 - 1)


class SupportRequirement(_Contract):
    """What the data must exhibit before the statistic may mean anything.

    Declared by the analysis designer, who has not seen the design, and
    checked by ordinary code against the records the run actually wrote. This
    is the defence the co-design finding asked for and could not have at the
    time: a design that collapses the variable a statistic depends on -- two
    identical lambda levels, one instance size -- produces data that fails
    its own contract, and the conclusion is INSUFFICIENT rather than a number
    that means what the grid made it mean.
    """

    observable: str = Field(pattern=ANALYSIS_NAME_PATTERN)
    min_records: int = Field(default=1, ge=1, le=1_000_000)
    #: Field -> the least number of distinct values the analysed records must
    #: hold for it.
    min_distinct: dict[str, int] = Field(default_factory=dict)

    @field_validator("min_distinct")
    @classmethod
    def _distinct(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > MAX_CONDITIONS:
            raise ValueError(f"at most {MAX_CONDITIONS} distinctness requirements")
        checked: dict[str, int] = {}
        for name, count in value.items():
            if not 1 <= int(count) <= 100_000:
                raise ValueError("a distinctness requirement is between 1 and 100000")
            checked[_field_name(name, "a distinctness field")] = int(count)
        return checked


class AnalysisSpec(_Contract):
    """The analysis half of a scientific contract. See the section comment.

    ``analysable: false`` is a complete answer, exactly as ``testable: false``
    is for a design: "the observables this project can produce do not
    identify the quantity this idea is about" is recorded, frozen, and caps
    what any measurement of the idea may conclude at INSUFFICIENT.
    """

    analysable: bool
    unanalysable_reason: str = _shown(MAX_SUMMARY_CHARS, default="")
    #: The quantity the idea is about, in words.
    estimand: str = _shown(MAX_STATEMENT_CHARS, default="")
    #: What a SUPPORTS conclusion would mean about the idea, in words.
    target_claim: str = _shown(MAX_STATEMENT_CHARS, default="")
    observables: tuple[Observable, ...] = ()
    reductions: tuple[Reduction, ...] = ()
    primary_statistic: str = Field(default="", pattern=r"^([a-z][a-z0-9_]{0,47})?$")
    uncertainty: Uncertainty | None = None
    success: DecisionPredicate | None = None
    failure: DecisionPredicate | None = None
    support: tuple[SupportRequirement, ...] = ()
    #: Fixed, and stated so the frozen record says it: one execution of the
    #: preregistered specification, read once. No interim looks, no "run it
    #: again until it clears".
    stopping_rule: Literal["fixed_single_execution"] = "fixed_single_execution"
    #: Fixed, and stated for the same reason: a required observable, field or
    #: reduction that is absent, non-numeric or undefined makes the
    #: conclusion INSUFFICIENT. There is no other value, and in particular no
    #: "estimate it".
    on_missing: Literal["INSUFFICIENT"] = "INSUFFICIENT"

    @field_validator("unanalysable_reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) <= MAX_SUMMARY_CHARS:
            return stripped
        return stripped[: MAX_SUMMARY_CHARS - 14].rstrip() + " [clipped]"

    @field_validator("estimand", "target_claim")
    @classmethod
    def _statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    @field_validator("observables")
    @classmethod
    def _bounded_observables(
        cls, value: tuple[Observable, ...]
    ) -> tuple[Observable, ...]:
        if len(value) > MAX_OBSERVABLES:
            raise ValueError(f"at most {MAX_OBSERVABLES} observables")
        return value

    @field_validator("reductions")
    @classmethod
    def _bounded_reductions(cls, value: tuple[Reduction, ...]) -> tuple[Reduction, ...]:
        if len(value) > MAX_REDUCTIONS:
            raise ValueError(f"at most {MAX_REDUCTIONS} reductions")
        return value

    @field_validator("support")
    @classmethod
    def _bounded_support(
        cls, value: tuple[SupportRequirement, ...]
    ) -> tuple[SupportRequirement, ...]:
        if len(value) > MAX_SUPPORT_RULES:
            raise ValueError(f"at most {MAX_SUPPORT_RULES} support requirements")
        return value

    # -- relationships the field validators cannot see ----------------------
    def check(self) -> None:
        """Refuse an analysis that is incoherent before anything is frozen."""

        if not self.analysable:
            if not self.unanalysable_reason:
                raise ContractError(
                    "an analysis that says the idea cannot be analysed must say "
                    "why; the reason is what a person reads to add the missing "
                    "observable"
                )
            if self.observables or self.reductions or self.success or self.failure:
                raise ContractError(
                    "an unanalysable answer carries a reason and nothing else; a "
                    "half-specified analysis is how a rule gets finished after "
                    "the numbers are in"
                )
            return
        if self.unanalysable_reason:
            raise ContractError(
                "an analysis is either specified or declared impossible, never both"
            )
        if not self.estimand:
            raise ContractError("an analysis must name its estimand before any result")
        if not self.observables:
            raise ContractError("an analysis must read at least one raw observable")
        if not self.reductions:
            raise ContractError("an analysis must compute at least one quantity")
        if self.success is None or self.failure is None:
            raise ContractError(
                "an analysis must fix both predicates -- the one under which the "
                "idea's prediction held and the one under which it failed -- "
                "before any result exists"
            )

        observables = {item.name: item for item in self.observables}
        names = list(observables) + [item.name for item in self.reductions]
        if len(set(names)) != len(names):
            raise ContractError("observable and reduction names must all be distinct")

        defined: dict[str, Reduction] = {}
        for item in self.reductions:
            self._check_reduction(item, observables=observables, defined=defined)
            defined[item.name] = item

        if self.primary_statistic not in defined:
            raise ContractError(
                f"the primary statistic {self.primary_statistic!r} is not a "
                f"reduction this analysis computes"
            )
        if self.uncertainty is not None:
            exact = [
                predicate.comparator
                for predicate in (self.success, self.failure)
                if predicate.comparator in {"==", "!="}
            ]
            if exact:
                raise ContractError(
                    "an interval cannot be compared for exact equality; with an "
                    "uncertainty the predicates must be <, <=, > or >="
                )
            if not self._depends_on_records(
                self.primary_statistic, defined, observables
            ):
                raise ContractError(
                    "a bootstrap resamples records, and the primary statistic "
                    "reads none; drop the uncertainty or compute it from records"
                )
        for rule in self.support:
            observable = observables.get(rule.observable)
            if observable is None:
                raise ContractError(
                    f"a support requirement names {rule.observable!r}, which is "
                    f"not an observable of this analysis"
                )
            if observable.kind != "records" and (
                rule.min_distinct or rule.min_records > 1
            ):
                raise ContractError(
                    f"{rule.observable!r} is a scalar; only a records observable "
                    f"can be required to hold records or distinct values"
                )

    @staticmethod
    def _check_reduction(
        item: Reduction,
        *,
        observables: Mapping[str, Observable],
        defined: Mapping[str, Reduction],
    ) -> None:
        op = item.op
        if op in {"difference", "ratio"}:
            if len(item.of) != 2:
                raise ContractError(f"{item.name}: {op} takes exactly two reductions")
            missing = [name for name in item.of if name not in defined]
            if missing:
                raise ContractError(
                    f"{item.name}: {op} refers to {missing}, which are not "
                    f"reductions computed earlier in the list"
                )
            return
        if item.of:
            raise ContractError(f"{item.name}: only difference and ratio take 'of'")
        observable = observables.get(item.observable)
        if observable is None:
            raise ContractError(
                f"{item.name}: {op} must read an observable of this analysis, and "
                f"{item.observable!r} is not one"
            )
        if op == "value":
            if observable.kind != "scalar":
                raise ContractError(f"{item.name}: value reads a scalar observable")
            return
        if observable.kind != "records":
            raise ContractError(f"{item.name}: {op} reads a records observable")
        if op in FIELD_OPERATIONS and not item.field:
            raise ContractError(f"{item.name}: {op} needs the field it aggregates")
        if op == "quantile" and item.q is None:
            raise ContractError(f"{item.name}: a quantile needs q")
        if op != "quantile" and item.q is not None:
            raise ContractError(f"{item.name}: only a quantile takes q")
        if op == "fraction" and not item.where:
            raise ContractError(
                f"{item.name}: a fraction is the share of records satisfying its "
                f"conditions, so it needs at least one"
            )
        if op == "correlation" and not (item.field and item.other_field):
            raise ContractError(f"{item.name}: a correlation needs two fields")
        if op == "ols_coefficient":
            if not item.response or not item.terms:
                raise ContractError(
                    f"{item.name}: a regression needs a response field and terms"
                )
            if item.coefficient not in item.terms:
                raise ContractError(
                    f"{item.name}: the coefficient {item.coefficient!r} must be one "
                    f"of the terms {list(item.terms)}"
                )
            if len(set(item.terms)) != len(item.terms):
                raise ContractError(f"{item.name}: a term is listed twice")

    @staticmethod
    def _depends_on_records(
        name: str,
        defined: Mapping[str, Reduction],
        observables: Mapping[str, Observable],
    ) -> bool:
        item = defined[name]
        if item.op in {"difference", "ratio"}:
            return any(
                AnalysisSpec._depends_on_records(other, defined, observables)
                for other in item.of
            )
        observable = observables.get(item.observable)
        return observable is not None and observable.kind == "records"

    def sources(self) -> tuple[str, ...]:
        """Every raw output this analysis reads, in a stable order."""

        return tuple(sorted({item.source for item in self.observables}))

    def rendered_decision(self) -> str:
        if not self.analysable or self.success is None or self.failure is None:
            return "no analysis: " + (self.unanalysable_reason or "(no reason)")
        interval = (
            f" over the {self.uncertainty.level:g} bootstrap interval"
            if self.uncertainty
            else ""
        )
        return (
            f"{self.primary_statistic}{interval}: supports when "
            f"{self.success.rendered()}, contradicts when {self.failure.rendered()}"
        )


class DesignVariable(_Contract):
    """One variable of an experimental design, and its levels."""

    name: str = _shown(64)
    role: Literal["manipulated", "controlled", "measured", "blocking"]
    levels: tuple[float | int | str | bool, ...] = ()
    description: str = _shown(MAX_STATEMENT_CHARS, default="")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _bounded(value, 64, "a design variable's name")

    @field_validator("levels")
    @classmethod
    def _levels(
        cls, value: tuple[float | int | str | bool, ...]
    ) -> tuple[float | int | str | bool, ...]:
        if len(value) > MAX_LEVELS:
            raise ValueError(f"at most {MAX_LEVELS} levels")
        for item in value:
            if isinstance(item, str) and len(item) > 128:
                raise ValueError("a level is at most 128 characters")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped


class CapabilityRequest(_Contract):
    """What a command would have to do for this idea to be testable here.

    Designed autonomously and executable by nobody: declaring a command is the
    researcher's act, in ``experiments.yaml``, outside every worktree. What a
    request buys is that the refusal is *scientifically useful* -- it names
    the capability precisely, in terms of the frozen analysis's observables,
    and it survives on the contract so the idea resumes from here the moment
    a person declares it.
    """

    name: str = _shown(64)
    purpose: str = _shown(MAX_STATEMENT_CHARS)
    inputs: str = _shown(MAX_STATEMENT_CHARS, default="")
    outputs: str = _shown(MAX_STATEMENT_CHARS)
    why_declared_commands_do_not_suffice: str = _shown(MAX_SUMMARY_CHARS, default="")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _bounded(value, 64, "a requested capability's name")

    @field_validator("purpose", "outputs")
    @classmethod
    def _required(cls, value: str) -> str:
        return _bounded(value, MAX_STATEMENT_CHARS, "this field")

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    @field_validator("why_declared_commands_do_not_suffice")
    @classmethod
    def _why(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) <= MAX_SUMMARY_CHARS:
            return stripped
        return stripped[: MAX_SUMMARY_CHARS - 14].rstrip() + " [clipped]"


class DesignSpecification(_Contract):
    """The design half of a scientific contract, authored against a frozen analysis.

    What is *absent* is the point: there is no decision rule, no threshold
    and no primary statistic here. The analysis already fixed them, before
    this design existed, and the designer is not shown the thresholds -- so a
    grid cannot be chosen to land on the right side of a number it does not
    know. A design that tries to carry its own rule is refused by the
    contract (``extra="forbid"``), not by a sentence in a prompt.
    """

    testable: bool
    untestable_reason: str = _shown(MAX_SUMMARY_CHARS, default="")
    #: When no declared command can produce the analysis's observables: the
    #: command that would. Optional, and valuable.
    required_capability: CapabilityRequest | None = None
    command: str = _shown(64, default="")
    command_parameters: dict[str, Any] = Field(default_factory=dict)
    seeds: tuple[int, ...] = ()
    resources: dict[str, str] = Field(default_factory=dict)
    #: The experimental variables and their levels -- the grid, in words the
    #: frozen record keeps beside the parameters that realise it.
    variables: tuple[DesignVariable, ...] = ()
    #: How the grid or sample is drawn, and how the parameters realise it.
    sampling: str = _shown(MAX_STATEMENT_CHARS, default="")
    dataset_identity: str = _shown(MAX_STATEMENT_CHARS, default="")
    #: Which clause of the idea's falsifier this measurement tests.
    falsification_criterion: str = _shown(MAX_STATEMENT_CHARS, default="")
    #: Replication only: what this second measurement varies, and how.
    variation_kind: str = _shown(MAX_STATEMENT_CHARS, default="")
    variation_detail: str = _shown(MAX_SUMMARY_CHARS, default="")

    @field_validator("command")
    @classmethod
    def _command_name(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 64:
            raise ValueError("a command name is at most 64 characters")
        return stripped

    @field_validator(
        "sampling", "dataset_identity", "falsification_criterion", "variation_kind"
    )
    @classmethod
    def _statement(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    @field_validator("untestable_reason", "variation_detail")
    @classmethod
    def _explanation(cls, value: str) -> str:
        # Clipped rather than refused, for the reason
        # `ExperimentDesign._explanation` records: an explanation nothing
        # reads as evidence should not cost the answer it explains.
        stripped = value.strip()
        if len(stripped) <= MAX_SUMMARY_CHARS:
            return stripped
        return stripped[: MAX_SUMMARY_CHARS - 14].rstrip() + " [clipped]"

    @field_validator("variables")
    @classmethod
    def _bounded_variables(
        cls, value: tuple[DesignVariable, ...]
    ) -> tuple[DesignVariable, ...]:
        if len(value) > MAX_DESIGN_VARIABLES:
            raise ValueError(f"at most {MAX_DESIGN_VARIABLES} design variables")
        return value

    # The same bounds `ExperimentDesign` applies, for the same reasons, and
    # through the same functions so the two shapes cannot drift apart.
    @field_validator("seeds")
    @classmethod
    def _bounded_seeds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _checked_seeds(value)

    @field_validator("command_parameters")
    @classmethod
    def _bounded_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _checked_parameters(value)

    @field_validator("resources")
    @classmethod
    def _executor_resources(cls, value: dict[str, str]) -> dict[str, str]:
        return _checked_resources(value)

    def check(self) -> None:
        if not self.testable:
            if not self.untestable_reason:
                raise ContractError(
                    "a design that says the idea is not testable must say why"
                )
            return
        if not self.command:
            raise ContractError("a testable design must name a declared command")
        if not self.falsification_criterion:
            raise ContractError(
                "a testable design must quote the clause of the idea's falsifier "
                "it tests, so a design that tests something else is visible"
            )


class ReviewOutput(_Contract):
    """What each of the three independent reviewers returns.

    ``verdict`` and ``objections`` must agree: ``PASS`` with an objection is a
    contradiction, and it is the shape a reviewer that wants to be agreeable
    produces. The schema refuses it here and the database refuses it again.
    """

    verdict: ReviewVerdict
    summary: str = _shown(MAX_SUMMARY_CHARS)
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
    summary: str = _shown(MAX_SUMMARY_CHARS)
    unresolved_disagreements: tuple[str, ...] = _shown_list(
        items=MAX_SUMMARY_CHARS, count=MAX_LIST_ITEMS, default=()
    )
    #: New questions the reviews raise, each pursued as its own idea when the
    #: recommendation is BRANCH or DEEPEN -- or recorded on any other.
    follow_up_questions: tuple[str, ...] = _shown_list(
        items=MAX_STATEMENT_CHARS, count=3, default=()
    )

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "the meta-review summary")

    @field_validator("follow_up_questions")
    @classmethod
    def _follow_ups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 3:
            raise ValueError("at most 3 follow-up questions")
        return tuple(
            _bounded(item, MAX_STATEMENT_CHARS, "a question") for item in value
        )

    @field_validator("unresolved_disagreements")
    @classmethod
    def _bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} disagreements")
        # Each becomes an objection summary row, so the per-item bound matters
        # as much as the list length.
        return tuple(
            _bounded(item, MAX_SUMMARY_CHARS, "a disagreement") for item in value
        )


class DuplicateAdjudication(_Contract):
    """Layer 4 of deduplication. An opinion, recorded as an edge.

    ``of_idea_id`` must be one of the neighbours that were supplied. A model
    naming an idea nobody showed it has not adjudicated anything, and the
    caller checks it against the list -- the same fail-closed shape the
    literature analyst uses for citations.
    """

    verdict: str = Field(pattern="^(duplicate|merge|distinct)$")
    of_idea_id: str = ""
    rationale: str = _shown(MAX_STATEMENT_CHARS, default="")

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
    relations: tuple[str, ...] = _shown_list(
        count=MAX_CANDIDATES,
        choices=("DERIVED_FROM", "GENERALIZES", "SPECIALIZES"),
        default=(),
    )

    @field_validator("children")
    @classmethod
    def _bounded_children(
        cls, value: tuple[CandidateIdea, ...]
    ) -> tuple[CandidateIdea, ...]:
        # A schema bound as well as `check(maximum=...)`, so the ceiling is a
        # property of the type rather than something a caller must remember.
        if len(value) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} children")
        return value

    @field_validator("relations")
    @classmethod
    def _lineage_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} relations")
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


class LiteratureStatement(_Contract):
    """One statement about the published record, and the works it rests on."""

    statement: str = _shown(MAX_STATEMENT_CHARS)
    work_keys: tuple[str, ...] = _shown_list(items=128, count=6)
    #: A verbatim quotation from one of those works' title or abstract, when
    #: the statement rests on specific words. Checked against the stored
    #: text before anything is kept; a quotation not found there invalidates
    #: the whole reading.
    excerpt: str = _shown(600, default="")

    @field_validator("statement")
    @classmethod
    def _statement(cls, value: str) -> str:
        return _bounded(value, MAX_STATEMENT_CHARS, "a statement")

    @field_validator("work_keys")
    @classmethod
    def _keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError(
                "a statement about the literature must cite the retrieved works "
                "it rests on"
            )
        if len(value) > 6:
            raise ValueError("at most 6 works per statement")
        return tuple(_bounded(item, 128, "a work key") for item in value)

    @field_validator("excerpt")
    @classmethod
    def _excerpt(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 600:
            raise ValueError("an excerpt is at most 600 characters")
        return stripped


class LiteratureClaimOut(LiteratureStatement):
    kind: Literal["FINDING", "METHOD", "DATASET", "LIMITATION", "OPEN_QUESTION"]
    #: What the statement does to the idea that asked, if one did.
    relation_to_idea: Literal["SUPPORTS", "CONTRADICTS", "CONSISTENT_WITH", "NONE"] = (
        "NONE"
    )


class LiteratureAnswer(_Contract):
    """What the literature reader returns for one targeted question."""

    answer: str = _shown(MAX_SUMMARY_CHARS)
    claims: tuple[LiteratureClaimOut, ...] = ()
    disagreements: tuple[LiteratureStatement, ...] = ()
    gaps: tuple[LiteratureStatement, ...] = ()

    @field_validator("answer")
    @classmethod
    def _answer(cls, value: str) -> str:
        return _bounded(value, MAX_SUMMARY_CHARS, "the answer")

    @field_validator("claims")
    @classmethod
    def _bounded_claims(
        cls, value: tuple[LiteratureClaimOut, ...]
    ) -> tuple[LiteratureClaimOut, ...]:
        if len(value) > MAX_MATRIX_ROWS:
            raise ValueError(f"at most {MAX_MATRIX_ROWS} claims")
        return value

    @field_validator("disagreements", "gaps")
    @classmethod
    def _bounded_statements(
        cls, value: tuple[LiteratureStatement, ...]
    ) -> tuple[LiteratureStatement, ...]:
        if len(value) > MAX_OBJECTIONS:
            raise ValueError(f"at most {MAX_OBJECTIONS}")
        return value


class FollowUpOutput(_Contract):
    """What the follow-up explorer returns for one frontier request.

    Children and how each relates to the idea whose event raised them. There
    is no field for the parent -- no revised statement, no new falsifier, no
    changed threshold -- because a follow-up is a new question and the
    parent's frozen record is not this role's to touch.
    """

    children: tuple[CandidateIdea, ...] = ()
    relations: tuple[str, ...] = _shown_list(
        count=MAX_CANDIDATES,
        choices=("DERIVED_FROM", "GENERALIZES", "SPECIALIZES"),
        default=(),
    )
    nothing_to_propose: str = _shown(MAX_STATEMENT_CHARS, default="")

    @field_validator("children")
    @classmethod
    def _bounded_children(
        cls, value: tuple[CandidateIdea, ...]
    ) -> tuple[CandidateIdea, ...]:
        if len(value) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} children")
        return value

    @field_validator("relations")
    @classmethod
    def _lineage_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} relations")
        bad = [
            item
            for item in value
            if item not in {"DERIVED_FROM", "GENERALIZES", "SPECIALIZES"}
        ]
        if bad:
            raise ValueError(f"not lineage relations: {bad}")
        return value

    @field_validator("nothing_to_propose")
    @classmethod
    def _nothing(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > MAX_STATEMENT_CHARS:
            raise ValueError(f"at most {MAX_STATEMENT_CHARS} characters")
        return stripped

    def check(self, *, maximum: int) -> None:
        if len(self.children) > maximum:
            raise ContractError(
                f"the bound is {maximum} children and {len(self.children)} were "
                f"proposed"
            )
        if len(self.relations) != len(self.children):
            raise ContractError("every child must name how it relates to its parent")
        if not self.children and not self.nothing_to_propose:
            raise ContractError(
                "proposing nothing is a legitimate answer and must say why"
            )


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
    "analysis_designer": AnalysisSpec,
    "experiment_designer": DesignSpecification,
    "replication_designer": DesignSpecification,
    "falsifier": FalsifierOutput,
    "methodology_reviewer": ReviewOutput,
    "novelty_reviewer": ReviewOutput,
    "skeptic_reviewer": ReviewOutput,
    "replicator": ReviewOutput,
    "meta_reviewer": MetaReviewOutput,
    "brancher": BranchOutput,
    "follow_up_explorer": FollowUpOutput,
    "literature_reader": LiteratureAnswer,
    "literature_explorer": ExplorerOutput,
}
