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

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from research_os.runtime.actions.base import (
    EXCERPT_KEY,
    SEMANTIC_KEY,
    ActionOutcome,
    bounded_excerpt,
)
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import MAX_EXCERPT_CHARS
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.policy import ActionKind
from research_os.runtime.prompts import FRONTIER, SCIENTIFIC_REVIEWER
from research_os.runtime.sciencecontext import noncanonical_science

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
    # **Only the budget is caught here.** A `RoutingError` -- which is what a
    # provider that did not answer now raises -- is deliberately allowed to
    # propagate out of this handler.
    #
    # Catching it looked careful and was the opposite. It converted an outage
    # into an `ActionOutcome`, and an `ActionOutcome` carries a failure class
    # and nothing else: not the breaker's `cooldown_until`, not whether an
    # invocation happened. Both are what the queue needs to schedule the
    # retry, so every provider failure that came through this door was
    # rescheduled by the linear backoff alone and charged an attempt even when
    # routing had refused before calling anything -- the 2026-09-19 arithmetic
    # exactly, on a second path.
    #
    # A budget refusal is genuinely different and stays: it is a policy answer
    # rather than a malfunction, no amount of waiting changes it, and the
    # honest terminal state is BUDGET_EXHAUSTED.
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            f"the scientific reviewer did not run: {exc}",
            failure_class=FailureClass.BUDGET_EXHAUSTED,
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
            data={
                **payload,
                "recommendation": "DONE_FOR_NOW",
                "ranked_actions": [],
                # Keyed like every other exit. `decisions` is empty here on
                # purpose rather than by omission: with nothing outstanding
                # there is nothing for a proposal to cover, so what is in front
                # of the researcher cannot change this answer.
                SEMANTIC_KEY: assessment_identity(
                    frontier, recommendation="DONE_FOR_NOW", actions=()
                ),
            },
            artifacts=(ref,),
        )

    depth = context.store.lineage_depth(state["run_id"])
    ceiling = context.config.settings.max_cycles_per_objective
    # What has already been done, and what has already been asked.
    #
    # The same three labelled categories the planner gets, for the reason
    # `frontier@2` records: @1 saw the capsule frontier and this cycle's
    # previous result and nothing else, so its highest-ranked candidate -- audit
    # whether the outstanding proposals already cover the open questions -- was
    # an action it had no way to perform, and it said so in the rationale it
    # recorded as a finding. Proposals now arrive with their items and the
    # capsule objects each item addresses, so coverage is a set intersection
    # rather than a request for file access.
    science = noncanonical_science(
        context.store,
        project_id=str(state["project_id"]),
        artifacts=context.artifacts,
        repo_path=str(state.get("repo_path") or "") or None,
        # Not its own previous assessments.
        #
        # A review pointed out what showing them does: the block is labelled
        # "work this runtime has already finished", so cycle N+1's frontier
        # reads cycle N's recommendation and rationale as an established
        # finding -- and with the identity fix keeping the *first* occurrence's
        # wording, one early answer would be reinforced indefinitely. The role
        # whose answer decides whether more money is spent and whether the
        # system stops for a person is the last one that should be anchored on
        # what it said last time. The planner passes nothing and sees them.
        exclude_finding_actions=(str(ActionKind.ASSESS_FRONTIER),),
    )
    decisions = decision_context_digest(science)
    prompt = FRONTIER.render(
        fields={
            "objective": state["objective"],
            "cycles_used": str(depth + 1),
            "cycles_remaining": str(max(0, ceiling - depth - 1)),
            "noncanonical_census": science.census(),
        },
        blocks={
            "frontier": [json.dumps(payload, indent=2, sort_keys=True)],
            "completed_findings": [
                json.dumps(entry, indent=2, sort_keys=True)
                for entry in science.findings
            ],
            # Compact. See the same block in `graphs.cycle.plan_one_action`:
            # `indent=2` cost about 1 600 characters across a twelve-item
            # proposal and nine of the twelve items with it.
            "outstanding_proposals": [
                json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
                for entry in science.proposals
            ],
            "preregistered_designs": [
                json.dumps(entry, indent=2, sort_keys=True, default=str)
                for entry in science.preregistrations
            ],
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
    except BudgetExhaustedError as exc:
        # The deterministic frontier is still a usable answer. Ranking is an
        # improvement on it, not a prerequisite, so an exhausted budget
        # degrades to "there is work outstanding" rather than failing the
        # cycle.
        #
        # **`RoutingError` used to be caught here too, and that was the worst
        # instance of it.** This branch returns `succeeded` with
        # `recommendation: START_NEXT_CYCLE`, so a provider outage produced a
        # cycle that concluded DONE_FOR_NOW, recorded a citable finding, was
        # offered to the next capsule change as parked, and opened a successor
        # -- spending one of the objective's twelve cycles on work that never
        # ran. Every property the provider-failure closure establishes,
        # violated through one `except` clause.
        #
        # A budget refusal keeps the degradation because it is a different
        # kind of thing: the researcher said stop spending, waiting will not
        # change it, and the deterministic frontier is the honest answer to
        # give. An outage is temporary, and the honest answer is to wait.
        #
        # **Keyed, and it was not.** Two reviews found the same hole
        # independently: the identity fix was applied to the success path only,
        # so a degraded assessment fell back to the content digest -- and its
        # summary embeds `BudgetExhaustedError`'s text, which carries the run
        # id and a moving dollar figure. The summary was therefore *guaranteed*
        # unique per cycle, so a provider outage or a tight budget minted one
        # new citable finding every cycle and twenty of them filled the
        # planner's window. The defect this mechanism exists to stop, on the
        # path most likely to repeat.
        return ActionOutcome.succeeded(
            f"ranking unavailable ({exc}); the deterministic frontier stands",
            data={
                **payload,
                "recommendation": "START_NEXT_CYCLE",
                "ranked_actions": [],
                SEMANTIC_KEY: assessment_identity(
                    frontier,
                    recommendation="START_NEXT_CYCLE",
                    actions=(),
                    decisions=decisions,
                    degraded=type(exc).__name__,
                ),
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
                SEMANTIC_KEY: assessment_identity(
                    frontier,
                    recommendation="START_NEXT_CYCLE",
                    actions=(),
                    decisions=decisions,
                    degraded="unusable_response",
                ),
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
            SEMANTIC_KEY: assessment_identity(
                frontier,
                recommendation=recommendation,
                actions=actions,
                decisions=decisions,
            ),
        },
        artifacts=(ref, ranked_ref),
    )


def ranked_targets(
    actions: Sequence[Mapping[str, Any]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Each ranked candidate as ``(action, sorted addresses)``, in rank order.

    One normalisation, used by both readers of ``ranked_actions``. It was two,
    and a review pointed out that both iterated a model-supplied ``addresses``
    with no guard -- nothing validates the provider's structured output against
    ``_FRONTIER_SCHEMA``, so ``"addresses": null`` raises ``TypeError``, which
    is not in the nets the graph catches. One function, one guard, and the
    identity and the excerpt cannot drift from each other.
    """

    normalised: list[tuple[str, tuple[str, ...]]] = []
    for candidate in actions:
        if not isinstance(candidate, Mapping):
            continue
        raw = candidate.get("addresses")
        targets = raw if isinstance(raw, list | tuple) else ()
        normalised.append(
            (
                str(candidate.get("action") or "").strip(),
                tuple(
                    sorted(
                        {
                            str(target).strip()
                            for target in targets
                            if str(target).strip()
                        }
                    )
                ),
            )
        )
    return normalised


def decision_context_digest(science: Any) -> str:
    """What is in front of the researcher, as one stable hash.

    **Why the frontier digest is not enough on its own.** A security review
    pointed out that ``frontier_digest`` covers capsule-derived identifiers,
    the capsule cannot move without a human promotion, and so it is effectively
    *constant* for a whole autonomous session -- while this release just gave
    the role three further categories to assess over. Two assessments made
    before and after a researcher declined an item, or before and after a
    proposal's basis went stale, would have collapsed onto one finding whose
    stored rationale described inputs it was not computed from.

    So the decisions already in front of the person are part of the identity:
    per readable proposal, its id, the items nobody has decided yet, and the
    *class* of its basis. The class rather than the sentence, because a longer
    list of changed objects does not change the conclusion that a stale
    proposal is not a live decision.

    **And the findings deliberately are not.** Including them would put the
    churn straight back: every cycle records a finding, so every cycle would
    change the identity, and a repeated assessment over unchanged canonical
    state would mint a new citable object again -- which is the defect this
    whole mechanism exists to stop. What is in the key is exactly what moves
    only when a *person* acts or the science does.
    """

    entries = [
        {
            "proposal_id": str(row.get("proposal_id") or ""),
            # Unknown coverage is its own state, and it changes the reasoning.
            "unreadable": bool(row.get("items_unavailable")),
            "undecided": sorted(
                str(item.get("item_id") or "")
                for item in (row.get("items") or ())
                if isinstance(item, Mapping) and not item.get("decided_by_human")
            ),
            "basis": str(row.get("basis") or "").split(":", 1)[0],
        }
        for row in getattr(science, "proposals", ())
    ]
    entries.sort(key=lambda entry: entry["proposal_id"])
    material = json.dumps(
        {"v": 1, "proposals": entries}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode()).hexdigest()


def assessment_identity(
    frontier: Any,
    *,
    recommendation: str,
    actions: Sequence[Mapping[str, Any]],
    decisions: str = "",
    degraded: str = "",
) -> str:
    """What makes two frontier assessments the same assessment.

    **The defect this closes.** A finding is identified by a digest over its
    content, which for this handler includes the model's rationale and the
    content hash of the artifact holding it. Reach the identical conclusion
    over the identical scientific state twice and the wording differs, so the
    digest differs, so a second citable identifier is minted for one
    observation. Twenty repetitions fill all twelve slots of the planner's
    finding window and push the project's real findings out of it -- and
    nothing about the project has changed. That is operational repetition
    wearing scientific progress as a costume, which is the failure this
    release exists to stop, one layer below where it was first found.

    **What is material, therefore.** Three things, and they are the three a
    researcher would name if asked whether two assessments say the same thing:

    - the canonical state assessed. ``frontier_digest`` over the unresolved
      identifiers, sorted -- so a reordered input is the same input, and a
      frontier that gained a hypothesis is not;
    - the decisions already in front of the researcher, through
      :func:`decision_context_digest`. Added after a review observed that the
      frontier digest alone is constant for a whole autonomous session, so an
      assessment made before a person declined an item and one made after it
      would otherwise have been one observation;
    - the conclusion. The recommendation, which is the field the runtime acts
      on;
    - what was ranked, and about what. Each candidate as ``action`` plus its
      sorted ``addresses``, in rank order, because a ranking that puts a
      different action first is a different ranking.

    **What is deliberately not material,** each for a stated reason:

    - the rationales, and every other piece of prose. Two assessments that
      reach one conclusion over one state, phrased differently, are one
      assessment; treating them as two is precisely the defect;
    - importance, information gain, feasibility and cost. They are the
      *reasoning* toward the recommendation, not the recommendation, and they
      move between equivalent restatements;
    - the artifact ids, timestamps, run id, cycle index and work id. Which
      cycle noticed something is not part of what was noticed --
      :attr:`research_os.runtime.findings.RuntimeFinding.digest` gives the
      same reason for excluding the same fields.

    Rank order *is* material, which is a choice worth stating: two assessments
    recommending the same thing having ranked the same candidates in a
    different order are recorded as different observations. That is the
    conservative direction. Collapsing them would mean a genuine change of
    mind about what to do first could be silently deduplicated away.
    """

    from research_os.runtime.graphs.cycle import frontier_digest

    material = json.dumps(
        {
            "v": 2,
            "frontier": frontier_digest(frontier),
            "decisions": decisions,
            "recommendation": recommendation,
            "ranked": [
                {"action": action, "addresses": list(targets)}
                for action, targets in ranked_targets(actions)
            ],
            # Empty for an assessment that actually ran. A degraded one is a
            # different observation from a completed one that happened to
            # reach the same words, and the *class* of degradation is the part
            # that is stable: the message carries a run id and a moving dollar
            # figure, so hashing it would make every outage a new finding,
            # which is the churn in a new costume.
            **({"degraded": degraded} if degraded else {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"assess_frontier:v2:{hashlib.sha256(material.encode()).hexdigest()}"


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
    # Normalised once, by the same function the identity uses, so the excerpt
    # and the key cannot disagree about what was ranked -- and so the
    # unguarded iteration over a model-supplied `addresses` exists in one
    # place rather than two. Nothing validates the provider's structured
    # output against `_FRONTIER_SCHEMA`, so `"addresses": null` is reachable.
    targets = dict(enumerate(ranked_targets(actions)))
    candidates = [item for item in actions if isinstance(item, Mapping)]
    for position, candidate in enumerate(candidates, start=1):
        action, addressed = targets[position - 1]
        action = action or "unnamed"
        addresses = ", ".join(addressed)
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
