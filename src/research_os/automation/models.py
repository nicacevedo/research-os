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
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.errors import RunStateError
from research_os.models import NonBlankStr

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

RUN_ID_RE = re.compile(r"^RUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
TASK_ID_RE = re.compile(r"^T-[0-9]{3}$")
INVOCATION_ID_RE = re.compile(r"^INV-[0-9]{4}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


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
    """The bounded worker roles this MVP dispatches."""

    PLANNER = "planner"
    ANALYST = "analyst"
    CODER = "coder"
    REVIEWER = "reviewer"


class Access(StrEnum):
    """What a worker is allowed to reach. The smallest distinction that is needed.

    Not a permission framework: three named positions, each with one meaning.

    ``CONTEXT_ONLY`` is the planner and the reviewer. They receive no tools at
    all and run from runtime-owned space, so they cannot reach a repository
    even if their prompt is subverted.

    ``SNAPSHOT_READ`` is the analysis worker. It receives exactly the read-only
    file tools in :data:`READ_ONLY_TOOLS` and runs inside an isolated Git
    snapshot pinned to the run's base commit, never the researcher's checkout.
    It is still read-only: the controller compares the snapshot before and
    after and fails the run if anything moved.

    ``ISOLATED_WRITE`` is the coding worker, with write tools inside its own
    disposable worktree.
    """

    CONTEXT_ONLY = "context_only"
    SNAPSHOT_READ = "snapshot_read"
    ISOLATED_WRITE = "isolated_write"


#: The only tools a snapshot-read worker may ever be given.
#:
#: Verified against the local ``claude --help`` built-in tool set. None of them
#: can modify a file or run a command: there is no ``Write``, no ``Edit``, and
#: no ``Bash``, so the analysis worker has nothing to act with, which is a
#: stronger guarantee than instructing it not to act.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep"})


class RiskClass(StrEnum):
    """How much authority a work order needs."""

    READ_ONLY = "read_only"
    SNAPSHOT_READ = "snapshot_read"
    WRITE_ISOLATED = "write_isolated"


class ExpectedOutput(StrEnum):
    """What the controller expects a completed work order to have produced."""

    DIFF = "diff"
    REPORT = "report"


class WorkOrderStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    EXECUTED = "executed"
    ANALYZED = "analyzed"
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


class DependencyArtifact(BaseModel):
    """The exact archived artifact one work order consumed from another.

    Recorded with its digest so the handoff is reconstructible: a reader can
    tell which parsed analysis a coding worker actually saw, rather than
    inferring it from the order the tasks happen to appear in.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: Role
    path: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _digest_shape(cls, value: str) -> str:
        if SHA256_RE.fullmatch(value) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class Importance(StrEnum):
    """How much one analysis finding matters. Advisory: it gates nothing."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Confidence(StrEnum):
    """How sure the analyst says it is. Advisory: it gates nothing."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AnalystFinding(BaseModel):
    """One thing an analysis worker claims to have found.

    Data, not instruction. Nothing here can change a tool set, a scope, an
    acceptance command, a budget, a worktree path, or run state; a downstream
    work order that consumes it keeps every bound the plan gave it.
    """

    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    statement: NonBlankStr
    importance: Importance
    file_refs: list[str] = Field(default_factory=list)
    confidence: Confidence

    @field_validator("id")
    @classmethod
    def _finding_id_shape(cls, value: str) -> str:
        if FINDING_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "a finding id must be 1-32 characters of letters, digits, "
                "'-', '_', or '.'"
            )
        return value

    @field_validator("file_refs")
    @classmethod
    def _repository_relative_refs(cls, value: list[str]) -> list[str]:
        """Refuse a file reference that points outside the snapshot.

        A reference is a pointer the human and the downstream worker will
        follow, so an absolute path, a ``~``, or a ``..`` segment is refused
        rather than normalised: it is the one field of analyst output that
        names the filesystem, and it is checked exactly like a scope entry.
        """

        return [_relative_path(item) for item in value]


class AnalystEvidence(BaseModel):
    """One concrete observation an analysis finding rests on."""

    model_config = ConfigDict(extra="forbid")

    finding_id: NonBlankStr
    file_ref: str
    detail: NonBlankStr

    @field_validator("file_ref")
    @classmethod
    def _repository_relative_ref(cls, value: str) -> str:
        return _relative_path(value)


class AnalystReport(BaseModel):
    """A snapshot-read worker's validated structured findings."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    summary: NonBlankStr
    findings: list[AnalystFinding] = Field(default_factory=list)
    evidence: list[AnalystEvidence] = Field(default_factory=list)
    uncertainties: list[NonBlankStr] = Field(default_factory=list)
    recommended_action: NonBlankStr
    provider: NonBlankStr
    model: str | None = None
    invocation_id: str
    snapshot_commit: str
    raw_output_path: str | None = None

    @field_validator("snapshot_commit")
    @classmethod
    def _full_commit(cls, value: str) -> str:
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("snapshot_commit must be a full 40-character commit sha")
        return value

    @model_validator(mode="after")
    def _findings_are_uniquely_identified(self) -> Self:
        ids = [item.id for item in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("analyst finding ids must not repeat")
        unknown = sorted({item.finding_id for item in self.evidence} - set(ids))
        if unknown:
            raise ValueError(
                "analyst evidence refers to findings that were not reported: "
                + ", ".join(unknown)
            )
        return self


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
    read_paths: list[str] = Field(default_factory=list)
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
    analysis_path: str | None = None
    dependency_artifacts: list[DependencyArtifact] = Field(default_factory=list)
    repair_attempts: int = Field(default=0, ge=0)
    repair_reason: str | None = None
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

    @field_validator("allowed_paths", "forbidden_paths", "read_paths")
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
        if self.role is Role.ANALYST:
            return self._validate_analysis_order()
        if self.read_only and self.risk_class is not RiskClass.READ_ONLY:
            raise ValueError("read-only work orders must use the read_only risk class")
        if not self.read_only and self.risk_class is RiskClass.READ_ONLY:
            raise ValueError("write work orders must not use the read_only risk class")
        if not self.read_only and not self.allowed_paths:
            raise ValueError("write work orders require a non-empty allowed_paths")
        if self.risk_class is RiskClass.SNAPSHOT_READ:
            raise ValueError(
                "the snapshot_read risk class belongs to an analyst work order"
            )
        if self.task_id in self.dependencies:
            raise ValueError("a work order cannot depend on itself")
        return self

    def _validate_analysis_order(self) -> Self:
        """Refuse an analysis work order that carries any write authority.

        An analyst reads a pinned snapshot and produces findings. It therefore
        has a read scope and nothing else: no writable paths, and no acceptance
        command, because the controller never runs a command on its behalf.
        """

        if not self.read_only:
            raise ValueError("an analyst work order must be read-only")
        if self.risk_class is not RiskClass.SNAPSHOT_READ:
            raise ValueError(
                "an analyst work order must use the snapshot_read risk class"
            )
        if not self.read_paths:
            raise ValueError(
                "an analyst work order requires a non-empty read_paths; the "
                "plan must state explicitly what the analyst may read"
            )
        if self.allowed_paths:
            raise ValueError(
                "an analyst work order must declare no allowed_paths; it "
                "changes nothing, so there is no write scope to grant"
            )
        if self.acceptance_commands:
            raise ValueError(
                "an analyst work order must declare no acceptance commands; "
                "the controller runs commands only against changed code"
            )
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
    max_repair_attempts: int = Field(default=1, ge=0, le=1)
    """How many bounded repair attempts one work order may make.

    The upper bound is one, enforced by the field itself rather than by the
    controller, so no configuration file, CLI flag, or future caller can turn
    the single bounded repair into a loop.
    """


class RoleSetting(BaseModel):
    """Provider, model, and reach for one worker role."""

    model_config = ConfigDict(extra="forbid")

    provider: NonBlankStr
    model: str | None = None
    effort: str | None = None
    read_only: bool
    access: Access = Access.CONTEXT_ONLY
    tools: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_access_from_authority(cls, data: Any) -> Any:
        """Derive ``access`` from ``read_only`` when it was not stated.

        Snapshot-read is the one position that must be asked for by name. A
        configuration that merely says ``read_only: true`` still means the
        context-only planner or reviewer, so an existing config file cannot
        acquire file tools by accident.
        """

        if not isinstance(data, dict) or data.get("access") is not None:
            return data
        authority = data.get("read_only")
        if not isinstance(authority, bool):
            return data
        derived = Access.CONTEXT_ONLY if authority else Access.ISOLATED_WRITE
        return {**data, "access": derived}

    @model_validator(mode="after")
    def _tools_match_access(self) -> Self:
        """Refuse any role whose declared reach and tool set disagree.

        Each position is enforced by what the worker is handed, not by what it
        is asked to do, so a role that declares two contradictory things is a
        configuration error rather than a preference the invocation layer may
        quietly correct. The invocation layer independently re-applies the same
        rule.
        """

        if self.access is Access.CONTEXT_ONLY:
            if not self.read_only:
                raise ValueError(
                    "a context_only role must be read_only; it is given no "
                    "tools and cannot write anything"
                )
            if self.tools:
                raise ValueError(
                    "a read_only role must declare no tools, but this one "
                    f"declares {', '.join(self.tools)}; context-only workers "
                    "are given an empty tool set so they cannot act on the "
                    "repository at all"
                )
            return self
        if self.access is Access.SNAPSHOT_READ:
            if not self.read_only:
                raise ValueError(
                    "a snapshot_read role must be read_only; it reads a pinned "
                    "snapshot and never writes"
                )
            if not self.tools:
                raise ValueError(
                    "a snapshot_read role must declare at least one read-only "
                    f"tool from {', '.join(sorted(READ_ONLY_TOOLS))}, otherwise "
                    "it has nothing to read the snapshot with"
                )
            forbidden = [item for item in self.tools if item not in READ_ONLY_TOOLS]
            if forbidden:
                raise ValueError(
                    "a snapshot_read role may only declare the read-only tools "
                    f"{', '.join(sorted(READ_ONLY_TOOLS))}, but this one "
                    f"declares {', '.join(forbidden)}; a snapshot reader is "
                    "never given a tool that can change a file or run a command"
                )
            return self
        if self.read_only:
            raise ValueError(
                "an isolated_write role must not be read_only; write work runs "
                "in its own disposable worktree"
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
