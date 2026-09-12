"""Turning a research goal into a bounded DAG of typed tasks.

The planner is read-only and tool-free. It sees the project's scientific state,
the repository's shape, and any promoted insights from other projects, and it
returns a small typed plan the controller can dispatch.

What makes the plan safe is not the prompt: it is that every kind in the schema
has a handler, every constraint the prompt states is re-checked locally, and a
plan that breaks one is refused before anything is dispatched. The prompt says
the constraints so a capable planner can satisfy them, not so the system can
rely on it having done so.

The one genuinely important instruction is about cost. An experiment can spend
real cluster time and a writing task spends a frontier-model call on prose; a
literature search costs almost nothing. A plan that reaches for the expensive
step first is usually a plan that did not need it, so the prompt asks for the
cheap deterministic work first and the controller's budgets make the ordering
matter.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_os.automation.promptdata import prompt_safe_block
from research_os.automation.structured import extract_json_object
from research_os.errors import ResearchPlanError
from research_os.models import NonBlankStr
from research_os.research.models import (
    WRITING_KINDS,
    ResearchBudget,
    ResearchTask,
    TaskKind,
)

MAX_GOAL_CHARS = 6_000

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
                    "kind",
                    "title",
                    "goal",
                    "depends_on",
                    "query",
                    "read_paths",
                    "allowed_paths",
                    "acceptance_commands",
                    "experiment_task",
                    "experiment_parameters",
                    "section",
                    "question",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in TaskKind],
                    },
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "query": {"type": "string"},
                    "read_paths": {"type": "array", "items": {"type": "string"}},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
                    "acceptance_commands": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "experiment_task": {"type": "string"},
                    "experiment_parameters": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "section": {"type": "string"},
                    "question": {"type": "string"},
                },
            },
        },
    },
}


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: TaskKind
    title: NonBlankStr
    goal: NonBlankStr
    depends_on: list[str] = Field(default_factory=list)
    query: str = ""
    read_paths: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    acceptance_commands: list[list[str]] = Field(default_factory=list)
    experiment_task: str = ""
    experiment_parameters: dict[str, str] = Field(default_factory=dict)
    section: str = ""
    question: str = ""


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: NonBlankStr
    tasks: list[PlannedTask] = Field(min_length=1)


def build_research_plan_prompt(
    *,
    goal: str,
    budget: ResearchBudget,
    science_context: str,
    repository_context: str,
    declared_experiments: list[str],
    insight_section: str = "",
    execute_experiments: bool = False,
    allowed_programs: tuple[str, ...] = (),
) -> str:
    """Return the complete prompt for the research planner."""

    experiments = (
        "\n".join(f"    {item}" for item in declared_experiments)
        or "    (none - this project has declared no experiment commands, so an\n"
        "     experiment task cannot be dispatched)"
    )
    programs = ", ".join(sorted(allowed_programs)) or "none"
    execution = (
        "Experiments in this run ARE authorised to execute."
        if execute_experiments
        else (
            "Experiments in this run are NOT authorised to execute. You may still\n"
            "plan one: it will resolve its command and stop, so the researcher can\n"
            "see exactly what would run. Say so in the task goal."
        )
    )
    return f"""You are the research planner of a deterministic research automation
controller. You have no tools and no repository access. Plan only from the
context below.

Produce a small, bounded plan out of typed tasks. A deterministic controller
dispatches each kind to a worker it already has; it cannot dispatch anything
else, and a plan naming an unknown kind is refused before anything runs.

THE RESEARCHER'S GOAL
{prompt_safe_block(goal, limit=MAX_GOAL_CHARS)}

TASK KINDS

- "literature": retrieve and read published work. Needs "query". Cheap. Do this
  first when the goal touches anything somebody else may already have settled.
- "analysis": a read-only investigation of this repository inside a pinned
  snapshot, with Read, Glob, and Grep and nothing else. Needs "read_paths".
  Use it when the goal needs the code understood before it can be changed.
- "proposal": turn what is known into structured, reviewable scientific
  proposals -- questions, hypotheses, experiments, claims. Produces nothing the
  project accepts; a human promotes anything worth keeping.
- "code": a write-enabled implementation in an isolated worktree, verified by
  acceptance commands the controller runs itself. Needs "allowed_paths" and
  "acceptance_commands". Available programs: {programs}.
- "experiment": run a command the researcher has already declared. Needs
  "experiment_task", naming one of the declared commands below, and
  "experiment_parameters" filling in that command's declared parameters as
  strings. You cannot add a command, change its argv, or introduce a parameter
  it did not declare. This is the only kind that can spend real compute.
- "paper": draft a manuscript section from accepted claims. Needs
  "allowed_paths" and "section".
