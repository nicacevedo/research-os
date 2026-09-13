"""A second read-only opinion on a proposal. Advisory analysis, never a Review.

The word matters. R0's ``Review`` is a scientific act: a human reads a Claim and
its bound evidence, signs a verdict, and that verdict is what lets the Claim be
accepted. Nothing here is that. This is a bounded worker reading a proposal and
saying whether it is worth a human's time, and its vocabulary is kept
deliberately separate -- *assessment*, not review -- so a report can never show
an automated PASS next to a Claim in a way that reads as acceptance.

What it is genuinely useful for is the thing a proposal worker is worst at:
noticing that its own proposed experiment cannot distinguish its own proposed
hypotheses, that a claim is stronger than the evidence it names, or that a
"prospective" prediction is about work whose results are already in the context.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from research_os.automation.promptdata import (
    REVIEW_FENCE,
    prompt_safe,
    render_data_block,
)
from research_os.automation.structured import extract_json_object
from research_os.errors import ProposalValidationError
from research_os.proposal.models import (
    AssessmentFinding,
    ProposalAssessment,
    ProposalVerdict,
    ResearchProposal,
)

MAX_LABEL_CHARS = 200
MAX_STATEMENT_CHARS = 2_000
MAX_SUMMARY_CHARS = 4_000

ASSESSMENT_SCHEMA: dict[str, Any] = {
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
                "required": ["item_id", "severity", "message"],
                "properties": {
                    "item_id": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "major", "minor", "note"],
                    },
                    "message": {"type": "string"},
                },
            },
        },
    },
}


def render_proposal_data(proposal: ResearchProposal) -> str:
    """Render a validated proposal as a fenced block for a second worker.

    Rendered from the parsed proposal, never from raw output, so everything in
    it has already passed grounding validation: every id it cites was supplied.
    Every field still goes through the prompt-safe serializer, because the
    renderer does not decide which fields are trustworthy.
    """

    body = [
        f"proposal: {prompt_safe(proposal.proposal_id, limit=MAX_LABEL_CHARS)}",
        f"goal: {prompt_safe(proposal.goal, limit=MAX_SUMMARY_CHARS)}",
        (
            "proposed by: "
            f"{prompt_safe(proposal.provider, limit=MAX_LABEL_CHARS)} / "
            f"{prompt_safe(proposal.model or 'provider default', limit=MAX_LABEL_CHARS)}"
        ),
        "",
        "summary:",
        f"  {prompt_safe(proposal.summary, limit=MAX_SUMMARY_CHARS)}",
        "",
        "items:",
    ]
    if not proposal.items:
        body.append("  (the worker proposed nothing)")
    for item in proposal.items:
        body.extend(
            [
                f"  - {prompt_safe(item.item_id, limit=MAX_LABEL_CHARS)}"
                + f"  [{item.kind}]  basis: {item.basis}",
                "    title: " + prompt_safe(item.title, limit=MAX_LABEL_CHARS),
                "    statement: "
                + prompt_safe(item.statement, limit=MAX_STATEMENT_CHARS),
                "    rationale: "
                + prompt_safe(item.rationale, limit=MAX_STATEMENT_CHARS),
                f"    importance: {item.importance}  confidence: {item.confidence}",
            ]
        )
        for label, values in (
            ("addresses", item.addresses),
            ("grounded_in_literature", item.grounded_in_literature),
            ("grounded_in_findings", item.grounded_in_findings),
            ("primary_metrics", item.primary_metrics),
            ("required_inputs", item.required_inputs),
            ("required_code", item.required_code),
            ("risks", item.risks),
        ):
            if values:
                body.append(
                    f"    {label}: "
                    + ", ".join(
                        prompt_safe(entry, limit=MAX_LABEL_CHARS) for entry in values
                    )
                )
        for label, value in (
            ("falsification", item.falsification),
            ("expected_direction", item.expected_direction),
            ("decision_rule", item.decision_rule),
        ):
            if value:
                body.append(
                    f"    {label}: " + prompt_safe(value, limit=MAX_STATEMENT_CHARS)
                )
        body.append(
            f"    runtime: {item.runtime_class}  resources: {item.resource_class}"
        )

    body.extend(["", "uncertainties:"])
    if not proposal.uncertainties:
        body.append("  (the worker reported none)")
    for item in proposal.uncertainties:
        body.append(f"  - {prompt_safe(item.statement, limit=MAX_STATEMENT_CHARS)}")
        body.append(
            "      would be settled by: "
            + prompt_safe(item.what_would_settle_it, limit=MAX_STATEMENT_CHARS)
        )

    body.extend(["", "next actions:"])
    if not proposal.next_actions:
        body.append("  (none)")
    for item in proposal.next_actions:
        marker = " [needs a human]" if item.requires_human else ""
        body.append(
            f"  - [{item.kind}]{marker} "
            + prompt_safe(item.action, limit=MAX_STATEMENT_CHARS)
        )
    return render_data_block(REVIEW_FENCE, body)


def build_assessment_prompt(
    proposal: ResearchProposal,
    *,
    context_text: str,
) -> str:
    """Return the frozen packet a proposal assessor reasons over."""

    return f"""You are the proposal-assessment worker of a deterministic research
