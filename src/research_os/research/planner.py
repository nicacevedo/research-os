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

from research_os.automation.checkprofiles import CheckProfile, render_check_profiles
from research_os.automation.promptdata import (
    TASK_FENCE,
    prompt_safe,
    prompt_safe_block,
    render_data_block,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import ResearchPlanError
from research_os.models import NonBlankStr
from research_os.research.checkpoints import (
    HARD_CHECKPOINT_KINDS,
    SCIENTIFIC_ONLY_REASON,
    CheckpointContext,
    CheckpointKind,
    CheckpointPolicy,
    eligibility_failure,
)
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
                    "required_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "experiment_task": {"type": "string"},
                    "experiment_parameters": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "section": {"type": "string"},
                    "question": {"type": "string"},
                    "checkpoint_kind": {
                        "type": "string",
                        "enum": [item.value for item in CheckpointKind],
                    },
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
    required_checks: list[str] = Field(default_factory=list)
    experiment_task: str = ""
    experiment_parameters: dict[str, str] = Field(default_factory=dict)
    section: str = ""
    question: str = ""
    checkpoint_kind: CheckpointKind = CheckpointKind.DISCRETIONARY


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
    experiment_parameters: dict[str, list[str]] | None = None,
    insight_section: str = "",
    execute_experiments: bool = False,
    allowed_programs: tuple[str, ...] = (),
    profile_context: str = "",
    capsule_present: bool = True,
    checkpoint_policy: CheckpointPolicy = CheckpointPolicy.STANDARD,
    check_profiles: tuple[CheckProfile, ...] = (),
) -> str:
    """Return the complete prompt for the research planner."""

    # The declared commands *and* what each of them requires. Listing only the
    # names was a defect a live pilot paid for twice: the validator refuses a
    # plan that omits a required parameter -- correctly, since the researcher
    # declared it -- and the prompt had never said the parameter existed. The
    # controller knew, and did not say.
    parameters = experiment_parameters or {}
    experiments = (
        "\n".join(
            f"    {item}"
            + (
                "\n        parameters: " + ", ".join(parameters[item])
                if parameters.get(item)
                else "\n        parameters: none"
            )
            for item in declared_experiments
        )
        or "    (none - this project has declared no experiment commands, so an\n"
        "     experiment task cannot be dispatched)"
    )
    programs = ", ".join(sorted(allowed_programs)) or "none"
    checks = render_check_profiles(check_profiles)
    check_rule = (
        'A "code" task must set "required_checks" and must NOT set '
        '"acceptance_commands". This project has controller-owned validation '
        "profiles, so the environment invocation is already decided and a plan "
        "that writes its own command will be refused."
        if check_profiles
        else 'A "code" task must set "acceptance_commands". This project has '
        "no controller-owned validation profile, so there is no check id to "
        "name."
    )
    code_kind = (
        f"""a write-enabled implementation in an isolated worktree. Sets
  "allowed_paths" to the paths it may change and "required_checks" to the ids of
  the checks below that must pass, for example ["tests", "lint"].
  Do not write a command yourself: the controller already knows the exact
  argument vector each id runs for this project, and it is the one that actually
  works in this project's environment.

  THIS PROJECT'S CONTROLLER-OWNED CHECKS
{checks}"""
        if check_profiles
        else f"""a write-enabled implementation in an isolated worktree. Sets
  "allowed_paths" to the paths it may change and "acceptance_commands" to the
  argument vectors the controller will run to verify it, for example
  ["pytest", "-q"]. This project has no controller-owned validation profile, so
  the plan must name the commands itself. Available programs: {programs}."""
    )
    checkpoint_kinds = "\n".join(f"    {item.value}" for item in CheckpointKind)
    checkpoint_guidance = (
        """Put a "human_checkpoint" before anything the researcher would want to decide
themselves: spending real compute, changing the project's direction, or
accepting a scientific conclusion. The controller stops there and waits."""
        if checkpoint_policy is CheckpointPolicy.STANDARD
        else """This run is unattended, so nobody is waiting to answer a question.
Only a hard checkpoint may stop it: """
        + ", ".join(sorted(item.value for item in HARD_CHECKPOINT_KINDS))
        + """.
Any plan containing a discretionary checkpoint is refused. Where you would have
asked, decide, and say in the task goal what you decided."""
    )
    proposal_kind = (
        """turn what is known into structured, reviewable scientific
  proposals -- questions, hypotheses, experiments, claims. Sets no key of its
  own. Produces nothing the project accepts; a human promotes anything worth
  keeping."""
        if capsule_present
        else """produce a grounded TECHNICAL ASSESSMENT of this
  repository. Sets no key of its own. This project has no Research Capsule, so
  there is no scientific proposal to make and no scientific identifier to cite:
  the controller dispatches this task to the assessment worker, which reasons
  about repository files, deterministic checks and retrieved literature. Use it
  when the goal asks what state this repository is in or what to do next with
  it."""
    )
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

