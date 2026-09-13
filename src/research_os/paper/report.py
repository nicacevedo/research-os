"""Rendering a draft for the researcher who has to decide whether to keep it.

What the report insists on:

**Nothing was merged and nothing was accepted.** A draft is a diff on a branch.
The report says so and gives the exact commands.

**The deterministic findings come before the reviewer's.** They are facts; the
reviewer's are judgement, and a reader who meets them in that order knows which
is which.

**A blocked draft is not summarised as though it were fine.** A draft whose
citations do not resolve is not "PASS with some notes"; it is a draft nobody
should read as grounded, and the report leads with why.
"""

from __future__ import annotations

from research_os.paper.models import (
    DraftRecord,
    GroundingReport,
    SourcePacket,
    WritingReview,
    WritingVerdict,
)
from research_os.textsafe import terminal_safe

RULE = "=" * 72
THIN = "-" * 72


def render_draft(draft: DraftRecord, packet: SourcePacket | None = None) -> str:
    """Render one writing task in full."""

    lines = [
        "",
        RULE,
        f"Draft {draft.draft_id}  —  {draft.section}",
        RULE,
        "",
        f"project           {draft.project_path}",
        f"project_id        {draft.project_id}",
        f"base commit       {draft.base_commit or 'none'}",
        f"branch            {draft.branch or '-'}",
        f"worktree          {draft.worktree_path or '-'}",
        (
            f"writer            {draft.provider or '-'} / "
            f"{draft.model or 'provider default'}"
        ),
        f"repair attempts   {draft.repair_attempts}",
        "",
        "instruction",
        f"  {draft.instruction}",
        "",
        "files changed",
    ]
    lines.extend(f"  {item}" for item in draft.changed_paths or ["(none)"])

    if packet is not None:
        lines.extend(_packet_section(packet))
    if draft.manifest is not None:
        lines.extend(_manifest_section(draft))
    if draft.grounding is not None:
        lines.extend(_grounding_section(draft.grounding))
    if draft.review is not None:
        lines.extend(_review_section(draft.review))
    if draft.failure_reason:
        lines.extend(["", f"failure           {draft.failure_reason}"])

    lines.extend(["", RULE, "next human action", RULE, ""])
    lines.extend(_next_action(draft))
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def _packet_section(packet: SourcePacket) -> list[str]:
    lines = [
        "",
        THIN,
        "what the writer was given",
        THIN,
        f"  accepted claims   {len(packet.claims)}",
        f"  evidence          {len(packet.evidence)}"
        + (
            f"  ({len(packet.contrary_evidence)} contrary)"
            if packet.contrary_evidence
            else ""
        ),
        f"  experiments       {len(packet.experiments)}",
        f"  literature        {len(packet.literature)}",
        f"  limitations       {len(packet.limitations)}",
    ]
    if packet.excluded_claims:
        lines.append("")
        lines.append("  claims asked for and withheld")
        for claim_id, reason in sorted(packet.excluded_claims.items()):
            lines.append(f"    {claim_id}: {reason}")
    return lines


def _manifest_section(draft: DraftRecord) -> list[str]:
    manifest = draft.manifest
    assert manifest is not None
    lines = ["", THIN, "source manifest (what the draft says it used)", THIN]
    for label, values in (
        ("claims", manifest.claim_ids),
        ("evidence", manifest.evidence_ids),
        ("experiments", manifest.experiment_ids),
        ("citations", manifest.citation_keys),
    ):
        lines.append(f"  {label:<14}{', '.join(values) or '(none)'}")
    lines.append("")
    lines.append("  unresolved caveats the writer reported")
    if not manifest.unresolved_caveats:
        lines.append("    (none)")
    lines.extend(f"    - {item}" for item in manifest.unresolved_caveats)
    return lines