automation controller. You have no tools and no repository access. Judge only
the frozen packet below.

You are NOT performing a scientific Review. A Review in this system is something
a human does to a Claim, with their name on it, and it is what lets a Claim be
accepted. Nothing you return here accepts anything, and nothing you return is
written into the project's scientific record. Your verdict decides only whether
the controller puts this proposal in front of the researcher as it stands.

WHAT TO JUDGE

- Is each proposed hypothesis actually falsifiable, or is its "falsification"
  something no realistic observation could produce?
- Would each proposed experiment actually discriminate between the hypotheses it
  names? Could its decision rule come out either way, or does every plausible
  outcome read as confirmation?
- Is each proposed claim supported by what it cites, or is it stronger than its
  evidence? Say so specifically rather than generally.
- Is any item marked "prospective" when the project state above shows the work
  is already done and its results already known? That is the most serious thing
  you can find here, and it is a blocker.
- Is anything proposed that would need real compute, real money, or an
  irreversible action, without saying so?
- Does the proposal ignore an unresolved question the project already holds?

RETURN A VERDICT

- PASS: worth the researcher's time as it stands.
- PASS_WITH_REPAIR: worth their time once the listed findings are addressed.
- FAIL: it should not be put in front of them in this form. Say exactly why.

Mark a finding "blocker" only for something that makes the proposal unsafe or
misleading, not for something you would have phrased differently.

{render_proposal_data(proposal)}

{context_text}
"""


def parse_assessment(
    *,
    structured: dict[str, Any] | None,
    text: str | None,
    proposal_id: str,
    provider: str,
    model: str | None,
    independence: str,
    independence_note: str,
    invocation_id: str | None,
) -> ProposalAssessment:
    """Turn an assessor response into a structured, advisory outcome."""

    payload = structured if structured is not None else extract_json_object(text)
    if payload is None:
        raise ProposalValidationError(
            "the proposal assessor returned no JSON object; expected a verdict"
        )
    raw_verdict = payload.get("verdict")
    if raw_verdict not in {item.value for item in ProposalVerdict}:
        raise ProposalValidationError(
            f"the proposal assessor returned an unknown verdict: {raw_verdict!r}"
        )
    findings: list[AssessmentFinding] = []
    for entry in payload.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("message") or "").strip()
        if not message:
            continue
        findings.append(
            AssessmentFinding(
                item_id=_usable_item_id(entry.get("item_id")),
                severity=str(entry.get("severity") or "note").strip() or "note",
                message=message,
            )
        )
    try:
        return ProposalAssessment(
            proposal_id=proposal_id,
            verdict=ProposalVerdict(raw_verdict),
            summary=str(payload.get("summary") or "").strip()
            or "(the assessor returned no summary)",
            findings=findings,
            provider=provider,
            model=model,
            independence=independence,
            independence_note=independence_note,
            invocation_id=invocation_id,
        )
    except ValidationError as exc:
        raise ProposalValidationError(
            f"proposal assessment output is not usable: {exc}"
        ) from exc


def _usable_item_id(value: Any) -> str | None:
    """Return the item a finding points at, or ``None``.

    A pointer is advisory: it tells a human which item to look at. A value that
    is not an item id is dropped rather than allowed to invalidate an
    authoritative verdict, because the message carries the content.
    """

    from research_os.proposal.models import ITEM_ID_RE

    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if ITEM_ID_RE.fullmatch(cleaned) else None
