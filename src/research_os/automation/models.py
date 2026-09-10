"""Runtime models and the run-state machine for the automation control plane.

These describe orchestration, not science. No model here is a scientific object,
none of them is digested, and none of them may be referenced by a capsule. They
are validated as strictly as the scientific models because a control plane that
executes commands and dispatches write-enabled workers must not accept a shape
it did not intend.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.errors import RunStateError
from research_os.models import NonBlankStr

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

RUN_ID_RE = re.compile(r"^RUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
TASK_ID_RE = re.compile(r"^T-[0-9]{3}$")
INVOCATION_ID_RE = re.compile(r"^INV-[0-9]{4}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def utc_now() -> str:
    """Return the current UTC time in the runtime timestamp format."""

    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


class RunState(StrEnum):
    """Explicit deterministic run states.

    Only the controller assigns these. No model output is ever mapped onto a
    run state, and no transition happens implicitly as a side effect of I/O.
    """

    CREATED = "CREATED"
    PREFLIGHTED = "PREFLIGHTED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    EXECUTING = "EXECUTING"
    CHECKING = "CHECKING"
    REVIEWING = "REVIEWING"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.READY_FOR_HUMAN, RunState.FAILED, RunState.CANCELLED}
)

_FORWARD_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.PREFLIGHTED}),
    RunState.PREFLIGHTED: frozenset({RunState.PLANNING}),
    RunState.PLANNING: frozenset({RunState.PLAN_READY}),
    RunState.PLAN_READY: frozenset({RunState.EXECUTING}),
    RunState.EXECUTING: frozenset({RunState.CHECKING}),
    RunState.CHECKING: frozenset({RunState.REVIEWING, RunState.EXECUTING}),
    RunState.REVIEWING: frozenset({RunState.READY_FOR_HUMAN, RunState.EXECUTING}),
    RunState.READY_FOR_HUMAN: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    """Return every state reachable in one step from ``state``.

    Any non-terminal state may also fail or be cancelled, so those two are added
    here rather than repeated in the forward table.
    """

    forward = _FORWARD_TRANSITIONS[state]
    if state in TERMINAL_RUN_STATES:
        return forward
    return forward | {RunState.FAILED, RunState.CANCELLED}


def assert_transition(current: RunState, target: RunState) -> None:
    """Raise unless ``current`` may move directly to ``target``."""

    if target not in allowed_transitions(current):
        raise RunStateError(f"invalid run state transition {current} -> {target}")


class Role(StrEnum):
    """The three bounded worker roles this MVP dispatches."""

    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"


class RiskClass(StrEnum):
    """How much authority a work order needs."""

    READ_ONLY = "read_only"
    WRITE_ISOLATED = "write_isolated"


class ExpectedOutput(StrEnum):
    """What the controller expects a completed work order to have produced."""

    DIFF = "diff"
    REPORT = "report"


class WorkOrderStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    EXECUTED = "executed"
    CHECKS_PASSED = "checks_passed"
    CHECKS_FAILED = "checks_failed"
    REVIEWED = "reviewed"
    FAILED = "failed"
    BLOCKED = "blocked"


TERMINAL_ORDER_FAILURES: frozenset[WorkOrderStatus] = frozenset(
    {WorkOrderStatus.CHECKS_FAILED, WorkOrderStatus.FAILED, WorkOrderStatus.BLOCKED}
)


class ReviewVerdict(StrEnum):
    """The only verdicts an automated reviewer may return.

    Deliberately unrelated to the scientific ``Verdict`` enum: this reviews a
    diff, not science, and must never be mistakable for a human Review.
    """

    PASS = "PASS"
    PASS_WITH_REPAIR = "PASS_WITH_REPAIR"
    FAIL = "FAIL"


class Independence(StrEnum):
    """How independent the reviewer actually was from the implementer."""

    INDEPENDENT_PROVIDER_FAMILY = "INDEPENDENT_PROVIDER_FAMILY"
    DEGRADED_SAME_PROVIDER_FAMILY = "DEGRADED_SAME_PROVIDER_FAMILY"
    DEGRADED_SAME_MODEL = "DEGRADED_SAME_MODEL"


def _relative_path(value: str) -> str:
    """Reject a scope pointer that is not a portable repository-relative path."""

    if not value.strip():
        raise ValueError("path scope entries must be non-empty")
    if value.startswith(("/", "~")):
        raise ValueError("path scope entries must be repository-relative")
    if "\\" in value:
        raise ValueError("path scope entries must use POSIX '/' separators")
    segments = [item for item in value.split("/") if item != ""]
    if not segments:
        raise ValueError("path scope entries must name at least one segment")
    if any(item in {".", ".."} for item in segments):
        raise ValueError("path scope entries must not contain '.' or '..' segments")
    return value


class AcceptanceCommand(BaseModel):
    """One deterministic command the controller runs itself.

    Stored as an argument vector, never as a shell string: the controller
    executes it without a shell, so there is no quoting, expansion, or
    metacharacter surface for a model to reach through.
    """

    model_config = ConfigDict(extra="forbid")

    argv: list[NonBlankStr] = Field(min_length=1)
    description: str | None = None
    required: bool = True

    @field_validator("argv")
    @classmethod
    def _executable_is_a_bare_name(cls, value: list[str]) -> list[str]:
        program = value[0]
        if "/" in program or program.startswith("-"):
            raise ValueError("acceptance command must start with a bare program name")
        return value

    @property
    def display(self) -> str:
        return " ".join(self.argv)


class CommandResult(BaseModel):
    """What the controller observed when it ran one acceptance command."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    cwd: str
    required: bool
    exit_code: int | None
    timed_out: bool
    timeout_seconds: int
    started_at: str
    ended_at: str
    duration_ms: int
    stdout_path: str | None = None
    stderr_path: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    @property
    def display(self) -> str:
        return " ".join(self.argv)


