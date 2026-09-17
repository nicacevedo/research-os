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
    BudgetExceededError,
    ProposalBudgetError,
    ProposalError,
    ProposalGroundingError,
    ProposalValidationError,
    ProviderInvocationError,
    ProviderUnavailableError,
    ResearchOSError,
)
from research_os.runtime.actions.base import ActionOutcome, charge_delegated_spend
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import FindingPacket, RuntimeFinding, packet_from
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.spend import DelegatedSpendAuthority

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
            excerpt=item.excerpt,
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


def _cited_findings(proposal: Any) -> tuple[str, ...]:
    """The finding ids this proposal's items are actually grounded in.

    Read off the validated proposal, so it is the worker's own citations and not
    the caller's guess at them. The validator has already refused anything
    outside the allowlist, so every id here is one the packet offered.
    """

    cited: set[str] = set()
    for item in getattr(proposal, "items", ()):
        cited.update(str(one) for one in getattr(item, "grounded_in_findings", ()))
    return tuple(sorted(cited))


def reconcile_reserved_proposal(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Did a previous attempt already create this cycle's proposal?

    Re-derives the reserved id from the cycle's own identity and asks the
    proposal store on disk. ``None`` means the effect did not take hold and the
    action may proceed.

    The *existence* question is put to the directory, not to the reservation
    row. The row says an id was reserved; only the directory says a proposal
    exists, and a crash between reserving and calling the worker leaves the
    first without the second.

    The *identity* comes from the row when there is one. An adversarial review
    pointed out that re-deriving it here made the reconciler correct only for
    as long as two functions agreed: bump the derivation inside
    ``reserved_proposal_id``, or change what ``reservation_key_for`` includes,
    and this starts asking about a directory that was never created -- finds
    nothing, returns ``None``, and the action writes a second proposal for the
    same cycle. Deriving is the fallback for a key with no row, which is the
    case where nothing was reserved and there is nothing to find.
    """

    from research_os.errors import ProposalNotFoundError, ProposalStoreError
    from research_os.proposal.store import ProposalStore, reserved_proposal_id

    del plan  # the identity comes from the cycle, never from the planner
    key = reservation_key_for(state)
    row = context.store.reserved_proposal(key)
    reserved = row[0] if row is not None else reserved_proposal_id(reservation_key=key)
    try:
        store = ProposalStore.open(reserved)
        proposal = store.load()
    except (ProposalNotFoundError, ProposalStoreError):
        return None
    packet = packet_from(context.store.proposal_findings(reserved))
    return {
        "ok": True,
        "detail": f"recovered proposal {reserved} created by an earlier attempt",
        # The recovered proposal's own citations, so the caller can write the
        # citation edges for a proposal it did not create.
        "cited_findings": _cited_findings(proposal),
        "data": _payload(
            _RecoveredOutcome(
                proposal=proposal,
                assessment=store.load_assessment(),
                store=store,
            ),
            packet=packet,
        ),
    }


def _controller(context: CycleContext, *, authority: Any = None) -> Any:
    """A v1 proposal controller whose providers answer to the runtime's budget.

    ``authority`` wraps every adapter, so each call this controller makes
    reserves runtime capacity *before* it happens and settles afterwards. The
    controller is unchanged and unaware: it was given a registry and it calls
    ``invoke`` on it, which is precisely why the registry is where the budget
    belongs. Without one -- a caller with no runtime ledger -- the adapters are
    the plain ones.
    """

    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config
    from research_os.literature.config import load_config as load_literature_config
    from research_os.proposal.controller import ProposalController

    del context
    providers = provider_registry()
    if authority is not None:
        providers = authority.wrap(providers)
    return ProposalController(
        providers=providers,
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
        # Symmetric with the two refusal paths, which set it False. It was
        # present only when it was False, so "was a proposal written" could not
        # be asked of the payload uniformly -- a caller had to know which shape
        # it was holding. `adopted` says whether *this* attempt wrote it.
        "proposed": True,
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
        # Two digests, two names, because they are two values and giving them
        # one name made a run report and a stored basis disagree about "the"
        # digest. `finding_packet_digest` is what the proposal *stores* -- v1's
        # `supplied_findings_digest`, which is what a promotion re-checks
        # against -- and `runtime_packet_digest` is this packet's own identity.
        "finding_packet_digest": (
            proposal.scientific_basis.finding_packet_digest
            if proposal.scientific_basis is not None
            else None
        ),
        "runtime_packet_digest": packet.digest,
        "uncertainties": len(proposal.uncertainties),
        "next_actions": [
            {
                "kind": str(action.kind),
                "action": action.action,
                "requires_human": bool(action.requires_human),
            }
            for action in proposal.next_actions
        ],
        # Stated as its own boolean rather than left to be inferred from a
        # null. A proposal that reached a person without the independent
        # assessment is a weaker thing than one that passed it, and the first
        # closed-loop pilot produced exactly that -- the assessor exhausted its
        # structured-output retries after the proposal had been stored. Nothing
        # said so where a reader would look.
        "assessed": assessment is not None,
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
            f"`propose promote {proposal.proposal_id} --item PR-001` if you agree, "
            f"or `propose decline {proposal.proposal_id} --reason ...` if you do "
            f"not. Both are decisions; only the second stops the runtime asking"
        ),
    }


def _equivalent_pending_proposal(
    *,
    project_id: str,
    packet: FindingPacket,
    context: CycleContext,
    exclude: str = "",
) -> str | None:
    """A pending proposal of this project grounded in exactly these findings.

    Matched on the *grounding* digest rather than on the text, because the text
    is a model's and two runs of one worker over one packet will not produce
    identical prose while proposing the same thing. `supplied_findings_digest`
    is what the basis snapshot already records, so the comparison needs nothing
    new stored.

    "Pending" means **it still has items nobody has acted on**, and getting that
    predicate right took a real pilot. It was "has no promotions at all", and
    the second pilot showed what that permits: a nine-item proposal, one item
    promoted by the researcher, eight left undecided -- and the successor cycle,
    holding the *same single finding*, wrote a second proposal. Same grounding
    digest, no new evidence, eight questions re-asked. The pilot's own assertion
    "exactly one logical proposal" is what caught it.

    A promotion is not an answer to the items it did not touch. So the test is
    the difference between the proposal's item ids and the item ids its
    promotion records name; an empty difference means the researcher has acted
    on all of it and a repeat would be arguing with a decision.

    **A decline answers an item too**, and until this release it could not.
    `ProposalStore` wrote promotions and nothing else, so "I read this and I do
    not want it" had no representation: a rejected proposal stayed pending
    forever, this function kept returning it as the answer to every cycle with
    the same grounding, and the runtime never proposed about those findings
    again. The predicate now asks `decided_item_ids`, which is promotions and
    declines together, because the question here is whether a *person has acted*
    and not which way they acted.

    A decline is human-authored and this module cannot write one -- it reads the
    store and `tests/test_runtime_authority.py` asserts structurally that no
    runtime module can reach `record_decline`. Reading a decision is not making
    one.
    """

    from research_os.errors import ProposalStoreError
    from research_os.proposal.store import ProposalStore

    del context
    # **v1's digest, not the runtime's.** The two are different functions over
    # different material: `FindingPacket.digest` hashes
    # `(finding_id, RuntimeFinding.digest)` and `supplied_findings_digest`
    # hashes `(finding_id, kind, statement, rests_on)` with its own prefix. What
    # the proposal *stores* in its basis snapshot is v1's, because v1 wrote it.
    # Comparing the runtime's against the stored one can never match, so this
    # function returned `None` unconditionally and the cross-cycle dedup it
    # implements did nothing at all. The second real pilot found it: two
    # proposals over one unchanged finding packet, caught by the pilot's own
    # "exactly one logical proposal" assertion rather than by any test.
    from research_os.proposal.planner import supplied_findings_digest

    wanted = supplied_findings_digest(_supplied(packet.findings))
    if wanted is None:
        return None
    try:
        known = ProposalStore.list_proposal_ids()
    except ResearchOSError:  # pragma: no cover - the root may not exist yet
        return None
    for proposal_id in reversed(known):
        if proposal_id == exclude:
            # This cycle's own reserved id. A crashed earlier attempt of *this*
            # cycle leaves exactly that directory, and it has to reach the
            # recovery path -- which settles the reservation and writes the
            # finding links -- rather than being reported as somebody else's
            # equivalent proposal. Found by the crash-recovery test the moment
            # this comparison started working.
            continue
        try:
            store = ProposalStore.open(proposal_id)
            proposal = store.load()
        except (ResearchOSError, ProposalStoreError):
            continue
        if proposal.project_id != project_id:
            continue
        basis = proposal.scientific_basis
        if basis is None or basis.finding_packet_digest != wanted:
            continue
        decided = store.decided_item_ids()
        undecided = [
            item.item_id for item in proposal.items if item.item_id not in decided
        ]
        if not undecided:
            # Every item acted on. Re-offering it would argue with a decision.
            continue
        return proposal_id
    return None


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

    # Derived before the dedup runs, because the dedup has to exclude it.
    reservation_key = reservation_key_for(state)
    proposal_id = reserved_proposal_id(reservation_key=reservation_key)

    # An equivalent proposal already waiting for a decision is not a second
    # decision. The reservation makes one *cycle* produce one proposal; it says
    # nothing about two cycles over the same findings, which
    # `_work_advance_objective` produces whenever any unrelated capsule change
    # moves the frontier while a proposal sits undecided. An adversarial review
    # executed it: two proposals, identical items, identical grounding digest,
    # two independent assessments, both pending.
    #
    # The nomination path has had this check since it was written; this is the
    # same check, and it is deliberately narrow -- only *pending* proposals, so
    # a promoted or declined one is never re-offered, because both are answered.
    existing = _equivalent_pending_proposal(
        project_id=project_id,
        packet=packet,
        context=context,
        exclude=proposal_id,
    )
    if existing is not None:
        return ActionOutcome.succeeded(
            f"an equivalent proposal is already waiting for your decision, and "
            f"it rests on exactly these findings: {existing}",
            data={
                "proposed": False,
                "reason": "an equivalent pending proposal exists",
                "proposal_id": existing,
                "grounded_in_findings": list(packet.ids),
                "finding_packet_digest": packet.digest,
                "promoted": False,
                "requires_human_promotion": True,
            },
        )

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

    # One authority for this action's whole delegation, so the reconciliation
    # below can subtract exactly what it settled.
    authority = DelegatedSpendAuthority(
        budgets=context.budgets,
        run_id=str(state["run_id"]),
        project_id=project_id,
        work_id=state.get("work_id"),
        action="propose_capsule_change",
    )
    controller = _controller(context, authority=authority)
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
        # The provenance rows for what the delegated worker did, and a
        # reconciliation of anything the authority did not already reserve and
        # settle. The money itself was accounted before each call was made.
        charge_delegated_spend(
            state,
            context,
            outcome.invocations,
            action="propose_capsule_change",
            authority=authority,
        )
        # Written before the result is returned, so the links exist whenever the
        # proposal does. A proposal a person can read whose grounding this
        # database cannot name is the state this whole mechanism exists to
        # prevent.
        context.store.link_proposal_findings(
            proposal_id=outcome.proposal.proposal_id,
            finding_ids=packet.ids,
            # The citation edges, taken from the proposal's own items rather
            # than from the packet. An adversarial review pointed out that
            # linking the packet alone made the table say a proposal rested on
            # every finding the worker was shown -- eight offered, two cited,
            # six rows asserting a dependence that does not exist. Both facts
            # are kept and they are distinguishable; see
            # `sql/0011_proposal_link_citation.sql`.
            cited_ids=_cited_findings(outcome.proposal),
            run_id=str(state["run_id"]),
        )
        assessed = outcome.assessment is not None
        return {
            "ok": True,
            "detail": (
                f"proposal {outcome.proposal.proposal_id} "
                f"({len(outcome.proposal.items)} item(s)) is waiting for you"
                + (
                    ""
                    if assessed
                    else "; it has NO independent assessment, so read it more "
                    "carefully than one that passed"
                )
            ),
            "data": _payload(outcome, packet=packet),
        }

    # A completed invocation whose proposal has since been deleted is a
    # decision, not a result to replay. Reopened so the action runs again rather
    # than reporting a directory that is not there.
    remembered = context.ledger.get(key)
    if remembered is not None and _proposal_is_gone(
        str((remembered.result or {}).get("data", {}).get("proposal_id") or proposal_id)
    ):
        LOG.info(
            "the proposal this cycle produced has been deleted; reopening %s so "
            "the action is not reported from a stale record",
            key[:16],
        )
        context.ledger.reopen(key)

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
    except (ProposalError, ProviderInvocationError, BudgetExceededError) as exc:
        # **Look before failing, for every exception raised after the store.**
        #
        # `ProposalController` creates the proposal directory and *then* runs
        # the independent assessment, so anything the assessor raises arrives
        # with the valuable artifact already on disk. There is more than one
        # such exception: a provider that does not answer raises
        # `ProviderInvocationError`, and a provider that answers *wrongly* --
        # well-formed JSON with a verdict the schema does not know -- raises
        # `ProposalValidationError`. The second is the likelier one, and an
        # earlier version of this handler covered only the first: an
        # adversarial review executed it and found a valid, promotable proposal
        # on disk with the action reporting failure, the reservation FAILED and
        # no finding links written.
        #
        # So the recovery is attempted once, here, for all of them, and the
        # per-class failure returns below are reached only when there is really
        # nothing to recover.
        # The failed attempt's calls cost the same as a successful one's, so
        # they are charged before anything else happens on this path. v1
        # attaches the records to the exception for exactly this.
        charge_delegated_spend(
            state,
            context,
            getattr(exc, "model_invocations", ()),
            action="propose_capsule_change",
            authority=authority,
        )
        recovered = reconcile_reserved_proposal(state, context, plan)
        if recovered is not None:
            # The links, written on this path too. They are written inside
            # `perform` on the ordinary path, which never ran to completion
            # here -- so a recovered proposal used to be reported as grounded
            # in nothing, with a real-looking digest of the empty packet.
            context.store.link_proposal_findings(
                proposal_id=str(recovered["data"]["proposal_id"]),
                finding_ids=packet.ids,
                cited_ids=tuple(recovered.get("cited_findings") or ()),
                run_id=str(state["run_id"]),
            )
            context.store.settle_proposal_reservation(
                reservation_key,
                status="CREATED",
                detail=f"stored; the assessment failed: {str(exc)[:300]}",
            )
            payload = dict(recovered.get("data") or {})
            payload["assessed"] = False
            payload["assessment_error"] = str(exc)[:500]
            payload["grounded_in_findings"] = list(packet.ids)
            payload["finding_packet_digest"] = packet.digest
            return ActionOutcome.succeeded(
                f"proposal {payload.get('proposal_id')} is waiting for you, and "
                f"it has NO independent assessment: {exc}. Read it more "
                f"carefully than one that passed.",
                data=payload,
            )

        context.store.settle_proposal_reservation(
            reservation_key, status="FAILED", detail=str(exc)[:500]
        )
        if isinstance(exc, ProposalGroundingError):
            return ActionOutcome.failed(
                f"the proposal cited something this cycle did not supply and the "
                f"one bounded correction did not repair it: {exc}",
                # Terminal, not retried. The correction has already happened
                # once; asking again would be a model being asked repeatedly
                # until it happens to produce something that passes, which is
                # how a grounding gate stops meaning anything.
                failure_class=FailureClass.MODEL_OUTPUT_INVALID_REPEATED,
            )
        if isinstance(exc, ProposalBudgetError | BudgetExceededError):
            # Two budgets, one terminal answer. `ProposalBudgetError` is the v1
            # controller's own per-action call ceiling; `BudgetExceededError`
            # here is the *runtime* budget refusing a call before it was made,
            # raised by the wrapped provider. Neither is repaired: a budget
            # that retries is not a budget.
            return ActionOutcome.failed(
                str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
            )
        if isinstance(exc, ProposalValidationError):
            return ActionOutcome.failed(
                f"the proposal worker's output is not a valid proposal: {exc}",
                failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            )
        if isinstance(exc, ProviderUnavailableError | ProviderInvocationError):
            return ActionOutcome.failed(
                f"the proposal worker failed: {exc}",
                failure_class=FailureClass.PROVIDER_UNAVAILABLE,
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


def _proposal_is_gone(proposal_id: str) -> bool:
    """Whether a proposal the ledger remembers has since been deleted.

    Deleting a proposal is a legitimate human act -- ``proposal/store.py`` says
    in its first paragraph that a proposal directory can be removed and loses
    nothing scientific -- and it means "I have decided about this".

    The ledger does not know that. An adversarial review executed the
    consequence: with the invocation `COMPLETED`, `perform` is never called
    again, so the stale payload was returned verbatim. The action reported
    "proposal PROP-... is waiting for you" with a directory that no longer
    existed, `conclude` parked the objective at
    `WAITING_FOR_SCIENTIFIC_DECISION`, and the follow-up command it printed
    raised `ProposalNotFoundError`. The objective never moved again.
    """

    from research_os.errors import ProposalNotFoundError, ProposalStoreError
    from research_os.proposal.store import ProposalStore

    if not proposal_id:
        return False
    try:
        ProposalStore.open(proposal_id).load()
    except (ProposalNotFoundError, ProposalStoreError):
        return True
    return False
