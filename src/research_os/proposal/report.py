"""Rendering a proposal for the person who has to decide what to do about it.

Two things every view insists on saying, because a report that leaves them out
is the report that gets a draft promoted without being read:

**Nothing here is science yet.** Every rendering says so, in those words, and
says what the human action would be.

**Basis is shown next to every item.** ``prospective`` and ``historical`` are
the difference between a prediction and a description, and a reader skimming a
list of proposed experiments needs to see which is which without looking it up.

Everything a worker wrote passes through the display sanitizer on the way out.
"""

from __future__ import annotations

from research_os.proposal.models import (
    DeclineRecord,
    EvidenceBasis,
    PromotionRecord,
    ProposalAssessment,
    ProposalVerdict,
    ResearchProposal,
)
from research_os.proposal.promote import PreparedPromotion
from research_os.textsafe import terminal_safe

RULE = "=" * 72
THIN = "-" * 72


def render_proposal(
    proposal: ResearchProposal,
    *,
    assessment: ProposalAssessment | None = None,
    promotions: list[PromotionRecord] | None = None,
    declines: list[DeclineRecord] | None = None,
) -> str:
    """Render one complete proposal, its assessment, and what was decided.

    Both decisions, because a reader who sees only promotions cannot tell an
    item nobody has opened from one that was read and rejected -- and those are
    the two states this view exists to distinguish.
    """

    done = {item.item_id: item for item in promotions or []}
    refused = {item.item_id: item for item in declines or []}
    lines = [
        "",
        RULE,
        f"Proposal {proposal.proposal_id}",
        RULE,
        "",
        f"goal            {proposal.goal}",
        f"project         {proposal.project_path}",
        f"project_id      {proposal.project_id or 'unregistered'}",
        f"base commit     {proposal.base_commit or '-'}",
        (
            f"proposed by     {proposal.provider} / "
            f"{proposal.model or 'provider default'}"
        ),
        f"created         {proposal.created_at}",
        "",
        "summary",
        f"  {proposal.summary}",
    ]

    lines.extend(["", THIN, "proposed items", THIN])
    if not proposal.items:
        lines.append("  (nothing was proposed)")
    for item in proposal.items:
        marker = ""
        if item.item_id in done:
            record = done[item.item_id]
            marker = f"   PROMOTED -> {record.object_id} ({record.object_status})"
        elif item.item_id in refused:
            marker = f"   DECLINED: {terminal_safe(refused[item.item_id].reason)}"
        lines.extend(
            [
                "",
                f"{item.item_id}  [{item.kind}]  basis: {item.basis}{marker}",
                f"  title           {item.title}",
                f"  statement       {item.statement}",
                f"  rationale       {item.rationale}",
                f"  importance      {item.importance}   confidence: {item.confidence}",
            ]
        )
        if item.basis is EvidenceBasis.HISTORICAL:
            lines.append(
                "  NOTE            historical: this describes work already done, "
                "so it can never be promoted as a preregistered experiment"
            )
        for label, values in (
            ("addresses", item.addresses),
            ("from literature", item.grounded_in_literature),
            ("from findings", item.grounded_in_findings),
            ("primary metrics", item.primary_metrics),
            ("required inputs", item.required_inputs),
            ("required code", item.required_code),
            ("risks", item.risks),
        ):
            if values:
                lines.append(f"  {label:<15} {', '.join(values)}")
        for label, value in (
            ("falsification", item.falsification),
            ("expected", item.expected_direction),
            ("decision rule", item.decision_rule),
        ):
            if value:
                lines.append(f"  {label:<15} {value}")
        if item.kind.value == "experiment":
            lines.append(
                f"  cost            runtime {item.runtime_class}, "
                f"resources {item.resource_class}"
            )
        if not item.promotable:
            lines.append(
                "  promotion       not promotable: this is a reading of results "
                "for you, not something the project asserts"
            )

    lines.extend(["", THIN, "open uncertainties", THIN])
    if not proposal.uncertainties:
        lines.append("  (the worker reported none)")
    for item in proposal.uncertainties:
        lines.append(f"  - {item.statement}")
        lines.append(f"      would be settled by: {item.what_would_settle_it}")
        if item.blocks:
            lines.append(f"      blocks: {', '.join(item.blocks)}")

    lines.extend(["", THIN, "recommended next actions", THIN])
    if not proposal.next_actions:
        lines.append("  (none)")
    for item in proposal.next_actions:
        flag = "  [NEEDS A HUMAN]" if item.requires_human else ""
        lines.append(f"  [{item.kind}]{flag} {item.action}")
        lines.append(f"      {item.rationale}")

    lines.extend(render_assessment_section(assessment))
    lines.extend(["", RULE, "what this is, and is not", RULE, ""])
    lines.extend(_boundary_lines(proposal, done))
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_assessment_section(assessment: ProposalAssessment | None) -> list[str]:
    """Render the advisory assessment, labelled so it cannot read as a Review."""

    lines = [
        "",
        THIN,
        "independent assessment (advisory; NOT a scientific Review)",
        THIN,
    ]
    if assessment is None:
        lines.append("  (none was run)")
        return lines
    lines.extend(
        [
            f"  verdict       {assessment.verdict}",
            (
                f"  assessor      {assessment.provider} / "
                f"{assessment.model or 'provider default'}"
            ),
            f"  independence  {assessment.independence}",
        ]
    )
    if assessment.independence_note:
        lines.append(f"                {assessment.independence_note}")
    lines.append(f"  summary       {assessment.summary}")
    if assessment.findings:
        lines.append("  findings")
        for finding in assessment.findings:
            where = f" [{finding.item_id}]" if finding.item_id else ""
            lines.append(f"    {finding.severity}{where}: {finding.message}")
    else:
        lines.append("  findings      (none)")
    return lines


