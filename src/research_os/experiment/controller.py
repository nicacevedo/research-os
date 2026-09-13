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
3. **Is the checkout sound?** An experiment runs in an isolated worktree that
   this controller creates, never the researcher's checkout, and a worktree
   containing a symlink that leaves it is refused before a process starts --
   otherwise a write to a declared output path could land outside it and Git
   would show nothing. A caller may override the directory; the run then
   records ``isolated=False``, which is the only way an experiment touches
   canonical science.

What comes out is a candidate packet. It is never Evidence, never accepted, and
never a verdict about a hypothesis.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.filescope import assert_contained_symlinks
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import WorktreeRecord, utc_now
from research_os.automation.worktree import (
    branch_name,
    create_worktree,
    release_worktree,
    worktree_path,
)
from research_os.errors import (
    ExperimentAuthorizationError,
    ExperimentConfigError,
    ExperimentError,
    ExperimentStoreError,
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
from research_os.experiment.store import (
    ExperimentStore,
    experiments_root,
    make_experiment_run_id,
)
from research_os.runlock import run_lock

#: How an execution was authorised, recorded on every run.
#:
#: Recorded rather than assumed, because "who said this could run" is the first
#: question about an experiment that cost something.
AUTHORIZED_EXPLICIT = "explicit --execute"
AUTHORIZED_CONFIGURED = "configured: require_explicit_execute is false"


#: How many same-second retries of one experiment are distinguishable.
#:
#: A bound rather than a loop without one. Reaching it means something is
#: retrying the identical experiment faster than a second, many times over,
#: which is a caller defect worth surfacing rather than papering over.
MAX_RUN_ID_ATTEMPTS = 64


def _unused_run_id(*, project_path: str, task_name: str, created_at: str) -> str:
    """Return this execution's id, stepping past ids already on disk.

    The first attempt is the plain deterministic id, so nothing about existing
    runs or their ids changes. Only a genuine same-second collision takes a
    discriminator, and it takes the lowest one that is free, so the choice is
    reproducible rather than a random suffix.
    """

    for index in range(MAX_RUN_ID_ATTEMPTS):
        candidate = make_experiment_run_id(
            project_path=project_path,
            task_name=task_name,
            created_at=created_at,
            attempt="" if index == 0 else str(index),
        )
        if not (experiments_root() / candidate).exists():
            return candidate
    raise ExperimentStoreError(
        f"{task_name} already has {MAX_RUN_ID_ATTEMPTS} runs recorded for "
        f"{created_at}; refusing to start another in the same second"
    )


#: The timestamp half of a run id, as the shared worktree helpers require it.
_RUN_STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _worktree_run_id(experiment_run_id: str) -> str:
    """Return a run id shaped the way the shared worktree helpers require.

    They are shared with the automation control plane, which validates the
    shape. An experiment run is not an automation run, so this derives a stable
    id from it rather than pretending one exists; the real home of the record is
    the experiment run directory.

    Stable meaning a pure function of its argument. It once read the clock for
    the timestamp half, which made two calls a second apart disagree -- harmless
    while the only call was the one that created the worktree, and wrong the
    moment the path had to be written down before it was created. The stamp
    comes from the experiment run id, which already carries the second this run
    was created in.
    """

    stamp = experiment_run_id.split("-")[1] if "-" in experiment_run_id else ""
    if not _RUN_STAMP_RE.fullmatch(stamp):  # pragma: no cover - defensive
        raise ExperimentStoreError(
            f"{experiment_run_id!r} is not an experiment run id, so no worktree "
            "id can be derived from it"
        )
    digest = hashlib.sha256(experiment_run_id.encode("utf-8")).hexdigest()[:8]
    return f"RUN-{stamp}-{digest}"


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
        worktree: Path | None = None,
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

        ``worktree`` defaults to a fresh isolated worktree at the project's
        current commit, which is what every other write-capable task in this
        system gets. It did not always: an independent reviewer found every
        caller passing the researcher's own checkout, so an experiment ran with
        its cwd inside canonical science and the containment scan below covered
        the whole repository -- which also meant any project with a ``.venv``
        was refused outright, its interpreter symlinks pointing out of the tree.

        A caller may still supply a directory. That is a deliberate act, it is
        recorded as ``isolated=False`` on the run, and it is the only way an
        experiment touches the checkout.
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
        run_id = _unused_run_id(
            project_path=str(root), task_name=task_name, created_at=created_at
        )
        supplied = worktree is not None
        # Where the isolated worktree *will* be, computed rather than observed.
        #
        # This ordering is the whole crash-consistency fix, and it is why the
        # path is derived before anything exists at it. ``create_worktree``
        # registers a worktree in the researcher's own repository and locks it:
        # irreversible, and invisible to a store that has not heard of this run.
        # A process killed between creating the worktree and writing the record
        # used to leave a directory, a lock and a Git registration that neither
        # ``experiment cleanup`` nor ``storage --reclaim`` could find, because
        # both start from the store. Writing the record first, in PREPARING,
        # means the owner always exists before the thing it owns.
        branch: str | None = None
        if worktree is None:
            worktree = worktree_path(_worktree_run_id(run_id), "T-001")
            branch = branch_name(_worktree_run_id(run_id), "T-001")
            # Resolved against the checkout a moment ago; resolve it again now
            # that the command knows where it will actually run.
            command = self.resolve(
                project_id=project_id,
                task_name=task_name,
                parameters=parameters,
                worktree=worktree,
            )
        run = ExperimentRun(
            run_id=run_id,
            task_name=task_name,
            project_id=project_id,
            project_path=str(root),
            base_commit=head_commit(root) if has_commits(root) else None,
            worktree_path=str(worktree),
            branch=branch,
            isolated=not supplied,
            executor=command.executor,
            argv=list(command.argv),
            parameters=dict(command.parameters),
            declared_outputs=list(command.outputs),
            timeout_seconds=command.timeout_seconds,
            created_at=created_at,
            state=(ExecutionState.PREPARED if supplied else ExecutionState.PREPARING),
            config_digest=self.config.digest(),
            authorized_by=authorization,
        )
        store = ExperimentStore.create(run)
        if not supplied:
            store.append_event(
                "experiment_preparing",
                task_name=task_name,
                worktree=str(worktree),
                branch=branch,
            )
            # Held across the whole PREPARING window, and that is what makes the
            # window safe to be visible in.
            #
            # Writing the record before the worktree is what lets recovery find
            # a crash here. It also makes a run that is being prepared *right
            # now* visible to recovery, in a state that is deliberately not
            # ACTIVE -- so a concurrent ``storage --reclaim`` could see a live
            # preparation, call it idle, and delete the worktree out from under
            # it. An independent review found that: the fix for one race opened
            # another.
            #
            # The kernel settles it. A flock is released when its holder dies,
            # so "this lock is held" means a living process is preparing this
            # run and "it is free" means nobody is. No staleness heuristic, no
            # timeout, no note to misread.
            with run_lock(run_id, action="experiment prepare"):
                run, _record = self._prepare_worktree(
                    store, run, run_id=run_id, root=root, worktree=worktree
                )
        store.append_event(
            "experiment_prepared",
            task_name=task_name,
            worktree=str(worktree),
            isolated=not supplied,
            branch=branch,
            executor=str(command.executor),
            argv=list(command.argv),
            parameters=dict(command.parameters),
            declared_outputs=list(command.outputs),
            authorized_by=authorization,
            config_digest=run.config_digest,
        )

        # Git-level isolation is not filesystem isolation. A declared output
        # path that is a symlink out of the worktree would put a result outside
        # it, and Git would report a clean run.
        #
        # This scans the directory the experiment will actually run in. When
        # that is a fresh worktree it holds tracked files only, so an ordinary
        # project's ``.venv`` -- whose interpreter symlinks point at the system
        # Python -- is simply not there to refuse.
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

    def _prepare_worktree(
        self,
        store: ExperimentStore,
        run: ExperimentRun,
        *,
        run_id: str,
        root: Path,
        worktree: Path,
    ) -> tuple[ExperimentRun, WorktreeRecord]:
        """Create the isolated worktree this PREPARING run already recorded.

        Called with this run's lock held, so nothing else may reclaim what it is
        building. The record moves to PREPARED only once the worktree really
        exists at the path the record already names.
        """

        try:
            record = create_worktree(
                run_id=_worktree_run_id(run_id),
                task_id="T-001",
                repository=root,
                base_commit=head_commit(root) if has_commits(root) else "",
            )
        except Exception as exc:
            # The record outlives the failure on purpose. A PREPARING run whose
            # worktree never appeared is the honest description of what
            # happened, and it is what stops a phantom active experiment from
            # existing at all.
            run = run.model_copy(
                update={
                    "state": ExecutionState.FAILED,
                    "failure_reason": f"worktree creation failed: {exc}",
                    "ended_at": utc_now(),
                }
            )
            store.save(run)
            store.append_event(
                "experiment_preparation_failed",
                worktree=str(worktree),
                detail=str(exc),
            )
            raise
        if record.path != str(worktree):  # pragma: no cover - defensive
            raise ExperimentError(
                f"worktree was created at {record.path}, not at the reserved "
                f"path {worktree} this run already recorded"
            )
        run = run.model_copy(update={"state": ExecutionState.PREPARED})
        store.save(run)
        return run, record

    def cleanup(self, store: ExperimentStore) -> tuple[ExperimentRun, tuple[str, ...]]:
        """Release this run's worktree. The branch and the record are kept.

        Added because making experiments isolated created something to clean up
        and nothing to clean it up with: an independent reviewer measured three
        runs leaving three permanent branches and three orphaned locks that
        neither ``doctor`` nor ``storage --reclaim`` could see.

        The branch is kept deliberately. It holds the exact tree the experiment
        ran in, and the artifacts are recorded by path *and* content hash, so a
        researcher who has not yet taken what they need can still get it.
        """

        from research_os.automation.models import WorktreeRecord
        from research_os.automation.worktree import lock_path as worktree_lock_path
        from research_os.automation.worktree import release_worktree_lock
        from research_os.experiment.models import ACTIVE_STATES
        from research_os.runlock import is_held

        run = store.load()
        if not run.isolated or not run.worktree_path:
            return run, ()
        if run.state is ExecutionState.PREPARING and is_held(run.run_id):
            # Being prepared right now by a living process. The second half of
            # the same guard as in ``diagnostics``: reclaim asks before it
            # selects a run, and this asks again before it deletes one, because
            # a preparation can begin between those two moments.
            raise ExperimentStoreError(
                f"{run.run_id} is being prepared by another process; its "
                "worktree is being created right now. Wait for it to finish, "
                "or stop that process, before releasing its directory."
            )
        if run.state in ACTIVE_STATES:
            # A submitted cluster job's working directory *is* this worktree.
            # Removing it under a running job was possible until a third
            # independent review pointed it out; the automation controller has
            # always refused the equivalent.
            raise ExperimentStoreError(
                f"{run.run_id} is {run.state}: something is still using this "
                "worktree. Poll it with 'researchctl experiment poll', or cancel "
                "it, before releasing the directory it is running in."
            )
        target = Path(run.worktree_path)
        if not target.exists():
            # A run killed between taking the lock and creating the worktree
            # leaves the lock and nothing else. Releasing it here is what makes
            # that crash point recoverable rather than permanently blocking the
            # one path this run is allowed to use.
            if release_worktree_lock(target):
                store.append_event("worktree_lock_released", path=str(target))
                return run, (run.worktree_path,)
            return run, ()
        release_worktree(
            WorktreeRecord(
                task_id="T-001",
                path=run.worktree_path,
                branch=run.branch or "unknown",
                base_commit=run.base_commit or "0" * 40,
                lock_path=str(worktree_lock_path(target)),
                created_at=run.created_at,
            ),
            repository=Path(run.project_path),
        )
        store.append_event(
            "worktree_removed", path=run.worktree_path, branch=run.branch
        )
        return run, (run.worktree_path,)

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
