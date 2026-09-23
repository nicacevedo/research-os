"""The research frontier: questions owed an idea, and what each idea is owed next.

Two jobs, both of which the portfolio had no owner for, and both of which are
why recursive discovery was unreachable on real work (report §AB.7).

**Frontier requests.** A result, an anomaly, a falsifier's objection, a
reviewer's criticism, a replication that disagreed, a literature
contradiction, a referee's finding or a writer's evidence gap is *recorded*
where it happens -- by ordinary code, from a typed field, never from a model's
say-so about what should happen next -- as one ``frontier_requests`` row. The
portfolio then buys one follow-up explorer for it, like any other bounded
unit of work, and the children it proposes are new ideas with their own
versions, their own contracts and a lineage edge to the idea the question
came from. **A follow-up never edits its parent.** The parent's frozen
hypothesis and contract stay exactly what they were; what changes is that the
portfolio has a new question.

**Explicit continuation.** An idea whose track has nothing left to do --
``select_stage`` returns nothing -- used to sit in limbo: not rejected, not
parked, not blocked, and never chosen again. :func:`settle` gives every such
idea an explicit state with its reason: ``REJECTED`` when a fatal objection
to the claim stands, ``PARKED`` with a revisit condition otherwise. A
``VALIDATED`` idea with nothing left to run is left as it is: it is closed for
synthesis, which is what the writer reads.

Bounds, because every generator here is a way to loop (invariant 14): one
follow-up in flight per project, one request per raising event (a unique
index), children bounded by ``max_children_per_branch`` and by the lineage's
active ceiling, requests from ideas at ``max_lineage_depth`` declined, and a
request that fails ``max_stage_failures`` times declined rather than retried.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research_os.portfolio import dedup as pdedup
from research_os.portfolio import packets, stages
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.contracts import ContractError, FollowUpOutput, parse
from research_os.portfolio.models import (
    SEVERITY_ORDER,
    EdgeKind,
    FrontierRequest,
    IdeaOrigin,
    IdeaStatus,
    ObjectionTarget,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    RequestState,
    Severity,
    Stage,
)
from research_os.portfolio.prompts import TEMPLATES as PORTFOLIO_TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ArtifactStore, ModelProvider, ModelRequest
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.portfolio.frontier")

#: The work kind the allocator buys for one open follow-up request.
FOLLOW_UP = "portfolio_follow_up"

#: Statuses on which an idea is still owed work, and so still owed a
#: continuation decision when its track has none left.
_UNSETTLED: frozenset[IdeaStatus] = frozenset(
    {
        IdeaStatus.CANDIDATE,
        IdeaStatus.PROMISING,
        IdeaStatus.INVESTIGATING,
        IdeaStatus.REVIEW,
    }
)


# ------------------------------------------------------------- raising --
def raise_request(
    store: PortfolioStore,
    *,
    project_id: str,
    basis: RequestBasis,
    source_ref: str,
    question: str,
    kind: RequestKind = RequestKind.FOLLOW_UP,
    source_idea_id: str | None = None,
    source_version: int | None = None,
    detail: str = "",
) -> FrontierRequest:
    """Record one question the portfolio now owes an idea. Idempotent per event."""

    return store.open_request(
        project_id=project_id,
        kind=kind,
        basis=basis,
        source_idea_id=source_idea_id,
        source_version=source_version,
        source_ref=source_ref,
        question=question.strip()[:2_000] or "(no question stated)",
        detail=detail[:4_000],
    )


def requests_from_objections(
    store: PortfolioStore,
    *,
    project_id: str,
    idea_id: str,
    version: int,
    basis: RequestBasis,
    objections: Sequence[tuple[str, str, str]],
) -> int:
    """One request per objection that proposed a follow-up question.

    ``objections`` is ``(objection_id, summary, follow_up_question)``. Only an
    objection whose author wrote a follow-up question raises one: the
    reviewer is the one who knows whether a criticism is also a question, and
    turning every objection into a paid follow-up would make each review a
    fan-out. What the reviewer may *not* do is change the idea it criticised;
    its question becomes somebody else's new idea.
    """

    raised = 0
    for objection_id, summary, question in objections:
        if not question.strip():
            continue
        raise_request(
            store,
            project_id=project_id,
            basis=basis,
            source_ref=objection_id,
            question=question,
            source_idea_id=idea_id,
            source_version=version,
            detail=f"raised by the objection: {summary}",
        )
        raised += 1
    return raised


# ----------------------------------------------------------- continuation --
def settle(
    store: PortfolioStore,
    idea: Any,
    snapshot: stages.TrackSnapshot,
    config: PortfolioConfig,
) -> str | None:
    """Give an idea with nothing left to run an explicit state, and say why.

    Returns the status applied, or ``None`` when the idea still has work or
    is already settled. Deterministic: the decision reads the same rows
    ``select_stage`` read and the reason is its reason.
    """

    if idea.status not in _UNSETTLED:
        return None
    stage, reason = stages.select_stage(snapshot, config)
    if stage is not None:
        return None
    if "retrieved source" in reason:
        # The one dead end the portfolio can do something about itself: the
        # index does not hold enough sources for this question. Ask the
        # literature -- discovery, then a verified reading -- once per
        # version, and wait for the answer rather than parking on a gap
        # retrieval might close. A second arrival here, after the answer, is
        # a real dead end and parks.
        from research_os.portfolio import litintel

        source_ref = f"novelty:{idea.idea_id}:v{snapshot.version.version}"
        asked = [
            item
            for item in store.list_requests(
                project_id=idea.project_id, source_idea_id=idea.idea_id
            )
            if item.source_ref == source_ref
        ]
        if not asked:
            litintel.ask(
                store,
                project_id=idea.project_id,
                idea_id=idea.idea_id,
                version=snapshot.version.version,
                question=(
                    f"What published work bears on: {snapshot.version.research_question}"
                ),
                source_ref=source_ref,
                basis=RequestBasis.LITERATURE,
                wait=True,
            )
            return "WAITING_FOR_LITERATURE"
    fatal = [
        item
        for item in snapshot.open_objections
        if item.severity is Severity.FATAL and item.target is ObjectionTarget.CLAIM
    ]
    if fatal:
        store.set_status(
            idea_id=idea.idea_id,
            status=IdeaStatus.REJECTED,
            retire_reason=f"a fatal objection to the claim stands: {fatal[0].summary}",
        )
        return str(IdeaStatus.REJECTED)
    store.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason=f"no further autonomous step: {reason}",
        revisit_if=_revisit_condition(snapshot, reason),
    )
    return str(IdeaStatus.PARKED)


def _revisit_condition(snapshot: stages.TrackSnapshot, reason: str) -> str:
    settled = snapshot.settled_measurement
    if settled is not None:
        return (
            f"a follow-up idea answers what the {settled.conclusion} measurement "
            f"{settled.experiment_id} left open, or a capability that can measure "
            f"this is declared"
        )
    if "retrieved source" in reason:
        return "the literature index gains enough sources to audit this novelty claim"
    if snapshot.blocking_objections:
        worst = max(
            snapshot.blocking_objections, key=lambda item: SEVERITY_ORDER[item.severity]
        )
        return f"the standing objection is answered: {worst.summary[:200]}"
    return "a person or a later finding reopens it: " + reason[:300]


# -------------------------------------------------------- the explorer --
@dataclass(slots=True)
class FrontierContext:
    """What a follow-up explorer may reach. No idea track, no capsule handle."""

    config: PortfolioConfig
    portfolio: PortfolioStore
    runtime: RuntimeStore
    models: ModelProvider
    artifacts: ArtifactStore
    project_id: str
    run_id: str
    established_facts: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FollowUpResult:
    ok: bool
    detail: str
    created: tuple[str, ...] = ()
    converged: tuple[str, ...] = ()
    cost_usd: Decimal = Decimal(0)
    model_calls: int = 0
    failure_class: FailureClass | None = None


def run_follow_up(context: FrontierContext, request_id: str) -> FollowUpResult:
    """Turn one open request into the new ideas it raises -- or say why none.

    The one model call is the follow-up explorer, and it is shown the parent
    idea (when there is one), the event that raised the question, and the
    evidence the parent rests on -- never the parent's reviewers' verdicts
    beyond what the event itself carries, and never a field through which it
    could revise the parent. Its children go through the same deterministic
    deduplication as an explorer's; a child that another route already
    proposed is recorded as *convergence* on the existing idea rather than
    dropped.
    """

    store = context.portfolio
    request = store.require_request(request_id)
    if request.state is not RequestState.OPEN:
        return FollowUpResult(
            ok=True, detail=f"{request_id} is already {request.state}"
        )
    bounds = context.config.bounds

    parent = store.get_idea(request.source_idea_id) if request.source_idea_id else None
    if parent is not None and parent.depth >= bounds.max_lineage_depth:
        store.close_request(
            request_id,
            state=RequestState.DECLINED,
            resolution=(
                f"{parent.idea_id} is at depth {parent.depth}, the lineage depth "
                f"bound; a question this deep is recorded and not pursued"
            ),
        )
        return FollowUpResult(ok=True, detail="declined: the lineage depth bound")
    room = bounds.max_children_per_branch
    if parent is not None:
        active = store.lineage_active_counts(context.project_id).get(
            parent.lineage_root, 0
        )
        room = min(room, max(0, bounds.max_active_per_lineage - active))
    if room <= 0:
        store.close_request(
            request_id,
            state=RequestState.DECLINED,
            resolution="the lineage already holds its ceiling of live ideas",
        )
        return FollowUpResult(ok=True, detail="declined: the lineage ceiling")

    template = PORTFOLIO_TEMPLATES["follow_up_explorer"]
    parent_block: list[str] = ["(this question was not raised by an existing idea)"]
    evidence_block: list[str] = []
    if parent is not None:
        version = store.require_version(
            parent.idea_id, request.source_version or parent.current_version
        )
        packet = packets.build_packet(
            version=version,
            evidence=store.list_evidence(
                idea_id=parent.idea_id, idea_version=version.version
            ),
            objections=(),
        )
        parent_block = [
            f"idea id: {parent.idea_id} ({parent.status}, depth {parent.depth})"
        ]
        parent_block += packet.idea_block()
        evidence_block = list(packet.evidence_block())
    finding = [
        f"basis: {request.basis}",
        f"raised by: {request.source_ref}",
        f"question: {request.question}",
    ]
    if request.detail:
        finding.append(f"detail: {request.detail}")
    try:
        response = context.models.complete(
            ModelRequest(
                role=template.role,
                capability=template.capability,
                prompt=template.render(
                    fields={"maximum_children": str(room)},
                    blocks={
                        "parent": parent_block,
                        "finding": finding,
                        "evidence": evidence_block or ["(no evidence recorded)"],
                        "established_facts": list(context.established_facts),
                    },
                ),
                prompt_version=template.identity,
                criticality=template.criticality,
                independence=template.independence,
                json_schema=template.output_schema,
                max_cost_usd=float(context.config.cost_for(Stage.BRANCH)),
            )
        )
    except ProviderCallFailedError as exc:
        store.count_request_attempt(request_id)
        return FollowUpResult(
            ok=False,
            detail=f"the follow-up explorer could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    cost = Decimal(str(response.cost_usd or 0))
    if not response.ok:
        return _failed(context, request, f"nothing usable: {response.error}", cost)
    try:
        output = parse(
            FollowUpOutput,
            structured=response.structured,
            text=response.text,
            role=template.name,
        )
        output.check(maximum=room)
    except ContractError as exc:
        return _failed(context, request, str(exc), cost)

    if not output.children:
        store.close_request(
            request_id,
            state=RequestState.DECLINED,
            resolution=f"the follow-up explorer proposed nothing: {output.nothing_to_propose}",
            resolved_by=response.call_id,
        )
        return FollowUpResult(
            ok=True, detail="nothing to propose", cost_usd=cost, model_calls=1
        )

    created: list[str] = []
    converged: list[str] = []
    basis = ProvenanceBasis(str(request.basis))
    for child, relation in zip(output.children, output.relations, strict=True):
        fields = child.as_fields()
        screened = pdedup.screen(
            store,
            project_id=context.project_id,
            fields=fields,
            config=context.config,
            lineage_family=(
                store.lineage_family(parent.idea_id) if parent is not None else None
            ),
        )
        if screened.is_duplicate and screened.match_idea_id:
            store.record_provenance(
                idea_id=screened.match_idea_id,
                basis=ProvenanceBasis.CONVERGENCE,
                source_ref=request.request_id,
                request_id=request.request_id,
                call_id=response.call_id,
                detail=(
                    f"a follow-up to {request.basis} {request.source_ref} proposed "
                    f"this direction again ({screened.detail})"
                ),
            )
            converged.append(screened.match_idea_id)
            continue
        idea, _version = store.create_idea(
            project_id=context.project_id,
            origin=IdeaOrigin.FOLLOW_UP,
            fields=fields,
            parent_idea_id=parent.idea_id if parent is not None else None,
            edge_kind=EdgeKind(relation),
            edge_detail=f"follow-up to {request.basis} {request.source_ref}",
            origin_call_id=response.call_id,
            origin_role=str(template.role),
            origin_stage="follow_up",
            dimensions=child.dimensions,
            provenance=(basis, request.source_ref, request.request_id),
        )
        created.append(idea.idea_id)

    store.close_request(
        request_id,
        state=RequestState.CONSUMED,
        resolution=(
            f"{len(created)} new idea(s)"
            + (
                f", {len(converged)} convergence(s) with existing ideas"
                if converged
                else ""
            )
        ),
        resolved_by=response.call_id,
    )
    return FollowUpResult(
        ok=True,
        detail=f"opened {len(created)} follow-up idea(s) for {request.basis}",
        created=tuple(created),
        converged=tuple(converged),
        cost_usd=cost,
        model_calls=1,
    )


def _failed(
    context: FrontierContext, request: FrontierRequest, why: str, cost: Decimal
) -> FollowUpResult:
    """A malformed answer: retry within the bound, then decline and say so."""

    attempts = context.portfolio.count_request_attempt(request.request_id)
    if attempts >= context.config.bounds.max_stage_failures:
        context.portfolio.close_request(
            request.request_id,
            state=RequestState.DECLINED,
            resolution=f"the follow-up explorer failed {attempts} times: {why}",
        )
        return FollowUpResult(
            ok=True,
            detail=f"declined after {attempts} failures",
            cost_usd=cost,
            model_calls=1,
        )
    return FollowUpResult(
        ok=False,
        detail=why,
        cost_usd=cost,
        model_calls=1,
        failure_class=FailureClass.MODEL_OUTPUT_INVALID,
    )


def open_requests_summary(store: PortfolioStore, project_id: str) -> Mapping[str, int]:
    """Counts of open requests by basis, for status surfaces."""

    counts: dict[str, int] = {}
    for item in store.list_requests(project_id=project_id, states=(RequestState.OPEN,)):
        counts[str(item.basis)] = counts.get(str(item.basis), 0) + 1
    return counts


__all__ = [
    "FOLLOW_UP",
    "FollowUpResult",
    "FrontierContext",
    "raise_request",
    "requests_from_objections",
    "run_follow_up",
    "settle",
]
