"""Rendering the runtime's state for a person.

The questions these views exist to answer, in the order a researcher actually
asks them:

```text
What is running?            runtime status
Why is it running?          runtime run show  (the objective and the plan notes)
What question does it address?
What is waiting, and on what?
What failed, and how?
What external jobs exist?
How much has this cost?     runtime costs
Which decisions are pending? runtime approvals
What scientific state changed?
```

Two rules throughout. Every view is plain text with a deterministic ``--json``
counterpart, because a researcher pipes things. And every string that came from
a model or from a fetched document goes through
:mod:`research_os.textsafe` on its way to a terminal -- an escape sequence in a
paper's abstract must not repaint the researcher's screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from research_os.runtime.checkpoints import checkpoint_sizes
from research_os.runtime.db import Database
from research_os.runtime.models import (
    Approval,
    BudgetRecord,
    Event,
    ExternalJob,
    ModelCall,
    ResearchRun,
    RunStatus,
    WorkItem,
    WorkStatus,
)
from research_os.runtime.notify import read_inbox
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from research_os.textsafe import terminal_safe


def _safe(value: object, *, limit: int = 300) -> str:
    """Make one untrusted string safe to print, and short enough to read.

    ``keep=frozenset()`` rather than the display default, because these strings
    go into aligned table cells: a newline or a tab from a model's output would
    break the layout as effectively as an escape sequence would repaint the
    screen.
    """

    text = terminal_safe(str(value if value is not None else ""), keep=frozenset())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _age(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172_800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "  (none)\n"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    ]
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(
            "  "
            + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Everything ``runtime status`` shows, gathered once."""

    active_runs: tuple[ResearchRun, ...]
    waiting_human: tuple[ResearchRun, ...]
    waiting_external: tuple[ResearchRun, ...]
    work_counts: dict[WorkStatus, int]
    failed_work: tuple[WorkItem, ...]
    pending_approvals: tuple[Approval, ...]
    active_jobs: tuple[ExternalJob, ...]
    budgets: tuple[BudgetRecord, ...]
    checkpoint_bytes: dict[str, int]
    notifications: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "active_runs": [run.run_id for run in self.active_runs],
            "waiting_human": [run.run_id for run in self.waiting_human],
            "waiting_external": [run.run_id for run in self.waiting_external],
            "work_counts": {
                str(status): count for status, count in self.work_counts.items()
            },
            "failed_work": [
                {
                    "work_id": item.work_id,
                    "kind": item.kind,
                    "failure_class": item.failure_class,
                    "last_error": _safe(item.last_error, limit=400),
                }
                for item in self.failed_work
            ],
            "pending_approvals": [
                {
                    "approval_id": approval.approval_id,
                    "run_id": approval.run_id,
                    "kind": approval.kind,
                    "question": _safe(approval.question),
                }
                for approval in self.pending_approvals
            ],
            "active_jobs": [
                {
                    "job_id": job.job_id,
                    "executor": job.executor,
                    "scheduler_job_id": job.scheduler_job_id,
                    "status": str(job.status),
                }
                for job in self.active_jobs
            ],
            "budgets": [
                {
                    "scope": str(budget.scope),
                    "scope_id": budget.scope_id,
                    "dimension": budget.dimension,
                    "limit": str(budget.limit_value),
                    "reserved": str(budget.reserved),
                    "spent": str(budget.spent),
                    "available": str(budget.available),
                }
                for budget in self.budgets
            ],
            "checkpoint_bytes": dict(self.checkpoint_bytes),
            "notifications": [
                {
                    key: _safe(value) if isinstance(value, str) else value
                    for key, value in record.items()
                }
                for record in self.notifications
            ],
        }


