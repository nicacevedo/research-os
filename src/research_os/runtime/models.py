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


class WorkStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    #: The decision has been recorded *and* acted upon. Separate from GRANTED so
    #: a resume that runs twice cannot apply one approval twice.
    APPLIED = "APPLIED"


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


class BudgetScope(StrEnum):
    SYSTEM = "system"
    PROJECT = "project"
    RUN = "run"
    WORK_ITEM = "work_item"


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
    autonomy: Autonomy
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
    submitted_at: datetime
    last_polled_at: datetime | None
    finished_at: datetime | None


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
ENUM_CONSTRAINTS: dict[str, frozenset[str]] = {
    "research_runs_status_ck": frozenset(s.value for s in RunStatus),
    "research_runs_terminal_ck": frozenset(s.value for s in TerminalState),
    "research_runs_autonomy_ck": frozenset(s.value for s in Autonomy),
    "work_items_status_ck": frozenset(s.value for s in WorkStatus),
    "approvals_status_ck": frozenset(s.value for s in ApprovalStatus),
    "tool_invocations_status_ck": frozenset(s.value for s in InvocationStatus),
    "model_calls_status_ck": frozenset(s.value for s in ModelCallStatus),
    "external_jobs_status_ck": frozenset(s.value for s in ExternalJobStatus),
    "budgets_scope_ck": frozenset(s.value for s in BudgetScope),
    "budget_reservations_status_ck": frozenset(s.value for s in ReservationStatus),
}
