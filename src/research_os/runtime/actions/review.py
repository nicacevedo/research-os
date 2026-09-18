"""Scientific review and frontier ranking.

Two handlers with one thing in common: both produce *judgement recorded as
provenance*, and neither changes any scientific state.

**Scientific review is not code review.** Code review asks whether the diff does
what the task said; scientific review asks whether the conclusion follows from
the evidence. They are separate roles, separate prompts, separate independence
groups, and separate actions -- because a system that runs one and reports the
other has told the researcher their science was checked when their formatting
was.

**A reviewer cannot approve.** Its verdict is advice. Acceptance requires a
qualifying human Review, recorded through ``researchctl review``, and no
verdict here moves a Claim.

**Frontier ranking can say "stop".** The ranking prompt's recommendation enum
includes ``DONE_FOR_NOW``, and the deterministic frontier can be empty. A
ranker that always finds something worth doing next is a ranker that never lets
the system stop, which is the failure mode that turns bounded cycles into an
unbounded bill.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from research_os.runtime.actions.base import (
    EXCERPT_KEY,
    ActionOutcome,
    bounded_excerpt,
)
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import MAX_EXCERPT_CHARS
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.prompts import FRONTIER, SCIENTIFIC_REVIEWER
from research_os.runtime.routing import RoutingError

LOG = logging.getLogger("research_os.runtime.actions.review")

VALID_RECOMMENDATIONS = frozenset(
    {"START_NEXT_CYCLE", "WAIT_EXTERNAL", "WAIT_HUMAN", "BLOCKED", "DONE_FOR_NOW"}
)


def review_science(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Review what this cycle established, on a frozen packet.

    The packet is assembled here from the plan and the previous action's
    structured result. The reviewer receives no conversational history from
    whatever produced the work -- invariant 9, and the reason review runs as its
    own model call rather than as a follow-up turn.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    subject = str(
        plan.get("parameters", {}).get("subject")
        or ", ".join(str(item) for item in plan.get("addresses", ()))
        or state["objective"]
    )
    if not previous:
        return ActionOutcome.succeeded(
            "nothing produced in this cycle to review", data={"reviewed": False}
        )

    prompt = SCIENTIFIC_REVIEWER.render(
        fields={"claim_or_hypothesis": subject},
        blocks={
            "specification": [json.dumps(dict(plan), indent=2, sort_keys=True)],
            "results": [json.dumps(previous, indent=2, sort_keys=True)],
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=SCIENTIFIC_REVIEWER.role,
                capability=SCIENTIFIC_REVIEWER.capability,
                prompt=prompt,
                prompt_version=SCIENTIFIC_REVIEWER.identity,
                criticality=SCIENTIFIC_REVIEWER.criticality,
                independence=SCIENTIFIC_REVIEWER.independence,
                independence_group=f"review:{state['run_id']}:{state['cycle_index']}",
                json_schema=SCIENTIFIC_REVIEWER.output_schema,
            )
        )
    except (BudgetExhaustedError, RoutingError) as exc:
        return ActionOutcome.failed(
            f"the scientific reviewer did not run: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.failed(
            f"the scientific reviewer returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    verdict = dict(response.structured)
    ref = context.artifacts.put_text(
        json.dumps(verdict, indent=2, sort_keys=True),
        media_type="application/json",
        role="scientific_review",
        producer=f"{response.provider}:{SCIENTIFIC_REVIEWER.identity}",
    )
    return ActionOutcome.succeeded(
        f"scientific review: {verdict.get('verdict')} / "
        f"{verdict.get('claim_support')} ({response.independence})",
        data={
            "reviewed": True,
            "subject": subject,
            "verdict": verdict.get("verdict"),
            "claim_support": verdict.get("claim_support"),
            "alternative_explanations": verdict.get("alternative_explanations", []),
            "evidence_gaps": verdict.get("evidence_gaps", []),
            "overclaiming": verdict.get("overclaiming", []),
            "required_changes": verdict.get("required_changes", []),
            "independence": str(response.independence),
            "independence_note": response.independence_note,
            # Stated in the record, not only in the prompt.
            "approves_nothing": True,
            # The verdict word is not the review. What a later proposal has to
            # weigh is which alternatives the reviewer raised and what it said
            # was overclaimed, and neither survives "verdict: REVISE".
            EXCERPT_KEY: bounded_excerpt(
                _review_lines(verdict), limit=MAX_EXCERPT_CHARS
            ),
        },
        artifacts=(ref,),
    )


def _review_lines(verdict: Mapping[str, Any]) -> list[str]:
    """The reviewer's substantive findings, labelled by what kind each is.

    Labelled because the four lists mean different things and a flat
    concatenation of them reads as one undifferentiated complaint: an
    alternative explanation is a rival account, an overclaim is a sentence that
    outran its evidence, and a required change is neither.
    """

    lines: list[str] = []
    for label, key in (
        ("alternative", "alternative_explanations"),
        ("evidence gap", "evidence_gaps"),
        ("overclaiming", "overclaiming"),
        ("required change", "required_changes"),
    ):
        for entry in verdict.get(key, ()) or ():
            text = str(entry).strip()
            if text:
                lines.append(f"[{label}] {text}")
    return lines


def assess_frontier_ranked(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Rank what to do next, and recommend whether another cycle is warranted.

    The frontier itself is recomputed deterministically first, so the ranking is
    over facts rather than over a remembered summary. An empty frontier short
    circuits: there is nothing to rank, the answer is ``DONE_FOR_NOW``, and
    spending a model call to be told so would be the waste §20 warns about.
    """

    frontier = context.kernel.frontier()
    payload: dict[str, Any] = {
        "summary": frontier.summary(),
        "empty": frontier.empty,
        "open_questions": list(frontier.open_questions),
        "actionable_hypotheses": list(frontier.actionable_hypotheses),
        "hypotheses_without_tests": list(frontier.hypotheses_without_tests),
        "claims_awaiting_review": list(frontier.claims_awaiting_review),
        "claims_with_stale_review": list(frontier.claims_with_stale_review),
        "contested_claims": list(frontier.contested_claims),
        "evidence_gaps": list(frontier.evidence_gaps),
        "pending_experiments": list(frontier.pending_experiments),
    }
    ref = context.artifacts.put_text(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        role="frontier",
        producer="kernel.frontier",
    )
    if frontier.empty:
        return ActionOutcome.succeeded(
            "the frontier is empty",
            data={**payload, "recommendation": "DONE_FOR_NOW", "ranked_actions": []},
            artifacts=(ref,),
        )

    depth = context.store.lineage_depth(state["run_id"])
    ceiling = context.config.settings.max_cycles_per_objective
    prompt = FRONTIER.render(
        fields={
            "objective": state["objective"],
            "cycles_used": str(depth + 1),
            "cycles_remaining": str(max(0, ceiling - depth - 1)),
        },
        blocks={
            "frontier": [json.dumps(payload, indent=2, sort_keys=True)],
            "recent_outcomes": [
                json.dumps(
                    dict(state.get("action_result", {}).get("data") or {}),
                    indent=2,
                    sort_keys=True,
                )
            ],
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=FRONTIER.role,
                capability=FRONTIER.capability,
                prompt=prompt,
                prompt_version=FRONTIER.identity,
                criticality=FRONTIER.criticality,
                independence=FRONTIER.independence,
                independence_group=f"frontier:{state['run_id']}",
                json_schema=FRONTIER.output_schema,
            )
        )
    except (BudgetExhaustedError, RoutingError) as exc:
        # The deterministic frontier is still a usable answer. Ranking is an
        # improvement on it, not a prerequisite, so a missing provider degrades
        # to "there is work outstanding" rather than failing the cycle.
        return ActionOutcome.succeeded(
            f"ranking unavailable ({exc}); the deterministic frontier stands",
            data={
                **payload,
                "recommendation": "START_NEXT_CYCLE",
                "ranked_actions": [],
            },
            artifacts=(ref,),
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.succeeded(
            f"ranking returned nothing usable ({response.error}); the "
            f"deterministic frontier stands",
            data={
                **payload,
                "recommendation": "START_NEXT_CYCLE",
                "ranked_actions": [],
            },
            artifacts=(ref,),
        )

    ranking = dict(response.structured)
    recommendation = str(ranking.get("recommendation") or "")
    if recommendation not in VALID_RECOMMENDATIONS:
        # A recommendation this build does not understand must not be acted on.
        # Falling back to "there is work outstanding" is the conservative
        # reading, and the control plane's own bounds still apply to it.
        LOG.warning("frontier returned an unknown recommendation: %r", recommendation)
        recommendation = "START_NEXT_CYCLE"

    ranked_ref = context.artifacts.put_text(
        json.dumps(ranking, indent=2, sort_keys=True),
        media_type="application/json",
        role="frontier_ranking",
        producer=f"{response.provider}:{FRONTIER.identity}",
    )
    actions = list(ranking.get("ranked_actions", []))[:10]
    return ActionOutcome.succeeded(
        f"{len(actions)} ranked candidate(s); recommends {recommendation}",
        data={
            **payload,
            "recommendation": recommendation,
            "recommendation_rationale": ranking.get("recommendation_rationale", ""),
            "ranked_actions": actions,
            "cycles_used": depth + 1,
            "cycles_remaining": max(0, ceiling - depth - 1),
            # **The gap an acceptance run found.** Excerpts were added so that a
            # later proposal could weigh what a finding actually said rather
            # than its one-line summary -- and this handler, the one that ranks
            # what to do next, was left out. A real cycle produced
            # `7 ranked candidate(s); recommends WAIT_HUMAN` and nothing else:
            # not which seven, not why, not why waiting was the answer. That is
            # the same blindness the release set out to remove, in the finding
            # whose content bears most directly on the next decision.
            EXCERPT_KEY: bounded_excerpt(
                _ranking_lines(recommendation, ranking, actions),
                limit=MAX_EXCERPT_CHARS,
            ),
        },
        artifacts=(ref, ranked_ref),
    )


def _ranking_lines(
    recommendation: str,
    ranking: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
) -> list[str]:
    """The ranking's substance, labelled by what each line is.

    Producer-authored: this handler knows that the recommendation and its
    reason come first, and that a candidate is only useful to a later reader
    with the action it names and the reason it was ranked where it was.
    """

    lines = [f"recommendation: {recommendation}"]
    rationale = str(ranking.get("recommendation_rationale") or "").strip()
    if rationale:
        lines.append(f"why: {rationale}")
    for position, candidate in enumerate(actions, start=1):
        if not isinstance(candidate, Mapping):
            continue
        action = str(candidate.get("action") or "").strip() or "unnamed"
        addresses = ", ".join(
            str(item) for item in candidate.get("addresses", ()) if item
        )
        importance = str(candidate.get("importance") or "").strip()
        reason = str(candidate.get("rationale") or "").strip()
        parts = [f"candidate {position}: {action}"]
        if addresses:
            parts.append(f"addresses {addresses}")
        if importance:
            parts.append(f"importance {importance}")
        if reason:
            parts.append(reason)
        lines.append(" -- ".join(parts))
    return lines