def collect_status(db: Database, *, project_id: str | None = None) -> StatusReport:
    store = RuntimeStore(db)
    queue = WorkQueue(db)

    active = store.list_runs(project_id=project_id, active_only=True, limit=100)
    with db.tx() as conn:
        failed = conn.execute(
            """
            select work_id, run_id, project_id, kind, payload, status, priority,
                   scheduled_at, attempts, max_attempts, lease_owner, lease_expires_at,
                   dedup_key, failure_class, last_error, result, created_at, updated_at
            from work_items where status = 'FAILED'
            order by updated_at desc limit 20
            """
        ).fetchall()
        system_budgets = conn.execute(
            "select budget_id, scope, scope_id, dimension, limit_value, reserved, spent, "
            "created_at, updated_at from budgets order by scope, scope_id, dimension limit 100"
        ).fetchall()
    return StatusReport(
        active_runs=tuple(run for run in active if run.status is RunStatus.RUNNING),
        waiting_human=tuple(
            run for run in active if run.status is RunStatus.WAITING_HUMAN
        ),
        waiting_external=tuple(
            run for run in active if run.status is RunStatus.WAITING_EXTERNAL
        ),
        work_counts=queue.counts_by_status(),
        failed_work=tuple(WorkItem.model_validate(row) for row in failed),
        pending_approvals=store.list_approvals(pending_only=True, limit=20),
        active_jobs=store.active_external_jobs(limit=20),
        budgets=tuple(BudgetRecord.model_validate(row) for row in system_budgets),
        checkpoint_bytes=checkpoint_sizes(db),
        notifications=read_inbox(limit=5),
    )


def render_status(report: StatusReport) -> str:
    parts: list[str] = ["Research OS runtime\n"]

    parts.append("\nRUNNING\n")
    parts.append(
        _table(
            ("run", "cycle", "age", "objective"),
            [
                (
                    run.run_id,
                    str(run.cycle_index),
                    _age(run.started_at),
                    _safe(run.objective, limit=60),
                )
                for run in report.active_runs
            ],
        )
    )

    if report.waiting_human:
        parts.append("\nWAITING FOR YOU\n")
        parts.append(
            _table(
                ("run", "age", "detail"),
                [
                    (run.run_id, _age(run.updated_at), _safe(run.detail, limit=70))
                    for run in report.waiting_human
                ],
            )
        )

    if report.waiting_external:
        parts.append("\nWAITING FOR AN EXTERNAL DEPENDENCY\n")
        parts.append(
            _table(
                ("run", "age", "detail"),
                [
                    (run.run_id, _age(run.updated_at), _safe(run.detail, limit=70))
                    for run in report.waiting_external
                ],
            )
        )

    parts.append("\nWORK\n")
    counts = report.work_counts
    if counts:
        parts.append(
            "  "
            + "  ".join(f"{status}={count}" for status, count in sorted(counts.items()))
            + "\n"
        )
    else:
        parts.append("  (empty queue)\n")

    if report.failed_work:
        parts.append("\nFAILED WORK\n")
        parts.append(
            _table(
                ("work", "kind", "class", "error"),
                [
                    (
                        item.work_id,
                        item.kind,
                        item.failure_class or "-",
                        _safe(item.last_error, limit=60),
                    )
                    for item in report.failed_work
                ],
            )
        )

    if report.pending_approvals:
        parts.append("\nDECISIONS PENDING\n")
        parts.append(
            _table(
                ("approval", "run", "kind", "question"),
                [
                    (
                        approval.approval_id,
                        approval.run_id,
                        approval.kind,
                        _safe(approval.question, limit=50),
                    )
                    for approval in report.pending_approvals
                ],
            )
        )
        parts.append("  Answer with: researchctl runtime approve <approval>\n")

    if report.active_jobs:
        parts.append("\nEXTERNAL JOBS\n")
        parts.append(
            _table(
                ("job", "executor", "scheduler", "status", "polled"),
                [
                    (
                        job.job_id,
                        job.executor,
                        job.scheduler_job_id or "-",
                        str(job.status),
                        _age(job.last_polled_at),
                    )
                    for job in report.active_jobs
                ],
            )
        )

    spent = [budget for budget in report.budgets if budget.spent or budget.reserved]
    if spent:
        parts.append("\nBUDGETS\n")
        parts.append(
            _table(
                ("scope", "dimension", "spent", "reserved", "available"),
                [
                    (
                        f"{budget.scope}:{budget.scope_id}",
                        budget.dimension,
                        str(budget.spent),
                        str(budget.reserved),
                        str(budget.available),
                    )
                    for budget in spent
                ],
            )
        )

    total_checkpoints = sum(report.checkpoint_bytes.values())
    parts.append(f"\nCHECKPOINTS  {total_checkpoints / 1_048_576:.1f} MiB\n")
    return "".join(parts)


