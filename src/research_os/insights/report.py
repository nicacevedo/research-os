"""Rendering insights for a human, with the boundary before the conclusion.

Every view here puts scope and assumptions above the statement, or immediately
beside it. That ordering is the safety property: a reader skimming a list of
insights should meet "this held in project X, assuming Y" before they meet the
finding, because the failure this whole feature risks is a scoped result quietly
becoming a general belief.

Everything an agent or another project wrote passes through the display
sanitizer on the way out.
"""

from __future__ import annotations

from research_os.insights.models import (
    InsightMatch,
    InsightNomination,
    InsightStatus,
    PromotedInsight,
)
from research_os.textsafe import terminal_safe

RULE = "=" * 72
THIN = "-" * 72


def render_insight(insight: PromotedInsight, *, preview: bool = False) -> str:
    """Render one insight in full.

    ``preview`` is used before it exists, when a human is deciding whether to
    create it, and adds what promotion would mean.
    """

    source = insight.source
    lines = [
        "",
        RULE,
        ("Would promote: " if preview else "") + insight.title,
        RULE,
        "",
        f"insight_id      {insight.insight_id}",
        f"from project    {source.project_id}",
        f"kind            {insight.promotion_type}",
        f"confidence      {insight.confidence}",
        f"status          {insight.status}",
        "",
        "statement",
        f"  {insight.statement}",
        "",
        "holds only within",
        f"  {insight.scope}",
        "",
        "assumed",
        *(f"  - {item}" for item in insight.assumptions),
        "",
        "applies when",
        f"  {insight.applicability}",
        "",
        "provenance",
        f"  project       {source.project_id}",
    ]
    if source.project_path:
        lines.append(f"  path          {source.project_path}")
    if source.object_id:
        lines.append(
            f"  object        {source.object_id} ({source.object_type or '?'})"
        )
    if source.object_digest:
        lines.append(f"  digest        {source.object_digest}")
    if source.commit:
        lines.append(f"  commit        {source.commit}")
    if source.run_id:
        lines.append(f"  run           {source.run_id}")
    if source.detail:
        lines.append(f"  detail        {source.detail}")
    lines.extend(
        [
            f"  promoted by   {insight.promoted_by}",
            f"  created       {insight.created_at}",
        ]
    )
    if insight.keywords:
        lines.append(f"  keywords      {', '.join(insight.keywords)}")
    if insight.status is InsightStatus.RETIRED:
        lines.extend(
            [
                "",
                "RETIRED",
                f"  {insight.retire_reason}",
                "  It stays readable so the same mistake is not repeated.",
            ]
        )
    elif insight.status is InsightStatus.SUPERSEDED:
        lines.extend(["", f"SUPERSEDED BY   {insight.superseded_by}"])

    lines.extend(["", THIN, "what this is", THIN, ""])
    lines.extend(
        [
            "This is not a Claim, in this project or any other. It rests on no",
            "Evidence here and has passed no Review here. It is something a",
            "researcher decided was worth remembering outside the project that",
            "produced it.",
            "",
            "It reaches other projects labelled with the three things above the",
            "statement -- source, scope, assumptions -- and is never presented",
            "there as established.",
        ]
    )
    if preview:
        lines.extend(
            [
                "",
                "Promoting it makes it readable by every project on this machine.",
                "You can withdraw it later with 'researchctl insight retire'; it",
                "stays readable when you do.",
            ]
        )
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_insight_list(insights: list[PromotedInsight]) -> str:
    lines = [""]
    if not insights:
        lines.extend(
            [
                "no promoted insights",
                "",
                "Nothing has been moved between projects. An agent may nominate a",
                "candidate; only you promote one.",
                "",
            ]
        )
        return terminal_safe("\n".join(lines) + "\n")
    for insight in insights:
        marker = "" if insight.live else f"   [{insight.status}]"
        lines.append(f"{insight.insight_id}  {insight.promotion_type:16}{marker}")
        lines.append(f"    {insight.title}")
        lines.append(
            f"    from {insight.source.project_id}; holds within: {insight.scope}"
        )
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_matches(matches: list[InsightMatch], *, query: str) -> str:
    lines = ["", f"{len(matches)} insight(s) matching: {query}", ""]
    if not matches:
        lines.extend(
            [
                "Nothing another project promoted matches this.",
                "",
            ]
        )
        return terminal_safe("\n".join(lines) + "\n")
    for position, match in enumerate(matches, start=1):
        insight = match.insight
        marker = "" if insight.live else f"  [{insight.status}]"
        lines.append(f"{position:2}. {insight.title}{marker}")
        lines.append(
            f"    {insight.insight_id}   score {match.score:.2f}   "
            f"matched: {', '.join(match.matched_terms)}"
        )
        lines.append(f"    FROM PROJECT {insight.source.project_id} (not yours)")
        lines.append(f"    holds within: {insight.scope}")
        lines.append(f"    {insight.statement}")
        if insight.status is InsightStatus.RETIRED:
            lines.append(f"    RETIRED: {insight.retire_reason}")
        lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_nomination(nomination: InsightNomination) -> str:
    """Render one nomination, saying plainly that it reaches nobody yet."""

    lines = [
        "",
        f"Nominated {nomination.nomination_id}",
        "",
        f"  title         {nomination.title}",
        f"  statement     {nomination.statement}",
        f"  from project  {nomination.source.project_id}",
        f"  kind          {nomination.promotion_type}",
        f"  nominated by  {nomination.nominated_by}",
        f"  rationale     {nomination.rationale}",
    ]
    missing = nomination.missing_for_promotion
    if missing:
        lines.extend(
            [
                "",
                f"  still needed  {', '.join(missing)}",
                "                An insight without a scope and its assumptions is",
                "                a sentence that will be applied where it does not",
                "                hold, so promotion refuses until you supply them.",
            ]
        )
    if nomination.promoted_insight_id:
        lines.append(f"  promoted as   {nomination.promoted_insight_id}")
    elif nomination.declined_reason:
        lines.append(f"  declined      {nomination.declined_reason}")
    else:
        lines.extend(
            [
                "",
                "  This has reached no other project. It is a suggestion in runtime",
                "  state; promoting it is a human action:",
                "",
                f"      researchctl insight promote {nomination.nomination_id} \\",
                '          --scope "..." --assumption "..." --applicability "..."',
            ]
        )
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_nomination_list(nominations: list[InsightNomination]) -> str:
    lines = [""]
    if not nominations:
        lines.extend(["no nominations", ""])
        return terminal_safe("\n".join(lines) + "\n")
    for item in nominations:
        state = (
            f"promoted as {item.promoted_insight_id}"
            if item.promoted_insight_id
            else f"declined: {item.declined_reason}"
            if item.declined_reason
            else "pending"
        )
        lines.append(f"{item.nomination_id}  {state}")
        lines.append(f"    {item.title}   (from {item.source.project_id})")
        if item.pending and item.missing_for_promotion:
            lines.append(f"    needs: {', '.join(item.missing_for_promotion)}")
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")