def _grounding_section(grounding: GroundingReport) -> list[str]:
    lines = [
        "",
        THIN,
        "deterministic checks (facts, not judgement)",
        THIN,
        f"  grounded          {'yes' if grounding.grounded else 'NO'}",
        f"  citations found   {', '.join(grounding.cited_keys) or '(none)'}",
        f"  objects named     {', '.join(grounding.referenced_object_ids) or '(none)'}",
    ]
    if grounding.unsupported_numbers:
        lines.append(
            f"  numbers with no source  {', '.join(grounding.unsupported_numbers)}"
        )
    lines.append("")
    if not grounding.issues:
        lines.append("  every check passed")
    for item in grounding.issues:
        lines.append(f"  [{item.severity}] {item.check}: {item.message}")
        if item.detail:
            lines.append(f"        {item.detail}")
    return lines


def _review_section(review: WritingReview) -> list[str]:
    lines = [
        "",
        THIN,
        "independent writing review (advisory; NOT a scientific Review)",
        THIN,
        f"  verdict           {review.verdict}",
        f"  reviewer          {review.provider} / {review.model or 'provider default'}",
        f"  independence      {review.independence}",
    ]
    if review.independence_note:
        lines.append(f"                    {review.independence_note}")
    lines.append(f"  summary           {review.summary}")
    lines.append("")
    if not review.findings:
        lines.append("  findings          (none)")
    for finding in review.findings:
        where = f" [{finding.path}]" if finding.path else ""
        lines.append(f"  {finding.severity}{where}: {finding.message}")
    return lines


def _next_action(draft: DraftRecord) -> list[str]:
    grounding = draft.grounding
    review = draft.review
    if draft.failure_reason:
        return [
            f"This writing task failed: {draft.failure_reason}",
            "",
            "The worktree was left in place for you to inspect:",
            f"  {draft.worktree_path or '(none was created)'}",
        ]
    if grounding is not None and not grounding.grounded:
        return [
            "This draft is NOT grounded in its sources. Do not read it as though",
            "it were:",
            *(f"  - {item.message}" for item in grounding.blockers),
            "",
            "The diff is on a branch and nothing was merged:",
            f"     git -C {draft.worktree_path} diff HEAD",
        ]
    if review is not None and review.verdict is WritingVerdict.FAIL:
        return [
            f"The independent review returned FAIL: {review.summary}",
            "",
            "The draft is on a branch. Nothing was merged.",
            f"     git -C {draft.worktree_path} diff HEAD",
        ]
    lines = [
        "Nothing has been merged, pushed, or scientifically accepted. No Claim",
        "changed and no Evidence was created by writing this.",
        "",
        "1. Read the draft:",
        f"     git -C {draft.worktree_path} diff HEAD",
        "",
        "2. If you accept it, merge it yourself from the project:",
        f"     git -C {draft.project_path} merge --no-ff {draft.branch}",
        "",
        "3. Release the worktree when you are done:",
        f"     researchctl paper cleanup {draft.draft_id}",
    ]
    if review is not None and review.findings:
        lines.extend(
            [
                "",
                "The reviewer left findings above. They are advisory: the",
                "controller neither applied nor dismissed them.",
            ]
        )
    if draft.manifest is not None and draft.manifest.unresolved_caveats:
        lines.extend(
            [
                "",
                "The writer flagged caveats it was not confident about. Read those",
                "before the prose.",
            ]
        )
    return lines


def render_draft_list(drafts: list[DraftRecord]) -> str:
    lines = [""]
    if not drafts:
        lines.extend(["no drafts yet", ""])
        return terminal_safe("\n".join(lines) + "\n")
    for draft in drafts:
        state = (
            "failed"
            if draft.failure_reason
            else "ready"
            if draft.ready_for_human
            else "not grounded"
            if draft.grounding is not None and not draft.grounding.grounded
            else "needs attention"
        )
        lines.append(f"{state:16} {draft.draft_id}  {draft.section}")
        lines.append(f"    {draft.project_path}")
        lines.append(f"    {draft.instruction[:100]}")
        if draft.branch:
            lines.append(f"    branch {draft.branch}")
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_manifest(draft: DraftRecord) -> str:
    """Render the manifest alone, for a reader who wants only the provenance."""

    if draft.manifest is None:
        return terminal_safe("\nthis draft has no source manifest\n")
    lines = ["", f"Source manifest for {draft.draft_id}", ""]
    lines.extend(_manifest_section(draft)[4:])
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")
