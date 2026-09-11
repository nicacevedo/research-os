"""Read-only planning: goal plus deterministic context becomes bounded work orders.

The planner has no tools, so it cannot read or write the repository directly; it
reasons only over the context packet the controller built. Its output is
schema-constrained structured data, validated again locally before anything is
dispatched, because a plan is an instruction to execute commands and run a
write-enabled worker.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_os.automation.command_policy import (
    SUPPORTED_COMMAND_FORMS,
    authorize_planner_commands,
)
from research_os.automation.models import (
    TASK_ID_RE,
    AcceptanceCommand,
    Budget,
    ExpectedOutput,
    RiskClass,
    Role,
    RoleSetting,
    WorkOrder,
    safe_relative_path,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import CommandPolicyError, PlanValidationError
from research_os.models import NonBlankStr

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tasks"],
    "properties": {
        "summary": {"type": "string"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "title",
                    "goal",
                    "role",
                    "read_only",
                    "allowed_paths",
                    "forbidden_paths",
                    "read_paths",
                    "acceptance_commands",
                    "expected_artifacts",
                    "completion_condition",
                    "dependencies",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "role": {"type": "string", "enum": ["analyst", "coder"]},
                    "read_only": {"type": "boolean"},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
                    "read_paths": {"type": "array", "items": {"type": "string"}},
                    "forbidden_paths": {"type": "array", "items": {"type": "string"}},
                    "acceptance_commands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["argv", "description"],
                            "properties": {
                                "argv": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "expected_artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "completion_condition": {"type": "string"},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


class PlannedRole(StrEnum):
    """The roles a plan may assign to a task.

    Deliberately not :class:`~research_os.automation.models.Role`: a plan may
    not assign the planner or the reviewer, which the controller dispatches
    itself.
    """

    ANALYST = "analyst"
    CODER = "coder"


class PlannedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[NonBlankStr] = Field(min_length=1)
    description: str = ""


class PlannedTask(BaseModel):
    """One task as the planner proposed it, before local validation.

    ``role`` defaults to the coding worker so a plan that names no role still
    means what it meant before analysis existed: analysis has to be asked for.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: NonBlankStr
    goal: NonBlankStr
    role: PlannedRole = PlannedRole.CODER
    read_only: bool
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    read_paths: list[str] = Field(default_factory=list)
    acceptance_commands: list[PlannedCommand] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    completion_condition: NonBlankStr
    dependencies: list[str] = Field(default_factory=list)

    @property
    def is_analysis(self) -> bool:
        return self.role is PlannedRole.ANALYST


class PlanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: NonBlankStr
    tasks: list[PlannedTask] = Field(min_length=1)