{profile_context}

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
- "proposal": {proposal_kind}
- "code": {code_kind}
- "experiment": run a command the researcher has already declared. Sets
  "experiment_task" to one of the declared commands below and
  "experiment_parameters" to a value for every parameter that command lists as
  required, as strings. A plan that omits one is refused. You cannot add a
  command, change its argv, or introduce a parameter it did not declare. This
  is the only kind that can spend real compute.
- "paper": draft a manuscript section from accepted claims. Sets
  "allowed_paths" and "section".
- "human_checkpoint": stop and ask the researcher something. Sets "question" to
  what you want them to decide and "checkpoint_kind" to what kind of decision it
  is. The kinds are:
{checkpoint_kinds}
  The controller checks the kind against what it knows about this project: a
  kind this project cannot corroborate -- a Claim acceptance where there is no
  Claim, a prespecified-criterion change where nothing is prespecified -- is
  refused, and labelling a preference as a scientific boundary does not make it
  one.

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
- {check_rule}
- A "code" or "paper" task must list the specific paths it may change. "src/"
  is a scope; the whole repository is not.

HOW TO ORDER IT

Cheap and certain first. Literature and analysis cost almost nothing and often
make the expensive step unnecessary; an experiment can spend real cluster time
and a writing task spends a frontier call on prose. A plan that reaches for the
expensive step first is usually a plan that did not need it.

{checkpoint_guidance}

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
        "answer",
        "basic",
        "data",
        "description",
        "dummy",
        "first",
        "for",
        "generic",
        "goal",
        "here",
        "minimal",
        "of",
        "one",
        "output",
        "plan",
        "query",
        "result",
        "results",
        "sample",
        "second",
        "simple",
        "step",
        "stuff",
        "summary",
        "task",
        "the",
        "thing",
        "things",
        "third",
        "this",
        "three",
        "title",
        "to",
        "two",
    }
)

