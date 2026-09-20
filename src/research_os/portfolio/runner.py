"""The stage handlers: what each bounded step of an idea track actually does.

One function per stage, dispatched from a closed table, with the same contract
:mod:`research_os.runtime.actions.base` established for the cycle graph's
handlers and for the same reason:

    ``ok=True`` means the stage did what it was asked to. That includes a
    falsifier that killed the idea -- the work succeeded, the answer was no.
    ``ok=False`` with a failure class means something malfunctioned.

There is deliberately no third option, so a handler cannot express "the science
came out negative so this failed". A system that retried refutations until they
stopped refuting would be a machine for manufacturing positive results, and
this is the second place in the codebase where that would have to start.

**What a handler may not do.** Write a capsule file, author a Review, promote
anything, or decide a quality tier. The last is the easy one to get wrong: a
handler records evidence, reviews and objections, and
:func:`research_os.portfolio.gates.evaluate` decides what those permit. The
only handler that produces a *disposition* from a model's opinion is
``meta_review``, and it passes that opinion through
:func:`research_os.portfolio.gates.permit`, which can only lower it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from research_os.portfolio import dedup as pdedup
from research_os.portfolio import gates, packets, stages
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.contracts import (
    BranchOutput,
    ContractError,
    DiscoveryOutput,
    DuplicateAdjudication,
    FalsifierOutput,
    MetaReviewOutput,
    NoveltyAuditOutput,
    ReviewOutput,
    ScreenOutput,
    parse,
)
from research_os.portfolio.models import (
    AdjudicationType,
    ContextClass,
    Disposition,
    EdgeKind,
    EvidenceKind,
    EvidenceStrength,
    IdeaOrigin,
    IdeaStatus,
    QualityDimensions,
    QualityTier,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.prompts import CURRENT_REVIEW_PROMPTS
from research_os.portfolio.prompts import TEMPLATES as PORTFOLIO_TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import (
    ArtifactStore,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from research_os.runtime.prompts import PromptTemplate
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.portfolio.runner")


class LiteratureSource(Protocol):
    """How a stage retrieves scholarship, without knowing where from.

    One method, and the return value is the v1 :class:`LiteraturePacket` --
    reused rather than wrapped, because it is already "a bounded, fenced view
    of retrieved scholarship" built from the store rather than from a provider
    response, which is exactly what a novelty audit must read.

    Injected rather than imported so a deployment with no literature access is
    a *missing capability* the gate reports, not a crash. An idea whose novelty
    cannot be established does not reach VALIDATED, and saying that is better
    than pretending.
    """

    def search(self, query: str, *, limit: int = 12) -> Any: ...


@dataclass(slots=True)
class TrackContext:
    """The live services one stage may reach, and nothing it may not.

    Shaped after :class:`research_os.runtime.context.CycleContext`, and absent
    for the same reason: there is no handle here that could write a capsule.
    """

    config: PortfolioConfig
    portfolio: PortfolioStore
    runtime: RuntimeStore
    models: ModelProvider
    artifacts: ArtifactStore
    project_id: str
    idea_id: str
    run_id: str
    work_id: str | None = None
    #: Read-only project context for the generators. Plain strings, assembled
    #: by the caller from the charter and the capsule, never read from disk
    #: here.
    charter: str = ""
    problem: str = ""
    established_facts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    literature: LiteratureSource | None = None
    #: Set when this machine can execute something. Its absence is why a
    #: mathematical idea on a host with no executor stops below VALIDATED
    #: rather than being validated on prose.
    can_execute: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What one stage did. Same shape as ``actions.base.ActionOutcome``."""

    ok: bool
    detail: str
    disposition: Disposition | None = None
    cost_usd: Decimal = Decimal(0)
    model_calls: int = 0
    failure_class: FailureClass | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ok and self.failure_class is not None:
            raise ValueError(
                "a stage that succeeded has no failure class. A falsifier that "
                "killed an idea succeeded; see this module's docstring."
            )
        if not self.ok and self.failure_class is None:
            raise ValueError("a stage that failed must name its failure class")

    @classmethod
    def succeeded(
        cls,
        detail: str,
        *,
        disposition: Disposition | None = None,
        cost_usd: Decimal = Decimal(0),
        model_calls: int = 0,
        data: Mapping[str, Any] | None = None,
    ) -> StageOutcome:
        return cls(
            ok=True,
            detail=detail,
            disposition=disposition,
            cost_usd=cost_usd,
            model_calls=model_calls,
            data=data or {},
        )

    @classmethod
    def failed(
        cls,
        detail: str,
        *,
        failure_class: FailureClass,
        cost_usd: Decimal = Decimal(0),
        model_calls: int = 0,
    ) -> StageOutcome:
        return cls(
            ok=False,
            detail=detail,
            failure_class=failure_class,
            cost_usd=cost_usd,
            model_calls=model_calls,
        )


# ------------------------------------------------------------- snapshots --
def build_snapshot(context: TrackContext) -> stages.TrackSnapshot:
    """Assemble everything :func:`select_stage` reads, from the store."""

    store = context.portfolio
    idea = store.require_idea(context.idea_id)
    version = store.require_version(context.idea_id)
    return stages.TrackSnapshot(
        status=idea.status,
        version=version,
        succeeded_stages=store.succeeded_stages_for_version(
            idea_id=context.idea_id, idea_version=version.version
        ),
        evidence=store.list_evidence(
            idea_id=context.idea_id, idea_version=version.version
        ),
        live_reviews=store.live_reviews(
            idea_id=context.idea_id,
            current_prompt_versions=CURRENT_REVIEW_PROMPTS,
            max_age_seconds=context.config.thresholds.review_max_age_seconds,
        ),
        open_objections=store.open_objections(idea_id=context.idea_id),
        revision_count=store.revision_count(context.idea_id),
        review_count=store.review_count(context.idea_id),
        lineage_active=store.lineage_active_counts(context.project_id).get(
            idea.lineage_root, 0
        ),
        depth_without_evidence=store.depth_without_evidence(context.idea_id),
    )


