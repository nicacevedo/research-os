"""The runtime action that turns a cycle's findings into a reviewable proposal.

This is the action that closes the loop, and it is deliberately thin. **It
delegates to :class:`research_os.proposal.controller.ProposalController` rather
than proposing anything itself** -- the same relationship
``actions/coding.py`` has to the automation controller, for the same reason.
That controller already does the whole authority-preserving path:

```text
capsule read deterministically
  -> one bounded proposal worker
  -> deterministic grounding validation (nothing cited that was not supplied)
  -> one bounded grounding correction, never a loop
  -> one independent assessment
  -> ProposalStore, outside every project
  -> a human promotion command, which produces a DRAFT and nothing stronger
```

A second proposal engine behind a nicer interface would be a second set of
bugs, and the interesting ones would be the grounding bugs.

What this module adds is the three things the v1 layer has no way to know
about.

**Grounding in runtime findings.** The v1 grounding allowlist has always had a
``finding_ids`` field and has always refused a citation to an id that was not
supplied -- and nothing ever supplied any. So an autonomous result reached the
proposal layer as prose inside the natural-language goal, and "what is this
proposal resting on" was answerable only by reading a model's sentence. This
handler resolves a bounded set of stored :class:`RuntimeFinding` records,
converts them to the v1 :class:`SuppliedFinding` type, and hands them over --
which puts their ids in the allowlist, their text in a fenced block, and
immutable links from the proposal to each finding in the operational database.

**Replay safety.** ``make_proposal_id`` is deterministic in project, goal and
*second*, and a retry does not reproduce the second. So a crash after the
proposal directory was created and before the runtime acknowledged it produced
a second proposal for one logical decision. This handler reserves the identity
first, from the action's stable key, and reconciles against it.

**What it must not do, stated as code rather than as a promise.** No capsule
file is written, ``write_promotion`` is not called and not imported, no Review
is authored, no Claim is accepted, and no canonical scientific status changes.
``tests/test_runtime_authority.py`` asserts the absence by parsing this module.
The proposal is noncanonical runtime state in the state home; crossing into the
capsule remains ``researchctl propose promote``, which requires an interactive
terminal.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_os.errors import (
    ProposalBudgetError,
    ProposalError,
    ProposalGroundingError,
    ProposalValidationError,
    ProviderUnavailableError,
    ResearchOSError,
)
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import FindingPacket, RuntimeFinding, packet_from
from research_os.runtime.idempotency import idempotency_key

LOG = logging.getLogger("research_os.runtime.actions.proposals")

#: The most model calls one proposal action may spend.
#:
#: Three: the proposal worker, one bounded grounding correction, one
#: independent assessment. The literature pass is deliberately not included --
#: a proposal grounded in runtime findings is grounded in work this runtime
#: already did, and adding a retrieval here would make one action reach the
#: network on a budget the plan did not ask for. ``search_literature`` is its
#: own action, with its own authority and its own budget line.
MAX_PROPOSAL_MODEL_CALLS = 3


def _supplied(findings: tuple[RuntimeFinding, ...]) -> list[Any]:
    """Convert stored runtime findings to the v1 proposal layer's type.

    A conversion rather than a shared type, because the dependency direction is
    ``runtime -> v1`` and never the reverse: a proposal layer that imported
    :mod:`research_os.runtime.findings` would make the scientific layers depend
    on PostgreSQL. ``tests/test_runtime_layering.py`` asserts the direction.
    """

    from research_os.proposal.models import SuppliedFinding

    return [
        SuppliedFinding(
            finding_id=item.finding_id,
            kind=str(item.kind),
            statement=item.summary,
            rests_on=[
                *item.artifact_ids,
                *item.capsule_refs,
                *item.literature_keys,
                *((item.experiment_job_id,) if item.experiment_job_id else ()),
            ],
        )
        for item in findings
    ]


def packet_for(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> FindingPacket:
    """Resolve the findings this proposal will be grounded in.

    An explicit ``parameters.finding_ids`` from the plan takes precedence and is
    resolved fail-closed: an id this project does not hold is an error, not
    something to drop. Without one, the most recent findings of this project are
    used, bounded -- which is the ordinary autonomous path, where the cycle that
    produced the findings is the cycle proposing from them.

    Never "every finding this project ever had". The bound is what keeps the
    grounding allowlist something a person can read, and an unbounded allowlist
    is one a worker cites from decoratively.
    """

    from research_os.runtime.findings import MAX_PACKET_FINDINGS

    project_id = str(state["project_id"])
    requested = tuple(
        str(value) for value in plan.get("parameters", {}).get("finding_ids", ())
    )
    if requested:
        return packet_from(
            context.store.resolve_findings(
                requested[:MAX_PACKET_FINDINGS], project_id=project_id
            )
        )
    return packet_from(
        context.store.list_findings(project_id=project_id, limit=MAX_PACKET_FINDINGS)
    )


@dataclass(frozen=True, slots=True)
class _RecoveredOutcome:
    """A proposal found on disk, shaped like a fresh ``ProposalOutcome``.

    So :func:`_payload` has one implementation rather than two. ``model_calls``
    is zero because this attempt made none -- the calls were paid for by the
    attempt that crashed, and reporting them again would double-count the
    spend in the run report.
    """

    proposal: Any
    assessment: Any
    store: Any
    model_calls: int = 0
    adopted: bool = True


def reservation_key_for(state: Mapping[str, Any]) -> str:
    """The action's stable identity: this cycle's one proposal.

    ``propose_capsule_change:<run>:<cycle>`` and nothing else. Two things about
    that are deliberate.

    **It contains nothing per-attempt.** Keying on anything a retry does not
    reproduce yields a ledger that records every duplicate faithfully and
    prevents none.

    **It contains nothing that can change between attempts either** -- and that
    is a stronger requirement, which a first version of this got wrong by
    including the grounding digest. A crashed attempt is retried after the
    daemon has ticked, and a tick may have recorded new findings; the digest
    would then differ, the key would differ, the reserved id would differ, and
    the reconciler would find no proposal and create a second one for the same
    decision. The grounding digest belongs in the proposal's basis snapshot,
    where changing it is *supposed* to be detected, and not in its identity.

    One cycle, at most one proposal, is a real property rather than an
    assumption: ``plan_one_action`` chooses exactly one action per cycle.
    """

    return f"propose_capsule_change:{state['run_id']}:{state['cycle_index']}"


def reconcile_reserved_proposal(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Did a previous attempt already create this cycle's proposal?

    Re-derives the reserved id from the cycle's own identity and asks the
    proposal store on disk. ``None`` means the effect did not take hold and the
    action may proceed.

    Deliberately consults the *directory*, not the reservation row. The row says
    an id was reserved; only the directory says a proposal exists, and a crash
    between reserving and calling the worker leaves the first without the
    second.
    """

    from research_os.errors import ProposalNotFoundError, ProposalStoreError
    from research_os.proposal.store import ProposalStore, reserved_proposal_id

    del plan  # the identity comes from the cycle, never from the planner
    reserved = reserved_proposal_id(reservation_key=reservation_key_for(state))
    try:
        store = ProposalStore.open(reserved)
        proposal = store.load()
    except (ProposalNotFoundError, ProposalStoreError):
        return None
    packet = packet_from(context.store.proposal_findings(reserved))
    return {
        "ok": True,
        "detail": f"recovered proposal {reserved} created by an earlier attempt",
        "data": _payload(
            _RecoveredOutcome(
                proposal=proposal,
                assessment=store.load_assessment(),
                store=store,
            ),
            packet=packet,
        ),
    }


