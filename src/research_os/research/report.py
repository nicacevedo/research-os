"""Rendering a research run for the person who has to decide what to do about it.

A research run finishes by handing a researcher a pile of pointers: an automation
run, a proposal, an experiment run, a draft. The report's whole job is to make
that pile legible and to be honest about what it is.

Three things every view insists on saying.

**What it spent.** Model calls, write tasks, experiments, and cluster
submissions, each against its ceiling. A run that cost something should say so
without being asked.

**What it did not do.** A skipped experiment prints the command it would have
run. A pending checkpoint prints the question. Silence about a step that did not
happen is how a reader concludes it did.

**That none of it is science.** Everything here is a candidate: proposals are
suggestions, evidence packets are candidates, drafts are prose. Accepting any of
it is a human act, and the report says so in those words.

Everything a worker wrote passes through the display sanitizer on the way out.
"""

from __future__ import annotations

import json
from typing import Any

from research_os.research.controller import ready_blockers
from research_os.research.models import (
    HumanCheckpoint,
    ResearchRun,
    ResearchState,
    ResearchTask,
    TaskKind,
    TaskStatus,
)
from research_os.textsafe import terminal_safe

RULE = "=" * 72
THIN = "-" * 72

#: What a reader should do next, per terminal state.
NEXT_STEPS: dict[ResearchState, str] = {
    ResearchState.PLAN_READY: (
        "Review the plan above, then run it with 'researchctl research run'."
    ),
    ResearchState.WAITING_FOR_HUMAN: (
        "Answer the open question with 'researchctl research answer', then run "
        "'researchctl research run' again to continue."
    ),
    ResearchState.EXECUTING: (
        "This run says it is executing, which after a crash it is not. Recover "
        "it with 'researchctl research resume RUN_ID'."
    ),
    ResearchState.INTERRUPTED: (
        "This run was interrupted and has been recovered. Continue it with "
        "'researchctl research run RUN_ID'; the task that was in flight is "
        "marked above."
    ),
    ResearchState.READY_FOR_HUMAN: (
        "Read each artifact below in the store that owns it. Nothing here has "
        "entered the capsule: proposals are suggestions, evidence packets are "
        "candidates, and drafts are prose. Promoting any of it is your decision."
    ),
    ResearchState.FAILED: (
        "Nothing was accepted. The failure reason is above; the run directory "
        "keeps every prompt, every model output, and the full event ledger."
    ),
    ResearchState.CANCELLED: "This run was stopped before it finished.",
}

#: Where to look for whatever a task produced.
ARTIFACT_HOMES: dict[TaskKind, str] = {
    TaskKind.ANALYSIS: "researchctl auto report",
    TaskKind.CODE: "researchctl auto report",
    TaskKind.PROPOSAL: "researchctl propose show",
    TaskKind.EXPERIMENT: "researchctl experiment show",
    TaskKind.PAPER: "researchctl paper show",
}


def render_run(run: ResearchRun, *, events: list[dict[str, Any]] | None = None) -> str:
    """Render one research run in full."""

    lines = [
        "",
        RULE,
        f"Research run {run.run_id}   [{run.state}]",
        RULE,
        "",
        f"goal            {terminal_safe(run.goal)}",
        f"project         {run.project_path}",
        f"project_id      {run.project_id or 'unregistered'}",
        f"base commit     {run.base_commit or '-'}",
        f"created         {run.created_at}",
        f"updated         {run.updated_at}",
    ]
    if run.finished_at:
        lines.append(f"finished        {run.finished_at}")
    if run.independence:
        note = (
            f"  ({terminal_safe(run.independence_note)})"
            if run.independence_note
            else ""
        )
        lines.append(f"independence    {run.independence}{note}")
    lines.append(
        "experiments     "
        + (
            "authorised to execute"
            if run.execute_experiments
            else "not authorised to execute; they resolve and stop"
        )
    )
    if run.plan_summary:
        lines.extend(["", "plan summary", f"  {terminal_safe(run.plan_summary)}"])
    if run.failure_reason:
        lines.extend(["", "failure reason", f"  {terminal_safe(run.failure_reason)}"])

    lines.extend(["", THIN, "budget", THIN, *_budget_lines(run)])
    lines.extend(["", THIN, "plan", THIN])
    if not run.tasks:
        lines.append("  (this run has no tasks)")
    for task in run.tasks:
        lines.extend(_task_lines(task))

    if run.checkpoints:
        lines.extend(["", THIN, "human checkpoints", THIN])
        for checkpoint in run.checkpoints:
            lines.extend(_checkpoint_lines(checkpoint))

    blockers = ready_blockers(run)
    if blockers and run.state is not ResearchState.READY_FOR_HUMAN:
        lines.extend(["", THIN, "not ready for a human because", THIN])
        lines.extend(f"  - {terminal_safe(item)}" for item in blockers)

    if events:
        lines.extend(["", THIN, "event ledger", THIN])
        lines.extend(_event_lines(events))

    step = NEXT_STEPS.get(run.state)
    if step:
        lines.extend(["", THIN, "what happens next", THIN, *_wrapped(step)])
    lines.append("")
    return "\n".join(lines)


