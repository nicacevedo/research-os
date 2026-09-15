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
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.budgets import BudgetExhaustedError, Dimension
from research_os.runtime.context import CycleContext
from research_os.runtime.executors import (
    LOCAL,
    SLURM,
    ExecutorError,
    prepare_run_dir,
    spec_digest,
)
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.ids import new_external_job_id
from research_os.runtime.interfaces import ExecutionSpec, ModelRequest
from research_os.runtime.models import ExternalJobStatus
from research_os.runtime.prompts import EXPERIMENTALIST
from research_os.runtime.routing import RoutingError

LOG = logging.getLogger("research_os.runtime.actions.experiments")


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
        f"{name}: {spec.description or '(no description)'} "
        f"[parameters: {', '.join(p.name for p in spec.parameters) or 'none'}]"
        for name, spec in sorted(available.items())
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
    except (BudgetExhaustedError, RoutingError) as exc:
        return ActionOutcome.failed(
            f"the experimentalist did not run: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
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
        role="preregistration",
        producer=f"{response.provider}:{EXPERIMENTALIST.identity}",
    )
    # Linked here rather than relying on the graph node to do it. The
    # preregistration check looks the artifact up *through* its run link, so a
    # handler that leaves the linking to its caller is a handler whose guard
    # depends on who called it -- which is exactly the kind of thing that works
    # in the pipeline and fails everywhere else.
    context.artifacts.link(ref, role="preregistration", run_id=state["run_id"])
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


def _preregistration_exists(
    context: CycleContext, digest: str, *, project_id: str
) -> bool:
    """Whether a preregistration artifact with this spec digest was stored.

    The artifact is content-addressed, so its id is the hash of the
    preregistration *record*, not of the spec. So the lookup reads this
    *project's* ``role='preregistration'`` artifacts and compares each
    ``spec_digest``. Scoped to the project and bounded at 500: an earlier
    version scanned globally with a limit of 200, which fails closed -- a
    busy machine would silently refuse a legitimate submission.

    The alternative to any of this is trusting the caller about when a criterion
    was fixed, which is the whole thing the guard exists to establish.
    """

    with context.db.tx() as conn:
        rows = conn.execute(
            """
            select distinct a.artifact_id, a.created_at
            from artifacts a
            join artifact_links l on l.artifact_id = a.artifact_id
            join research_runs r on r.run_id = l.run_id
            where a.role = 'preregistration' and r.project_id = %s
            order by a.created_at desc
            limit 500
            """,
            (project_id,),
        ).fetchall()
    for row in rows:
        try:
            record = json.loads(context.artifacts.get_text(str(row["artifact_id"])))
        except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
            # Logged rather than swallowed: an unreadable preregistration would
            # otherwise look identical to "no preregistration matches", which is
            # a refusal the researcher would have no way to explain.
            LOG.warning(
                "could not read preregistration artifact %s: %s",
                row["artifact_id"],
                exc,
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
        candidate = run_dir / relative
        if candidate.is_file() and candidate.stat().st_size:
            refs.append(
                context.artifacts.put_file(
                    candidate, role=f"result:{relative}", producer="executor"
                )
            )
    return tuple(refs)


def run_local_experiment(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    return _submit(state, context, plan, executor_name=LOCAL)


def submit_cluster_experiment(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    return _submit(state, context, plan, executor_name=SLURM)


def _preregistered_criteria(
    context: CycleContext, *, digest: str, project_id: str
) -> dict[str, Any] | None:
    """The criteria recorded before this spec ran, or ``None`` if there are none.

    Looked up by spec digest through the same stored preregistrations the
    submission guard consults, so "these were the criteria" is a claim about a
    stored artifact rather than about whatever happens to be in memory.
    """

    with context.db.tx() as conn:
        rows = conn.execute(
            """
            select distinct a.artifact_id, a.created_at
            from artifacts a
            join artifact_links l on l.artifact_id = a.artifact_id
            join research_runs r on r.run_id = l.run_id
            where a.role = 'preregistration' and r.project_id = %s
            order by a.created_at desc
            limit 500
            """,
            (project_id,),
        ).fetchall()
    for row in rows:
        try:
            record = json.loads(context.artifacts.get_text(str(row["artifact_id"])))
        except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
            LOG.warning(
                "could not read preregistration artifact %s: %s",
                row["artifact_id"],
                exc,
            )
            continue
        if str(record.get("spec_digest") or "") != digest:
            continue
        return {
            "primary_endpoint": str(record.get("primary_endpoint", "")),
            "secondary_endpoints": list(record.get("secondary_endpoints", ())),
            "success_criteria": str(record.get("success_criteria", "")),
            "failure_criteria": str(record.get("failure_criteria", "")),
            "dataset_identity": str(record.get("dataset_identity", "")),
            "preregistration_artifact": str(row["artifact_id"]),
        }
    return None


def _latest_finished_job(context: CycleContext, *, project_id: str) -> Any | None:
    """The most recently finished job for this project, if any.

    Deliberately simple: "finished and most recent". A per-job interpreted flag
    would be better bookkeeping and is the obvious next step; what matters here
    is that the handler has a reachable input at all.
    """

    with context.db.tx() as conn:
        row = conn.execute(
            """
            select job_id from external_jobs
            where project_id = %s
              and status in ('COMPLETED','FAILED','TIMED_OUT','CANCELLED')
            order by finished_at desc nulls last
            limit 1
            """,
            (project_id,),
        ).fetchone()
    return context.store.get_external_job(str(row["job_id"])) if row else None


def interpret_results(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Compare the results to the criteria that were fixed before them.

    Deterministic where it can be: whether the job completed, whether the
    declared outputs exist, and what the prespecified criteria were. The
    *reading* of a result is scientific judgement and goes through the reviewer
    and then a person; what this does is establish the facts and refuse to let
    the criteria move.

    Both outcomes are ``ok=True``. An experiment that ran correctly and refuted
    its hypothesis has succeeded.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    job_id = str(
        plan.get("parameters", {}).get("job_id") or previous.get("job_id") or ""
    )
    if not job_id:
        # Graph state does not cross a cycle boundary -- a successor is a new
        # thread seeded only with identity -- and an experiment is almost always
        # submitted in one cycle and finished by the time of the next. So the
        # durable record answers "which job is there to interpret". Without
        # this the handler was unreachable, and an independent review found that
        # the pipeline could therefore write up results it had never compared
        # against the criteria.
        job = _latest_finished_job(context, project_id=str(state["project_id"]))
        if job is None:
            return ActionOutcome.succeeded(
                "no finished experiment is waiting to be interpreted",
                data={"interpreted": False},
            )
    else:
        job = context.store.get_external_job(job_id)
        if job is None:
            return ActionOutcome.failed(
                f"no such job: {job_id}", failure_class=FailureClass.ARTIFACT_MISSING
            )
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
        context, digest=job.spec_digest, project_id=str(state["project_id"])
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
        return ActionOutcome.failed(
            f"no preregistration found for {job.job_id} (spec "
            f"{job.spec_digest[:12]}), so there are no prespecified criteria to "
            f"compare the result against",
            failure_class=FailureClass.ARTIFACT_MISSING,
            data={"interpreted": False, "job_id": job.job_id},
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
        },
    )