# ------------------------------------------------------------ model call --
def _ask(
    context: TrackContext,
    template: PromptTemplate,
    *,
    fields: Mapping[str, str] | None = None,
    blocks: Mapping[str, Sequence[str]] | None = None,
    independence_group: str | None = None,
) -> ModelResponse:
    """One typed model request, built from a template and answered by the router.

    Note what is not here: a provider, a model, or an effort. A stage that
    named one would have hard-coded model identity into the discovery
    semantics, which is what ``tests/test_runtime_layering.py`` checks for in
    the cycle graph and what the extended version of it checks for here.
    """

    request = ModelRequest(
        role=template.role,
        capability=template.capability,
        prompt=template.render(fields=fields, blocks=blocks),
        prompt_version=template.identity,
        criticality=template.criticality,
        independence=template.independence,
        independence_group=independence_group,
        json_schema=template.output_schema,
        max_cost_usd=float(
            context.config.cost_for(_STAGE_FOR_TEMPLATE.get(template.name, Stage.DEDUP))
        ),
    )
    return context.models.complete(request)


#: Which stage's ceiling each template's calls are charged against. A table
#: rather than a parameter, so a new template that forgets to say which stage
#: it belongs to is a missing key rather than an unbounded call.
_STAGE_FOR_TEMPLATE: dict[str, Stage] = {
    "portfolio_blind_explorer": Stage.DEDUP,
    "portfolio_seeded_explorer": Stage.DEDUP,
    "portfolio_failure_mining_explorer": Stage.DEDUP,
    "duplicate_adjudicator": Stage.DEDUP,
    "novelty_screen": Stage.NOVELTY_SCREEN,
    "falsifier": Stage.FALSIFY,
    "scientific_discovery": Stage.DISCOVER,
    "literature_scout": Stage.LITERATURE_AUDIT,
    "methodology_reviewer": Stage.REVIEW_BOARD,
    "novelty_reviewer": Stage.REVIEW_BOARD,
    "skeptic_reviewer": Stage.REVIEW_BOARD,
    "meta_reviewer": Stage.META_REVIEW,
    "replicator": Stage.REPLICATE,
    "brancher": Stage.BRANCH,
}


def _independence_group(context: TrackContext, version_digest: str) -> str:
    """The group the origin and every reviewer of one version share.

    Sharing it is what lets the router route a reviewer *away* from the family
    that produced the work. It is keyed by the content digest rather than by
    the idea, so a revision starts a fresh group -- otherwise the second
    version's reviewers would be routed away from families that answered about
    text nobody is reviewing any more.
    """

    return f"idea:{context.idea_id}:{version_digest}"


def _cost(response: ModelResponse) -> Decimal:
    return Decimal(str(response.cost_usd or 0))


# --------------------------------------------------------------- stages --
def run_dedup(context: TrackContext, snapshot: stages.TrackSnapshot) -> StageOutcome:
    """Layers one to three for free; layer four only if they could not decide."""

    store = context.portfolio
    outcome = pdedup.screen(
        store,
        project_id=context.project_id,
        fields={
            "title": snapshot.version.title,
            "research_question": snapshot.version.research_question,
            "core_idea": snapshot.version.core_idea,
        },
        config=context.config,
        exclude=context.idea_id,
        lineage_family=store.lineage_family(context.idea_id),
    )
    if outcome.is_duplicate and outcome.match_idea_id:
        return _record_duplicate(
            context, survivor=outcome.match_idea_id, detail=outcome.detail
        )
    if outcome.verdict is not pdedup.DedupVerdict.NEEDS_ADJUDICATION:
        return StageOutcome.succeeded(
            "nothing in this portfolio is close to it", disposition=Disposition.CONTINUE
        )

    supplied = tuple(idea_id for idea_id, _score in outcome.neighbours)
    neighbours = []
    for idea_id, score in outcome.neighbours:
        other = store.get_version(idea_id)
        if other is None:
            continue
        neighbours.append(
            f"id: {idea_id} (similarity {score:.2f})\n"
            f"question: {other.research_question}\ncore: {other.core_idea}"
        )
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["duplicate_adjudicator"],
            blocks={
                "candidate": packets.build_packet(
                    version=snapshot.version, evidence=(), objections=()
                ).idea_block(),
                "neighbours": neighbours,
            },
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the duplicate adjudicator could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the duplicate adjudicator returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        verdict = parse(
            DuplicateAdjudication,
            structured=response.structured,
            text=response.text,
            role="duplicate_adjudicator",
        )
        verdict.check(supplied=supplied)
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    if verdict.verdict == "distinct":
        return StageOutcome.succeeded(
            f"adjudicated distinct from {len(supplied)} close neighbour(s)",
            disposition=Disposition.CONTINUE,
            cost_usd=_cost(response),
            model_calls=1,
        )
    result = _record_duplicate(
        context,
        survivor=verdict.of_idea_id,
        detail=verdict.rationale or f"adjudicated {verdict.verdict}",
        kind=(
            EdgeKind.MERGED_FROM
            if verdict.verdict == "merge"
            else EdgeKind.DUPLICATE_OF
        ),
    )
    return StageOutcome.succeeded(
        result.detail,
        disposition=result.disposition,
        cost_usd=_cost(response),
        model_calls=1,
    )


