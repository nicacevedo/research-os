"""Typed runtime records, and the enums the database checks against.

Every status enum here has a matching ``check`` constraint in
``sql/0001_runtime.sql``. That duplication is deliberate and is tested:
``tests/test_runtime_schema.py`` reads the constraints out of the live catalog
and asserts they hold exactly these values. Either the database rejects a status
Python can produce -- a crash in the one code path that was meant to record a
failure -- or it accepts one Python does not know how to handle. Both are worse
than a duplicated list with a test on it.

These models are read models. Nothing here writes; :mod:`research_os.runtime.store`
does that, and hands back instances of these.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_os.runtime.findings import FindingKind, FindingRefKind


class RunStatus(StrEnum):
    """Where a bounded research cycle is.

    ``WAITING_EXTERNAL`` and ``WAITING_HUMAN`` are distinct on purpose. Both
    mean "not progressing", and conflating them is how a system ends up telling
    a researcher it is stuck when it is actually waiting for Slurm, or telling
    them Slurm is slow when it is waiting for them.
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_HUMAN = "WAITING_HUMAN"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class TerminalState(StrEnum):
    """What a finished cycle concluded, in the researcher's vocabulary.

    Distinct from :class:`RunStatus`, which is the runtime's. A cycle can be
    ``SUCCEEDED`` and still have concluded ``WAITING_FOR_SCIENTIFIC_DECISION``:
    the runtime did everything it was allowed to do, and what remains is not
    its to decide.
    """

    DONE_FOR_NOW = "DONE_FOR_NOW"
    WAITING_FOR_SCIENTIFIC_DECISION = "WAITING_FOR_SCIENTIFIC_DECISION"
    WAITING_FOR_EXTERNAL_DEPENDENCY = "WAITING_FOR_EXTERNAL_DEPENDENCY"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FATAL_INFRASTRUCTURE_ERROR = "FATAL_INFRASTRUCTURE_ERROR"
    CANCELLED = "CANCELLED"


class RunKind(StrEnum):
    """What kind of run a ``research_runs`` row is.

    ``CYCLE`` is the R5 bounded objective cycle and is the default, so nothing
    written before the portfolio layer changes meaning. ``IDEA_TRACK`` is one
    bounded stage of one portfolio idea: it borrows the run row for budgets,
    provenance and ``researchctl runtime run <id>``, and is deliberately
    invisible to objective continuation and to cycle reconciliation. See
    ``sql/0023_research_run_kind.sql``.
    """

    CYCLE = "cycle"
    IDEA_TRACK = "idea_track"


class WorkStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"


