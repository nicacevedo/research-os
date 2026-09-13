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

from research_os.automation.promptdata import (
    TASK_FENCE,
    prompt_safe,
    prompt_safe_block,
    render_data_block,
)
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

#: How much of a validator message is quoted into a correction.
MAX_REASON_CHARS = 1_000

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
                # Only the keys every task genuinely has.
                #
                # Requiring all thirteen was a mistake, and an expensive one to
                # find: a planner writing a rich four-task plan had to emit an
                # empty value for every key its kind did not use, and when one
                # was missed the provider's structured-output retries degraded
                # the whole answer to the smallest object that validated -- a
                # one-task plan whose every string was the word "test", after
                # thirteen thousand output tokens of real work. The rest of the
                # keys have defaults on ``PlannedTask``; what each kind actually
                # needs is checked by the validators below, which can say so in
                # a sentence a person can act on.
                "required": ["id", "kind", "title", "goal"],
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
{render_data_block(TASK_FENCE, prompt_safe_block(goal, limit=MAX_GOAL_CHARS).split(chr(10)))}

TASK KINDS

Every task has "id", "kind", "title" and "goal". Beyond those, set only the
keys the kind actually uses -- each kind below says which -- and leave the rest
out. "depends_on" is a list of earlier task ids, on any kind that needs one.

- "literature": retrieve and read published work. Sets "query" to the search
  you want run. Cheap. Do this first when the goal touches anything somebody
  else may already have settled.
- "analysis": a read-only investigation of this repository inside a pinned
  snapshot, with Read, Glob, and Grep and nothing else. Sets "read_paths" to
  the repository-relative paths the analysis should concentrate on. Use it when
  the goal needs the code understood before it can be changed.
- "proposal": turn what is known into structured, reviewable scientific
  proposals -- questions, hypotheses, experiments, claims. Sets no key of its
  own. Produces nothing the project accepts; a human promotes anything worth
  keeping.
- "code": a write-enabled implementation in an isolated worktree. Sets
  "allowed_paths" to the paths it may change and "acceptance_commands" to the
  argument vectors the controller will run to verify it, for example
  ["pytest", "-q"]. Available programs: {programs}.
- "experiment": run a command the researcher has already declared. Sets
  "experiment_task" to one of the declared commands below and
  "experiment_parameters" to that command's declared parameters, as strings.
  You cannot add a command, change its argv, or introduce a parameter it did
  not declare. This is the only kind that can spend real compute.
- "paper": draft a manuscript section from accepted claims. Sets
  "allowed_paths" and "section".
- "human_checkpoint": stop and ask the researcher something. Sets "question" to
  what you want them to decide.

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

Write the summary, titles, goals and queries for the researcher, because the
researcher reads them. Each one should say something specific about this
project and this goal that they could act on.


{insight_section}
{science_context}
{repository_context}
"""


def build_plan_correction_prompt(prompt: str, *, reason: str) -> str:
    """Return the planner's one re-ask, with the validator's exact objection.

    The original prompt in full, then what was wrong with the answer. Nothing
    from the rejected plan is quoted back: unlike the analyst's correction,
    where the worker needs its own findings to repair a reference, a plan that
    was refused for being placeholders is better rewritten than edited.
    """

    return f"""{prompt}

YOUR PREVIOUS ANSWER WAS REJECTED

The controller validated your last plan and refused it. This is the reason,
exactly as the validator produced it:

    {prompt_safe(reason, limit=MAX_REASON_CHARS)}

Write the plan again, properly this time. Every summary, title, goal, query and
question is read by the researcher and has to say something specific about
their project and their goal.

This is your one correction. There is no second.
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


#: Strings that are never an answer, whatever else is true of them.
#:
#: Not style policing, and deliberately narrow. A provider's structured-output
#: retry loop does not fail loudly when a rich answer keeps missing a strict
#: schema -- it converges on the smallest object that validates, and what comes
#: back is a plan whose every string is one placeholder word. Seen live, after
#: thirteen thousand output tokens of real planning. Refusing exactly that turns
#: a run that silently executes a placeholder into one that says what happened.
#:
#: A terse but real goal is none of this controller's business. Only a field
#: made of nothing but these is refused.
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "...",
        "bar",
        "baz",
        "example",
        "foo",
        "n/a",
        "na",
        "none",
        "null",
        "placeholder",
        "tbd",
        "test",
        "tests",
        "todo",
        "xxx",
    }
)


