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
from collections.abc import Mapping
from typing import Any

from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
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
        },
        artifacts=(ref,),
    )


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
        },
        artifacts=(ref, ranked_ref),
    )