def _boundary_lines(
    proposal: ResearchProposal, done: dict[str, PromotionRecord]
) -> list[str]:
    promotable = [item for item in proposal.items if item.promotable]
    remaining = [item for item in promotable if item.item_id not in done]
    lines = [
        "Nothing above is part of this project's scientific record. No Claim was",
        "accepted, no Review was written, and no capsule file was changed by",
        "producing this proposal.",
        "",
    ]
    if remaining:
        lines.extend(
            [
                "To turn one item into a DRAFT capsule object -- which you then still",
                "have to complete, review, and accept yourself:",
                "",
                (
                    f"     researchctl propose promote {proposal.proposal_id} "
                    f"--item {remaining[0].item_id}"
                ),
                "",
                "That command requires an interactive terminal and asks you to",
                "confirm. It only ever creates a draft.",
            ]
        )
    else:
        lines.append(
            "Every promotable item here has already been promoted, or there were none."
        )
    if proposal.human_checkpoints:
        lines.extend(
            [
                "",
                "The worker flagged these as decisions only you can make:",
                *(f"  - {item.action}" for item in proposal.human_checkpoints),
            ]
        )
    return lines


def render_promotion_preview(prepared: PreparedPromotion) -> str:
    """Render exactly what would be written, before anything is written."""

    item = prepared.item
    lines = [
        "",
        RULE,
        f"Promote {item.item_id} into a DRAFT {prepared.obj.type}",
        RULE,
        "",
        f"proposal        {prepared.proposal.proposal_id}",
        f"project         {prepared.git_root}",
        f"new object      {prepared.obj.id}  (status: {prepared.obj.status})",
        f"file            {prepared.relative_target}",
        f"basis           {item.basis}",
        "",
    ]
    if item.basis is EvidenceBasis.HISTORICAL:
        lines.extend(
            [
                "This item describes work whose results were already known when it",
                "was proposed. It is being written as a DRAFT with no",
                "preregistration fields: a decision rule recorded after the outcome",
                "is a description, not a prediction. The proposed rule is kept in",
                "the object's notes, labelled retrospective.",
                "",
            ]
        )
    lines.extend(
        [
            "This is what would be written:",
            "",
            THIN,
            prepared.document.rstrip("\n"),
            THIN,
            "",
            "This creates a DRAFT. It does not accept a Claim, does not write a",
            "Review, and does not commit anything to Git. You remain responsible",
            "for everything it says.",
            "",
        ]
    )
    return terminal_safe("\n".join(lines) + "\n")


def render_proposal_list(
    entries: list[tuple[ResearchProposal, ProposalAssessment | None, int]],
) -> str:
    """Render one line per proposal: what it was for, and where it stands."""

    lines = [""]
    if not entries:
        lines.extend(["no proposals yet", ""])
        return terminal_safe("\n".join(lines) + "\n")
    for proposal, assessment, promoted in entries:
        verdict = str(assessment.verdict) if assessment else "unassessed"
        lines.append(
            f"{proposal.proposal_id}  {verdict:16} "
            f"{len(proposal.items)} item(s), {promoted} promoted"
        )
        lines.append(f"    {proposal.project_path}")
        lines.append(f"    {proposal.goal}")
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def blocking_summary(assessment: ProposalAssessment | None) -> str:
    """Return a one-line warning when an assessment found something serious."""

    if assessment is None:
        return ""
    if assessment.verdict is ProposalVerdict.FAIL:
        return (
            "The independent assessment returned FAIL on this proposal. Read its "
            "findings before promoting anything from it."
        )
    if assessment.blocking:
        return (
            f"The independent assessment left {len(assessment.blocking)} "
            "blocking or major finding(s) unresolved."
        )
    return ""


def render_decline_preview(
    proposal: ResearchProposal, item_ids: list[str], *, reason: str
) -> str:
    """Show exactly what is about to be closed, before it is closed.

    A decline writes no file, so there is no document to preview -- which is
    precisely why the items have to be named and quoted here. The thing being
    decided is not visible anywhere else at the moment of deciding, and
    "decline PROP-... --reason ..." on a nine-item proposal is otherwise a
    command whose effect the person cannot see.
    """

    by_id = {item.item_id: item for item in proposal.items}
    lines = [
        "",
        RULE,
        f"Decline {len(item_ids)} item(s) of {proposal.proposal_id}",
        RULE,
        "",
        f"project         {terminal_safe(proposal.project_path)}",
        f"reason          {terminal_safe(reason)}",
        "",
        "These items would be recorded as decided and not pursued:",
        "",
    ]
    for item_id in item_ids:
        item = by_id.get(item_id)
        if item is None:  # pragma: no cover - the caller validated the ids
            continue
        lines.append(f"  {item_id}  {terminal_safe(item.title)}")
        lines.append(f"            {terminal_safe(item.statement)}")
        lines.append("")
    lines.extend(
        [
            THIN,
            "This writes nothing into your project and changes no scientific",
            "object. What it changes is that the runtime will stop treating",
            "these items as questions nobody has answered, so it will not keep",
            "re-proposing them from the same findings.",
            "",
            "It is still your decision and it is recorded as yours, with the",
            "reason, in the proposal's ledger.",
            "",
        ]
    )
    return "\n".join(lines)