def render_runs(runs: tuple[ResearchRun, ...]) -> str:
    return "RUNS\n" + _table(
        ("run", "status", "terminal", "cycle", "parent", "age", "objective"),
        [
            (
                run.run_id,
                str(run.status),
                str(run.terminal_state) if run.terminal_state else "-",
                str(run.cycle_index),
                run.parent_run_id or "-",
                _age(run.created_at),
                _safe(run.objective, limit=50),
            )
            for run in runs
        ],
    )


def render_run_detail(
    run: ResearchRun,
    *,
    work: tuple[WorkItem, ...],
    events: tuple[Event, ...],
    approvals: tuple[Approval, ...],
    jobs: tuple[ExternalJob, ...],
    calls: tuple[ModelCall, ...],
    budgets: tuple[BudgetRecord, ...],
) -> str:
    parts = [
        f"RUN {run.run_id}\n",
        f"  project     {run.project_id}\n",
        f"  objective   {_safe(run.objective, limit=200)}\n",
        f"  status      {run.status}"
        + (f" -> {run.terminal_state}" if run.terminal_state else "")
        + "\n",
        f"  autonomy    {run.autonomy}\n",
        f"  cycle       {run.cycle_index}"
        + (f" (after {run.parent_run_id})" if run.parent_run_id else " (first)")
        + "\n",
        f"  thread      {run.thread_id or '-'}\n",
        f"  created     {run.created_at.isoformat()}\n",
    ]
    if run.finished_at:
        parts.append(f"  finished    {run.finished_at.isoformat()}\n")
    if run.detail:
        parts.append(f"  detail      {_safe(run.detail, limit=200)}\n")

    parts.append("\nWORK\n")
    parts.append(
        _table(
            ("work", "kind", "status", "attempts", "class"),
            [
                (
                    item.work_id,
                    item.kind,
                    str(item.status),
                    f"{item.attempts}/{item.max_attempts}",
                    item.failure_class or "-",
                )
                for item in work
            ],
        )
    )

    if approvals:
        parts.append("\nDECISIONS\n")
        parts.append(
            _table(
                ("approval", "kind", "status", "decided by"),
                [
                    (
                        approval.approval_id,
                        approval.kind,
                        str(approval.status),
                        approval.decided_by or "-",
                    )
                    for approval in approvals
                ],
            )
        )

    if jobs:
        parts.append("\nEXTERNAL JOBS\n")
        parts.append(
            _table(
                ("job", "executor", "scheduler", "status", "exit", "class"),
                [
                    (
                        job.job_id,
                        job.executor,
                        job.scheduler_job_id or "-",
                        str(job.status),
                        "-" if job.exit_code is None else str(job.exit_code),
                        job.failure_class or "-",
                    )
                    for job in jobs
                ],
            )
        )

    if calls:
        parts.append("\nMODEL CALLS\n")
        parts.append(
            _table(
                (
                    "role",
                    "provider",
                    "model",
                    "prompt",
                    "independence",
                    "cost",
                    "status",
                ),
                [
                    (
                        call.role,
                        call.provider,
                        call.model or "-",
                        call.prompt_version or "-",
                        call.independence_group or "-",
                        "-" if call.cost_usd is None else f"{call.cost_usd:.4f}",
                        str(call.status),
                    )
                    for call in calls
                ],
            )
        )

    if budgets:
        parts.append("\nBUDGET\n")
        parts.append(
            _table(
                ("dimension", "limit", "spent", "reserved", "available"),
                [
                    (
                        budget.dimension,
                        str(budget.limit_value),
                        str(budget.spent),
                        str(budget.reserved),
                        str(budget.available),
                    )
                    for budget in budgets
                ],
            )
        )

    parts.append("\nEVENTS (most recent first)\n")
    parts.append(
        _table(
            ("when", "event", "detail"),
            [
                (
                    _age(event.created_at),
                    event.kind,
                    _safe(
                        ", ".join(f"{k}={v}" for k, v in sorted(event.payload.items())),
                        limit=70,
                    ),
                )
                for event in events
            ],
        )
    )
    return "".join(parts)


