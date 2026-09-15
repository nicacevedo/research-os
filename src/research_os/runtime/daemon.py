"""researchd: the durable local control plane.

One process, one loop, deterministic work. Its responsibilities are entirely
operational: turn events into work, claim work that is due, keep leases alive,
recover leases whose holders died, resume cycles that stopped, poll external
jobs, release reservations nobody settled, prune finished cycles' checkpoints,
and tell a person when a scientific decision is genuinely owed.

It calls no model. That is what keeps invariant 2 ("no continuously thinking
agents") true while a background process exists: the daemon is a scheduler and a
recoverer, and every frontier-model call still happens inside a claimed work
item against a reserved budget. A background *process* is not a background
*agent*.

**The loop is one testable function.** :meth:`Daemon.tick` performs one pass of
everything and returns a :class:`TickReport`. No threads are needed to test the
control plane, and ``run_forever`` is a thin wrapper that calls ``tick`` and
sleeps. A daemon whose behaviour can only be observed by starting it is a daemon
nobody writes tests for.

**Nothing is held across work.** A work item is claimed in one short
transaction, executed with no transaction at all while a background thread
renews the lease, and completed in another. The database is never waiting on a
model, a cluster, or a person.

**It is not a distributed system.** One researcher, one workstation, an optional
cluster. Several ``researchd`` processes on one machine are safe -- everything
goes through ``skip locked`` and leases -- but nothing here assumes more than
one, and nothing needs a broker, a scheduler, or a container runtime to run.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any

from research_os.errors import EXIT_ERROR, EXIT_OK, ResearchOSError
from research_os.runtime import checkpoints
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.clock import Clock, SystemClock
from research_os.runtime.config import RuntimeConfig, load_config, redact_dsn
from research_os.runtime.cycles import (
    CycleResult,
    resume_cycle,
    should_continue,
    start_cycle,
)
from research_os.runtime.db import Database, TransientDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import InvocationLedger, worker_identity
from research_os.runtime.interfaces import ModelProvider, Notifier
from research_os.runtime.leases import LeaseKeeper
from research_os.runtime.migrations import migrate
from research_os.runtime.models import (
    Autonomy,
    Event,
    ExternalJobStatus,
    TerminalState,
    WorkItem,
    WorkStatus,
)
from research_os.runtime.notify import FileNotifier
from research_os.runtime.queue import LeaseLostError, WorkQueue
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("researchd")

#: How often maintenance runs. An hour: pruning a day-old checkpoint a few
#: minutes late costs nothing, and the query is cheap but not free.
_MAINTENANCE_INTERVAL = 3600.0


# --------------------------------------------------------------- work kinds --
class WorkKind:
    """The operational work kinds the daemon knows how to run.

    Plain constants rather than an enum because these are queue payload
    discriminators rather than a domain model, and the queue stores them as
    text. An unknown kind is failed as ``POLICY_REFUSED`` rather than skipped:
    work nobody can run must not sit claimable forever.
    """

    RUN_CYCLE = "run_cycle"
    RESUME_CYCLE = "resume_cycle"
    CONTINUE_OBJECTIVE = "continue_objective"
    POLL_EXTERNAL_JOBS = "poll_external_jobs"
    PRUNE_CHECKPOINTS = "prune_checkpoints"
    INTEGRITY_AUDIT = "integrity_audit"


#: Which event kind produces which work. The whole event-to-work mapping, in one
#: table, so "why did this run" is answerable by reading twelve lines.
EVENT_WORK: dict[str, str] = {
    "RESEARCH_RUN_REQUESTED": WorkKind.RUN_CYCLE,
    "SCIENTIFIC_DECISION_RECORDED": WorkKind.RESUME_CYCLE,
    "EXTERNAL_JOB_FINISHED": WorkKind.RESUME_CYCLE,
    "RESEARCH_CYCLE_FINISHED": WorkKind.CONTINUE_OBJECTIVE,
}

#: Deliberately absent above: ``WORKER_RECOVERED``.
#:
#: A reclaimed work item *is* its own retry -- ``reclaim_expired`` puts it back
#: on the queue -- so mapping the recovery event to a second item only ever
#: duplicated it, and because the two had different kinds they had different
#: dedup keys, so the duplication was not even caught.
#:
#: The per-run key introduced to bound that duplication then collided with the
#: real resume events, which is the failure recorded in ``_dedup_key``. Removing
#: the mapping fixes both: there is nothing to bound, and the key can go back to
#: being per event.


@dataclass
class TickReport:
    """What one pass of the control plane did.

    Returned rather than logged-and-forgotten so the loop can be driven by a
    test and asserted on.
    """

    events_ingested: int = 0
    work_enqueued: int = 0
    work_claimed: int = 0
    work_succeeded: int = 0
    work_failed: int = 0
    leases_reclaimed: int = 0
    invocations_abandoned: int = 0
    reservations_released: int = 0
    jobs_polled: int = 0
    schedules_fired: int = 0
    checkpoints_pruned: int = 0
    approvals_surfaced: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def did_something(self) -> bool:
        return any(
            (
                self.events_ingested,
                self.work_enqueued,
                self.work_claimed,
                self.leases_reclaimed,
                self.invocations_abandoned,
                self.reservations_released,
                self.jobs_polled,
                self.schedules_fired,
                self.checkpoints_pruned,
                self.approvals_surfaced,
            )
        )

    def payload(self) -> dict[str, Any]:
        return {
            "events_ingested": self.events_ingested,
            "work_enqueued": self.work_enqueued,
            "work_claimed": self.work_claimed,
            "work_succeeded": self.work_succeeded,
            "work_failed": self.work_failed,
            "leases_reclaimed": self.leases_reclaimed,
            "invocations_abandoned": self.invocations_abandoned,
            "reservations_released": self.reservations_released,
            "jobs_polled": self.jobs_polled,
            "schedules_fired": self.schedules_fired,
            "checkpoints_pruned": self.checkpoints_pruned,
            "approvals_surfaced": self.approvals_surfaced,
            "notes": list(self.notes),
        }


#: Resolves a project id to the repository it lives in. Injected so tests do not
#: need a registry and so the daemon has one place that answers "where is this
#: project", rather than each handler guessing.
RepoResolver = Callable[[str], Path]

#: Builds the model provider for one run. A factory rather than an instance
#: because the router records provenance against a specific run and work item.
ModelFactory = Callable[[str, str, str | None], ModelProvider]


class Daemon:
    """The control plane. One instance per process."""

    __slots__ = (
        "_budgets",
        "_clock",
        "_config",
        "_db",
        "_ledger",
        "_maintenance_stamp",
        "_models",
        "_notifier",
        "_owner",
        "_queue",
        "_repo_for",
        "_stopping",
        "_store",
    )

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        db: Database,
        repo_for: RepoResolver,
        models: ModelFactory,
        notifier: Notifier | None = None,
        clock: Clock | None = None,
        owner: str | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._store = RuntimeStore(db)
        self._queue = WorkQueue(db)
        self._ledger = InvocationLedger(db)
        self._budgets = BudgetLedger(db)
        self._repo_for = repo_for
        self._models = models
        self._notifier = notifier or FileNotifier()
        self._clock = clock or SystemClock()
        self._owner = owner or worker_identity()
        self._stopping = False
        self._maintenance_stamp: datetime | None = None

    @property
    def owner(self) -> str:
        return self._owner

    def stop(self) -> None:
        """Ask the loop to finish its current pass and exit."""
        self._stopping = True

    # ---------------------------------------------------------------- tick --
    def tick(self) -> TickReport:
        """One pass of everything the control plane owes.

        Ordered by urgency, and the order matters. Recovery comes first: a lease
        whose holder died must go back on the queue *before* anything is
        claimed, or a restarting daemon picks up new work while old work sits
        stranded. Claiming comes last, so every pass leaves the system in a
        recovered state even if the worker then crashes.
        """

        report = TickReport()
        self._recover(report)
        self._ingest_events(report)
        self._fire_schedules(report)
        self._poll_external_jobs(report)
        self._maintain(report)
        self._surface_approvals(report)
        self._claim_and_run(report)
        return report

    # --------------------------------------------------------- maintenance --
    def _maintain(self, report: TickReport) -> None:
        """Prune the checkpoints of cycles that finished long ago.

        Called from ``tick`` rather than waiting for something to enqueue the
        work, because nothing did. ``prune_checkpoints`` had a handler, a
        ``checkpoint_retention_days`` setting, and a module docstring insisting
        retention was "implemented rather than aspirational" -- and no code path
        that ever created the work item. The checkpoint tables were append-only
        for the life of a deployment, and ``TickReport.checkpoints_pruned`` was
        permanently zero. An independent review found it.

        Paced by an in-process timestamp rather than a schedule row, so it works
        on a fresh database with no seeding. A prune that runs a few minutes
        late costs nothing.
        """

        now = self._clock.now()
        if (
            self._maintenance_stamp is not None
            and (now - self._maintenance_stamp).total_seconds() < _MAINTENANCE_INTERVAL
        ):
            return
        self._maintenance_stamp = now
        try:
            pruned = checkpoints.prune(
                self._db,
                self._config.require_dsn(),
                retention_days=self._config.settings.checkpoint_retention_days,
            )
        except ResearchOSError as exc:
            report.notes.append(f"checkpoint prune failed: {exc}")
            return
        report.checkpoints_pruned = len(pruned)
        if pruned:
            LOG.info("pruned checkpoints for %d finished cycle(s)", len(pruned))

    # ------------------------------------------------------------ recovery --
    def _recover(self, report: TickReport) -> None:
        """Put back what dead workers were holding.

        Three kinds of orphan, three different treatments, and the difference
        matters. An expired *lease* means work nobody is doing: requeue it. A
        stale *invocation* means a side effect whose outcome is unknown: flag it
        for reconciliation, never silently retry. A stale *reservation* means
        capacity held for a spend that may never have happened: release it,
        because charging for unknown work is the worse error.
        """

        reclaimed = self._queue.reclaim_expired()
        report.leases_reclaimed = len(reclaimed)
        for item in reclaimed:
            if item.status is WorkStatus.FAILED:
                # Out of attempts. The event is recorded so the failure is
                # visible, but it must not map to new work: an independent
                # review found that it did, and that the new item arrived with a
                # *fresh* attempt budget -- so an item that killed three workers
                # was marked FAILED and immediately replaced by one that would
                # kill three more. The queue's attempt cap was defeated by the
                # thing that was supposed to honour it, and the backlog for one
                # run grew without bound.
                self._store.record_event(
                    kind="WORK_EXHAUSTED",
                    project_id=item.project_id,
                    run_id=item.run_id,
                    work_id=item.work_id,
                    payload={
                        "kind": item.kind,
                        "attempts": item.attempts,
                        "failure_class": item.failure_class,
                    },
                    dedup_key=f"exhausted:{item.work_id}",
                )
                report.notes.append(
                    f"{item.work_id} ({item.kind}) is out of attempts after "
                    f"{item.attempts} and will not be retried"
                )
                continue
            if item.run_id:
                # Requeued, and the requeued item *is* the retry. The event
                # exists so the recovery is visible; its work item is deduped
                # per run so it cannot become a second concurrent attempt.
                self._store.record_event(
                    kind="WORKER_RECOVERED",
                    project_id=item.project_id,
                    run_id=item.run_id,
                    work_id=item.work_id,
                    payload={"kind": item.kind, "attempts": item.attempts},
                    dedup_key=f"recovered:{item.work_id}:{item.attempts}",
                )

        lease_grace = self._config.settings.lease_seconds * 3
        abandoned = self._ledger.abandon_stale(older_than_seconds=lease_grace)
        report.invocations_abandoned = len(abandoned)
        for invocation in abandoned:
            report.notes.append(
                f"invocation {invocation.invocation_id} ({invocation.kind}) was "
                f"abandoned by {invocation.owner}; its outcome needs reconciling"
            )

        report.reservations_released = self._budgets.reconcile_stale(
            older_than_seconds=max(3600, lease_grace)
        )

    # -------------------------------------------------------------- events --
    def _ingest_events(self, report: TickReport) -> None:
        """Turn unconsumed events into queued work.

        Claiming an event and enqueuing its work are separate transactions, and
        the queue's dedup key is what makes that safe: a crash between them
        leaves the event consumed and the work absent, and the *next* producer
        of the same event enqueues the same dedup key rather than a duplicate.
        """

        events = self._store.claim_events(
            limit=50,
            owner=self._owner,
            lease_seconds=self._config.settings.lease_seconds,
        )
        report.events_ingested = len(events)
        for event in events:
            kind = EVENT_WORK.get(event.kind)
            if kind is None:
                # Informational. Consumed so it is not claimed again; it
                # deliberately produces no work.
                self._store.consume_event(event.event_id)
                continue
            if event.project_id is None:
                # Consumed, not left. "Left for inspection" meant its lease
                # expired and it was re-claimed every lease period forever --
                # and because `claim_events` orders oldest first with a limit,
                # fifty such events would starve every real one. A total
                # control-plane stall, with no error. A project-less schedule is
                # enough to produce one.
                self._store.record_event(
                    kind="EVENT_UNROUTABLE",
                    payload={"event_id": event.event_id, "event_kind": event.kind},
                    dedup_key=f"unroutable:{event.event_id}",
                )
                self._store.consume_event(event.event_id)
                report.notes.append(
                    f"event {event.event_id} ({event.kind}) has no project, so it "
                    f"cannot become work; recorded EVENT_UNROUTABLE and consumed"
                )
                continue
            if self._enqueue_for(event, kind):
                report.work_enqueued += 1
            else:
                # The dedup key was already taken. Recorded rather than passed
                # over silently: this is exactly how a dropped decision looked
                # from the outside -- no work, no note, no log.
                report.notes.append(
                    f"event {event.event_id} ({event.kind}) matched an existing "
                    f"{kind} work item and queued nothing"
                )
            # Consumed only now that the work row exists. A crash before this
            # leaves the event claimable again once its lease expires, which is
            # why `claim_events` leases rather than consumes.
            self._store.consume_event(event.event_id)

    def _enqueue_for(self, event: Event, kind: str) -> bool:
        if event.project_id is None:  # pragma: no cover - checked by the caller
            return False
        result = self._queue.enqueue(
            project_id=event.project_id,
            kind=kind,
            run_id=event.run_id,
            payload={
                "event_id": event.event_id,
                "event_kind": event.kind,
                **event.payload,
            },
            max_attempts=self._config.settings.max_attempts,
            dedup_key=self._dedup_key(event, kind),
        )
        return result.created

    @staticmethod
    def _dedup_key(event: Event, kind: str) -> str:
        """The key that decides whether this is new work.

        Per *event* for everything but continuation, because two distinct
        events legitimately mean two pieces of work.

        ``RESUME_CYCLE`` was briefly per *run*, to bound a duplication that is
        now removed at its source, and that was the worst bug this system had:
        once the key was taken, a researcher's answered gate produced
        ``SCIENTIFIC_DECISION_RECORDED``, got ``created=False``, and queued
        nothing -- so the run waited forever with a GRANTED approval and no
        operator verb to rescue it. Concurrency is the run lock's job, not the
        dedup key's.

        ``CONTINUE_OBJECTIVE`` stays per run, because there the invariant really
        is "at most one successor".
        """

        if kind == WorkKind.CONTINUE_OBJECTIVE and event.run_id:
            # Per run: a run may have at most one successor, however many events
            # claim it finished -- and two once did.
            return f"{kind}:{event.run_id}"
        return f"{kind}:{event.event_id}"

    def _fire_schedules(self, report: TickReport) -> None:
        """Schedules produce events, never work directly.

        So a scheduled literature refresh passes through exactly the same
        provenance, budget, locking and failure handling as one a person asked
        for. A schedule that bypassed the queue would be a second execution path
        with none of those.
        """

        due = self._store.claim_due_schedules()
        report.schedules_fired = len(due)
        for schedule in due:
            self._store.record_event(
                kind=schedule.kind,
                project_id=schedule.project_id,
                payload={"schedule_id": schedule.schedule_id, **schedule.payload},
                dedup_key=f"schedule:{schedule.schedule_id}:{schedule.next_run_at.isoformat()}",
            )

    # ------------------------------------------------------- external jobs --
    def _poll_external_jobs(self, report: TickReport) -> None:
        """Reconcile submitted jobs with what the scheduler says.

        This is the loop that removes "check Slurm by hand" from the
        researcher's day. An executor that cannot be reached is not a job
        failure: the job keeps its status and is polled again.
        """

        active = self._store.active_external_jobs(
            limit=50,
            poll_interval_seconds=self._config.settings.external_poll_seconds,
        )
        if not active:
            return
        from research_os.runtime.executors import poll_job

        for job in active:
            try:
                updated = poll_job(job, store=self._store, config=self._config)
            except ResearchOSError as exc:
                LOG.warning("could not poll %s: %s", job.job_id, exc)
                continue
            report.jobs_polled += 1
            if updated.status in {
                ExternalJobStatus.COMPLETED,
                ExternalJobStatus.FAILED,
                ExternalJobStatus.CANCELLED,
                ExternalJobStatus.TIMED_OUT,
            }:
                self._store.record_event(
                    kind="EXTERNAL_JOB_FINISHED",
                    project_id=updated.project_id,
                    run_id=updated.run_id,
                    work_id=updated.work_id,
                    payload={
                        "job_id": updated.job_id,
                        "status": str(updated.status),
                        "exit_code": updated.exit_code,
                        "failure_class": updated.failure_class,
                    },
                    dedup_key=f"job-finished:{updated.job_id}",
                )

    # ---------------------------------------------------------- approvals --
    def _surface_approvals(self, report: TickReport) -> None:
        """Tell a person, once, that a decision is owed.

        Once is enforced by the notification's own dedup event rather than by
        the daemon remembering: a restart must not re-notify, and a decision
        that has been waiting a week must not notify every two seconds.
        """

        pending = self._store.list_approvals(pending_only=True, limit=20)
        for approval in pending:
            _event, created = self._store.record_event(
                kind="APPROVAL_SURFACED",
                project_id=approval.project_id,
                run_id=approval.run_id,
                payload={"approval_id": approval.approval_id},
                dedup_key=f"surfaced:{approval.approval_id}",
            )
            if not created:
                continue
            report.approvals_surfaced += 1
            self._notifier.notify(
                subject=f"A scientific decision is required: {approval.kind}",
                body=(
                    f"{approval.question}\n\n"
                    f"Run `researchctl runtime approvals` to see the prepared decision "
                    f"packet, then `researchctl runtime approve {approval.approval_id}` "
                    f"or `... decline {approval.approval_id}`."
                ),
                run_id=approval.run_id,
                urgent=True,
            )

    # ------------------------------------------------------------ the work --
    def _claim_and_run(self, report: TickReport) -> None:
        claimed = self._queue.claim(
            owner=self._owner,
            lease_seconds=self._config.settings.lease_seconds,
            limit=1,
        )
        report.work_claimed = len(claimed)
        for item in claimed:
            self._run_item(item, report)

    def _run_item(self, item: WorkItem, report: TickReport) -> None:
        handler = {
            WorkKind.RUN_CYCLE: self._work_run_cycle,
            WorkKind.RESUME_CYCLE: self._work_resume_cycle,
            WorkKind.CONTINUE_OBJECTIVE: self._work_continue_objective,
            WorkKind.POLL_EXTERNAL_JOBS: self._work_poll_jobs,
            WorkKind.PRUNE_CHECKPOINTS: self._work_prune_checkpoints,
            WorkKind.INTEGRITY_AUDIT: self._work_integrity_audit,
        }.get(item.kind)

        if handler is None:
            self._queue.fail(
                item.work_id,
                owner=self._owner,
                failure_class=FailureClass.POLICY_REFUSED,
                error=f"no handler for work kind {item.kind!r}",
                force_terminal=True,
            )
            report.work_failed += 1
            return

        with LeaseKeeper(
            self._queue,
            work_id=item.work_id,
            owner=self._owner,
            lease_seconds=self._config.settings.lease_seconds,
            renew_every=self._config.settings.lease_renew_seconds,
        ) as keeper:
            try:
                result = handler(item)
            except LeaseLostError as exc:
                report.notes.append(f"{item.work_id}: {exc}")
                return
            except TransientDatabaseError as exc:
                self._finish_failed(
                    item, FailureClass.DATABASE_TRANSIENT, str(exc), report
                )
                return
            except ResearchOSError as exc:
                self._finish_failed(
                    item, self._classify(exc), f"{type(exc).__name__}: {exc}", report
                )
                return
            except Exception as exc:
                LOG.exception("work %s (%s) raised", item.work_id, item.kind)
                self._finish_failed(
                    item,
                    FailureClass.CODE_EXCEPTION,
                    f"{type(exc).__name__}: {exc}",
                    report,
                )
                return

            if keeper.lost:
                # Whatever this did is durable and reusable; the new owner will
                # find the completed invocations. Say nothing to the queue.
                report.notes.append(f"{item.work_id}: finished after losing its lease")
                return
            try:
                self._queue.succeed(item.work_id, owner=self._owner, result=result)
            except LeaseLostError as exc:
                report.notes.append(f"{item.work_id}: {exc}")
                return
            report.work_succeeded += 1

    def _finish_failed(
        self,
        item: WorkItem,
        failure_class: FailureClass,
        error: str,
        report: TickReport,
    ) -> None:
        try:
            self._queue.fail(
                item.work_id,
                owner=self._owner,
                failure_class=failure_class,
                error=error,
            )
        except LeaseLostError:
            report.notes.append(f"{item.work_id}: failed after losing its lease")
            return
        report.work_failed += 1
        if item.run_id:
            self._store.record_event(
                kind="WORK_FAILED",
                project_id=item.project_id,
                run_id=item.run_id,
                work_id=item.work_id,
                payload={"failure_class": str(failure_class), "error": error[:1000]},
                dedup_key=f"work-failed:{item.work_id}:{item.attempts}",
            )

    @staticmethod
    def _classify(exc: ResearchOSError) -> FailureClass:
        """Map a domain exception onto the taxonomy.

        Kept small and explicit. An exception with no mapping is ``UNKNOWN``,
        which the policy table makes terminal -- so an unclassified failure
        stops rather than looping, and shows up as something to classify.
        """

        from research_os.runtime.artifacts import ArtifactMissingError
        from research_os.runtime.budgets import BudgetExhaustedError
        from research_os.runtime.idempotency import UnreconciledInvocationError
        from research_os.runtime.kernel import ScientificAuthorityError
        from research_os.runtime.locks import RepositoryBusyError
        from research_os.runtime.policy import PolicyRefusedError, ScientificGateError
        from research_os.runtime.routing import RoutingError

        mapping: tuple[tuple[type[BaseException], FailureClass], ...] = (
            (BudgetExhaustedError, FailureClass.BUDGET_EXHAUSTED),
            (ScientificGateError, FailureClass.MISSING_SCIENTIFIC_AUTHORITY),
            (ScientificAuthorityError, FailureClass.MISSING_SCIENTIFIC_AUTHORITY),
            (PolicyRefusedError, FailureClass.POLICY_REFUSED),
            (RepositoryBusyError, FailureClass.GIT_CONFLICT),
            (ArtifactMissingError, FailureClass.ARTIFACT_MISSING),
            (UnreconciledInvocationError, FailureClass.WORKER_CRASH),
            (RoutingError, FailureClass.PROVIDER_UNAVAILABLE),
        )
        for kind, failure_class in mapping:
            if isinstance(exc, kind):
                return failure_class
        return FailureClass.UNKNOWN

    # ------------------------------------------------------- work handlers --
    def _work_run_cycle(self, item: WorkItem) -> dict[str, Any]:
        """Enter a cycle's graph, whether or not it has run before.

        There is no separate "start" path here. The run row already exists --
        the event pipeline created it -- and ``resume_cycle`` decides from the
        thread's own checkpoint whether this is the first superstep or the
        fiftieth. A daemon that tracked that itself would be keeping a second
        copy of state LangGraph already has, and would be wrong after a crash.
        """

        run_id = item.run_id
        if run_id is None:
            raise ResearchOSError(f"{item.work_id}: run_cycle work with no run")
        run = self._store.require_run(run_id)
        if run.terminal:
            return {"skipped": "the run is already finished", "status": str(run.status)}
        result = resume_cycle(
            config=self._config,
            db=self._db,
            run_id=run_id,
            repo_path=self._repo_for(run.project_id),
            models=self._models(run_id, run.project_id, item.work_id),
        )
        return self._cycle_result_payload(result)

    def _work_resume_cycle(self, item: WorkItem) -> dict[str, Any]:
        run_id = item.run_id
        if run_id is None:
            raise ResearchOSError(f"{item.work_id}: resume_cycle work with no run")
        run = self._store.require_run(run_id)
        if run.terminal:
            return {"skipped": "the run is already finished", "status": str(run.status)}
        repo = self._repo_for(run.project_id)
        resume_value: Any = None
        if item.payload.get("event_kind") == "SCIENTIFIC_DECISION_RECORDED":
            # A wake-up, carrying no verdict. `await_decision` reads the actual
            # decision from the approvals table; passing `granted` here would be
            # the control plane asserting an outcome it is not entitled to
            # assert, and an earlier version did exactly that.
            resume_value = {
                "source": "recorded decision",
                "approval_id": item.payload.get("approval_id"),
            }
        result = resume_cycle(
            config=self._config,
            db=self._db,
            run_id=run_id,
            repo_path=repo,
            models=self._models(run_id, run.project_id, item.work_id),
            resume_value=resume_value,
        )
        return self._cycle_result_payload(result)

    def _work_continue_objective(self, item: WorkItem) -> dict[str, Any]:
        """Open a successor cycle, if all three bounds permit it.

        This is where isolated agents become a Research OS: nobody decides which
        step runs next. But it is also the single most dangerous handler in the
        system, so the decision is made by
        :func:`~research_os.runtime.cycles.should_continue`, which consults the
        cycle's own recommendation, the lineage depth measured in SQL, and the
        budget -- and says no if any of them does.
        """

        run_id = item.run_id
        if run_id is None:
            raise ResearchOSError(f"{item.work_id}: continue work with no run")
        run = self._store.require_run(run_id)
        repo = self._repo_for(run.project_id)

        recommendation = str(item.payload.get("recommendation") or "")
        previous = CycleResult(
            run=run,
            status=run.status,
            terminal_state=run.terminal_state,
            pending_approval_id=None,
            recommendation=recommendation,
            notes=(),
            state={},
        )
        proceed, why = should_continue(
            db=self._db, config=self._config, result=previous
        )
        if not proceed:
            return {"continued": False, "reason": why}

        successor = start_cycle(
            config=self._config,
            db=self._db,
            project_id=run.project_id,
            repo_path=repo,
            objective=run.objective,
            # A factory, not a provider: the router records provenance against a
            # run id, and the successor's does not exist until `open_cycle` has
            # created it.
            models=lambda successor_id: self._models(
                successor_id, run.project_id, item.work_id
            ),
            autonomy=Autonomy(str(run.autonomy)),
            parent_run_id=run.run_id,
            cycle_index=run.cycle_index + 1,
        )
        return {
            "continued": True,
            "reason": why,
            "successor_run_id": successor.run.run_id,
            **self._cycle_result_payload(successor),
        }

    def _work_poll_jobs(self, item: WorkItem) -> dict[str, Any]:
        report = TickReport()
        self._poll_external_jobs(report)
        return {"jobs_polled": report.jobs_polled}

    def _work_prune_checkpoints(self, item: WorkItem) -> dict[str, Any]:
        pruned = checkpoints.prune(
            self._db,
            self._config.require_dsn(),
            retention_days=self._config.settings.checkpoint_retention_days,
        )
        return {"pruned": list(pruned)}

    def _work_integrity_audit(self, item: WorkItem) -> dict[str, Any]:
        """Verify that stored artifacts still hash to their addresses.

        Corruption here is otherwise silent: the file is present and the right
        length, and the first sign would be a model reading a damaged PDF.
        """

        from research_os.runtime.artifacts import FilesystemArtifactStore

        artifacts = FilesystemArtifactStore(
            self._config.artifacts_root, store=self._store
        )
        with self._db.tx() as conn:
            rows = conn.execute(
                "select artifact_id from artifacts order by created_at desc limit %s",
                (int(item.payload.get("limit", 500)),),
            ).fetchall()
        corrupt = [
            str(row["artifact_id"])
            for row in rows
            if not artifacts.verify(str(row["artifact_id"]))
        ]
        if corrupt:
            self._notifier.notify(
                subject="Artifact integrity audit found corruption",
                body="These artifacts no longer hash to their addresses:\n"
                + "\n".join(corrupt[:20]),
                urgent=True,
            )
        return {"checked": len(rows), "corrupt": corrupt}

    def _cycle_result_payload(self, result: CycleResult) -> dict[str, Any]:
        # No RESEARCH_CYCLE_FINISHED event is emitted here. `cycles._execute`
        # already emits exactly one, and emitting a second with a different
        # dedup key produced two continuation work items for one finished cycle
        # -- each of which would independently pass `should_continue` and open a
        # successor. Found by reading the event log of the first real
        # end-to-end run, which showed the event twice.
        if result.terminal_state is TerminalState.FATAL_INFRASTRUCTURE_ERROR:
            self._notifier.notify(
                subject="A research cycle hit a fatal infrastructure condition",
                body="\n".join(result.notes[-5:]) or "no detail recorded",
                run_id=result.run.run_id,
                urgent=True,
            )
        return {
            "run_id": result.run.run_id,
            "status": str(result.status),
            "terminal_state": str(result.terminal_state)
            if result.terminal_state
            else None,
            "recommendation": result.recommendation,
            "pending_approval_id": result.pending_approval_id,
        }

    # ------------------------------------------------------------ the loop --
    def run_forever(self, *, max_ticks: int | None = None) -> TickReport:
        """Tick until asked to stop.

        Sleeps only when a pass found nothing to do, so a busy queue is drained
        at full speed and an idle one costs one cheap query every couple of
        seconds.
        """

        total = TickReport()
        ticks = 0
        while not self._stopping and (max_ticks is None or ticks < max_ticks):
            ticks += 1
            try:
                report = self.tick()
            except TransientDatabaseError as exc:
                LOG.warning("database unavailable, retrying: %s", exc)
                self._clock.sleep(self._config.settings.poll_interval_seconds * 5)
                continue
            _accumulate(total, report)
            if not report.did_something:
                self._clock.sleep(self._config.settings.poll_interval_seconds)
        return total


def _accumulate(total: TickReport, report: TickReport) -> None:
    for name in (
        "events_ingested",
        "work_enqueued",
        "work_claimed",
        "work_succeeded",
        "work_failed",
        "leases_reclaimed",
        "invocations_abandoned",
        "reservations_released",
        "jobs_polled",
        "schedules_fired",
        "checkpoints_pruned",
        "approvals_surfaced",
    ):
        setattr(total, name, getattr(total, name) + getattr(report, name))
    total.notes.extend(report.notes)


# ------------------------------------------------------------ entry point --
def default_repo_resolver(store: RuntimeStore) -> RepoResolver:
    """Resolve a project to its repository from the operational projects table.

    Not from the global registry: the registry is explicitly disposable
    discovery metadata, and a daemon that stopped working because someone
    deleted it would be depending on something the architecture says is
    deletable.
    """

    def resolve(project_id: str) -> Path:
        project = store.get_project(project_id)
        if project is None:
            raise ResearchOSError(f"no repository recorded for project {project_id!r}")
        return Path(project.repo_path)

    return resolve


def default_model_factory(
    *,
    config: RuntimeConfig,
    db: Database,
    adapters: Mapping[str, Any] | None = None,
) -> ModelFactory:
    """Build a router per run, with whatever providers this machine has."""

    from research_os.automation.commands import provider_registry
    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.routing import ModelRouter, profiles_from_adapters

    registry = dict(adapters) if adapters is not None else provider_registry()
    profiles = profiles_from_adapters(registry)
    store = RuntimeStore(db)

    def build(run_id: str, project_id: str, work_id: str | None) -> ModelProvider:
        return ModelRouter(
            adapters=registry,
            profiles=profiles,
            store=store,
            artifacts=FilesystemArtifactStore(config.artifacts_root, store=store),
            budgets=BudgetLedger(db),
            run_id=run_id,
            project_id=project_id,
            work_id=work_id,
        )

    return build


def build_daemon(
    config: RuntimeConfig,
    *,
    db: Database | None = None,
    adapters: Mapping[str, Any] | None = None,
) -> tuple[Daemon, Database]:
    """Assemble a daemon from configuration. Returns it and the database it owns."""

    database = db or Database(config.require_dsn(), max_size=8)
    migrate(database)
    checkpoints.ensure_tables(config.require_dsn())
    store = RuntimeStore(database)
    daemon = Daemon(
        config=config,
        db=database,
        repo_for=default_repo_resolver(store),
        models=default_model_factory(config=config, db=database, adapters=adapters),
    )
    return daemon, database


def main(argv: list[str] | None = None) -> None:
    """``researchd``. Foreground by default; there is no fork-and-detach mode.

    Foreground because the supervisor should be ``systemd`` or a terminal, both
    of which are better at restarting a process than this program would be, and
    because a daemon that daemonises itself is a daemon whose logs go somewhere
    nobody looks.
    """

    parser = argparse.ArgumentParser(
        prog="researchd",
        description="The Research OS autonomous-runtime control plane.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit. For tests and cron.",
    )
    parser.add_argument(
        "--max-ticks", type=int, default=None, help="Stop after this many passes."
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default: INFO)."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_config()
        daemon, database = build_daemon(config)
    except ResearchOSError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None

    LOG.info("researchd starting; database %s", redact_dsn(config.dsn))

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        LOG.info("signal %s received; finishing the current pass", signum)
        daemon.stop()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_name, handle_signal)

    try:
        if args.once:
            report = daemon.tick()
            LOG.info("one pass: %s", report.payload())
        else:
            total = daemon.run_forever(max_ticks=args.max_ticks)
            LOG.info("researchd stopping: %s", total.payload())
    finally:
        database.close()
    raise SystemExit(EXIT_OK)
