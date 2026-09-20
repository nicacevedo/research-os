"""The periodic digest: what happened, rendered from rows.

**No model writes a word of it.** This is the one artifact a returning
researcher reads *instead of* everything else, and an unlabelled model
narrative there would do more damage per word than anywhere else in the
system. Every sentence below is assembled from stored fields by ordinary
Python, and the parts that are quotations -- an idea's research question, a
reviewer's objection -- are quoted as what they are.

**Top ideas are a Pareto front, not an ordering.** The brief's §8 is explicit
that a single opaque score must not replace the dimensions, and §25 that the
human-facing output must preserve diversity. So the selection takes the
non-dominated set across the quality dimensions and then thins it by diversity,
rather than sorting by the scheduling utility -- which exists, is operational,
and is deliberately not consulted here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from research_os.portfolio import allocation
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.gates import board_independence
from research_os.portfolio.models import (
    SEVERITY_ORDER,
    TIER_ORDER,
    IdeaStatus,
    PortfolioDigestRecord,
    PortfolioIdea,
    QualityDimensions,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database

LOG = logging.getLogger("research_os.portfolio.digest")

#: The dimensions the Pareto front is computed over. Not every dimension: the
#: three a system can assess about itself without evidence -- plausibility,
#: tractability, reviewer confidence -- are excluded from *selection* and still
#: reported, because otherwise the front would rank confident prose highly.
FRONT_DIMENSIONS: tuple[str, ...] = (
    "novelty",
    "potential_impact",
    "falsifiability",
    "evidence_strength",
    "reproducibility",
    "literature_confidence",
)

MAX_TOP_IDEAS = 8


@dataclass(frozen=True, slots=True)
class TopIdea:
    idea: PortfolioIdea
    research_question: str
    why_it_matters: str
    closest_prior_work: str
    falsifier: str
    limitations: tuple[str, ...]
    next_action: str
    evidence: tuple[str, ...]
    review_consensus: str
    strongest_objection: str
    lineage: tuple[str, ...]
    board_independence: int


def _dominates(left: QualityDimensions, right: QualityDimensions) -> bool:
    """Pareto dominance over the assessed dimensions.

    An unassessed dimension counts as the midpoint on both sides, so an idea is
    not ranked above another for having been measured less.
    """

    better_anywhere = False
    for name in FRONT_DIMENSIONS:
        a = getattr(left, name)
        b = getattr(right, name)
        a = 0.5 if a is None else a
        b = 0.5 if b is None else b
        if a < b:
            return False
        if a > b:
            better_anywhere = True
    return better_anywhere


def pareto_front(
    entries: Sequence[tuple[PortfolioIdea, QualityDimensions]],
) -> list[tuple[PortfolioIdea, QualityDimensions]]:
    """The non-dominated set, in idea-id order so the output is reproducible."""

    front = [
        item
        for item in entries
        if not any(
            _dominates(other[1], item[1]) for other in entries if other is not item
        )
    ]
    return sorted(front, key=lambda item: item[0].idea_id)


def diversify(
    entries: Sequence[tuple[PortfolioIdea, QualityDimensions, allocation.DiversityKey]],
    *,
    limit: int,
    threshold: float = 0.6,
) -> list[tuple[PortfolioIdea, QualityDimensions, allocation.DiversityKey]]:
    """Thin a list so the researcher does not read eight versions of one idea.

    Greedy, deterministic, and it prefers the earlier entry on a tie, which
    combined with the front's id ordering makes the output a function of state.
    """

    kept: list[tuple[PortfolioIdea, QualityDimensions, allocation.DiversityKey]] = []
    for entry in entries:
        if len(kept) >= limit:
            break
        if any(entry[2].overlap(other[2]) >= threshold for other in kept):
            continue
        kept.append(entry)
    return kept


def produce(
    *,
    db: Database,
    project_id: str,
    config: PortfolioConfig,
    since: datetime | None = None,
    now: datetime | None = None,
) -> PortfolioDigestRecord:
    """Assemble and store one digest."""

    store = PortfolioStore(db)
    moment = now or datetime.now(UTC)
    state = store.get_state(project_id) or store.upsert_state(project_id=project_id)
    start = since or state.last_digest_at or (moment - timedelta(days=1))
    # Clamped, because the period is a closed interval and the schema says so.
    # The case that reaches here is two digests produced at the same instant --
    # the second reads the first's `last_digest_at`, which is a real clock
    # reading taken after the `now` it was handed. An empty period is a true
    # statement about nothing having happened; an inverted one is a constraint
    # violation in the middle of the one artifact a researcher reads.
    start = min(start, moment)

    ideas = store.list_ideas(project_id=project_id, limit=2_000)
    counts = store.counts_by_status(project_id)
    recent = [item for item in ideas if item.created_at >= start]
    newly_human_ready = [
        item
        for item in ideas
        if item.status is IdeaStatus.HUMAN_READY and item.updated_at >= start
    ]
    demoted = [
        item
        for item in ideas
        if item.status in {IdeaStatus.REJECTED, IdeaStatus.PARKED}
        and TIER_ORDER[item.quality_tier] > 0
    ]
    spend = sum(
        (store.spend_for_idea(item.idea_id) for item in ideas), start=Decimal(0)
    )
    top = _top_ideas(store, ideas, project_id)

    payload: dict[str, Any] = {
        "digest_id": "",
        "project_id": project_id,
        "period_start": start.isoformat(),
        "period_end": moment.isoformat(),
        "counts": {
            "ideas_total": len(ideas),
            "ideas_new_this_period": len(recent),
            "candidate": counts.get(IdeaStatus.CANDIDATE, 0),
            "promising": counts.get(IdeaStatus.PROMISING, 0),
            "investigating": counts.get(IdeaStatus.INVESTIGATING, 0),
            "in_review": counts.get(IdeaStatus.REVIEW, 0),
            "validated": counts.get(IdeaStatus.VALIDATED, 0),
            "human_ready": counts.get(IdeaStatus.HUMAN_READY, 0),
            "human_ready_new": len(newly_human_ready),
            "rejected": counts.get(IdeaStatus.REJECTED, 0),
            "parked": counts.get(IdeaStatus.PARKED, 0),
            "superseded": counts.get(IdeaStatus.SUPERSEDED, 0),
            "demoted_after_reaching_a_tier": len(demoted),
            "active_tracks": store.active_count(project_id),
            "uncurated": store.uncurated_count(project_id),
            "spend_usd": str(spend),
            "lineage_families": len(store.lineage_active_counts(project_id)),
        },
        "diversity": store.adjudication_counts(project_id),
        "sections": {
            "negative_results": _negative_results(store, ideas, start),
            "demoted": [
                f"`{item.idea_id}` reached {item.quality_tier} and is now "
                f"{item.status}: {item.retire_reason or 'no reason recorded'}"
                for item in sorted(demoted, key=lambda row: row.idea_id)
            ],
            "blocked": [
                f"`{item.idea_id}` is {item.operational_state}"
                for item in sorted(ideas, key=lambda row: row.idea_id)
                if item.operational_state.name.startswith("BLOCKED")
            ],
        },
        "top_ideas": [_render_top(item) for item in top],
    }
    record = store.record_digest(
        project_id=project_id,
        period_start=start,
        period_end=moment,
        payload=payload,
    )
    # The id is part of what a reader needs and is not known until the row
    # exists. Written back rather than left blank, because a digest that cannot
    # name itself cannot be cited.
    payload["digest_id"] = record.digest_id
    with db.tx() as conn:
        from research_os.runtime.db import jsonb

        conn.execute(
            "update portfolio_digests set payload = %s where digest_id = %s",
            (jsonb(payload), record.digest_id),
        )
    store.mark_digest_scheduled(project_id)
    del config
    return record.model_copy(update={"payload": payload})


def _negative_results(
    store: PortfolioStore, ideas: Sequence[PortfolioIdea], since: datetime
) -> list[str]:
    """What was ruled out, and why. The half of a portfolio nobody writes down.

    Ordered by severity then id, so the thing that killed an idea outright
    appears before the thing that merely parked one.
    """

    found: list[tuple[int, str]] = []
    for idea in ideas:
        if idea.status not in {IdeaStatus.REJECTED, IdeaStatus.PARKED}:
            continue
        if idea.updated_at < since:
            continue
        objections = store.open_objections(idea_id=idea.idea_id)
        worst = max(
            (item.severity for item in objections),
            key=lambda item: SEVERITY_ORDER[item],
            default=None,
        )
        rank = SEVERITY_ORDER[worst] if worst else 0
        found.append(
            (
                -rank,
                f"`{idea.idea_id}` {idea.status.lower()}: "
                f"{idea.retire_reason or 'no reason recorded'}"
                + (f" (worst objection: {worst})" if worst else ""),
            )
        )
    return [text for _rank, text in sorted(found)]


def _top_ideas(
    store: PortfolioStore, ideas: Sequence[PortfolioIdea], project_id: str
) -> list[TopIdea]:
    del project_id
    eligible = [
        item
        for item in ideas
        if item.status
        in {
            IdeaStatus.HUMAN_READY,
            IdeaStatus.VALIDATED,
            IdeaStatus.REVIEW,
            IdeaStatus.PROMISING,
            IdeaStatus.INVESTIGATING,
        }
    ]
    scored: list[tuple[PortfolioIdea, QualityDimensions]] = []
    for idea in eligible:
        version = store.get_version(idea.idea_id)
        if version is None:
            continue
        scored.append((idea, version.dimensions))

    front = pareto_front(scored)
    # HUMAN_READY first, then VALIDATED, then the rest: the front is about
    # quality dimensions and the tier is about what the gates established, and
    # a researcher reading this wants the second first.
    front.sort(key=lambda item: (-TIER_ORDER[item[0].quality_tier], item[0].idea_id))
    with_keys = []
    for idea, dimensions in front:
        version = store.get_version(idea.idea_id)
        if version is None:
            continue
        with_keys.append(
            (
                idea,
                dimensions,
                allocation.diversity_key(
                    lineage_root=idea.lineage_root,
                    adjudication=[str(item) for item in version.adjudication_types],
                    research_question=version.research_question,
                ),
            )
        )
    chosen = diversify(with_keys, limit=MAX_TOP_IDEAS)
    return [_build_top(store, idea) for idea, _dimensions, _key in chosen]


def _build_top(store: PortfolioStore, idea: PortfolioIdea) -> TopIdea:
    version = store.require_version(idea.idea_id)
    reviews = store.live_reviews(idea_id=idea.idea_id)
    objections = store.open_objections(idea_id=idea.idea_id)
    strongest = max(
        objections, key=lambda item: SEVERITY_ORDER[item.severity], default=None
    )
    verdicts = ", ".join(
        f"{item.reviewer_role}: {item.verdict}"
        for item in sorted(reviews, key=lambda row: str(row.reviewer_role))
    )
    evidence = store.list_evidence(idea_id=idea.idea_id, idea_version=version.version)
    return TopIdea(
        idea=idea,
        research_question=version.research_question,
        why_it_matters=version.why_it_matters,
        closest_prior_work=version.closest_prior_work,
        falsifier=version.falsifier,
        limitations=version.open_uncertainties,
        next_action=version.next_best_action,
        evidence=tuple(
            f"[{item.kind}/{item.strength}] {item.summary}"
            for item in sorted(evidence, key=lambda row: row.evidence_id)
        ),
        review_consensus=verdicts or "no live review",
        strongest_objection=(
            f"[{strongest.severity}] {strongest.summary}"
            if strongest
            else "none standing"
        ),
        lineage=store.ancestors(idea.idea_id),
        board_independence=board_independence(reviews),
    )


def _render_top(item: TopIdea) -> dict[str, Any]:
    return {
        "idea_id": item.idea.idea_id,
        "status": str(item.idea.status),
        "quality_tier": str(item.idea.quality_tier),
        "research_question": item.research_question,
        "why_it_matters": item.why_it_matters,
        "closest_prior_work": item.closest_prior_work,
        "falsifier": item.falsifier,
        "limitations": list(item.limitations),
        "next_action": item.next_action,
        "evidence": list(item.evidence),
        "review_consensus": item.review_consensus,
        "strongest_objection": item.strongest_objection,
        "lineage": list(item.lineage),
        "board_independence": item.board_independence,
        # Repeated on every idea rather than stated once at the top, because a
        # reader who skips to one entry must not miss it.
        "independence_note": (
            "every review of this idea came from one model; that is not "
            "independent review"
            if item.board_independence <= 1
            else f"{item.board_independence} distinct reviewer models"
        ),
    }


def render(payload: dict[str, Any]) -> str:
    """The digest as a person reads it. Deterministic, from the payload."""

    counts = payload.get("counts", {})
    lines = [
        f"# Digest {payload.get('digest_id', '')}",
        "",
        "> Produced autonomously by Research OS. No human has evaluated any of it.",
        f"> {payload.get('period_start')} to {payload.get('period_end')}",
        "",
        "## Where the portfolio is",
        "",
    ]
    for key in sorted(counts):
        lines.append(f"- {key.replace('_', ' ')}: {counts[key]}")
    lines.append("")
    diversity = payload.get("diversity", {})
    if diversity:
        lines.append("## Diversity, by how ideas would be settled")
        lines.append("")
        for key in sorted(diversity):
            lines.append(f"- {key}: {diversity[key]}")
        lines.append("")
    for name, items in sorted(payload.get("sections", {}).items()):
        if not items:
            continue
        lines.append(f"## {name.replace('_', ' ').capitalize()}")
        lines.append("")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    top = payload.get("top_ideas", [])
    lines.append("## Worth your attention")
    lines.append("")
    if not top:
        lines.append("Nothing has reached a tier worth surfacing yet.")
        lines.append("")
    for entry in top:
        lines.append(
            f"### `{entry['idea_id']}` — {entry['status']} "
            f"(tier reached: {entry['quality_tier']})"
        )
        lines.append("")
        lines.append(f"- question: {entry['research_question']}")
        lines.append(f"- why it matters: {entry['why_it_matters'] or '(not stated)'}")
        lines.append(
            f"- closest prior work: {entry['closest_prior_work'] or '(not established)'}"
        )
        lines.append(f"- falsifier: {entry['falsifier'] or '(none stated)'}")
        lines.append(f"- review consensus: {entry['review_consensus']}")
        lines.append(f"- independence: {entry['independence_note']}")
        lines.append(f"- strongest objection: {entry['strongest_objection']}")
        for item in entry.get("limitations", []):
            lines.append(f"- limitation: {item}")
        for item in entry.get("evidence", []):
            lines.append(f"- evidence: {item}")
        if entry.get("lineage"):
            lines.append("- lineage: " + " <- ".join(entry["lineage"]))
        lines.append(f"- next: {entry['next_action'] or '(none recorded)'}")
        lines.append("")
    return "\n".join(lines) + "\n"


__all__ = ["MAX_TOP_IDEAS", "TopIdea", "diversify", "pareto_front", "produce", "render"]
