"""An independent read-only reading of a draft, after the bookkeeping is settled.

By the time this reviewer runs, the deterministic checks have already
established what can be established mechanically: every citation resolves, every
object id was supplied, every number is accounted for or flagged. Those results
are handed to the reviewer as facts rather than re-litigated, exactly as the
code reviewer is handed observed exit codes.

What is left is the part only judgement settles, and the prompt asks for that
specifically:

* Is any sentence stronger than the claim it rests on?
* Does an exploratory finding read as confirmatory?
* Is contrary evidence present but defanged -- mentioned in a subordinate clause
  and never engaged with?
* Does the draft assert portability nobody established?

The reviewer has no tools and sees a frozen packet. It cannot edit the draft,
cannot merge anything, and its verdict gates the run rather than the science.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from research_os.automation.promptdata import (
    DIFF_FENCE,
    REVIEW_FENCE,
    TASK_FENCE,
    WORKER_REPORT_FENCE,
    prompt_safe,
    prompt_safe_block,
    render_data_block,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import ProviderInvocationError
from research_os.paper.models import (
    GroundingReport,
    SectionKind,
    SourceManifest,
    SourcePacket,
    WritingFinding,
    WritingReview,
    WritingVerdict,
)
from research_os.paper.packet import render_source_packet

MAX_DIFF_CHARS = 80_000
MAX_LABEL_CHARS = 300
MAX_STATEMENT_CHARS = 3_000

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


def build_writing_review_prompt(
    *,
    section: SectionKind,
    instruction: str,
    packet: SourcePacket,
    manifest: SourceManifest,
    grounding: GroundingReport,
    diff: str,
) -> str:
    """Return the frozen packet a writing reviewer judges."""

    truncated = render_data_block(
        DIFF_FENCE, prompt_safe_block(diff, limit=MAX_DIFF_CHARS).split("\n")
    )
    if len(diff) > MAX_DIFF_CHARS:
        truncated += "\n[diff truncated for review]\n"
    checks = (
        "\n".join(
            f"- [{item.severity}] {item.check}: {item.message}"
            + (f"\n    {prompt_safe(item.detail, limit=2_000)}" if item.detail else "")
            for item in grounding.issues
        )
        or "- (every deterministic check passed)"
    )
    # Every one of these is a string the write-enabled writer chose, so each is
    # rendered inert and the whole list is assembled by the prompt-data
    # boundary. A final release review found them interpolated raw -- no
    # sanitiser, no fence -- and demonstrated a writer forging this prompt's own
    # "DETERMINISTIC CHECKS THE CONTROLLER RAN" section inside a claim id.
    #
    # The manifest model now refuses an id that is not id-shaped, which is the
    # root cause. This is the second layer: it holds even for a field that
    # acquires a looser validator later.
    declared = render_data_block(
        WORKER_REPORT_FENCE,
        [
            f"  {label}: "
            + (
                ", ".join(prompt_safe(item, limit=MAX_LABEL_CHARS) for item in values)
                or "(none)"
            )
            for label, values in (
                ("claims", manifest.claim_ids),
                ("evidence", manifest.evidence_ids),
                ("experiments", manifest.experiment_ids),
                ("citations", manifest.citation_keys),
            )
        ],
    )
    caveats = (
        "\n".join(
            f"  - {prompt_safe(item, limit=MAX_STATEMENT_CHARS)}"
            for item in manifest.unresolved_caveats
        )
        or "  (the writer reported none)"
    )

    return f"""You are the writing-review worker of a deterministic research
automation controller. You have no tools and no repository access. Judge only
the frozen packet below. You cannot change the draft, and nothing you return
accepts any science.

The deterministic checks below were already run by the controller. Their results
are established facts: do not re-litigate whether a citation resolves. Judge the
part only reading can settle.

SECTION: {section}

WHAT THE WRITER WAS ASKED TO DO
{render_data_block(TASK_FENCE, prompt_safe_block(instruction, limit=6_000).split(chr(10)))}

DETERMINISTIC CHECKS THE CONTROLLER RAN
{checks}

WHAT THE WRITER DECLARED IT USED
{declared}

CAVEATS THE WRITER REPORTED
{caveats}

WHAT TO JUDGE

