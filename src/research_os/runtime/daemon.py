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

from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    CapsuleError,
    ResearchOSError,
)
from research_os.runtime import checkpoints
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.capsulewatch import observed_digests
from research_os.runtime.clock import Clock, SystemClock
from research_os.runtime.config import RuntimeConfig, load_config, redact_dsn
from research_os.runtime.cycles import (
    CycleResult,
    resume_cycle,
    should_continue,
    start_cycle,
)
from research_os.runtime.db import Database, TransientDatabaseError
from research_os.runtime.failures import (
    PROVIDER_FAILURES,
    FailureClass,
    is_infrastructure,
    retry_delay_seconds,
)
from research_os.runtime.idempotency import InvocationLedger, worker_identity
from research_os.runtime.interfaces import ModelProvider, Notifier
from research_os.runtime.leases import LeaseKeeper
from research_os.runtime.locks import (
    RepositoryBusyError,
    daemon_lock,
    runs_being_executed,
)
from research_os.runtime.migrations import migrate
from research_os.runtime.models import (
    Autonomy,
    Event,
    ExternalJobStatus,
    RunStatus,
    StrandedRun,
    TerminalState,
    WorkItem,
    WorkStatus,
)
from research_os.runtime.notify import FileNotifier
from research_os.runtime.queue import LeaseLostError, WorkQueue
from research_os.runtime.store import (
    RuntimeStateError,
    RuntimeStore,
    SuccessorExistsError,
)

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
    ADVANCE_OBJECTIVE = "advance_objective"
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
    # A person changed the canonical science. Not a resume -- the thread that
    # was waiting has finished, and reviving it would be turning a bounded
    # cycle into an immortal one. A *successor* cycle, with recorded lineage.
    "CAPSULE_CHANGED": WorkKind.ADVANCE_OBJECTIVE,
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