def render_status(run: ResearchRun) -> str:
    """Render the short view: where the run is and what it is waiting on."""

    counts = run.counts()
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines = [
        f"{run.run_id}  {run.state}",
        f"  goal      {terminal_safe(run.goal)}",
        f"  tasks     {summary or 'none'}",
        f"  spent     {_spend(run)}",
    ]
    pending = run.pending_checkpoints
    if pending:
        question = terminal_safe(pending[0].question)
        lines.append(f"  waiting   {pending[0].task_id}: {question}")
    unfinished = run.unfinished
    if unfinished and not pending:
        lines.append(
            "  next      "
            + f"{unfinished[0].task_id} [{unfinished[0].kind}] "
            + terminal_safe(unfinished[0].title)
        )
    return "\n".join(lines)


def render_run_list(runs: list[ResearchRun]) -> str:
    """Render every known research run, newest last."""

    if not runs:
        return "No research runs yet. Start one with 'researchctl research start'.\n"
    lines = [f"{len(runs)} research run(s):", ""]
    for run in runs:
        lines.append(f"  {run.run_id}  {run.state:<18} {terminal_safe(run.goal)[:44]}")
    lines.append("")
    return "\n".join(lines)


def _budget_lines(run: ResearchRun) -> list[str]:
    rows = [
        ("model calls", run.model_calls_used, run.budget.max_model_calls),
        ("write tasks", run.write_tasks_used, run.budget.max_write_tasks),
        ("experiments", run.experiments_used, run.budget.max_experiments),
        (
            "cluster submissions",
            run.cluster_submissions_used,
            run.budget.max_cluster_submissions,
        ),
    ]
    lines = [f"  {label:<22}{used} / {ceiling}" for label, used, ceiling in rows]
    lines.append(
        f"  {'repair attempts':<22}at most {run.budget.max_repair_attempts} per item"
    )
    return lines


def _spend(run: ResearchRun) -> str:
    parts = [f"{run.model_calls_used}/{run.budget.max_model_calls} model calls"]
    if run.write_tasks_used:
        parts.append(f"{run.write_tasks_used} write task(s)")
    if run.experiments_used:
        parts.append(f"{run.experiments_used} experiment(s)")
    if run.cluster_submissions_used:
        parts.append(f"{run.cluster_submissions_used} cluster submission(s)")
    return ", ".join(parts)


def _task_lines(task: ResearchTask) -> list[str]:
    depends = f"  after {', '.join(task.depends_on)}" if task.depends_on else ""
    lines = [
        "",
        f"{task.task_id}  [{task.kind}]  {task.status}{depends}",
        f"  title           {terminal_safe(task.title)}",
        f"  goal            {terminal_safe(task.goal)}",
    ]
    if task.query:
        lines.append(f"  query           {terminal_safe(task.query)}")
    if task.read_paths:
        lines.append(f"  reads           {', '.join(task.read_paths)}")
    if task.allowed_paths:
        lines.append(f"  may write       {', '.join(task.allowed_paths)}")
    for command in task.acceptance_commands:
        lines.append(f"  check           {' '.join(terminal_safe(c) for c in command)}")
    if task.experiment_task:
        lines.append(f"  experiment      {terminal_safe(task.experiment_task)}")
        for name, value in sorted(task.experiment_parameters.items()):
            lines.append(f"    {name} = {terminal_safe(value)}")
    if task.section:
        lines.append(f"  section         {task.section}")
    if task.question:
        lines.append(f"  asks            {terminal_safe(task.question)}")
    if task.detail:
        lines.append(f"  outcome         {terminal_safe(task.detail)}")
    if task.failure_reason:
        lines.append(f"  failed          {terminal_safe(task.failure_reason)}")
    if task.artifact_id:
        home = ARTIFACT_HOMES.get(task.kind)
        pointer = f"  produced        {task.artifact_id}"
        if home:
            pointer += f"   ({home} {task.artifact_id})"
        lines.append(pointer)
    if task.model_calls:
        lines.append(f"  model calls     {task.model_calls}")
    if task.status is TaskStatus.SKIPPED and not task.detail:
        lines.append("  outcome         skipped")
    return lines


def _checkpoint_lines(checkpoint: HumanCheckpoint) -> list[str]:
    lines = [
        "",
        f"{checkpoint.task_id}  {checkpoint.decision}",
        f"  question        {terminal_safe(checkpoint.question)}",
        f"  reached         {checkpoint.reached_at}",
    ]
    if checkpoint.answer is not None:
        lines.append(f"  answer          {terminal_safe(checkpoint.answer)}")
        lines.append(f"  answered        {checkpoint.answered_at}")
    else:
        lines.append(
            "  answer          (none yet - 'researchctl research answer' continues "
            "this run)"
        )
    return lines


def _event_lines(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for record in events:
        seq = record.get("seq", "?")
        name = record.get("event", "?")
        rest = {
            key: value
            for key, value in sorted(record.items())
            if key not in {"seq", "ts", "run_id", "event"}
        }
        detail = json.dumps(rest, sort_keys=True, ensure_ascii=True) if rest else ""
        lines.append(f"  {seq:>3}  {record.get('ts', '')}  {name}  {detail}")
    return lines


def _wrapped(text: str, *, width: int = 70) -> list[str]:
    import textwrap

    return [f"  {line}" for line in textwrap.wrap(terminal_safe(text), width=width)]