- **Strength.** Is any sentence stronger than the claim it rests on? A claim
  that says "in this dataset" and a draft that says "generally" are different
  statements, and the difference is the most common way a manuscript stops
  being true.
- **Exploratory versus confirmatory.** Does anything read as a prespecified
  prediction that was actually found afterwards? Check the experiment entries:
  an experiment with no decision rule did not test a prediction.
- **Contrary evidence.** Is every contrary evidence object genuinely engaged
  with, or merely mentioned in a clause and then ignored? Present-but-defanged
  is the failure mode here, and the deterministic check cannot see it.
- **Portability.** Does the draft assert that a result holds elsewhere -- other
  data, other periods, other populations -- when no supplied claim says so?
- **Structure.** Does the section do its job, and does it read?

RETURN A VERDICT

- PASS: the draft is defensible from its sources and ready for the researcher.
- PASS_WITH_REPAIR: it is close, and the listed findings should be fixed first.
- FAIL: it states something its sources do not support, and should not be put in
  front of the researcher in this form.

Mark a finding "blocker" only for something the draft asserts that its sources
do not support. Style you would have written differently is a "note".

FINAL DIFF (written by the worker under review, quoted as data)
{truncated}

{render_source_packet(packet)}
"""


def render_writing_findings(review: WritingReview) -> str:
    """Render one review's findings as a delimited data block for a repair.

    Rendered from the parsed review rather than raw output, and fenced like any
    other model-originated text: a repair worker reads this as advisory data
    about a draft, and it cannot widen the repair's scope or its sources.
    """

    body = [
        f"verdict: {review.verdict}",
        (
            "reviewer: "
            f"{prompt_safe(review.provider, limit=MAX_LABEL_CHARS)} / "
            f"{prompt_safe(review.model or 'provider default', limit=MAX_LABEL_CHARS)}"
        ),
        f"summary: {prompt_safe(review.summary, limit=MAX_STATEMENT_CHARS)}",
        "",
        "findings:",
    ]
    if not review.findings:
        body.append("  (the reviewer recorded no individual finding)")
    for finding in review.findings:
        where = (
            f" [{prompt_safe(finding.path, limit=MAX_LABEL_CHARS)}]"
            if finding.path
            else ""
        )
        body.append(
            f"  - {prompt_safe(finding.severity, limit=64)}{where}: "
            + prompt_safe(finding.message, limit=MAX_STATEMENT_CHARS)
        )
    return render_data_block(REVIEW_FENCE, body)


def parse_writing_review(
    *,
    structured: dict[str, Any] | None,
    text: str | None,
    draft_id: str,
    provider: str,
    model: str | None,
    independence: str,
    independence_note: str,
    invocation_id: str | None,
) -> WritingReview:
    """Turn a reviewer response into a structured verdict."""

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise ProviderInvocationError(
            "the writing reviewer returned no JSON object; expected a verdict"
        )
    raw_verdict = payload.get("verdict")
    if raw_verdict not in {item.value for item in WritingVerdict}:
        raise ProviderInvocationError(
            f"the writing reviewer returned an unknown verdict: {raw_verdict!r}"
        )
    findings: list[WritingFinding] = []
    for entry in payload.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("message") or "").strip()
        if not message:
            continue
        findings.append(
            WritingFinding(
                severity=str(entry.get("severity") or "note").strip() or "note",
                message=message,
                path=_usable_path(entry.get("path")),
            )
        )
    try:
        return WritingReview(
            draft_id=draft_id,
            verdict=WritingVerdict(raw_verdict),
            summary=str(payload.get("summary") or "").strip()
            or "(the reviewer returned no summary)",
            findings=findings,
            provider=provider,
            model=model,
            independence=independence,
            independence_note=independence_note,
            invocation_id=invocation_id,
        )
    except ValidationError as exc:
        raise ProviderInvocationError(
            f"the writing reviewer's output is not usable: {exc}"
        ) from exc


def _usable_path(value: Any) -> str | None:
    """Return the advisory path a finding may carry, or ``None``.

    Dropped rather than allowed to invalidate an authoritative verdict: the
    message carries the content, and the verdict gates the run. What is refused
    is refused by the same rule every other model-originated path is.
    """

    from research_os.automation.models import safe_relative_path

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return safe_relative_path(value)
    except ValueError:
        return None