- "human_checkpoint": stop and ask the researcher something. Needs "question".

DECLARED EXPERIMENT COMMANDS FOR THIS PROJECT
{experiments}

{execution}

HARD CONSTRAINTS. A plan that breaks any of these is refused before anything
runs, so satisfy them rather than explaining them:

- At most {budget.max_tasks} tasks.
- At most {budget.max_write_tasks} of them may write (code or paper).
- At most {budget.max_experiments} experiment tasks.
- Task ids must be "T-001", "T-002", ... in order, and every entry in
  "depends_on" must be the id of an earlier task.
- No task may write under ".research/". Those are canonical scientific files
  and only a human changes them.
- An acceptance command is an argument vector with no shell syntax: no pipes,
  no redirection, no "&&". Paths in it are relative to the worktree.
- A "code" or "paper" task must list the specific paths it may change. "src/"
  is a scope; the whole repository is not.

HOW TO ORDER IT

Cheap and certain first. Literature and analysis cost almost nothing and often
make the expensive step unnecessary; an experiment can spend real cluster time
and a writing task spends a frontier call on prose. A plan that reaches for the
expensive step first is usually a plan that did not need it.

Put a "human_checkpoint" before anything the researcher would want to decide
themselves: spending real compute, changing the project's direction, or
accepting a scientific conclusion. The controller stops there and waits.

Keep the plan as small as it can be and still achieve the goal. This run has
{budget.max_model_calls} model calls in total.

{insight_section}
{science_context}
{repository_context}
"""


def parse_research_plan(
    *, structured: dict[str, Any] | None, text: str | None
) -> ResearchPlan:
    """Turn a planner response into a validated plan document."""

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise ResearchPlanError(
            "the research planner returned no JSON object; expected a structured plan"
        )
    try:
        return ResearchPlan.model_validate(payload)
    except ValidationError as exc:
        raise ResearchPlanError(
            f"the research planner's output is not a valid plan: {exc}"
        ) from exc


def validate_research_plan(
    plan: ResearchPlan,
    *,
    budget: ResearchBudget,
    declared_experiments: frozenset[str],
) -> None:
    """Reject a plan the controller must not execute.

    Everything the prompt asked for is re-checked here, because the prompt is a
    request and this is the rule. The experiment check is the one that matters
    most: a plan naming a command the researcher never declared would otherwise
    fail at the moment of spending rather than at the moment of planning.
    """

    if len(plan.tasks) > budget.max_tasks:
        raise ResearchPlanError(
            f"the plan has {len(plan.tasks)} tasks; this run's budget allows "
            f"{budget.max_tasks}"
        )
    writes = [task for task in plan.tasks if task.kind in WRITING_KINDS]
    if len(writes) > budget.max_write_tasks:
        raise ResearchPlanError(
            f"the plan has {len(writes)} writing tasks; this run's budget allows "
            f"{budget.max_write_tasks}"
        )
    experiments = [task for task in plan.tasks if task.kind is TaskKind.EXPERIMENT]
    if len(experiments) > budget.max_experiments:
        raise ResearchPlanError(
            f"the plan has {len(experiments)} experiment tasks; this run's budget "
            f"allows {budget.max_experiments}"
        )
    for task in experiments:
        if task.experiment_task not in declared_experiments:
            raise ResearchPlanError(
                f"{task.id} would run the experiment command "
                f"{task.experiment_task!r}, which this project has not declared. "
                "Experiment commands are declared by the researcher in "
                "experiments.yaml, outside every worktree, and a plan cannot add "
                "one. Declared: " + (", ".join(sorted(declared_experiments)) or "none")
            )


def to_tasks(plan: ResearchPlan) -> list[ResearchTask]:
    """Turn a validated plan into the tasks the controller will dispatch.

    Every bound a task carries is set here, from the plan. Nothing a worker
    later says can revise it.
    """

    tasks: list[ResearchTask] = []
    for planned in plan.tasks:
        try:
            tasks.append(
                ResearchTask(
                    task_id=planned.id,
                    kind=planned.kind,
                    title=planned.title,
                    goal=planned.goal,
                    depends_on=list(planned.depends_on),
                    query=planned.query,
                    read_paths=list(planned.read_paths),
                    allowed_paths=list(planned.allowed_paths),
                    acceptance_commands=[
                        list(item) for item in planned.acceptance_commands
                    ],
                    experiment_task=planned.experiment_task,
                    experiment_parameters=dict(planned.experiment_parameters),
                    section=planned.section,
                    question=planned.question,
                )
            )
        except ValidationError as exc:
            raise ResearchPlanError(
                f"task {planned.id} is not a dispatchable task: {exc}"
            ) from exc
    return tasks