def _controller(context: CycleContext) -> Any:
    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config
    from research_os.literature.config import load_config as load_literature_config
    from research_os.proposal.controller import ProposalController

    return ProposalController(
        providers=provider_registry(),
        config=load_config(),
        literature_config=load_literature_config(),
    )


def _payload(outcome: Any, *, packet: FindingPacket) -> dict[str, Any]:
    """The small, structured record of a proposal that goes into graph state.

    Not the proposal. The proposal is a file in the proposal store and a
    reference here; a checkpoint that carried every proposed item would grow by
    the size of everything the runtime ever suggested.
    """

    proposal = outcome.proposal
    assessment = outcome.assessment
    return {
        "proposal_id": proposal.proposal_id,
        "proposal_directory": str(outcome.store.directory),
        "adopted": bool(getattr(outcome, "adopted", False)),
        "items": [
            {
                "item_id": item.item_id,
                "kind": str(item.kind),
                "title": item.title,
                "basis": str(item.basis),
                "addresses": list(item.addresses),
                "grounded_in_findings": list(item.grounded_in_findings),
                "grounded_in_literature": list(item.grounded_in_literature),
            }
            for item in proposal.items
        ],
        "grounded_in_findings": list(packet.ids),
        "finding_packet_digest": packet.digest,
        "uncertainties": len(proposal.uncertainties),
        "next_actions": [
            {
                "kind": str(action.kind),
                "action": action.action,
                "requires_human": bool(action.requires_human),
            }
            for action in proposal.next_actions
        ],
        "assessment": (
            {
                "verdict": str(assessment.verdict),
                "blocking": [
                    f"{finding.severity}: {finding.message}"
                    for finding in assessment.blocking
                ][:5],
            }
            if assessment is not None
            else None
        ),
        "model_calls": outcome.model_calls,
        # Said plainly in the data a later node and the run report read, because
        # the one thing a reader must not conclude from "a proposal exists" is
        # that anything scientific happened.
        "promoted": False,
        "requires_human_promotion": True,
        "follow_up": (
            f"researchctl propose show {proposal.proposal_id}  # then "
            f"`propose promote {proposal.proposal_id} --item PR-001` if you agree"
        ),
    }


