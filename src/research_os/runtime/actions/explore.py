"""Independent co-exploration: a blind branch, a seeded branch, and a skeptic.

Three roles, and what separates them is *information*, not persona. A system
that asks one model to "act as a skeptic" and then hands it everything the
optimist saw has produced a second opinion from the same evidence and the same
framing, which is worth much less than it looks.

So the separation is enforced where it cannot be forgotten:

**The blind explorer is not told the project's preferred hypothesis.** Its
prompt template declares only the question and a description of the data, and
:meth:`PromptTemplate.render` refuses any field it did not declare. Passing it
the frontier is an error rather than a quiet loss of independence. This is the
branch whose value is entirely in what it does not know.

**The seeded explorer is told everything.** The contrast is the point: where the
two branches agree, the agreement is not an artefact of anchoring; where they
disagree, there is something to test.

**The skeptic sees the proposals and not the reasoning that produced them.**
Structured output only -- statements, mechanisms, predictions, falsifiers -- and
never the producing model's scratch text. A reviewer that inherits the
producer's chain of thought inherits its blind spots, which is exactly what
independent review exists to avoid.

**Independence is recorded per branch.** Each branch runs in the same
independence group, so the router routes each away from the families that have
already answered and records what separation it actually achieved. On a machine
with one provider family that is an acknowledged degradation rather than a false
claim, and it appears in the run report.

Nothing here promotes anything. Every proposal is a candidate; a Hypothesis
object in a capsule is written by a person through the v1 proposal layer, which
already requires that.
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
from research_os.runtime.interfaces import ArtifactRef, ModelRequest
from research_os.runtime.policy import ActionKind
from research_os.runtime.prompts import (
    BLIND_EXPLORER,
    DERIVER,
    SEEDED_EXPLORER,
    SKEPTIC,
    PromptTemplate,
)
from research_os.runtime.routing import RoutingError
from research_os.runtime.sciencecontext import MAX_PLANNER_FINDINGS

LOG = logging.getLogger("research_os.runtime.actions.explore")

#: How many proposals from any one branch are carried forward. A branch that
#: returns forty hypotheses has not been more helpful than one that returned
#: four; it has moved the filtering to whoever reads the report.
MAX_PROPOSALS_PER_BRANCH = 6

#: The most derivation steps carried into the structured result.
#:
#: The full document is always in the artifact. This bounds what reaches a
#: checkpoint and an excerpt, and a derivation needing more than forty steps is
#: one whose excerpt was never going to be the thing a person checked.
MAX_DERIVATION_STEPS = 40

#: The most prior derivations of one target shown to a new derivation.
MAX_PRIOR_DERIVATIONS = 3

#: Outcomes this build understands. An unknown one degrades to INCOMPLETE.
VALID_DERIVATION_OUTCOMES = frozenset(
    {
        "DERIVED",
        "REFUTED_BY_COUNTEREXAMPLE",
        "NOT_DERIVABLE_AS_STATED",
        "INCOMPLETE",
    }
)

#: Characters of scientific substance a branch's finding carries forward.
#:
#: Equal to :data:`research_os.runtime.findings.MAX_EXCERPT_CHARS`, imported
#: rather than restated so the two cannot drift: a handler that authored more
#: than the store keeps would produce an excerpt whose stored form differs from
#: the one it wrote, and the digest would then be over text nobody chose.
EXCERPT_LIMIT = MAX_EXCERPT_CHARS


def _alternative_lines(proposals: Sequence[Mapping[str, Any]]) -> list[str]:
    """The scientifically load-bearing line of each proposal or alternative.

    ``statement`` and nothing else, by default: it is the assertion, and the
    mechanism, predictions and falsifiers around it are how it would be
    *tested* rather than what it says. A reader deciding whether an alternative
    bears on a hypothesis needs the assertion; a reader deciding how to test it
    opens the artifact, whose id travels with the finding.

    ``novelty`` rides along because it is one word and it is the field that
    distinguishes "this is a known result" from "this is a new claim" -- which
    is the distinction the thesis programme's whole novelty gate turns on.
    """

    lines: list[str] = []
    for index, proposal in enumerate(proposals, start=1):
        statement = str(proposal.get("statement") or "").strip()
        if not statement:
            continue
        novelty = str(proposal.get("novelty") or "").strip()
        suffix = f" [novelty: {novelty}]" if novelty else ""
        lines.append(f"{index}. {statement}{suffix}")
    return lines


def _group(state: Mapping[str, Any]) -> str:
    return f"explore:{state['run_id']}:{state['cycle_index']}"


def _question_for(state: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    """The question this exploration addresses, from the plan or the frontier.

    Prefers the plan's own ``addresses``, because the planner chose it. Falls
    back to the first open question, and says so rather than inventing one.
    """

    addressed = [
        str(item) for item in plan.get("addresses", ()) if str(item).startswith("Q-")
    ]
    frontier = state.get("frontier", {})
    if not addressed:
        addressed = [str(item) for item in frontier.get("open_questions", ())][:1]
    if not addressed:
        return state["objective"]
    kernel_ids = ", ".join(addressed)
    return f"{state['objective']} (addressing {kernel_ids})"


def _statements(context: CycleContext, object_ids: tuple[str, ...]) -> list[str]:
    """Render the scientific statements of some capsule objects, for a prompt.

    Read through the kernel adapter, which is the only path to capsule content,
    and returned as plain strings for the serializer to fence.
    """

    lines: list[str] = []
    for object_id in object_ids:
        try:
            obj = context.kernel.object(object_id)
        except Exception as exc:  # noqa: BLE001 - a missing object is not fatal here
            # WARNING, not DEBUG. An unreadable object silently removes context
            # from the *seeded* branch, which degrades it toward the blind one --
            # and the difference between those two branches is the entire
            # scientific value of running both.
            LOG.warning(
                "could not read %s for the seeded branch, which will run without "
                "it: %s",
                object_id,
                exc,
            )
            continue
        statement = getattr(obj, "statement", "") or getattr(obj, "title", "")
        lines.append(f"{object_id}: {statement}")
    return lines


def _run_branch(
    template: PromptTemplate,
    *,
    state: Mapping[str, Any],
    context: CycleContext,
    fields: dict[str, str],
    blocks: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], str, str, ArtifactRef | None]:
    """Run one exploration branch.

    Returns ``(proposals, independence, note, artifact)``. A branch that fails
    returns no proposals and says why: one unavailable provider must not lose
    the other branches' work, and a co-exploration that silently became a
    single-branch exploration is the failure mode this avoids.
    """

    prompt = template.render(fields=fields, blocks=blocks or {})
    try:
        response = context.models.complete(
            ModelRequest(
                role=template.role,
                capability=template.capability,
                prompt=prompt,
                prompt_version=template.identity,
                criticality=template.criticality,
                independence=template.independence,
                independence_group=_group(state),
                json_schema=template.output_schema,
            )
        )
    except (BudgetExhaustedError, RoutingError) as exc:
        return [], "none", f"{template.name} did not run: {exc}", None

    if not response.ok or response.structured is None:
        return (
            [],
            str(response.independence),
            f"{template.name} returned nothing usable: {response.error}",
            None,
        )

    proposals = list(response.structured.get("proposals", []))[
        :MAX_PROPOSALS_PER_BRANCH
    ]
    artifact = context.artifacts.put_text(
        json.dumps(dict(response.structured), indent=2, sort_keys=True),
        media_type="application/json",
        role=f"proposals:{template.name}",
        producer=f"{response.provider}:{template.identity}",
    )
    return (
        proposals,
        str(response.independence),
        response.independence_note,
        artifact,
    )


def propose_hypotheses(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Generate competing hypotheses from deliberately separated branches.

    Blind first, then seeded, then the skeptic over both. Order matters for the
    independence accounting: the blind branch answers before any family has been
    used in this group, so the seeded branch and the skeptic can be routed away
    from it.
    """

    question = _question_for(state, plan)
    frontier = state.get("frontier", {})
    data_description = str(
        plan.get("parameters", {}).get("data_description")
        or "the project's own repository and capsule; no external dataset declared"
    )

    artifacts: list[ArtifactRef] = []
    branches: dict[str, dict[str, Any]] = {}

    blind, blind_independence, blind_note, blind_artifact = _run_branch(
        BLIND_EXPLORER,
        state=state,
        context=context,
        # Only these two fields exist on this template. That is the blindness.
        fields={"question": question, "data_description": data_description},
    )
    branches["blind"] = {
        "proposals": blind,
        "independence": blind_independence,
        "note": blind_note,
    }
    if blind_artifact:
        artifacts.append(blind_artifact)

    hypothesis_ids = tuple(frontier.get("actionable_hypotheses", ()))
    seeded, seeded_independence, seeded_note, seeded_artifact = _run_branch(
        SEEDED_EXPLORER,
        state=state,
        context=context,
        fields={"question": question, "data_description": data_description},
        blocks={
            "frontier": [json.dumps(frontier, indent=2, sort_keys=True)],
            "current_hypotheses": _statements(context, hypothesis_ids)
            or ["(the capsule records no actionable hypothesis)"],
        },
    )
    branches["seeded"] = {
        "proposals": seeded,
        "independence": seeded_independence,
        "note": seeded_note,
    }
    if seeded_artifact:
        artifacts.append(seeded_artifact)

    combined = blind + seeded
    if not combined:
        return ActionOutcome.failed(
            "no branch produced a usable proposal: "
            + "; ".join(branch["note"] for branch in branches.values()),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            data={"branches": branches},
            artifacts=tuple(artifacts),
        )

    critique, critic_independence, critic_note, critic_artifact = _run_branch(
        SKEPTIC,
        state=state,
        context=context,
        fields={"question": question},
        # The proposals only. Never the producing model's reasoning.
        blocks={"proposals": [json.dumps(combined, indent=2, sort_keys=True)]},
    )
    branches["skeptic"] = {
        "proposals": critique,
        "independence": critic_independence,
        "note": critic_note,
    }
    if critic_artifact:
        artifacts.append(critic_artifact)

    unfalsifiable = [
        proposal.get("statement", "(no statement)")
        for proposal in combined
        if not proposal.get("falsifiers")
    ]

    return ActionOutcome.succeeded(
        f"{len(blind)} blind + {len(seeded)} seeded proposals, "
        f"{len(critique)} alternatives from the skeptic",
        data={
            "question": question,
            "branches": branches,
            "proposal_count": len(combined),
            "unfalsifiable": unfalsifiable,
            "independence": {
                name: branch["independence"] for name, branch in branches.items()
            },
            "degraded": [
                name
                for name, branch in branches.items()
                if str(branch["note"]).startswith("DEGRADED")
            ],
            EXCERPT_KEY: bounded_excerpt(
                _alternative_lines(combined), limit=EXCERPT_LIMIT
            ),
        },
        artifacts=tuple(artifacts),
    )