def build_planner_prompt(
    *,
    goal: str,
    context_text: str,
    budget: Budget,
    allowed_programs: tuple[str, ...],
    project_path: Path,
) -> str:
    """Return the complete planner prompt.

    States the boundaries as constraints on valid output rather than as
    etiquette: everything below is re-checked locally, so a plan that ignores
    them is rejected instead of executed.
    """

    programs = ", ".join(sorted(allowed_programs))
    forms = "\n".join(f"    {item}" for item in SUPPORTED_COMMAND_FORMS)
    return f"""You are the planning worker of a deterministic research automation
controller. You have no tools and no repository access. Plan only from the
context below.

Produce a bounded work plan out of two kinds of task, which a deterministic
controller then dispatches and verifies itself.

TASK KINDS

- "role": "analyst" - a read-only investigation. It runs inside an isolated Git
  snapshot with exactly the Read, Glob, and Grep tools, so it can look at real
  files. It has no Write, no Edit, and no Bash: it changes nothing, runs
  nothing, and returns structured findings. Use it when the goal needs the
  repository understood before it can be changed - why a result is wrong, where
  a defect actually lives, what the tests really assert.
- "role": "coder" - a write-enabled implementation. It runs inside its own
  disposable Git worktree, and the controller verifies it afterwards by running
  acceptance commands itself.

A coding task may depend on an analysis task. When it does, the controller
gives the coding worker the analyst's validated findings as data. A typical
research plan is therefore two tasks:

    T-001  analyst   investigate and explain
    T-002  coder     implement the smallest correction, depends_on T-001

USER GOAL
{goal}

REPOSITORY
{project_path}

HARD CONSTRAINTS on the plan you return. A plan that breaks any of these is
rejected by the controller before anything runs:

- At most {budget.max_work_orders} tasks, and at most
  {budget.max_write_work_orders} of them may write.
- Task ids must be "T-001", "T-002", ... in order, and any entry in
  "dependencies" must be the id of an earlier task.
- Every task must set "role" to either "analyst" or "coder".

For an "analyst" task:
- "read_only" must be true.
- "read_paths" must be non-empty and must list the repository-relative paths
  the analysis should concentrate on (POSIX separators, no leading "/", no
  "..", no "~", no control characters). It states the focus of the task and is
  recorded with the run; what actually bounds the analyst is the isolated
  snapshot it runs in and its three read-only tools, so name the paths that
  make the task clear rather than trying to describe a permission.
- "allowed_paths" and "acceptance_commands" must both be empty. An analyst
  writes nothing, so it has no write scope, and the controller runs no command
  on its behalf.

For a "coder" task:
- "read_only" must be false, and "read_paths" must be empty.
- "allowed_paths" must be non-empty and must list repository-relative paths
  (files or directories, POSIX separators, no leading "/", no "..", no "~").
  The controller fails the task if the worker changes anything outside them.
- "acceptance_commands" must be non-empty. Each is an argument vector run by
  the controller, without a shell, in the task's worktree. Only these command
  forms are authorised, and only these programs are available: {programs}.
{forms}
  Every path argument must be relative to the worktree: no leading "/", no
  "..", no "~". No shell operators, pipes, redirection, or "&&" - they are not
  interpreted, and a command containing them is rejected.
- Prefer the narrowest commands that actually prove the goal was met, for
  example a specific test file rather than the entire suite.

For every task:
- "completion_condition" must be an objective, checkable statement.
- Do not plan anything that edits files under a ".research/" directory. Those
  are canonical scientific files and only a human may change their meaning.
- Do not plan commits, merges, pushes, network access, or dependency
  installation.

Keep the plan as small as it can be while still achieving the goal. Each task
costs model calls, and this run has {budget.max_model_calls} in total: one
analysis task costs at least one, and one coding task at least two.

{context_text}
"""


def parse_plan(
    *,
    structured: dict[str, Any] | None,
    text: str | None,
) -> PlanDocument:
    """Turn a planner response into a validated plan document."""

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise PlanValidationError(
            "planner returned no JSON object; expected structured plan output"
        )
    try:
        return PlanDocument.model_validate(payload)
    except ValidationError as exc:
        raise PlanValidationError(f"planner output is not a valid plan: {exc}") from exc


#: Model calls the controller must be able to make for one coding task.
#:
#: One coding invocation and one review invocation. A bounded repair is not
#: counted: it is optional, so requiring budget for it up front would refuse
#: plans that never need it.
MODEL_CALLS_PER_CODING_TASK = 2

#: Model calls the controller must be able to make for one analysis task.
MODEL_CALLS_PER_ANALYSIS_TASK = 1


def minimum_model_calls(plan: PlanDocument) -> int:
    """Return the fewest model calls this plan could possibly need."""

    return sum(
        MODEL_CALLS_PER_ANALYSIS_TASK
        if task.is_analysis
        else MODEL_CALLS_PER_CODING_TASK
        for task in plan.tasks
    )


