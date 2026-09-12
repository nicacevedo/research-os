"""The deterministic path from a declared command to a candidate evidence packet.

No model is involved at any point. Which command runs is a name the researcher
declared; what its parameters may be is a type the researcher declared; whether
it may run at all is an authorisation the researcher gave. The controller
resolves, authorises, executes, ingests, checks, and archives -- all of it
ordinary Python, all of it reproducible.

Three gates stand before anything runs, in this order, because each is cheaper
and more certain than the next:

1. **Is this command declared?** A command nobody declared cannot be resolved,
   so there is nothing to authorise.
2. **Is this run allowed to spend?** Execution requires an explicit
   authorisation by default, and a run has a bounded number of local executions
   and cluster submissions. A literature question and a cluster job must not be
   able to look the same from the outside.
3. **Is the checkout sound?** An experiment runs in an isolated worktree, and a
   worktree containing a symlink that leaves it is refused before a process
   starts -- otherwise a write to a declared output path could land outside it
   and Git would show nothing.

What comes out is a candidate packet. It is never Evidence, never accepted, and
never a verdict about a hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.filescope import assert_contained_symlinks
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import utc_now
from research_os.errors import (
    ExperimentAuthorizationError,
    ExperimentConfigError,
    SchedulerUnavailableError,
)
from research_os.experiment.config import (
    ExecutionLimits,
    ExperimentConfig,
    default_config,
)
from research_os.experiment.ingest import (
    build_evidence_packet,
    collect_artifacts,
    discover_changed_paths,
    run_checks,
    snapshot_mtimes,
)
from research_os.experiment.local import run_locally
from research_os.experiment.models import (
    EvidencePacket,
    ExecutionState,
    ExecutorKind,
    ExperimentRun,
)
from research_os.experiment.slurm import SlurmExecutor, probe
from research_os.experiment.spec import ResolvedCommand, resolve_command
from research_os.experiment.store import ExperimentStore, make_experiment_run_id

#: How an execution was authorised, recorded on every run.
#:
#: Recorded rather than assumed, because "who said this could run" is the first
#: question about an experiment that cost something.
AUTHORIZED_EXPLICIT = "explicit --execute"
AUTHORIZED_CONFIGURED = "configured: require_explicit_execute is false"


@dataclass
class ExperimentBudget:
    """What one research run has already spent on experiments.

    Passed in and mutated by the controller, so a single research run's whole
    experimental spend is bounded even when it dispatches several executions.
    """

    local_runs: int = 0
    submissions: int = 0

    def record(self, executor: ExecutorKind) -> None:
        if executor is ExecutorKind.LOCAL:
            self.local_runs += 1
        else:
            self.submissions += 1


@dataclass
class ExperimentController:
    """Runs declared experiments and turns their results into candidate packets."""

    config: ExperimentConfig = field(default_factory=default_config)
    budget: ExperimentBudget = field(default_factory=ExperimentBudget)

    # -- planning --------------------------------------------------------

    def resolve(
        self,
        *,
        project_id: str | None,
        task_name: str,
        parameters: dict[str, object] | None = None,
        worktree: Path | None = None,
    ) -> ResolvedCommand:
        """Return the argv for one declared command with its parameters filled in."""

        spec = self.config.command(project_id, task_name)
        return resolve_command(spec, parameters, worktree=worktree)

    def preview(
        self,
        *,
        project_id: str | None,
        task_name: str,
        parameters: dict[str, object] | None = None,
        worktree: Path | None = None,
    ) -> ResolvedCommand:
        """Return what would run, without running it and without a run record.

        Separate from :meth:`run` rather than a flag on it. A dry run that
        created a run directory and an evidence packet would put a record of an
        execution that never happened next to records of ones that did.
        """

        return self.resolve(
            project_id=project_id,
            task_name=task_name,
            parameters=parameters,
            worktree=worktree,
        )

    def authorize(
        self,
        *,
        project_id: str | None,
        command: ResolvedCommand,
        execute: bool,
        partition: str | None = None,
    ) -> str:
        """Return how this execution is authorised, or refuse it.

        The refusals are deliberately separate messages. "You did not say
        --execute", "this run has already submitted four jobs", and "that
        partition is not in your allowlist" are three different things for a
        researcher to do something about.
        """

        limits = self.config.limits_for(project_id)
        if not execute and limits.require_explicit_execute:
            raise ExperimentAuthorizationError(
                f"running {command.name!r} would execute "
                f"{command.display!r}, which costs real time and possibly real "
                "money. Pass --execute to authorise it, or set "
                "limits.require_explicit_execute to false in experiments.yaml if "
                "you want this project's experiments to run without asking."
            )
        self._assert_within_limits(command, limits)
        if command.executor is not ExecutorKind.LOCAL:
            self._assert_scheduler_allows(project_id, partition)
        return AUTHORIZED_EXPLICIT if execute else AUTHORIZED_CONFIGURED

    def _assert_within_limits(
        self, command: ResolvedCommand, limits: ExecutionLimits
    ) -> None:
        if command.timeout_seconds > limits.max_wall_clock_seconds:
            raise ExperimentAuthorizationError(
                f"{command.name!r} asks for {command.timeout_seconds}s of wall "
                f"clock and this project's limit is {limits.max_wall_clock_seconds}s"
            )
        if command.executor is ExecutorKind.LOCAL:
            if self.budget.local_runs >= limits.max_local_runs_per_run:
                raise ExperimentAuthorizationError(
                    f"this run has already executed {self.budget.local_runs} "
                    f"experiment(s), which is its limit of "
                    f"{limits.max_local_runs_per_run}"
                )
        elif self.budget.submissions >= limits.max_submissions_per_run:
            raise ExperimentAuthorizationError(
                f"this run has already submitted {self.budget.submissions} job(s), "
                f"which is its limit of {limits.max_submissions_per_run}. A "
                "bounded number of cluster submissions is the point of the limit."
            )

    def _assert_scheduler_allows(
        self, project_id: str | None, partition: str | None
    ) -> None:
        settings = self.config.slurm_for(project_id)
        found = probe(settings)
        if not found.available:
            raise SchedulerUnavailableError(
                f"this experiment asks for the Slurm executor, but {found.detail}"
            )
        chosen = partition or settings.default_partition
        if chosen is None:
            raise ExperimentConfigError(
                "no Slurm partition is configured for this project. List the "
                "partitions you may submit to in experiments.yaml, most "
                "preferred first; the first one becomes the default."
            )
        if not settings.allows(chosen):
            raise ExperimentAuthorizationError(
                f"partition {chosen!r} is not in this project's allowed "
                f"partitions ({', '.join(settings.partitions)}). Add it to "
                "experiments.yaml if you mean to submit there."
            )

    # -- execution -------------------------------------------------------

    def run(
        self,
        *,
        project_path: Path,
        task_name: str,
        worktree: Path,
        parameters: dict[str, object] | None = None,
        project_id: str | None = None,
        execute: bool = False,
        partition: str | None = None,
    ) -> tuple[ExperimentStore, ExperimentRun, EvidencePacket | None]:
        """Run one declared experiment and ingest whatever it produced.

        Returns the store, the run record, and -- for a local execution -- the
        candidate evidence packet. A submitted cluster job returns no packet
        yet: it has not finished, and a packet for a job that is still queued
        would describe results that do not exist.
        """

        root = repository_root(project_path)
        command = self.resolve(
            project_id=project_id,
            task_name=task_name,
            parameters=parameters,
            worktree=worktree,
        )
        # One gate, one answer. ``authorize`` either refuses or says how this
        # execution is allowed to happen; there is no third state in which the
        # controller runs something it could not explain the authority for.
        authorization = self.authorize(
            project_id=project_id,
            command=command,
            execute=execute,
            partition=partition,
        )

        created_at = utc_now()
        run = ExperimentRun(
            run_id=make_experiment_run_id(
                project_path=str(root), task_name=task_name, created_at=created_at
            ),
            task_name=task_name,
            project_id=project_id,
            project_path=str(root),
            base_commit=head_commit(root) if has_commits(root) else None,
            worktree_path=str(worktree),
            executor=command.executor,
            argv=list(command.argv),
            parameters=dict(command.parameters),
            declared_outputs=list(command.outputs),
            timeout_seconds=command.timeout_seconds,
            created_at=created_at,
            config_digest=self.config.digest(),
            authorized_by=authorization,
        )
        store = ExperimentStore.create(run)
        store.append_event(
            "experiment_prepared",
            task_name=task_name,
            executor=str(command.executor),
            argv=list(command.argv),
            parameters=dict(command.parameters),
            declared_outputs=list(command.outputs),
            authorized_by=authorization,
            config_digest=run.config_digest,
        )

        # Git-level isolation is not filesystem isolation. A declared output
        # path that is a symlink out of the worktree would put a result outside
        # the checkout, and Git would report a clean run.
        assert_contained_symlinks(worktree)

        if command.executor is ExecutorKind.LOCAL:
            run = self._run_locally(store, run, command, worktree=worktree)
        else:
            run = self._submit(
                store, run, command, worktree=worktree, partition=partition
            )
            store.save(run)
            return store, run, None

        packet = self.ingest(store, run, worktree=worktree, checks=list(command.checks))
        return store, store.load(), packet

    def _run_locally(
        self,
        store: ExperimentStore,
        run: ExperimentRun,
        command: ResolvedCommand,
        *,
        worktree: Path,
    ) -> ExperimentRun:
        before = snapshot_mtimes(worktree)
        outcome = run_locally(
            command,
            worktree=worktree,
            stdout_path=store.path("logs", "stdout.txt"),
            stderr_path=store.path("logs", "stderr.txt"),
        )
        self.budget.record(ExecutorKind.LOCAL)
        changed = discover_changed_paths(worktree, since=before)
        artifacts = collect_artifacts(
            worktree, declared=list(command.outputs), discovered=changed
        )
        run = run.model_copy(
            update={
                "state": outcome.state,
                "exit_code": outcome.exit_code,
                "started_at": outcome.started_at,
                "ended_at": outcome.ended_at,
                "usage": outcome.usage,
                "stdout_path": store.relative(store.path("logs", "stdout.txt")),
                "stderr_path": store.relative(store.path("logs", "stderr.txt")),
                "failure_reason": outcome.failure_reason,
                "artifacts": artifacts,
            }
        )
        store.save(run)
        store.append_event(
            "experiment_executed",
            state=str(run.state),
            exit_code=run.exit_code,
            wall_clock_seconds=run.usage.wall_clock_seconds,
            artifacts=len(run.artifacts),
            missing_outputs=run.missing_outputs,
            undeclared=run.undeclared_artifacts,
        )
        return run

    def _submit(
        self,
        store: ExperimentStore,
        run: ExperimentRun,
        command: ResolvedCommand,
        *,
        worktree: Path,
        partition: str | None,
    ) -> ExperimentRun:
        settings = self.config.slurm_for(run.project_id)
        executor = SlurmExecutor(settings=settings)
        chosen = partition or settings.default_partition
        assert chosen is not None  # authorize() already refused the alternative
        working_directory = settings.remote_working_directory or str(worktree)
        script = executor.build_script(
            command,
            job_name=f"research-os-{run.task_name}",
            partition=chosen,
            working_directory=working_directory,
            time_limit=settings.default_time_limit,
            stdout_path=str(store.path("logs", "slurm-stdout.txt")),
            stderr_path=str(store.path("logs", "slurm-stderr.txt")),
        )
        script_path = store.write_text("job.sbatch", script)
        record = executor.submit(script_path, partition=chosen)
        self.budget.record(command.executor)
        store.append_event(
            "experiment_submitted",
            job_id=record.job_id,
            partition=chosen,
            host=settings.ssh_host,
            script=store.relative(script_path),
        )
        return run.model_copy(
            update={
                "state": ExecutionState.SUBMITTED,
                "started_at": utc_now(),
                "script_path": store.relative(script_path),
                "scheduler": record,
            }
        )

    # -- polling and ingestion -------------------------------------------

    def poll(self, store: ExperimentStore) -> ExperimentRun:
        """Ask the scheduler what became of a submitted job and record it."""

        run = store.load()
        if run.scheduler is None:
            raise SchedulerUnavailableError(
                f"{run.run_id} was not submitted to a scheduler, so there is "
                "nothing to poll"
            )
        if run.terminal:
            return run
        settings = self.config.slurm_for(run.project_id)
        state, record, usage = SlurmExecutor(settings=settings).poll(
            run.scheduler.job_id
        )
        updated = run.model_copy(
            update={
                "state": state,
                "exit_code": record.exit_code,
                "scheduler": record.model_copy(
                    update={
                        "partition": record.partition or run.scheduler.partition,
                        "submitted_at": run.scheduler.submitted_at,
                    }
                ),
                "usage": usage if usage.anything_observed else run.usage,
                "ended_at": utc_now()
                if state
                in {
                    ExecutionState.COMPLETED,
                    ExecutionState.FAILED,
                    ExecutionState.CANCELLED,
                    ExecutionState.TIMED_OUT,
                }
                else run.ended_at,
            }
        )
        store.save(updated)
        store.append_event(
            "experiment_polled",
            job_id=record.job_id,
            state=str(state),
            raw_state=record.raw_state,
            exit_code=record.exit_code,
        )
        return updated

    def ingest(
        self,
        store: ExperimentStore,
        run: ExperimentRun,
        *,
        worktree: Path,
        checks: list[str],
    ) -> EvidencePacket:
        """Hash what the run produced, run the declared checks, build the packet."""

        if not run.artifacts and run.declared_outputs:
            run = run.model_copy(
                update={
                    "artifacts": collect_artifacts(
                        worktree, declared=list(run.declared_outputs)
                    )
                }
            )
            store.save(run)
        outcomes = run_checks(run, worktree=worktree, names=checks)
        packet = build_evidence_packet(run, checks=outcomes)
        store.save_packet(packet)
        store.append_event(
            "evidence_packet_built",
            packet_id=packet.packet_id,
            usable=packet.usable,
            checks_passed=sum(1 for item in outcomes if item.passed),
            checks_failed=sum(1 for item in outcomes if not item.passed),
            artifacts=len(packet.artifacts),
            blocking=packet.blocking_notes,
        )
        return packet

    def cancel(self, store: ExperimentStore) -> ExperimentRun:
        run = store.load()
        if run.scheduler is None:
            raise SchedulerUnavailableError(
                f"{run.run_id} was not submitted to a scheduler"
            )
        settings = self.config.slurm_for(run.project_id)
        record = SlurmExecutor(settings=settings).cancel(run.scheduler.job_id)
        updated = run.model_copy(
            update={
                "state": ExecutionState.CANCELLED,
                "scheduler": record,
                "ended_at": utc_now(),
                "failure_reason": "cancelled by the researcher",
            }
        )
        store.save(updated)
        store.append_event("experiment_cancelled", job_id=record.job_id)
        return updated