class ModelInvocation(BaseModel):
    """One bounded worker invocation, recorded whether it succeeded or not.

    Token and cost fields are optional because not every provider reports them.
    An unobservable value is stored as ``None`` and rendered as ``unknown``; it
    is never estimated.
    """

    model_config = ConfigDict(extra="forbid")

    invocation_id: str
    run_id: str
    task_id: str | None = None
    role: Role
    provider: NonBlankStr
    model: str | None = None
    effort: str | None = None
    read_only: bool
    cwd: str
    started_at: str
    ended_at: str
    duration_ms: int
    timeout_seconds: int
    timed_out: bool = False
    exit_code: int | None = None
    prompt_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    raw_output_path: str | None = None
    provider_session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    error: str | None = None

    @field_validator("invocation_id")
    @classmethod
    def _invocation_id_shape(cls, value: str) -> str:
        if INVOCATION_ID_RE.fullmatch(value) is None:
            raise ValueError("invocation_id must look like INV-0001")
        return value

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0


class WorktreeRecord(BaseModel):
    """One automatically created isolated Git worktree."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    path: str
    branch: NonBlankStr
    base_commit: str
    lock_path: str
    created_at: str
    removed_at: str | None = None

    @field_validator("base_commit")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("base_commit must be a full 40-character commit sha")
        return value


class WorkOrder(BaseModel):
    """One bounded unit of dispatched work.

    Scope is data the controller enforces, not advice to the worker: paths
    changed outside ``allowed_paths`` fail the order after execution, and the
    acceptance commands are run by the controller in this worktree.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: NonBlankStr
    goal: NonBlankStr
    role: Role
    risk_class: RiskClass
    project_path: NonBlankStr
    base_commit: str
    read_only: bool
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    acceptance_commands: list[AcceptanceCommand] = Field(default_factory=list)
    expected_artifacts: list[NonBlankStr] = Field(default_factory=list)
    completion_condition: NonBlankStr
    timeout_seconds: int = Field(ge=1)
    provider: NonBlankStr
    model: str | None = None
    expected_output: ExpectedOutput
    dependencies: list[str] = Field(default_factory=list)
    status: WorkOrderStatus = WorkOrderStatus.PENDING
    worktree_path: str | None = None
    branch: str | None = None
    head_commit: str | None = None
    changed_paths: list[str] = Field(default_factory=list)
    diff_path: str | None = None
    invocation_ids: list[str] = Field(default_factory=list)
    check_results: list[CommandResult] = Field(default_factory=list)
    failure_reason: str | None = None

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        if TASK_ID_RE.fullmatch(value) is None:
            raise ValueError("task_id must look like T-001")
        return value

    @field_validator("base_commit")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("base_commit must be a full 40-character commit sha")
        return value

    @field_validator("allowed_paths", "forbidden_paths")
    @classmethod
    def _scope_paths(cls, value: list[str]) -> list[str]:
        return [_relative_path(item) for item in value]

    @field_validator("dependencies")
    @classmethod
    def _dependency_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if TASK_ID_RE.fullmatch(item) is None:
                raise ValueError("dependencies must be task ids like T-001")
        if len(set(value)) != len(value):
            raise ValueError("dependencies must not repeat")
        return value

    @model_validator(mode="after")
    def _authority_matches_risk(self) -> Self:
        if self.read_only and self.risk_class is not RiskClass.READ_ONLY:
            raise ValueError("read-only work orders must use the read_only risk class")
        if not self.read_only and self.risk_class is RiskClass.READ_ONLY:
            raise ValueError("write work orders must not use the read_only risk class")
        if not self.read_only and not self.allowed_paths:
            raise ValueError("write work orders require a non-empty allowed_paths")
        if self.task_id in self.dependencies:
            raise ValueError("a work order cannot depend on itself")
        return self

    @property
    def required_checks_passed(self) -> bool:
        """Return whether every required check ran *and* succeeded.

        Fail-closed: a work order with no required check result has not been
        verified, so it reports False rather than the vacuous truth of
        ``all([])``. Nothing may treat "nothing was checked" as "the checks
        passed".
        """

        required = [item for item in self.check_results if item.required]
        if not required:
            return False
        return all(item.ok for item in required)