def _record_duplicate(
    context: TrackContext,
    *,
    survivor: str,
    detail: str,
    kind: EdgeKind = EdgeKind.DUPLICATE_OF,
) -> StageOutcome:
    """Keep the duplicate as a reference. Nothing explored is deleted."""

    store = context.portfolio
    store.add_edge(
        parent_idea_id=survivor,
        child_idea_id=context.idea_id,
        kind=kind,
        detail=detail,
    )
    store.set_status(
        idea_id=context.idea_id,
        status=IdeaStatus.SUPERSEDED,
        retire_reason=f"{detail}; superseded by {survivor}",
    )
    return StageOutcome.succeeded(
        f"a restatement of {survivor}: {detail}", disposition=Disposition.DUPLICATE
    )


def run_novelty_screen(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """Cheap, and establishes nothing. The deep audit is what a gate reads."""

    retrieved: list[str] = []
    supplied_keys: tuple[str, ...] = ()
    if context.literature is not None:
        packet = context.literature.search(snapshot.version.research_question, limit=8)
        supplied_keys = tuple(getattr(packet, "work_keys", ()))
        retrieved = _render_literature(packet)
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["novelty_screen"],
            blocks={
                "candidate": packets.build_packet(
                    version=snapshot.version, evidence=(), objections=()
                ).idea_block(),
                "retrieved": retrieved,
            },
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the novelty screen could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the novelty screen returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        screen = parse(
            ScreenOutput,
            structured=response.structured,
            text=response.text,
            role="novelty_screen",
        )
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    # The screen's own opinion moves a *dimension*, which the allocator reads,
    # and never a status, which a gate reads. A screen that could demote an
    # idea would be a novelty judgement made without a retrieved source.
    _merge_dimensions(
        context,
        snapshot,
        QualityDimensions(novelty=0.2 if screen.likely_known else 0.7),
    )
    detail = (
        f"likely already known: {screen.nearest_known_work[:160]}"
        if screen.likely_known
        else "no obvious prior work in the cheap screen"
    )
    if not supplied_keys:
        detail += "; nothing was retrieved, so this rests on model recollection only"
    return StageOutcome.succeeded(
        detail,
        disposition=Disposition.CONTINUE,
        cost_usd=_cost(response),
        model_calls=1,
        data={"likely_known": screen.likely_known},
    )


def run_falsify(context: TrackContext, snapshot: stages.TrackSnapshot) -> StageOutcome:
    """The adversarial pass. Killing the idea here is the successful outcome."""

    packet = packets.build_packet(
        version=snapshot.version,
        evidence=snapshot.evidence,
        objections=snapshot.open_objections,
    )
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["falsifier"],
            blocks={
                "idea": packet.idea_block(),
                "established_facts": list(context.established_facts),
            },
            independence_group=_independence_group(
                context, snapshot.version.content_digest
            ),
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the falsifier could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the falsifier returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        result = parse(
            FalsifierOutput,
            structured=response.structured,
            text=response.text,
            role="falsifier",
        )
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    verdict = (
        ReviewVerdict.REJECT
        if result.worst is Severity.FATAL
        else ReviewVerdict.PASS_WITH_OBJECTIONS
        if result.objections
        else ReviewVerdict.PASS
    )
    review = _record_review(
        context,
        snapshot,
        role=ReviewerRole.FALSIFIER,
        template=PORTFOLIO_TEMPLATES["falsifier"],
        response=response,
        verdict=verdict,
        severity=result.worst,
        summary=result.summary,
        objections=[(item.severity, item.summary) for item in result.objections],
    )
    if result.worst is Severity.FATAL:
        context.portfolio.set_status(
            idea_id=context.idea_id,
            status=IdeaStatus.REJECTED,
            retire_reason=result.objections[0].summary,
        )
        return StageOutcome.succeeded(
            f"killed by the falsifier: {result.objections[0].summary[:160]}",
            disposition=Disposition.REJECT,
            cost_usd=_cost(response),
            model_calls=1,
        )
    return StageOutcome.succeeded(
        f"survived the falsifier with {len(result.objections)} objection(s); "
        f"attempted: {', '.join(result.attempted[:3]) or 'not stated'}",
        disposition=Disposition.CONTINUE,
        cost_usd=_cost(response),
        model_calls=1,
        data={"review_id": review},
    )


def run_discover(context: TrackContext, snapshot: stages.TrackSnapshot) -> StageOutcome:
    """Make the idea precise, or report that it cannot be made precise."""

    packet = packets.build_packet(
        version=snapshot.version,
        evidence=snapshot.evidence,
        objections=snapshot.open_objections,
    )
    blocks = {
        "candidate": packet.idea_block() + _objection_lines(packet),
        "established_facts": list(context.established_facts),
    }
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["scientific_discovery"],
            fields={"charter": context.charter},
            blocks=blocks,
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"scientific discovery could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"scientific discovery returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        result = parse(
            DiscoveryOutput,
            structured=response.structured,
            text=response.text,
            role="scientific_discovery",
        )
        result.check()
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    if not result.can_be_made_precise or result.refined is None:
        context.portfolio.set_status(
            idea_id=context.idea_id,
            status=IdeaStatus.PARKED,
            retire_reason=f"could not be made precise: {result.obstacle}",
            revisit_if=result.minimum_decisive_action
            or "the obstacle above is removed",
        )
        return StageOutcome.succeeded(
            f"could not be made precise: {result.obstacle[:160]}",
            disposition=Disposition.PARK,
            cost_usd=_cost(response),
            model_calls=1,
        )

    fields = result.refined.as_fields()
    fields["next_best_action"] = result.minimum_decisive_action or fields.get(
        "next_best_action", ""
    )
    # A revision claims to answer the objections it was shown. Claiming is not
    # resolving: `resolve_objection` still requires a later review by another
    # role to agree.
    claimed = tuple(item.objection_key for item in snapshot.open_objections)
    version = context.portfolio.append_version(
        idea_id=context.idea_id,
        fields={**fields, "adjudication_types": []},
        origin_call_id=response.call_id,
        origin_role=str(PORTFOLIO_TEMPLATES["scientific_discovery"].role),
        origin_stage=str(Stage.DISCOVER),
        dimensions=snapshot.version.dimensions.merged(result.refined.dimensions),
        addressed_objections=claimed,
    )
    return StageOutcome.succeeded(
        f"sharpened into version {version.version}",
        disposition=Disposition.REVISE,
        cost_usd=_cost(response),
        model_calls=1,
        data={"version": version.version},
    )


def run_adjudicate(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """Read the falsifier and record what kind of work would settle this.

    No model, no cost, and that is the point. Every quality gate is
    parameterised by the adjudication type, so it is computed by
    ``runtime.adjudication.classify`` from the sentence in which somebody
    already wrote down what would settle the thing.
    """

    declared = stages.classify_from_falsifier(snapshot.version)
    context.portfolio.set_adjudication_types(
        idea_id=context.idea_id,
        version=snapshot.version.version,
        types=[str(item) for item in declared],
    )
    return StageOutcome.succeeded(
        f"settled by {', '.join(str(item) for item in declared)}, read from the "
        f"falsifier",
        disposition=Disposition.CONTINUE,
        data={"adjudication_types": [str(item) for item in declared]},
    )


def run_literature_audit(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """The deep audit. Every row cites a work that was actually retrieved."""

    if context.literature is None:
        return StageOutcome.failed(
            "no literature source is configured, so novelty cannot be "
            "established. This idea will not reach VALIDATED, which is the "
            "correct outcome rather than a failure of the idea.",
            failure_class=FailureClass.CAPABILITY_DENIED,
        )
    packet = context.literature.search(snapshot.version.research_question, limit=12)
    supplied = tuple(getattr(packet, "work_keys", ()))
    if not supplied:
        return StageOutcome.failed(
            "the literature search returned nothing, so there is no source to "
            "audit against",
            failure_class=FailureClass.CAPABILITY_DENIED,
        )
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["literature_scout"],
            blocks={
                "proposal": packets.build_packet(
                    version=snapshot.version, evidence=(), objections=()
                ).idea_block(),
                "sources": _render_literature(packet),
            },
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the literature scout could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the literature scout returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        audit = parse(
            NoveltyAuditOutput,
            structured=response.structured,
            text=response.text,
            role="literature_scout",
        )
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    # The fail-closed citation rule, applied here rather than trusted to the
    # prompt. A key that was not supplied invalidates the whole report: the
    # statement it supported was reached some other way, and keeping the
    # statement while dropping its only stated ground leaves an ungrounded
    # claim looking grounded.
    invented = [row.source_key for row in audit.rows if row.source_key not in supplied]
    if invented:
        return StageOutcome.failed(
            f"the novelty matrix cites {len(invented)} work(s) that were not "
            f"retrieved ({', '.join(invented[:3])}). A claim about what is known "
            f"that rests on a paper nobody retrieved was reached some other way.",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    artifact = context.artifacts.put_bytes(
        audit.model_dump_json(indent=2).encode("utf-8"),
        media_type="application/json",
        role="novelty_matrix",
        producer=str(PORTFOLIO_TEMPLATES["literature_scout"].identity),
    )
    recorded = 0
    # What each matrix row *does to the idea*, which is not the same as what it
    # says about the literature:
    #
    #   same      a prior result already states this -> it CONTRADICTS the
    #             novelty claim, and that is the row that kills the idea;
    #   different the closest retrieved work is genuinely different -> it
    #             SUPPORTS the novelty claim, grounded in a source somebody
    #             actually fetched. A novelty claim is a claim about absence,
    #             and an absence found by a recorded search is evidence;
    #   partial   CONSISTENT_WITH, which satisfies nothing on its own.
    #
    # The first version mapped everything but `same` to CONSISTENT_WITH, so a
    # literature-adjudicated idea could never accumulate substantive evidence
    # and the evidence stage was selected forever. The integration test found
    # it as a track that would not advance.
    _POLARITY = {
        "same": EvidenceStrength.CONTRADICTS,
        "different": EvidenceStrength.SUPPORTS,
        "partial": EvidenceStrength.CONSISTENT_WITH,
    }
    for row in audit.rows:
        strength = _POLARITY[row.relation]
        context.portfolio.add_evidence(
            idea_id=context.idea_id,
            idea_version=snapshot.version.version,
            kind=EvidenceKind.LITERATURE,
            strength=strength,
            summary=f"{row.proposed_component} vs {row.closest_known_result} "
            f"({row.relation}): {row.precise_difference}",
            literature_key=row.source_key,
            artifact_id=artifact.artifact_id,
            source_call_id=response.call_id,
        )
        recorded += 1
    _merge_dimensions(
        context,
        snapshot,
        QualityDimensions(
            literature_confidence=min(1.0, len(audit.queries) / 6.0),
            novelty=0.1 if any(row.relation == "same" for row in audit.rows) else 0.8,
        ),
    )
    _begin_investigating(context)
    return StageOutcome.succeeded(
        f"{recorded} matrix row(s) from {len(set(audit.source_keys))} retrieved "
        f"source(s), over {len(audit.queries)} query(ies)",
        disposition=Disposition.CONTINUE,
        cost_usd=_cost(response),
        model_calls=1,
        data={"queries": list(audit.queries)},
    )


def run_evidence(context: TrackContext, snapshot: stages.TrackSnapshot) -> StageOutcome:
    """Get the evidence this kind of idea would be settled by.

    Dispatches on the adjudication type, which was read from the falsifier.
    Two of the four routes are honest refusals on a host that cannot do the
    work, and refusing is the right answer: an idea that cannot be settled here
    stops below VALIDATED and says why, rather than being validated on prose.
    """

    declared = set(snapshot.version.adjudication_types)
    # NOVELTY_OR_LITERATURE is deliberately absent from this dispatch. The
    # audit *is* that type's evidence, it is cheaper than every other route,
    # and `select_stage` therefore reaches it first -- so routing here as well
    # would mean the same work recorded under two stage names, and
    # `LITERATURE_AUDIT` would never register as having run.
    if declared & {AdjudicationType.EMPIRICAL, AdjudicationType.DIAGNOSTIC}:
        if not context.can_execute:
            return StageOutcome.failed(
                "this idea is settled by measurement and nothing on this host can "
                "execute anything. It stops here rather than being concluded from "
                "reasoning about what the measurement would have shown.",
                failure_class=FailureClass.CAPABILITY_DENIED,
            )
        return StageOutcome.failed(
            "the experiment pipeline is not wired into the idea track in this "
            "build; the objective cycle owns it. See docs/"
            "AUTONOMOUS_DISCOVERY_ARCHITECTURE.md §18.",
            failure_class=FailureClass.CAPABILITY_DENIED,
        )
    if AdjudicationType.MATHEMATICAL in declared:
        return StageOutcome.failed(
            "this idea is settled by derivation and by an executed counterexample "
            "search. The derivation path is not wired into the idea track in this "
            "build; `derive_mathematics` in the objective cycle owns it.",
            failure_class=FailureClass.CAPABILITY_DENIED,
        )
    return StageOutcome.failed(
        f"no evidence route for {sorted(str(item) for item in declared)}",
        failure_class=FailureClass.POLICY_REFUSED,
    )


def run_one_review(
    context: TrackContext,
    snapshot: stages.TrackSnapshot,
    role: ReviewerRole,
) -> StageOutcome:
    """One independent reading, from a frozen packet.

    Its own function because the track graph gives each reviewer a node. A
    crash after the second reviewer then resumes at the third instead of paying
    for all three again, which is the one measured property that makes
    LangGraph worth using here rather than a plain pipeline.
    """

    packet = packets.build_packet(
        version=snapshot.version,
        evidence=snapshot.evidence,
        objections=snapshot.open_objections,
    )
    templates = {
        ReviewerRole.METHODOLOGY: PORTFOLIO_TEMPLATES["methodology_reviewer"],
        ReviewerRole.NOVELTY: PORTFOLIO_TEMPLATES["novelty_reviewer"],
        ReviewerRole.SKEPTIC: PORTFOLIO_TEMPLATES["skeptic_reviewer"],
    }
    template = templates[role]
    # Each reviewer gets exactly the blocks its own template declares, and
    # nothing else. `PromptTemplate.render` refuses an undeclared block, so a
    # caller that assumed every reviewer takes `evidence` gets a PromptError
    # from inside a graph node -- which is how this was found. Building the
    # blocks from the template is also what keeps the novelty reviewer's
    # *additional* input, the matrix, from leaking to the other two.
    literature_lines = [
        summary
        for kind, _strength, summary in packet.evidence
        if kind == str(EvidenceKind.LITERATURE)
    ]
    available: dict[str, Sequence[str]] = {
        "packet": packet.idea_block(),
        "evidence": packet.evidence_block(),
        "novelty_matrix": literature_lines or ["(no novelty matrix has been produced)"],
        "sources": literature_lines or ["(no sources were retrieved)"],
    }
    blocks = {
        name: available[name] for name, _fence in template.blocks if name in available
    }
    try:
        response = _ask(
            context,
            template,
            blocks=blocks,
            independence_group=_independence_group(
                context, snapshot.version.content_digest
            ),
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"{role} could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"{role} returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        review = parse(
            ReviewOutput,
            structured=response.structured,
            text=response.text,
            role=str(role),
        )
        review.check()
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    review_id = _record_review(
        context,
        snapshot,
        role=role,
        template=template,
        response=response,
        verdict=review.verdict,
        severity=review.severity,
        summary=review.summary,
        objections=[(item.severity, item.summary) for item in review.objections],
        packet_digest=packet.digest,
    )
    _merge_dimensions(context, snapshot, review.dimensions)
    return StageOutcome.succeeded(
        f"{role}: {review.verdict} with {len(review.objections)} objection(s)",
        disposition=Disposition.CONTINUE,
        cost_usd=_cost(response),
        model_calls=1,
        data={"review_id": review_id, "role": str(role)},
    )


def run_review_board(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """The three independent readings, in sequence, from one frozen packet.

    Used when the board is dispatched as a single stage. The track graph
    instead gives each reviewer its own node and calls
    :func:`run_one_review`; both paths share the same recording, so a review
    written by one is indistinguishable from a review written by the other.
    """

    total = Decimal(0)
    calls = 0
    recorded: list[str] = []
    for role in snapshot.missing_review_roles:
        outcome = run_one_review(context, snapshot, role)
        total += outcome.cost_usd
        calls += outcome.model_calls
        if not outcome.ok:
            return StageOutcome.failed(
                outcome.detail,
                failure_class=outcome.failure_class
                or FailureClass.MODEL_OUTPUT_INVALID,
                cost_usd=total,
                model_calls=calls,
            )
        recorded.append(str(outcome.data.get("review_id", "")))

    finish_review_board(context, snapshot)
    return StageOutcome.succeeded(
        f"{len(recorded)} review(s) recorded",
        disposition=Disposition.CONTINUE,
        cost_usd=total,
        model_calls=calls,
        data={"reviews": recorded},
    )


def finish_review_board(context: TrackContext, snapshot: stages.TrackSnapshot) -> None:
    """What happens once every missing reviewer has answered.

    Separate from the reviewers so the graph can call it after its third node,
    and so that "the board is complete" is one statement rather than one per
    dispatch path.
    """

    _try_resolve_objections(context, _refresh(context, snapshot))
    if (
        context.portfolio.require_idea(context.idea_id).status
        is IdeaStatus.INVESTIGATING
    ):
        context.portfolio.set_status(idea_id=context.idea_id, status=IdeaStatus.REVIEW)


def _refresh(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> stages.TrackSnapshot:
    """Re-read what the reviewers just wrote, keeping the version fixed.

    The reviewers add reviews and objections, and objection resolution has to
    see them. The *version* is deliberately carried over rather than re-read:
    nothing in a review board changes the version, and re-reading it would make
    the function's behaviour depend on a concurrent revision.
    """

    store = context.portfolio
    return stages.TrackSnapshot(
        status=store.require_idea(context.idea_id).status,
        version=snapshot.version,
        succeeded_stages=snapshot.succeeded_stages,
        evidence=snapshot.evidence,
        live_reviews=store.live_reviews(
            idea_id=context.idea_id,
            current_prompt_versions=CURRENT_REVIEW_PROMPTS,
            max_age_seconds=context.config.thresholds.review_max_age_seconds,
        ),
        open_objections=store.open_objections(idea_id=context.idea_id),
        revision_count=snapshot.revision_count,
        review_count=store.review_count(context.idea_id),
        lineage_active=snapshot.lineage_active,
        depth_without_evidence=snapshot.depth_without_evidence,
    )


def run_meta_review(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """Synthesise, then let the gate decide what the synthesis is worth."""

    packet = packets.build_packet(
        version=snapshot.version,
        evidence=snapshot.evidence,
        objections=snapshot.open_objections,
    )
    review_lines = [
        f"[{item.reviewer_role}] {item.verdict} ({item.severity}) "
        f"[provider family {item.provider_family}, model {item.model}]: {item.summary}"
        for item in snapshot.live_reviews
    ]
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["meta_reviewer"],
            blocks={
                "packet": packet.idea_block(),
                "reviews": review_lines,
                "standing_objections": packet.objections_block(),
            },
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the meta-reviewer could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the meta-reviewer returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        meta = parse(
            MetaReviewOutput,
            structured=response.structured,
            text=response.text,
            role="meta_reviewer",
        )
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    _record_review(
        context,
        snapshot,
        role=ReviewerRole.META,
        template=PORTFOLIO_TEMPLATES["meta_reviewer"],
        response=response,
        verdict=ReviewVerdict.PASS
        if not meta.unresolved_disagreements
        else ReviewVerdict.PASS_WITH_OBJECTIONS,
        severity=Severity.NONE if not meta.unresolved_disagreements else Severity.MINOR,
        summary=meta.summary,
        objections=[
            (Severity.MINOR, f"unresolved disagreement: {item}")
            for item in meta.unresolved_disagreements
        ],
        recommendation=meta.recommendation,
        packet_digest=packet.digest,
    )
    result = _evaluate(context)
    allowed = gates.permit(meta.recommendation, result)
    _apply_disposition(context, allowed, result)
    detail = f"recommended {meta.recommendation}; the gate permits {allowed}"
    if allowed is not meta.recommendation:
        detail += f". Unmet: {'; '.join(result.unmet[:3])}"
    return StageOutcome.succeeded(
        detail,
        disposition=allowed,
        cost_usd=_cost(response),
        model_calls=1,
        data={
            "recommended": str(meta.recommendation),
            "permitted": str(allowed),
            "unmet": list(result.unmet),
            "board_independence": result.board_independence,
        },
    )


def run_replicate(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> StageOutcome:
    """Second-line verification along the line the adjudication type fixes."""

    packet = packets.build_packet(
        version=snapshot.version,
        evidence=snapshot.evidence,
        objections=(),
    )
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["replicator"],
            blocks={
                "question": [
                    f"research question: {packet.research_question}",
                    f"falsifier: {packet.falsifier}",
                ],
                # The original interpretation is deliberately not quoted: a
                # replicator that read the conclusion is a second opinion.
                "raw_outputs": [
                    f"[{kind}] {summary}"
                    for kind, _strength, summary in packet.evidence
                ],
            },
            independence_group=_independence_group(
                context, snapshot.version.content_digest
            ),
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the replicator could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the replicator returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        review = parse(
            ReviewOutput,
            structured=response.structured,
            text=response.text,
            role="replicator",
        )
        review.check()
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    _record_review(
        context,
        snapshot,
        role=ReviewerRole.REPLICATOR,
        template=PORTFOLIO_TEMPLATES["replicator"],
        response=response,
        verdict=review.verdict,
        severity=review.severity,
        summary=review.summary,
        objections=[(item.severity, item.summary) for item in review.objections],
        packet_digest=packet.digest,
    )
    agreed = review.verdict in {ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_OBJECTIONS}
    if agreed:
        # The reconstruction itself is stored and cited. `idea_evidence` refuses
        # a row with nothing under it, and a model call id alone does not count
        # -- deliberately, because otherwise any model's opinion would be
        # storable as evidence. What makes this a row rather than prose is that
        # the document a person would have to read to disagree with it exists,
        # by content hash.
        #
        # This still satisfies no gate on its own for a mathematical or an
        # empirical idea: both require an *execution*, and a reconstruction in
        # prose has no job.
        artifact = context.artifacts.put_bytes(
            review.model_dump_json(indent=2).encode("utf-8"),
            media_type="application/json",
            role="replication",
            producer=PORTFOLIO_TEMPLATES["replicator"].identity,
        )
        context.portfolio.add_evidence(
            idea_id=context.idea_id,
            idea_version=snapshot.version.version,
            kind=EvidenceKind.REPLICATION,
            strength=EvidenceStrength.SUPPORTS,
            summary=review.summary,
            source_call_id=response.call_id,
            artifact_id=artifact.artifact_id,
        )
    return StageOutcome.succeeded(
        "the reconstruction agreed" if agreed else "the reconstruction disagreed",
        disposition=Disposition.CONTINUE,
        cost_usd=_cost(response),
        model_calls=1,
    )


def run_branch(context: TrackContext, snapshot: stages.TrackSnapshot) -> StageOutcome:
    """Open the child directions a surviving idea suggests."""

    bounds = context.config.bounds
    room = max(0, bounds.max_active_per_lineage - snapshot.lineage_active)
    maximum = min(bounds.max_children_per_branch, room)
    if maximum <= 0:
        return StageOutcome.succeeded(
            "this lineage is already at its ceiling; no children opened",
            disposition=Disposition.CONTINUE,
        )
    packet = packets.build_packet(
        version=snapshot.version, evidence=snapshot.evidence, objections=()
    )
    try:
        response = _ask(
            context,
            PORTFOLIO_TEMPLATES["brancher"],
            fields={"maximum_children": str(maximum)},
            blocks={"parent": packet.idea_block(), "evidence": packet.evidence_block()},
        )
    except ProviderCallFailedError as exc:
        return StageOutcome.failed(
            f"the brancher could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    if not response.ok:
        return StageOutcome.failed(
            f"the brancher returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )
    try:
        branch = parse(
            BranchOutput,
            structured=response.structured,
            text=response.text,
            role="brancher",
        )
        branch.check(maximum=maximum)
    except ContractError as exc:
        return StageOutcome.failed(
            str(exc),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            cost_usd=_cost(response),
            model_calls=1,
        )

    opened: list[str] = []
    for child, relation in zip(branch.children, branch.relations, strict=True):
        created, _version = context.portfolio.create_idea(
            project_id=context.project_id,
            origin=IdeaOrigin.BRANCH,
            fields=child.as_fields(),
            parent_idea_id=context.idea_id,
            edge_kind=EdgeKind(relation),
            edge_detail=f"branched from {context.idea_id}",
            origin_call_id=response.call_id,
            origin_role=str(PORTFOLIO_TEMPLATES["brancher"].role),
            origin_stage=str(Stage.BRANCH),
            dimensions=child.dimensions,
        )
        opened.append(created.idea_id)
    return StageOutcome.succeeded(
        f"opened {len(opened)} child direction(s)",
        disposition=Disposition.BRANCH,
        cost_usd=_cost(response),
        model_calls=1,
        data={"children": opened},
    )


# -------------------------------------------------------------- helpers --
def _render_literature(packet: Any) -> list[str]:
    entries = getattr(packet, "entries", ())
    lines: list[str] = []
    for entry in entries:
        work = entry.work
        lines.append(
            f"key: {work.key}\ntitle: {getattr(work, 'title', '')}\n"
            f"abstract: {getattr(work, 'abstract', '') or entry.excerpt}"
        )
    return lines


def _objection_lines(packet: packets.ReviewPacket) -> list[str]:
    if not packet.standing_objections:
        return []
    return ["standing objections this revision must answer:"] + [
        f"[{severity}] {summary}" for severity, summary in packet.standing_objections
    ]


def _record_review(
    context: TrackContext,
    snapshot: stages.TrackSnapshot,
    *,
    role: ReviewerRole,
    template: PromptTemplate,
    response: ModelResponse,
    verdict: ReviewVerdict,
    severity: Severity,
    summary: str,
    objections: Sequence[tuple[Severity, str]],
    recommendation: Disposition | None = None,
    packet_digest: str | None = None,
) -> str:
    """Write one review, its independence, and its objections.

    The independence class is *computed here* from the two model calls, rather
    than reported by anything that could be wrong about it. A review whose call
    is the origin call raises; see
    :func:`research_os.portfolio.gates.classify_independence`.
    """

    from research_os.automation.providers import provider_family

    origin_call = None
    if snapshot.version.origin_call_id:
        origin_call = next(
            (
                item
                for item in context.runtime.list_model_calls(run_id=None, limit=500)
                if item.call_id == snapshot.version.origin_call_id
            ),
            None,
        )
    independence = gates.classify_independence(
        origin_call_id=snapshot.version.origin_call_id,
        review_call_id=response.call_id,
        origin_provider_family=(
            provider_family(origin_call.provider) if origin_call else None
        ),
        review_provider_family=provider_family(response.provider),
        origin_model=origin_call.model if origin_call else None,
        review_model=response.model,
        frozen_packet=True,
    )
    digest = (
        packet_digest
        or packets.build_packet(
            version=snapshot.version,
            evidence=snapshot.evidence,
            objections=snapshot.open_objections,
        ).digest
    )
    review, _created = context.portfolio.record_review(
        idea_id=context.idea_id,
        idea_version=snapshot.version.version,
        reviewer_role=role,
        verdict=verdict,
        severity=severity,
        summary=summary,
        recommendation=recommendation,
        reviewed_content_digest=snapshot.version.content_digest,
        reviewed_evidence_digest=context.portfolio.evidence_digest(
            idea_id=context.idea_id, idea_version=snapshot.version.version
        ),
        packet_digest=digest,
        prompt_version=template.identity,
        provider=response.provider,
        provider_family=provider_family(response.provider),
        model=response.model,
        independence_vs_origin=independence,
        context_class=str(ContextClass.FROZEN_PACKET),
        independence_note=response.independence_note,
        call_id=response.call_id,
    )
    for objection_severity, objection_summary in objections:
        context.portfolio.raise_objection(
            idea_id=context.idea_id,
            review_id=review.review_id,
            raised_at_version=snapshot.version.version,
            severity=objection_severity,
            summary=objection_summary,
        )
    return review.review_id


def _try_resolve_objections(
    context: TrackContext, snapshot: stages.TrackSnapshot
) -> int:
    """Close objections a later version claimed and a different role did not re-raise.

    Deliberately conservative and deliberately not the producer's decision. An
    objection is resolved only when: a version after the one that raised it
    named it in ``addressed_objections``; a review of that later version exists
    by a role other than the raiser's; and none of that version's live reviews
    raised the same objection key again.
    """

    store = context.portfolio
    version = snapshot.version
    resolved = 0
    live_keys = {
        item.objection_key
        for item in store.open_objections(idea_id=context.idea_id)
        if item.raised_at_version == version.version
    }
    for objection in store.open_objections(idea_id=context.idea_id):
        if objection.raised_at_version >= version.version:
            continue
        if objection.objection_key not in version.addressed_objections:
            continue
        if objection.objection_key in live_keys:
            continue
        raiser = next(
            (
                item
                for item in store.list_reviews(idea_id=context.idea_id)
                if item.review_id == objection.raised_in_review
            ),
            None,
        )
        candidates = [
            item
            for item in snapshot.live_reviews
            if raiser is None or item.reviewer_role is not raiser.reviewer_role
        ]
        if not candidates:
            continue
        store.resolve_objection(
            objection_id=objection.objection_id,
            addressed_at_version=version.version,
            response=(
                f"answered in version {version.version} and not re-raised by "
                f"{candidates[0].reviewer_role}"
            ),
            resolved_by_review=candidates[0].review_id,
        )
        resolved += 1
    return resolved


def _begin_investigating(context: TrackContext) -> None:
    """Move PROMISING to INVESTIGATING once evidence has actually been gathered.

    The one transition nothing else performs, and leaving it out was a real
    defect: ``STAGE_MINIMUM_STATUS`` gates the review board at
    ``INVESTIGATING``, so an idea that stayed PROMISING accumulated evidence
    and was never reviewed -- the track simply ran out of stages and reported
    that everything its state called for had run. The integration test found it
    as an idea that reached the end without a single review.

    Status is moved by evidence arriving, not by a model saying the idea is
    being investigated.
    """

    if context.portfolio.require_idea(context.idea_id).status is IdeaStatus.PROMISING:
        context.portfolio.set_status(
            idea_id=context.idea_id, status=IdeaStatus.INVESTIGATING
        )


def _merge_dimensions(
    context: TrackContext,
    snapshot: stages.TrackSnapshot,
    assessed: QualityDimensions,
) -> None:
    """Fold newly assessed dimensions into the current version, in place.

    ``dimensions`` is immaterial -- it is not in ``content_digest`` -- so
    writing it changes no review's binding. It is the one column on an
    otherwise append-only table that is updated, and it is updated because the
    alternative is a new version per assessment, which would stale every review
    for a number nobody reviewed.
    """

    merged = snapshot.version.dimensions.merged(assessed)
    with context.portfolio.db.tx() as conn:
        conn.execute(
            "update idea_versions set dimensions = %s "
            "where idea_id = %s and version = %s",
            (
                __import__("research_os.runtime.db", fromlist=["jsonb"]).jsonb(
                    merged.model_dump()
                ),
                context.idea_id,
                snapshot.version.version,
            ),
        )


def _evaluate(context: TrackContext) -> gates.GateResult:
    store = context.portfolio
    version = store.require_version(context.idea_id)
    return gates.evaluate(
        version=version,
        live_reviews=store.live_reviews(
            idea_id=context.idea_id,
            current_prompt_versions=CURRENT_REVIEW_PROMPTS,
            max_age_seconds=context.config.thresholds.review_max_age_seconds,
        ),
        objections=store.open_objections(idea_id=context.idea_id),
        evidence=store.list_evidence(
            idea_id=context.idea_id, idea_version=version.version
        ),
        succeeded_stages=store.succeeded_stages_for_version(
            idea_id=context.idea_id, idea_version=version.version
        ),
        config=context.config,
    )


_STATUS_FOR_DISPOSITION: dict[Disposition, IdeaStatus] = {
    Disposition.PROMISING: IdeaStatus.PROMISING,
    Disposition.VALIDATED: IdeaStatus.VALIDATED,
    Disposition.HUMAN_READY: IdeaStatus.HUMAN_READY,
}


def _apply_disposition(
    context: TrackContext, disposition: Disposition, result: gates.GateResult
) -> None:
    """Move the idea's status, but only where the gate already agreed.

    ``disposition`` has been through :func:`gates.permit`, so this cannot
    promote past what the rows support. The assertion is kept anyway, because
    the one function between a model's opinion and a scientific-sounding status
    is worth defending twice.
    """

    status = _STATUS_FOR_DISPOSITION.get(disposition)
    if status is None:
        return
    wanted = {
        IdeaStatus.PROMISING: QualityTier.PROMISING,
        IdeaStatus.VALIDATED: QualityTier.VALIDATED,
        IdeaStatus.HUMAN_READY: QualityTier.HUMAN_READY,
    }[status]
    if not result.at_least(wanted):  # pragma: no cover - permit() prevents it
        raise AssertionError(
            f"refusing to set {status} on a gate result of {result.tier}"
        )
    context.portfolio.set_status(idea_id=context.idea_id, status=status)


#: The closed dispatch table. A stage with no handler is refused when the track
#: is assembled, not discovered half way through.
STAGE_HANDLERS: dict[
    Stage, Callable[[TrackContext, stages.TrackSnapshot], StageOutcome]
] = {
    Stage.DEDUP: run_dedup,
    Stage.NOVELTY_SCREEN: run_novelty_screen,
    Stage.FALSIFY: run_falsify,
    Stage.DISCOVER: run_discover,
    Stage.ADJUDICATE: run_adjudicate,
    Stage.EVIDENCE: run_evidence,
    Stage.LITERATURE_AUDIT: run_literature_audit,
    Stage.REVIEW_BOARD: run_review_board,
    Stage.META_REVIEW: run_meta_review,
    Stage.REPLICATE: run_replicate,
    Stage.BRANCH: run_branch,
}
