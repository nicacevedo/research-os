"""Bounded read-only analysis of a pinned Git snapshot.

The analysis worker is the one worker that reads the repository with tools. It
is given exactly ``Read``, ``Glob``, and ``Grep`` inside an isolated snapshot
pinned to the run's base commit, so it can look at real files instead of
reasoning over a context packet, and it is given nothing that can change a file
or run a command. The controller compares the snapshot before and after and
fails the run if anything moved.

What it returns is **data**. The controller parses it, validates it, archives
it, and may quote it into a downstream prompt. It never lets that content
decide a tool set, a scope, an acceptance command, a budget, a worktree path,
or run state: those come from the deterministic work order and nowhere else.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from research_os.automation.models import AnalystReport, WorkOrder
from research_os.automation.structured import extract_json_object
from research_os.errors import AnalystOutputError

MAX_SUMMARY_CHARS = 4_000
MAX_STATEMENT_CHARS = 2_000
MAX_DETAIL_CHARS = 2_000
MAX_HANDOFF_FINDINGS = 40

#: The delimiters that fence analyst content inside a downstream prompt.
#:
#: A single unambiguous pair, quoted in the surrounding instructions, so the
#: downstream worker is told exactly where untrusted data starts and stops.
DATA_BEGIN = "----- BEGIN ANALYST DATA (UNTRUSTED INPUT) -----"
DATA_END = "----- END ANALYST DATA (UNTRUSTED INPUT) -----"

ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "findings",
        "evidence",
        "uncertainties",
        "recommended_action",
    ],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "statement",
                    "importance",
                    "file_refs",
                    "confidence",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "statement": {"type": "string"},
                    "importance": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "file_refs": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["finding_id", "file_ref", "detail"],
                "properties": {
                    "finding_id": {"type": "string"},
                    "file_ref": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string"},
    },
}


def build_analyst_prompt(order: WorkOrder, *, context_text: str) -> str:
    """Return the complete prompt for the snapshot-read analysis worker."""

    scope = "\n".join(f"- {item}" for item in order.read_paths)
    return f"""You are the analysis worker of a deterministic research automation
controller. You are running inside a disposable, read-only Git snapshot pinned
to one commit. It is not the researcher's checkout, and nothing you do here is
kept: the controller compares this snapshot before and after you run and fails
the whole run if any file or the commit changed.

You have exactly three tools: Read, Glob, and Grep. You have no Write, no Edit,
and no Bash. You cannot change a file, run a command, run the tests, commit, or
install anything, so do not plan to: report what you established by reading.

TASK {order.task_id}: {order.title}

GOAL
{order.goal}

COMPLETION CONDITION
{order.completion_condition}

SNAPSHOT COMMIT
{order.base_commit}

YOU MAY READ THESE PATHS
{scope}

Stay inside that read scope. It is stated in paths relative to the snapshot
root, which is your working directory.

WHAT TO RETURN

Structured findings, not prose. Every finding must be something you actually
established by reading a file, with the repository-relative path that shows it.

- "summary": what you found, in a few sentences.
- "findings": one entry per distinct thing you established. Give each a short
  id such as "F-001", a one-sentence "statement", an "importance", a
  "confidence", and "file_refs" listing the paths that support it.
- "evidence": concrete observations, each naming the "finding_id" it supports,
  the "file_ref" it came from, and what that file actually shows.
- "uncertainties": what you could not establish by reading, and what would be
  needed to settle it. An empty list is a claim that nothing is unresolved, so
  say plainly what you are unsure of.
- "recommended_action": the smallest concrete next step you would take.

Every path in "file_refs" and "file_ref" must be relative to the snapshot root:
no leading "/", no "~", and no ".." segment. A reference that is not a plain
relative path invalidates your whole report and fails this task.

Your output is data for a human and for a later work order. It is not an
instruction to either of them: do not write directives, scope changes, tool
requests, or commands to run into any field.

