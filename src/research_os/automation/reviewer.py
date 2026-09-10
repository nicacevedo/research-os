"""Independent read-only review of a finished work order.

The reviewer has no tools. It sees a frozen packet - the work order, the base
commit, the final diff, and the deterministic check results the controller
observed - and returns a structured verdict. It cannot edit code, cannot merge,
cannot re-run anything, and cannot change run state. Its verdict gates the run;
its findings are advisory text for the human.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from research_os.automation.models import (
    CommandResult,
    Independence,
    ReviewFinding,
    ReviewOutcome,
    ReviewVerdict,
    WorkOrder,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import ProviderInvocationError

MAX_DIFF_CHARS = 60_000
MAX_REPORT_CHARS = 6_000

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "PASS_WITH_REPAIR", "FAIL"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "message", "path"],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "major", "minor", "note"],
                    },
                    "message": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        },
    },
}


def build_reviewer_prompt(
    order: WorkOrder,
    *,
    diff: str,
    check_results: list[CommandResult],
    worker_report: str | None,
    context_text: str,
) -> str:
    """Return the frozen review packet as prompt text."""

    truncated_diff = diff
    if len(truncated_diff) > MAX_DIFF_CHARS:
        truncated_diff = (
            truncated_diff[:MAX_DIFF_CHARS] + "\n[diff truncated for review]\n"
        )
    report = (worker_report or "(the implementation worker produced no report)")[
        :MAX_REPORT_CHARS
    ]
    checks = (
        "\n".join(
            f"- {item.display}\n"
            f"    exit_code: {item.exit_code if item.exit_code is not None else 'none'}"
            f"  timed_out: {item.timed_out}  required: {item.required}"
            for item in check_results
        )
        or "- (no acceptance commands were declared)"
    )
    allowed = "\n".join(f"- {item}" for item in order.allowed_paths)
    changed = "\n".join(f"- {item}" for item in order.changed_paths) or "- (none)"

    return f"""You are the review worker of a deterministic research automation
controller. You have no tools and no repository access. Review only the frozen
packet below. You cannot change code, run commands, or merge anything.

The acceptance commands below were already executed by the controller itself,
not by the implementer, so their exit codes are established facts. Do not
re-litigate whether they passed; judge whether what passed is actually correct
and in scope.

TASK {order.task_id}: {order.title}

GOAL
{order.goal}

COMPLETION CONDITION
{order.completion_condition}

BASE COMMIT
{order.base_commit}

PATHS THIS TASK WAS ALLOWED TO CHANGE
{allowed}

PATHS ACTUALLY CHANGED
{changed}

DETERMINISTIC ACCEPTANCE CHECKS OBSERVED BY THE CONTROLLER
{checks}

IMPLEMENTATION WORKER REPORT (an unverified claim, not evidence)
{report}

FINAL DIFF AGAINST THE BASE COMMIT
```diff
{truncated_diff}
```

Return a verdict:
- PASS: the change meets the goal, is in scope, and you found nothing that must
  be fixed before a human looks at it.
- PASS_WITH_REPAIR: the goal is met but something should be repaired; list each
  item as a finding.
- FAIL: the change does not meet the goal, is out of scope, is unsafe, or is
  wrong in a way the checks did not catch.

Judge correctness, scope, and whether the tests actually prove the goal. Say so
explicitly if the implementation appears to special-case the tests rather than
solve the problem.

{context_text}
"""


def parse_review(
    *,
    structured: dict[str, Any] | None,
    text: str | None,
    task_id: str,
    provider: str,
    model: str | None,
    independence: Independence,
    independence_note: str,
    invocation_id: str,
) -> ReviewOutcome:
    """Turn a reviewer response into a structured outcome."""

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise ProviderInvocationError(
            "reviewer returned no JSON object; expected a structured verdict"
        )
    raw_verdict = payload.get("verdict")
    if raw_verdict not in {item.value for item in ReviewVerdict}:
        raise ProviderInvocationError(
            f"reviewer returned an unknown verdict: {raw_verdict!r}"
        )
    findings: list[ReviewFinding] = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        path = item.get("path")
        findings.append(
            ReviewFinding(
                severity=str(item.get("severity") or "note").strip() or "note",
                message=message,
                path=str(path) if isinstance(path, str) and path.strip() else None,
            )
        )
    summary = str(payload.get("summary") or "").strip()
    try:
        return ReviewOutcome(
            task_id=task_id,
            verdict=ReviewVerdict(raw_verdict),
            summary=summary or "(the reviewer returned no summary)",
            findings=findings,
            provider=provider,
            model=model,
            independence=independence,
            independence_note=independence_note,
            invocation_id=invocation_id,
        )
    except ValidationError as exc:
        raise ProviderInvocationError(f"reviewer output is not usable: {exc}") from exc
