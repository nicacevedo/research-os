"""Read-only planning: goal plus deterministic context becomes bounded work orders.

The planner has no tools, so it cannot read or write the repository directly; it
reasons only over the context packet the controller built. Its output is
schema-constrained structured data, validated again locally before anything is
dispatched, because a plan is an instruction to execute commands and run a
write-enabled worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_os.automation.checks import assert_programs_allowed
from research_os.automation.models import (
    TASK_ID_RE,
    AcceptanceCommand,
    Budget,
    ExpectedOutput,
    RiskClass,
    Role,
    RoleSetting,
    WorkOrder,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import PlanValidationError
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
                    "read_only",
                    "allowed_paths",
                    "forbidden_paths",
                    "acceptance_commands",
                    "expected_artifacts",
                    "completion_condition",
                    "dependencies",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "read_only": {"type": "boolean"},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
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


class PlannedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[NonBlankStr] = Field(min_length=1)
    description: str = ""


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: NonBlankStr
    goal: NonBlankStr
    read_only: bool
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    acceptance_commands: list[PlannedCommand] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    completion_condition: NonBlankStr
    dependencies: list[str] = Field(default_factory=list)


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
    return f"""You are the planning worker of a deterministic research automation
controller. You have no tools and no repository access. Plan only from the
context below.

Produce a bounded work plan that a single write-enabled coding worker can carry
out in one isolated Git worktree, and that a deterministic controller can then
verify by running commands itself.

USER GOAL
{goal}

REPOSITORY
{project_path}

HARD CONSTRAINTS on the plan you return. A plan that breaks any of these is
rejected by the controller before anything runs:

- At most {budget.max_work_orders} tasks, and at most
  {budget.max_write_work_orders} of them may write.
- Every task must set "read_only": false. This controller dispatches only
  write-enabled coding work orders; there is no read-only executor.
- Task ids must be "T-001", "T-002", ... in order, and any entry in
  "dependencies" must be the id of an earlier task.
- "allowed_paths" must be non-empty for every task and must list
  repository-relative paths (files or directories, POSIX separators, no
  leading "/", no "..", no "~"). The controller fails the task if the worker
  changes anything outside them.
- "acceptance_commands" must be non-empty for every task. Each is an argument
  vector run by the controller, without a shell, in the task's worktree. The
  first element must be one of: {programs}. No shell operators, pipes,
  redirection, or "&&" - they will not be interpreted.
- Prefer the narrowest commands that actually prove the goal was met, for
  example a specific test file rather than the entire suite.
- "completion_condition" must be an objective, checkable statement.
- Do not plan anything that edits files under a ".research/" directory. Those
  are canonical scientific files and only a human may change their meaning.
- Do not plan commits, merges, pushes, network access, or dependency
  installation.

Keep the plan as small as it can be while still achieving the goal. One task is
usually the right answer.

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


def validate_plan(
    plan: PlanDocument,
    *,
    budget: Budget,
    allowed_programs: tuple[str, ...],
) -> None:
    """Reject a plan the controller must not execute."""

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
        if task.read_only:
            raise PlanValidationError(
                f"task {task.id} is read-only; this controller dispatches only "
                "write-enabled coding work orders"
            )
        if not task.allowed_paths:
            raise PlanValidationError(f"task {task.id} has an empty allowed_paths")
        if any(item.startswith(".research/") for item in task.allowed_paths):
            raise PlanValidationError(
                f"task {task.id} would write under .research/; canonical "
                "scientific files are never written by an automated worker"
            )
        if not task.acceptance_commands:
            raise PlanValidationError(
                f"task {task.id} declares no acceptance command, so the "
                "controller could not verify it"
            )
        seen.append(task.id)

    writes = [task for task in plan.tasks if not task.read_only]
    if len(writes) > budget.max_write_work_orders:
        raise PlanValidationError(
            f"plan has {len(writes)} write tasks; the run budget allows "
            f"{budget.max_write_work_orders}"
        )

    try:
        for task in plan.tasks:
            assert_programs_allowed(
                [
                    AcceptanceCommand(argv=list(command.argv))
                    for command in task.acceptance_commands
                ],
                allowed_programs,
            )
    except (ValueError, ValidationError) as exc:
        raise PlanValidationError(str(exc)) from exc


def plan_to_work_orders(
    plan: PlanDocument,
    *,
    project_path: Path,
    base_commit: str,
    coder: RoleSetting,
    timeout_seconds: int,
) -> list[WorkOrder]:
    """Turn a validated plan into work orders the controller will dispatch."""

    orders: list[WorkOrder] = []
    for task in plan.tasks:
        try:
            orders.append(
                WorkOrder(
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
                    expected_artifacts=[
                        item for item in task.expected_artifacts if item.strip()
                    ],
                    completion_condition=task.completion_condition,
                    timeout_seconds=timeout_seconds,
                    provider=coder.provider,
                    model=coder.model,
                    expected_output=ExpectedOutput.DIFF,
                    dependencies=list(task.dependencies),
                )
            )
        except ValidationError as exc:
            raise PlanValidationError(
                f"task {task.id} is not a valid work order: {exc}"
            ) from exc
    return orders