def critique_hypotheses(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Attack what the capsule currently believes, without proposing first.

    Separate from :func:`propose_hypotheses` because a project sometimes wants
    only the critique -- the hypotheses already exist and the question is
    whether they survive -- and running the explorers to get there would spend
    two model calls to produce input that is already on disk.
    """

    question = _question_for(state, plan)
    frontier = state.get("frontier", {})
    targets = tuple(
        frontier.get("actionable_hypotheses", ())
        or frontier.get("contested_claims", ())
    )
    if not targets:
        return ActionOutcome.succeeded(
            "nothing on the frontier to critique", data={"targets": []}
        )

    critique, independence, note, artifact = _run_branch(
        SKEPTIC,
        state=state,
        context=context,
        fields={"question": question},
        blocks={"proposals": _statements(context, targets)},
    )
    if not critique and not note.startswith("DEGRADED"):
        return ActionOutcome.failed(
            f"the skeptic returned nothing usable: {note}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            artifacts=(artifact,) if artifact else (),
        )
    return ActionOutcome.succeeded(
        f"{len(critique)} alternative explanation(s) for {len(targets)} target(s)",
        data={
            "targets": list(targets),
            "alternatives": critique,
            "independence": independence,
            "independence_note": note,
            # What the summary above cannot say. Six alternatives counted is
            # not six alternatives read, and a proposal grounded in this
            # finding has to be able to tell which hypotheses they bear on.
            EXCERPT_KEY: bounded_excerpt(
                _alternative_lines(critique), limit=EXCERPT_LIMIT
            ),
        },
        artifacts=(artifact,) if artifact else (),
    )


def derive_mathematics(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Derive a mathematical proposition, or report that it does not go through.

    **Why this action exists at all.** Before it, the runtime's only verb for an
    unresolved hypothesis was ``design_experiment``. HYP-0002 of the thesis
    pilot is a biconditional about when an infimum is finite; the runtime
    designed six experiments for it, each a slightly different specification,
    none of which could have decided it. Routing away from ``design_experiment``
    only helps if there is somewhere to route *to*, and this is that place.

    **It establishes nothing.** The output is an artifact and a noncanonical
    finding, exactly like a critique or a literature audit. ``DERIVED`` is a
    model's report that a derivation went through, and it reaches the capsule
    only the way everything else does: a proposal, and a person. A runtime that
    could mark a proposition proved would be writing canonical scientific state,
    which is the one thing this system is built not to do.

    **A witness is not a proof.** The schema lets the deriver suggest a
    numerical check and *requires* it to state what that check would and would
    not show. The suggestion is carried in the result as a suggestion; nothing
    here dispatches an experiment, and nothing here treats a suggested
    computation as part of the derivation.
    """

    targets = tuple(
        str(item)
        for item in plan.get("addresses", ())
        if str(item).startswith(("HYP-", "Q-", "CLAIM-"))
    )
    if not targets:
        frontier = state.get("frontier", {})
        targets = tuple(
            str(item) for item in frontier.get("actionable_hypotheses", ())
        )[:1]
    if not targets:
        return ActionOutcome.succeeded(
            "nothing on the frontier to derive", data={"targets": []}
        )

    target = targets[0]
    proposition = "\n".join(_statements(context, (target,))) or target
    assumptions = _assumptions_for(context, target)

    prompt = DERIVER.render(
        fields={"target": target, "proposition": proposition},
        blocks={
            "assumptions": assumptions or ["(none recorded on this object)"],
            # Prior derivations from this project's own findings, so a second
            # cycle on the same target builds on the first rather than
            # restating it. Bounded, and empty on the first pass.
            "prior_derivations": _prior_derivations(context, state, target),
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=DERIVER.role,
                capability=DERIVER.capability,
                prompt=prompt,
                prompt_version=DERIVER.identity,
                criticality=DERIVER.criticality,
                independence=DERIVER.independence,
                independence_group=f"derive:{state['run_id']}:{target}",
                json_schema=DERIVER.output_schema,
            )
        )
    except (BudgetExhaustedError, RoutingError) as exc:
        return ActionOutcome.failed(
            f"the deriver did not run: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.failed(
            f"the deriver returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    derivation = dict(response.structured)
    outcome = str(derivation.get("outcome") or "INCOMPLETE")
    if outcome not in VALID_DERIVATION_OUTCOMES:
        # An outcome this build does not know must not be acted on, and
        # INCOMPLETE is the reading that claims least.
        LOG.warning("the deriver returned an unknown outcome: %r", outcome)
        outcome = "INCOMPLETE"
    steps = list(derivation.get("steps", []))[:MAX_DERIVATION_STEPS]
    ref = context.artifacts.put_text(
        json.dumps(derivation, indent=2, sort_keys=True, ensure_ascii=False),
        media_type="application/json",
        role=f"derivation:{target}",
        producer=f"{response.provider}:{DERIVER.identity}",
    )
    return ActionOutcome.succeeded(
        f"derivation of {target}: {outcome} in {len(steps)} step(s) "
        f"({response.independence})",
        data={
            "target": target,
            "outcome": outcome,
            "convention": derivation.get("convention", ""),
            "assumptions_used": derivation.get("assumptions_used", []),
            "result": derivation.get("result", ""),
            "residual_gaps": derivation.get("residual_gaps", []),
            "numerical_witness": derivation.get("numerical_witness", {}),
            "step_count": len(steps),
            "independence": str(response.independence),
            "independence_note": response.independence_note,
            # Stated in the record rather than only in the prompt, and it is
            # the same sentence `review_science` records for the same reason.
            "establishes_nothing": True,
            EXCERPT_KEY: bounded_excerpt(
                _derivation_lines(target, outcome, derivation, steps),
                limit=EXCERPT_LIMIT,
            ),
        },
        artifacts=(ref,),
    )


def _derivation_lines(
    target: str,
    outcome: str,
    derivation: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> list[str]:
    """The derivation as a reader would need to check it.

    The convention first, because a derivation read without its normalisation
    is a derivation of a different statement. Then the outcome, the result, the
    steps in order, and the gaps -- and the witness disclaimer last, in the
    deriver's own words, so that a proposal quoting this excerpt quotes the
    sentence saying what the numerics would not establish.
    """

    lines = [
        f"target: {target}",
        f"outcome: {outcome}",
        f"convention: {derivation.get('convention', '')}",
        f"result: {derivation.get('result', '')}",
    ]
    lines += [
        f"[step {index}] {step.get('claim', '')} -- because "
        f"{step.get('justification', '')}"
        for index, step in enumerate(steps, start=1)
    ]
    lines += [
        f"[residual gap] {gap}" for gap in derivation.get("residual_gaps", ()) or ()
    ]
    witness = derivation.get("numerical_witness") or {}
    if isinstance(witness, Mapping) and witness.get("suggested"):
        lines.append(
            f"[suggested numerical witness -- NOT a proof] "
            f"{witness.get('suggested')}; would show: "
            f"{witness.get('what_it_would_show', '')}"
        )
    return lines


def _assumptions_for(context: CycleContext, target: str) -> list[str]:
    """The statements of the assumptions the target declares, if readable."""

    try:
        obj = context.kernel.object(target)
    except Exception as exc:  # noqa: BLE001 - a missing object is not fatal
        LOG.warning("could not read %s for its assumptions: %s", target, exc)
        return []
    ids = tuple(str(item) for item in (getattr(obj, "assumptions", None) or ()))
    return _statements(context, ids)


def _prior_derivations(
    context: CycleContext, state: Mapping[str, Any], target: str
) -> list[str]:
    """Excerpts of derivations this project already recorded for this target.

    **When this is non-empty.** The routing guard in
    :mod:`research_os.runtime.graphs.cycle` refuses ``derive_mathematics`` for a
    target this project already holds a derivation for, so on the ordinary path
    -- where the planner names its target in ``addresses`` -- this returns
    nothing and the block is empty. What it covers is the *fallback*: a plan
    that addressed no capsule object at all, where the handler above picks the
    first unresolved hypothesis off the frontier. The guard could not have
    checked that target, because the plan never named it. So this is the second
    line, and it is the only line, on that path.

    Reads findings, not artifacts -- the excerpt is exactly the bounded,
    producer-authored form this is for, and reading the artifacts instead would
    be the unbounded scrape that `findings.MAX_EXCERPT_CHARS` exists to avoid.

    An unreadable store yields nothing rather than failing: this is context for
    a prompt, and a cycle must not die because a query did.
    """

    try:
        findings = context.store.list_findings(
            project_id=str(state["project_id"]), limit=MAX_PLANNER_FINDINGS
        )
    except Exception as exc:  # noqa: BLE001 - prompt context, never fatal
        LOG.warning("could not read prior derivations for %s: %s", target, exc)
        return []
    return [
        f"{item.finding_id}: {item.excerpt or item.summary}"
        for item in findings
        if item.source_action == str(ActionKind.DERIVE_MATHEMATICS)
        and target in item.capsule_refs
    ][:MAX_PRIOR_DERIVATIONS]
