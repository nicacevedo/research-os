"""Designing and running experiments, with the specification frozen first.

The separation this module exists to enforce:

```text
design      -> a specification, digested, written down, nothing run
execute     -> the specification, unchanged, submitted to an executor
interpret   -> the results, against the criteria that were fixed before them
```

**The primary endpoint is fixed before any result exists.** The experimentalist
prompt says so and the schema requires ``primary_endpoint``,
``success_criteria`` and ``failure_criteria`` as a condition of being a valid
design. The digest of the frozen spec goes on the job row, so "was this the test
we said we would run" is answerable by comparing two hashes rather than by
reading a diff of a manifest.

**Changing the primary endpoint after seeing results is an `A2` action.** Not a
runtime mutation, not a quiet edit, and not something approval lets the runtime
do -- it is
:data:`~research_os.runtime.policy.ActionKind.CHANGE_PRIMARY_ENDPOINT`, which
the person performs and which becomes a recorded Decision. This is the single
most important gate in the system, because a post-hoc endpoint change is how a
null result becomes a positive one, and it is the change that leaves no trace if
the system lets it happen silently.

**A refutation is a success.** :func:`interpret_results` returns
``ok=True`` whether the prespecified criteria were met or not, and records which.
An experiment that ran correctly and answered "no" has succeeded; nothing in
this module can express it as a failure, because
:class:`~research_os.runtime.failures.FailureClass` has no member for it.

**An interpretation names the experiment it is of, durably.** Not "the job that
finished most recently" -- which is what this module used to do, and which is
association by temporal coincidence. Two jobs that finish while a cycle is
planning gave the interpretation to whichever the scheduler reaped second; a
job interpreted in one cycle was interpreted again in the next; and a reading
could be compared against a preregistration belonging to a different
experiment. :data:`INTERPRETER_VERSION` and the ``experiment_interpretations``
relation replace all of that: the claim is durable, it is unique per
``(job, interpreter version)``, and it carries the ``spec_digest`` so the
binding to one frozen specification is a property of the row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import (
    EXCERPT_KEY,
    ActionOutcome,
    bounded_excerpt,
)
from research_os.runtime.budgets import BudgetExhaustedError, Dimension
from research_os.runtime.context import CycleContext
from research_os.runtime.executors import (
    LOCAL,
    SLURM,
    ContainmentUnavailableError,
    ExecutorError,
    prepare_run_dir,
    spec_digest,
)
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import MAX_EXCERPT_CHARS
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.ids import new_external_job_id
from research_os.runtime.interfaces import ExecutionSpec, ModelRequest
from research_os.runtime.models import ExternalJobStatus, InterpretationStatus
from research_os.runtime.prompts import EXPERIMENTALIST

LOG = logging.getLogger("research_os.runtime.actions.experiments")

#: Which reader produced an interpretation, and therefore what counts as
#: already-read.
#:
#: In the interpretation identity key rather than outside it, because
#: changing how a result is read is a legitimate reason to read the same
#: experiment again -- and the second reading is a *different*
#: interpretation rather than a correction of the first. Both are kept, and
#: a person can see that two readers disagreed.
#:
#: Bump this when the *comparison* changes: which criteria are consulted,
#: how "ran correctly" is decided, what is written into the artifact. Do not
#: bump it for a message, a log line or a refactor, because every bump
#: re-reads every terminal job in every project.
INTERPRETER_VERSION = "interpret_results@1"


def declared_commands(context: CycleContext, project_id: str) -> dict[str, Any]:
    """The experiment commands the *researcher* declared for this project.

    From ``experiments.yaml``, through the v1 experiment config. This is the
    boundary that keeps the runtime from executing model-authored argv: the
    experimentalist chooses *which declared command* to run and supplies values
    for the parameters the researcher declared, and it cannot express anything
    else.

    The first version took ``argv`` straight from the model and ran it with
    ``cwd`` set to the canonical checkout. An independent review pointed out
    what that is. It was unreachable only because the executors were never
    wired up, which is a wiring omission rather than a policy -- so the policy
    is here now.
    """

    from research_os.experiment.config import load_config as load_experiment_config

    try:
        config = load_experiment_config()
    except ResearchOSError as exc:
        LOG.debug("no experiment configuration: %s", exc)
        return {}
    project = config.projects.get(project_id)
    return dict(project.commands) if project is not None else {}


def _parameter_contract(parameter: Any) -> str:
    """One declared parameter, with every constraint the validator enforces.

    The catalogue used to list parameter *names* and nothing else, and the
    experimentalist had no way to learn the rules its answer would be checked
    against. On the thesis pilot it proposed an absolute path for
    ``adjudicate-pricing``'s ``out``; ``spec._assert_relative`` rejected it,
    ``design_experiment`` failed, and the next cycle proposed the same absolute
    path and failed identically -- twice, because nothing in the prompt had
    changed. A constraint the model is graded on and never shown is a loop.
    """

    parts = [f"type={parameter.type}"]
    parts.append("required" if parameter.required else "optional")
    if parameter.default is not None:
        parts.append(f"default={parameter.default!r}")
    if parameter.minimum is not None:
        parts.append(f"minimum={parameter.minimum}")
    if parameter.maximum is not None:
        parts.append(f"maximum={parameter.maximum}")
    if parameter.choices:
        parts.append("choices=" + "|".join(str(one) for one in parameter.choices))
    if str(parameter.type) == "path":
        parts.append(
            "MUST be a relative in-tree path: no leading '/' or '~', POSIX '/' "
            "separators, no '.' or '..' segments"
        )
    detail = f" -- {parameter.description}" if parameter.description else ""
    return f"    {parameter.name}: {', '.join(parts)}{detail}"


def _command_lines(name: str, spec: Any) -> list[str]:
    """One declared command as the experimentalist needs to see it."""

    lines = [f"{name}: {spec.description or '(no description)'}"]
    if not spec.parameters:
        lines.append("    (no parameters)")
    else:
        lines.extend(_parameter_contract(one) for one in spec.parameters)
    if spec.outputs:
        lines.append("    declared outputs: " + ", ".join(spec.outputs))
    return lines


def design_experiment(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Produce a preregistered specification over a declared command. Runs nothing.

    ``A0``: writing down what you intend to test changes nothing. Whether it is
    then *run* is a separate action with its own authority, which is what makes
    "we designed it and then decided not to" an expressible outcome rather than
    a wasted submission.
    """

    frontier = state.get("frontier", {})
    hypothesis_ids = tuple(
        plan.get("addresses", ())
        or frontier.get("hypotheses_without_tests", ())
        or frontier.get("actionable_hypotheses", ())
    )
    if not hypothesis_ids:
        return ActionOutcome.succeeded(
            "no untested hypothesis to design for", data={"hypotheses": []}
        )

    available = declared_commands(context, str(state["project_id"]))
    if not available:
        # Not a failure, and not something to work around. An experiment this
        # runtime may run is one the researcher declared in `experiments.yaml`;
        # with none declared there is nothing it is permitted to run, and saying
        # so is the correct outcome.
        return ActionOutcome.succeeded(
            "no experiment commands are declared for this project, so there is "
            "nothing the runtime may run. Declare one in experiments.yaml.",
            data={"hypotheses": list(hypothesis_ids), "declared_commands": []},
        )

    target = str(hypothesis_ids[0])
    try:
        obj = context.kernel.object(target)
        statement = getattr(obj, "statement", "") or getattr(obj, "title", target)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not read {target}: {exc}", failure_class=FailureClass.CODE_EXCEPTION
        )

    catalogue = [
        line
        for name, spec in sorted(available.items())
        for line in _command_lines(name, spec)
    ]
    prompt = EXPERIMENTALIST.render(
        fields={
            "hypothesis": f"{target}: {statement}",
            "data_description": str(
                plan.get("parameters", {}).get("data_description")
                or "the project repository; no external dataset declared"
            ),
            "available_executors": ", ".join(sorted(context.executors)) or "local",
        },
        blocks={"repository": [*catalogue, f"repository: {state['repo_path']}"]},
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=EXPERIMENTALIST.role,
                capability=EXPERIMENTALIST.capability,
                prompt=prompt,
                prompt_version=EXPERIMENTALIST.identity,
                criticality=EXPERIMENTALIST.criticality,
                independence=EXPERIMENTALIST.independence,
                independence_group=f"design:{state['run_id']}:{target}",
                json_schema=EXPERIMENTALIST.output_schema,
            )
        )
    # **Only the budget is caught here.** A `RoutingError` -- which is what a
    # provider that did not answer now raises -- is deliberately allowed to
    # propagate out of this handler.
    #
    # Catching it looked careful and was the opposite. It converted an outage
    # into an `ActionOutcome`, and an `ActionOutcome` carries a failure class
    # and nothing else: not the breaker's `cooldown_until`, not whether an
    # invocation happened. Both are what the queue needs to schedule the
    # retry, so every provider failure that came through this door was
    # rescheduled by the linear backoff alone and charged an attempt even when
    # routing had refused before calling anything -- the 2026-09-19 arithmetic
    # exactly, on a second path.
    #
    # A budget refusal is genuinely different and stays: it is a policy answer
    # rather than a malfunction, no amount of waiting changes it, and the
    # honest terminal state is BUDGET_EXHAUSTED.
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            f"the experimentalist did not run: {exc}",
            failure_class=FailureClass.BUDGET_EXHAUSTED,
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.failed(
            f"the experimentalist returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    design = dict(response.structured)
    if not design.get("testable", False):
        # A valid and useful answer. Saying "this cannot be tested with the data
        # described" is better science than specifying something else and
        # calling it a test of the hypothesis.
        return ActionOutcome.succeeded(
            f"{target} is not testable with the declared commands",
            data={
                "hypothesis": target,
                "testable": False,
                "reason": design.get("untestable_reason", ""),
            },
        )

    chosen = str(design.get("command") or "").strip()
    if chosen not in available:
        return ActionOutcome.failed(
            f"{chosen!r} is not a command this project declares. Available: "
            f"{', '.join(sorted(available))}",
            failure_class=FailureClass.POLICY_REFUSED,
        )

    from research_os.experiment.spec import resolve_command

    try:
        resolved = resolve_command(
            available[chosen], dict(design.get("command_parameters") or {})
        )
    except ResearchOSError as exc:
        # The v1 resolver validates each value against the type the researcher
        # declared and refuses a value for a parameter that was not declared.
        return ActionOutcome.failed(
            f"the experimentalist's parameters do not fit {chosen}: {exc}",
            failure_class=FailureClass.POLICY_REFUSED,
        )

    spec = _spec_from_resolved(
        resolved, design, state=state, name=f"{target.lower()}-test"
    )
    digest = spec_digest(spec)
    record = {
        "hypothesis": target,
        "testable": True,
        "spec_digest": digest,
        "command": chosen,
        "command_parameters": dict(resolved.parameters),
        "primary_endpoint": design.get("primary_endpoint", ""),
        "secondary_endpoints": design.get("secondary_endpoints", []),
        "success_criteria": design.get("success_criteria", ""),
        "failure_criteria": design.get("failure_criteria", ""),
        "dataset_identity": design.get("dataset_identity", ""),
        # The frozen spec itself, in full, so the submitting step reconstructs
        # exactly this and not its own reading of the model's design. An earlier
        # version re-derived it at submission and named it differently, so the
        # digests disagreed and the guard rejected every legitimate experiment.
        "spec": _spec_record(spec),
    }
    ref = context.artifacts.put_text(
        json.dumps(record, indent=2, sort_keys=True),
        media_type="application/json",
        # The digest is in the role so the guard can find this record by
        # equality instead of scanning a window of the project's
        # preregistrations and reading each one. See
        # `sql/0012_preregistration_lookup.sql` and `_preregistration_rows`.
        role=preregistration_role(digest),
        producer=f"{response.provider}:{EXPERIMENTALIST.identity}",
    )
    # Linked here rather than relying on the graph node to do it. The
    # preregistration check looks the artifact up *through* its run link, so a
    # handler that leaves the linking to its caller is a handler whose guard
    # depends on who called it -- which is exactly the kind of thing that works
    # in the pipeline and fails everywhere else.
    context.artifacts.link(
        ref, role=preregistration_role(digest), run_id=state["run_id"]
    )
    return ActionOutcome.succeeded(
        f"preregistered a test of {target} using the declared command {chosen} "
        f"({digest[:12]})",
        data=record,
        artifacts=(ref,),
    )


def _spec_from_resolved(
    resolved: Any,
    design: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    name: str,
) -> ExecutionSpec:
    """Build the frozen spec from a *resolved declared command*.

    ``argv`` comes from the researcher's declaration with validated parameter
    values substituted, never from the model.

    ``cwd`` is the declared ``working_directory`` if the researcher set one, and
    otherwise the project checkout. An earlier version of this docstring claimed
    it was the run directory; it never was, and it cannot be -- ``cwd`` is part
    of the spec digest, and a run directory is named for the job, so the digest
    would change on every submission and the preregistration check would reject
    every experiment. An independent review caught the claim.

    Running in the checkout is where an experiment's code and data are, so it is
    the right default and not a lapse. What it means is that a declared command
    *can* write to the repository, with model-supplied parameter values in its
    argv. That is the same exposure the coding path has, and it gets the same
    treatment: :func:`_submit` fingerprints the canonical capsule and every Git
    ref before and after and refuses on drift. Detection, not prevention.

    Seeds default to one fixed value rather than a random one: an experiment
    whose seed is chosen at submission is an experiment nobody can rerun.
    """

    resources = {
        str(key): str(value)
        for key, value in dict(design.get("resources") or {}).items()
    }
    resources.pop("timeout_seconds", None)
    seeds = tuple(int(seed) for seed in design.get("seeds") or (20260915,))
    return ExecutionSpec(
        name=name,
        argv=tuple(str(token) for token in resolved.argv),
        cwd=str(resolved.working_directory or state["repo_path"]),
        environment={"kind": "uv", "project": str(state["repo_path"])},
        resources=resources,
        env={
            f"RESEARCH_OS_SEED_{index}": str(seed) for index, seed in enumerate(seeds)
        },
        timeout_seconds=int(resolved.timeout_seconds),
        outputs=tuple(str(item) for item in resolved.outputs),
        seeds=seeds,
    )


def _spec_record(spec: ExecutionSpec) -> dict[str, Any]:
    """The frozen spec as plain JSON, for the preregistration record."""

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


def _spec_from_record(record: Mapping[str, Any]) -> ExecutionSpec:
    """Rebuild the exact spec that was preregistered.

    Field for field, with no re-derivation and no defaults: anything missing is
    a record this build did not write, and a spec assembled from a partial
    record would hash differently and be refused -- correctly, but confusingly.
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


#: How many legacy, un-digested preregistrations one lookup will read.
#:
#: Only rows written before ``sql/0012_preregistration_lookup.sql``, which do
#: not carry the digest in their role and can therefore only be found by
#: reading them. It is a horizon and it is documented as one: see that
#: migration for why the digested path has no window at all.
LEGACY_PREREGISTRATION_WINDOW = 500


def preregistration_role(digest: str) -> str:
    """The artifact role a preregistration of this spec is stored under."""

    return f"preregistration:{digest}"


def _preregistration_rows(
    context: CycleContext, *, digest: str, project_id: str
) -> tuple[str, ...]:
    """Artifact ids that may be a preregistration of this spec, newest first.

    Two questions, because there are two generations of row. Records written
    from ``0012`` on carry ``preregistration:<spec digest>`` as their role, so
    the first question is an indexed equality match with no window: every such
    record is found, however many the project has. Records written before that
    carry the bare ``preregistration`` role with the digest only inside the
    document, so the second question is the old bounded scan, and the caller
    still has to read each one to know what it froze.

    Both are scoped to the project by ``artifact_links.project_id``, with a
    left join to the run for links written before ``0015`` added that column. A
    preregistration from another project is not this project's commitment, and
    the earliest version of this lookup did not say so.

    **The scoping used to go through the run, and that made pruning fatal.** It
    was ``join research_runs r on r.run_id = l.run_id``, so deleting a run --
    an ordinary retention action -- removed the only path from the artifact to
    its project, and this returned nothing. The guard then refused the
    experiment permanently, with the preregistration document sitting intact in
    the content-addressed store. A final adversarial review of the previous
    release executed that sequence; ``0015`` is the fix and this is the reader
    that uses it.
    """

    with context.db.tx() as conn:
        exact = conn.execute(
            """
            select distinct a.artifact_id, a.created_at
            from artifacts a
            join artifact_links l on l.artifact_id = a.artifact_id
            left join research_runs r on r.run_id = l.run_id
            where a.role = %(role)s
              and coalesce(l.project_id, r.project_id) = %(project_id)s
            order by a.created_at desc
            """,
            {"role": preregistration_role(digest), "project_id": project_id},
        ).fetchall()
        legacy = conn.execute(
            """
            select distinct a.artifact_id, a.created_at
            from artifacts a
            join artifact_links l on l.artifact_id = a.artifact_id
            left join research_runs r on r.run_id = l.run_id
            where a.role = 'preregistration'
              and coalesce(l.project_id, r.project_id) = %(project_id)s
            order by a.created_at desc
            limit %(window)s
            """,
            {"project_id": project_id, "window": LEGACY_PREREGISTRATION_WINDOW},
        ).fetchall()
    seen: dict[str, None] = {}
    for row in (*exact, *legacy):
        seen.setdefault(str(row["artifact_id"]), None)
    return tuple(seen)


def _preregistration_exists(
    context: CycleContext, digest: str, *, project_id: str
) -> bool:
    """Whether a preregistration artifact with this spec digest was stored.

    Every candidate is still *read* and its ``spec_digest`` compared, including
    the ones the role already names. The role is an index, not evidence: it is
    written by the same code path that writes the document, and a guard that
    trusted it would be trusting a label instead of the record it labels.

    The alternative to any of this is trusting the caller about when a criterion
    was fixed, which is the whole thing the guard exists to establish.
    """

    for artifact_id in _preregistration_rows(
        context, digest=digest, project_id=project_id
    ):
        try:
            record = json.loads(context.artifacts.get_text(artifact_id))
        except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
            # Logged rather than swallowed: an unreadable preregistration would
            # otherwise look identical to "no preregistration matches", which is
            # a refusal the researcher would have no way to explain.
            LOG.warning(
                "could not read preregistration artifact %s: %s", artifact_id, exc
            )
            continue
        if str(record.get("spec_digest") or "") == digest:
            return True
    return False


def _submit(
    state: Mapping[str, Any],
    context: CycleContext,
    plan: Mapping[str, Any],
    *,
    executor_name: str,
) -> ActionOutcome:
    """Submit the frozen spec, recording the job before the executor is told."""

    design = dict(plan.get("parameters", {}).get("design") or {})
    if not design:
        previous = dict(state.get("action_result", {}).get("data") or {})
        design = previous if previous.get("testable") else {}
    if not design:
        return ActionOutcome.failed(
            "no preregistered design to run; design_experiment must come first",
            failure_class=FailureClass.POLICY_REFUSED,
        )

    executor = context.executors.get(executor_name)
    if executor is None:
        return ActionOutcome.failed(
            f"no {executor_name} executor is available on this machine",
            failure_class=FailureClass.SCHEDULER_UNAVAILABLE,
        )

    frozen = design.get("spec")
    if not frozen:
        return ActionOutcome.failed(
            "the design carries no frozen specification; it was not produced by "
            "design_experiment in this build",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    try:
        spec = _spec_from_record(frozen)
    except (KeyError, TypeError, ValueError) as exc:
        return ActionOutcome.failed(
            f"the frozen specification is unreadable: {exc}",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    digest = spec_digest(spec)
    declared = str(design.get("spec_digest") or "")
    if not declared:
        # `if declared and declared != digest` let a design with no digest past
        # the guard entirely, and the design comes from plan parameters under an
        # unconstrained schema -- so the planner supplied both halves of the
        # comparison and omitting one half skipped it. An independent review
        # found this.
        return ActionOutcome.failed(
            "the design declares no spec_digest, so there is nothing to check it "
            "against. Only design_experiment produces a runnable design.",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    if not _preregistration_exists(
        context, digest, project_id=str(state["project_id"])
    ):
        # And the digest must name a preregistration this runtime actually
        # stored. Otherwise the check compares a self-consistent blob against
        # itself, which establishes nothing about when the endpoint was fixed.
        return ActionOutcome.failed(
            f"no stored preregistration matches {digest[:12]}. The endpoint and "
            f"its criteria must be recorded by design_experiment before the "
            f"experiment runs, not supplied alongside it.",
            failure_class=FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
        )
    if declared != digest:
        # The thing about to run is not the thing that was preregistered.
        return ActionOutcome.failed(
            f"the specification changed after preregistration "
            f"({declared[:12]} -> {digest[:12]}). Running a different test under a "
            f"preregistration is not a runtime decision; it needs "
            f"change_preregistration, which a person performs.",
            failure_class=FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
        )

    dimension = (
        Dimension.EXTERNAL_JOBS if executor_name == SLURM else Dimension.WORK_ITEMS
    )
    try:
        grants = context.budgets.reserve_all(
            dimension=dimension,
            amount=1,
            run_id=state["run_id"],
            project_id=state["project_id"],
        )
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )

    key = idempotency_key(f"experiment.submit.{executor_name}", state["run_id"], digest)
    job_id = new_external_job_id()

    def reconcile(_invocation: Any) -> dict[str, Any] | None:
        """Has a job for this exact spec already been submitted?

        Answered from our own ``external_jobs`` table by spec digest, which is
        the only reliable question available: the scheduler cannot be asked
        about a job whose id was never recorded.
        """

        with context.db.tx() as conn:
            row = conn.execute(
                "select job_id, scheduler_job_id, status from external_jobs "
                "where run_id = %s and spec_digest = %s and executor = %s "
                "order by submitted_at limit 1",
                (state["run_id"], digest, executor_name),
            ).fetchone()
        if row is None or not row["scheduler_job_id"]:
            return None
        return {
            "ok": True,
            "detail": f"recovered submission {row['scheduler_job_id']}",
            "data": {
                "job_id": row["job_id"],
                "scheduler_job_id": row["scheduler_job_id"],
                "status": str(row["status"]),
                "spec_digest": digest,
            },
        }

    def perform() -> dict[str, Any]:
        # The same guard the coding path has, for the same reason: a declared
        # command runs in the project checkout with model-supplied parameter
        # values in its argv, and nothing here can stop it writing there. An
        # independent review pointed out that this path had no detector at all,
        # in the module whose whole purpose is that the preregistered thing is
        # what ran.
        from research_os.runtime.actions.coding import (
            _describe_drift,
            canonical_fingerprint,
        )

        repo = Path(str(state["repo_path"]))
        before = canonical_fingerprint(repo)
        run_dir = prepare_run_dir(spec, job_id=job_id)
        # The row first, in SUBMITTING, so a crash in the window between this and
        # the executor returning leaves something to reconcile rather than a job
        # nobody knows about.
        context.store.create_external_job(
            job_id=job_id,
            project_id=state["project_id"],
            run_id=state["run_id"],
            executor=executor_name,
            spec_digest=digest,
            run_dir=str(run_dir),
        )
        handle = executor.submit(spec, run_dir=run_dir)
        status = (
            ExternalJobStatus.COMPLETED
            if handle.finished and handle.exit_code == 0
            else ExternalJobStatus.FAILED
            if handle.finished
            else ExternalJobStatus.SUBMITTED
        )
        context.store.update_external_job(
            job_id,
            status=status,
            scheduler_job_id=handle.scheduler_job_id,
            exit_code=handle.exit_code,
            detail=handle.detail,
        )

        after = canonical_fingerprint(repo)
        if after != before:
            drift = _describe_drift(before, after)
            LOG.error("experiment %s changed the canonical checkout: %s", job_id, drift)
            return {
                "ok": False,
                "detail": (
                    f"the experiment changed the canonical checkout, which it must "
                    f"never do: {drift}. The declared command wrote outside its "
                    f"outputs. The job record and its run directory are left for "
                    f"inspection."
                ),
                "data": {"job_id": job_id, "canonical_drift": drift},
                "_escaped": True,
            }
        return {
            "ok": True,
            "detail": handle.detail or "submitted",
            "data": {
                "job_id": job_id,
                "scheduler_job_id": handle.scheduler_job_id,
                "status": str(status),
                "spec_digest": digest,
                "run_dir": str(run_dir),
                "finished": handle.finished,
                "exit_code": handle.exit_code,
                "primary_endpoint": design.get("primary_endpoint", ""),
                "success_criteria": design.get("success_criteria", ""),
                "failure_criteria": design.get("failure_criteria", ""),
            },
        }

    try:
        outcome = context.ledger.run(
            key=key,
            kind=f"experiment.submit.{executor_name}",
            run_id=state["run_id"],
            request={"spec_digest": digest, "executor": executor_name},
            perform=perform,
            reconcile=reconcile,
        )
    except ContainmentUnavailableError as exc:
        # Terminal, not repairable. No repair makes a kernel offer user
        # namespaces, and `EXECUTOR_FAILED` is `REPAIR`.
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.CAPABILITY_DENIED
        )
    except ExecutorError as exc:
        context.budgets.release_all(grants)
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.EXECUTOR_FAILED
        )
    except ResearchOSError as exc:
        context.budgets.release_all(grants)
        return ActionOutcome.failed(
            f"could not submit: {exc}", failure_class=FailureClass.EXECUTOR_FAILED
        )

    context.budgets.settle_all(grants)
    data = dict(outcome.result.get("data") or {})
    if outcome.result.get("_escaped"):
        # A policy refusal, not an executor failure: it must not be retried,
        # because retrying would run the same escaping command again.
        return ActionOutcome.failed(
            str(outcome.result.get("detail") or "the experiment escaped its scope"),
            failure_class=FailureClass.POLICY_REFUSED,
            data=data,
        )
    artifacts = _collect_outputs(context, spec, data)
    return ActionOutcome.succeeded(
        str(outcome.result.get("detail") or "submitted"),
        data=data,
        artifacts=artifacts,
    )


def _collect_outputs(
    context: CycleContext, spec: ExecutionSpec, data: Mapping[str, Any]
) -> tuple[Any, ...]:
    """Put whatever the run produced into the artifact store, by content hash.

    Only for a run that has finished. A Slurm job that is still queued has no
    outputs yet, and the control plane collects them when it reconciles.
    """

    if not data.get("finished"):
        return ()
    run_dir = Path(str(data.get("run_dir") or ""))
    if not run_dir.is_dir():
        return ()
    refs = []
    for relative in (*spec.outputs, "logs/stdout.txt", "logs/stderr.txt"):
        # Through the containment rule the portfolio's readers use, not
        # `is_file()`. The run directory is bound writable into the sandbox,
        # so the program can leave a link there -- at an output, at its own
        # log, or at `logs/` -- and a reader that followed it stored host
        # content as the run's result, which a finding then cited.
        ref = context.artifacts.put_contained(
            run_dir, relative, role=f"result:{relative}", producer="executor"
        )
        if ref is not None and ref.size_bytes:
            refs.append(ref)
    return tuple(refs)


def run_local_experiment(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    return _submit(state, context, plan, executor_name=LOCAL)


def submit_cluster_experiment(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    return _submit(state, context, plan, executor_name=SLURM)


def _preregistrations_for(
    context: CycleContext, *, digest: str, project_id: str
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Every stored preregistration of this project whose spec digest matches.

    Plural, deliberately. ``spec_digest`` covers the *execution* -- argv, cwd,
    environment, seeds -- and says nothing about the criteria, which live beside
    it in the record. So two design passes over one declared command can store
    two preregistrations with the same digest and different primary endpoints,
    and a function that returned "the" preregistration would have to choose.

    It used to choose the newest, and an adversarial review executed what that
    permits: read an experiment, crash before recording it, design again with a
    different endpoint, and the retry compares the same result against the new
    criteria while reporting ``criteria_were_fixed_before_results: true``. A
    post-hoc primary-endpoint change with no recorded Decision.

    Scoped to the project, and found by the digested role rather than by
    scanning a window of everything the project ever preregistered. See
    :func:`_preregistration_rows`.
    """

    found: list[tuple[str, dict[str, Any]]] = []
    for artifact_id in _preregistration_rows(
        context, digest=digest, project_id=project_id
    ):
        try:
            record = json.loads(context.artifacts.get_text(artifact_id))
        except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
            # Logged rather than swallowed: an unreadable preregistration would
            # otherwise look identical to "no preregistration matches", which is
            # a refusal the researcher would have no way to explain.
            LOG.warning(
                "could not read preregistration artifact %s: %s", artifact_id, exc
            )
            continue
        if str(record.get("spec_digest") or "") != digest:
            continue
        found.append((artifact_id, record))
    return tuple(found)


def _criteria_from(artifact_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The prespecified criteria, as this record stated them."""

    return {
        "primary_endpoint": str(record.get("primary_endpoint", "")),
        "secondary_endpoints": list(record.get("secondary_endpoints", ())),
        "success_criteria": str(record.get("success_criteria", "")),
        "failure_criteria": str(record.get("failure_criteria", "")),
        "dataset_identity": str(record.get("dataset_identity", "")),
        "preregistration_artifact": artifact_id,
    }


def _comparable(criteria: Mapping[str, Any]) -> str:
    """The criteria, canonically, for deciding whether two agree."""

    return json.dumps(
        {
            key: value
            for key, value in criteria.items()
            if key != "preregistration_artifact"
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _preregistered_criteria(
    context: CycleContext,
    *,
    digest: str,
    project_id: str,
    artifact_id: str | None = None,
) -> dict[str, Any] | None:
    """The criteria recorded before this spec ran, or ``None`` if there are none.

    ``artifact_id`` names the preregistration this interpretation was bound to
    when it was claimed. Given one, that artifact's criteria are the answer and
    nothing designed since can change them -- which is the whole point of
    storing it on the row.

    Without one (a first reading), the criteria are looked up by spec digest.
    If two preregistrations share the digest and **disagree** about the
    criteria, this returns ``None`` rather than choosing: "which of these two
    did we commit to" is not a question a runtime may answer by picking, and
    the honest outcome is to refuse and let a person say.
    """

    if artifact_id:
        try:
            record = json.loads(context.artifacts.get_text(artifact_id))
        except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
            LOG.warning(
                "the preregistration this interpretation was bound to (%s) is "
                "unreadable: %s",
                artifact_id,
                exc,
            )
            return None
        return _criteria_from(artifact_id, record)

    found = _preregistrations_for(context, digest=digest, project_id=project_id)
    if not found:
        return None
    candidates = [_criteria_from(item, record) for item, record in found]
    distinct = {_comparable(item) for item in candidates}
    if len(distinct) > 1:
        LOG.error(
            "%s has %d preregistrations sharing spec digest %s with different "
            "criteria (%s); refusing to choose between them",
            project_id,
            len(candidates),
            digest[:12],
            ", ".join(item["preregistration_artifact"][:12] for item in candidates),
        )
        return None
    # Identical criteria, so the oldest is the commitment and the rest are
    # re-statements of it. Oldest, not newest: the earliest record is the one
    # that was made before the result existed.
    return candidates[-1]


def _bound_preregistration(
    context: CycleContext, *, job: Any, project_id: str
) -> str | None:
    """The one preregistration the claim may name, or nothing.

    ``None`` when the spec digest resolves to zero preregistrations or to more
    than one. Naming an arbitrary member of an ambiguous set would be worse
    than naming none: `_preregistered_criteria` refuses an ambiguous set that
    disagrees, and it can only do that if the claim has not already picked a
    winner.
    """

    offered = _preregistrations_for(
        context, digest=job.spec_digest, project_id=project_id
    )
    return offered[-1][0] if len(offered) == 1 else None


def _interpretation_record(
    *,
    job: Any,
    criteria: Mapping[str, Any],
    ran_correctly: bool,
    interpretation_id: str,
) -> dict[str, Any]:
    """The bytes that constitute the interpretation.

    Written to the artifact store before the interpretation is marked complete,
    so a crash in that window leaves an artifact the recovery path can find and
    reconnect rather than a claim with nothing behind it.

    Deliberately contains no verdict sentence of its own. It records what ran,
    what the criteria were, and whether the execution was valid. Whether the
    criteria were *met* is what a person and the reviewer read the numbers for;
    a machine-authored "the hypothesis is supported" in a durable artifact is
    exactly the sentence that gets quoted later as though someone had checked
    it.
    """

    return {
        "interpretation_id": interpretation_id,
        "interpreter_version": INTERPRETER_VERSION,
        "job_id": job.job_id,
        "executor": job.executor,
        "scheduler_job_id": job.scheduler_job_id,
        "spec_digest": job.spec_digest,
        "run_dir": job.run_dir,
        "status": str(job.status),
        "exit_code": job.exit_code,
        "ran_correctly": ran_correctly,
        "criteria_were_fixed_before_results": True,
        "criteria": dict(criteria),
    }


def interpret_results(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Compare the results to the criteria that were fixed before them.

    Deterministic where it can be: which experiment this is about, whether it
    completed, and what the prespecified criteria were. The *reading* of a
    result is scientific judgement and goes through the reviewer and then a
    person; what this does is establish the facts, bind them to one experiment,
    and refuse to let the criteria move.

    Both outcomes are ``ok=True``. An experiment that ran correctly and refuted
    its hypothesis has succeeded.

    Crash-safety, in order, because the order is the property:

    1. resolve *which* job -- explicit ``job_id`` beats selection;
    2. claim ``(job, interpreter version)`` durably, before reading anything;
    3. if that claim already exists and is ``COMPLETED``, return its artifact
       and read nothing again;
    4. retrieve the criteria from the stored preregistration, by spec digest;
    5. write the artifact;
    6. mark the claim complete.

    A process that dies between 5 and 6 leaves an ``IN_PROGRESS`` claim and an
    artifact. The next attempt re-derives the *same* artifact bytes -- the
    inputs are the job row and the stored preregistration, both immutable -- so
    the content-addressed store returns the same id, and step 6 attaches it to
    the existing claim. One logical interpretation, one artifact, whatever the
    process did.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    project_id = str(state["project_id"])
    requested = str(
        plan.get("parameters", {}).get("job_id") or previous.get("job_id") or ""
    )

    claim = None
    created = False
    if requested:
        # An explicitly named job takes precedence over any selection, and a
        # name that does not resolve is an error rather than an invitation to
        # pick something else. Interpreting a different experiment than the one
        # the planner asked about is the failure this whole relation exists to
        # prevent.
        job = context.store.get_external_job(requested)
        if job is None:
            return ActionOutcome.failed(
                f"no such job: {requested}",
                failure_class=FailureClass.ARTIFACT_MISSING,
            )
        if str(job.project_id) != project_id:
            return ActionOutcome.failed(
                f"{requested} belongs to project {job.project_id}, not "
                f"{project_id}; an experiment is never interpreted across a "
                f"project boundary",
                failure_class=FailureClass.POLICY_REFUSED,
            )
    else:
        # Select and claim in one transaction. Two workers advancing the same
        # project used to select the same oldest-eligible job and each do the
        # whole reading; see `RuntimeStore.claim_next_interpretation`.
        taken = context.store.claim_next_interpretation(
            project_id=project_id,
            interpreter_version=INTERPRETER_VERSION,
            run_id=str(state["run_id"]),
            preregistration_for=lambda found: _bound_preregistration(
                context, job=found, project_id=project_id
            ),
        )
        if taken is None:
            return ActionOutcome.succeeded(
                "no finished experiment is waiting to be interpreted",
                data={"interpreted": False},
            )
        job, claim, created = taken

    if job.status not in {
        ExternalJobStatus.COMPLETED,
        ExternalJobStatus.FAILED,
        ExternalJobStatus.TIMED_OUT,
        ExternalJobStatus.CANCELLED,
    }:
        return ActionOutcome.failed(
            f"{job.job_id} is {job.status}; there is nothing to interpret yet",
            failure_class=FailureClass.SCHEDULER_UNAVAILABLE,
        )

    if claim is None:
        # Resolved *before* the claim, so the claim can name it and a replay
        # can never be handed a different set of criteria. See
        # `_preregistered_criteria` and
        # `sql/0010_interpretation_preregistration.sql`.
        claim, created = context.store.claim_interpretation(
            job_id=job.job_id,
            project_id=project_id,
            spec_digest=job.spec_digest,
            interpreter_version=INTERPRETER_VERSION,
            run_id=str(state["run_id"]),
            preregistration_artifact_id=_bound_preregistration(
                context, job=job, project_id=project_id
            ),
        )
    if not created and claim.status is InterpretationStatus.COMPLETED:
        # Already read, by this reader version. Return what was concluded
        # rather than concluding it again: a second reading of one experiment
        # is a second scientific interpretation, and two of them is how a
        # project ends up with a result it can quote twice.
        return ActionOutcome.succeeded(
            f"{job.job_id} was already interpreted as {claim.interpretation_id}",
            data={
                "interpreted": True,
                "reused": True,
                "interpretation_id": claim.interpretation_id,
                "job_id": claim.job_id,
                "spec_digest": claim.spec_digest,
                "interpreter_version": claim.interpreter_version,
                "artifact_id": claim.artifact_id,
                "status": str(job.status),
                "exit_code": job.exit_code,
            },
        )

    ran_correctly = (
        job.status is ExternalJobStatus.COMPLETED and (job.exit_code or 0) == 0
    )
    # The criteria come from the *stored preregistration*, looked up by the
    # job's spec digest -- not from graph state, which is empty on the
    # cross-cycle path this handler is normally reached by. An earlier version
    # read them from state and asserted
    # `criteria_were_fixed_before_results: True` while reporting none of them,
    # which is the one claim in this handler that must never be made loosely.
    criteria = _preregistered_criteria(
        context,
        digest=job.spec_digest,
        project_id=project_id,
        artifact_id=claim.preregistration_artifact_id,
    )
    if criteria is None and previous.get("success_criteria"):
        criteria = {
            "primary_endpoint": str(previous.get("primary_endpoint", "")),
            "secondary_endpoints": list(previous.get("secondary_endpoints", ())),
            "success_criteria": str(previous.get("success_criteria", "")),
            "failure_criteria": str(previous.get("failure_criteria", "")),
        }

    if criteria is None:
        # No preregistration can be found for what ran. That is not an
        # interpretation problem to paper over: without the criteria there is
        # nothing to compare against, and claiming they were fixed beforehand
        # would be asserting exactly what cannot be shown.
        #
        # The claim row is left `IN_PROGRESS` rather than completed. This job is
        # still owed an interpretation -- the missing preregistration is the
        # thing to fix -- and marking it read would hide that permanently.
        return ActionOutcome.failed(
            f"no preregistration is reachable for {job.job_id} (spec "
            f"{job.spec_digest[:12]}), so there are no prespecified criteria to "
            f"compare the result against. The lookup is scoped by "
            f"artifact_links.project_id, which survives run pruning from "
            f"schema 0015 on; a link written before that and whose run has "
            f"since been deleted is unreachable, and that is the one case "
            f"where the document may exist with nothing naming it",
            failure_class=FailureClass.ARTIFACT_MISSING,
            data={
                "interpreted": False,
                "job_id": job.job_id,
                "interpretation_id": claim.interpretation_id,
            },
        )

    record = _interpretation_record(
        job=job,
        criteria=criteria,
        ran_correctly=ran_correctly,
        interpretation_id=claim.interpretation_id,
    )
    ref = context.artifacts.put_text(
        json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False),
        media_type="application/json",
        role=f"interpretation:{job.job_id}",
        producer=INTERPRETER_VERSION,
    )
    completed = context.store.complete_interpretation(
        claim.interpretation_id,
        artifact_id=ref.artifact_id,
        detail=f"{job.status}, exit {job.exit_code}",
    )

    return ActionOutcome.succeeded(
        (
            "the experiment ran; the prespecified criteria decide the result"
            if ran_correctly
            else f"the experiment did not run correctly ({job.status}); no scientific "
            f"conclusion follows from it"
        ),
        data={
            "interpreted": True,
            "reused": False,
            "interpretation_id": completed.interpretation_id,
            "interpreter_version": INTERPRETER_VERSION,
            "artifact_id": completed.artifact_id,
            "job_id": job.job_id,
            "status": str(job.status),
            "exit_code": job.exit_code,
            "ran_correctly": ran_correctly,
            "spec_digest": job.spec_digest,
            # Quoted back unchanged from the preregistration. If a later step
            # wants different criteria, that is change_primary_endpoint, which a
            # person performs.
            **criteria,
            "criteria_were_fixed_before_results": True,
            # The criteria, quoted, beside whether the run that was measured
            # against them was valid.
            #
            # This is the excerpt with the sharpest edge on it, so it is worth
            # being explicit: it carries the *prespecified* endpoint and
            # decision rule and the fact of how the job exited. It does not
            # carry a verdict, because this handler does not reach one -- the
            # reading of a result is the reviewer's and then a person's, and an
            # excerpt that said "SUPPORTED" would be this layer deciding the
            # science it exists to keep separate.
            EXCERPT_KEY: bounded_excerpt(
                [
                    (
                        f"job {job.job_id} finished {job.status} (exit "
                        f"{job.exit_code}); ran_correctly={ran_correctly}"
                    ),
                    f"[primary endpoint] {criteria.get('primary_endpoint', '')}",
                    *(
                        f"[secondary endpoint] {item}"
                        for item in criteria.get("secondary_endpoints", ()) or ()
                    ),
                    (
                        "[success criteria, fixed beforehand] "
                        f"{criteria.get('success_criteria', '')}"
                    ),
                    (
                        "[failure criteria, fixed beforehand] "
                        f"{criteria.get('failure_criteria', '')}"
                    ),
                ],
                limit=MAX_EXCERPT_CHARS,
            ),
        },
        artifacts=(ref,),
    )