class ApprovalStatus(StrEnum):
    """What a person decided. Not whether it has been acted on.

    There is deliberately no ``APPLIED``. An earlier version had one and wrote
    it over ``GRANTED`` or ``DECLINED``, which destroyed the distinction the
    status exists to record -- a replayed node read a *declined* gate back as
    granted. ``Approval.applied_at`` carries "acted on" orthogonally, and
    ``mark_approval_applied`` claims it exactly once.
    """

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class InvocationStatus(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    #: Started, never finished, and the worker that started it is gone. The
    #: outcome of the side effect is genuinely unknown; only a reconciler that
    #: can look at the world can say.
    ABANDONED = "ABANDONED"


class ModelCallStatus(StrEnum):
    OK = "OK"
    FAILED = "FAILED"
    REFUSED = "REFUSED"
    MALFORMED = "MALFORMED"
    TIMEOUT = "TIMEOUT"


class ExternalJobStatus(StrEnum):
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"


ACTIVE_JOB_STATUSES: frozenset[ExternalJobStatus] = frozenset(
    {
        ExternalJobStatus.SUBMITTING,
        ExternalJobStatus.SUBMITTED,
        ExternalJobStatus.PENDING,
        ExternalJobStatus.RUNNING,
        ExternalJobStatus.UNKNOWN,
    }
)
TERMINAL_JOB_STATUSES: frozenset[ExternalJobStatus] = frozenset(
    {
        ExternalJobStatus.COMPLETED,
        ExternalJobStatus.FAILED,
        ExternalJobStatus.CANCELLED,
        ExternalJobStatus.TIMED_OUT,
    }
)


class InterpretationStatus(StrEnum):
    """Where one experiment interpretation is.

    ``ABANDONED`` rather than ``FAILED``, deliberately, and for the same reason
    the invocation ledger draws that distinction: a reading whose worker died
    leaves a row whose *outcome* is unknown, and the recovery for that is to
    look at what was actually produced -- the artifact -- rather than to assume
    nothing happened and read the experiment a second time.

    There is no status here for "the experiment refuted its hypothesis". An
    experiment that ran correctly and answered no has been interpreted
    successfully; the answer is in the artifact, not in this column.
    """

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class BudgetScope(StrEnum):
    SYSTEM = "system"
    PROJECT = "project"
    RUN = "run"
    WORK_ITEM = "work_item"
    #: A discovery-portfolio idea and its lineage family (`sql/0036`). Reserved
    #: per call alongside run, project and system when a request names them,
    #: so the portfolio's two ceilings are checked where the spend is taken
    #: rather than summed after the stage that made it.
    IDEA = "idea"
    LINEAGE = "lineage"


class ReservationStatus(StrEnum):
    HELD = "HELD"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class Autonomy(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Project(_Record):
    project_id: str
    repo_path: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class ResearchRun(_Record):
    run_id: str
    project_id: str
    objective: str
    status: RunStatus
    terminal_state: TerminalState | None = None
    #: What this cycle concluded should happen next, recorded with the terminal
    #: state in one statement.
    #:
    #: **This, and not an event payload, is what continuation reads.** The
    #: recommendation used to exist only in the ``RESEARCH_CYCLE_FINISHED``
    #: event and in the work-item payload copied from it, two messages away
    #: from the run that reached it -- so a cycle whose frontier concluded
    #: ``WAIT_HUMAN`` could leave a queued instruction reading
    #: ``START_NEXT_CYCLE``, and a later build fixing the conclusion could not
    #: fix the frozen copy. A live thesis run did exactly that.
    #:
    #: ``None`` means a build predating the column finished this run. It is not
    #: the same as "recommended nothing", and
    #: :meth:`~research_os.runtime.daemon.Daemon._work_continue_objective`
    #: distinguishes them: it falls back to the payload only for a null, and
    #: says in its result that it did.
    next_recommendation: str | None = None
    autonomy: Autonomy
    run_kind: RunKind = RunKind.CYCLE
    parent_run_id: str | None = None
    cycle_index: int
    thread_id: str | None = None
    detail: str | None = None
    #: A hash of the frontier as this cycle left it. Compared against the
    #: parent's so a cycle that changed nothing does not open a successor.
    frontier_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


class StrandedRun(_Record):
    """A run that is in flight with nothing left that could advance it.

    A run plus the evidence about why, kept together because a reconciler
    needs both and reading them separately invites the two to disagree. See
    :meth:`research_os.runtime.store.RuntimeStore.stranded_runs`.
    """

    run: ResearchRun
    #: The failure class of the run's most recent work item, if it had one.
    #: Evidence for the reconciler's decision, never part of the test for
    #: whether the run is stranded -- a run stranded by a daemon killed
    #: between `open_cycle` and the ingest pass has no work item at all.
    last_failure_class: str | None = None
    last_error: str | None = None
    last_work_status: str | None = None


class WorkItem(_Record):
    work_id: str
    run_id: str | None
    project_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: WorkStatus
    priority: int
    scheduled_at: datetime
    attempts: int
    max_attempts: int
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    dedup_key: str | None = None
    failure_class: str | None = None
    last_error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)


class Event(_Record):
    event_id: str
    project_id: str | None
    run_id: str | None
    work_id: str | None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    dedup_key: str | None = None
    created_at: datetime
    consumed_at: datetime | None = None


class Approval(_Record):
    approval_id: str
    run_id: str
    project_id: str
    kind: str
    question: str
    packet: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus
    decision: dict[str, Any] | None = None
    decided_by: str | None = None
    thread_id: str | None = None
    interrupt_key: str | None = None
    requested_at: datetime
    decided_at: datetime | None = None
    applied_at: datetime | None = None


class ToolInvocation(_Record):
    invocation_id: str
    idempotency_key: str
    run_id: str | None
    #: Whose side effect this was, kept independently of the run.
    #:
    #: A run is a unit of work and can be pruned; the record that an externally
    #: visible effect was claimed is provenance and must survive it. Before
    #: 0015 the only route from this row to a project was the run, so pruning
    #: the run made the effect unattributable. Derived at insert from the run,
    #: so it cannot disagree with it.
    project_id: str | None = None
    work_id: str | None
    kind: str
    request: dict[str, Any] = Field(default_factory=dict)
    status: InvocationStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int
    owner: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ModelCall(_Record):
    call_id: str
    run_id: str | None
    #: Whose money this was. See :attr:`ToolInvocation.project_id`; the reason
    #: is the same and the consequence here is cost attribution.
    project_id: str | None = None
    work_id: str | None
    invocation_id: str | None
    provider: str
    model: str | None
    role: str
    criticality: str
    independence_group: str | None
    prompt_version: str | None
    input_digest: str | None
    output_artifact_id: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    latency_ms: int | None
    status: ModelCallStatus
    error: str | None
    #: The separation actually achieved, and why. Persisted rather than left in
    #: a log line and a prunable checkpoint: "independently reviewed" is the
    #: claim the review apparatus rests on, and an unfalsifiable claim is worse
    #: than an acknowledged absence.
    independence: str | None = None
    independence_note: str | None = None
    created_at: datetime


class ExternalJob(_Record):
    job_id: str
    run_id: str | None
    work_id: str | None
    project_id: str
    executor: str
    scheduler_job_id: str | None
    spec_digest: str
    run_dir: str
    status: ExternalJobStatus
    failure_class: str | None
    exit_code: int | None
    detail: str | None
    #: Whether the sandbox actually contained this run, and which backend
    #: reported so. ``None`` on rows written before migration 0030, which is
    #: "not recorded" and deliberately not ``False``.
    contained: bool | None = None
    containment: str | None = None
    wall_clock_seconds: Decimal | None = None
    submitted_at: datetime
    last_polled_at: datetime | None
    finished_at: datetime | None


class ExperimentInterpretation(_Record):
    """One reading of one experiment, bound to the exact spec that produced it.

    ``spec_digest`` is copied here at claim time rather than read through
    ``job_id`` when needed. That is not denormalisation for speed: it is what
    makes "this interpretation is of *that* frozen specification" a fact about
    this row, so a later correction to the job record cannot silently re-point
    a scientific reading at a different experiment.
    """

    interpretation_id: str
    job_id: str
    project_id: str
    run_id: str | None = None
    work_id: str | None = None
    spec_digest: str
    interpreter_version: str
    artifact_id: str | None = None
    preregistration_artifact_id: str | None = None
    """The artifact whose criteria this interpretation was compared against.

    Named on the row rather than re-queried by ``spec_digest``, because that
    digest covers the execution and not the criteria -- so a lookup by digest
    can be handed a *different* set of criteria designed after the result was
    known. Resolved once, at claim time. See
    ``sql/0010_interpretation_preregistration.sql``.
    """
    status: InterpretationStatus
    detail: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return self.status is InterpretationStatus.COMPLETED


class ArtifactRecord(_Record):
    artifact_id: str
    size_bytes: int
    media_type: str
    role: str | None
    producer: str | None
    source: str | None
    created_at: datetime


class BudgetRecord(_Record):
    budget_id: str
    scope: BudgetScope
    scope_id: str
    dimension: str
    limit_value: Decimal
    reserved: Decimal
    spent: Decimal
    #: Whether a person set this limit, rather than the runtime deriving it.
    #:
    #: The runtime raises a *derived* project ceiling when an objective needs
    #: more than it -- see `cycles.ensure_budgets`, and the bricked project
    #: that rule exists because of. It may not raise an explicit one: a
    #: number a researcher typed is an authorisation, and multiplying it is
    #: making one.
    explicit: bool = False
    created_at: datetime
    updated_at: datetime

    @property
    def available(self) -> Decimal:
        return self.limit_value - self.reserved - self.spent

    @property
    def exhausted(self) -> bool:
        return self.available <= 0


class Reservation(_Record):
    reservation_id: str
    budget_id: str
    work_id: str | None
    amount: Decimal
    status: ReservationStatus
    created_at: datetime
    settled_at: datetime | None


class Schedule(_Record):
    schedule_id: str
    project_id: str | None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int
    next_run_at: datetime
    last_run_at: datetime | None
    enabled: bool
    created_at: datetime


class ProviderHealth(_Record):
    provider: str
    healthy: bool
    consecutive_failures: int
    cooldown_until: datetime | None
    last_error: str | None
    last_ok_at: datetime | None
    updated_at: datetime


#: Every ``check`` constraint in the schema that mirrors an enum above, keyed by
#: constraint name. Read by the schema-agreement test.
class ProposalReservationStatus(StrEnum):
    """The life of one caller-reserved proposal identity.

    Mirrored by ``runtime_proposal_reservations_status_ck``. ``RESERVED`` is
    written before the v1 proposal controller is called; exactly one of
    ``CREATED`` or ``FAILED`` settles it afterwards, and the store refuses to
    move a row out of ``CREATED``, because a settled proposal is a thing a
    person may already have read.
    """

    RESERVED = "RESERVED"
    CREATED = "CREATED"
    FAILED = "FAILED"


ENUM_CONSTRAINTS: dict[str, frozenset[str]] = {
    "research_runs_status_ck": frozenset(s.value for s in RunStatus),
    "research_runs_terminal_ck": frozenset(s.value for s in TerminalState),
    "research_runs_autonomy_ck": frozenset(s.value for s in Autonomy),
    "research_runs_kind_ck": frozenset(s.value for s in RunKind),
    "work_items_status_ck": frozenset(s.value for s in WorkStatus),
    "approvals_status_ck": frozenset(s.value for s in ApprovalStatus),
    "tool_invocations_status_ck": frozenset(s.value for s in InvocationStatus),
    "model_calls_status_ck": frozenset(s.value for s in ModelCallStatus),
    "external_jobs_status_ck": frozenset(s.value for s in ExternalJobStatus),
    "budgets_scope_ck": frozenset(s.value for s in BudgetScope),
    "budget_reservations_status_ck": frozenset(s.value for s in ReservationStatus),
    "experiment_interpretations_status_ck": frozenset(
        s.value for s in InterpretationStatus
    ),
    "runtime_findings_kind_ck": frozenset(s.value for s in FindingKind),
    "runtime_finding_refs_kind_ck": frozenset(s.value for s in FindingRefKind),
    "runtime_proposal_reservations_status_ck": frozenset(
        s.value for s in ProposalReservationStatus
    ),
}
"""Every value-list check constraint in the schema, and the Python enum it
mirrors.

``tests/test_runtime_schema.py`` checks this in both directions. Forwards: each
constraint named here allows exactly the enum's values. Backwards -- and this is
the half that was missing -- every ``in (...)`` check constraint the live
database actually has appears here. The two constraints added immediately above
are the ones that omission hid: both were written in SQL, neither had a Python
enum, and the forward-only proof reported success while not looking at them.
"""
