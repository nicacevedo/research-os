"""The typed task DAG a research run executes, and the budgets that bound it.

A research run is the layer that turns "find out whether X" into a bounded
sequence of the things this system already knows how to do. It owns no worker of
its own: every task kind here maps onto an existing controller, and the research
controller's job is dispatch, ordering, budgeting, and knowing when to stop for a
person.

Three properties are enforced by these models rather than by the controller.

**Unknown kinds fail closed.** :class:`TaskKind` is a closed enum, and the
controller dispatches only kinds it has a handler for. A plan naming something
else is refused at validation, not discovered at dispatch.

**The graph is acyclic and forward-only.** A task may depend only on earlier
tasks, which makes a topological order the declaration order and makes a cycle
impossible to express rather than something to detect.

**A budget is spent, not consulted.** Model calls, write tasks, experiments, and
cluster submissions each have a counter on the run, and the controller decrements
against the persisted value before it spends anything -- so a run that crashes
and is inspected shows what it actually spent.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.automation.models import safe_relative_path, utc_now
from research_os.automation.profile import ProjectProfile
from research_os.models import NonBlankStr

RESEARCH_RUN_ID_RE = re.compile(r"^RR-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
TASK_ID_RE = re.compile(r"^T-[0-9]{3}$")


class TaskKind(StrEnum):
    """The kinds of work a research run can dispatch.

    A closed set, and closed is the point: the controller has a handler per kind
    and refuses anything else. A plan that names a kind this build does not know
    is a plan this build must not execute, and failing at validation is better
    than discovering it half way through.
    """

    LITERATURE = "literature"
    ANALYSIS = "analysis"
    PROPOSAL = "proposal"
    CODE = "code"
    EXPERIMENT = "experiment"
    PAPER = "paper"
    HUMAN_CHECKPOINT = "human_checkpoint"


#: Kinds that write to a repository, and therefore need a worktree and a scope.
WRITING_KINDS: frozenset[TaskKind] = frozenset({TaskKind.CODE, TaskKind.PAPER})

#: Kinds that may spend real compute beyond a model call.
COSTLY_KINDS: frozenset[TaskKind] = frozenset({TaskKind.EXPERIMENT})


class ResearchState(StrEnum):
    """The deterministic states of one research run.

    Assigned only by the controller, never mapped from model output.
    ``WAITING_FOR_HUMAN`` is distinct from ``READY_FOR_HUMAN`` because they ask
    for different things: the first means the run stopped mid-plan and can
    continue once a person decides something, the second means the run finished
    and its output is waiting to be judged.
    """

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    EXECUTING = "EXECUTING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    INTERRUPTED = "INTERRUPTED"
    """The process stopped while this run was executing.

    A distinct state rather than a stale ``EXECUTING``, because the two mean
    different things to whoever finds the run: ``EXECUTING`` says something is
    happening, and after a crash nothing is. Recording the interruption is also
    what makes ``researchctl research resume`` a deliberate act -- a person
    decides what to do about the task that was in flight, rather than the
    controller guessing.
    """

    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[ResearchState] = frozenset(
    {ResearchState.READY_FOR_HUMAN, ResearchState.FAILED, ResearchState.CANCELLED}
)

#: States a stopped run may be resumed from.
#:
#: Three, and each unambiguous: a plan that has not started, a run paused at a
#: checkpoint whose answer a person has now given, and a run whose interruption
#: a person has already decided what to do about. A FAILED or CANCELLED run is
#: never resurrected -- what "continue" would mean there depends on why it
#: stopped, and guessing is how a run silently repeats a costly step.
RESUMABLE_STATES: frozenset[ResearchState] = frozenset(
    {
        ResearchState.PLAN_READY,
        ResearchState.WAITING_FOR_HUMAN,
        ResearchState.INTERRUPTED,
    }
)

#: States that mean a process stopped without finishing what it started.
#:
#: Only ``researchctl research resume`` moves a run out of one of these, and it
#: makes a person say what should happen to the task that was in flight.
INTERRUPTIBLE_STATES: frozenset[ResearchState] = frozenset(
    {ResearchState.PLANNING, ResearchState.EXECUTING}
)

_FORWARD: dict[ResearchState, frozenset[ResearchState]] = {
    ResearchState.CREATED: frozenset({ResearchState.PLANNING}),
    ResearchState.PLANNING: frozenset({ResearchState.PLAN_READY}),
    ResearchState.PLAN_READY: frozenset({ResearchState.EXECUTING}),
    ResearchState.EXECUTING: frozenset(
        {
            ResearchState.WAITING_FOR_HUMAN,
            ResearchState.INTERRUPTED,
            ResearchState.READY_FOR_HUMAN,
        }
    ),
    ResearchState.WAITING_FOR_HUMAN: frozenset({ResearchState.EXECUTING}),
    ResearchState.INTERRUPTED: frozenset({ResearchState.EXECUTING}),
    ResearchState.READY_FOR_HUMAN: frozenset(),
    ResearchState.FAILED: frozenset(),
    ResearchState.CANCELLED: frozenset(),
}


def allowed_transitions(state: ResearchState) -> frozenset[ResearchState]:
    forward = _FORWARD[state]
    if state in TERMINAL_STATES:
        return forward
    return forward | {ResearchState.FAILED, ResearchState.CANCELLED}


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"


SATISFIED_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.SKIPPED}
)


class ResearchBudget(BaseModel):
    """What one research run may spend, in every currency it spends in.

    Separate counters rather than one, because the resources are not
    interchangeable and a researcher cares about them differently. Running out
    of model calls is a cost; submitting five cluster jobs when you meant one is
    a different kind of problem.
    """

    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=8, ge=1, le=24)
    max_model_calls: int = Field(default=20, ge=1, le=200)
    max_write_tasks: int = Field(default=2, ge=0, le=8)
    max_experiments: int = Field(default=2, ge=0, le=20)
    max_cluster_submissions: int = Field(default=2, ge=0, le=20)
    max_wall_clock_seconds: int | None = Field(default=7200, ge=60)
    """How long one execution pass may keep starting new tasks.

    Checked before each task rather than enforced as a timeout, because killing
    a task mid-flight is how a run ends up holding a worktree nobody knows
    about or a cluster job nobody is watching. The bound is on the loop: past
    the limit, the run stops starting work and hands what it has to a person.
    """
    max_repair_attempts: int = Field(default=1, ge=0, le=1)
    """How many bounded repairs one work item may make.

    Capped at one by the field itself, exactly as the automation budget caps it,
    so no configuration file or future caller can turn the single bounded repair
    into a loop.
    """


class ResearchTask(BaseModel):
    """One typed unit of work in a research run's plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    kind: TaskKind
    title: NonBlankStr
    goal: NonBlankStr
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING

    query: str = ""
    """The literature query, for a literature task."""

    read_paths: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    acceptance_commands: list[list[str]] = Field(default_factory=list)
    experiment_task: str = ""
    experiment_parameters: dict[str, str] = Field(default_factory=dict)
    section: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    literature_keys: list[str] = Field(default_factory=list)
    question: str = ""
    """What the human is being asked, for a checkpoint."""

    started_at: str | None = None
    finished_at: str | None = None
    detail: str = ""
    artifact_id: str | None = None
    """The id of whatever this task produced, in whichever store owns it.

    A run id, a proposal id, an experiment run id, a draft id. The research run
    records the pointer rather than copying the record, so there is one copy of
    every artifact and it lives with the machinery that understands it.
    """

    model_calls: int = Field(default=0, ge=0)
    failure_reason: str | None = None

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        if TASK_ID_RE.fullmatch(value) is None:
            raise ValueError("task_id must look like T-001")
        return value

    @field_validator("depends_on")
    @classmethod
    def _dependency_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if TASK_ID_RE.fullmatch(item) is None:
                raise ValueError("dependencies must be task ids like T-001")
        if len(set(value)) != len(value):
            raise ValueError("dependencies must not repeat")
        return value

    @field_validator("read_paths", "allowed_paths")
    @classmethod
    def _scope_paths(cls, value: list[str]) -> list[str]:
        return [safe_relative_path(item) for item in value]

    @model_validator(mode="after")
    def _kind_specific_requirements(self) -> Self:
        """Refuse a task that is not executable as the kind it claims to be.

        Each kind needs something specific to dispatch at all, and a task
        missing it would fail at the point of spending rather than at the point
        of planning. Refusing here is the cheaper failure by a long way.
        """

        if self.task_id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        if self.kind is TaskKind.LITERATURE and not self.query.strip():
            raise ValueError(f"{self.task_id} is a literature task with no query")
        if self.kind is TaskKind.ANALYSIS and not self.read_paths:
            raise ValueError(
                f"{self.task_id} is an analysis task with no read_paths; the plan "
                "must say what it is asking to be analysed"
            )
        if self.kind is TaskKind.CODE:
            if not self.allowed_paths:
                raise ValueError(f"{self.task_id} is a code task with no allowed_paths")
            if not self.acceptance_commands:
                raise ValueError(
                    f"{self.task_id} is a code task with no acceptance command, so "
                    "the controller could not verify it"
                )
        if self.kind is TaskKind.EXPERIMENT and not self.experiment_task.strip():
            raise ValueError(
                f"{self.task_id} is an experiment task but names no declared "
                "experiment command. A command the researcher has not declared "
                "cannot be run."
            )
        if self.kind is TaskKind.PAPER:
            if not self.allowed_paths:
                raise ValueError(
                    f"{self.task_id} is a paper task with no allowed_paths"
                )
            if not self.section.strip():
                raise ValueError(f"{self.task_id} is a paper task with no section")
        if self.kind is TaskKind.HUMAN_CHECKPOINT and not self.question.strip():
            raise ValueError(
                f"{self.task_id} is a checkpoint that asks the human nothing"
            )
        for entry in self.allowed_paths:
            normalised = entry.rstrip("/")
            if normalised == ".research" or normalised.startswith(".research/"):
                raise ValueError(
                    f"{self.task_id} would write under .research/; canonical "
                    "scientific files are never written by an automated worker"
                )
        return self

    @property
    def writes(self) -> bool:
        return self.kind in WRITING_KINDS

    @property
    def costly(self) -> bool:
        return self.kind in COSTLY_KINDS

    @property
    def terminal(self) -> bool:
        return self.status in {
            TaskStatus.DONE,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }


class HumanCheckpoint(BaseModel):
    """One thing the run stopped to ask a person.

    Recorded rather than merely printed, because the answer is what lets the run
    continue, and a run resumed on an answer nobody wrote down is a run whose
    record does not explain itself.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    question: NonBlankStr
    reached_at: str = Field(default_factory=utc_now)
    answered_at: str | None = None
    answer: str | None = None
    decision: str = "pending"

    @property
    def pending(self) -> bool:
        return self.answered_at is None


class ResearchRun(BaseModel):
    """The complete record of one research run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    project_id: str | None = None
    project_path: NonBlankStr
    base_commit: str | None = None
    goal: NonBlankStr
    profile: ProjectProfile | None = None
    """What the controller established about this project before planning.

    Persisted rather than recomputed on demand, because it is the context the
    planner was actually given. A run whose plan looks wrong is read by someone
    asking what the planner knew, and "what it knew" has to be a record rather
    than a re-derivation against a tree that has since moved.
    """

    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None
    state: ResearchState = ResearchState.CREATED
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    plan_summary: str = ""
    tasks: list[ResearchTask] = Field(default_factory=list)
    checkpoints: list[HumanCheckpoint] = Field(default_factory=list)
    model_calls_used: int = Field(default=0, ge=0)
    write_tasks_used: int = Field(default=0, ge=0)
    experiments_used: int = Field(default=0, ge=0)
    cluster_submissions_used: int = Field(default=0, ge=0)
    execute_experiments: bool = False
    """Whether this run was authorised to actually execute experiments.

    Off unless the researcher said so. An experiment task in a run without it
    still plans, still resolves its command, and stops -- so the plan is visible
    without anything being spent.
    """

    independence: str | None = None
    independence_note: str | None = None
    failure_reason: str | None = None
    provider_cost_usd: float | None = None

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, value: str) -> str:
        if RESEARCH_RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must look like RR-20260912T101500Z-0a1b2c3d")
        return value

    @model_validator(mode="after")
    def _plan_is_a_forward_only_graph(self) -> Self:
        """Refuse a plan that is not a forward-only acyclic graph.

        Enforced as "a dependency must name an earlier task" rather than as a
        cycle detection pass, because the stronger rule is simpler to state, is
        what a reader assumes anyway, and makes the declaration order a valid
        execution order with no sorting at all.
        """

        seen: list[str] = []
        for index, task in enumerate(self.tasks, start=1):
            expected = f"T-{index:03d}"
            if task.task_id != expected:
                raise ValueError(
                    f"task {index} has id {task.task_id}; ids must be sequential "
                    "from T-001"
                )
            for dependency in task.depends_on:
                if dependency not in seen:
                    raise ValueError(
                        f"{task.task_id} depends on {dependency}, which is not an "
                        "earlier task. A research plan is a forward-only graph."
                    )
            seen.append(task.task_id)
        if self.model_calls_used > self.budget.max_model_calls:
            raise ValueError("model_calls_used exceeds this run's model-call budget")
        return self

    def task(self, task_id: str) -> ResearchTask:
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        raise KeyError(task_id)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def resumable(self) -> bool:
        return self.state in RESUMABLE_STATES

    @property
    def pending_checkpoints(self) -> list[HumanCheckpoint]:
        return [item for item in self.checkpoints if item.pending]

    @property
    def remaining_model_calls(self) -> int:
        return max(0, self.budget.max_model_calls - self.model_calls_used)

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for task in self.tasks:
            found[str(task.status)] = found.get(str(task.status), 0) + 1
        return found

    @property
    def unfinished(self) -> list[ResearchTask]:
        return [item for item in self.tasks if not item.terminal]