def propose_capsule_change(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Turn this project's runtime findings into a grounded, noncanonical proposal.

    The action a cycle takes when its work has produced something a *person*
    should decide about. It writes no science: it writes a proposal, records
    which findings it rests on, and hands over the command.
    """

    repo = Path(state["repo_path"])
    project_id = str(state["project_id"])
    goal = str(
        plan.get("parameters", {}).get("goal")
        or plan.get("rationale")
        or state["objective"]
    ).strip()
    if not goal:
        return ActionOutcome.failed(
            "a proposal needs a goal; the plan supplied neither parameters.goal "
            "nor a rationale, and the run has no objective",
            failure_class=FailureClass.POLICY_REFUSED,
        )

    try:
        packet = packet_for(state, context, plan)
    except ResearchOSError as exc:
        # A plan citing a finding this project does not hold is refused, not
        # quietly narrowed. Narrowing it would produce a proposal grounded in
        # less than the planner believed it was grounded in.
        return ActionOutcome.failed(
            f"the findings this proposal would cite are not available: {exc}",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    if not packet.findings:
        # Not a failure. A cycle with nothing to propose from should say so
        # rather than ask a model to invent something, which is the one way this
        # action could manufacture work.
        return ActionOutcome.succeeded(
            "this project has no runtime findings to ground a proposal in; "
            "nothing was proposed",
            data={"proposed": False, "reason": "no findings"},
        )

    from research_os.proposal.store import reserved_proposal_id

    reservation_key = reservation_key_for(state)
    proposal_id = reserved_proposal_id(reservation_key=reservation_key)
    stored_id, _status, _created = context.store.reserve_proposal(
        reservation_key=reservation_key,
        proposal_id=proposal_id,
        project_id=project_id,
        run_id=str(state["run_id"]),
    )
    # The reservation is authoritative: if an earlier attempt reserved a
    # different id for this key -- it cannot, the derivation is a function of
    # the key -- the stored one wins, because that is the one the directory
    # would have been created under.
    proposal_id = stored_id

    controller = _controller(context)
    key = idempotency_key(
        "proposal.create", state["run_id"], state["cycle_index"], reservation_key
    )

    def perform() -> dict[str, Any]:
        outcome = controller.propose(
            project_path=repo,
            goal=goal,
            # Deliberately off. Literature retrieval is its own action with its
            # own authority; a proposal action that reached the network would be
            # spending a budget line the plan did not ask for.
            with_literature=False,
            retrieve=False,
            assess=True,
            max_model_calls=MAX_PROPOSAL_MODEL_CALLS,
            findings=_supplied(packet.findings),
            proposal_id=proposal_id,
        )
        # Written before the result is returned, so the links exist whenever the
        # proposal does. A proposal a person can read whose grounding this
        # database cannot name is the state this whole mechanism exists to
        # prevent.
        context.store.link_proposal_findings(
            proposal_id=outcome.proposal.proposal_id,
            finding_ids=packet.ids,
            run_id=str(state["run_id"]),
        )
        return {
            "ok": True,
            "detail": (
                f"proposal {outcome.proposal.proposal_id} "
                f"({len(outcome.proposal.items)} item(s)) is waiting for you"
            ),
            "data": _payload(outcome, packet=packet),
        }

    try:
        result = context.ledger.run(
            key=key,
            kind="proposal.create",
            run_id=str(state["run_id"]),
            request={
                "goal": goal,
                "proposal_id": proposal_id,
                "finding_ids": list(packet.ids),
            },
            perform=perform,
            reconcile=lambda _invocation: reconcile_reserved_proposal(
                state, context, plan
            ),
        )
    except ProposalGroundingError as exc:
        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        return ActionOutcome.failed(
            f"the proposal cited something this cycle did not supply and the one "
            f"bounded correction did not repair it: {exc}",
            # Terminal, not retried. The correction has already happened once;
            # asking again would be a model being asked repeatedly until it
            # happens to produce something that passes, which is how a
            # grounding gate stops meaning anything.
            failure_class=FailureClass.MODEL_OUTPUT_INVALID_REPEATED,
        )
    except ProposalBudgetError as exc:
        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )
    except ProposalValidationError as exc:
        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        return ActionOutcome.failed(
            f"the proposal worker's output is not a valid proposal: {exc}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )
    except ProviderUnavailableError as exc:
        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.PROVIDER_UNAVAILABLE
        )
    except ProposalError as exc:
        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        return ActionOutcome.failed(
            f"the proposal could not be produced: {exc}",
            failure_class=FailureClass.CODE_EXCEPTION,
        )

    context.store.settle_proposal_reservation(reservation_key, status="CREATED")
    payload = dict(result.result.get("data") or {})
    return ActionOutcome.succeeded(
        str(result.result.get("detail") or "a proposal is waiting for you"),
        data=payload,
    )
