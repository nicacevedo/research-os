"""Reads and writes for the operational tables that are not the queue.

Projects, runs, events, approvals, model-call provenance, external jobs and
provider health. The queue is in :mod:`research_os.runtime.queue`, budgets in
:mod:`research_os.runtime.budgets`, and the idempotency ledger in
:mod:`research_os.runtime.idempotency`, because each of those has semantics
worth reading on its own rather than semantics buried in a hundred-method class.

Two properties are enforced here rather than left to callers.

**Events are append-only and deduplicated.** :meth:`RuntimeStore.record_event`
takes an optional ``dedup_key`` and returns the existing event when one is
already there. A retried handler therefore cannot double-count, and "did this
happen twice" is answerable from the table rather than by reasoning about
delivery.

**A run's status transitions are checked.** Not as an elaborate state machine --
the runtime is allowed to move a run between waiting states freely -- but a
terminal run is terminal. :meth:`RuntimeStore.set_run_status` refuses to move a
``SUCCEEDED`` run back to ``RUNNING``, because the thing that tries to do that
is always a resumed worker acting on stale state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.db import Database, RuntimeDatabaseError, jsonb
from research_os.runtime.findings import FindingKind, FindingRefKind, RuntimeFinding
from research_os.runtime.ids import (
    new_approval_id,
    new_event_id,
    new_external_job_id,
    new_finding_id,
    new_interpretation_id,
    new_model_call_id,
    new_run_id,
    new_schedule_id,
    thread_id_for,
)
from research_os.runtime.models import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_RUN_STATUSES,
    Approval,
    ApprovalStatus,
    Autonomy,
    Event,
    ExperimentInterpretation,
    ExternalJob,
    ExternalJobStatus,
    InterpretationStatus,
    ModelCall,
    ModelCallStatus,
    Project,
    ProposalReservationStatus,
    ProviderHealth,
    ResearchRun,
    RunKind,
    RunStatus,
    Schedule,
    StrandedRun,
    TerminalState,
)

LOG = logging.getLogger("research_os.runtime.store")

RUN_COLUMNS = (
    "run_id, project_id, objective, status, terminal_state, next_recommendation, "
    "autonomy, run_kind, parent_run_id, cycle_index, thread_id, detail, "
    "frontier_digest, created_at, started_at, finished_at, updated_at"
)
#: The same list, prefixed for a query that joins. A bare ``status`` beside a
#: work item's ``status`` is ambiguous, and PostgreSQL says so rather than
#: guessing -- which is the good outcome, but only once.
_QUALIFIED_RUN_COLUMNS = ", ".join(
    f"r.{column.strip()}" for column in RUN_COLUMNS.split(",")
)

#: The active external-job statuses, as a SQL literal list, derived from the
#: enum rather than written out.
#:
#: Written out by hand once, and it was wrong in both directions: it named
#: ``QUEUED``, which is not a member and which the table's check constraint
#: rejects, so that third of the test could never match; and it omitted
#: ``SUBMITTING``, ``PENDING`` and ``UNKNOWN``, which are the statuses a
#: cluster job actually sits in while it waits. A run whose Slurm job was
#: pending would have been diagnosed as stranded and re-entered. An
#: independent security review found it. Derived now, so the two cannot drift
#: again.
_ACTIVE_JOB_SQL = ", ".join(f"'{status}'" for status in sorted(ACTIVE_JOB_STATUSES))
EVENT_COLUMNS = "event_id, project_id, run_id, work_id, kind, payload, dedup_key, created_at, consumed_at"
APPROVAL_COLUMNS = (
    "approval_id, run_id, project_id, kind, question, packet, status, decision, "
    "decided_by, thread_id, interrupt_key, requested_at, decided_at, applied_at"
)
JOB_COLUMNS = (
    "job_id, run_id, work_id, project_id, executor, scheduler_job_id, spec_digest, "
    "run_dir, status, failure_class, exit_code, detail, contained, containment, "
    "wall_clock_seconds, submitted_at, last_polled_at, finished_at"
)
MODEL_CALL_COLUMNS = (
    "call_id, run_id, project_id, work_id, invocation_id, provider, model, role, "
    "criticality, "
    "independence_group, independence, independence_note, prompt_version, "
    "input_digest, output_artifact_id, tokens_in, tokens_out, cost_usd, latency_ms, "
    "status, error, created_at"
)
FINDING_COLUMNS = (
    "finding_id, project_id, kind, summary, excerpt, source_run_id, source_cycle, "
    "source_work_id, source_action, experiment_job_id, spec_digest, semantic_key, "
    "digest, created_at"
)
INTERPRETATION_COLUMNS = (
    "interpretation_id, job_id, project_id, run_id, work_id, spec_digest, "
    "interpreter_version, artifact_id, preregistration_artifact_id, status, "
    "detail, created_at, completed_at"
)
SCHEDULE_COLUMNS = (
    "schedule_id, project_id, kind, payload, interval_seconds, next_run_at, "
    "last_run_at, enabled, created_at"
)


class RuntimeStateError(ResearchOSError):
    """Raised when an operational record is missing or a transition is refused."""


#: The partial unique index that makes "one successor per run" a fact.
SUCCESSOR_INDEX = "research_runs_one_successor_idx"


def _violated_constraint(exc: BaseException) -> str:
    """The constraint or index name a unique violation names, or ``""``.

    Read from the driver's diagnostics rather than by searching the message
    text. A substring test over `str(exc)` also matches a *value* that happens
    to contain the name -- and an objective, a goal or a finding summary can
    contain anything, because a model wrote it.
    """

    for candidate in (exc, exc.__cause__, exc.__context__):
        diag = getattr(candidate, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if name:
            return str(name)
    return ""


class SuccessorExistsError(RuntimeStateError):
    """Raised when a parent run already has the successor a caller is opening.

    A lost race, not a defect. The caller's response is to skip this objective
    and report that another pass advanced it, which is the truth.
    """


_ELIGIBLE_JOB_SQL = """
select {columns} from external_jobs j
where j.project_id = %(project_id)s
  and j.status in ('COMPLETED','FAILED','TIMED_OUT','CANCELLED')
  and not exists (
      select 1 from experiment_interpretations i
      where i.job_id = j.job_id
        and i.interpreter_version = %(version)s
        and i.status = 'COMPLETED'
  )
order by j.finished_at nulls last, j.job_id
limit 1
{lock}
"""
"""The eligibility query, in one place so the locked and unlocked readers
cannot drift apart.

Two callers format it. :meth:`RuntimeStore.claim_next_interpretation` adds
``for update of j skip locked`` and is the one production readers reach:
`interpret_results` selects and claims in one transaction.
:meth:`RuntimeStore.eligible_job_for_interpretation` formats it with no lock
and answers the weaker question -- "is anything waiting" -- which is what the
interpretation tests assert against and what a read-only reporting caller would
want; no production path calls it today.