def validate_plan(
    plan: PlanDocument,
    *,
    budget: Budget,
    allowed_programs: tuple[str, ...],
) -> None:
    """Reject a plan the controller must not execute.

    The model-call check here is deliberately the weaker of the two the
    controller applies: it refuses a plan that could not fit this run's whole
    budget however the calls were counted, which is a fact about the plan. What
    actually remains after planning is checked again before execution begins,
    where the answer depends on what has already been spent.
    """

    if len(plan.tasks) > budget.max_work_orders:
        raise PlanValidationError(
            f"plan has {len(plan.tasks)} tasks; the run budget allows "
            f"{budget.max_work_orders}"
        )

    seen: list[str] = []
    for index, task in enumerate(plan.tasks, start=1):
        if TASK_ID_RE.fullmatch(task.id) is None:
            raise PlanValidationError(f"task id {task.id!r} must look like T-001")
        if task.id in seen:
            raise PlanValidationError(f"duplicate task id {task.id}")
        if task.id != f"T-{index:03d}":
            raise PlanValidationError(
                f"task {index} has id {task.id}; ids must be sequential from T-001"
            )
        for dependency in task.dependencies:
            if dependency not in seen:
                raise PlanValidationError(
                    f"task {task.id} depends on {dependency}, which is not an "
                    "earlier task"
                )
        if task.is_analysis:
            _validate_analysis_task(task)
        else:
            _validate_coding_task(task)
        seen.append(task.id)

    writes = [task for task in plan.tasks if not task.read_only]
    if len(writes) > budget.max_write_work_orders:
        raise PlanValidationError(
            f"plan has {len(writes)} write tasks; the run budget allows "
            f"{budget.max_write_work_orders}"
        )

    required = minimum_model_calls(plan)
    if required > budget.max_model_calls:
        raise PlanValidationError(
            f"plan cannot be executed within its budget: {len(plan.tasks)} "
            f"tasks need at least {required} model calls and this run's whole "
            f"budget is {budget.max_model_calls}"
        )

    try:
        for task in plan.tasks:
            authorize_planner_commands(
                [
                    AcceptanceCommand(argv=list(command.argv))
                    for command in task.acceptance_commands
                ],
                allowed_programs,
            )
    except (CommandPolicyError, ValueError, ValidationError) as exc:
        raise PlanValidationError(str(exc)) from exc


def _validate_coding_task(task: PlannedTask) -> None:
    """Reject a coding task the controller must not dispatch."""

    if task.read_only:
        raise PlanValidationError(
            f"task {task.id} is read-only; a coding work order writes code. "
            "Read-only work belongs to an analyst task, which must set "
            '"role": "analyst"'
        )
    if not task.allowed_paths:
        raise PlanValidationError(f"task {task.id} has an empty allowed_paths")
    if any(_is_research_scope(item) for item in task.allowed_paths):
        raise PlanValidationError(
            f"task {task.id} would write under .research/; canonical "
            "scientific files are never written by an automated worker"
        )
    if not task.acceptance_commands:
        raise PlanValidationError(
            f"task {task.id} declares no acceptance command, so the "
            "controller could not verify it"
        )
    if task.read_paths:
        raise PlanValidationError(
            f"task {task.id} declares read_paths, which belong to an analyst "
            "task; a coding task states its scope in allowed_paths"
        )


def _validate_analysis_task(task: PlannedTask) -> None:
    """Reject an analysis task that asks for anything but a bounded read.

    The analysis scope must be stated explicitly and must be a set of
    repository-relative paths inside the pinned snapshot: an absolute path, a
    ``~``, or a ``..`` segment is refused here, and refused again when the work
    order is built. That validation keeps the field a set of plain in-repository
    names; it does not make it a filesystem permission. The analyst's boundary
    is the snapshot it runs in and the read-only tool set it is given, and
    ``read_paths`` is the focus the plan asked for, recorded for provenance.
    """

    if not task.read_only:
        raise PlanValidationError(
            f"task {task.id} is an analyst task, so it must set "
            '"read_only": true; an analyst never writes'
        )
    if not task.read_paths:
        raise PlanValidationError(
            f"task {task.id} is an analyst task with an empty read_paths; the "
            "plan must state explicitly which repository-relative paths the "
            "analysis is meant to concentrate on"
        )
    for entry in task.read_paths:
        try:
            safe_relative_path(entry)
        except ValueError as exc:
            raise PlanValidationError(
                f"task {task.id} asks to analyse {entry!r}; an analysis scope "
                f"may only name paths relative to the pinned snapshot: {exc}"
            ) from exc
    if task.allowed_paths:
        raise PlanValidationError(
            f"task {task.id} is an analyst task but declares allowed_paths; an "
            "analyst changes nothing, so it is given no write scope"
        )
    if task.acceptance_commands:
        raise PlanValidationError(
            f"task {task.id} is an analyst task but declares acceptance "
            "commands; the controller runs commands only against changed code"
        )


