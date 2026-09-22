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
from collections.abc import Mapping
from typing import Any

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
    assumptions: tuple[str, ...] = ()
    alternative_explanations: tuple[str, ...] = ()
    open_uncertainties: tuple[str, ...] = ()
    next_best_action: str = _shown(MAX_STATEMENT_CHARS, default="")
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
    summary: str = _shown(MAX_SUMMARY_CHARS)
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
    queries: tuple[str, ...] = ()
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
    secondary_endpoints: tuple[str, ...] = ()
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
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} seeds")
        for seed in value:
            if not 0 <= seed <= 2**31 - 1:
                raise ValueError("a seed must fit in a non-negative 32-bit integer")
        return value

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

        kept = {
            str(name): str(supplied)[:128]
            for name, supplied in value.items()
            if str(name) in RESOURCE_KEYS
        }
        if len(kept) > len(RESOURCE_KEYS):  # pragma: no cover - unreachable
            raise ValueError("more resource settings than there are resources")
        return kept

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
    relations: tuple[str, ...] = ()

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
    "experiment_designer": ExperimentDesign,
    "replication_designer": ExperimentDesign,
    "falsifier": FalsifierOutput,
    "methodology_reviewer": ReviewOutput,
    "novelty_reviewer": ReviewOutput,
    "skeptic_reviewer": ReviewOutput,
    "replicator": ReviewOutput,
    "meta_reviewer": MetaReviewOutput,
    "brancher": BranchOutput,
}