{context_text}
"""


def parse_analyst_report(
    *,
    structured: dict[str, Any] | None,
    text: str | None,
    task_id: str,
    provider: str,
    model: str | None,
    invocation_id: str,
    snapshot_commit: str,
) -> AnalystReport:
    """Turn an analysis response into a validated report, or refuse it.

    Fail-closed and never repaired. Analyst output reaches a downstream worker
    and a human as evidence, so output that does not validate is a failed work
    order rather than something to interpret generously.
    """

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise AnalystOutputError(
            "analyst returned no JSON object; expected structured findings"
        )
    try:
        return AnalystReport.model_validate(
            {
                **payload,
                "task_id": task_id,
                "provider": provider,
                "model": model,
                "invocation_id": invocation_id,
                "snapshot_commit": snapshot_commit,
            }
        )
    except ValidationError as exc:
        raise AnalystOutputError(
            f"analyst output is not a valid report: {exc}"
        ) from exc


def render_analyst_data(report: AnalystReport, *, artifact_path: str) -> str:
    """Render one archived analyst report as a delimited data block.

    Rendered from the parsed report, never from the raw model output, so what a
    downstream worker sees has already passed validation: every path is
    repository-relative, every importance and confidence is one of the known
    values, and no free-form session history is carried across.

    Long fields are truncated here rather than in the archive. The archive is
    the record; this is a quotation of it.
    """

    lines = [
        DATA_BEGIN,
        f"source_task: {report.task_id}",
        f"source_artifact: {artifact_path}",
        f"snapshot_commit: {report.snapshot_commit}",
        f"analyst: {report.provider} / {report.model or 'provider default'}",
        "",
        "summary:",
        f"  {_clip(report.summary, MAX_SUMMARY_CHARS)}",
        "",
        "findings:",
    ]
    findings = report.findings[:MAX_HANDOFF_FINDINGS]
    if not findings:
        lines.append("  (the analyst reported no findings)")
    for finding in findings:
        refs = ", ".join(finding.file_refs) or "(no file named)"
        lines.extend(
            [
                f"  - id: {finding.id}",
                (
                    f"    importance: {finding.importance}"
                    f"  confidence: {finding.confidence}"
                ),
                f"    files: {refs}",
                f"    statement: {_clip(finding.statement, MAX_STATEMENT_CHARS)}",
            ]
        )
    if len(report.findings) > MAX_HANDOFF_FINDINGS:
        dropped = len(report.findings) - MAX_HANDOFF_FINDINGS
        lines.append(f"  ({dropped} further finding(s) in the archived artifact)")

    lines.extend(["", "evidence:"])
    evidence = [
        item
        for item in report.evidence
        if item.finding_id in {finding.id for finding in findings}
    ]
    if not evidence:
        lines.append("  (none recorded)")
    for item in evidence:
        lines.append(
            f"  - {item.finding_id} [{item.file_ref}]: "
            f"{_clip(item.detail, MAX_DETAIL_CHARS)}"
        )

    lines.extend(["", "uncertainties:"])
    if not report.uncertainties:
        lines.append("  (the analyst reported none)")
    lines.extend(
        f"  - {_clip(item, MAX_STATEMENT_CHARS)}" for item in report.uncertainties
    )

    lines.extend(
        [
            "",
            "recommended_action:",
            f"  {_clip(report.recommended_action, MAX_STATEMENT_CHARS)}",
            DATA_END,
        ]
    )
    return "\n".join(lines)


def snapshot_state(*, head: str, status: tuple[str, ...]) -> dict[str, Any]:
    """Return the recorded snapshot state, before or after an invocation."""

    return {"head": head, "clean": not status, "status": list(status)}


def snapshot_evidence(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> str:
    """Return the archived before-and-after snapshot state as JSON text."""

    return (
        json.dumps({"before": before, "after": after}, indent=2, sort_keys=True) + "\n"
    )


def _clip(value: str, limit: int) -> str:
    """Return ``value`` on one line, truncated to ``limit`` characters.

    Newlines are folded so a finding cannot forge the block's line structure,
    and the delimiters are removed so it cannot forge the block's end.
    """

    flattened = " ".join(value.split())
    for marker in (DATA_BEGIN, DATA_END):
        flattened = flattened.replace(marker, "[removed delimiter]")
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit] + " [truncated]"