def _is_research_scope(entry: str) -> bool:
    """Return whether a scope entry names the capsule directory itself.

    ``.research`` and ``.research/`` grant exactly the authority ``.research/x``
    does, so all three are refused here rather than only the last one. Execution
    time refuses them again from the observed diff.
    """

    normalised = entry.rstrip("/")
    return normalised == ".research" or normalised.startswith(".research/")


def plan_to_work_orders(
    plan: PlanDocument,
    *,
    project_path: Path,
    base_commit: str,
    coder: RoleSetting,
    timeout_seconds: int,
    analyst: RoleSetting | None = None,
    analyst_timeout_seconds: int | None = None,
) -> list[WorkOrder]:
    """Turn a validated plan into work orders the controller will dispatch.

    Every bound a work order carries is set here, from the plan and the
    resolved role settings. Nothing a worker later says can revise it.
    """

    orders: list[WorkOrder] = []
    for task in plan.tasks:
        if task.is_analysis and analyst is None:
            raise PlanValidationError(
                f"task {task.id} is an analyst task but no analyst role is "
                "configured for this run"
            )
        try:
            orders.append(
                _analysis_order(
                    task,
                    project_path=project_path,
                    base_commit=base_commit,
                    analyst=analyst,  # type: ignore[arg-type]
                    timeout_seconds=analyst_timeout_seconds or timeout_seconds,
                )
                if task.is_analysis
                else _coding_order(
                    task,
                    project_path=project_path,
                    base_commit=base_commit,
                    coder=coder,
                    timeout_seconds=timeout_seconds,
                )
            )
        except ValidationError as exc:
            raise PlanValidationError(
                f"task {task.id} is not a valid work order: {exc}"
            ) from exc
    return orders


def _coding_order(
    task: PlannedTask,
    *,
    project_path: Path,
    base_commit: str,
    coder: RoleSetting,
    timeout_seconds: int,
) -> WorkOrder:
    return WorkOrder(
        task_id=task.id,
        title=task.title,
        goal=task.goal,
        role=Role.CODER,
        risk_class=RiskClass.WRITE_ISOLATED,
        project_path=str(project_path),
        base_commit=base_commit,
        read_only=False,
        allowed_paths=list(task.allowed_paths),
        forbidden_paths=list(task.forbidden_paths),
        acceptance_commands=[
            AcceptanceCommand(
                argv=list(command.argv),
                description=command.description or None,
            )
            for command in task.acceptance_commands
        ],
        expected_artifacts=[item for item in task.expected_artifacts if item.strip()],
        completion_condition=task.completion_condition,
        timeout_seconds=timeout_seconds,
        provider=coder.provider,
        model=coder.model,
        expected_output=ExpectedOutput.DIFF,
        dependencies=list(task.dependencies),
    )


def _analysis_order(
    task: PlannedTask,
    *,
    project_path: Path,
    base_commit: str,
    analyst: RoleSetting,
    timeout_seconds: int,
) -> WorkOrder:
    return WorkOrder(
        task_id=task.id,
        title=task.title,
        goal=task.goal,
        role=Role.ANALYST,
        risk_class=RiskClass.SNAPSHOT_READ,
        project_path=str(project_path),
        base_commit=base_commit,
        read_only=True,
        allowed_paths=[],
        forbidden_paths=list(task.forbidden_paths),
        read_paths=list(task.read_paths),
        acceptance_commands=[],
        expected_artifacts=[item for item in task.expected_artifacts if item.strip()],
        completion_condition=task.completion_condition,
        timeout_seconds=timeout_seconds,
        provider=analyst.provider,
        model=analyst.model,
        expected_output=ExpectedOutput.REPORT,
        dependencies=list(task.dependencies),
    )
