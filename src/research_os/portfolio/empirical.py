"""The bridge an empirical idea crosses to become measured rather than argued.

Every adjudicated idea both real projects have produced is ``empirical``. The
falsifiers ask for grids to be run, bootstraps to be resampled and wall clock
to be recorded -- and the evidence stage refused all of them, because nothing
connected an idea version to the experiment machinery that already existed.
This module is that connection and deliberately nothing more. It owns no
executor, no artifact store, no queue and no budget: it *uses*
:mod:`research_os.experiment` for what the researcher declared,
:mod:`research_os.runtime.executors` for running it,
:mod:`research_os.runtime.idempotency` for running it once, and
:mod:`research_os.runtime.budgets` for paying for it.

The separation it enforces, which is the one the whole layer rests on::

    design      the model chooses a DECLARED command and fixes a rule
    execute     ordinary code runs it, contained, in a disposable worktree
    analyse     ordinary code applies the frozen rule to the collected bytes
    record      the conclusion becomes one evidence row, version-bound

**The model is never asked what the result means.** It is asked, before any
result exists, to name one number and two thresholds on it. Afterwards
:func:`analyse` reads that number out of the file the run wrote and compares
it. There is no step at which a model reads an output and reports a verdict,
which is the step this architecture exists to not have.

**An execution that did not happen is never evidence.** A provider that did
not answer, an executor that crashed, a host that cannot contain -- each ends
the stage as an operational failure with a class the queue understands, leaves
the experiment row recoverable, and writes no evidence at all.
:class:`~research_os.portfolio.models.EmpiricalConclusion` has
``OPERATIONALLY_BLOCKED`` for exactly this, and
:data:`~research_os.portfolio.models.EVIDENCE_STRENGTH_FOR_CONCLUSION` has no
entry for it, so a row cannot be written from one by accident.

**One live experiment per idea version per role.** Enforced by a partial
unique index rather than by this module remembering, because "replay creates
no duplicate" is a property a replay must not be able to argue with. Partial
because a design made by a prompt this build has superseded is retired
rather than deleted, and a record must not occupy the name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.portfolio.contracts import (
    ContractError,
    DecisionRule,
    ExperimentDesign,
    parse,
)
from research_os.portfolio.models import (
    EVIDENCE_STRENGTH_FOR_CONCLUSION,
    AdjudicationType,
    EmpiricalConclusion,
    EvidenceKind,
    ExperimentRole,
    ExperimentState,
    IdeaExperiment,
    IdeaVersion,
)
from research_os.portfolio.prompts import TEMPLATES as PORTFOLIO_TEMPLATES
from research_os.portfolio.store import DuplicateExperimentError
from research_os.runtime.budgets import BudgetExhaustedError, Dimension
from research_os.runtime.executors import (
    LOCAL,
    ContainmentUnavailableError,
    ExecutorError,
    prepare_run_dir,
    spec_digest,
)
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import IdempotencyError, idempotency_key
from research_os.runtime.ids import new_external_job_id
from research_os.runtime.interfaces import ExecutionSpec
from research_os.runtime.models import ExternalJob, ExternalJobStatus
from research_os.runtime.routing import ProviderCallFailedError

LOG = logging.getLogger("research_os.portfolio.empirical")

#: The default seed, when a design names none.
#:
#: Fixed rather than drawn. An experiment whose seed is chosen at submission
#: is an experiment nobody can rerun, and this is the same constant
#: ``runtime.actions.experiments`` uses for the same reason.
DEFAULT_SEED = 20260915

#: The largest declared output this bridge will read a metric out of.
#:
#: A decision rule addresses a number in a JSON document. A gigabyte of
#: results is a legitimate experimental output and is not a document to parse
#: in a work item; it is collected, hashed and referenced like any other
#: artifact, and the rule over it reports that it could not be evaluated.
MAX_METRIC_DOCUMENT_BYTES = 8 * 1024 * 1024

#: The most declared outputs one experiment's collection will store.
MAX_COLLECTED_OUTPUTS = 32

#: The largest single output file copied into the artifact store.
MAX_COLLECTED_BYTES = 256 * 1024 * 1024


class EmpiricalError(ResearchOSError):
    """Raised when the empirical bridge cannot proceed and must say why."""

    def __init__(self, message: str, *, failure_class: FailureClass) -> None:
        super().__init__(message)
        self.failure_class = failure_class


# ------------------------------------------------------------- identity --
def variation_digest(spec: ExecutionSpec) -> str:
    """The scientific identity of an execution, with *where it ran* removed.

    ``spec_digest`` covers ``cwd``, and it must: two runs in different trees
    are different executions and the preregistration check compares hashes.
    But the workspace here is a disposable worktree named for the experiment,
    so ``spec_digest`` differs between a primary and its replication whatever
    else is true -- which would make "the replication varies something" a test
    that passes on the directory name.

    So the *variation* is hashed separately: the argument vector, the seeds,
    the resources and the expected outputs. Two experiments with an equal
    variation digest are the same measurement run twice, and
    :func:`assert_varies` refuses that as a replication.
    """

    payload = {
        "argv": list(spec.argv),
        "env": dict(sorted(spec.env.items())),
        "resources": dict(sorted(spec.resources.items())),
        "outputs": sorted(spec.outputs),
        "seeds": list(spec.seeds),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def workspace_for(experiment_id: str) -> Path:
    """Where this experiment's disposable worktree goes.

    A pure function of the experiment id, and that is the property that
    matters: ``cwd`` is inside ``spec_digest``, so a path derived from the
    clock or from an attempt counter would give one experiment a different
    digest on every retry and the preregistration comparison would reject
    every legitimate resubmission. Deriving it from the row's own id makes the
    digest stable for the life of the experiment.
    """

    from research_os.automation.worktree import worktree_path

    return worktree_path(_worktree_run_id(experiment_id), "T-001")


def _worktree_run_id(experiment_id: str) -> str:
    """A run id shaped the way the shared worktree helpers require.

    They are shared with the automation control plane, which validates the
    shape. A portfolio experiment is not an automation run, so this derives a
    stable id from it rather than pretending one exists -- the same thing
    :func:`research_os.experiment.controller._worktree_run_id` does, for the
    same reason.
    """

    stamp = experiment_id.split("-")[1] if "-" in experiment_id else ""
    if len(stamp) != 16 or not stamp.endswith("Z"):  # pragma: no cover - defensive
        raise EmpiricalError(
            f"{experiment_id!r} is not a portfolio experiment id",
            failure_class=FailureClass.CODE_EXCEPTION,
        )
    digest = hashlib.sha256(experiment_id.encode("utf-8")).hexdigest()[:8]
    return f"RUN-{stamp}-{digest}"


# --------------------------------------------------------------- design --
@dataclass(frozen=True, slots=True)
class DesignedExperiment:
    """A validated design, its frozen specification, and both digests."""

    design: ExperimentDesign
    spec: ExecutionSpec
    experiment_id: str
    spec_digest: str
    variation_digest: str
    workspace: Path
    call_id: str | None


def declared_commands(project_id: str) -> dict[str, Any]:
    """The experiment commands the *researcher* declared for this project.

    From ``experiments.yaml``, which lives under the config home -- outside
    every worktree, so a write-enabled worker cannot reach it. This is the
    boundary that makes "no model writes a command" true by construction, and
    it is the same function the objective cycle's experimentalist uses.
    """

    from research_os.experiment.config import load_config as load_experiment_config

    try:
        config = load_experiment_config()
    except ResearchOSError as exc:
        LOG.debug("no experiment configuration: %s", exc)
        return {}
    project = config.projects.get(project_id)
    return dict(project.commands) if project is not None else {}


#: Extensions a ``path`` parameter plausibly names as an *input*.
#:
#: Data and configuration, and deliberately not source: a declared command's
#: code is already in the checkout and no parameter selects it.
INPUT_SUFFIXES: frozenset[str] = frozenset(
    {".json", ".yaml", ".yml", ".csv", ".tsv", ".toml", ".npz", ".parquet"}
)

#: How many candidate input files the catalogue lists.
MAX_INPUT_CANDIDATES = 60


def input_candidates(
    repository: Path | None, *, limit: int = MAX_INPUT_CANDIDATES
) -> tuple[str, ...]:
    """Tracked data and configuration files a ``path`` parameter could name.

    **Measured, not anticipated.** The first real design this route produced
    chose the right command -- ``benchmark``, whose plan file "decides every
    instance, penalty, solver, tolerance, repetition and timeout" -- and then
    named ``plans/2026/modern_conic_vs_decomposition.yaml``, which does not
    exist. The run went the whole way: contained, in a disposable worktree,
    fifty-two packages installed offline, and then ``FileNotFoundError`` and
    an operational failure with no evidence written. The refusal on a
    *different* idea a few minutes earlier said the same thing in words: "No
    plan encoding this sweep is known to exist in the tree."

    It was reasoning about a repository it had never been shown. This is the
    third time this codebase has paid for a constraint the model is graded on
    and never shown -- see ``_parameter_contract`` for the first.

    Tracked files only, so nothing a previous experiment left behind can be
    named as an input; the capsule excluded, because ``.research/`` is
    scientific state and not experimental data; bounded, because a listing
    longer than this is a prompt rather than an answer.
    """

    if repository is None:
        return ()
    from research_os.automation.gitutil import git

    try:
        output = git(["ls-files", "-z"], cwd=repository, check=False).stdout
    except ResearchOSError as exc:  # pragma: no cover - a repository with no git
        LOG.debug("could not list tracked files: %s", exc)
        return ()
    found = [
        name
        for name in output.split("\0")
        if name
        and not name.startswith(".research/")
        and Path(name).suffix.lower() in INPUT_SUFFIXES
    ]
    return tuple(sorted(found)[:limit])


#: How many numeric paths one declared output's schema listing shows.
MAX_SCHEMA_PATHS = 40

#: The largest committed result this will read a schema out of.
MAX_SCHEMA_BYTES = 4 * 1024 * 1024


def numeric_paths(document: Any, *, prefix: str = "") -> list[str]:
    """Every dotted path in a parsed JSON document that addresses a number.

    A list is described by its first element, because a decision rule indexes
    one by integer and every element of a results array has the same shape.

    A boolean is not a number: ``True`` is ``1`` in Python, and a rule
    comparing a flag against a threshold would mean something nobody wrote
    down. :func:`metric_from` refuses one for the same reason, and the two
    have to agree or the catalogue would advertise a path the analyser will
    not read.
    """

    if isinstance(document, Mapping):
        found: list[str] = []
        for key in sorted(str(item) for item in document):
            found.extend(numeric_paths(document[key], prefix=f"{prefix}{key}."))
        return found
    if isinstance(document, Sequence) and not isinstance(document, str | bytes):
        return numeric_paths(document[0], prefix=f"{prefix}0.") if document else []
    leaf = prefix.rstrip(".")
    if leaf and not isinstance(document, bool) and isinstance(document, int | float):
        return [leaf]
    return []


def output_schema_lines(
    commands: Mapping[str, Any], *, repository: Path | None
) -> list[str]:
    """What a declared output looks like, read from one the project committed.

    **The third instance of one mistake, and the designer named it itself.**
    Refusing an idea on 2026-09-22 it wrote: "its output schema is not
    declared (declared outputs: none), so any metric_path I named inside the
    file I create would be a guess rather than a preregistration." That is
    the correct standard and it made every command unusable, because nothing
    told it what any of them writes.

    Nothing here invents that. A command's *declared* output path comes from
    ``experiments.yaml``, which the researcher owns; this reads the file at
    that path when the project has committed one from an earlier run, which
    is the same file the command writes.

    **Keys and types only, never values.** Showing the numbers a previous run
    produced would let a design choose a threshold the last result already
    satisfies, which is preregistration theatre -- the rule is supposed to be
    fixed before the result exists, and a rule fitted to a result that does
    exist is fixed after. So the listing is paths, and a designer that wants
    to know what value to expect has to reason about the science.
    """

    if repository is None:
        return []
    from research_os.automation.gitutil import git

    try:
        tracked = set(
            git(["ls-files", "-z"], cwd=repository, check=False).stdout.split("\0")
        )
    except ResearchOSError:  # pragma: no cover - a repository with no git
        return []

    lines: list[str] = []
    for name, spec in sorted(commands.items()):
        for relative in spec.outputs:
            if relative not in tracked:
                continue
            target = repository / relative
            try:
                if not target.is_file() or target.stat().st_size > MAX_SCHEMA_BYTES:
                    continue
                document = json.loads(
                    target.read_text(encoding="utf-8"), parse_constant=_not_a_number
                )
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            paths = numeric_paths(document)[:MAX_SCHEMA_PATHS]
            if not paths:
                continue
            lines.append(
                f"{name} writes {relative}; a committed run of it has these "
                f"numeric paths (names and shapes only -- the values are "
                f"deliberately not shown, because a threshold chosen to fit "
                f"a result that already exists is not a preregistration):"
            )
            lines.extend(f"    {item}" for item in paths)
    return lines


def command_catalogue(
    commands: Mapping[str, Any], *, repository: Path | None = None
) -> list[str]:
    """The declared commands, with every constraint the validator enforces.

    A constraint the model is graded on and never shown is a loop: the
    objective cycle's experimentalist proposed an absolute path for a ``path``
    parameter twice in a row because nothing in its prompt had changed between
    the two refusals. So the bounds go in the prompt.
    """

    lines: list[str] = []
    for name, spec in sorted(commands.items()):
        lines.append(f"{name}: {spec.description or '(no description)'}")
        if not spec.parameters:
            lines.append("    (no parameters)")
        for parameter in spec.parameters:
            parts = [f"type={parameter.type}"]
            parts.append("required" if parameter.required else "optional")
            if parameter.default is not None:
                parts.append(f"default={parameter.default!r}")
            if parameter.minimum is not None:
                parts.append(f"minimum={parameter.minimum}")
            if parameter.maximum is not None:
                parts.append(f"maximum={parameter.maximum}")
            if parameter.choices:
                parts.append(
                    "choices=" + "|".join(str(one) for one in parameter.choices)
                )
            if str(parameter.type) == "path":
                parts.append(
                    "MUST be a relative in-tree path: no leading '/' or '~', "
                    "POSIX '/' separators, no '.' or '..' segments"
                )
            detail = f" -- {parameter.description}" if parameter.description else ""
            lines.append(f"    {parameter.name}: {', '.join(parts)}{detail}")
        if spec.outputs:
            lines.append("    declared outputs: " + ", ".join(spec.outputs))
        else:
            lines.append(
                "    declared outputs: none. This command's results go wherever "
                "its path parameters say, so a decision rule must name one of "
                "those paths."
            )
        lines.append(f"    wall-clock ceiling: {spec.timeout_seconds}s")
    if any(
        str(parameter.type) == "path"
        for spec in commands.values()
        for parameter in spec.parameters
    ):
        candidates = input_candidates(repository)
        lines.append("")
        lines.append(
            "tracked data and configuration files in this checkout, which is "
            "the complete set a path parameter may name as an INPUT:"
        )
        lines.extend(f"    {name}" for name in candidates)
        if not candidates:
            lines.append("    (none)")
        lines.append(
            "A path that is not in that list is an OUTPUT this run will "
            "create. Naming a file that does not exist as an input is not "
            "refused here and fails when the command runs, having spent the "
            "whole execution."
        )
    schema = output_schema_lines(commands, repository=repository)
    if schema:
        lines.append("")
        lines.extend(schema)
    return lines


def build_spec(
    design: ExperimentDesign,
    *,
    commands: Mapping[str, Any],
    workspace: Path,
    max_seconds: int,
) -> tuple[ExecutionSpec, DecisionRule | None]:
    """Turn a validated design into a frozen specification, or refuse it.

    Everything executable is checked by ordinary code against what the project
    declared, and a value that does not fit is refused rather than repaired:

    - the command must be one of ``commands``;
    - every parameter value passes :func:`research_os.experiment.spec.
      resolve_command`, which checks it against the type and the bounds the
      researcher wrote;
    - the decision rule's ``output_path`` must be a file this command actually
      writes -- a declared output, or the value supplied for one of its
      ``path`` parameters. A rule over a file the command does not write is a
      rule that will never evaluate, and discovering that after the run is
      discovering it too late.

    The wall clock is the *tighter* of the researcher's declaration and the
    portfolio's own ceiling. Configuration can make an experiment shorter and
    never longer, which is the same direction every other bound in this layer
    moves.
    """

    from research_os.experiment.spec import resolve_command

    chosen = design.command
    if chosen not in commands:
        raise EmpiricalError(
            f"{chosen!r} is not a command this project declares. Declared: "
            f"{', '.join(sorted(commands)) or '(none)'}",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    spec = commands[chosen]
    try:
        resolved = resolve_command(
            spec, dict(design.command_parameters), worktree=workspace
        )
    except ResearchOSError as exc:
        raise EmpiricalError(
            f"the design's parameters do not fit {chosen}: {exc}",
            failure_class=FailureClass.POLICY_REFUSED,
        ) from None

    written = _paths_this_command_writes(spec, resolved)
    rule = design.decision_rule
    outputs = list(resolved.outputs)
    if rule is not None:
        if rule.output_path not in written:
            raise EmpiricalError(
                f"the decision rule reads {rule.output_path!r}, which {chosen} "
                f"does not write. It writes: {', '.join(sorted(written)) or '(nothing declared)'}. "
                f"A rule over a file that will not exist is a rule that can "
                f"never be evaluated.",
                failure_class=FailureClass.POLICY_REFUSED,
            )
        if rule.output_path not in outputs:
            # Declared here so the executor collects it and the packet can
            # cite it. A command whose outputs are decided by a path parameter
            # -- which both of this machine's real declarations are -- has an
            # empty `outputs` list, and without this the one file the rule
            # depends on would be the one file nobody collected.
            outputs.append(rule.output_path)

    seeds = tuple(design.seeds) or (DEFAULT_SEED,)
    resources = {
        str(key): str(value)
        for key, value in dict(design.resources).items()
        if key != "timeout_seconds"
    }
    frozen = ExecutionSpec(
        name=f"idea-experiment-{chosen}",
        argv=tuple(str(token) for token in resolved.argv),
        cwd=str(workspace),
        environment={"kind": "uv", "workspace": "disposable-worktree"},
        resources=resources,
        env={
            f"RESEARCH_OS_SEED_{index}": str(seed) for index, seed in enumerate(seeds)
        },
        timeout_seconds=min(int(resolved.timeout_seconds), int(max_seconds)),
        outputs=tuple(sorted(outputs)),
        seeds=seeds,
    )
    return frozen, rule


def _paths_this_command_writes(spec: Any, resolved: Any) -> set[str]:
    """Every in-tree path this resolved command could legitimately produce.

    Its declared outputs, plus the values supplied for its ``path``
    parameters. The second half is what makes a command like ``--out {out}``
    usable at all: its ``outputs`` list is empty because the file it writes is
    named by the caller, and the resolver has already checked that the value
    stays inside the workspace.
    """

    written = set(resolved.outputs)
    for parameter in spec.parameters:
        if str(parameter.type) != "path":
            continue
        supplied = resolved.parameters.get(parameter.name)
        if supplied:
            written.add(str(supplied))
    return written


def assert_varies(*, replication: str, primary: str) -> None:
    """Refuse a replication that is the primary run a second time.

    Invariant 13 of the empirical brief, and the reason it is code rather than
    a sentence in a prompt: a replication that varies nothing is the cheapest
    thing a model can produce and the easiest to believe. Comparing
    *variation* digests rather than spec digests is what makes the check
    meaningful -- see :func:`variation_digest`.
    """

    if replication == primary:
        raise EmpiricalError(
            "the replication's specification is identical to the original's in "
            "everything but where it runs. Rerunning the same command with the "
            "same seeds is a reproducibility check; it is not an independent "
            "replication and must not be recorded as one.",
            failure_class=FailureClass.POLICY_REFUSED,
        )


# -------------------------------------------------------------- analysis --
@dataclass(frozen=True, slots=True)
class Analysis:
    """What ordinary code concluded from what the run left on disk."""

    conclusion: EmpiricalConclusion
    #: One sentence, assembled from facts. Never a model's sentence.
    summary: str
    #: The metric the rule addressed, when it could be read.
    observed: float | None = None
    #: Every collected output, by relative path and content hash.
    outputs: tuple[tuple[str, str, int], ...] = ()
    #: Why the rule could not be applied, when it could not.
    notes: tuple[str, ...] = ()

    def record(
        self,
        *,
        experiment: IdeaExperiment,
        spec: ExecutionSpec,
        job_id: str,
        exit_code: int | None,
        contained: str,
    ) -> dict[str, Any]:
        """The analysis document, as the bytes that get hashed and stored.

        Deliberately contains the arithmetic and not a verdict sentence: the
        rule, the number it read, the comparison, and the outputs by content
        hash. A reader who disagrees has everything needed to disagree.
        """

        return {
            "schema": "portfolio-empirical-analysis-v1",
            "experiment_id": experiment.experiment_id,
            "idea_id": experiment.idea_id,
            "idea_version": experiment.idea_version,
            "role": str(experiment.role),
            "job_id": job_id,
            "spec_digest": experiment.spec_digest,
            "variation_digest": experiment.variation_digest,
            "argv": list(spec.argv),
            "seeds": list(spec.seeds),
            "workspace": experiment.workspace_path,
            "containment": contained,
            "exit_code": exit_code,
            "decision_rule": experiment.decision_rule,
            "no_rule_reason": experiment.no_rule_reason,
            "observed": self.observed,
            "conclusion": str(self.conclusion),
            "outputs": [
                {"path": path, "sha256": digest, "bytes": size}
                for path, digest, size in self.outputs
            ],
            "notes": list(self.notes),
        }


class NotANumberError(ValueError):
    """A document used ``NaN``, ``Infinity`` or ``-Infinity``.

    Python's `json` accepts all three by default and no other JSON reader
    does. Refusing them where the document is parsed is what keeps them out
    of the arithmetic and out of the stored record at the same time.
    """


def _not_a_number(literal: str) -> float:
    raise NotANumberError(literal)


def metric_from(document: Any, path: str) -> float | None:
    """Read one number out of a parsed JSON document by dotted path.

    Integer segments index a list, everything else is a mapping key, and a
    boolean is **not** a number: ``True`` is ``1`` in Python and a decision
    rule that compared a flag against a threshold would silently mean
    something nobody wrote down.
    """

    current: Any = document
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return None
            current = current[segment]
            continue
        if isinstance(current, Sequence) and not isinstance(current, str | bytes):
            try:
                index = int(segment)
            except ValueError:
                return None
            if not -len(current) <= index < len(current):
                return None
            current = current[index]
            continue
        return None
    if isinstance(current, bool) or not isinstance(current, int | float):
        return None
    value = float(current)
    if not math.isfinite(value):
        # NaN and the infinities are floats to Python and not numbers to a
        # decision rule. An independent review found what that costs: with
        # the common "not exactly zero" rule shape, `NaN != 0` is True and
        # `NaN == 0` is False, so a run whose solver diverged and wrote NaN
        # -- a run that produced no number at all -- returned SUPPORTS. An
        # overflowed ratio reported the strongest possible confirmation.
        # §19.5 already says the answer: a metric that is not a number is
        # INSUFFICIENT.
        return None
    return value


def analyse(
    *,
    experiment: IdeaExperiment,
    rule: DecisionRule | None,
    workspace: Path,
    spec: ExecutionSpec,
    exit_code: int | None,
) -> Analysis:
    """Apply the frozen rule to what the run produced. No model is consulted.

    The order is the honesty:

    1. collect and hash every declared output that exists;
    2. if the design had no machine-checkable rule, stop at ``INSUFFICIENT``
       and say which reason was recorded when it was designed;
    3. read the metric the rule addresses out of the file the rule names;
    4. apply both predicates.

    Both predicates holding, or neither, is ``INCONCLUSIVE``. That is not a
    fallback: a rule whose success condition covers every value is one that
    would otherwise report support for anything, and a result that satisfies
    neither condition is genuinely a result that did not settle the question.
    """

    outputs = _collect(workspace, spec.outputs)
    produced = {path for path, _digest, _size in outputs}
    absent = [item for item in spec.outputs if item not in produced]
    # `_collect` bounds what it hashes -- the first `MAX_COLLECTED_OUTPUTS`
    # declared paths, and nothing over `MAX_COLLECTED_BYTES`. A file dropped
    # by either bound is not in `produced`, and reporting it as not produced
    # is a false statement about a run that did produce it. Checked against
    # the workspace, which still exists here, so the two cases are told apart
    # rather than merged into the more damning one.
    missing = [item for item in absent if not (workspace / item).exists()]
    unrecorded = [item for item in absent if (workspace / item).exists()]
    notes: list[str] = []
    if missing:
        notes.append("declared outputs were not produced: " + ", ".join(missing))
    if unrecorded:
        notes.append(
            "declared outputs were produced but not recorded, being past this "
            f"run's limit of {MAX_COLLECTED_OUTPUTS} files or "
            f"{MAX_COLLECTED_BYTES // (1024 * 1024)}MB each: " + ", ".join(unrecorded)
        )

    if rule is None:
        return Analysis(
            conclusion=EmpiricalConclusion.INSUFFICIENT,
            summary=(
                "no machine-checkable decision rule was fixed for this "
                f"experiment: {experiment.no_rule_reason or 'no reason recorded'}"
            ),
            outputs=outputs,
            notes=tuple(notes),
        )

    if rule.output_path not in produced:
        # The same distinction the notes above draw, and the first version of
        # that fix did not carry it here -- so the evidence row held the
        # correction and the falsehood side by side. A rule output past the
        # collection bound is a file the run *did* write.
        wrote_it = (workspace / rule.output_path).exists()
        return Analysis(
            conclusion=EmpiricalConclusion.INSUFFICIENT,
            summary=(
                (
                    f"the run exited {exit_code} and wrote "
                    f"{rule.output_path}, but it is past this run's "
                    f"collection limit, so the preregistered metric was not "
                    f"read out of it here"
                )
                if wrote_it
                else (
                    f"the run exited {exit_code} and did not write "
                    f"{rule.output_path}, which the preregistered rule reads"
                )
            ),
            outputs=outputs,
            notes=(
                *notes,
                f"{rule.output_path} was produced but not collected"
                if wrote_it
                else f"{rule.output_path} is absent",
            ),
        )

    target = workspace / rule.output_path
    try:
        if target.stat().st_size > MAX_METRIC_DOCUMENT_BYTES:
            return Analysis(
                conclusion=EmpiricalConclusion.INSUFFICIENT,
                summary=(
                    f"{rule.output_path} is larger than "
                    f"{MAX_METRIC_DOCUMENT_BYTES} bytes, so the preregistered "
                    f"metric was not read out of it here"
                ),
                outputs=outputs,
                notes=tuple(notes),
            )
        document = json.loads(
            target.read_text(encoding="utf-8"), parse_constant=_not_a_number
        )
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return Analysis(
            conclusion=EmpiricalConclusion.INSUFFICIENT,
            summary=f"{rule.output_path} could not be read as JSON: {exc}",
            outputs=outputs,
            notes=tuple(notes),
        )

    observed = metric_from(document, rule.metric_path)
    if observed is None:
        return Analysis(
            conclusion=EmpiricalConclusion.INSUFFICIENT,
            summary=(
                f"{rule.metric_path} is not a number in {rule.output_path}, so "
                f"the preregistered rule could not be applied"
            ),
            outputs=outputs,
            notes=tuple(notes),
        )

    supports = rule.success.holds(observed)
    contradicts = rule.failure.holds(observed)
    if supports and not contradicts:
        conclusion = EmpiricalConclusion.SUPPORTS
    elif contradicts and not supports:
        conclusion = EmpiricalConclusion.CONTRADICTS
    else:
        conclusion = EmpiricalConclusion.INCONCLUSIVE
        notes.append(
            "both prespecified conditions hold for this value"
            if supports
            else "neither prespecified condition holds for this value"
        )
    return Analysis(
        conclusion=conclusion,
        summary=(
            f"{rule.metric_path} = {observed} in {rule.output_path}; "
            f"prespecified support {rule.success.rendered()}, "
            f"refutation {rule.failure.rendered()}"
        ),
        observed=observed,
        outputs=outputs,
        notes=tuple(notes),
    )


def _collect(
    workspace: Path, declared: Sequence[str]
) -> tuple[tuple[str, str, int], ...]:
    """Hash every declared output that exists, newest state, never following a link.

    A symlink is skipped rather than followed. The workspace is scanned for
    outbound links before the run, but a command can create one during it, and
    hashing whatever it points at would record a file outside the experiment
    as the experiment's result.
    """

    found: list[tuple[str, str, int]] = []
    for relative in list(declared)[:MAX_COLLECTED_OUTPUTS]:
        target = workspace / relative
        if target.is_symlink() or not target.is_file():
            continue
        try:
            size = target.stat().st_size
            if size > MAX_COLLECTED_BYTES:
                continue
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            continue
        found.append((relative, digest.hexdigest(), size))
    return tuple(sorted(found))


# ------------------------------------------------------------ the driver --
@dataclass(frozen=True, slots=True)
class ExperimentStep:
    """What one bounded step of the empirical route did."""

    ok: bool
    detail: str
    experiment: IdeaExperiment | None = None
    failure_class: FailureClass | None = None
    cost_usd: str = "0"
    model_calls: int = 0
    conclusion: EmpiricalConclusion | None = None
    evidence_id: str | None = None


def design(
    context: Any,
    version: IdeaVersion,
    *,
    role: ExperimentRole = ExperimentRole.PRIMARY,
    previous: IdeaExperiment | None = None,
) -> ExperimentStep:
    """Ask for one experiment over a declared command, and freeze it.

    The one model call on the empirical route. What comes back is checked
    against the project's own declarations by ordinary code before anything is
    stored, and what is stored is the *resolved* specification -- so the thing
    that later runs is the thing that was preregistered, not a second reading
    of the same design.
    """

    commands = declared_commands(context.project_id)
    if not commands:
        return ExperimentStep(
            ok=False,
            detail=(
                "no experiment commands are declared for this project, so there "
                "is nothing this runtime may run. Declare one in "
                "experiments.yaml; until then an empirical idea cannot be "
                "settled here."
            ),
            failure_class=FailureClass.CAPABILITY_DENIED,
        )

    replication = role is ExperimentRole.REPLICATION
    template = designer_for(role)
    blocks: dict[str, Sequence[str]] = {
        "idea": _idea_block(version),
        "declared_commands": command_catalogue(
            commands,
            repository=Path(context.repo_path) if context.repo_path else None,
        ),
    }
    if replication:
        if previous is None:  # pragma: no cover - the caller always has one
            raise EmpiricalError(
                "a replication is designed against the experiment it replicates",
                failure_class=FailureClass.CODE_EXCEPTION,
            )
        blocks["first_experiment"] = _first_experiment_block(previous)

    from research_os.portfolio.runner import _ask, _cost

    try:
        response = _ask(
            context,
            template,
            blocks=blocks,
            independence_group=(
                f"idea:{context.idea_id}:{version.content_digest}"
                if replication
                else None
            ),
        )
    except ProviderCallFailedError as exc:
        return ExperimentStep(
            ok=False,
            detail=f"the experiment designer could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    except BudgetExhaustedError as exc:
        return ExperimentStep(
            ok=False, detail=str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )
    cost = str(_cost(response))
    if not response.ok:
        return ExperimentStep(
            ok=False,
            detail=f"the experiment designer returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=cost,
            model_calls=1,
        )
    try:
        proposed = parse(
            ExperimentDesign,
            structured=response.structured,
            text=response.text,
            role=template.name,
        )
        proposed.check()
    except ContractError as exc:
        return ExperimentStep(
            ok=False,
            detail=str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=cost,
            model_calls=1,
        )

    if not proposed.testable:
        # A complete and useful answer. "This idea cannot be measured with the
        # commands this project declares" is better science than measuring
        # something else and calling it a test of the idea.
        return ExperimentStep(
            ok=False,
            detail=(
                "this idea is not testable with the commands this project "
                f"declares: {proposed.untestable_reason}"
            ),
            failure_class=FailureClass.CAPABILITY_DENIED,
            cost_usd=cost,
            model_calls=1,
        )

    experiment_id = _reserve_id()
    workspace = workspace_for(experiment_id)
    try:
        spec, rule = build_spec(
            proposed,
            commands=commands,
            workspace=workspace,
            max_seconds=context.config.bounds.max_experiment_seconds,
        )
    except EmpiricalError as exc:
        return ExperimentStep(
            ok=False,
            detail=str(exc),
            failure_class=exc.failure_class,
            cost_usd=cost,
            model_calls=1,
        )

    variation = variation_digest(spec)
    if replication and previous is not None:
        try:
            assert_varies(replication=variation, primary=previous.variation_digest)
        except EmpiricalError as exc:
            return ExperimentStep(
                ok=False,
                detail=str(exc),
                failure_class=exc.failure_class,
                cost_usd=cost,
                model_calls=1,
            )

    digest = spec_digest(spec)
    record = {
        "schema": "portfolio-preregistration-v1",
        "experiment_id": experiment_id,
        "idea_id": context.idea_id,
        "idea_version": version.version,
        "idea_content_digest": version.content_digest,
        "role": str(role),
        "command": proposed.command,
        "command_parameters": dict(spec_parameters(spec, proposed)),
        "primary_endpoint": proposed.primary_endpoint,
        "secondary_endpoints": list(proposed.secondary_endpoints),
        "falsification_criterion": proposed.falsification_criterion,
        "dataset_identity": proposed.dataset_identity,
        "decision_rule": rule.model_dump(mode="json") if rule else None,
        "no_decision_rule_reason": proposed.no_decision_rule_reason,
        "variation_kind": proposed.variation_kind,
        "variation_detail": proposed.variation_detail,
        "spec_digest": digest,
        "variation_digest": variation,
        "spec": _spec_record(spec),
    }
    ref = context.artifacts.put_text(
        json.dumps(
            record, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        ),
        media_type="application/json",
        role=f"idea_preregistration:{digest}",
        producer=f"{response.provider}:{template.identity}",
    )
    context.artifacts.link(
        ref, role=f"idea_preregistration:{digest}", run_id=context.run_id
    )

    try:
        experiment = context.portfolio.create_experiment(
            idea_id=context.idea_id,
            idea_version=version.version,
            project_id=context.project_id,
            role=role,
            command=proposed.command,
            spec_digest=digest,
            variation_digest=variation,
            workspace_path=str(workspace),
            decision_rule=rule.model_dump(mode="json") if rule else None,
            no_rule_reason=proposed.no_decision_rule_reason or None,
            preregistration_artifact_id=ref.artifact_id,
            origin_call_id=response.call_id,
            # The id reserved above, not a fresh one. `workspace_for` derives
            # the disposable worktree from it and `spec.cwd` is inside the
            # specification digest, so a row with a different id would carry
            # a digest describing a directory nothing runs in.
            experiment_id=experiment_id,
            prompt_version=template.identity,
        )
    except DuplicateExperimentError as exc:
        # Another pass designed one first. Not a defect, and not something to
        # work around: the row that is there is the commitment, and this
        # design is discarded rather than stored beside it.
        return ExperimentStep(
            ok=False,
            detail=str(exc),
            failure_class=FailureClass.POLICY_REFUSED,
            cost_usd=cost,
            model_calls=1,
        )
    return ExperimentStep(
        ok=True,
        detail=(
            f"preregistered {role} experiment {experiment.experiment_id} over the "
            f"declared command {proposed.command} ({digest[:12]}); "
            + (rule.rendered() if rule else "no machine-checkable rule")
        ),
        experiment=experiment,
        cost_usd=cost,
        model_calls=1,
    )


def spec_parameters(spec: ExecutionSpec, design: ExperimentDesign) -> Mapping[str, Any]:
    """The parameter values that were accepted, for the preregistration record.

    Taken from the design rather than re-derived from the argv: the resolver
    has already refused anything that did not fit, so what is left is what was
    used, and reconstructing it by parsing the argument vector back apart
    would be a second implementation of substitution.
    """

    del spec
    return dict(design.command_parameters)


def _reserve_id() -> str:
    from research_os.portfolio.ids import new_idea_experiment_id

    return new_idea_experiment_id()


def _spec_record(spec: ExecutionSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "argv": list(spec.argv),
        "cwd": spec.cwd,
        "environment": dict(spec.environment),
        "resources": dict(spec.resources),
        "env": dict(spec.env),
        "timeout_seconds": spec.timeout_seconds,
        "outputs": list(spec.outputs),
        "seeds": list(spec.seeds),
    }


def spec_from_record(record: Mapping[str, Any]) -> ExecutionSpec:
    """Rebuild the exact specification that was preregistered.

    Field for field, with no defaults: anything missing is a record this build
    did not write, and a specification assembled from a partial one would hash
    differently and be refused -- correctly, and confusingly.
    """

    return ExecutionSpec(
        name=str(record["name"]),
        argv=tuple(str(token) for token in record["argv"]),
        cwd=str(record["cwd"]),
        environment={str(k): str(v) for k, v in dict(record["environment"]).items()},
        resources={str(k): str(v) for k, v in dict(record["resources"]).items()},
        env={str(k): str(v) for k, v in dict(record["env"]).items()},
        timeout_seconds=int(record["timeout_seconds"]),
        outputs=tuple(str(item) for item in record["outputs"]),
        seeds=tuple(int(seed) for seed in record["seeds"]),
    )


def _idea_block(version: IdeaVersion) -> list[str]:
    return [
        f"title: {version.title}",
        f"research question: {version.research_question}",
        f"core idea: {version.core_idea}",
        f"mechanism: {version.mechanism or '(none stated)'}",
        f"falsifier: {version.falsifier or '(none stated)'}",
        *(f"assumption: {item}" for item in version.assumptions),
        *(f"open uncertainty: {item}" for item in version.open_uncertainties),
    ]


def _first_experiment_block(previous: IdeaExperiment) -> list[str]:
    """What the replication designer is told about the run it must differ from.

    What it ran, and deliberately **not** what it concluded. A replicator that
    has read the first conclusion is producing a second opinion about that
    conclusion rather than an independent measurement, which is the
    distinction ``gates._replication_met`` exists to keep.
    """

    rule = previous.decision_rule or {}
    return [
        f"command: {previous.command}",
        f"specification digest: {previous.spec_digest}",
        f"variation digest: {previous.variation_digest}",
        (
            f"metric: {rule.get('metric_path', '(none)')} in "
            f"{rule.get('output_path', '(none)')}"
        ),
        "(the first experiment's outcome is deliberately not shown)",
    ]


# -------------------------------------------------------------- execute --
def _worktree_record(experiment: IdeaExperiment, *, base_commit: str) -> Any:
    from research_os.automation.models import WorktreeRecord, utc_now
    from research_os.automation.worktree import branch_name, lock_path

    target = Path(experiment.workspace_path)
    return WorktreeRecord(
        task_id="T-001",
        path=str(target),
        branch=branch_name(_worktree_run_id(experiment.experiment_id), "T-001"),
        base_commit=base_commit or "0" * 40,
        lock_path=str(lock_path(target)),
        created_at=utc_now(),
    )


def owned_refs(experiment: IdeaExperiment) -> tuple[str, ...]:
    """The one ref namespace this experiment's workspace is entitled to create.

    Named in advance from the experiment id, so the canonical fingerprint can
    tell the worktree branch this run is *supposed* to add from a branch that
    should not be there. Without it every experiment reports an escape it did
    not commit, which is the defect the coding pipeline already paid for once.
    """

    from research_os.runtime.actions.coding import owned_ref_prefix

    return (owned_ref_prefix(_worktree_run_id(experiment.experiment_id)),)


def ensure_workspace(experiment: IdeaExperiment, *, repository: Path) -> Path:
    """The disposable worktree this experiment runs in, created if it is absent.

    Idempotent on purpose. Creating a Git worktree is irreversible in the
    *project* repository, and an attempt that crashed after creating one and
    before recording anything must be adopted rather than refused: the path is
    a pure function of the experiment id, so the tree the previous attempt
    built is provably this experiment's own.

    What it will not do is adopt a directory that is not a worktree of this
    repository. ``assert_isolated`` is the same check the coding pipeline runs
    before every write-enabled invocation, and it is run here for the same
    reason -- a workspace that resolved to the researcher's checkout would put
    a model-parameterised command in canonical science.
    """

    from research_os.automation.gitutil import has_commits, head_commit
    from research_os.automation.worktree import assert_isolated, create_worktree
    from research_os.errors import WorktreeError

    target = Path(experiment.workspace_path)
    base = head_commit(repository) if has_commits(repository) else ""
    if target.exists():
        assert_isolated(
            _worktree_record(experiment, base_commit=base),
            canonical_repository=repository,
        )
        return target
    _clear_stale_branch(experiment, repository=repository)
    try:
        record = create_worktree(
            run_id=_worktree_run_id(experiment.experiment_id),
            task_id="T-001",
            repository=repository,
            base_commit=base,
        )
    except WorktreeError as exc:
        raise EmpiricalError(
            f"the experiment's disposable workspace could not be created: {exc}",
            failure_class=FailureClass.EXECUTOR_FAILED,
        ) from None
    if record.path != str(target):  # pragma: no cover - defensive
        raise EmpiricalError(
            f"the workspace was created at {record.path}, not at the path "
            f"{target} this experiment already recorded",
            failure_class=FailureClass.CODE_EXCEPTION,
        )
    return target


def _clear_stale_branch(experiment: IdeaExperiment, *, repository: Path) -> None:
    """Remove this experiment's own worktree branch when its directory is gone.

    The branch survives a released worktree deliberately -- it holds the exact
    tree the measurement ran in -- and ``create_worktree`` refuses to reuse
    one. Without this, an experiment whose workspace was released and which
    then had to be re-run could never be re-run: invariant 6 says a failed
    experiment stays recoverable, and a permanently-occupied branch name is
    the shape in which it would not.

    Narrow on purpose. The branch is
    ``automation/<run id derived from this experiment id>/t-001``, a pure
    function of this row, so nothing else can be behind that name. The stale
    worktree registration is pruned first, because Git will not delete a
    branch it still believes is checked out somewhere.
    """

    from research_os.automation.gitutil import branch_exists, git
    from research_os.automation.worktree import branch_name

    branch = branch_name(_worktree_run_id(experiment.experiment_id), "T-001")
    if not branch_exists(repository, branch):
        return
    git(["worktree", "prune"], cwd=repository, check=False)
    result = git(["branch", "-D", branch], cwd=repository, check=False)
    if result.returncode != 0:  # pragma: no cover - surfaced by the caller
        LOG.warning(
            "could not remove the stale workspace branch %s: %s",
            branch,
            (result.stderr or "").strip(),
        )


def release_workspace(experiment: IdeaExperiment, *, repository: Path) -> None:
    """Throw the disposable worktree away, branch and all.

    **The branch goes too, and that is the difference from the v1 experiment
    controller**, which keeps it because its outputs are referenced by path
    inside it. Here they are not: every declared output is in the
    content-addressed store by digest, and the analysis document records the
    argument vector, the seeds, the base commit and each output's hash. So
    the branch holds nothing that is not held better elsewhere -- and an
    unattended portfolio measuring ideas for hours would otherwise leave one
    ref per experiment in the researcher's own repository, permanently.

    That is contamination of a mild kind, and this layer's rule is that a
    canonical repository is byte-identical after a measurement. Deleting the
    branch is what makes the rule literally true rather than nearly true,
    and ``test_the_canonical_repository_is_byte_identical_afterwards``
    asserts it as an equality rather than as an allowance.
    """

    from research_os.automation.worktree import release_worktree, release_worktree_lock

    target = Path(experiment.workspace_path)
    if not target.exists():
        release_worktree_lock(target)
        _clear_stale_branch(experiment, repository=repository)
        return
    try:
        release_worktree(
            _worktree_record(experiment, base_commit=""), repository=repository
        )
    except ResearchOSError as exc:
        # A workspace that will not go away is untidy and is not a reason to
        # discard a measurement that has already been analysed and recorded.
        LOG.warning(
            "could not release the workspace for %s: %s", experiment.experiment_id, exc
        )
        return
    _clear_stale_branch(experiment, repository=repository)


def submit(context: Any, experiment: IdeaExperiment) -> ExperimentStep:
    """Run the preregistered specification, once, ever.

    The ordering is the whole of the crash-safety, and it is the runtime's
    own:

    1. rebuild the specification **from the stored preregistration** and check
       its digest against the one on the experiment row. The thing that runs
       is the thing that was written down, or nothing runs;
    2. reserve budget before the spend;
    3. take the invocation ledger, keyed on the experiment and the digest, so
       a replayed work item reuses the submission instead of making a second
       one. Its reconciler asks ``external_jobs`` by specification digest,
       which is the only question that can be answered after a crash;
    4. fingerprint the canonical checkout, run, fingerprint again.

    A provider is not involved at any point here, and neither is a judgement.
    Everything this returns is a fact about machinery.
    """

    from research_os.runtime.actions.coding import (
        _describe_drift,
        canonical_fingerprint,
        escaped,
    )

    try:
        spec = _preregistered_spec(context, experiment)
    except EmpiricalError as exc:
        # Returned rather than raised, because "the thing about to run is not
        # the thing that was written down" is a disposition of this
        # experiment and not a crash of the stage. It leaves the row
        # OPERATIONALLY_FAILED with the reason on it, which is where a person
        # looks.
        return _operational(context, experiment, str(exc), exc.failure_class)
    # Local, and only local, and that is a statement about what this build
    # has rather than a limit of the design. `SlurmExecutor` exists, the
    # objective cycle submits to it, and it is unvalidated -- no `sbatch` on
    # this host has ever seen a job from this program. An idea track that
    # could submit to a cluster would also have to hold a work slot across a
    # queue wait, which is a scheduling change this release does not make.
    # `ExperimentState.RUNNING` is therefore reachable in the state machine
    # and not on this path: a local submission returns finished.
    executor_name = LOCAL
    executor = context.executors.get(executor_name)
    if executor is None:
        return ExperimentStep(
            ok=False,
            detail=(
                "no local executor is available on this machine, so the "
                "measurement this idea needs cannot be taken here"
            ),
            failure_class=FailureClass.SCHEDULER_UNAVAILABLE,
            experiment=experiment,
        )
    repository = Path(context.repo_path)

    try:
        grants = context.budgets.reserve_all(
            dimension=Dimension.WORK_ITEMS,
            amount=1,
            run_id=context.run_id,
            project_id=context.project_id,
        )
    except BudgetExhaustedError as exc:
        return ExperimentStep(
            ok=False,
            detail=str(exc),
            failure_class=FailureClass.BUDGET_EXHAUSTED,
            experiment=experiment,
        )

    # **The attempt is in the key, and that is not the thing the ledger's
    # docstring forbids.** What it forbids is a key that differs per *queue*
    # retry, because that turns the ledger into an audit log of duplicates.
    # `experiment.attempts` counts something else: how many attempts have
    # been *recorded terminal*. It increments only when this module writes
    # OPERATIONALLY_FAILED, which means the previous attempt is over and its
    # outcome is known -- so "attempt 2" really is a different logical action
    # from "attempt 1", and without this a measurement that failed on a
    # defect could never be re-run after the defect was fixed. A crash
    # *inside* an attempt leaves the counter alone, so the replay finds the
    # same key and reuses the submission, which is the case the ledger exists
    # for.
    key = idempotency_key(
        "portfolio.experiment.submit",
        experiment.experiment_id,
        experiment.spec_digest,
        experiment.attempts,
    )
    job_id = new_external_job_id()
    owned = owned_refs(experiment)

    if experiment.state is ExperimentState.OPERATIONALLY_FAILED:
        # A retry after a *recorded* failure starts from a fresh checkout.
        #
        # The reason there is a retry at all is that something was repaired,
        # and a workspace adopted from the failed attempt is pinned to the
        # commit that failed -- so the retry would run the broken tree again
        # and fail identically, for ever, until the stage ceiling stopped it.
        # What the failed attempt leaves behind that matters is its run
        # directory: the frozen manifest, stdout and stderr, all outside the
        # worktree and all kept. The tree itself is a checkout plus whatever
        # the command wrote, and neither is a diagnosis.
        release_workspace(experiment, repository=repository)

    def reconcile(_invocation: Any) -> dict[str, Any] | None:
        """Was a job for this exact specification already submitted?

        Answered from ``external_jobs`` by specification digest, which is
        unique to this experiment because the workspace path -- derived from
        the experiment id -- is inside the digest. The scheduler cannot be
        asked about a job whose id was never recorded, so this is the only
        question there is.
        """

        with context.portfolio.db.tx() as conn:
            row = conn.execute(
                "select job_id, status, exit_code, run_dir from external_jobs "
                "where spec_digest = %s and project_id = %s "
                "order by submitted_at desc limit 1",
                (experiment.spec_digest, context.project_id),
            ).fetchone()
        if row is None or str(row["status"]) == str(ExternalJobStatus.SUBMITTING):
            return None
        return {
            "job_id": str(row["job_id"]),
            "status": str(row["status"]),
            "exit_code": row["exit_code"],
            "recovered": True,
            "contained": "recovered: containment as recorded on the first attempt",
        }

    def perform() -> dict[str, Any]:
        # The workspace first, then the fingerprint. Creating a worktree adds
        # a branch and a registration to the *canonical* repository -- that
        # is what worktree isolation is -- so fingerprinting before it would
        # compare a repository without this experiment's branch against one
        # with it and report the isolation machinery as an escape. What the
        # fingerprint is for is what the *command* did, and the command has
        # not run yet.
        ensure_workspace(experiment, repository=repository)
        # **The run directory is not the workspace.** It is the runtime's
        # immutable directory under the data home, holding the frozen
        # manifest and the logs -- and it has to be somewhere else, because
        # the workspace is thrown away once the reading is done. Passing the
        # workspace as `run_dir` put `logs/stdout.txt` inside the disposable
        # worktree, which both destroyed the run's own output and wrote a
        # directory the experiment never declared into the tree being
        # measured.
        run_dir = prepare_run_dir(spec, job_id=job_id)
        before = canonical_fingerprint(repository, owned_ref_prefixes=owned)
        # The row first, in SUBMITTING, so a crash between here and the
        # executor returning leaves something to reconcile rather than a run
        # nothing in this system has heard of.
        context.runtime.create_external_job(
            job_id=job_id,
            project_id=context.project_id,
            run_id=context.run_id,
            work_id=context.work_id,
            executor=executor_name,
            spec_digest=experiment.spec_digest,
            run_dir=str(run_dir),
        )
        started = time.monotonic()
        handle = executor.submit(spec, run_dir=run_dir)
        # Monotonic, so a clock adjustment during a long run cannot produce a
        # negative duration. Every other resource this run cost is
        # unobserved, and `experiment/models.py`'s rule applies: unobserved
        # is unknown, never a number.
        wall_clock_seconds = round(time.monotonic() - started, 3)
        status = (
            ExternalJobStatus.COMPLETED
            if handle.finished and handle.exit_code == 0
            else ExternalJobStatus.FAILED
            if handle.finished
            else ExternalJobStatus.SUBMITTED
        )
        context.runtime.update_external_job(
            job_id,
            status=status,
            scheduler_job_id=handle.scheduler_job_id,
            exit_code=handle.exit_code,
            detail=handle.detail,
            # Persisted, because the analysis document is written by a later
            # stage that has only this row to read. Before migration 0030
            # there was nowhere to put them, so `interpret` wrote
            # `job.detail` under the key `containment` -- the literal string
            # "completed" -- and the record could not tell a contained
            # measurement from an uncontained one.
            contained=handle.contained,
            containment=str(handle.containment),
            wall_clock_seconds=wall_clock_seconds,
        )
        after = canonical_fingerprint(repository, owned_ref_prefixes=owned)
        if escaped(before, after):
            # `escaped` and not `!=`: a key that appears and is an owned or a
            # reserved ref is permitted, and everything else -- a capsule
            # file changed, any other ref created, moved or deleted, an owned
            # ref *moved* -- is not. Comparing the dictionaries directly
            # reported the Curator writing the idea bank during a
            # measurement as the measurement escaping.
            drift = _describe_drift(before, after)
            LOG.error(
                "experiment %s changed the canonical checkout: %s",
                experiment.experiment_id,
                drift,
            )
            return {
                "job_id": job_id,
                "status": str(status),
                "exit_code": handle.exit_code,
                "escaped": drift,
                "contained": f"{handle.containment}",
            }
        return {
            "job_id": job_id,
            "status": str(status),
            "exit_code": handle.exit_code,
            "finished": handle.finished,
            "run_dir": str(run_dir),
            "wall_clock_seconds": wall_clock_seconds,
            "contained": (
                f"{handle.containment}"
                if handle.contained
                else f"UNCONTAINED: {handle.containment}"
            ),
        }

    try:
        outcome = context.ledger.run(
            key=key,
            kind="portfolio.experiment.submit",
            run_id=context.run_id,
            work_id=context.work_id,
            request={
                "experiment_id": experiment.experiment_id,
                "spec_digest": experiment.spec_digest,
            },
            perform=perform,
            reconcile=reconcile,
        )
    except ContainmentUnavailableError as exc:
        # Terminal for this attempt and not repairable: no repair makes a
        # kernel offer user namespaces. Recorded as an operational failure, so
        # the idea is blocked on an external dependency rather than refuted.
        context.budgets.release_all(grants)
        return _operational(
            context, experiment, str(exc), FailureClass.CAPABILITY_DENIED
        )
    except (ExecutorError, IdempotencyError) as exc:
        context.budgets.release_all(grants)
        return _operational(context, experiment, str(exc), FailureClass.EXECUTOR_FAILED)
    except EmpiricalError as exc:
        context.budgets.release_all(grants)
        return _operational(context, experiment, str(exc), exc.failure_class)
    except ResearchOSError as exc:
        context.budgets.release_all(grants)
        return _operational(
            context,
            experiment,
            f"could not submit: {exc}",
            FailureClass.EXECUTOR_FAILED,
        )
    context.budgets.settle_all(grants)

    result = dict(outcome.result)
    if result.get("escaped"):
        # A policy refusal rather than an executor failure: it must not be
        # retried, because retrying runs the same escaping command again.
        return _operational(
            context,
            experiment,
            (
                f"the experiment changed the canonical checkout, which it must "
                f"never do: {result['escaped']}. The drift is named above and "
                f"the job record, its frozen manifest and both logs are kept; "
                f"what has to be inspected is the checkout, not the workspace."
            ),
            FailureClass.POLICY_REFUSED,
            job_id=str(result.get("job_id") or ""),
        )

    status = ExternalJobStatus(str(result["status"]))
    wall_clock = result.get("wall_clock_seconds")
    updated = context.portfolio.update_experiment(
        experiment.experiment_id,
        state=(
            ExperimentState.COMPLETED
            if status
            in {
                ExternalJobStatus.COMPLETED,
                ExternalJobStatus.FAILED,
                ExternalJobStatus.TIMED_OUT,
                ExternalJobStatus.CANCELLED,
            }
            else ExperimentState.RUNNING
        ),
        job_id=str(result["job_id"]),
        detail=(
            f"{status}, exit {result.get('exit_code')}"
            + (f", {wall_clock}s" if wall_clock is not None else "")
        ),
        # **Not counted.** `attempts` means "recorded terminal failures", and
        # it is in the idempotency key -- so incrementing it on a successful
        # submission would give the crash-replay a different key and run the
        # measurement a second time, which is precisely the case the ledger
        # exists to prevent. Only `_operational` increments it.
        count_attempt=False,
    )
    return ExperimentStep(
        ok=True,
        detail=f"the experiment ran: {status}, exit {result.get('exit_code')}",
        experiment=updated,
    )


def _preregistered_spec(context: Any, experiment: IdeaExperiment) -> ExecutionSpec:
    """The specification as it was written down, or nothing runs.

    Read back out of the preregistration artifact rather than rebuilt from the
    design, and then hashed and compared against the digest on the experiment
    row. Two hashes agreeing is what makes "was this the test we said we would
    run" answerable without reading a diff of a manifest.
    """

    if not experiment.preregistration_artifact_id:
        raise EmpiricalError(
            f"{experiment.experiment_id} has no stored preregistration, so there "
            f"is nothing that says what it was going to run",
            failure_class=FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
        )
    try:
        record = json.loads(
            context.artifacts.get_text(experiment.preregistration_artifact_id)
        )
        spec = spec_from_record(record["spec"])
    except (
        ResearchOSError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
    ) as exc:
        raise EmpiricalError(
            f"the preregistration for {experiment.experiment_id} is unreadable: {exc}",
            failure_class=FailureClass.ARTIFACT_MISSING,
        ) from None
    rebuilt = spec_digest(spec)
    if rebuilt != experiment.spec_digest:
        raise EmpiricalError(
            f"the preregistered specification hashes to {rebuilt[:12]} and the "
            f"experiment record says {experiment.spec_digest[:12]}. Running a "
            f"different test under a preregistration is not something this "
            f"layer may decide.",
            failure_class=FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
        )
    return spec


def _operational(
    context: Any,
    experiment: IdeaExperiment,
    detail: str,
    failure_class: FailureClass,
    *,
    job_id: str | None = None,
    keep: Sequence[tuple[str, str, int]] = (),
) -> ExperimentStep:
    """Record that the measurement did not happen, and say so in those words.

    ``OPERATIONALLY_FAILED`` and never a conclusion. The experiment row stays
    recoverable: it keeps its preregistration, its specification digest and
    whatever job it did manage to create, so the next attempt resumes rather
    than designing a second experiment.

    ``keep`` is whatever the failed run did leave on disk, hashed, so it can
    be put in the store before the workspace goes. The workspace *does* go,
    including on this path, and that is what makes "the canonical repository
    is byte-identical afterwards" unconditional rather than true only of
    measurements that succeeded. A soak that failed three experiments would
    otherwise leave three worktrees and three branches in the researcher's
    repository, permanently, and the diagnosis is not in them: the argument
    vector, the frozen manifest, the exit status and both logs are all in the
    run directory under the data home.
    """

    if keep:
        _store_outputs(
            context,
            experiment,
            workspace=Path(experiment.workspace_path),
            analysis=Analysis(
                conclusion=EmpiricalConclusion.OPERATIONALLY_BLOCKED,
                summary=detail,
                outputs=tuple(keep),
            ),
        )
    updated = context.portfolio.update_experiment(
        experiment.experiment_id,
        state=ExperimentState.OPERATIONALLY_FAILED,
        job_id=job_id or None,
        failure_class=str(failure_class),
        detail=detail,
        count_attempt=True,
    )
    if context.repo_path is not None:
        release_workspace(updated, repository=Path(context.repo_path))
    return ExperimentStep(
        ok=False,
        detail=detail,
        experiment=updated,
        failure_class=failure_class,
        conclusion=EmpiricalConclusion.OPERATIONALLY_BLOCKED,
    )


# ------------------------------------------------------------- interpret --
def interpret(context: Any, experiment: IdeaExperiment) -> ExperimentStep:
    """Apply the frozen rule and record one evidence row. No model is asked.

    The result of the analysis is stored *before* the evidence row and the
    evidence row is written only when the experiment does not already name
    one, so a crash between the two leaves an artifact the retry recognises
    rather than a second reading of one experiment.
    """

    job = context.runtime.get_external_job(experiment.job_id or "")
    if job is None:
        return _operational(
            context,
            experiment,
            f"{experiment.experiment_id} names no execution that this runtime "
            f"can find, so there is nothing to read",
            FailureClass.ARTIFACT_MISSING,
        )
    if job.status not in {
        ExternalJobStatus.COMPLETED,
        ExternalJobStatus.FAILED,
        ExternalJobStatus.TIMED_OUT,
        ExternalJobStatus.CANCELLED,
    }:
        return ExperimentStep(
            ok=False,
            detail=f"{job.job_id} is {job.status}; the measurement is not finished",
            failure_class=FailureClass.SCHEDULER_UNAVAILABLE,
            experiment=experiment,
        )

    if job.status is not ExternalJobStatus.COMPLETED or (job.exit_code or 0) != 0:
        # The command ran and did not complete. That is a fact about the
        # program, not about the idea: an executor that crashed, a timeout, a
        # missing interpreter. `FailureClass` has no member for a refutation
        # and this is not one, so nothing is recorded as evidence.
        #
        # Whatever it did write is kept, by content hash, before the
        # workspace goes: a timed-out run's partial output is the most
        # useful thing about it, and it is not a result.
        partial: tuple[tuple[str, str, int], ...] = ()
        workspace = Path(experiment.workspace_path)
        if workspace.is_dir():
            try:
                partial = _collect(
                    workspace, _preregistered_spec(context, experiment).outputs
                )
            except EmpiricalError:  # pragma: no cover - reported below anyway
                partial = ()
        _store_logs(context, experiment, run_dir=Path(job.run_dir))
        return _operational(
            context,
            experiment,
            (
                f"the experiment did not run correctly ({job.status}, exit "
                f"{job.exit_code}: {job.detail or 'no detail'}). No scientific "
                f"conclusion follows from an execution that failed."
            ),
            FailureClass.EXECUTOR_FAILED,
            keep=partial,
        )

    spec = _preregistered_spec(context, experiment)
    rule = (
        DecisionRule.model_validate(experiment.decision_rule)
        if experiment.decision_rule
        else None
    )
    workspace = Path(experiment.workspace_path)
    if not workspace.is_dir():
        return _operational(
            context,
            experiment,
            (
                f"the workspace {workspace} this experiment ran in is gone "
                f"before its outputs were collected, so what it produced "
                f"cannot be established"
            ),
            FailureClass.ARTIFACT_MISSING,
        )

    analysis = analyse(
        experiment=experiment,
        rule=rule,
        workspace=workspace,
        spec=spec,
        exit_code=job.exit_code,
    )
    stored = _store_outputs(context, experiment, workspace=workspace, analysis=analysis)
    document = analysis.record(
        experiment=experiment,
        spec=spec,
        job_id=job.job_id,
        exit_code=job.exit_code,
        contained=_containment(job),
    )
    document["stored_outputs"] = stored
    document["stored_logs"] = _store_logs(
        context, experiment, run_dir=Path(job.run_dir)
    )
    # Every resource except wall clock is unobserved here, and it says so
    # rather than being estimated. Wall clock is the monotonic duration
    # `submit` measured around the executor call, persisted on the job row;
    # an earlier version read `job.detail`, which for a local run is the
    # string "completed".
    document["resource_usage"] = {
        "wall_clock_seconds": (
            "unknown" if job.wall_clock_seconds is None else str(job.wall_clock_seconds)
        ),
        "cpu_seconds": "unknown",
        "max_rss_kb": "unknown",
        "observed_by": "LocalExecutor (wall clock only)",
    }
    # Read from the workspace while it still exists, because it is about to
    # not. Invariant 11 of `DESIGN_INVARIANTS.md` -- an experiment is
    # traceable to code, configuration, data and version -- and the version
    # is the commit the disposable worktree was cut from.
    document["base_commit"] = _workspace_commit(workspace)
    ref = context.artifacts.put_text(
        json.dumps(
            document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        ),
        media_type="application/json",
        role=f"idea_experiment_analysis:{experiment.experiment_id}",
        producer="portfolio.empirical.analyse@1",
    )
    context.artifacts.link(
        ref,
        role=f"idea_experiment_analysis:{experiment.experiment_id}",
        run_id=context.run_id,
    )

    current = context.portfolio.require_experiment(experiment.experiment_id)
    evidence_id = current.evidence_id
    if evidence_id is None:
        strength = EVIDENCE_STRENGTH_FOR_CONCLUSION[analysis.conclusion]
        kind = _evidence_kind(context, experiment)
        evidence = context.portfolio.add_evidence(
            idea_id=experiment.idea_id,
            idea_version=experiment.idea_version,
            kind=kind,
            strength=strength,
            summary=_evidence_summary(experiment, analysis),
            artifact_id=ref.artifact_id,
            job_id=job.job_id,
            # The *design* call, so a replication can be shown to be
            # independent of the work it replicates. The analysis itself had
            # no model in it, which is the point; what a call id records here
            # is whose question this measurement answers.
            source_call_id=experiment.origin_call_id,
        )
        evidence_id = evidence.evidence_id

    updated = context.portfolio.update_experiment(
        experiment.experiment_id,
        state=ExperimentState.INTERPRETED,
        analysis_artifact_id=ref.artifact_id,
        conclusion=analysis.conclusion,
        evidence_id=evidence_id,
        detail=analysis.summary[:2000],
    )
    release_workspace(updated, repository=Path(context.repo_path))
    return ExperimentStep(
        ok=True,
        detail=f"{analysis.conclusion}: {analysis.summary}",
        experiment=updated,
        conclusion=analysis.conclusion,
        evidence_id=evidence_id,
    )


def _workspace_commit(workspace: Path) -> str:
    """The commit the disposable worktree was cut from, or a stated unknown."""

    from research_os.automation.gitutil import head_commit

    try:
        return head_commit(workspace)
    except ResearchOSError:  # pragma: no cover - a workspace with no commits
        return "unknown"


def _evidence_kind(context: Any, experiment: IdeaExperiment) -> EvidenceKind:
    """What sort of observation this measurement is.

    ``REPLICATION`` for the second one, whatever the type: that is what the
    HUMAN_READY rule asks for and what makes it a second line rather than a
    second row.

    Otherwise the adjudication type decides between the two kinds a run can
    honestly be. An `EMPIRICAL` idea is settled by measurement and its
    evidence is an ``EXPERIMENT``. A purely `DIAGNOSTIC` one is answerable by
    observing *this implementation*, and its evidence is ``CODE`` -- "an
    observation of this implementation, addressable as an artifact", which
    `EVIDENCE_RULES[DIAGNOSTIC]` asks for by name and which the same entry
    is careful to say "is a fact about the program and never on its own a
    reason to close a scientific target".

    Written as ``EXPERIMENT`` whenever `EMPIRICAL` is among the declared
    types, including alongside `DIAGNOSTIC`. An idea that declared both has
    asked for two kinds of evidence and one run produces one; the union rule
    is doing what it is for, and the track stops saying which is missing
    rather than one row being counted twice.
    """

    if experiment.role is ExperimentRole.REPLICATION:
        return EvidenceKind.REPLICATION
    declared = set(
        context.portfolio.require_version(
            experiment.idea_id, experiment.idea_version
        ).adjudication_types
    )
    if AdjudicationType.DIAGNOSTIC in declared and (
        AdjudicationType.EMPIRICAL not in declared
    ):
        return EvidenceKind.CODE
    return EvidenceKind.EXPERIMENT


def _evidence_summary(experiment: IdeaExperiment, analysis: Analysis) -> str:
    """What a reviewer reads. Facts, and the rule that was fixed beforehand.

    Invariant 12 of the empirical brief in one function: a reviewer sees what
    was measured and what the prespecified rule said about it, never the
    experiment author's reading of it. There is no author -- the comparison
    was arithmetic.
    """

    headline = (
        f"{experiment.role} experiment {experiment.experiment_id} "
        f"({experiment.command}, spec {experiment.spec_digest[:12]}): "
        f"{analysis.conclusion}"
    )
    parts = [headline, analysis.summary]
    if analysis.outputs:
        parts.append(
            "outputs: "
            + ", ".join(
                f"{path} sha256:{digest[:12]}" for path, digest, _ in analysis.outputs
            )
        )
    parts.extend(analysis.notes)
    return " | ".join(parts)[:4000]


def _store_logs(
    context: Any, experiment: IdeaExperiment, *, run_dir: Path
) -> list[dict[str, str]]:
    """Put the run's stdout and stderr in the artifact store.

    They live in the runtime's immutable run directory rather than in the
    disposable workspace, so they survive the worktree being thrown away --
    which is the whole reason the two directories are different. A reviewer
    who wants to know why a measurement said what it said reads these.
    """

    stored: list[dict[str, str]] = []
    for relative in ("logs/stdout.txt", "logs/stderr.txt"):
        target = run_dir / relative
        if not target.is_file() or not target.stat().st_size:
            continue
        try:
            ref = context.artifacts.put_file(
                target,
                media_type="text/plain; charset=utf-8",
                role=f"idea_experiment_log:{experiment.experiment_id}",
                producer=f"executor:{experiment.command}",
            )
        except ResearchOSError as exc:  # pragma: no cover - a log that vanished
            LOG.warning("could not store %s: %s", target, exc)
            continue
        context.artifacts.link(
            ref,
            role=f"idea_experiment_log:{experiment.experiment_id}",
            run_id=context.run_id,
        )
        stored.append({"path": relative, "artifact_id": ref.artifact_id})
    return stored


def _containment(job: ExternalJob) -> str:
    """What the record may say about whether this run was contained.

    Three answers and not two. ``None`` means the row predates migration
    0030 and genuinely does not know, which is not the same as knowing it
    ran uncontained -- and writing "uncontained" for it would be inventing
    the more alarming of two answers rather than reporting the absence of
    one.
    """

    if job.contained is None:
        return "unrecorded (this job predates the containment columns)"
    if job.contained:
        return str(job.containment or "contained")
    return f"UNCONTAINED: {job.containment or 'no backend'}"


def _store_outputs(
    context: Any,
    experiment: IdeaExperiment,
    *,
    workspace: Path,
    analysis: Analysis,
) -> list[dict[str, str]]:
    """Put every collected output into the content-addressed store.

    Immutability is the store's, not this function's: an artifact's id *is*
    the hash of its bytes, so storing the same output twice is one artifact
    and changing one afterwards changes its address. Nothing here has to
    enforce it and nothing here could.
    """

    stored: list[dict[str, str]] = []
    for relative, digest, _size in analysis.outputs:
        target = workspace / relative
        if target.is_symlink() or not target.is_file():  # pragma: no cover - raced
            continue
        try:
            ref = context.artifacts.put_file(
                target,
                role=f"idea_experiment_output:{experiment.experiment_id}",
                producer=f"executor:{experiment.command}",
            )
        except ResearchOSError as exc:
            LOG.warning("could not store %s from %s: %s", relative, workspace, exc)
            continue
        context.artifacts.link(
            ref,
            role=f"idea_experiment_output:{experiment.experiment_id}",
            run_id=context.run_id,
        )
        stored.append(
            {"path": relative, "artifact_id": ref.artifact_id, "sha256": digest}
        )
    return stored


# --------------------------------------------------------------- driver --
def designer_for(role: ExperimentRole) -> Any:
    """The template that designs this role's experiment."""

    return PORTFOLIO_TEMPLATES[
        "replication_designer"
        if role is ExperimentRole.REPLICATION
        else "experiment_designer"
    ]