#: The shortest a field a researcher reads may be and still say anything.
#:
#: Two characters. Not a style rule and not a guess at what is interesting: a
#: title of "t", a goal of "g" and a literature query of "q" are not terse, they
#: are empty, and the placeholder guard could not see them because a single
#: letter is neither a known placeholder word nor a word carrying subject
#: matter.
#:
#: Found live, and the way it was found is the reason the bound is here rather
#: than in a longer word list. A run against a real repository was handed
#: ``{"summary": "test summary two", "tasks": [{"title": "t", "goal": "g",
#: "query": "q"}]}``, searched three literature providers for "q", and reported
#: READY_FOR_HUMAN. The build record describes the same shape of failure one
#: release earlier, caught then by adding words; this is the rule that does not
#: depend on having guessed the word.
MIN_FIELD_CHARS = 3


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

    Two independent rules, because there are two ways for a field to say
    nothing and only one of them is about words.

    **Too short to say anything.** A field of one or two characters carries no
    subject matter whatever those characters are. This is the rule that does not
    depend on having guessed a word in advance: "t", "g" and "q" are caught by
    it, and they were not caught by anything before it existed.

    **Placeholder words and nothing else.** At least one token is a known
    placeholder and every other token is structural filler -- so "test", "TBD.",
    a bare "...", "Test task" and "test summary two" are refused, while "Read
    the field", "Testing the estimator" and any field with a real noun in it are
    not.

    That second rule dropped its placeholder requirement for one commit, and an
    independent review was right to object. Refusing any field built entirely of
    filler also refuses "Query the data", "Results summary" and "First results"
    -- ordinary titles a planner may legitimately write -- and the cost of that
    is a re-ask spent on a plan that was fine, then a failed run on the second
    identical phrasing. The rule is back to requiring a placeholder token, which
    still catches every degenerate field any live pilot has produced, because
    every one of them contained "test".

    What the length rule catches is the class the word list cannot: "t", "g" and
    "q" are not placeholders in any list and never will be.
    """

    stripped = value.strip()
    if not stripped:
        return False
    if len(stripped) < MIN_FIELD_CHARS:
        return True
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
            f"{what}. Every one of these is read by the researcher and has to "
            "say something specific about their project and their goal. The "
            "planner did not produce a usable plan, so nothing was run. Try "
            "again, or raise the model-call budget."
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
    check_profiles: tuple[CheckProfile, ...] = (),
    checkpoint_policy: CheckpointPolicy = CheckpointPolicy.STANDARD,
    checkpoint_context: CheckpointContext | None = None,
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
    _assert_checks_are_the_controllers(plan, check_profiles)
    _assert_checkpoints_are_permitted(
        plan,
        policy=checkpoint_policy,
        context=checkpoint_context or CheckpointContext.empty(),
    )
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


def _assert_checkpoints_are_permitted(
    plan: ResearchPlan,
    *,
    policy: CheckpointPolicy,
    context: CheckpointContext,
) -> None:
    """Refuse a checkpoint this run must not stop at, or cannot honestly claim.

    Two separate rules, applied in this order on purpose.

    **Eligibility first, under every policy.** A hard kind the project cannot
    corroborate is refused whether or not the run is unattended, because a run
    that stops for a "Claim acceptance" in a project with no Claims has stopped
    for nothing, and a ``standard`` run that accepted the label would leave the
    relabelling path open for the unattended one to walk down later.

    **Then the policy.** ``scientific_only`` permits hard checkpoints only. The
    reason it gives is one fixed string, quoted verbatim into the planner's one
    bounded correction, so the planner is told the rule rather than a paraphrase.
    """

    experiment_tasks = sum(1 for item in plan.tasks if item.kind is TaskKind.EXPERIMENT)
    for index, task in enumerate(plan.tasks):
        if task.kind is not TaskKind.HUMAN_CHECKPOINT:
            if task.checkpoint_kind is not CheckpointKind.DISCRETIONARY:
                raise ResearchPlanError(
                    f"{task.id} is a {task.kind} task and declares "
                    f"checkpoint_kind {task.checkpoint_kind.value!r}; only a "
                    "human_checkpoint task has a checkpoint kind"
                )
            continue
        failure = eligibility_failure(
            task.checkpoint_kind,
            context=context,
            experiment_tasks=experiment_tasks,
            position=index,
            later_experiment_tasks=sum(
                1
                for item in plan.tasks[index + 1 :]
                if item.kind is TaskKind.EXPERIMENT
            ),
        )
        if failure is not None:
            raise ResearchPlanError(
                f"{task.id} declares checkpoint_kind "
                f"{task.checkpoint_kind.value!r}, but {failure}. Give the "
                "checkpoint the kind it actually is, or drop it and decide."
            )
        if (
            policy is CheckpointPolicy.SCIENTIFIC_ONLY
            and task.checkpoint_kind is CheckpointKind.DISCRETIONARY
        ):
            raise ResearchPlanError(
                f"{task.id} is a discretionary checkpoint and this run's "
                f"checkpoint policy is {policy.value}. "
                + SCIENTIFIC_ONLY_REASON
                + " Revise the plan to continue without it, within the same "
                "budgets, and say in the task goal what you decided."
            )


def _assert_checks_are_the_controllers(
    plan: ResearchPlan, check_profiles: tuple[CheckProfile, ...]
) -> None:
    """Refuse a plan that writes its own command where the controller has one.

    The trap this closes is recorded in the v1.0.0 build record. A ``src``-layout
    Python project's tests only import under ``uv run``; a planner wrote a bare
    ``pytest``; collection failed; the single bounded repair spent itself on the
    import error and the run failed closed. Nothing about that was a model being
    careless -- it was a model being asked an environment question it had no way
    to answer.

    Where the controller can answer it, the model is not asked. A plan naming
    ``["tests", "lint"]`` gets whatever this project's tests and lint actually
    are; a plan writing argv beside those profiles is refused here, early enough
    that the bounded plan correction can fix it.
    """

    available = {item.check_id for item in check_profiles}
    for task in plan.tasks:
        if task.kind is not TaskKind.CODE:
            if task.required_checks:
                raise ResearchPlanError(
                    f"{task.id} is a {task.kind} task and names required_checks; "
                    "only a code task is verified by the controller's checks"
                )
            continue
        if check_profiles:
            if task.acceptance_commands:
                raise ResearchPlanError(
                    f"{task.id} writes its own acceptance command, but this "
                    "project has controller-owned validation profiles. Name the "
                    "checks by id in 'required_checks' instead and leave "
                    "'acceptance_commands' out; the controller already knows the "
                    "exact command each id runs in this project's environment. "
                    "Available: " + ", ".join(sorted(available))
                )
            if not task.required_checks:
                raise ResearchPlanError(
                    f"{task.id} is a code task that names no required_checks. "
                    "This project's controller-owned checks are: "
                    + ", ".join(sorted(available))
                )
            unknown = sorted(set(task.required_checks) - available)
            if unknown:
                raise ResearchPlanError(
                    f"{task.id} requires checks this project has no profile for: "
                    + ", ".join(unknown)
                    + ". Available: "
                    + ", ".join(sorted(available))
                )
            continue
        if task.required_checks:
            raise ResearchPlanError(
                f"{task.id} names required_checks, but this project has no "
                "controller-owned validation profile, so there is no check id to "
                "resolve. Name the acceptance commands in 'acceptance_commands'."
            )
        if not task.acceptance_commands:
            raise ResearchPlanError(
                f"{task.id} is a code task with no acceptance command, so the "
                "controller could not verify it"
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
                    required_checks=list(planned.required_checks),
                    experiment_task=planned.experiment_task,
                    experiment_parameters=dict(planned.experiment_parameters),
                    section=planned.section,
                    question=planned.question,
                    checkpoint_kind=planned.checkpoint_kind,
                )
            )
        except ValidationError as exc:
            raise ResearchPlanError(
                f"task {planned.id} is not a dispatchable task: {exc}"
            ) from exc
    return tasks