#: Structural words that carry no subject matter on their own.
#:
#: These are not placeholders. "goal", "task" and "query" appear in perfectly
#: good titles. What they cannot do is *rescue* a field that is otherwise a
#: placeholder, and that is the only thing this set is used for.
#:
#: Added after the release pilot. The guard below refused a field whose every
#: token was a placeholder, so a planner returning "test" was caught -- and one
#: returning "Test task", "Test goal." and "test query" was not, because "task",
#: "goal" and "query" are real words. The run then searched three providers for
#: "test query" against a real project and reported success.
FILLER: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "basic",
        "data",
        "dummy",
        "for",
        "generic",
        "goal",
        "here",
        "minimal",
        "of",
        "plan",
        "query",
        "sample",
        "simple",
        "step",
        "stuff",
        "task",
        "the",
        "thing",
        "things",
        "this",
        "to",
    }
)


def _tokens(value: str) -> list[str]:
    """Return lowered tokens, each also tried with punctuation stripped."""

    found: list[str] = []
    for token in value.split():
        lowered = token.lower()
        stripped = lowered.strip(".,;:!?\"'()[]")
        found.append(stripped or lowered)
    return [item for item in found if item]


def _is_placeholder(value: str) -> bool:
    """Return whether ``value`` says nothing but "this is a placeholder".

    True when at least one token is a placeholder and no token carries any
    subject matter -- so "test", "TBD.", a bare "...", and "Test task" are all
    refused, while "Read the field", "Testing the estimator" and any title with
    a real noun in it are not.

    Both halves matter. Requiring a placeholder token keeps the rule from
    grading ordinary terse prose; requiring every *other* token to be structural
    filler is what closes the gap the release pilot found.
    """

    tokens = _tokens(value)
    if not tokens:
        return False
    seen_placeholder = False
    for token in tokens:
        if token in PLACEHOLDERS:
            seen_placeholder = True
            continue
        if token in FILLER:
            continue
        return False
    return seen_placeholder


def _assert_not_a_placeholder(value: str, *, what: str, where: str) -> None:
    if _is_placeholder(value):
        raise ResearchPlanError(
            f"{where} is {value.strip()!r}, which is a placeholder rather than "
            f"{what}. The planner did not produce a usable plan, so nothing was "
            "run. Try again, or raise the model-call budget."
        )


def assert_plan_says_something(plan: ResearchPlan) -> None:
    """Refuse a plan that validates structurally but says nothing.

    Every string checked here is one a researcher reads. A plan that satisfies
    the schema and fails this is not a plan worth spending anything on, and
    saying so immediately beats discovering it in the report afterwards.
    """

    _assert_not_a_placeholder(
        plan.summary, what="a plan summary", where="the plan summary"
    )
    for task in plan.tasks:
        _assert_not_a_placeholder(
            task.title, what="a task title", where=f"{task.id}'s title"
        )
        _assert_not_a_placeholder(
            task.goal, what="a task goal", where=f"{task.id}'s goal"
        )
        if task.kind is TaskKind.LITERATURE:
            _assert_not_a_placeholder(
                task.query, what="a literature query", where=f"{task.id}'s query"
            )
        if task.kind is TaskKind.HUMAN_CHECKPOINT:
            _assert_not_a_placeholder(
                task.question,
                what="a question for the researcher",
                where=f"{task.id}'s question",
            )


def validate_research_plan(
    plan: ResearchPlan,
    *,
    budget: ResearchBudget,
    declared_experiments: frozenset[str],
    required_parameters: dict[str, frozenset[str]] | None = None,
) -> None:
    """Reject a plan the controller must not execute.

    Everything the prompt asked for is re-checked here, because the prompt is a
    request and this is the rule. The experiment check is the one that matters
    most: a plan naming a command the researcher never declared would otherwise
    fail at the moment of spending rather than at the moment of planning.

    ``required_parameters`` extends that argument to the rest of the command.
    Checking only the *name* left the check half-done, and a live pilot paid for
    the other half: a plan named a declared command and omitted its required
    seed, so the run spent three model calls and stopped a human before failing
    on something that was decidable the moment the plan arrived. A missing
    parameter is refused here instead, where the bounded plan correction can
    still fix it.
    """

    assert_plan_says_something(plan)
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
        required = (required_parameters or {}).get(
            task.experiment_task or "", frozenset()
        )
        missing = sorted(required - set(task.experiment_parameters))
        if missing:
            raise ResearchPlanError(
                f"{task.id} would run the experiment command "
                f"{task.experiment_task!r} without the parameter(s) it requires: "
                + ", ".join(missing)
                + ". Set them in this task's 'experiment_parameters'. A plan may "
                "fill in the parameters the researcher declared and no others."
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