def render_approval(approval: Approval) -> str:
    """The decision packet, rendered for a person to act on.

    The whole packet, not a summary: the researcher is being asked to exercise
    scientific authority, and the point of preparing this autonomously was so
    they would not have to go and look things up.
    """

    packet = approval.packet or {}
    parts = [
        f"DECISION REQUIRED  {approval.approval_id}\n",
        f"  run        {approval.run_id}\n",
        f"  project    {approval.project_id}\n",
        f"  requested  {approval.requested_at.isoformat()} ({_age(approval.requested_at)} ago)\n",
        f"\n  {_safe(approval.question, limit=400)}\n",
    ]
    if packet.get("why_it_matters"):
        parts.append(
            f"\nWHY IT MATTERS\n  {_safe(packet['why_it_matters'], limit=800)}\n"
        )
    evidence = packet.get("current_evidence") or {}
    if evidence:
        parts.append("\nCURRENT EVIDENCE\n")
        for key, value in sorted(evidence.items()):
            listed = (
                ", ".join(str(item) for item in value)
                if isinstance(value, list)
                else str(value)
            )
            parts.append(f"  {key:26} {_safe(listed or '(none)', limit=200)}\n")
    alternatives = packet.get("alternatives") or []
    if alternatives:
        parts.append("\nWHAT HAPPENS AFTER EACH CHOICE\n")
        for option in alternatives:
            if not isinstance(option, dict):
                continue
            parts.append(f"  {_safe(option.get('choice', '?'), limit=20)}\n")
            parts.append(f"    {_safe(option.get('consequence', ''), limit=400)}\n")
    if packet.get("uncertainty"):
        parts.append(f"\nUNCERTAINTY\n  {_safe(packet['uncertainty'], limit=300)}\n")
    if packet.get("recommendation"):
        parts.append(
            f"\nRECOMMENDATION\n  {_safe(packet['recommendation'], limit=400)}\n"
        )
    if packet.get("recommendation_rationale"):
        parts.append(f"  {_safe(packet['recommendation_rationale'], limit=500)}\n")
    parts.append(
        f"\n  researchctl runtime approve {approval.approval_id}\n"
        f"  researchctl runtime decline {approval.approval_id}\n"
    )
    return "".join(parts)


def render_costs(
    calls: tuple[ModelCall, ...], budgets: tuple[BudgetRecord, ...]
) -> str:
    by_role: dict[str, tuple[int, Decimal]] = {}
    for call in calls:
        count, cost = by_role.get(call.role, (0, Decimal(0)))
        by_role[call.role] = (count + 1, cost + (call.cost_usd or Decimal(0)))
    parts = ["COSTS\n"]
    parts.append(
        _table(
            ("role", "calls", "cost usd"),
            [
                (role, str(count), f"{cost:.4f}")
                for role, (count, cost) in sorted(by_role.items())
            ],
        )
    )
    total_calls = sum(count for count, _cost in by_role.values())
    total_cost = sum((cost for _count, cost in by_role.values()), Decimal(0))
    parts.append(f"  total {total_calls} calls, {total_cost:.4f} usd\n")
    unreported = sum(1 for call in calls if call.cost_usd is None)
    if unreported:
        parts.append(
            f"  {unreported} call(s) reported no cost; the figure above is a floor, "
            f"not a total\n"
        )
    if budgets:
        parts.append("\nBUDGETS\n")
        parts.append(
            _table(
                ("scope", "dimension", "limit", "spent", "available"),
                [
                    (
                        f"{budget.scope}:{budget.scope_id}",
                        budget.dimension,
                        str(budget.limit_value),
                        str(budget.spent),
                        str(budget.available),
                    )
                    for budget in budgets
                ],
            )
        )
    return "".join(parts)


def render_events(events: tuple[Event, ...]) -> str:
    return "EVENTS\n" + _table(
        ("when", "event", "run", "detail"),
        [
            (
                _age(event.created_at),
                event.kind,
                event.run_id or "-",
                _safe(
                    ", ".join(f"{k}={v}" for k, v in sorted(event.payload.items())),
                    limit=60,
                ),
            )
            for event in events
        ],
    )


def render_jobs(jobs: tuple[ExternalJob, ...]) -> str:
    return "EXTERNAL JOBS\n" + _table(
        ("job", "run", "executor", "scheduler", "status", "class", "polled"),
        [
            (
                job.job_id,
                job.run_id or "-",
                job.executor,
                job.scheduler_job_id or "-",
                str(job.status),
                job.failure_class or "-",
                _age(job.last_polled_at),
            )
            for job in jobs
        ],
    )