def _is_stale(experiment: IdeaExperiment, *, role: ExperimentRole) -> bool:
    """Whether this design was made by a prompt this build no longer uses.

    An interpreted experiment is never stale: what was measured was measured,
    and the reading of it stands whatever the prompt that designed it has
    since become. Only a commitment that has not yet been settled can be one
    to a question no longer being asked.
    """

    if experiment.state in {
        ExperimentState.INTERPRETED,
        ExperimentState.SUPERSEDED,
    }:
        return False
    return experiment.prompt_version != designer_for(role).identity


def previous_for(context: Any, role: ExperimentRole) -> IdeaExperiment | None:
    """The primary a replication is designed against, or nothing."""

    if role is not ExperimentRole.REPLICATION:
        return None
    version = context.portfolio.require_version(context.idea_id)
    return context.portfolio.get_experiment(
        idea_id=context.idea_id,
        idea_version=version.version,
        role=ExperimentRole.PRIMARY,
    )


def advance(
    context: Any,
    version: IdeaVersion,
    *,
    role: ExperimentRole = ExperimentRole.PRIMARY,
) -> ExperimentStep:
    """Move this idea version's experiment forward by one step.

    Re-entrant, and the states are the reason it can be: a stage invocation
    finds whatever the last one left and does the next thing. A local
    execution passes through design, submission and interpretation in one
    call because none of the intermediate states is waiting on anything; a
    cluster job stops at ``RUNNING`` and the next invocation reads it.
    """

    store = context.portfolio
    existing = store.get_experiment(
        idea_id=context.idea_id, idea_version=version.version, role=role
    )
    previous = previous_for(context, role)
    if role is ExperimentRole.REPLICATION and previous is None:
        return ExperimentStep(
            ok=False,
            detail=(
                "there is no primary experiment for this idea version, so "
                "there is nothing to replicate"
            ),
            failure_class=FailureClass.POLICY_REFUSED,
        )
    design_cost, design_calls = "0", 0
    if existing is None:
        step = design(context, version, role=role, previous=previous)
        if not step.ok or step.experiment is None:
            return step
        existing = step.experiment
        design_cost, design_calls = step.cost_usd, step.model_calls

    if _is_stale(existing, role=role):
        # Designed by a prompt this build has superseded. The portfolio
        # already treats a review that way -- `PortfolioStore.live_reviews`
        # reads `CURRENT_REVIEW_PROMPTS` and calls the rest stale, because a
        # review produced by a retired prompt answered a question no longer
        # being asked. A design is the same object under a different name,
        # and without this the improved prompt could never reach an idea
        # whose experiment the old one had already written: every attempt
        # would resubmit the old specification until the stage ceiling.
        #
        # Retired rather than deleted. What it measured, or failed to, is a
        # record, and the partial unique index lets the successor take the
        # name.
        context.portfolio.update_experiment(
            existing.experiment_id,
            state=ExperimentState.SUPERSEDED,
            detail=(
                f"designed by {existing.prompt_version or 'an unrecorded prompt'}, "
                f"which this build has superseded"
            ),
        )
        if context.repo_path is not None:
            release_workspace(existing, repository=Path(context.repo_path))
        step = design(context, version, role=role, previous=previous)
        if not step.ok or step.experiment is None:
            return step
        existing = step.experiment
        design_cost, design_calls = step.cost_usd, step.model_calls

    if existing.state is ExperimentState.INTERPRETED:
        return ExperimentStep(
            ok=True,
            detail=(
                f"{existing.experiment_id} has already been interpreted: "
                f"{existing.conclusion}"
            ),
            experiment=existing,
            conclusion=existing.conclusion,
            evidence_id=existing.evidence_id,
            cost_usd=design_cost,
            model_calls=design_calls,
        )
    if existing.state is ExperimentState.SUPERSEDED:  # pragma: no cover - defensive
        return ExperimentStep(
            ok=False,
            detail="this experiment measures a version that has been revised",
            failure_class=FailureClass.POLICY_REFUSED,
            experiment=existing,
        )

    if existing.state in {
        ExperimentState.PROPOSED,
        ExperimentState.EXECUTABLE,
        ExperimentState.OPERATIONALLY_FAILED,
        ExperimentState.RUNNING,
    }:
        step = submit(context, existing)
        if not step.ok or step.experiment is None:
            return ExperimentStep(
                ok=step.ok,
                detail=step.detail,
                experiment=step.experiment,
                failure_class=step.failure_class,
                cost_usd=design_cost,
                model_calls=design_calls,
                conclusion=step.conclusion,
            )
        existing = step.experiment

    if existing.state is not ExperimentState.COMPLETED:
        return ExperimentStep(
            ok=False,
            detail=(
                f"{existing.experiment_id} is {existing.state}; the measurement "
                f"is not finished and there is nothing to read yet"
            ),
            failure_class=FailureClass.SCHEDULER_UNAVAILABLE,
            experiment=existing,
            cost_usd=design_cost,
            model_calls=design_calls,
        )

    step = interpret(context, existing)
    return ExperimentStep(
        ok=step.ok,
        detail=step.detail,
        experiment=step.experiment,
        failure_class=step.failure_class,
        cost_usd=design_cost,
        model_calls=design_calls,
        conclusion=step.conclusion,
        evidence_id=step.evidence_id,
    )
