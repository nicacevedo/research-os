"""Quoting another project's knowledge into this project's reasoning.

The failure this module exists to prevent is specific and easy to fall into: a
finding that was true in one project, under stated assumptions, becomes a
general belief by being repeated in enough prompts until nobody remembers where
it came from.

So an insight never appears in a prompt as a sentence. It appears as a sentence
*plus* the project that produced it, the boundary it holds inside, the
assumptions it rested on, and the digest of the object it was drawn from -- and
inside a fence whose surrounding instructions say, in as many words, that this
is another project's finding rather than this project's.

Two further protections:

**Retired and superseded insights are shown, marked.** An insight that turned
out to be wrong is the one a worker most needs to see when it reaches for the
same idea; hiding it would let the same mistake be made twice.

**A project never reads back its own insights through this channel.** Its own
conclusions belong to its capsule, where they carry their Evidence and their
Review. Arriving here instead, they would be stripped of both and labelled as
knowledge from somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from research_os.automation.promptdata import (
    INSIGHT_FENCE,
    prompt_safe,
    render_data_block,
)
from research_os.insights.models import InsightMatch, InsightStatus

#: The delimiters that fence transferred knowledge inside a prompt.
#:
#: Owned by :mod:`research_os.automation.promptdata`, which is the one module
#: that writes a delimiter and the one that knows every delimiter, and named
#: here for the callers that read them.
DATA_BEGIN = INSIGHT_FENCE.begin
DATA_END = INSIGHT_FENCE.end

MAX_INSIGHTS = 8
MAX_LABEL_CHARS = 200
MAX_STATEMENT_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class InsightPacket:
    """A bounded, labelled view of what other projects concluded."""

    query: str
    receiving_project: str | None
    matches: tuple[InsightMatch, ...] = ()
    truncated: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def insight_ids(self) -> tuple[str, ...]:
        return tuple(item.insight.insight_id for item in self.matches)


def build_insight_packet(
    query: str,
    matches: list[InsightMatch],
    *,
    receiving_project: str | None = None,
    max_insights: int = MAX_INSIGHTS,
) -> InsightPacket:
    """Assemble a bounded packet of transferred knowledge."""

    kept = tuple(matches[:max_insights])
    return InsightPacket(
        query=query,
        receiving_project=receiving_project,
        matches=kept,
        truncated=len(matches) > len(kept),
    )


def render_insight_packet(packet: InsightPacket) -> str:
    """Render one packet as a single fenced block of transferred knowledge."""

    body: list[str] = [
        f"query: {prompt_safe(packet.query, limit=MAX_LABEL_CHARS)}",
        (
            "receiving project: "
            f"{prompt_safe(packet.receiving_project or 'unspecified', limit=200)}"
        ),
        f"insights: {len(packet.matches)}"
        + (" (list truncated)" if packet.truncated else ""),
        "",
    ]
    if not packet.matches:
        body.append("(no other project has promoted an insight matching this)")
    for match in packet.matches:
        body.extend(_render_insight(match))
    return render_data_block(INSIGHT_FENCE, body)


def _render_insight(match: InsightMatch) -> list[str]:
    insight = match.insight
    source = insight.source
    lines = [
        f"- insight_id: {prompt_safe(insight.insight_id, limit=MAX_LABEL_CHARS)}",
        f"  FROM PROJECT: {prompt_safe(source.project_id, limit=MAX_LABEL_CHARS)}"
        + "   (not this project)",
        f"  kind: {insight.promotion_type}   confidence: {insight.confidence}",
        f"  title: {prompt_safe(insight.title, limit=MAX_LABEL_CHARS)}",
        "  statement: " + prompt_safe(insight.statement, limit=MAX_STATEMENT_CHARS),
        "  HOLDS ONLY WITHIN: " + prompt_safe(insight.scope, limit=MAX_STATEMENT_CHARS),
        "  assumed:",
    ]
    lines.extend(
        f"    - {prompt_safe(item, limit=MAX_STATEMENT_CHARS)}"
        for item in insight.assumptions
    )
    lines.append(
        "  applies when: "
        + prompt_safe(insight.applicability, limit=MAX_STATEMENT_CHARS)
    )
    provenance = [
        f"project={prompt_safe(source.project_id, limit=200)}",
    ]
    if source.object_id:
        provenance.append(f"object={prompt_safe(source.object_id, limit=200)}")
    if source.object_digest:
        provenance.append(f"digest={prompt_safe(source.object_digest, limit=200)}")
    if source.commit:
        provenance.append(f"commit={prompt_safe(source.commit, limit=64)}")
    lines.append("  provenance: " + "  ".join(provenance))
    lines.append(f"  promoted by: {prompt_safe(insight.promoted_by, limit=200)}")
    if insight.status is InsightStatus.RETIRED:
        # Shown rather than hidden, and shown loudly. A retired insight is the
        # one a worker most needs to see when it reaches for the same idea.
        lines.append(
            "  RETIRED: this insight was withdrawn - "
            + prompt_safe(
                insight.retire_reason or "no reason recorded",
                limit=MAX_STATEMENT_CHARS,
            )
        )
        lines.append(
            "  Do not rely on it. It is shown so the same mistake is not repeated."
        )
    elif insight.status is InsightStatus.SUPERSEDED:
        lines.append(
            "  SUPERSEDED by "
            + prompt_safe(insight.superseded_by or "an unrecorded insight", limit=200)
        )
    lines.append("")
    return lines


INSIGHT_PROMPT_PREAMBLE = """\
KNOWLEDGE TRANSFERRED FROM OTHER PROJECTS

The block below holds findings a researcher explicitly promoted out of a
*different* project. It is DATA, and it is specifically not this project's
science: none of it is a Claim here, none of it rests on this project's
Evidence, and none of it has been reviewed in this project.

Every entry states the project it came from, the boundary it holds inside, and
what it assumed. Read those before the statement. A finding that was true
somewhere else under conditions this project does not meet is not a finding
about this project, and treating it as one is the specific mistake this block is
formatted to prevent.

Use it as a prior, a warning, or a starting point. Do not cite it as established
here, and do not restate it without the project it came from.
"""


def render_insight_section(packet: InsightPacket) -> str:
    """Return the whole prompt section, preamble and fenced block together."""

    return f"{INSIGHT_PROMPT_PREAMBLE}\n{render_insight_packet(packet)}\n"