#: Which work kind runs which handler, by method name.
#:
#: Method *names* rather than bound methods so the table is a module constant a
#: test can read. The previous version built the same mapping inside
#: ``_run_item`` and a separate test listed the runnable kinds by hand -- so
#: adding a kind meant editing two places, and forgetting the second one made a
#: test fail for a reason unrelated to the defect it was written to catch.
WORK_HANDLERS: dict[str, str] = {
    WorkKind.RUN_CYCLE: "_work_run_cycle",
    WorkKind.RESUME_CYCLE: "_work_resume_cycle",
    WorkKind.CONTINUE_OBJECTIVE: "_work_continue_objective",
    WorkKind.ADVANCE_OBJECTIVE: "_work_advance_objective",
    WorkKind.POLL_EXTERNAL_JOBS: "_work_poll_jobs",
    WorkKind.PRUNE_CHECKPOINTS: "_work_prune_checkpoints",
    WorkKind.INTEGRITY_AUDIT: "_work_integrity_audit",
}


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
    #: Items put back on the queue without charging an attempt, because the
    #: provider they need is in a breaker cooldown and no invocation was made.
    work_deferred: int = 0
    #: Stranded runs this pass put back on the queue.
    runs_rescheduled: int = 0
    #: Stranded runs this pass gave up on, after `max_run_reschedules`.
    runs_abandoned: int = 0
    leases_reclaimed: int = 0
    invocations_abandoned: int = 0
    interpretations_abandoned: int = 0
    reservations_released: int = 0
    jobs_polled: int = 0
    schedules_fired: int = 0
    checkpoints_pruned: int = 0
    approvals_surfaced: int = 0
    capsules_observed: int = 0
    capsule_changes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def did_something(self) -> bool:
        return any(
            (
                self.events_ingested,
                self.work_enqueued,
                self.work_claimed,
                self.work_deferred,
                self.runs_rescheduled,
                self.runs_abandoned,
                self.leases_reclaimed,
                self.invocations_abandoned,
                self.interpretations_abandoned,
                self.reservations_released,
                self.jobs_polled,
                self.schedules_fired,
                self.checkpoints_pruned,
                self.approvals_surfaced,
                self.capsule_changes,
            )
        )

    def payload(self) -> dict[str, Any]:
        return {
            "events_ingested": self.events_ingested,
            "work_enqueued": self.work_enqueued,
            "work_claimed": self.work_claimed,
            "work_succeeded": self.work_succeeded,
            "work_failed": self.work_failed,
            "work_deferred": self.work_deferred,
            "runs_rescheduled": self.runs_rescheduled,
            "runs_abandoned": self.runs_abandoned,
            "leases_reclaimed": self.leases_reclaimed,
            "invocations_abandoned": self.invocations_abandoned,
            "interpretations_abandoned": self.interpretations_abandoned,
            "reservations_released": self.reservations_released,
            "jobs_polled": self.jobs_polled,
            "schedules_fired": self.schedules_fired,
            "checkpoints_pruned": self.checkpoints_pruned,
            "approvals_surfaced": self.approvals_surfaced,
            "capsules_observed": self.capsules_observed,
            "capsule_changes": self.capsule_changes,
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
        "_observation_stamp",
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
        self._observation_stamp: datetime | None = None

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

        Capsule observation comes second, before ingest, so that a scientific
        change a person made becomes an event in the *same* pass that notices
        it. Observing after ingest would mean every change waited a full tick
        before anything looked at it, which is the kind of one-tick lag that is
        invisible in tests and looks like "it did not work" to a researcher who
        has just promoted something.

        Run reconciliation sits between the two, and the position is load
        bearing in both directions. After ``_recover``, because an expired
        lease that is about to be requeued is not a stranded run and would be
        double-counted as one. Before ``_observe_capsules``, because a run
        stuck RUNNING hides its objective from ``parked_objectives`` -- so a
        capsule change observed while the orphan still exists finds a smaller
        frontier than the researcher has. On 2026-09-19 that difference was
        five parked objectives against two.
        """

        report = TickReport()
        self._recover(report)
        self._reconcile_runs(report)
        self._observe_capsules(report)
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

        # A fourth orphan, with a fourth treatment: an interpretation whose
        # reader stopped reporting. Marked ABANDONED and deliberately *not*
        # deleted or re-claimed here. The row is the only thing that still holds
        # "an interpretation of this experiment was begun", and the next
        # attempt needs it to reconnect the artifact the dead reader may already
        # have written. Deleting it would turn one interrupted reading into two
        # scientific interpretations of one experiment.
        stale = self._store.abandon_stale_interpretations(
            older_than_seconds=lease_grace
        )
        report.interpretations_abandoned = len(stale)
        for interpretation in stale:
            report.notes.append(
                f"interpretation {interpretation.interpretation_id} of "
                f"{interpretation.job_id} was abandoned; the next cycle will "
                f"resume it rather than start a second one"
            )

    # ------------------------------------------------------ reconciliation --
    def _reconcile_runs(self, report: TickReport) -> None:
        """Give every stranded run a way out, without naming any of them.

        ``_recover`` answers "what were dead workers holding". This answers a
        question nothing asked before: **what is holding nothing at all?**

        A run is stranded when it is RUNNING or CREATED and no live work item,
        and no outstanding external job, could ever advance it -- see
        :meth:`RuntimeStore.stranded_runs`. There is no path out of that state
        in the rest of this control plane. Nothing polls it, no event names
        it, its work item is FAILED and the queue is finished with it. It is
        RUNNING until someone runs SQL.

        That is not only a stuck run; it is a lost *objective*, and that is
        the part worth stating. ``parked_objectives`` excludes an objective
        whose newest run is in flight, deliberately, so that a capsule change
        cannot open a second thread for work already under way. An orphan is
        in flight forever, so the objective it belongs to is removed from the
        research frontier permanently, by a network failure. Three of them
        were, on 2026-09-19.

        Two dispositions, and the order matters:

        **Reschedule**, when the run has not used up ``max_run_reschedules``.
        A fresh ``run_cycle`` item, which re-enters the *same* run at its own
        LangGraph checkpoint. Not a new cycle: no successor, no lineage link,
        nothing charged to ``max_cycles_per_objective``. The work the cycle
        already did is in the checkpoint and is reused.

        **Abandon**, past the bound: FAILED / FATAL_INFRASTRUCTURE_ERROR, with
        the evidence in ``detail``. Honest, and specifically *not* a scientific
        terminal state -- an abandoned run is not offered to a capsule change
        as though it had concluded something.

        Idempotence comes from the dedup key, which names the reschedule
        ordinal. Two daemons, or one restarted mid-pass, compute the same key
        and the second enqueues nothing. The bound is counted from the event
        ledger, so it cannot be reset by a restart either.
        """

        stranded = self._store.stranded_runs(
            grace_seconds=self._config.settings.run_reconcile_grace_seconds,
            limit=25,
        )
        # A cycle running right now looks exactly like a stranded one from the
        # outside: `_work_advance_objective` executes inline, so between
        # `open_cycle` and the ingest pass the run has no work item at all.
        # The advisory lock is the only thing that knows the difference.
        in_flight = runs_being_executed(
            self._db, [entry.run.run_id for entry in stranded]
        )
        ceiling = self._config.settings.max_run_reschedules
        for entry in stranded:
            run = entry.run
            if run.run_id in in_flight:
                continue
            if not self._blocking_condition_cleared(entry):
                # The reason it stopped has not lifted. Rescheduling now would
                # spend a reschedule to reach the same failure, so the pass
                # says nothing and looks again next tick. Deliberately silent:
                # an event per tick per waiting run is a log nobody reads.
                continue
            used = self._store.event_count(run_id=run.run_id, kind="RUN_RESCHEDULED")
            if used >= ceiling:
                self._abandon_run(entry, report, attempts=used)
                continue
            enqueued = self._queue.enqueue(
                project_id=run.project_id,
                kind=WorkKind.RUN_CYCLE,
                run_id=run.run_id,
                payload={
                    "reason": "reconciliation",
                    "reschedule": used + 1,
                    "stranded_after": entry.last_failure_class,
                },
                max_attempts=self._config.settings.max_attempts,
                dedup_key=f"reconcile:{run.run_id}:{used + 1}",
            )
            if not enqueued.created:
                # Another pass got there first, *or* a crash landed between
                # the enqueue and the event below on a previous tick. The two
                # are indistinguishable from here and the second one used to
                # wedge the run permanently: `event_count` stayed at n-1, so
                # this pass kept computing the same dedup key, the key kept
                # refusing, and the run was never rescheduled, never
                # abandoned and never mentioned -- the exact orphan state this
                # pass exists to end. An independent security review found it.
                #
                # Recording the event anyway is safe because the event has its
                # own dedup key: the genuine concurrent case writes it once.
                LOG.info(
                    "%s already has reschedule %d queued; recording it",
                    run.run_id,
                    used + 1,
                )
            _event, recorded = self._store.record_event(
                kind="RUN_RESCHEDULED",
                project_id=run.project_id,
                run_id=run.run_id,
                work_id=enqueued.item.work_id,
                payload={
                    "reschedule": used + 1,
                    "of": ceiling,
                    "stranded_after": entry.last_failure_class,
                    "last_error": (entry.last_error or "")[:1000],
                },
                dedup_key=f"rescheduled:{run.run_id}:{used + 1}",
            )
            # Paced: the grace period now applies to the *next* reschedule of
            # this run too. See `RuntimeStore.touch_run`.
            self._store.touch_run(run.run_id)
            if not recorded:
                # The event was already there, so this pass changed nothing
                # and must not report that it did.
                continue
            report.runs_rescheduled += 1
            if enqueued.created:
                report.work_enqueued += 1
            LOG.info(
                "%s was %s with nothing to run; rescheduled (%d of %d) after %s",
                run.run_id,
                run.status,
                used + 1,
                ceiling,
                entry.last_failure_class or "no recorded failure",
            )

    def _blocking_condition_cleared(self, entry: StrandedRun) -> bool:
        """Whether it is worth putting this run back on the queue yet.

        Only one condition is checked, because only one is *checkable*: a
        provider breaker publishes a ``cooldown_until``, so "the thing that
        stopped this has lifted" is a fact rather than a guess. Everything
        else -- a worker that crashed, a daemon killed mid-pass, an item that
        exhausted its attempts on something unclassified -- has no deadline to
        wait for, and the grace period in ``stranded_runs`` is already the
        answer to "has enough time passed".

        A conservative default in the safe direction: unknown means go. A run
        that is rescheduled too early fails again and is bounded by
        ``max_run_reschedules``; a run that is never rescheduled is the defect
        this pass exists to fix.
        """

        raw = entry.last_failure_class
        if raw is None:
            return True
        try:
            failure_class = FailureClass(raw)
        except ValueError:  # pragma: no cover - the column holds known values
            return True
        if failure_class not in PROVIDER_FAILURES:
            return True
        names = tuple(health.provider for health in self._store.provider_health())
        if not names:
            return True
        # Any provider out of cooldown is enough: routing will find it, and
        # whether it is the *right* one for this cycle's criticality is
        # routing's question, asked with a fresh attempt budget.
        return bool(self._store.usable_providers(names))

    def _abandon_run(
        self, entry: StrandedRun, report: TickReport, *, attempts: int
    ) -> None:
        """Stop trying, in a state that claims nothing about the science."""

        run = entry.run
        reason = entry.last_failure_class or "no recorded failure"
        detail = (
            f"abandoned after {attempts} reconciliation attempt(s): the cycle "
            f"could not be run ({reason}: {entry.last_error or 'no detail'}). "
            f"No scientific stage completed, so this run concludes nothing."
        )
        try:
            self._store.set_run_status(
                run.run_id,
                RunStatus.FAILED,
                terminal_state=TerminalState.FATAL_INFRASTRUCTURE_ERROR,
                detail=detail[:2000],
                next_recommendation="BLOCKED_INFRASTRUCTURE",
            )
        except RuntimeStateError as exc:
            # Someone finished it between the query and here. Nothing to do.
            report.notes.append(f"{run.run_id}: not abandoned ({exc})")
            return
        self._store.record_event(
            kind="RUN_ABANDONED",
            project_id=run.project_id,
            run_id=run.run_id,
            payload={
                "reschedules": attempts,
                "stranded_after": entry.last_failure_class,
                "last_error": (entry.last_error or "")[:1000],
            },
            dedup_key=f"abandoned:{run.run_id}",
        )
        report.runs_abandoned += 1
        report.notes.append(
            f"{run.run_id} could not be run after {attempts} reconciliation "
            f"attempt(s); failed as an infrastructure error"
        )

    # --------------------------------------------- capsule observation ----
    def _observe_capsules(self, report: TickReport) -> None:
        """Notice that a person changed the canonical science, and say so once.

        This is the pass that removes the last piece of routine human
        choreography. A researcher promotes a proposal with ``researchctl
        propose promote``; nothing tells the runtime, by design, because the
        alternative is a scientific kernel that depends on PostgreSQL and on a
        daemon being up. So the runtime looks.

        Paced by an in-process timestamp rather than a schedule row, exactly as
        ``_maintain`` is and for the same reason: it works on a fresh database
        with no seeding, and a first tick always observes.

        A project whose repository has gone -- moved, deleted, on an unmounted
        disk -- is noted and skipped rather than failing the pass. One
        unreachable project must not stop the control plane observing the
        others.
        """

        now = self._clock.now()
        if (
            self._observation_stamp is not None
            and (now - self._observation_stamp).total_seconds()
            < self._config.settings.capsule_observe_seconds
        ):
            return
        self._observation_stamp = now

        for project in self._store.list_projects():
            try:
                repo = self._repo_for(project.project_id)
            except ResearchOSError as exc:
                report.notes.append(
                    f"cannot locate {project.project_id} to observe its capsule: {exc}"
                )
                continue
            capsule, frontier = observed_digests(repo)
            report.capsules_observed += 1
            changed, previous_capsule, previous_frontier = self._store.observe_capsule(
                project_id=project.project_id,
                capsule_digest=capsule,
                frontier_digest=frontier,
            )
            if not changed:
                continue
            # Exactly one event per *transition*, not per destination digest.
            #
            # The key was `capsule-changed:{project}:{capsule}`, and event
            # dedup is global and permanent -- so promote, revert, re-promote
            # produced two events for three real changes and the third was
            # discarded in silence. An adversarial review executed it. Keying
            # on the pair means a state the project has been in before is still
            # a change when it is arrived at again.
            _event, created = self._store.record_event(
                kind="CAPSULE_CHANGED",
                project_id=project.project_id,
                payload={
                    "capsule_digest": capsule,
                    "previous_capsule_digest": previous_capsule,
                    "frontier_digest": frontier,
                    "previous_frontier_digest": previous_frontier,
                    "frontier_changed": frontier != (previous_frontier or ""),
                },
                dedup_key=(
                    f"capsule-changed:{project.project_id}:"
                    f"{(previous_capsule or 'none')[:16]}->{capsule[:16]}"
                ),
            )
            if created:
                report.capsule_changes += 1
                LOG.info(
                    "%s: canonical scientific state changed (%s -> %s)",
                    project.project_id,
                    (previous_capsule or "none")[:12],
                    capsule[:12],
                )
            else:
                # Counted only when it became an event. The counter is what a
                # person reads to answer "has this been noticing anything", and
                # one that counts observations no work came from answers wrongly
                # in the direction of reassurance -- which is the mistake
                # `observe_capsule`'s own docstring argues against.
                report.notes.append(
                    f"{project.project_id}: the capsule changed to a state it "
                    f"has been in before ({capsule[:12]}), which an earlier "
                    f"event already claimed; no new work was queued"
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
        if kind == WorkKind.ADVANCE_OBJECTIVE:
            # Per project *and observed digest*. One scientific change produces
            # at most one advance, however many times it is observed -- two
            # daemons, or one daemon restarted between the observation and the
            # ingest. Keyed on the digest rather than the project so that a
            # *second*, later change is a second advance rather than being
            # swallowed by the first one's key.
            # Per project and *transition*, matching the event's own key, so a
            # state the project has been in before still becomes work.
            previous = str(event.payload.get("previous_capsule_digest") or "none")
            digest = str(event.payload.get("capsule_digest") or event.event_id)
            return f"{kind}:{event.project_id}:{previous[:16]}->{digest[:16]}"
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
            # Claim the right to notify first, then notify, then let a failure
            # roll the claim back by *not* being recorded. Recording first meant
            # a notifier failure -- an unwritable state home -- consumed the one
            # notification the researcher was ever going to get, and took the
            # daemon down with it.
            already = [
                event
                for event in self._store.list_events(run_id=approval.run_id, limit=200)
                if event.kind == "APPROVAL_SURFACED"
                and event.payload.get("approval_id") == approval.approval_id
            ]
            if already:
                continue
            try:
                self._notifier.notify(
                    subject=f"A scientific decision is required: {approval.kind}",
                    body=(
                        f"{approval.question}\n\n"
                        f"Run `researchctl runtime approvals` to see the prepared "
                        f"decision packet, then `researchctl runtime approve "
                        f"{approval.approval_id}` or `... decline "
                        f"{approval.approval_id}`."
                    ),
                    run_id=approval.run_id,
                    urgent=True,
                )
            except Exception as exc:  # noqa: BLE001 - a notifier must not stop the plane
                LOG.error("could not notify about %s: %s", approval.approval_id, exc)
                report.notes.append(
                    f"could not notify about {approval.approval_id}: {exc}"
                )
                continue
            self._store.record_event(
                kind="APPROVAL_SURFACED",
                project_id=approval.project_id,
                run_id=approval.run_id,
                payload={"approval_id": approval.approval_id},
                dedup_key=f"surfaced:{approval.approval_id}",
            )
            report.approvals_surfaced += 1

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
        method = WORK_HANDLERS.get(item.kind)
        handler = getattr(self, method) if method is not None else None

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
                    item, FailureClass.DATABASE_TRANSIENT, str(exc), report, exc=exc
                )
                return
            except ResearchOSError as exc:
                self._finish_failed(
                    item,
                    self._classify(exc),
                    f"{type(exc).__name__}: {exc}",
                    report,
                    exc=exc,
                )
                return
            except Exception as exc:
                LOG.exception("work %s (%s) raised", item.work_id, item.kind)
                self._finish_failed(
                    item,
                    FailureClass.CODE_EXCEPTION,
                    f"{type(exc).__name__}: {exc}",
                    report,
                    exc=exc,
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
        *,
        exc: BaseException | None = None,
    ) -> None:
        """Record a failure, and schedule the retry against reality.

        Two things are read off the exception when it offers them, because the
        thing that failed knows more about the failure than the queue does:

        ``retry_at`` -- when the blocking condition is known to lift. For a
        provider breaker that is ``cooldown_until``. Without it the backoff
        and the breaker were independent clocks and the item burned its
        attempts inside the cooldown; see :meth:`WorkQueue.fail`.

        ``attempted`` -- whether an invocation actually happened. When routing
        refuses because every provider is cooling, nothing was tried, and
        charging an attempt for being turned away at the door is how three
        attempts become zero real tries. Those are *deferred* instead:
        rescheduled past the cooldown with the attempt refunded, bounded by
        ``max_parks`` so an outage that never ends still terminates.
        """

        retry_at = getattr(exc, "retry_at", None) if exc is not None else None
        attempted = bool(getattr(exc, "attempted", True)) if exc is not None else True
        if retry_at is None and failure_class in PROVIDER_FAILURES:
            # **The deadline is looked up, not only inherited.**
            #
            # The exception is the better source when it has one -- the router
            # knows exactly which breaker it hit. But an exception is not a
            # reliable channel: an action handler that catches a provider
            # error and reports it, or a broad `except ResearchOSError` two
            # frames up, produces a failure with the right *class* and no
            # deadline, and `perform_action` then re-raises a fresh exception
            # that never had one. A test audit found several such paths, and
            # the consequence was D2 intact: retried on the linear backoff,
            # inside a cooldown, until the attempts ran out.
            #
            # The breaker's state is in the database either way, so ask it.
            # That makes cooldown-aware scheduling a property of the failure
            # *class* rather than of how carefully each of nine handlers
            # preserved an exception.
            retry_at = self._provider_recovery_deadline()
        deferrable = (
            not attempted
            and failure_class in PROVIDER_FAILURES
            and retry_at is not None
        )

        if deferrable and retry_at is not None:
            # The deadline is handed to the queue as a timestamp and compared
            # there. Converting it to a delay here would mean subtracting a
            # database timestamp from this worker's clock, which `clock.py`
            # forbids for exactly this kind of value -- and the first version
            # of this path did it, producing a nine-month deferral under the
            # frozen clock the tests inject.
            #
            # No check that the cooldown is still in the future either: if it
            # has already lifted, the ordinary backoff floor applies and the
            # item comes back in a class-appropriate interval. One branch
            # fewer, and the branch removed was the one that needed a clock.
            try:
                parked = self._queue.wait_for_external(
                    item.work_id,
                    owner=self._owner,
                    detail=error,
                    retry_after_seconds=retry_delay_seconds(failure_class, attempt=1),
                    failure_class=failure_class,
                    not_before=retry_at,
                )
            except LeaseLostError:
                report.notes.append(f"{item.work_id}: deferred after losing its lease")
                return
            if parked.status is not WorkStatus.FAILED:
                report.work_deferred += 1
                self._record_work_event(
                    item,
                    kind="WORK_DEFERRED",
                    payload={
                        "failure_class": str(failure_class),
                        "error": error[:1000],
                        "retry_at": retry_at.isoformat(),
                    },
                    dedup_key=f"work-deferred:{item.work_id}:{retry_at.isoformat()}",
                )
                report.notes.append(
                    f"{item.work_id} ({item.kind}) waits for the provider until "
                    f"{retry_at.isoformat()}; no attempt was charged because none "
                    f"could be made"
                )
                return
            # It ran out of parks. Fall through and report it as a failure,
            # which is what the row now says it is.
            report.work_failed += 1
            self._record_work_event(
                item,
                kind="WORK_FAILED",
                payload={"failure_class": str(failure_class), "error": error[:1000]},
                dedup_key=f"work-failed:{item.work_id}:{item.attempts}",
            )
            return

        try:
            self._queue.fail(
                item.work_id,
                owner=self._owner,
                failure_class=failure_class,
                error=error,
                not_before=retry_at,
            )
        except LeaseLostError:
            report.notes.append(f"{item.work_id}: failed after losing its lease")
            return
        report.work_failed += 1
        self._record_work_event(
            item,
            kind="WORK_FAILED",
            payload={"failure_class": str(failure_class), "error": error[:1000]},
            dedup_key=f"work-failed:{item.work_id}:{item.attempts}",
        )

    def _provider_recovery_deadline(self) -> datetime | None:
        """When the providers come back, or ``None`` if one is already usable.

        ``None`` when any provider can be routed to now: there is nothing to
        wait for, and delaying would be inventing a reason. Otherwise the
        earliest moment a breaker lifts, which is the soonest a retry could
        possibly succeed.
        """

        names = tuple(health.provider for health in self._store.provider_health())
        if not names or self._store.usable_providers(names):
            return None
        cooling = self._store.provider_cooldowns(names)
        return min(cooling.values()) if cooling else None

    def _record_work_event(
        self,
        item: WorkItem,
        *,
        kind: str,
        payload: dict[str, Any],
        dedup_key: str,
    ) -> None:
        """Record what happened to a work item, run or no run.

        The ``run_id`` guard this replaces is why the 2026-09-19 incident was
        hard to read from the ledger. The ``advance_objective`` item that
        exhausted three attempts on the provider outage carries no run -- it
        is about a project, not a cycle -- so the one work item that explains
        how five parked objectives became two emitted no event at all. The
        three cycle failures around it did, which made the trail look
        complete.
        """

        self._store.record_event(
            kind=kind,
            project_id=item.project_id,
            run_id=item.run_id,
            work_id=item.work_id,
            payload={"kind": item.kind, **payload},
            dedup_key=dedup_key,
        )

    @staticmethod
    def _classify(exc: ResearchOSError) -> FailureClass:
        """Map a domain exception onto the taxonomy.

        Kept small and explicit. An exception with no mapping is ``UNKNOWN``,
        which the policy table makes terminal -- so an unclassified failure
        stops rather than looping, and shows up as something to classify.

        An exception that *states* its class is believed, and that is checked
        before the table. The table maps a type to a class, which works while
        one type means one thing; :class:`ProviderCallFailedError` means a
        timeout or an outage depending on what the provider did, and only the
        router knows which. Flattening both to ``PROVIDER_UNAVAILABLE`` would
        have given a timeout the wrong backoff.
        """

        declared = getattr(exc, "failure_class", None)
        if isinstance(declared, FailureClass):
            return declared

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
            # Last, because several of the classes above are `CapsuleError`
            # siblings rather than subclasses and the order of this tuple is
            # first-match. A capsule that cannot be read is the most common
            # genuinely-unclassified failure in this system: `kernel.frontier()`
            # raises it, and the frontier is consulted at the start of every
            # cycle. Without this it failed as UNKNOWN -- terminal, correctly,
            # but reported as "something we have not classified" rather than
            # "your capsule does not parse", which is a message a researcher can
            # act on in a second.
            #
            # MISSING_SCIENTIFIC_AUTHORITY rather than a new class: the response
            # is INTERRUPT_FOR_HUMAN, which is exactly right. A broken capsule is
            # not something the runtime may repair -- that would be writing
            # canonical scientific state -- so the only correct response is to
            # stop and tell the person.
            (CapsuleError, FailureClass.MISSING_SCIENTIFIC_AUTHORITY),
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

        # **What the run concluded, from the run.**
        #
        # This used to read `item.payload["recommendation"]`, which is a copy of
        # the `RESEARCH_CYCLE_FINISHED` payload, which is a copy of what the
        # graph returned. Two messages away from the row that concluded it, in a
        # queue row that outlives the process, the build, and any later
        # correction to how the conclusion is reached.
        #
        # The live thesis runtime showed what that costs.
        # `RRUN-20260918T054218Z-cb4962f4` asked the frontier role whether
        # another cycle was warranted, was told `WAIT_HUMAN`, and recorded that
        # in a finding -- but the build of the day reached `WAIT_HUMAN` only
        # through `requires_human_promotion`, so the cycle concluded
        # `START_NEXT_CYCLE`. `conclude` now honours the frontier's own
        # recommendation, and that fixed nothing for the work item already on
        # the queue: it still said `START_NEXT_CYCLE`, and on the next restart
        # it would still have been believed.
        #
        # So the payload is advisory and the row is authoritative. The fallback
        # is for a run finished before the column existed, where null means "we
        # do not know" rather than "nothing was recommended" -- and the result
        # says which source was used, because a continuation decision made from
        # a message rather than from a run is a thing an auditor should be able
        # to see.
        durable = run.next_recommendation
        carried = str(item.payload.get("recommendation") or "")
        recommendation = durable if durable is not None else carried
        source = "run record" if durable is not None else "event payload"
        overruled = durable is not None and carried and carried != durable
        if overruled:
            LOG.info(
                "%s carried recommendation %r; %s concluded %r, which wins",
                item.work_id,
                carried,
                run_id,
                durable,
            )

        def outcome(**fields: Any) -> dict[str, Any]:
            """This item's result, with what the decision was made on.

            ``parent_recommendation`` rather than ``recommendation``, because
            on the success path ``_cycle_result_payload`` already puts the
            *successor's* recommendation under that key. Two different facts
            under one name is how a reader ends up auditing the wrong cycle,
            and the first version of this helper did exactly that: a refusal
            reported the parent's conclusion and a success silently reported
            the child's, with ``parent_recommendation_source`` describing the
            parent in both.
            """

            record: dict[str, Any] = {
                "parent_recommendation": recommendation,
                "parent_recommendation_source": source,
                **fields,
            }
            if overruled:
                record["superseded_payload_recommendation"] = carried
            return record

        # At most one successor per parent, checked before the work rather than
        # only at the insert.
        #
        # `_work_advance_objective` has had this since
        # `sql/0014_one_successor_per_run.sql` and this handler did not, which
        # made the two disagree about the same invariant. The live runtime found
        # the gap: a researcher cancelled the successor this item had opened,
        # the item's lease expired, and a restart would have reclaimed it,
        # re-run it, and hit `research_runs_one_successor_idx` as an
        # *exception* -- three failed attempts and a dead-lettered work item,
        # for a system behaving exactly as intended.
        #
        # A cancelled successor counts. Cancelling a research cycle is a
        # person's act, and a queue row is not entitled to undo it by being
        # retried. The index makes this a pre-check rather than the guard: two
        # concurrent passes can both read False, and the loser raises
        # `SuccessorExistsError` at the insert below.
        if self._store.has_successor(run_id):
            return outcome(
                continued=False,
                reason=(
                    f"{run_id} already has a successor. A parent run has at "
                    "most one, and a successor that was cancelled stays "
                    "cancelled: reopening it would be this queue row undoing a "
                    "decision a person made."
                ),
            )

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
            return outcome(continued=False, reason=why)

        try:
            successor = start_cycle(
                config=self._config,
                db=self._db,
                project_id=run.project_id,
                repo_path=repo,
                objective=run.objective,
                # A factory, not a provider: the router records provenance
                # against a run id, and the successor's does not exist until
                # `open_cycle` has created it.
                models=lambda successor_id: self._models(
                    successor_id, run.project_id, item.work_id
                ),
                autonomy=Autonomy(str(run.autonomy)),
                parent_run_id=run.run_id,
                cycle_index=run.cycle_index + 1,
            )
        except SuccessorExistsError as exc:
            # The race the pre-check cannot close, closed by the database.
            # Losing it is not a failure of this work item: the successor
            # exists, which is what this item was for.
            return outcome(continued=False, reason=str(exc))
        return outcome(
            continued=True,
            reason=why,
            successor_run_id=successor.run.run_id,
            **self._cycle_result_payload(successor),
        )

    def _work_advance_objective(self, item: WorkItem) -> dict[str, Any]:
        """Continue a parked objective after a person changed the science.

        The other half of §8, and the half that has to be careful. Its job is to
        answer "does this scientific change give a parked objective something
        new to do", and to answer it *no* whenever it can, because every yes
        costs model calls.

        Eligibility, all of which must hold:

        1. the objective's latest cycle has **finished**. A cycle that is still
           waiting on an approval is waiting for a different answer, and its
           thread has a live interrupt in it -- opening a successor would leave
           two threads for one objective. Those are woken by
           ``SCIENTIFIC_DECISION_RECORDED``, not by this.
        2. it has **no successor already**. Two observations of one change
           cannot produce two cycles; the work item's dedup key makes that
           unlikely and this makes it impossible.
        3. the **frontier actually moved**, and moved *since this cycle recorded
           one*. Checked by `should_continue` against the frontier this pass
           measured, which is passed in explicitly -- see the comment at the
           call. A capsule change that leaves the unresolved work identical (a
           charter rewrite, a sharpened statement) is recorded as an event and
           opens no cycle, because a successor over an identical frontier is
           the seven-cycle pilot in ``docs/RUNTIME.md`` §16 again. A frontier
           that could not be *computed* is also not a change: unknown is not
           changed.
        4. ``should_continue``'s other bounds permit it: lineage depth against
           ``max_cycles_per_objective``, and the project budget.

        A refusal returns why. "Nothing happened and the log says nothing" is
        how the missing continuation looked from the outside before this
        existed, and it is not an improvement to reproduce that with a
        different cause.
        """

        project_id = item.project_id
        digest = str(item.payload.get("capsule_digest") or "")
        frontier = str(item.payload.get("frontier_digest") or "")
        parked = self._store.parked_objectives(project_id=project_id)
        if not parked:
            return {
                "advanced": False,
                "reason": (
                    "no objective for this project is parked waiting for a "
                    "scientific decision"
                ),
                "capsule_digest": digest,
            }

        advanced: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        blocked: list[dict[str, str]] = []
        #: The first infrastructure failure seen, kept so the item can be
        #: retried against the right deadline after every objective has had
        #: its turn. See the re-raise at the end of this handler.
        blocker: BaseException | None = None
        for run in parked:
            previous = CycleResult(
                run=run,
                status=run.status,
                terminal_state=run.terminal_state,
                pending_approval_id=None,
                # The recommendation `should_continue` needs. Supplied by this
                # handler rather than read from the parked run, because the
                # parked run's own recommendation was "stop, a person is
                # needed" -- and a person has now acted. That is the whole
                # point of this pass, and it is the one place the runtime
                # overrides a previous cycle's conclusion. It is allowed to,
                # because the thing the conclusion was waiting for happened.
                recommendation="START_NEXT_CYCLE",
                notes=(),
                state={},
            )
            # The measured frontier, passed explicitly. Without it
            # `should_continue` compares this parked cycle's digest against its
            # *parent's* -- which asks "did that old cycle learn anything", and
            # the answer is no, which is why it stopped and waited. It therefore
            # refused the successor at the exact moment the wait had ended. The
            # first closed-loop pilot against the real CCAO capsule caught it:
            # CAPSULE_CHANGED fired, the advance ran, and no cycle opened.
            # A cheap pre-check, not the guard. Two concurrent advances can
            # both read False here; `research_runs_one_successor_idx` is what
            # makes only one of them succeed, and the loser raises
            # `SuccessorExistsError` at the insert below. This used to call
            # `lock_run` first, which took an advisory lock and released it
            # before returning -- see `sql/0014_one_successor_per_run.sql`.
            if self._store.has_successor(run.run_id):
                skipped.append(
                    {
                        "run_id": run.run_id,
                        "reason": (
                            "another pass opened this objective's successor "
                            "while this one was deciding"
                        ),
                    }
                )
                continue
            proceed, why = should_continue(
                db=self._db,
                config=self._config,
                result=previous,
                observed_frontier=frontier,
            )
            if not proceed:
                skipped.append({"run_id": run.run_id, "reason": why})
                continue
            try:
                successor = start_cycle(
                    config=self._config,
                    db=self._db,
                    project_id=project_id,
                    repo_path=self._repo_for(project_id),
                    objective=run.objective,
                    models=lambda successor_id: self._models(
                        successor_id, project_id, item.work_id
                    ),
                    autonomy=Autonomy(str(run.autonomy)),
                    parent_run_id=run.run_id,
                    cycle_index=run.cycle_index + 1,
                )
            except SuccessorExistsError as exc:
                # The race the pre-check above cannot close, closed here by the
                # database. A lost race is not a failure of this work item.
                skipped.append({"run_id": run.run_id, "reason": str(exc)})
                continue
            except ResearchOSError as exc:
                # **Every other failure, isolated to the objective it belongs
                # to.**
                #
                # This used to be absent, and the shape of that absence is the
                # reason this handler is on the list. `start_cycle` runs a
                # whole research cycle, so anything a cycle can hit, this loop
                # can hit: a provider outage, a capsule that stopped parsing, a
                # repository lock. One of those in the middle of the loop
                # abandoned every objective after it -- not skipped with a
                # reason, not deferred, not logged. They were simply never
                # reached, and the result recorded the ones before the failure
                # as though the list had ended there.
                #
                # On 2026-09-19 the fourth of five parked objectives hit an
                # open provider breaker and the fifth was never evaluated. From
                # the outside that is indistinguishable from "the fifth was not
                # eligible", which is the one thing it must never be confused
                # with.
                #
                # So each objective gets its own disposition, and the loop
                # continues. Infrastructure failures are additionally
                # remembered, because they mean "not yet" rather than "no" and
                # the item must come back -- see the re-raise below.
                failure_class = self._classify(exc)
                blocked.append(
                    {
                        "run_id": run.run_id,
                        "failure_class": str(failure_class),
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                if blocker is None and is_infrastructure(failure_class):
                    blocker = exc
                LOG.warning(
                    "advancing %s failed (%s); continuing with the rest",
                    run.run_id,
                    failure_class,
                )
                continue
            advanced.append(
                {
                    "parent_run_id": run.run_id,
                    "successor_run_id": successor.run.run_id,
                    # What this pass overrode, recorded.
                    #
                    # This handler supplies `START_NEXT_CYCLE` itself rather
                    # than reading the parked run, which is correct and is the
                    # one place the runtime overrules a previous cycle's
                    # conclusion -- it is allowed to, because the thing that
                    # conclusion was waiting for happened. An audit pointed out
                    # that `continue_objective` now records which source its
                    # decision came from and this one recorded nothing, so the
                    # *deliberate* override was the invisible one. Every parked
                    # run in production carries `WAIT_HUMAN` here.
                    "parent_recommendation": run.next_recommendation or "",
                    "parent_recommendation_source": ("overridden by a capsule change"),
                    **self._cycle_result_payload(successor),
                }
            )
        # Every objective's disposition, durably, before anything is raised.
        #
        # I6 asks that no objective be silently dropped, and a result dict is
        # not an answer to that: a work item that raises has no result. The
        # event ledger is where a disposition survives a failed attempt, and
        # the dedup key -- objective, capsule digest -- means replaying the
        # same observation records it once rather than once per attempt.
        for disposition, entries in (
            ("ADVANCED", advanced),
            ("STILL_PARKED", skipped),
            ("FAILED_TO_ADVANCE", blocked),
        ):
            for entry in entries:
                parent = str(entry.get("parent_run_id") or entry.get("run_id") or "")
                self._store.record_event(
                    kind="OBJECTIVE_DISPOSITION",
                    project_id=project_id,
                    run_id=parent or None,
                    work_id=item.work_id,
                    payload={
                        "disposition": disposition,
                        "capsule_digest": digest,
                        "reason": str(entry.get("reason") or "")[:1000],
                        "successor_run_id": entry.get("successor_run_id"),
                        "failure_class": entry.get("failure_class"),
                    },
                    dedup_key=f"disposition:{parent}:{digest[:16]}",
                )

        if blocker is not None:
            # Raised after every objective has been evaluated, not instead of
            # evaluating them. The ones that advanced are durable -- their
            # successors exist and `has_successor` skips them -- so the retry
            # this raise produces resumes where this pass stopped rather than
            # starting over. That is what keeps I6 and I7 from pulling against
            # each other: isolation does not cost idempotence.
            raise blocker

        return {
            "advanced": bool(advanced),
            "capsule_digest": digest,
            "successors": advanced,
            "skipped": skipped,
            "blocked": blocked,
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
        "work_deferred",
        "runs_rescheduled",
        "runs_abandoned",
        "leases_reclaimed",
        "invocations_abandoned",
        "interpretations_abandoned",
        "reservations_released",
        "jobs_polled",
        "schedules_fired",
        "checkpoints_pruned",
        "approvals_surfaced",
        "capsules_observed",
        "capsule_changes",
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
    from research_os.automation.config import load_config as load_automation_config
    from research_os.automation.config import resolve_roles
    from research_os.automation.providers import probe_registry
    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.routing import ModelRouter, profiles_from_adapters

    registry = dict(adapters) if adapters is not None else provider_registry()
    profiles = profiles_from_adapters(registry)
    store = RuntimeStore(db)

    # The researcher's `automation.yaml` roles, resolved against the providers
    # this machine actually has. Without this the runtime passed no `--model`
    # and no `--effort` on any call, so the provider CLI's own default answered
    # -- including for the three roles that map onto the v1 planner, whose
    # default v1.1 changed from `sonnet` to `opus` on measured evidence.
    #
    # Failing softly is deliberate. `resolve_roles` raises when no provider is
    # available at all, and a daemon that could not start because of it would be
    # worse than one that runs with provider defaults: `runtime doctor` is where
    # "you have no provider" belongs, and every model call will fail with its own
    # clear message anyway.
    role_settings: dict[str, Any] = {}
    substitutions: tuple[str, ...] = ()
    try:
        automation_config = load_automation_config()
        resolved = resolve_roles(automation_config, probe_registry(registry))
        role_settings = dict(resolved.roles)
        substitutions = resolved.substitutions
    except ResearchOSError as exc:
        LOG.warning(
            "could not resolve the configured model roles, so every call will "
            "use its provider's default model: %s",
            exc,
        )
    for note in substitutions:
        # Surfaced rather than swallowed: a researcher who configured a planner
        # model on a provider this machine does not have gets the substitution
        # said out loud, because the run report records the model that answered
        # and not the alias that was asked for.
        LOG.warning("model role substitution: %s", note)

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
            role_settings=role_settings,
            require_independence=config.settings.review_independence == "require",
            failure_threshold=config.settings.provider_failure_threshold,
            cooldown_seconds=config.settings.provider_cooldown_seconds,
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

    # One control plane per operational database, held for the whole run.
    #
    # Not a correctness guard -- work claiming is `for update skip locked`,
    # leases expire and are recovered, and event ingestion is deduplicated, so
    # two daemons would compete rather than corrupt. It is what makes *starting*
    # idempotent, which is what a service manager needs: `systemctl --user
    # start researchd` when one is already running, or a manual `researchd`
    # beside an enabled unit, now says so and exits zero instead of quietly
    # running a second loop against the same rows. The lock lives on the
    # connection, so it is released when this process dies however it dies.
    # The database outlives the lock: `daemon_lock` releases by running
    # `pg_advisory_unlock` on a pooled connection, so closing the pool inside
    # the block would make every clean shutdown log a failed unlock.
    try:
        try:
            with daemon_lock(database):
                LOG.info("researchd starting; database %s", redact_dsn(config.dsn))
                _install_signal_handlers(daemon)
                if args.once:
                    report = daemon.tick()
                    LOG.info("one pass: %s", report.payload())
                else:
                    total = daemon.run_forever(max_ticks=args.max_ticks)
                    LOG.info("researchd stopping: %s", total.payload())
        finally:
            database.close()
    except RepositoryBusyError:
        print(
            "A Research OS control plane is already attached to "
            f"{redact_dsn(config.dsn)}. Nothing was started, and the running "
            "one is unaffected. `researchctl runtime status` shows what it is "
            "doing; `systemctl --user status researchd` shows whether it is a "
            "service.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_OK) from None
    raise SystemExit(EXIT_OK)


def _install_signal_handlers(daemon: object) -> None:
    """SIGINT and SIGTERM ask the loop to finish its current pass."""

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        LOG.info("signal %s received; finishing the current pass", signum)
        daemon.stop()  # type: ignore[attr-defined]

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_name, handle_signal)
