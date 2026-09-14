"""Rendering one technical assessment for a person to read.

The same discipline the proposal report follows: every string here was written
by a model about somebody else's repository, so it goes through the display
boundary on the way to a terminal, and nothing in the rendering implies the
assessment is more settled than it is.
"""

from __future__ import annotations

from research_os.assessment.models import TechnicalAssessment
from research_os.textsafe import terminal_safe as safe_display


def render_assessment(assessment: TechnicalAssessment) -> str:
    """Return the full human-readable rendering of one assessment."""

    lines = [
        f"assessment      {assessment.assessment_id}",
        f"mode            {assessment.mode}",
        f"project         {safe_display(assessment.project_id or 'unregistered')}",
        f"path            {safe_display(assessment.project_path)}",
        f"base commit     {assessment.base_commit or 'none'}",
        f"provider        {safe_display(assessment.provider)}"
        + (f" ({safe_display(assessment.model)})" if assessment.model else ""),
        "",
        "This is a technical assessment of a repository, not a scientific",
        "proposal. It holds no capsule objects, asserts nothing this project",
        "accepts, and nothing in it is promotable into scientific state.",
        "",
        "GOAL",
        f"  {safe_display(assessment.goal)}",
        "",
        "SUMMARY",
        f"  {safe_display(assessment.summary)}",
    ]
    if assessment.observations:
        lines.extend(["", "OBSERVATIONS"])
        for item in assessment.observations:
            lines.append(
                f"  {item.observation_id}  [{item.importance}/{item.confidence}]  "
                f"{safe_display(item.statement)}"
            )
            lines.append(f"      why: {safe_display(item.rationale)}")
            for reference in item.file_refs:
                suffix = (
                    f"::{safe_display(reference.symbol)}" if reference.symbol else ""
                )
                note = f"  -- {safe_display(reference.note)}" if reference.note else ""
                lines.append(
                    f"      file: {safe_display(reference.path)}{suffix}{note}"
                )
            for key in item.literature_keys:
                lines.append(f"      work: {safe_display(key)}")
            for check in item.check_ids:
                lines.append(f"      check: {safe_display(check)}")
    if assessment.open_question is not None:
        question = assessment.open_question
        lines.extend(
            [
                "",
                "HIGHEST-VALUE OPEN QUESTION",
                f"  {safe_display(question.question)}",
                f"      why it matters: {safe_display(question.why_it_matters)}",
                (
                    "      what would answer it: "
                    + safe_display(question.what_would_answer_it)
                ),
            ]
        )
        if question.blocked_by_evidence:
            lines.append(
                "      blocked: answering this needs evidence this run could not obtain"
            )
    if assessment.uncertainties:
        lines.extend(["", "UNCERTAINTIES"])
        for item in assessment.uncertainties:
            lines.append(f"  - {safe_display(item.statement)}")
            lines.append(
                "      would be settled by: " + safe_display(item.what_would_settle_it)
            )
    if assessment.next_actions:
        lines.extend(["", "RECOMMENDED NEXT ACTIONS"])
        for item in assessment.next_actions:
            mark = " [needs you]" if item.requires_human else ""
            lines.append(f"  - [{item.kind}]{mark} {safe_display(item.action)}")
            lines.append(f"      why: {safe_display(item.rationale)}")
    if assessment.capsule_suggestion:
        lines.extend(
            [
                "",
                "ABOUT STARTING A RESEARCH CAPSULE HERE",
                f"  {safe_display(assessment.capsule_suggestion)}",
                "  (a suggestion only; Research OS never creates a capsule itself)",
            ]
        )
    return "\n".join(lines) + "\n"
