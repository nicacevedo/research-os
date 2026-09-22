"""Where work runs: one contract, a local executor, and a Slurm executor.

A scientific graph node submits an
:class:`~research_os.runtime.interfaces.ExecutionSpec` and never a shell
command. That is the whole point of this module's existence, and
``tests/test_runtime_layering.py`` asserts it by reading the graph package for
``sbatch``.

**Submission never blocks.** ``submit`` freezes the specification, creates an
immutable run directory, writes a manifest, writes a batch script, submits, and
returns with a scheduler job id. The control plane reconciles later. A graph
node that waited for a two-day job would hold a worker, a lease and -- in the
naive version -- a transaction.

**The record precedes the submission.** The ``external_jobs`` row is created in
``SUBMITTING`` *before* ``sbatch`` is invoked, so a crash in that window leaves a
row with no scheduler id for the reconciler to investigate rather than a job
running on a cluster that nothing in this system knows about.

**Slurm's failure states are not one failure.** The v1 layer maps raw states
onto an :class:`ExecutionState`, which collapses ``PREEMPTED`` into
``CANCELLED`` and ``NODE_FAIL`` and ``OUT_OF_MEMORY`` into ``FAILED``. That is
correct for reporting what happened and wrong for deciding what to do next:

- ``PREEMPTED`` and ``NODE_FAIL`` want the same job resubmitted unchanged;
- ``OUT_OF_MEMORY`` and ``TIMEOUT`` want more resources, and resubmitting the
  same allocation just wastes the queue slot again;
- ``CANCELLED`` by a person wants nothing at all.

So this module maps the *raw* state to a
:class:`~research_os.runtime.failures.FailureClass` alongside the
``ExecutionState``, and the retry policy reads the class.

**A non-zero exit is not a scientific verdict.** An experiment that runs to
completion and refutes its hypothesis exits zero and is ``COMPLETED``. Nothing
here inspects results or forms an opinion about them; interpretation is a
separate, preregistered step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from research_os.errors import ResearchOSError
from research_os.experiment.models import ExecutionState
from research_os.paths import data_home
from research_os.runtime.config import RuntimeConfig
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ExecutionHandle, ExecutionSpec
from research_os.runtime.models import ExternalJob, ExternalJobStatus
from research_os.runtime.store import RuntimeStore
from research_os.sandbox import (
    SandboxError,
    SandboxMode,
    SandboxPreparationError,
    SandboxSpec,
    contain,
    process_limit_preexec,
)

LOG = logging.getLogger("research_os.runtime.executors")

LOCAL = "local"
SLURM = "slurm"


class ContainmentUnavailableError(ResearchOSError):
    """Raised when an execution requires containment this host cannot provide.

    Distinct from :class:`ExecutorError` because the responses differ and one of
    them is wrong. `EXECUTOR_FAILED` is `REPAIR`, which puts the work item back
    on the queue; no repair makes a kernel offer user namespaces. This maps to
    `CAPABILITY_DENIED`, which is terminal, which is the honest answer.
    """


class ExecutorError(ResearchOSError):
    """Raised when work cannot be prepared, submitted, or polled."""


def runs_root() -> Path:
    """Where immutable run directories live.

    Under the *data* home rather than the state home: a result is evidence, and
    deleting runtime state must lose no evidence.
    """

    return data_home() / "runs"


def spec_digest(spec: ExecutionSpec) -> str:
    """A stable hash of everything that defines this execution.

    Canonical JSON with sorted keys, so two specifications that differ only in
    dictionary ordering hash alike and two that differ in a seed do not. This
    digest is what makes an experiment identifiable months later, and it is what
    the idempotency key is built from, so it must not include anything
    per-attempt -- there is no timestamp and no run id in it.
    """

    payload = {
        "name": spec.name,
        "argv": list(spec.argv),
        "cwd": spec.cwd,
        "environment": dict(sorted(spec.environment.items())),
        "resources": dict(sorted(spec.resources.items())),
        "env": dict(sorted(spec.env.items())),
        "timeout_seconds": spec.timeout_seconds,
        "outputs": list(spec.outputs),
        "seeds": list(spec.seeds),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def prepare_run_dir(spec: ExecutionSpec, *, job_id: str) -> Path:
    """Create the immutable run directory and write the manifest.

    Immutable by convention and by naming: the directory is named for the job,
    the manifest records the spec digest, and nothing in this runtime rewrites
    either. A reader who finds ``manifest.json`` knows exactly what was run,
    with which seeds, in which environment, without trusting a log.
    """

    target = runs_root() / job_id
    (target / "logs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "job_id": job_id,
        "spec_digest": spec_digest(spec),
        "name": spec.name,
        "argv": list(spec.argv),
        "cwd": spec.cwd,
        "environment": dict(spec.environment),
        "resources": dict(spec.resources),
        "timeout_seconds": spec.timeout_seconds,
        "outputs": list(spec.outputs),
        "seeds": list(spec.seeds),
    }
    _atomic_write(
        target / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return target


def _atomic_write(path: Path, text: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".incoming-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        Path(temporary).unlink(missing_ok=True)
        raise ExecutorError(f"could not write {path}: {exc}") from None


@dataclass(frozen=True, slots=True)
class ToolingPaths:
    """What a contained command needs from outside its workspace.

    Assembled from the argument vector rather than guessed, and empty for
    every program this module has no specific knowledge of. A sandbox that
    inferred which paths a command might want would be a sandbox that
    inferred wrongly in the direction of permitting.
    """

    readable: tuple[Path, ...] = ()
    discarded: tuple[Path, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)


def _tooling_paths(argv: Sequence[str], workdir: Path) -> ToolingPaths:
    """The read-only and throwaway-writable paths this command needs.

    Three reuses, no new policy. ``uv_readonly_paths`` is uv's managed
    interpreters; ``uv_discarded_paths`` is its package cache, layered with a
    tmpfs so nothing written to it survives; ``linked_worktree_paths`` is the
    repository a linked Git worktree points at, which a worktree cannot
    resolve itself without. Each is documented where it is defined, and each
    was measured there rather than reasoned about.
    """

    from research_os.automation.checks import (
        UV_CACHE_DIR,
        UV_OFFLINE,
        UV_PROGRAM,
        uv_cache_dir,
        uv_discarded_paths,
        uv_readonly_paths,
    )
    from research_os.sandbox import linked_worktree_paths

    environment: dict[str, str] = {}
    if argv and argv[0] == UV_PROGRAM:
        cache = uv_cache_dir()
        if cache is not None:
            # Point uv at the cache it is being handed as an overlay. Without
            # this it uses the sandbox's own tmpfs HOME, which is empty, and
            # an empty cache with no network installs nothing.
            environment[UV_CACHE_DIR] = str(cache)
        # Told there is no network, uv resolves from the cache. Not told, it
        # attempts the index, retries three times, and fails the run with a
        # DNS diagnosis of a containment decision.
        environment[UV_OFFLINE] = "1"
    return ToolingPaths(
        readable=(*uv_readonly_paths(argv), *linked_worktree_paths(workdir)),
        discarded=uv_discarded_paths(argv),
        environment=environment,
    )


# ------------------------------------------------------------------- local --
@dataclass(slots=True)
class LocalExecutor:
    """Runs a specification on this machine, now.

    Synchronous, because there is nothing to gain from deferring a local run
    that the caller is already inside a leased work item for. The handle comes
    back ``finished``, so the control plane has nothing to reconcile.
    """

    name: str = LOCAL
    timeout_grace_seconds: int = 30
    sandbox_mode: SandboxMode = SandboxMode.PREFERRED
    """How much containment a local experiment requires.

    A declared command is the *researcher's* argv, not a model's -- the
    experimentalist selects from ``experiments.yaml`` and supplies validated
    parameter values -- so this is a weaker exposure than the coding pipeline's.
    It is still project code executing in the project checkout with model-chosen
    parameters, and it is still worth containing where the host can, so the
    default matches the configured default rather than being off.

    The runtime raises it to ``required`` at high autonomy, where nobody is
    watching.
    """

    def submit(self, spec: ExecutionSpec, *, run_dir: Path) -> ExecutionHandle:
        digest = spec_digest(spec)
        if shutil.which(spec.argv[0]) is None:
            return ExecutionHandle(
                executor=self.name,
                run_dir=str(run_dir),
                spec_digest=digest,
                finished=True,
                exit_code=None,
                detail=f"{spec.argv[0]} is not on PATH",
            )
        env = {**os.environ, **spec.env}
        stdout_path = run_dir / "logs" / "stdout.txt"
        stderr_path = run_dir / "logs" / "stderr.txt"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            workdir = Path(spec.cwd)
            tooling = _tooling_paths(spec.argv, workdir)
            sandbox_spec = SandboxSpec(
                workdir=workdir,
                # The frozen run directory is where declared outputs are
                # collected from, so the command has to be able to write it.
                writable=(run_dir,),
                # What a contained `uv run` needs to read -- its managed
                # interpreters -- and, when the workdir is a linked Git
                # worktree, the repository directory that worktree cannot
                # resolve itself without. Both read-only.
                #
                # This was missing, and the omission had a shape: the coding
                # pipeline's acceptance commands solved it in
                # `automation.checks` and this executor did not, so a
                # *declared experiment* whose command is `uv run` -- which is
                # what both of this machine's real declarations are -- failed
                # under containment with a DNS error attributed to the
                # project. The fix is the same three helpers, reused.
                readable=tooling.readable,
                # uv's package cache, exposed with every write thrown away.
                # Without it a contained `uv run` has no network and a cold
                # cache, which cannot install a dependency at all.
                discarded=tooling.discarded,
                # The two things inside the checkout a declared experiment must
                # not be able to write, whatever else it may.
                #
                # The workdir *is* the project checkout -- that is where an
                # experiment's code and data are, so it has to be writable --
                # and an adversarial review showed what that granted: `.git/
                # hooks` and `.research/` were writable inside "containment",
                # a `post-checkout` hook produced zero drift from
                # `canonical_fingerprint`, and the next `git worktree add` ran
                # it on the host.
                protected=(workdir / ".git", workdir / ".research"),
                network=False,
                wall_seconds=spec.timeout_seconds,
                # Only the seeds the spec froze, plus the tool settings the
                # *controller* decided -- where uv's cache is and that there
                # is no network. Not `os.environ`: an experiment that reads a
                # provider key from the environment is an experiment whose
                # result depends on something that is not in its provenance.
                environment={**spec.env, **tooling.environment},
            )
            prepared = contain(spec.argv, spec=sandbox_spec, mode=self.sandbox_mode)
        except SandboxPreparationError as exc:
            # Containment exists and *this* command could not be set up in it.
            # Not `ContainmentUnavailableError`, which means the host cannot
            # contain anything and no repair can change that: this names a
            # missing interpreter or a `#!` line pointing outside policy, which
            # is a fact about the declared command and belongs on the handle
            # beside "not on PATH".
            return ExecutionHandle(
                executor=self.name,
                run_dir=str(run_dir),
                spec_digest=digest,
                finished=True,
                exit_code=None,
                detail=f"the sandbox could not be prepared for {spec.name}: {exc}",
            )
        except SandboxError as exc:
            # A refusal, not an executor malfunction. `EXECUTOR_FAILED` is
            # `REPAIR`, so classifying it that way put the work item back on the
            # queue and invited a repair worker to react to "this host cannot
            # provide containment" -- which no repair can change. The coding
            # path already raises this distinction; an adversarial review found
            # that this one did not.
            raise ContainmentUnavailableError(str(exc)) from None
        try:
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                completed = subprocess.run(
                    list(prepared.argv),
                    cwd=spec.cwd,
                    env=prepared.environment if prepared.contained else env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    timeout=spec.timeout_seconds,
                    check=False,
                    # A session of its own, so the command has no controlling
                    # terminal and cannot write to the researcher's. The
                    # contained path gets this from `--new-session`; the
                    # uncontained path -- the live one on a host with no
                    # containment -- had nothing, and an adversarial review
                    # executed `open("/dev/tty","w")` from an acceptance
                    # command and repainted the terminal.
                    start_new_session=True,
                    # Only when contained. See `process_limit_preexec`:
                    # `RLIMIT_NPROC` counts the researcher's whole session
                    # outside a user namespace, so setting it there stops the
                    # child creating its first thread.
                    preexec_fn=process_limit_preexec(
                        sandbox_spec, contained=prepared.contained
                    ),
                )
        except subprocess.TimeoutExpired:
            return ExecutionHandle(
                executor=self.name,
                run_dir=str(run_dir),
                spec_digest=digest,
                finished=True,
                exit_code=None,
                detail=f"timed out after {spec.timeout_seconds}s",
                # Recorded here too. It defaulted to False, so a run that *was*
                # contained and then timed out was recorded as uncontained --
                # understating rather than overstating, but still wrong.
                contained=prepared.contained,
                containment=f"{prepared.technology}: {prepared.detail}",
            )
        except OSError as exc:
            raise ExecutorError(f"could not run {spec.name}: {exc}") from None
        return ExecutionHandle(
            executor=self.name,
            run_dir=str(run_dir),
            spec_digest=digest,
            finished=True,
            exit_code=completed.returncode,
            detail="completed" if completed.returncode == 0 else "non-zero exit",
            contained=prepared.contained,
            containment=f"{prepared.technology}: {prepared.detail}",
        )

    def poll(self, handle: ExecutionHandle) -> ExecutionHandle:
        return handle

    def cancel(self, handle: ExecutionHandle) -> None:
        return None


# ------------------------------------------------------------------- slurm --
#: Raw Slurm states that mean "this can be resubmitted unchanged".
RETRY_UNCHANGED = {"PREEMPTED", "NODE_FAIL", "BOOT_FAIL", "REVOKED", "REQUEUED"}
#: Raw Slurm states that mean "the same allocation will fail the same way".
NEEDS_MORE_RESOURCES = {"OUT_OF_MEMORY", "OOM", "TIMEOUT", "DEADLINE"}


def classify_slurm_state(raw: str) -> FailureClass | None:
    """Map a raw Slurm state to a failure class, or ``None`` if nothing failed.

    ``None`` for ``COMPLETED``, and for the states that are still in progress.
    ``None`` also for a job a person cancelled: that is not a malfunction and
    must not be retried.

    Never returns a class for a completed job, whatever the science said. A
    refuted hypothesis exits zero.
    """

    state = (raw or "").strip().upper().split()[0] if raw and raw.strip() else ""
    if state in {
        "COMPLETED",
        "PENDING",
        "CONFIGURING",
        "RUNNING",
        "COMPLETING",
        "RESIZING",
        "SUSPENDED",
    }:
        return None
    if state == "CANCELLED":
        return None
    if state in NEEDS_MORE_RESOURCES:
        return (
            FailureClass.SLURM_OUT_OF_MEMORY
            if state in {"OUT_OF_MEMORY", "OOM"}
            else FailureClass.SLURM_TIMEOUT
        )
    if state in RETRY_UNCHANGED:
        return (
            FailureClass.SLURM_PREEMPTED
            if state in {"PREEMPTED", "REVOKED", "REQUEUED"}
            else FailureClass.SLURM_NODE_FAILURE
        )
    if state == "FAILED":
        return FailureClass.EXECUTOR_FAILED
    # An unrecognised state is not a failure. The v1 layer's rule, kept here for
    # the same reason: a state this build has not seen is one it must not form an
    # opinion about, and calling it a failure would abandon a job that may still
    # be running. It stays UNKNOWN and keeps being polled.
    return None


#: Raw state -> operational job status. Distinct from the v1 ``ExecutionState``
#: mapping, which is about reporting; this is about what the queue does next.
_JOB_STATUS: dict[str, ExternalJobStatus] = {
    "PENDING": ExternalJobStatus.PENDING,
    "CONFIGURING": ExternalJobStatus.PENDING,
    "REQUEUED": ExternalJobStatus.PENDING,
    "RUNNING": ExternalJobStatus.RUNNING,
    "COMPLETING": ExternalJobStatus.RUNNING,
    "RESIZING": ExternalJobStatus.RUNNING,
    "SUSPENDED": ExternalJobStatus.RUNNING,
    "COMPLETED": ExternalJobStatus.COMPLETED,
    "FAILED": ExternalJobStatus.FAILED,
    "BOOT_FAIL": ExternalJobStatus.FAILED,
    "NODE_FAIL": ExternalJobStatus.FAILED,
    "OUT_OF_MEMORY": ExternalJobStatus.FAILED,
    "DEADLINE": ExternalJobStatus.FAILED,
    "CANCELLED": ExternalJobStatus.CANCELLED,
    "PREEMPTED": ExternalJobStatus.CANCELLED,
    "REVOKED": ExternalJobStatus.CANCELLED,
    "TIMEOUT": ExternalJobStatus.TIMED_OUT,
}


def job_status_for(raw: str) -> ExternalJobStatus:
    """Map a raw state to an operational status, defaulting to UNKNOWN.

    Unlisted states become ``UNKNOWN`` rather than ``FAILED``, following the v1
    layer's rule: a state this build has not seen is a state it must not form an
    opinion about, and calling it a failure would retry or abandon a job that may
    still be running.
    """

    state = (raw or "").strip().upper().split()[0] if raw and raw.strip() else ""
    return _JOB_STATUS.get(state, ExternalJobStatus.UNKNOWN)


@dataclass(slots=True)
class SlurmExecutor:
    """Submits to Slurm and returns immediately.

    Wraps the v1 :class:`research_os.experiment.slurm.SlurmExecutor` rather than
    reimplementing ``sbatch``/``squeue``/``sacct`` parsing, which is already
    tested in detail. What is added here is the run directory, the frozen
    manifest, the spec digest, and the failure classification the retry policy
    needs.
    """

    name: str = SLURM
    settings: object | None = None
    sandbox_mode: SandboxMode = SandboxMode.PREFERRED
    """What containment the caller demanded, so this executor can refuse.

    Nothing in this process can contain a process on another machine.
    :func:`research_os.sandbox.contain` wraps an argv this host is about to
    execute; a batch script runs on a compute node whose kernel, namespace
    permissions and installed tooling are not visible from here, and probing
    them would mean submitting a job to find out.

    So the mode is not *implemented* here, it is *honoured by refusal*. An
    adversarial review pointed out that the alternative was worse than a gap:
    with ``required`` in force for local work, writing ``executor: slurm`` in a
    plan was a one-word route around it, and the resulting job was recorded with
    no containment field at all. At ``required`` this executor raises
    :class:`ContainmentUnavailableError`, which is
    ``WAITING_FOR_EXTERNAL_DEPENDENCY`` rather than a repairable failure,
    because the missing thing really is external: cluster-side containment that
    Research OS does not configure. See ``docs/RUNTIME.md``.
    """

    def _inner(self) -> object:
        from research_os.experiment.config import SlurmSettings
        from research_os.experiment.slurm import SlurmExecutor as V1Slurm

        settings = self.settings or SlurmSettings(enabled=False)
        return V1Slurm(settings=settings)

    def build_script(self, spec: ExecutionSpec, *, run_dir: Path, job_name: str) -> str:
        """Write the batch script, from the frozen spec only.

        Assembled here rather than by the v1 builder because that one takes a
        ``ResolvedCommand`` from the experiment config, and the runtime's input
        is an ``ExecutionSpec``. The directives are the same and the ordering is
        the same; the resources come from ``spec.resources``, which the graph
        filled from a preregistered specification.
        """

        resources = dict(spec.resources)
        lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={job_name}",
            # sbatch's default is `--export=ALL`: the submitting process's whole
            # environment is copied into the job. The submitting process is the
            # runtime daemon, whose environment holds provider API keys, the
            # PostgreSQL DSN and whatever else the operator exported -- and the
            # job's stdout, its `env` if it ever prints one, and any node-shared
            # diagnostic then carry them. An adversarial review found this;
            # nothing in the runtime ever asked for that propagation, and the
            # spec's own `env` (exported explicitly below) is the complete set of
            # values an experiment is entitled to.
            "#SBATCH --export=NONE",
        ]
        for directive, key in (
            ("partition", "partition"),
            ("time", "time_limit"),
            ("account", "account"),
            ("cpus-per-task", "cpus"),
            ("mem", "memory"),
            ("gres", "gres"),
        ):
            value = resources.get(key)
            if value:
                lines.append(f"#SBATCH --{directive}={value}")
        lines.extend(
            [
                f"#SBATCH --output={run_dir / 'logs' / 'slurm-stdout.txt'}",
                f"#SBATCH --error={run_dir / 'logs' / 'slurm-stderr.txt'}",
                f"#SBATCH --chdir={spec.cwd}",
                "",
                "set -euo pipefail",
                "# Generated by Research OS from a frozen ExecutionSpec.",
                f"# spec digest: {spec_digest(spec)}",
                "",
            ]
        )
        # With `--export=NONE` the only environment the payload gets is what
        # these lines put there, which is the frozen spec and nothing else.
        for key, value in sorted(spec.env.items()):
            lines.append(f"export {key}={shlex.quote(value)}")
        lines.append(" ".join(shlex.quote(token) for token in spec.argv))
        return "\n".join(lines) + "\n"

    def submit(self, spec: ExecutionSpec, *, run_dir: Path) -> ExecutionHandle:
        if self.sandbox_mode is SandboxMode.REQUIRED:
            raise ContainmentUnavailableError(
                "containment is required and this build cannot contain a Slurm "
                "job: the payload runs on a compute node, and Research OS "
                "neither configures nor probes containment there. Run this "
                "experiment locally, or configure cluster-side containment and "
                "record it in the project's experiments.yaml command"
            )
        digest = spec_digest(spec)
        job_name = f"ros-{spec.name}"[:64]
        script_path = run_dir / "batch.sh"
        _atomic_write(
            script_path, self.build_script(spec, run_dir=run_dir, job_name=job_name)
        )
        partition = str(dict(spec.resources).get("partition") or "")
        inner = self._inner()
        record = inner.submit(script_path, partition=partition)  # type: ignore[attr-defined]
        return ExecutionHandle(
            executor=self.name,
            run_dir=str(run_dir),
            spec_digest=digest,
            scheduler_job_id=str(record.job_id),
            finished=False,
            detail=f"submitted to {partition or 'the default partition'}",
        )

    def poll(self, handle: ExecutionHandle) -> ExecutionHandle:
        if not handle.scheduler_job_id:
            return handle
        inner = self._inner()
        state, record, _usage = inner.poll(handle.scheduler_job_id)  # type: ignore[attr-defined]
        raw = getattr(record, "raw_state", "") or str(state)
        return ExecutionHandle(
            executor=self.name,
            run_dir=handle.run_dir,
            spec_digest=handle.spec_digest,
            scheduler_job_id=handle.scheduler_job_id,
            finished=state
            in {
                ExecutionState.COMPLETED,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
                ExecutionState.TIMED_OUT,
            },
            exit_code=getattr(record, "exit_code", None),
            detail=raw,
        )

    def cancel(self, handle: ExecutionHandle) -> None:
        if handle.scheduler_job_id:
            self._inner().cancel(handle.scheduler_job_id)  # type: ignore[attr-defined]


# ------------------------------------------------------------ reconciling --
def build_executors(
    config: RuntimeConfig,
    *,
    project_id: str | None = None,
    autonomy: str | None = None,
) -> dict[str, object]:
    """Whatever this machine can actually run work on.

    Slurm is included only when the project's ``experiments.yaml`` enables it
    *and* the probe finds the commands. An executor that is configured but
    unreachable is left out, so a graph that asks for it is refused explicitly
    rather than discovering it at submission.
    """

    from research_os.experiment.config import load_config as load_experiment_config
    from research_os.experiment.slurm import probe

    # High autonomy means nobody is watching, so a local experiment runs
    # contained or does not run. Lower settings use the researcher's configured
    # mode, because there a person is at the keyboard.
    mode = SandboxMode.REQUIRED if autonomy == "high" else _configured_sandbox_mode()
    executors: dict[str, object] = {LOCAL: LocalExecutor(sandbox_mode=mode)}
    # The same mode reaches the Slurm executor, which honours it by refusing.
    # Passing only the local one is how `required` became optional.
    try:
        experiment_config = load_experiment_config()
    except ResearchOSError as exc:
        LOG.debug("no experiment configuration: %s", exc)
        return executors
    settings = experiment_config.slurm_for(project_id)
    if probe(settings).available:
        executors[SLURM] = SlurmExecutor(settings=settings, sandbox_mode=mode)
    return executors


def _configured_sandbox_mode() -> SandboxMode:
    """The researcher's ``sandbox.mode``, or the safe default if unreadable.

    ``preferred`` on failure rather than ``off``: a config file that cannot be
    read is not permission to run model-adjacent code with the researcher's
    environment.
    """

    from research_os.automation.config import load_config as load_automation_config

    try:
        return load_automation_config().sandbox.mode
    except ResearchOSError as exc:
        LOG.debug("could not read the sandbox configuration: %s", exc)
        return SandboxMode.PREFERRED


def poll_job(
    job: ExternalJob, *, store: RuntimeStore, config: RuntimeConfig
) -> ExternalJob:
    """Ask the scheduler what became of one job, and record it.

    A ``SUBMITTING`` row with no scheduler id is the crash window: the record
    was written and the submission never returned. It is marked ``UNKNOWN``
    rather than retried, because the submission may well have succeeded and
    resubmitting would run the experiment twice.
    """

    if job.executor != SLURM:
        return job
    if not job.scheduler_job_id:
        return store.update_external_job(
            job.job_id,
            status=ExternalJobStatus.UNKNOWN,
            polled=True,
            detail=(
                "submitted without a recorded scheduler id: the submission may or may "
                "not have reached the scheduler, so it will not be resubmitted"
            ),
            failure_class=str(FailureClass.WORKER_CRASH),
        )

    executors = build_executors(config, project_id=job.project_id)
    executor = executors.get(SLURM)
    if executor is None:
        # Not a job failure. The scheduler is unreachable from here today.
        return store.update_external_job(
            job.job_id,
            status=job.status,
            polled=True,
            detail="the scheduler could not be reached from this host",
        )

    handle = ExecutionHandle(
        executor=SLURM,
        run_dir=job.run_dir,
        spec_digest=job.spec_digest,
        scheduler_job_id=job.scheduler_job_id,
    )
    polled = executor.poll(handle)  # type: ignore[attr-defined]
    failure_class = classify_slurm_state(polled.detail)
    return store.update_external_job(
        job.job_id,
        status=job_status_for(polled.detail),
        exit_code=polled.exit_code,
        detail=polled.detail,
        failure_class=str(failure_class) if failure_class else None,
        polled=True,
    )