class ReviewFinding(BaseModel):
    """One reviewer finding. Advisory: nothing here changes code or state."""

    model_config = ConfigDict(extra="forbid")

    severity: NonBlankStr
    message: NonBlankStr
    path: str | None = None


class ReviewOutcome(BaseModel):
    """A read-only reviewer's structured verdict on one work order."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    verdict: ReviewVerdict
    summary: NonBlankStr
    findings: list[ReviewFinding] = Field(default_factory=list)
    provider: NonBlankStr
    model: str | None = None
    independence: Independence
    independence_note: NonBlankStr
    invocation_id: str
    raw_output_path: str | None = None


class Budget(BaseModel):
    """Hard limits the controller enforces before it spends anything."""

    model_config = ConfigDict(extra="forbid")

    max_model_calls: int = Field(default=8, ge=1, le=100)
    max_command_timeout_seconds: int = Field(default=900, ge=1, le=7200)
    max_wall_clock_seconds: int | None = Field(default=3600, ge=1)
    max_write_work_orders: int = Field(default=2, ge=0, le=10)
    max_work_orders: int = Field(default=4, ge=1, le=20)


class RoleSetting(BaseModel):
    """Provider and model preference for one worker role."""

    model_config = ConfigDict(extra="forbid")

    provider: NonBlankStr
    model: str | None = None
    effort: str | None = None
    read_only: bool
    tools: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _read_only_roles_have_no_tools(self) -> Self:
        """Refuse a read-only role that was given tools.

        "Read-only" is enforced by handing the worker nothing to act with, so a
        read-only role configured with ``Write`` or ``Bash`` is not a stricter
        preference the invocation can quietly correct; it is a configuration
        that means two contradictory things. It is rejected here, and the
        invocation layer independently forces the effective tool set empty.
        """

        if self.read_only and self.tools:
            raise ValueError(
                "a read_only role must declare no tools, but this one declares "
                f"{', '.join(self.tools)}; read-only workers are given an empty "
                "tool set so they cannot act on the repository at all"
            )
        return self


class ProviderProbe(BaseModel):
    """What local inspection actually established about one provider CLI."""

    model_config = ConfigDict(extra="forbid")

    name: NonBlankStr
    family: NonBlankStr
    executable: str | None = None
    available: bool
    noninteractive_verified: bool = False
    version: str | None = None
    auth_status: str = "unknown"
    detail: str = ""


class AutomationRun(BaseModel):
    """The complete runtime record of one automation run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    project_id: str | None = None
    project_path: NonBlankStr
    capsule_present: bool = False
    goal: NonBlankStr
    base_commit: str
    base_branch: str | None = None
    created_at: str
    updated_at: str
    state: RunState = RunState.CREATED
    dry_run: bool = False
    budget: Budget = Field(default_factory=Budget)
    roles: dict[str, RoleSetting] = Field(default_factory=dict)
    work_orders: list[WorkOrder] = Field(default_factory=list)
    invocations: list[ModelInvocation] = Field(default_factory=list)
    worktrees: list[WorktreeRecord] = Field(default_factory=list)
    reviews: list[ReviewOutcome] = Field(default_factory=list)
    model_calls_used: int = Field(default=0, ge=0)
    plan_summary: str | None = None
    independence: Independence | None = None
    independence_note: str | None = None
    failure_reason: str | None = None
    finished_at: str | None = None

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, value: str) -> str:
        if RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must look like RUN-20260909T101500Z-0a1b2c3d")
        return value

    @field_validator("base_commit")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("base_commit must be a full 40-character commit sha")
        return value

    @model_validator(mode="after")
    def _budget_is_not_already_overspent(self) -> Self:
        if self.model_calls_used > self.budget.max_model_calls:
            raise ValueError("model_calls_used exceeds the run's model-call budget")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    @property
    def write_work_orders(self) -> list[WorkOrder]:
        return [item for item in self.work_orders if not item.read_only]

    def order(self, task_id: str) -> WorkOrder:
        for item in self.work_orders:
            if item.task_id == task_id:
                return item
        raise KeyError(task_id)

    def total_cost_usd(self) -> float | None:
        """Return the summed provider-reported cost, or ``None`` if unreported.

        Never estimated: if no invocation reported a cost, the answer is that
        the cost is unknown, not that it was zero.
        """

        observed = [
            item.total_cost_usd
            for item in self.invocations
            if item.total_cost_usd is not None
        ]
        if not observed:
            return None
        return sum(observed)
