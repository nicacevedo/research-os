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
from collections.abc import Mapping
from typing import Any

from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ArtifactRef, ModelRequest
from research_os.runtime.prompts import (
    BLIND_EXPLORER,
    SEEDED_EXPLORER,
    SKEPTIC,
    PromptTemplate,
)
from research_os.runtime.routing import RoutingError

LOG = logging.getLogger("research_os.runtime.actions.explore")

#: How many proposals from any one branch are carried forward. A branch that
#: returns forty hypotheses has not been more helpful than one that returned
#: four; it has moved the filtering to whoever reads the report.
MAX_PROPOSALS_PER_BRANCH = 6


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
            LOG.debug("could not read %s: %s", object_id, exc)
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
        },
        artifacts=(artifact,) if artifact else (),
    )