Sharing the predicate is the point. Written twice, the ordering and the
``not exists`` clause would drift, and the two questions would stop having the
same answer: a test asserting that a job is eligible would stop meaning that a
worker will take it.
"""


class RuntimeStore:
    """Operational reads and writes against one database."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db

    # ------------------------------------------------------------- projects --
    def upsert_project(
        self, *, project_id: str, repo_path: str, title: str | None = None
    ) -> Project:
        with self._db.tx() as conn:
            row = conn.execute(
                """
                insert into projects (project_id, repo_path, title)
                values (%(project_id)s, %(repo_path)s, %(title)s)
                on conflict (project_id) do update
                    set repo_path = excluded.repo_path,
                        title = coalesce(excluded.title, projects.title),
                        updated_at = now()
                returning project_id, repo_path, title, created_at, updated_at
                """,
                {"project_id": project_id, "repo_path": repo_path, "title": title},
            ).fetchone()
        return Project.model_validate(row)

    def get_project(self, project_id: str) -> Project | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "select project_id, repo_path, title, created_at, updated_at "
                "from projects where project_id = %s",
                (project_id,),
            ).fetchone()
        return Project.model_validate(row) if row else None

    def list_projects(self) -> tuple[Project, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "select project_id, repo_path, title, created_at, updated_at "
                "from projects order by project_id"
            ).fetchall()
        return tuple(Project.model_validate(row) for row in rows)

    # ------------------------------------------------------------------ runs --
    def create_run(
        self,
        *,
        project_id: str,
        objective: str,
        autonomy: Autonomy = Autonomy.HIGH,
        parent_run_id: str | None = None,
        cycle_index: int = 0,
        run_kind: RunKind = RunKind.CYCLE,
        thread_id: str | None = None,
    ) -> ResearchRun:
        """Open one bounded cycle.

        The LangGraph thread id is derived from the run id and stored, so the
        mapping between "a cycle" and "a checkpointed thread" is one to one and
        recorded, which is what makes checkpoint retention possible later.

        Raises :class:`SuccessorExistsError` when ``parent_run_id`` already has
        a successor. One parent, at most one successor, enforced by
        ``research_runs_one_successor_idx`` rather than by a lock the caller
        holds -- see ``sql/0014_one_successor_per_run.sql``.

        ``run_kind`` distinguishes an objective cycle from an idea-track stage.
        It defaults to ``CYCLE``, so nothing that predates the portfolio layer
        changes, and the two reconciliation queries that walk every run in a
        project filter on it -- see ``sql/0023_research_run_kind.sql`` for why
        a column rather than a convention on the objective text.

        ``thread_id`` overrides the derived one, for a run whose workflow is
        not the cycle graph. It stays unique per run either way.
        """

        run_id = new_run_id()
        try:
            return self._insert_run(
                run_id=run_id,
                project_id=project_id,
                objective=objective,
                autonomy=autonomy,
                parent_run_id=parent_run_id,
                cycle_index=cycle_index,
                run_kind=run_kind,
                thread_id=thread_id or thread_id_for(run_id),
            )
        except RuntimeDatabaseError as exc:
            # `research_runs_one_successor_idx`, not any unique violation: a
            # collision on the run id itself is a different fact and must not
            # be reported as "somebody else advanced this objective".
            if parent_run_id and _violated_constraint(exc) == SUCCESSOR_INDEX:
                raise SuccessorExistsError(
                    f"{parent_run_id} already has a successor; another pass "
                    f"advanced this objective"
                ) from exc
            raise

    def _insert_run(
        self,
        *,
        run_id: str,
        project_id: str,
        objective: str,
        autonomy: Autonomy,
        parent_run_id: str | None,
        cycle_index: int,
        run_kind: RunKind = RunKind.CYCLE,
        thread_id: str | None = None,
    ) -> ResearchRun:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into research_runs
                    (run_id, project_id, objective, autonomy, run_kind,
                     parent_run_id, cycle_index, thread_id)
                values (%(run_id)s, %(project_id)s, %(objective)s, %(autonomy)s,
                        %(run_kind)s, %(parent)s, %(cycle_index)s, %(thread_id)s)
                returning {RUN_COLUMNS}
                """,
                {
                    "run_id": run_id,
                    "project_id": project_id,
                    "objective": objective,
                    "autonomy": str(autonomy),
                    "run_kind": str(run_kind),
                    "parent": parent_run_id,
                    "cycle_index": cycle_index,
                    "thread_id": thread_id or thread_id_for(run_id),
                },
            ).fetchone()
        return ResearchRun.model_validate(row)

    def get_run(self, run_id: str) -> ResearchRun | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {RUN_COLUMNS} from research_runs where run_id = %s", (run_id,)
            ).fetchone()
        return ResearchRun.model_validate(row) if row else None

    def require_run(self, run_id: str) -> ResearchRun:
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeStateError(f"no such research run: {run_id}")
        return run

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> tuple[ResearchRun, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {RUN_COLUMNS} from research_runs
                where (%(project_id)s::text is null or project_id = %(project_id)s)
                  and (not %(active_only)s or status not in ('SUCCEEDED','FAILED','CANCELLED'))
                order by created_at desc, run_id desc
                limit %(limit)s
                """,
                {"project_id": project_id, "active_only": active_only, "limit": limit},
            ).fetchall()
        return tuple(ResearchRun.model_validate(row) for row in rows)

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        terminal_state: TerminalState | None = None,
        detail: str | None = None,
        next_recommendation: str | None = None,
    ) -> ResearchRun:
        """Move a run's status, refusing to revive a terminal one.

        A resumed worker acting on state it read before a crash is the only
        thing that ever attempts this, and letting it through would resurrect a
        cancelled run.

        ``next_recommendation`` is what the cycle concluded should happen next,
        and it is written **here** rather than in a second statement -- in the
        same row update that records that the run concluded, so a crash cannot
        leave a run terminal with no recorded conclusion. Before this column
        existed the recommendation lived only in the
        ``RESEARCH_CYCLE_FINISHED`` event payload and in the work-item payload
        copied from it, which is how a run whose frontier said ``WAIT_HUMAN``
        left behind a queued instruction saying ``START_NEXT_CYCLE``. See
        ``sql/0018_durable_next_recommendation.sql``.
        """

        is_terminal = status in TERMINAL_RUN_STATUSES
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update research_runs
                set status = %(status)s,
                    terminal_state = coalesce(%(terminal_state)s, terminal_state),
                    next_recommendation = coalesce(%(next_recommendation)s,
                                                   next_recommendation),
                    detail = coalesce(%(detail)s, detail),
                    started_at = coalesce(started_at,
                        case when %(status)s = 'RUNNING' then now() else null end),
                    finished_at = case when %(is_terminal)s then now() else finished_at end,
                    updated_at = now()
                where run_id = %(run_id)s
                  and status not in ('SUCCEEDED','FAILED','CANCELLED')
                returning {RUN_COLUMNS}
                """,
                {
                    "run_id": run_id,
                    "status": str(status),
                    "terminal_state": str(terminal_state) if terminal_state else None,
                    "next_recommendation": next_recommendation,
                    "detail": detail,
                    "is_terminal": is_terminal,
                },
            ).fetchone()
        if row is None:
            current = self.get_run(run_id)
            if current is None:
                raise RuntimeStateError(f"no such research run: {run_id}")
            raise RuntimeStateError(
                f"{run_id} is already {current.status}; refusing to move it to {status}"
            )
        return ResearchRun.model_validate(row)

    def set_thread_id(self, run_id: str, thread_id: str) -> None:
        """Name the workflow thread this run's graph executes in.

        Separate from :meth:`create_run` because a track's thread is derived
        from the run id, which does not exist until the row does. ``thread_id``
        is unique across runs, so this is also the guard against two runs
        claiming one thread.
        """

        with self._db.tx() as conn:
            conn.execute(
                "update research_runs set thread_id = %s, updated_at = now() "
                "where run_id = %s",
                (thread_id, run_id),
            )

    def set_frontier_digest(self, run_id: str, digest: str) -> None:
        """Record what the frontier looked like when this cycle concluded.

        So the *next* cycle's continuation decision can tell "we learned
        something" from "we ran again".
        """

        with self._db.tx() as conn:
            conn.execute(
                "update research_runs set frontier_digest = %s, updated_at = now() "
                "where run_id = %s",
                (digest, run_id),
            )

    def lineage_depth(self, run_id: str) -> int:
        """How many cycles this objective has already chained.

        Walked in SQL with a recursive CTE and a hard row cap, so a corrupted
        parent chain cannot become an infinite loop in the continuation policy.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                with recursive chain as (
                    select run_id, parent_run_id, 0 as depth
                    from research_runs where run_id = %s
                    union all
                    select r.run_id, r.parent_run_id, c.depth + 1
                    from research_runs r join chain c on r.run_id = c.parent_run_id
                    where c.depth < 1000
                )
                select max(depth) as depth from chain
                """,
                (run_id,),
            ).fetchone()
        return int(row["depth"] or 0) if row else 0

    # ---------------------------------------------------------------- events --
    def record_event(
        self,
        *,
        kind: str,
        project_id: str | None = None,
        run_id: str | None = None,
        work_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedup_key: str | None = None,
    ) -> tuple[Event, bool]:
        """Append one event. Returns ``(event, created)``.

        ``created`` is ``False`` when a ``dedup_key`` matched an existing event.
        Callers that must not act twice check it; callers that only wanted the
        record can ignore it.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into events (event_id, project_id, run_id, work_id, kind, payload, dedup_key)
                values (%(event_id)s, %(project_id)s, %(run_id)s, %(work_id)s, %(kind)s,
                        %(payload)s, %(dedup_key)s)
                on conflict (dedup_key) do nothing
                returning {EVENT_COLUMNS}
                """,
                {
                    "event_id": new_event_id(),
                    "project_id": project_id,
                    "run_id": run_id,
                    "work_id": work_id,
                    "kind": kind,
                    "payload": jsonb(payload or {}),
                    "dedup_key": dedup_key,
                },
            ).fetchone()
            if row is not None:
                return Event.model_validate(row), True
            existing = conn.execute(
                f"select {EVENT_COLUMNS} from events where dedup_key = %s", (dedup_key,)
            ).fetchone()
        if existing is None:  # pragma: no cover - only if the row vanished
            raise RuntimeStateError(f"event with dedup key {dedup_key!r} vanished")
        return Event.model_validate(existing), False

    def claim_events(
        self, *, limit: int = 50, owner: str = "", lease_seconds: int = 120
    ) -> tuple[Event, ...]:
        """Lease up to ``limit`` unconsumed events. Does **not** consume them.

        Consuming here and enqueueing the work in a second transaction lost the
        work permanently on a crash in between -- and nothing re-emits these
        events, so a run never started or a human's decision never resumed its
        cycle. An independent review found the comment that claimed otherwise.

        So this *leases*, exactly as the work queue does, and
        :meth:`consume_event` is called once the work row exists. An expired
        lease makes the event claimable again.

        ``skip locked`` so several ingest loops can share the stream.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                with due as (
                    select event_id from events
                    where consumed_at is null
                      and (claimed_at is null
                           or claimed_at < now() - make_interval(secs => %(lease)s))
                    order by created_at, event_id
                    for update skip locked
                    limit %(limit)s
                )
                update events e
                set claimed_at = now(), claimed_by = %(owner)s
                from due d where e.event_id = d.event_id
                returning {", ".join("e." + c.strip() for c in EVENT_COLUMNS.split(","))}
                """,
                {"limit": limit, "owner": owner or None, "lease": float(lease_seconds)},
            ).fetchall()
        return tuple(Event.model_validate(row) for row in rows)

    def consume_event(self, event_id: str) -> bool:
        """Mark one leased event handled. Returns ``False`` if it already was."""

        with self._db.tx() as conn:
            row = conn.execute(
                "update events set consumed_at = now() "
                "where event_id = %s and consumed_at is null returning event_id",
                (event_id,),
            ).fetchone()
        return row is not None

    def unconsumed_event_count(self) -> int:
        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from events where consumed_at is null"
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_events(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Event, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {EVENT_COLUMNS} from events
                where (%(run_id)s::text is null or run_id = %(run_id)s)
                  and (%(project_id)s::text is null or project_id = %(project_id)s)
                order by created_at desc, event_id desc limit %(limit)s
                """,
                {"run_id": run_id, "project_id": project_id, "limit": limit},
            ).fetchall()
        return tuple(Event.model_validate(row) for row in rows)

    # ------------------------------------------------------------- approvals --
    def request_approval(
        self,
        *,
        run_id: str,
        project_id: str,
        kind: str,
        question: str,
        packet: dict[str, Any],
        thread_id: str | None = None,
        interrupt_key: str | None = None,
    ) -> tuple[Approval, bool]:
        """Record that a human scientific decision is required.

        ``interrupt_key`` is unique, and that is what makes a replayed interrupt
        node safe: LangGraph re-executes a node from its beginning on resume, so
        the node asks for the same approval again and gets the same row back
        rather than opening a second identical question.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into approvals
                    (approval_id, run_id, project_id, kind, question, packet, thread_id, interrupt_key)
                values (%(approval_id)s, %(run_id)s, %(project_id)s, %(kind)s, %(question)s,
                        %(packet)s, %(thread_id)s, %(interrupt_key)s)
                on conflict (interrupt_key) do nothing
                returning {APPROVAL_COLUMNS}
                """,
                {
                    "approval_id": new_approval_id(),
                    "run_id": run_id,
                    "project_id": project_id,
                    "kind": kind,
                    "question": question,
                    "packet": jsonb(packet),
                    "thread_id": thread_id,
                    "interrupt_key": interrupt_key,
                },
            ).fetchone()
            if row is not None:
                return Approval.model_validate(row), True
            existing = conn.execute(
                f"select {APPROVAL_COLUMNS} from approvals where interrupt_key = %s",
                (interrupt_key,),
            ).fetchone()
        if existing is None:  # pragma: no cover
            raise RuntimeStateError(
                f"approval for interrupt {interrupt_key!r} vanished"
            )
        return Approval.model_validate(existing), False

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {APPROVAL_COLUMNS} from approvals where approval_id = %s",
                (approval_id,),
            ).fetchone()
        return Approval.model_validate(row) if row else None

    def list_approvals(
        self, *, run_id: str | None = None, pending_only: bool = True, limit: int = 50
    ) -> tuple[Approval, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {APPROVAL_COLUMNS} from approvals
                where (%(run_id)s::text is null or run_id = %(run_id)s)
                  and (not %(pending_only)s or status = 'PENDING')
                order by requested_at desc limit %(limit)s
                """,
                {"run_id": run_id, "pending_only": pending_only, "limit": limit},
            ).fetchall()
        return tuple(Approval.model_validate(row) for row in rows)

    def record_decision(
        self,
        approval_id: str,
        *,
        granted: bool,
        decision: dict[str, Any],
        decided_by: str,
        emit_event: bool = True,
    ) -> Approval:
        """Store a human's answer, and the event that resumes its cycle, atomically.

        One transaction, because two left a window an independent review found:
        a crash or a Ctrl-C between recording the decision and emitting the
        event left the approval decided and the run stalled in WAITING_HUMAN
        forever -- and unrecoverable by the researcher, because
        ``record_decision`` refuses an already-decided approval and nothing
        reconciles a decided-but-unresumed one.
        """

        status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DECLINED
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update approvals
                set status = %(status)s, decision = %(decision)s,
                    decided_by = %(decided_by)s, decided_at = now()
                where approval_id = %(approval_id)s and status = 'PENDING'
                returning {APPROVAL_COLUMNS}
                """,
                {
                    "approval_id": approval_id,
                    "status": str(status),
                    "decision": jsonb(decision),
                    "decided_by": decided_by,
                },
            ).fetchone()
            if row is not None and emit_event:
                conn.execute(
                    """
                    insert into events
                        (event_id, project_id, run_id, kind, payload, dedup_key)
                    values (%(event_id)s, %(project_id)s, %(run_id)s,
                            'SCIENTIFIC_DECISION_RECORDED', %(payload)s, %(dedup_key)s)
                    on conflict (dedup_key) do nothing
                    """,
                    {
                        "event_id": new_event_id(),
                        "project_id": row["project_id"],
                        "run_id": row["run_id"],
                        "payload": jsonb(
                            {
                                "approval_id": approval_id,
                                "granted": granted,
                                "kind": row["kind"],
                            }
                        ),
                        "dedup_key": f"decided:{approval_id}",
                    },
                )
        if row is None:
            current = self.get_approval(approval_id)
            if current is None:
                raise RuntimeStateError(f"no such approval: {approval_id}")
            raise RuntimeStateError(
                f"{approval_id} was already decided ({current.status}) at {current.decided_at}"
            )
        return Approval.model_validate(row)

    def mark_approval_applied(self, approval_id: str) -> bool:
        """Claim the right to act on a decision, exactly once.

        Returns ``True`` to exactly one caller. The node that applies an
        approved decision calls this first: if a resume replays that node, the
        second call returns ``False`` and the effect is not applied twice.

        Stamps ``applied_at`` and leaves ``status`` intact. An earlier version
        overwrote the status with ``APPLIED``, which destroyed the
        granted/declined distinction -- so a replayed ``await_decision`` read a
        *declined* gate back as granted. No authority was bypassed, because the
        second ``mark_approval_applied`` returned False and stopped the action,
        but the provenance said the opposite of what the researcher decided.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                update approvals set applied_at = now()
                where approval_id = %s
                  and status in ('GRANTED','DECLINED')
                  and applied_at is null
                returning approval_id
                """,
                (approval_id,),
            ).fetchone()
        return row is not None

    # ----------------------------------------------------------- model calls --
    def record_model_call(
        self,
        *,
        provider: str,
        role: str,
        status: ModelCallStatus,
        run_id: str | None = None,
        work_id: str | None = None,
        invocation_id: str | None = None,
        model: str | None = None,
        criticality: str = "normal",
        independence_group: str | None = None,
        independence: str | None = None,
        independence_note: str | None = None,
        prompt_version: str | None = None,
        input_digest: str | None = None,
        output_artifact_id: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: Decimal | float | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> ModelCall:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into model_calls
                    (call_id, run_id, project_id, work_id, invocation_id, provider,
                     model, role,
                     criticality, independence_group, independence, independence_note,
                     prompt_version, input_digest, output_artifact_id, tokens_in,
                     tokens_out, cost_usd, latency_ms, status, error)
                values (%(call_id)s, %(run_id)s,
                        (select project_id from research_runs
                          where run_id = %(run_id)s),
                        %(work_id)s, %(invocation_id)s, %(provider)s,
                        %(model)s, %(role)s, %(criticality)s, %(independence_group)s,
                        %(independence)s, %(independence_note)s,
                        %(prompt_version)s, %(input_digest)s, %(output_artifact_id)s,
                        %(tokens_in)s, %(tokens_out)s, %(cost_usd)s, %(latency_ms)s,
                        %(status)s, %(error)s)
                returning {MODEL_CALL_COLUMNS}
                """,
                {
                    "call_id": new_model_call_id(),
                    "run_id": run_id,
                    "work_id": work_id,
                    "invocation_id": invocation_id,
                    "provider": provider,
                    "model": model,
                    "role": role,
                    "criticality": criticality,
                    "independence_group": independence_group,
                    "independence": independence,
                    "independence_note": independence_note[:2000]
                    if independence_note
                    else None,
                    "prompt_version": prompt_version,
                    "input_digest": input_digest,
                    "output_artifact_id": output_artifact_id,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": Decimal(str(cost_usd))
                    if cost_usd is not None
                    else None,
                    "latency_ms": latency_ms,
                    "status": str(status),
                    "error": error[:4000] if error else None,
                },
            ).fetchone()
        return ModelCall.model_validate(row)

    def get_model_call(self, call_id: str) -> ModelCall | None:
        """One call by id.

        Added for the discovery portfolio's independence check, which compares
        a review's call against the call that produced the work. It was doing
        that by scanning the 500 most recent calls -- so past five hundred
        calls system-wide the origin was simply not found, the comparison fell
        through to its weakest answer, and every review afterwards recorded a
        weaker independence than it had. Silently, and invisibly to any test
        whose fixture makes ten calls.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"select {MODEL_CALL_COLUMNS} from model_calls where call_id = %s",
                (call_id,),
            ).fetchone()
        return ModelCall.model_validate(row) if row else None

    def list_model_calls(
        self, *, run_id: str | None = None, limit: int = 200
    ) -> tuple[ModelCall, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {MODEL_CALL_COLUMNS} from model_calls
                where (%(run_id)s::text is null or run_id = %(run_id)s)
                order by created_at desc limit %(limit)s
                """,
                {"run_id": run_id, "limit": limit},
            ).fetchall()
        return tuple(ModelCall.model_validate(row) for row in rows)

    # --------------------------------------------------------- external jobs --
    def create_external_job(
        self,
        *,
        project_id: str,
        executor: str,
        spec_digest: str,
        run_dir: str,
        run_id: str | None = None,
        work_id: str | None = None,
        job_id: str | None = None,
    ) -> ExternalJob:
        """Record a submission *before* the scheduler is told about it.

        The row exists in ``SUBMITTING`` while ``sbatch`` runs. A crash in that
        window leaves a row with no scheduler id, which the reconciler can find
        and investigate. The alternative -- submit first, record after -- leaves
        a job running on a cluster that nothing in this system knows about.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into external_jobs
                    (job_id, run_id, work_id, project_id, executor, spec_digest, run_dir)
                values (%(job_id)s, %(run_id)s, %(work_id)s, %(project_id)s, %(executor)s,
                        %(spec_digest)s, %(run_dir)s)
                returning {JOB_COLUMNS}
                """,
                {
                    "job_id": job_id or new_external_job_id(),
                    "run_id": run_id,
                    "work_id": work_id,
                    "project_id": project_id,
                    "executor": executor,
                    "spec_digest": spec_digest,
                    "run_dir": run_dir,
                },
            ).fetchone()
        return ExternalJob.model_validate(row)

    def update_external_job(
        self,
        job_id: str,
        *,
        status: ExternalJobStatus,
        scheduler_job_id: str | None = None,
        failure_class: str | None = None,
        exit_code: int | None = None,
        detail: str | None = None,
        contained: bool | None = None,
        containment: str | None = None,
        wall_clock_seconds: Decimal | float | None = None,
        polled: bool = False,
        allow_terminal_override: bool = False,
    ) -> ExternalJob:
        """Record what became of one job.

        Refuses to move a job that has already finished, unless explicitly
        overridden for a cancel. Two daemons polling one job is a real race --
        `squeue` is slow and their answers can arrive out of order -- and
        without the guard the slower answer reverted a COMPLETED job to RUNNING
        while keeping its `finished_at`, producing a self-contradictory
        provenance row that was then polled forever. Its finished event had
        already been emitted and deduped, so the real completion was never
        re-announced.
        """
        terminal = status in {
            ExternalJobStatus.COMPLETED,
            ExternalJobStatus.FAILED,
            ExternalJobStatus.CANCELLED,
            ExternalJobStatus.TIMED_OUT,
        }
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update external_jobs
                set status = %(status)s,
                    scheduler_job_id = coalesce(%(scheduler_job_id)s, scheduler_job_id),
                    failure_class = coalesce(%(failure_class)s, failure_class),
                    exit_code = coalesce(%(exit_code)s, exit_code),
                    detail = coalesce(%(detail)s, detail),
                    contained = coalesce(%(contained)s, contained),
                    containment = coalesce(%(containment)s, containment),
                    wall_clock_seconds = coalesce(
                        %(wall_clock_seconds)s, wall_clock_seconds
                    ),
                    last_polled_at = case when %(polled)s then now() else last_polled_at end,
                    finished_at = case when %(terminal)s then coalesce(finished_at, now()) else finished_at end
                where job_id = %(job_id)s
                  and (%(override)s
                       or status not in ('COMPLETED','FAILED','CANCELLED','TIMED_OUT'))
                returning {JOB_COLUMNS}
                """,
                {
                    "job_id": job_id,
                    "status": str(status),
                    "scheduler_job_id": scheduler_job_id,
                    "failure_class": failure_class,
                    "exit_code": exit_code,
                    "detail": detail,
                    "contained": contained,
                    "containment": containment,
                    "wall_clock_seconds": (
                        None
                        if wall_clock_seconds is None
                        else Decimal(str(wall_clock_seconds))
                    ),
                    "polled": polled,
                    "terminal": terminal,
                    "override": allow_terminal_override,
                },
            ).fetchone()
        if row is None:
            current = self.get_external_job(job_id)
            if current is None:
                raise RuntimeStateError(f"no such external job: {job_id}")
            # Already finished. Not an error: a slower poll arriving after a
            # faster one is ordinary, and the right response is to keep the
            # finished record.
            return current
        return ExternalJob.model_validate(row)

    def get_external_job(self, job_id: str) -> ExternalJob | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {JOB_COLUMNS} from external_jobs where job_id = %s", (job_id,)
            ).fetchone()
        return ExternalJob.model_validate(row) if row else None

    def active_external_jobs(
        self, *, limit: int = 100, poll_interval_seconds: float = 0.0
    ) -> tuple[ExternalJob, ...]:
        """Claim active jobs for polling, stamping ``last_polled_at`` up front.

        A claim rather than a read. Two daemons ticking together retrieved the
        identical set and both ran `squeue` against every job; stamping the
        timestamp inside the claim, under ``skip locked``, gives each job to one
        of them. ``poll_interval_seconds`` is how long a job is left alone after
        being polled, so the claim also paces the scheduler.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                with due as (
                    select job_id from external_jobs
                    where status in ('SUBMITTING','SUBMITTED','PENDING','RUNNING','UNKNOWN')
                      and (last_polled_at is null
                           or last_polled_at < now()
                              - make_interval(secs => %(interval)s))
                    order by last_polled_at nulls first, submitted_at
                    for update skip locked
                    limit %(limit)s
                )
                update external_jobs j set last_polled_at = now()
                from due d where j.job_id = d.job_id
                returning {", ".join("j." + c.strip() for c in JOB_COLUMNS.split(","))}
                """,
                {"limit": limit, "interval": float(poll_interval_seconds)},
            ).fetchall()
        return tuple(ExternalJob.model_validate(row) for row in rows)

    def list_external_jobs(
        self, *, run_id: str | None = None, limit: int = 100
    ) -> tuple[ExternalJob, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {JOB_COLUMNS} from external_jobs
                where (%(run_id)s::text is null or run_id = %(run_id)s)
                order by submitted_at desc limit %(limit)s
                """,
                {"run_id": run_id, "limit": limit},
            ).fetchall()
        return tuple(ExternalJob.model_validate(row) for row in rows)

    # -------------------------------------------- experiment interpretations --
    def eligible_job_for_interpretation(
        self, *, project_id: str, interpreter_version: str
    ) -> ExternalJob | None:
        """The oldest terminal job this reader version has not finished reading.

        Three things about this query are the whole point of the table it reads.

        **Oldest first, not newest.** The previous implementation ordered by
        ``finished_at desc`` and took one row, so which experiment got
        interpreted depended on the order the scheduler happened to reap two
        jobs in. Oldest-eligible-first is a stable total order over a set that
        only grows at one end, so two workers asking the same question at the
        same time get the same answer, and a backlog drains in the order it
        formed rather than newest-first forever.

        **``not exists`` against a COMPLETED row, not against any row.** An
        interpretation whose worker died mid-reading leaves an ``IN_PROGRESS``
        row, and that job is still owed an interpretation. Excluding it on the
        presence of *any* row would strand it permanently.

        **Ties broken by ``job_id``.** ``finished_at`` has second-or-better
        resolution and two jobs can share it; without the tiebreak the order is
        not total and the "stable" claim above would be false.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                _ELIGIBLE_JOB_SQL.format(columns=JOB_COLUMNS, lock=""),
                {"project_id": project_id, "version": interpreter_version},
            ).fetchone()
        return ExternalJob.model_validate(row) if row else None

    def claim_next_interpretation(
        self,
        *,
        project_id: str,
        interpreter_version: str,
        run_id: str | None = None,
        work_id: str | None = None,
        preregistration_for: Callable[[ExternalJob], str | None] | None = None,
    ) -> tuple[ExternalJob, ExperimentInterpretation, bool] | None:
        """Select the next job to interpret *and* claim it, in one transaction.

        :meth:`eligible_job_for_interpretation` and :meth:`claim_interpretation`
        are each correct and were called one after the other, which left the
        window between them open: two workers advancing the same project at the
        same time both selected the oldest eligible job, both did the whole
        reading, and the second one's work was discarded at the end because the
        unique constraint had already given the identity to the first. Nothing
        incorrect was recorded -- the reading is deterministic and
        :meth:`complete_interpretation` yields to whoever finished first -- but
        the duplicated work is real, and "two workers, one of them wasted" is
        not a property to leave in a scheduler that is allowed to run two
        workers.

        ``for update of j skip locked`` closes it. The second worker does not
        block on the locked job row and does not take it; it takes the *next*
        eligible one, or gets ``None``. The lock lives only as long as the
        transaction, which is exactly as long as it needs to: once the claim is
        committed, a later worker re-selecting the same job finds the existing
        row and gets ``created=False``, which is the crash-recovery path and
        must keep working.

        ``preregistration_for`` runs inside the transaction, between the lock
        and the insert, so the claim can name the preregistration it was
        resolved against. It reads the artifact store, not the database, so it
        does not nest a transaction. It is called at most once.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                _ELIGIBLE_JOB_SQL.format(
                    columns=JOB_COLUMNS, lock="for update of j skip locked"
                ),
                {"project_id": project_id, "version": interpreter_version},
            ).fetchone()
            if row is None:
                return None
            job = ExternalJob.model_validate(row)
            prereg = preregistration_for(job) if preregistration_for else None
            record, created = self._claim_interpretation(
                conn,
                job_id=job.job_id,
                project_id=project_id,
                spec_digest=job.spec_digest,
                interpreter_version=interpreter_version,
                run_id=run_id,
                work_id=work_id,
                preregistration_artifact_id=prereg,
            )
        return job, record, created

    def claim_interpretation(
        self,
        *,
        job_id: str,
        project_id: str,
        spec_digest: str,
        interpreter_version: str,
        run_id: str | None = None,
        work_id: str | None = None,
        preregistration_artifact_id: str | None = None,
    ) -> tuple[ExperimentInterpretation, bool]:
        """Claim the right to interpret one experiment. Returns ``(row, created)``.

        ``created=False`` means this ``(job, reader version)`` was already
        claimed -- by an earlier cycle, or by this one before a crash -- and the
        caller gets the existing row rather than a second one. The unique
        constraint does the work; ``on conflict do nothing`` plus a re-read is
        the whole API, exactly as the event ledger does it.

        A claim happens *before* anything is read, so the row exists to be
        reconciled if the process dies during the reading. That is the ordering
        the invocation ledger uses for external effects, applied here because a
        scientific interpretation is an effect too: the thing a later reader
        must not do is produce a second one.
        """

        with self._db.tx() as conn:
            return self._claim_interpretation(
                conn,
                job_id=job_id,
                project_id=project_id,
                spec_digest=spec_digest,
                interpreter_version=interpreter_version,
                run_id=run_id,
                work_id=work_id,
                preregistration_artifact_id=preregistration_artifact_id,
            )

    def _claim_interpretation(
        self,
        conn: Any,
        *,
        job_id: str,
        project_id: str,
        spec_digest: str,
        interpreter_version: str,
        run_id: str | None,
        work_id: str | None,
        preregistration_artifact_id: str | None,
    ) -> tuple[ExperimentInterpretation, bool]:
        """The claim itself, on a caller-supplied connection.

        Separate from :meth:`claim_interpretation` only so
        :meth:`claim_next_interpretation` can put the select and the insert in
        one transaction. The semantics are identical.
        """

        inserted = conn.execute(
            f"""
            insert into experiment_interpretations
                (interpretation_id, job_id, project_id, run_id, work_id,
                 spec_digest, interpreter_version, preregistration_artifact_id)
            values (%(interpretation_id)s, %(job_id)s, %(project_id)s, %(run_id)s,
                    %(work_id)s, %(spec_digest)s, %(version)s, %(prereg)s)
            on conflict (job_id, interpreter_version) do nothing
            returning {INTERPRETATION_COLUMNS}
            """,
            {
                "interpretation_id": new_interpretation_id(),
                "job_id": job_id,
                "project_id": project_id,
                "run_id": run_id,
                "work_id": work_id,
                "spec_digest": spec_digest,
                "version": interpreter_version,
                "prereg": preregistration_artifact_id,
            },
        ).fetchone()
        if inserted is not None:
            return ExperimentInterpretation.model_validate(inserted), True
        row = conn.execute(
            f"""
            select {INTERPRETATION_COLUMNS} from experiment_interpretations
            where job_id = %(job_id)s and interpreter_version = %(version)s
            """,
            {"job_id": job_id, "version": interpreter_version},
        ).fetchone()
        if row is None:  # pragma: no cover - the conflict implies a row exists
            raise RuntimeStateError(
                f"could not claim an interpretation of {job_id} at "
                f"{interpreter_version}"
            )
        return ExperimentInterpretation.model_validate(row), False

    def complete_interpretation(
        self,
        interpretation_id: str,
        *,
        artifact_id: str | None = None,
        detail: str | None = None,
    ) -> ExperimentInterpretation:
        """Record that the reading finished, once.

        Claimable from ``IN_PROGRESS`` **or** ``ABANDONED``, and that second one
        is the fix for a real loss an adversarial review executed.
        ``ABANDONED`` means "a reading was begun and its outcome is unknown" --
        which is precisely the state a worker completing its own reading is
        resolving. Guarding on ``IN_PROGRESS`` alone meant that a worker whose
        claim had been reaped for age while it was still working (the recovery
        pass has no way to tell a slow worker from a dead one) found its update
        matching nothing, and the artifact it had just written was attached to
        nothing. The job then stayed eligible, so every later cycle re-read the
        same experiment, re-wrote the same artifact, reported success, and never
        advanced.

        A ``COMPLETED`` row stays immutable: a replayed worker cannot overwrite
        the artifact of a finished interpretation with its own.

        And it no longer returns a row it did not complete. The previous version
        fell back to a bare re-read, so a caller was handed an ``ABANDONED`` row
        with ``artifact_id`` null and reported ``interpreted: True``. A
        ``COMPLETED`` row comes back as the recovery path needs; anything else
        raises, because "I could not record this reading" is a fact the caller
        has to handle rather than a value to interpret.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update experiment_interpretations
                set status = 'COMPLETED',
                    artifact_id = coalesce(%(artifact_id)s, artifact_id),
                    detail = coalesce(%(detail)s, detail),
                    completed_at = now()
                where interpretation_id = %(interpretation_id)s
                  and status in ('IN_PROGRESS', 'ABANDONED')
                returning {INTERPRETATION_COLUMNS}
                """,
                {
                    "interpretation_id": interpretation_id,
                    "artifact_id": artifact_id,
                    "detail": detail,
                },
            ).fetchone()
            if row is not None:
                return ExperimentInterpretation.model_validate(row)
            existing = conn.execute(
                f"select {INTERPRETATION_COLUMNS} from experiment_interpretations "
                f"where interpretation_id = %s",
                (interpretation_id,),
            ).fetchone()
        if existing is None:
            raise RuntimeStateError(f"no such interpretation: {interpretation_id}")
        record = ExperimentInterpretation.model_validate(existing)
        if record.status is InterpretationStatus.COMPLETED:
            # Somebody else finished it. Theirs is the interpretation.
            return record
        raise RuntimeStateError(  # pragma: no cover - the statuses are exhaustive
            f"{interpretation_id} is {record.status} and could not be completed"
        )

    def get_interpretation(
        self, *, job_id: str, interpreter_version: str
    ) -> ExperimentInterpretation | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                select {INTERPRETATION_COLUMNS} from experiment_interpretations
                where job_id = %(job_id)s and interpreter_version = %(version)s
                """,
                {"job_id": job_id, "version": interpreter_version},
            ).fetchone()
        return ExperimentInterpretation.model_validate(row) if row else None

    def list_interpretations(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ExperimentInterpretation, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {INTERPRETATION_COLUMNS} from experiment_interpretations
                where (%(project_id)s::text is null or project_id = %(project_id)s)
                  and (%(run_id)s::text is null or run_id = %(run_id)s)
                order by created_at desc, interpretation_id
                limit %(limit)s
                """,
                {"project_id": project_id, "run_id": run_id, "limit": limit},
            ).fetchall()
        return tuple(ExperimentInterpretation.model_validate(row) for row in rows)

    def abandon_stale_interpretations(
        self, *, older_than_seconds: float
    ) -> tuple[ExperimentInterpretation, ...]:
        """Mark long-running claims as ``ABANDONED`` so they can be resolved.

        Not re-claimed and not deleted. An abandoned row still holds the
        identity of the experiment whose reading was interrupted, and the
        recovery path needs it to find the artifact the dead worker may already
        have produced. Deleting it would turn a recoverable interruption into a
        second interpretation of the same experiment.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                update experiment_interpretations
                set status = 'ABANDONED',
                    detail = coalesce(detail, 'the worker stopped reporting')
                where status = 'IN_PROGRESS'
                  and created_at < now() - make_interval(secs => %(age)s)
                returning {INTERPRETATION_COLUMNS}
                """,
                {"age": float(older_than_seconds)},
            ).fetchall()
        return tuple(ExperimentInterpretation.model_validate(row) for row in rows)

    # ------------------------------------------------- runtime findings ----
    def record_finding(self, finding: RuntimeFinding) -> tuple[RuntimeFinding, bool]:
        """Store one noncanonical finding. Returns ``(finding, created)``.

        Deduplicated on ``(project_id, digest)``, so a deterministic repeat of
        the same observation returns the *existing* finding and its existing id.
        That is what makes a finding citable: the runtime recomputes an
        unchanged frontier on every cycle, and a new id per recomputation would
        give one fact seven identifiers over seven cycles.

        The references are written in the same transaction as the row. A finding
        whose edges landed separately could be cited while its provenance was
        still absent, and "cited but we cannot say what it rests on" is the one
        state this table exists to make impossible.
        """

        digest = finding.digest
        with self._db.tx() as conn:
            inserted = conn.execute(
                f"""
                insert into runtime_findings
                    (finding_id, project_id, kind, summary, excerpt, source_run_id,
                     source_cycle, source_work_id, source_action,
                     experiment_job_id, spec_digest, semantic_key, digest)
                values (%(finding_id)s, %(project_id)s, %(kind)s, %(summary)s,
                        %(excerpt)s, %(source_run_id)s, %(source_cycle)s,
                        %(source_work_id)s, %(source_action)s,
                        %(experiment_job_id)s, %(spec_digest)s, %(semantic_key)s,
                        %(digest)s)
                on conflict (project_id, digest) do nothing
                returning {FINDING_COLUMNS}
                """,
                {
                    "finding_id": new_finding_id(),
                    "project_id": finding.project_id,
                    "kind": str(finding.kind),
                    "summary": finding.summary,
                    "excerpt": finding.excerpt,
                    "source_run_id": finding.source_run_id,
                    "source_cycle": finding.source_cycle,
                    "source_work_id": finding.source_work_id,
                    "source_action": finding.source_action,
                    "experiment_job_id": finding.experiment_job_id,
                    "spec_digest": finding.spec_digest,
                    "semantic_key": finding.semantic_key,
                    "digest": digest,
                },
            ).fetchone()
            if inserted is None:
                row = conn.execute(
                    f"select {FINDING_COLUMNS} from runtime_findings "
                    f"where project_id = %s and digest = %s",
                    (finding.project_id, digest),
                ).fetchone()
                if row is None:  # pragma: no cover - the conflict implies a row
                    raise RuntimeStateError(
                        f"could not record or retrieve finding {digest[:12]}"
                    )
                return _finding_from(row), False
            finding_id = str(inserted["finding_id"])
            for kind, ref in finding.references():
                conn.execute(
                    "insert into runtime_finding_refs (finding_id, kind, ref) "
                    "values (%s, %s, %s) on conflict do nothing",
                    (finding_id, kind, ref),
                )
        return _finding_from(inserted), True

    def get_finding(self, finding_id: str) -> RuntimeFinding | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {FINDING_COLUMNS} from runtime_findings where finding_id = %s",
                (finding_id,),
            ).fetchone()
            if row is None:
                return None
            refs = conn.execute(
                "select kind, ref from runtime_finding_refs where finding_id = %s "
                "order by kind, ref",
                (finding_id,),
            ).fetchall()
        return _finding_from(row, refs)

    def last_refused_action(self, *, run_id: str) -> tuple[str, str] | None:
        """The most recent action of ``run_id`` that refused, and why.

        Exists because a successor cycle is a new LangGraph thread seeded only
        with identity, so everything the previous cycle learned by *being
        refused* was lost -- and the refusals in this system are the useful
        kind. Observed twice on real work on 2026-09-17: a planner chose
        ``run_local_experiment`` with no preregistered design, the action
        refused with "design_experiment must come first", and the successor
        planned the identical action; and an experimentalist supplied an
        absolute path for a ``path`` parameter, the declared-parameter check
        refused it naming the exact reason, and the successor supplied another
        absolute path.

        Read from `tool_invocations` rather than from graph state, because
        graph state is the thing the successor does not have. The invocation
        kind is ``cycle.<action>`` and the refusal is in its ``result``.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select kind, result from tool_invocations
                where run_id = %(run_id)s
                  and kind like 'cycle.%%'
                  and status = 'COMPLETED'
                  and result is not null
                  and (result->>'ok') = 'false'
                order by started_at desc
                limit 1
                """,
                {"run_id": run_id},
            ).fetchone()
        if row is None:
            return None
        action = str(row["kind"]).removeprefix("cycle.")
        detail = str((row["result"] or {}).get("detail") or "").strip()
        return action, detail

    def count_findings(
        self,
        *,
        project_id: str | None = None,
        exclude_actions: Sequence[str] = (),
    ) -> int:
        """How many findings this project has, without fetching any.

        `noncanonical_science` used to answer this with
        ``len(list_findings(limit=10_000))``, and `list_findings` fetched one
        refs row-set *per finding* -- so computing a number for a prompt cost up
        to 10 001 round trips, against a table that grows by one row per cycle.
        An independent review found it. `count_created_proposals` was already
        the pattern beside it.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(*) as total from runtime_findings
                where (%(project_id)s::text is null or project_id = %(project_id)s)
                  and (cardinality(%(exclude_actions)s::text[]) = 0
                       or source_action is null
                       or not (source_action = any(%(exclude_actions)s::text[])))
                """,
                {
                    "project_id": project_id,
                    "exclude_actions": list(exclude_actions),
                },
            ).fetchone()
        return int((row or {}).get("total") or 0)

    def list_findings(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        exclude_actions: Sequence[str] = (),
    ) -> tuple[RuntimeFinding, ...]:
        """Findings, newest first, bounded.

        ``exclude_actions`` filters in SQL rather than in the caller, and that
        is the whole reason it exists here. Filtering after the fetch would let
        the excluded rows consume slots of ``limit`` and hand the caller fewer
        findings than it asked for -- so a project with twelve prior frontier
        assessments would get an empty list rather than its twelve real
        findings, which is a worse failure than the one the exclusion is for.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {FINDING_COLUMNS} from runtime_findings
                where (%(project_id)s::text is null or project_id = %(project_id)s)
                  and (%(run_id)s::text is null or source_run_id = %(run_id)s)
                  and (cardinality(%(exclude_actions)s::text[]) = 0
                       or source_action is null
                       or not (source_action = any(%(exclude_actions)s::text[])))
                order by created_at desc, finding_id
                limit %(limit)s
                """,
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "limit": limit,
                    "exclude_actions": list(exclude_actions),
                },
            ).fetchall()
            if not rows:
                return ()
            # One query for every finding's references, not one per finding.
            # The previous shape was a round trip per row, which a review
            # measured at up to 10 001 for a single planner prompt.
            identifiers = [str(row["finding_id"]) for row in rows]
            grouped: dict[str, list[Any]] = {name: [] for name in identifiers}
            for ref in conn.execute(
                "select finding_id, kind, ref from runtime_finding_refs "
                "where finding_id = any(%s) order by kind, ref",
                (identifiers,),
            ).fetchall():
                grouped.setdefault(str(ref["finding_id"]), []).append(ref)
            found = [
                _finding_from(row, grouped[str(row["finding_id"])]) for row in rows
            ]
        return tuple(found)

    def resolve_findings(
        self, finding_ids: tuple[str, ...], *, project_id: str
    ) -> tuple[RuntimeFinding, ...]:
        """Load exactly these findings, refusing any that this project lacks.

        Project-scoped and fail-closed. A finding id from another project is
        refused rather than skipped, because a grounding allowlist assembled by
        silently dropping what it could not find is an allowlist that permits a
        proposal to rest on nothing.
        """

        found = {
            item.finding_id: item
            for item in (self.get_finding(value) for value in finding_ids)
            if item is not None and item.project_id == project_id
        }
        missing = [value for value in finding_ids if value not in found]
        if missing:
            raise RuntimeStateError(
                f"{project_id} has no runtime finding(s) " + ", ".join(sorted(missing))
            )
        return tuple(found[value] for value in finding_ids)

    def link_proposal_findings(
        self,
        *,
        proposal_id: str,
        finding_ids: tuple[str, ...],
        cited_ids: tuple[str, ...] = (),
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> int:
        """Record which findings one proposal was offered, and which it cited.

        ``finding_ids`` is everything the worker was shown; ``cited_ids`` is the
        subset its items are actually grounded in. Both are recorded, on one row
        per finding, because they answer different questions -- see
        ``sql/0011_proposal_link_citation.sql``. Passing no ``cited_ids`` records
        the whole packet as offered and nothing as cited, which is the accurate
        statement for a caller that does not know what was cited.

        An id in ``cited_ids`` that is not in ``finding_ids`` is a programming
        error, not a row to write: the proposal validator already refuses a
        citation outside the allowlist, so reaching here means the two were
        computed from different sets.

        The upsert **replaces** ``cited`` rather than OR-ing it. Proposal ids
        are deliberately stable across retries, so an attempt citing
        ``{F-1, F-2}`` followed by a retry citing only ``{F-1}`` left both rows
        ``cited`` under the OR -- re-acquiring exactly the padding
        ``sql/0011_proposal_link_citation.sql`` exists to remove, on the one
        path designed to run twice. The last writer's citation set is the
        proposal's citation set, because the last writer is the attempt whose
        proposal is on disk.
        """

        stray = set(cited_ids) - set(finding_ids)
        if stray:
            raise RuntimeStateError(
                f"{proposal_id} cites finding(s) it was not offered: "
                + ", ".join(sorted(stray))
            )
        if not finding_ids:
            return 0
        cited = set(cited_ids)
        with self._db.tx() as conn:
            for finding_id in finding_ids:
                conn.execute(
                    """
                    insert into runtime_proposal_links
                        (proposal_id, finding_id, run_id, work_id, cited)
                    values (%s, %s, %s, %s, %s)
                    on conflict (proposal_id, finding_id) do update
                        set cited = excluded.cited
                    """,
                    (proposal_id, finding_id, run_id, work_id, finding_id in cited),
                )
        return len(finding_ids)

    def link_nomination_findings(
        self,
        *,
        nomination_id: str,
        finding_ids: tuple[str, ...],
        project_id: str,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> int:
        """Record which findings one cross-project nomination rests on.

        Invariant 15's audit trail. A promoted insight reaches another project's
        prompt behind a fence saying whose findings it was, and these rows are
        what make that traceable back to the artifact a cycle produced rather
        than to a sentence somebody wrote.
        """

        if not finding_ids:
            return 0
        with self._db.tx() as conn:
            for finding_id in finding_ids:
                conn.execute(
                    """
                    insert into runtime_nomination_links
                        (nomination_id, finding_id, project_id, run_id, work_id)
                    values (%s, %s, %s, %s, %s)
                    on conflict (nomination_id, finding_id) do nothing
                    """,
                    (nomination_id, finding_id, project_id, run_id, work_id),
                )
        return len(finding_ids)

    def nomination_findings(self, nomination_id: str) -> tuple[RuntimeFinding, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "select finding_id from runtime_nomination_links "
                "where nomination_id = %s order by finding_id",
                (nomination_id,),
            ).fetchall()
        resolved = [self.get_finding(str(row["finding_id"])) for row in rows]
        return tuple(item for item in resolved if item is not None)

    def proposal_findings(
        self, proposal_id: str, *, cited_only: bool = False
    ) -> tuple[RuntimeFinding, ...]:
        """The findings linked to one proposal.

        Everything it was offered by default, because the reconciler that reads
        this needs the packet it built the proposal from. ``cited_only`` narrows
        it to the citation edges, which is what a reader asking "what does this
        proposal rest on" wants.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                "select finding_id from runtime_proposal_links "
                "where proposal_id = %(proposal_id)s "
                "  and (not %(cited_only)s or cited) "
                "order by finding_id",
                {"proposal_id": proposal_id, "cited_only": cited_only},
            ).fetchall()
        resolved = [self.get_finding(str(row["finding_id"])) for row in rows]
        return tuple(item for item in resolved if item is not None)

    def created_proposals(
        self, *, project_id: str, limit: int = 6
    ) -> tuple[dict[str, object], ...]:
        """Proposals this runtime has put in front of a person, newest first.

        Only ``CREATED`` rows: a ``RESERVED`` row is an attempt in flight and a
        ``FAILED`` one is an attempt that produced nothing, and neither is a
        decision anybody is waiting on.

        Deliberately says nothing about whether a proposal was *promoted*. That
        is canonical scientific state, it lives in the capsule, and the capsule
        is already where the frontier comes from. A runtime that tracked
        promotion in its own tables would have a second answer to a question
        the project has one answer to.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select r.proposal_id, r.run_id, r.created_at,
                       count(l.finding_id) filter (where l.cited) as cited_findings,
                       count(l.finding_id) as offered_findings
                from runtime_proposal_reservations r
                left join runtime_proposal_links l
                       on l.proposal_id = r.proposal_id
                where r.project_id = %(project_id)s and r.status = 'CREATED'
                group by r.proposal_id, r.run_id, r.created_at
                order by r.created_at desc, r.proposal_id
                limit %(limit)s
                """,
                {"project_id": project_id, "limit": limit},
            ).fetchall()
        return tuple(
            {
                "proposal_id": str(row["proposal_id"]),
                "run_id": str(row["run_id"] or ""),
                "created_at": row["created_at"].isoformat(),
                "cited_findings": int(row["cited_findings"] or 0),
                "offered_findings": int(row["offered_findings"] or 0),
            }
            for row in rows
        )

    def stored_preregistrations(
        self, *, project_id: str, limit: int = 6
    ) -> tuple[dict[str, object], ...]:
        """Preregistered experiment designs this project already holds.

        Newest first, bounded. Scoped through ``artifact_links.project_id``
        coalesced with the run's, which is the scoping
        ``actions.experiments._preregistration_rows`` documents and the reason
        it does not go through the run alone: pruning a run used to sever the
        only path from the artifact to its project.

        Only the ``preregistration:<digest>`` roles written from ``0012`` on.
        The legacy bare ``preregistration`` role is deliberately absent: this
        feeds a planning prompt, where a design whose digest cannot be read off
        the row is not worth a document read, and the execution guard still
        finds both.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select distinct a.artifact_id, a.role, a.created_at
                from artifacts a
                join artifact_links l on l.artifact_id = a.artifact_id
                left join research_runs r on r.run_id = l.run_id
                where a.role like 'preregistration:%%'
                  and coalesce(l.project_id, r.project_id) = %(project_id)s
                order by a.created_at desc
                limit %(limit)s
                """,
                {"project_id": project_id, "limit": limit},
            ).fetchall()
        return tuple(
            {
                "artifact_id": str(row["artifact_id"]),
                "spec_digest": str(row["role"]).partition(":")[2],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        )

    def count_stored_preregistrations(self, *, project_id: str) -> int:
        """How many designs this project holds, however many are shown."""

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(distinct a.artifact_id) as n
                from artifacts a
                join artifact_links l on l.artifact_id = a.artifact_id
                left join research_runs r on r.run_id = l.run_id
                where a.role like 'preregistration:%%'
                  and coalesce(l.project_id, r.project_id) = %(project_id)s
                """,
                {"project_id": project_id},
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_created_proposals(self, *, project_id: str) -> int:
        """How many proposals exist for this project, however many are shown."""

        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from runtime_proposal_reservations "
                "where project_id = %s and status = 'CREATED'",
                (project_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    # --------------------------------------------- proposal reservations ----
    def reserve_proposal(
        self,
        *,
        reservation_key: str,
        proposal_id: str,
        project_id: str,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> tuple[str, str, bool]:
        """Reserve one proposal identity. Returns ``(proposal_id, status, created)``.

        Written *before* the v1 ``ProposalController`` is called, so a crash
        between "the proposal store has a directory" and "the runtime knows
        about it" leaves the id to reconcile against rather than an orphan that
        a retry would duplicate.

        ``reservation_key`` is the action's stable identity -- run, cycle,
        action, grounding digest -- and never the attempt. A retry passes the
        same key, gets ``created=False`` and the *same* ``proposal_id`` back,
        and the reconciler then asks the proposal store whether that directory
        exists.
        """

        with self._db.tx() as conn:
            inserted = conn.execute(
                """
                insert into runtime_proposal_reservations
                    (reservation_key, proposal_id, project_id, run_id, work_id)
                values (%s, %s, %s, %s, %s)
                on conflict (reservation_key) do nothing
                returning proposal_id, status
                """,
                (reservation_key, proposal_id, project_id, run_id, work_id),
            ).fetchone()
            if inserted is not None:
                return str(inserted["proposal_id"]), str(inserted["status"]), True
            row = conn.execute(
                "select proposal_id, status from runtime_proposal_reservations "
                "where reservation_key = %s",
                (reservation_key,),
            ).fetchone()
        if row is None:  # pragma: no cover - the conflict implies a row exists
            raise RuntimeStateError(
                f"could not reserve a proposal for {reservation_key}"
            )
        return str(row["proposal_id"]), str(row["status"]), False

    def reserved_proposal(self, reservation_key: str) -> tuple[str, str] | None:
        """``(proposal_id, status)`` for one reservation key, or ``None``.

        So a reconciler can *read* the reserved id instead of recomputing it.
        The two are the same today, and that is the problem: they are the same
        because two functions agree, and if they ever stop agreeing -- a bump in
        ``reserved_proposal_id``'s derivation, a change in what
        ``reservation_key_for`` includes -- the reconciler starts looking for a
        directory that was never created, finds nothing, and lets the action
        create a second proposal for the same cycle. The row is the record of
        what was actually reserved; the derivation is how it was chosen.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                "select proposal_id, status from runtime_proposal_reservations "
                "where reservation_key = %s",
                (reservation_key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["proposal_id"]), str(row["status"])

    def settle_proposal_reservation(
        self, reservation_key: str, *, status: str, detail: str | None = None
    ) -> None:
        """Mark a reservation ``CREATED`` or ``FAILED``.

        Only ``CREATED`` is absorbing, and that asymmetry is the point.
        ``CREATED`` is a statement about the *directory*, which is monotone --
        once a proposal exists it exists -- so nothing may overwrite it.
        ``FAILED`` is a statement about one attempt, and a later attempt that
        finds the proposal must be able to correct it.

        Guarding both on ``RESERVED`` was the defect an adversarial review
        executed: the v1 controller stores the proposal and *then* assesses, so
        an assessor failure settles ``FAILED`` while the directory exists. The
        retry then recovered the proposal, told the researcher to read it, and
        its ``settle(..., "CREATED")`` was a silent no-op -- leaving a proposal
        a person is being asked to decide about whose reservation row says the
        attempt failed.
        """

        if status not in {
            ProposalReservationStatus.CREATED.value,
            ProposalReservationStatus.FAILED.value,
        }:
            raise RuntimeStateError(f"not a settlement status: {status!r}")
        with self._db.tx() as conn:
            conn.execute(
                """
                update runtime_proposal_reservations
                set status = %(status)s,
                    detail = coalesce(%(detail)s, detail),
                    settled_at = now()
                where reservation_key = %(key)s
                  and status <> 'CREATED'
                  and (status = 'RESERVED' or %(status)s = 'CREATED')
                """,
                {"key": reservation_key, "status": status, "detail": detail},
            )

    def has_successor(self, run_id: str) -> bool:
        """Whether any run names this one as its parent.

        A cheap pre-check that saves the work, not the thing that makes the
        outcome correct. Two passes can both read ``False`` here; the one that
        loses fails at the insert with :class:`SuccessorExistsError`, because
        ``research_runs_one_successor_idx`` allows one row per parent. This used
        to be preceded by ``lock_run``, an advisory lock taken and released
        inside its own transaction, which serialised nothing -- see
        ``sql/0014_one_successor_per_run.sql``.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                "select 1 as present from research_runs where parent_run_id = %s "
                "limit 1",
                (run_id,),
            ).fetchone()
        return row is not None

    def parked_objectives(
        self, *, project_id: str, limit: int = 20
    ) -> tuple[ResearchRun, ...]:
        """The latest cycle of each objective that finished waiting for a person.

        "Parked" means three things, and each excludes something that must not
        be woken by a scientific change:

        - **finished**, so a cycle still holding a live LangGraph interrupt is
          not given a sibling. Those are resumed by the recorded decision that
          answers them.
        - **the latest in its lineage** -- no run has it as ``parent_run_id`` --
          so one objective gets one successor however many of its ancestors are
          also terminal.
        - concluded ``DONE_FOR_NOW`` or ``WAITING_FOR_SCIENTIFIC_DECISION``, the
          two terminal states that mean "the runtime did everything it was
          allowed to". ``BUDGET_EXHAUSTED`` is deliberately absent: the science
          moving does not create budget, and opening a cycle that cannot finish
          would turn one exhausted objective into a stream of them.

        One row per objective, and by a *total* order. Two runs of one objective
        can share ``created_at`` to the microsecond -- a retry, a scripted
        double start -- and the max-timestamp filter alone would then return
        both, each eligible, each getting its own successor. ``distinct on
        (objective)`` with ``run_id`` as the tiebreak makes "the successor
        belongs to the newest" single-valued.

        The max-timestamp filter stays, and it is doing something the
        ``distinct on`` cannot: excluding an objective whose newest run is
        *not* eligible. A cycle still RUNNING is not a child of the parked one,
        so the lineage check would not see it -- but advancing an objective
        while a cycle of it is in flight is exactly the two-threads-per-
        objective state the whole design avoids.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                rf"""
                select distinct on (regexp_replace(btrim(r.objective), '\s+', ' ', 'g'))
                       {RUN_COLUMNS}
                from research_runs r
                where r.project_id = %(project_id)s
                  -- Idea-track stages are advanced by the portfolio, not by
                  -- objective continuation. See sql/0023_research_run_kind.sql.
                  and r.run_kind = 'cycle'
                  and r.status in ('SUCCEEDED','FAILED')
                  and r.terminal_state in (
                      'DONE_FOR_NOW','WAITING_FOR_SCIENTIFIC_DECISION')
                  and not exists (
                      select 1 from research_runs child
                      where child.parent_run_id = r.run_id
                  )
                  and r.created_at = (
                      select max(peer.created_at) from research_runs peer
                      where peer.project_id = r.project_id
                        -- Compared with whitespace collapsed. Exact text
                        -- equality made "whether the widget deforms" and
                        -- "whether  the widget deforms" two objectives, so one
                        -- capsule change opened a full cycle for each -- and
                        -- the cost of one observation became unbounded in the
                        -- number of near-duplicate objective strings. An
                        -- adversarial review executed three.
                        and regexp_replace(btrim(peer.objective), '\s+', ' ', 'g')
                            = regexp_replace(btrim(r.objective), '\s+', ' ', 'g')
                  )
                order by regexp_replace(btrim(r.objective), '\s+', ' ', 'g'),
                         r.run_id desc
                limit %(limit)s
                """,
                {"project_id": project_id, "limit": limit},
            ).fetchall()
        return tuple(ResearchRun.model_validate(row) for row in rows)

    def stranded_runs(
        self, *, grace_seconds: int, limit: int = 50
    ) -> tuple[StrandedRun, ...]:
        """Runs that are in flight with nothing left that could advance them.

        The generic shape of the 2026-09-19 orphans, expressed without naming
        one of them. A run qualifies when **all** of the following hold:

        - its status is ``RUNNING`` or ``CREATED`` -- in flight, not finished,
          and not parked at a gate. ``WAITING_HUMAN`` and ``WAITING_EXTERNAL``
          are deliberately excluded: those are waiting for something real, and
          the things they wait for have their own wake-ups;
        - **no live work item** references it. Live means PENDING, LEASED or
          WAITING: something is running, scheduled, or will be. A FAILED or
          SUCCEEDED item is not going to move this run;
        - **no outstanding external job** references it, so a run whose Slurm
          job is still queued is left alone whatever its status says;
        - nothing has touched it for ``grace_seconds``.

        Note what is *not* in the definition: how the run got here. A worker
        that segfaulted, a provider that went away mid-cycle, a daemon killed
        between opening a run and enqueuing its work, an item that exhausted
        its attempts -- all of them land in the same state, and all of them
        are equally unreachable by any other mechanism this runtime has. A
        reconciler that asked *why* would need a case per cause and would miss
        the next one.

        ``last_failure_class`` and ``last_error`` come from the most recent
        work item for the run, when there is one, so the caller can decide
        whether the blocking condition has lifted without a second query. They
        are evidence, not part of the qualifying test.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {_QUALIFIED_RUN_COLUMNS},
                       recent.failure_class as last_failure_class,
                       recent.last_error as last_error,
                       recent.status as last_work_status
                from research_runs r
                left join lateral (
                    select w.failure_class, w.last_error, w.status
                    from work_items w
                    where w.run_id = r.run_id
                    order by w.updated_at desc, w.work_id desc
                    limit 1
                ) recent on true
                where r.run_kind = 'cycle'
                  -- A stranded idea-track stage is reconciled by the portfolio
                  -- tick, which knows how to re-open one. This reconciler
                  -- enqueues `resume_cycle`, which resumes a CycleGraph
                  -- thread, and a track's thread is not one.
                  and r.status in ('CREATED','RUNNING')
                  and r.updated_at < now()
                      - make_interval(secs => %(grace)s)
                  and not exists (
                      select 1 from work_items live
                      where live.run_id = r.run_id
                        and live.status in ('PENDING','LEASED','WAITING')
                  )
                  and not exists (
                      select 1 from external_jobs j
                      where j.run_id = r.run_id
                        and j.status in ({_ACTIVE_JOB_SQL})
                  )
                order by r.updated_at
                limit %(limit)s
                """,
                {"grace": float(grace_seconds), "limit": limit},
            ).fetchall()
        # The evidence columns are dropped before the run is validated. The
        # record models forbid extra fields -- deliberately, so a renamed
        # column is an error rather than a silently absent attribute -- and
        # this query selects more than a run on purpose.
        evidence = ("last_failure_class", "last_error", "last_work_status")
        return tuple(
            StrandedRun(
                run=ResearchRun.model_validate(
                    {key: value for key, value in row.items() if key not in evidence}
                ),
                last_failure_class=(
                    str(row["last_failure_class"])
                    if row["last_failure_class"]
                    else None
                ),
                last_error=str(row["last_error"]) if row["last_error"] else None,
                last_work_status=(
                    str(row["last_work_status"]) if row["last_work_status"] else None
                ),
            )
            for row in rows
        )

    def touch_run(self, run_id: str) -> None:
        """Move a run's ``updated_at`` to now, changing nothing else.

        What reconciliation does after putting a run back on the queue, so the
        grace period paces the *next* attempt instead of only the first.

        Without it the throttle depended on a coincidence. Reconciliation does
        not write to the run row, so a rescheduled run kept its old
        ``updated_at`` and qualified as stranded again the instant its new work
        item died -- and the whole reschedule budget could be spent in as many
        seconds as the poll interval allows. For a provider failure that
        happens to be harmless, because three failed attempts open the breaker
        and the reconciler then waits; for any other class there is no breaker
        to wait for, and a bound meant to span half an hour was spendable in
        ten seconds.

        A bound should not rely on something else happening to be slow.
        """

        with self._db.tx() as conn:
            conn.execute(
                "update research_runs set updated_at = now() where run_id = %s",
                (run_id,),
            )

    def event_count(self, *, run_id: str, kind: str) -> int:
        """How many events of one kind this run has. The reschedule counter.

        Counted from the event ledger rather than kept in a column, because
        the ledger is append-only and already survives everything a column
        would have to: a restart, a reconnect, a second daemon. A bound read
        from durable history cannot be reset by the thing it bounds.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from events where run_id = %s and kind = %s",
                (run_id, kind),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------- capsule observations ------
    def observe_capsule(
        self, *, project_id: str, capsule_digest: str, frontier_digest: str
    ) -> tuple[bool, str | None, str | None]:
        """Compare-and-set one project's observed scientific digest.

        Returns ``(changed, previous_capsule_digest, previous_frontier_digest)``.

        ``changed`` is false on the *first* observation as well as on an
        unchanged one, and that is deliberate: the first time the runtime sees a
        project it has no idea whether what it is looking at is new, and
        treating "I have never looked before" as "a person just changed
        something" would open a successor cycle on every fresh database.

        Serialised by ``select ... for update`` on the project's own row. Two
        daemons observing together would otherwise both read the old digest,
        both see a change, and both emit -- and while the event's dedup key
        collapses those into one event, ``changes_seen`` is what a person reads
        to answer "has this actually been noticing anything", and
        double-counting it would make the answer wrong in the direction of
        reassurance.

        ``observed_at`` advances only when something moved, so it means "when
        this project's science last changed" rather than "when the daemon last
        looked". The second is in the tick report; the first is the one worth
        keeping.
        """

        with self._db.tx() as conn:
            existing = conn.execute(
                "select capsule_digest, frontier_digest from capsule_observations "
                "where project_id = %s for update",
                (project_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into capsule_observations
                        (project_id, capsule_digest, frontier_digest)
                    values (%s, %s, %s)
                    on conflict (project_id) do nothing
                    """,
                    (project_id, capsule_digest, frontier_digest),
                )
                # Whether this insert or a concurrent one won, the state now
                # recorded is the state just observed, and neither daemon saw a
                # change.
                return False, None, None

            previous_capsule = str(existing["capsule_digest"])
            previous_frontier = str(existing["frontier_digest"])
            # Either digest moving is a change. Not just the capsule one, and
            # that was a real blind spot: the two digests answer different
            # questions and neither is a superset of the other. An adversarial
            # review executed the case -- a Review's verdict changing moved the
            # frontier and left the capsule digest identical, so the highest-
            # authority human act in the system was invisible. The capsule
            # digest now covers a Review's fields as well, and this is the
            # belt: whatever else either digest fails to see, a frontier that
            # has moved means there is different work to do, which is the only
            # thing the advance needs to know.
            changed = (
                previous_capsule != capsule_digest
                or previous_frontier != frontier_digest
            )
            if changed:
                conn.execute(
                    """
                    update capsule_observations
                    set capsule_digest = %(capsule)s,
                        frontier_digest = %(frontier)s,
                        observed_at = now(),
                        changes_seen = changes_seen + %(increment)s
                    where project_id = %(project_id)s
                    """,
                    {
                        "project_id": project_id,
                        "capsule": capsule_digest,
                        "frontier": frontier_digest,
                        "increment": 1,
                    },
                )
        return changed, previous_capsule, previous_frontier

    def observed_capsule(self, project_id: str) -> tuple[str, str, int] | None:
        """What the runtime last saw, or ``None`` if it has never looked."""

        with self._db.tx() as conn:
            row = conn.execute(
                "select capsule_digest, frontier_digest, changes_seen "
                "from capsule_observations where project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            str(row["capsule_digest"]),
            str(row["frontier_digest"]),
            int(row["changes_seen"]),
        )

    # ----------------------------------------------------------- schedules --
    def create_schedule(
        self,
        *,
        kind: str,
        interval_seconds: int,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Schedule:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into schedules
                    (schedule_id, project_id, kind, payload, interval_seconds)
                values (%(schedule_id)s, %(project_id)s, %(kind)s, %(payload)s, %(interval)s)
                returning {SCHEDULE_COLUMNS}
                """,
                {
                    "schedule_id": new_schedule_id(),
                    "project_id": project_id,
                    "kind": kind,
                    "payload": jsonb(payload or {}),
                    "interval": interval_seconds,
                },
            ).fetchone()
        return Schedule.model_validate(row)

    def claim_due_schedules(self, *, limit: int = 20) -> tuple[Schedule, ...]:
        """Take due schedules and advance them, in one transaction.

        Advancing inside the claim is what stops a schedule firing twice when
        two daemon loops tick together, and what stops a restart during the
        firing from firing it again.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                with due as (
                    select schedule_id from schedules
                    where enabled and next_run_at <= now()
                    order by next_run_at
                    for update skip locked
                    limit %(limit)s
                )
                update schedules s
                set last_run_at = now(),
                    next_run_at = now() + make_interval(secs => s.interval_seconds)
                from due d where s.schedule_id = d.schedule_id
                returning {", ".join("s." + c.strip() for c in SCHEDULE_COLUMNS.split(","))}
                """,
                {"limit": limit},
            ).fetchall()
        return tuple(Schedule.model_validate(row) for row in rows)

    def list_schedules(self) -> tuple[Schedule, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {SCHEDULE_COLUMNS} from schedules order by kind, schedule_id"
            ).fetchall()
        return tuple(Schedule.model_validate(row) for row in rows)

    # ------------------------------------------------------ provider health --
    def record_provider_result(
        self,
        provider: str,
        *,
        ok: bool,
        error: str | None = None,
        threshold: int = 3,
        cooldown: int = 300,
    ) -> ProviderHealth:
        """Update one provider's health from one observation.

        The counter resets on success rather than decaying, because a provider
        that works once is working. The cooldown exists so a provider that is
        down is not hammered by every worker in turn.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                insert into provider_status (provider, healthy, consecutive_failures,
                                             cooldown_until, last_error, last_ok_at)
                values (%(provider)s,
                        -- A first failure is not yet unhealthy unless the threshold is 1.
                        case when %(ok)s then true else 1 < %(threshold)s end,
                        case when %(ok)s then 0 else 1 end,
                        case when not %(ok)s and 1 >= %(threshold)s
                             then now() + make_interval(secs => %(cooldown)s) end,
                        %(error)s, case when %(ok)s then now() else null end)
                on conflict (provider) do update set
                    consecutive_failures = case when %(ok)s then 0
                        else provider_status.consecutive_failures + 1 end,
                    healthy = case when %(ok)s then true
                        else provider_status.consecutive_failures + 1 < %(threshold)s end,
                    cooldown_until = case
                        when %(ok)s then null
                        when provider_status.consecutive_failures + 1 >= %(threshold)s
                        then now() + make_interval(secs => %(cooldown)s)
                        else provider_status.cooldown_until end,
                    last_error = case when %(ok)s then null else %(error)s end,
                    last_ok_at = case when %(ok)s then now() else provider_status.last_ok_at end,
                    updated_at = now()
                returning provider, healthy, consecutive_failures, cooldown_until,
                          last_error, last_ok_at, updated_at
                """,
                {
                    "provider": provider,
                    "ok": ok,
                    "error": error[:2000] if error else None,
                    "threshold": threshold,
                    "cooldown": float(cooldown),
                },
            ).fetchone()
        return ProviderHealth.model_validate(row)

    def provider_health(self) -> tuple[ProviderHealth, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "select provider, healthy, consecutive_failures, cooldown_until, "
                "last_error, last_ok_at, updated_at from provider_status order by provider"
            ).fetchall()
        return tuple(ProviderHealth.model_validate(row) for row in rows)

    def usable_providers(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        """Filter ``candidates`` to those not in cooldown, preserving order.

        One query. The first version read every provider's health and then
        opened a fresh transaction *per candidate* to ask the server whether a
        timestamp it already held had passed -- a pool checkout and a round trip
        each, on the model-routing hot path.
        """

        if not candidates:
            return ()
        with self._db.tx() as conn:
            rows = conn.execute(
                "select provider from provider_status "
                "where provider = any(%s) and cooldown_until > now()",
                (list(candidates),),
            ).fetchall()
        cooling = {str(row["provider"]) for row in rows}
        return tuple(name for name in candidates if name not in cooling)

    def provider_cooldowns(self, candidates: tuple[str, ...]) -> dict[str, datetime]:
        """When each named provider stops being in cooldown, for those that are.

        The companion to :meth:`usable_providers`, which answers *whether* a
        provider may be used and throws away *when* that changes. Both facts
        are in the same row and the second one is what makes a retry schedule
        correct: an item retried three times inside a five-minute cooldown has
        spent its whole attempt budget on calls that could not have succeeded,
        which is what happened on 2026-09-19.
        """

        if not candidates:
            return {}
        with self._db.tx() as conn:
            rows = conn.execute(
                "select provider, cooldown_until from provider_status "
                "where provider = any(%s) and cooldown_until > now()",
                (list(candidates),),
            ).fetchall()
        return {str(row["provider"]): row["cooldown_until"] for row in rows}


def _finding_from(row: Any, refs: Any = ()) -> RuntimeFinding:
    """Rebuild a finding from its row and, when loaded, its reference edges.

    ``refs`` is optional because the list and dedupe paths do not need the
    edges and loading them per row would be a query per finding. A finding
    returned without edges reports empty tuples, which is honest -- it says
    "not loaded" the same way it would say "none" -- and every caller that
    needs the provenance uses :meth:`RuntimeStore.get_finding`, which loads it.
    """

    by_kind: dict[str, list[str]] = {
        FindingRefKind.ARTIFACT.value: [],
        FindingRefKind.CAPSULE_OBJECT.value: [],
        FindingRefKind.LITERATURE_KEY.value: [],
    }
    for ref in refs or ():
        by_kind.setdefault(str(ref["kind"]), []).append(str(ref["ref"]))
    return RuntimeFinding(
        finding_id=str(row["finding_id"]),
        project_id=str(row["project_id"]),
        kind=FindingKind(str(row["kind"])),
        summary=str(row["summary"]),
        excerpt=str(row["excerpt"] or ""),
        source_run_id=row["source_run_id"],
        source_cycle=row["source_cycle"],
        source_work_id=row["source_work_id"],
        source_action=row["source_action"],
        artifact_ids=tuple(by_kind[FindingRefKind.ARTIFACT.value]),
        capsule_refs=tuple(by_kind[FindingRefKind.CAPSULE_OBJECT.value]),
        literature_keys=tuple(by_kind[FindingRefKind.LITERATURE_KEY.value]),
        experiment_job_id=row["experiment_job_id"],
        spec_digest=row["spec_digest"],
        semantic_key=str(row["semantic_key"] or ""),
        created_at=str(row["created_at"]),
    )
