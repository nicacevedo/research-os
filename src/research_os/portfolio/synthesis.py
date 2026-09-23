"""Evidence synthesis and its referee: writing from the record, and back to the frontier.

**What the writer is given** is built here, deterministically, from rows:
the ideas the portfolio's own gates have raised to ``VALIDATED`` or
``HUMAN_READY``, their current version's evidence (with the frozen contract
each measurement was read under), and the verified literature claims tied to
them. Chat history is not an input; nothing a model said about the evidence
is -- only the evidence rows and the claims, each with an identifier.

**What the writer may say** is checked by ordinary code before anything is
stored, fail-closed: every citation must name a supplied identifier; a
finding must cite a row of evidence that bears on it (``SUPPORTS`` or
``CONTRADICTS``), not merely a claim about the literature; a novelty
statement must cite the literature; and every number in a finding or an
interpretation must appear in the text of what it cites. A draft failing any
of these is refused whole, for the reason the literature reader's is: the
statements around the bad one were reached the same way.

**What the writer may ask for** is structured: an evidence gap names the idea
and the question, and becomes a frontier request -- a new idea to measure, or
a literature request -- so missing evidence is new work rather than prose.

**The referee** reads the same packet and the draft's statements -- never
the writer's reasoning -- and returns typed findings: an unsupported claim, a
missing control, over-interpretation, a novelty claim the literature does not
bear out, missing literature, a reproducibility or methodological weakness,
an inconsistency between a statement and its evidence. A finding that asks a
question becomes a frontier request; one that names missing literature
becomes a literature request. **The referee approves nothing.** Its verdict
is advisory, a synthesis is at most ``REFEREED``, and no idea's status, no
capsule object and no Review is touched here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research_os.portfolio.contracts import (
    ContractError,
    RefereeReport,
    SynthesisDraft,
    parse,
)
from research_os.portfolio.frontier import FrontierContext, raise_request
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    RequestBasis,
    RequestKind,
    Stage,
)
from research_os.portfolio.prompts import TEMPLATES as PORTFOLIO_TEMPLATES
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.routing import ProviderCallFailedError

LOG = logging.getLogger("research_os.portfolio.synthesis")

#: The work kind the allocator buys when the reviewed evidence changed.
SYNTHESIZE = "portfolio_synthesize"

#: Statuses whose evidence the writer may synthesise: the ones this system's
#: own gates have passed. Neither is a claim a person accepted.
SYNTHESIZED_STATUSES: tuple[IdeaStatus, ...] = (
    IdeaStatus.VALIDATED,
    IdeaStatus.HUMAN_READY,
)

#: Numbers that appear in ordinary prose about anything and are not results.
_TRIVIAL = frozenset({str(n) for n in range(11)})
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.-])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")


@dataclass(frozen=True, slots=True)
class Packet:
    """What the writer and the referee read. Rows, with identifiers."""

    ideas: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Mapping[str, Any]]
    claims: Mapping[str, Mapping[str, Any]]
    basis_digest: str

    @property
    def supplied(self) -> set[str]:
        return (
            {item["idea_id"] for item in self.ideas}
            | set(self.evidence)
            | set(self.claims)
        )

    def lines(self) -> list[str]:
        rendered: list[str] = []
        for idea in self.ideas:
            rendered.append(
                f"idea {idea['idea_id']} [{idea['status']}; board independence "
                f"{idea['board_independence']}]: {idea['research_question']}"
            )
            for evidence_id in idea["evidence_ids"]:
                item = self.evidence[evidence_id]
                rendered.append(
                    f"  evidence {evidence_id} [{item['kind']}/{item['strength']}]: "
                    f"{item['summary']}"
                )
            for claim_id in idea["claim_ids"]:
                item = self.claims[claim_id]
                rendered.append(
                    f"  literature {claim_id} [{item['kind']}; sources "
                    f"{', '.join(item['work_keys'])}]: {item['statement']}"
                )
        return rendered


def build_packet(store: Any, project_id: str, *, limit: int = 12) -> Packet | None:
    """The reviewed record of one project, or ``None`` when there is none."""

    from research_os.portfolio import gates

    ideas: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    claims: dict[str, dict[str, Any]] = {}
    for idea in store.list_ideas(
        project_id=project_id, statuses=list(SYNTHESIZED_STATUSES), limit=limit
    ):
        version = store.require_version(idea.idea_id)
        rows = store.list_evidence(idea_id=idea.idea_id, idea_version=version.version)
        linked = store.list_literature_claims(
            project_id=project_id, idea_id=idea.idea_id
        )
        for row in rows:
            evidence[row.evidence_id] = {
                "idea_id": idea.idea_id,
                "kind": str(row.kind),
                "strength": str(row.strength),
                "summary": row.summary,
                "literature_key": row.literature_key,
                "job_id": row.job_id,
            }
        for claim in linked:
            claims[claim.claim_id] = {
                "idea_id": idea.idea_id,
                "kind": str(claim.kind),
                "statement": claim.statement,
                "work_keys": list(claim.work_keys),
            }
        ideas.append(
            {
                "idea_id": idea.idea_id,
                "status": str(idea.status),
                "research_question": version.research_question,
                "content_digest": version.content_digest,
                "board_independence": gates.board_independence(
                    store.live_reviews(idea_id=idea.idea_id)
                ),
                "evidence_ids": [row.evidence_id for row in rows],
                "claim_ids": [claim.claim_id for claim in linked],
            }
        )
    if not ideas:
        return None
    basis = json.dumps(
        {
            "ideas": sorted(
                (item["idea_id"], item["content_digest"]) for item in ideas
            ),
            "evidence": sorted(evidence),
            "claims": sorted(claims),
        },
        sort_keys=True,
    )
    digest = "psyn-basis-v1:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return Packet(
        ideas=tuple(ideas), evidence=evidence, claims=claims, basis_digest=digest
    )


# -------------------------------------------------------------- ground --
def grounding_problems(draft: SynthesisDraft, packet: Packet) -> list[str]:
    """Every reason this draft cannot be stored. Empty means it can."""

    supplied = packet.supplied
    problems: list[str] = []
    for statement in draft.statements:
        invented = [item for item in statement.cites if item not in supplied]
        if invented:
            problems.append(
                f"{statement.statement_id} cites {', '.join(invented[:3])}, which was "
                f"not supplied"
            )
            continue
        cited_evidence = [
            packet.evidence[item] for item in statement.cites if item in packet.evidence
        ]
        cited_claims = [
            packet.claims[item] for item in statement.cites if item in packet.claims
        ]
        if statement.kind == "FINDING" and not any(
            item["strength"]
            in {str(EvidenceStrength.SUPPORTS), str(EvidenceStrength.CONTRADICTS)}
            for item in cited_evidence
        ):
            problems.append(
                f"{statement.statement_id} is a finding and cites no evidence that "
                f"bears on it; a claim about the literature or a consistent-with "
                f"row is not a finding's ground"
            )
        if statement.kind == "NOVELTY" and not (
            cited_claims
            or any(
                item["kind"] == str(EvidenceKind.LITERATURE) for item in cited_evidence
            )
        ):
            problems.append(
                f"{statement.statement_id} claims novelty and cites no literature; "
                f"whether something is new is a question about the published record"
            )
        if statement.kind in {"FINDING", "INTERPRETATION"}:
            vocabulary = " ".join(
                [item["summary"] for item in cited_evidence]
                + [item["statement"] for item in cited_claims]
            )
            known = set(_NUMBER.findall(vocabulary))
            unsupported = sorted(
                {
                    token
                    for token in _NUMBER.findall(statement.text)
                    if token not in known and token not in _TRIVIAL
                }
            )
            if unsupported:
                problems.append(
                    f"{statement.statement_id} states {', '.join(unsupported)}, which "
                    f"appear in nothing it cites"
                )
    idea_ids = {item["idea_id"] for item in packet.ideas}
    for request in draft.evidence_requests:
        if request.idea_id not in idea_ids:
            problems.append(
                f"an evidence request names {request.idea_id}, which was not supplied"
            )
    return problems


# ---------------------------------------------------------------- run --
@dataclass(frozen=True, slots=True)
class SynthesisResult:
    ok: bool
    detail: str
    synthesis_id: str | None = None
    raised: tuple[str, ...] = ()
    cost_usd: Decimal = Decimal(0)
    model_calls: int = 0
    failure_class: FailureClass | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def _call(
    context: FrontierContext,
    template: Any,
    blocks: Mapping[str, Sequence[str]],
    group: str | None = None,
) -> Any:
    return context.models.complete(
        ModelRequest(
            role=template.role,
            capability=template.capability,
            prompt=template.render(blocks=blocks),
            prompt_version=template.identity,
            criticality=template.criticality,
            independence=template.independence,
            independence_group=group,
            json_schema=template.output_schema,
            max_cost_usd=float(context.config.cost_for(Stage.REVIEW_BOARD)),
        )
    )


def synthesize(context: FrontierContext) -> SynthesisResult:
    """Write (if not yet written for this basis) and referee one synthesis."""

    store = context.portfolio
    packet = build_packet(store, context.project_id)
    if packet is None:
        return SynthesisResult(ok=True, detail="no reviewed evidence to synthesise yet")
    existing = store.synthesis_for_basis(
        project_id=context.project_id, basis_digest=packet.basis_digest
    )
    cost = Decimal(0)
    calls = 0
    raised: list[str] = []
    group = f"synthesis:{context.project_id}:{packet.basis_digest}"
    if existing is None:
        writer = PORTFOLIO_TEMPLATES["synthesis_writer"]
        try:
            response = _call(context, writer, {"record": packet.lines()}, group)
        except ProviderCallFailedError as exc:
            return SynthesisResult(
                ok=False,
                detail=f"the synthesis writer could not be reached: {exc}",
                failure_class=FailureClass.PROVIDER_UNAVAILABLE,
            )
        cost += Decimal(str(response.cost_usd or 0))
        calls += 1
        try:
            if not response.ok:
                raise ContractError(f"nothing usable: {response.error}")
            draft = parse(
                SynthesisDraft,
                structured=response.structured,
                text=response.text,
                role=writer.name,
            )
            problems = grounding_problems(draft, packet)
            if problems:
                raise ContractError(
                    "the draft was not stored because it is not grounded in the "
                    "record it was given: " + "; ".join(problems[:4])
                )
        except ContractError as exc:
            return SynthesisResult(
                ok=False,
                detail=str(exc),
                cost_usd=cost,
                model_calls=calls,
                failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            )
        document = {
            "schema": "portfolio-synthesis-v1",
            "project_id": context.project_id,
            "basis_digest": packet.basis_digest,
            "ideas": [dict(item) for item in packet.ideas],
            "draft": draft.model_dump(mode="json"),
            "grounding": {
                "checked": [
                    "every citation names a supplied identifier",
                    "a finding cites evidence that bears on it",
                    "a novelty statement cites the literature",
                    "every number in a finding or interpretation appears in what it cites",
                ],
                "problems": [],
            },
            "produced_autonomously": True,
            "human_evaluated": False,
        }
        ref = context.artifacts.put_text(
            json.dumps(document, indent=2, sort_keys=True),
            media_type="application/json",
            role="portfolio_synthesis",
            producer=writer.identity,
        )
        existing = store.create_synthesis(
            project_id=context.project_id,
            basis_digest=packet.basis_digest,
            document_artifact_id=ref.artifact_id,
            writer_call_id=response.call_id,
            statements=len(draft.statements),
        )
        for index, request in enumerate(draft.evidence_requests):
            row = raise_request(
                store,
                project_id=context.project_id,
                kind=RequestKind.LITERATURE
                if request.kind == "literature"
                else RequestKind.FOLLOW_UP,
                basis=RequestBasis.EVIDENCE_GAP,
                source_ref=f"{existing.synthesis_id}:gap:{index}",
                question=request.question,
                source_idea_id=request.idea_id,
                detail=f"the synthesis writer found a gap: {request.reason}",
            )
            raised.append(row.request_id)
    if existing.state.name == "REFEREED":
        return SynthesisResult(
            ok=True,
            detail=f"{existing.synthesis_id} is already refereed",
            synthesis_id=existing.synthesis_id,
        )

    document = json.loads(context.artifacts.get_text(existing.document_artifact_id))
    draft = SynthesisDraft.model_validate(document["draft"])
    referee = PORTFOLIO_TEMPLATES["synthesis_referee"]
    statements = [
        f"{item.statement_id} [{item.kind}; cites {', '.join(item.cites) or 'nothing'}]: {item.text}"
        for item in draft.statements
    ]
    try:
        response = _call(
            context,
            referee,
            {"record": packet.lines(), "statements": statements},
            group,
        )
    except ProviderCallFailedError as exc:
        return SynthesisResult(
            ok=False,
            detail=f"the referee could not be reached: {exc}",
            synthesis_id=existing.synthesis_id,
            raised=tuple(raised),
            cost_usd=cost,
            model_calls=calls,
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    cost += Decimal(str(response.cost_usd or 0))
    calls += 1
    known_statements = {item.statement_id: item for item in draft.statements}
    idea_ids = {item["idea_id"] for item in packet.ideas}
    try:
        if not response.ok:
            raise ContractError(f"nothing usable: {response.error}")
        report = parse(
            RefereeReport,
            structured=response.structured,
            text=response.text,
            role=referee.name,
        )
        for finding in report.findings:
            unknown = [
                item for item in finding.statement_ids if item not in known_statements
            ]
            if unknown or (finding.idea_id and finding.idea_id not in idea_ids):
                raise ContractError(
                    f"{finding.finding_id} names {unknown or finding.idea_id}, which the "
                    f"referee was not shown"
                )
    except ContractError as exc:
        return SynthesisResult(
            ok=False,
            detail=str(exc),
            synthesis_id=existing.synthesis_id,
            raised=tuple(raised),
            cost_usd=cost,
            model_calls=calls,
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )
    ref = context.artifacts.put_text(
        json.dumps(
            {
                "schema": "portfolio-synthesis-referee-v1",
                "synthesis_id": existing.synthesis_id,
                "report": report.model_dump(mode="json"),
                "independence_note": response.independence_note,
                "grants_approval": False,
            },
            indent=2,
            sort_keys=True,
        ),
        media_type="application/json",
        role="portfolio_synthesis_referee",
        producer=referee.identity,
    )
    for finding in report.findings:
        source_idea = finding.idea_id or next(
            (
                idea
                for statement_id in finding.statement_ids
                for idea in known_statements[statement_id].ideas
                if idea in idea_ids
            ),
            None,
        )
        missing_literature = finding.kind == "MISSING_LITERATURE"
        question = finding.follow_up_question or (
            f"What published work bears on: {finding.summary}"
            if missing_literature
            else ""
        )
        if not question:
            continue
        row = raise_request(
            store,
            project_id=context.project_id,
            kind=RequestKind.LITERATURE
            if missing_literature
            else RequestKind.FOLLOW_UP,
            basis=RequestBasis.REFEREE_FINDING,
            source_ref=f"{existing.synthesis_id}:{finding.finding_id}",
            question=question,
            source_idea_id=source_idea,
            detail=f"[{finding.severity} {finding.kind}] {finding.summary}",
        )
        raised.append(row.request_id)
    store.referee_synthesis(
        existing.synthesis_id,
        referee_artifact_id=ref.artifact_id,
        referee_call_id=response.call_id,
        verdict=report.verdict,
        findings=len(report.findings),
    )
    return SynthesisResult(
        ok=True,
        detail=(
            f"{existing.synthesis_id}: {len(draft.statements)} grounded statement(s), "
            f"refereed {report.verdict} with {len(report.findings)} finding(s); "
            f"{len(raised)} request(s) back to the frontier"
        ),
        synthesis_id=existing.synthesis_id,
        raised=tuple(raised),
        cost_usd=cost,
        model_calls=calls,
    )


def due(store: Any, project_id: str) -> str | None:
    """The basis a synthesis is owed for, or ``None``. Deterministic; no model."""

    packet = build_packet(store, project_id)
    if packet is None:
        return None
    existing = store.synthesis_for_basis(
        project_id=project_id, basis_digest=packet.basis_digest
    )
    if existing is not None and existing.state.name == "REFEREED":
        return None
    return packet.basis_digest


__all__ = [
    "SYNTHESIZE",
    "SYNTHESIZED_STATUSES",
    "Packet",
    "SynthesisResult",
    "build_packet",
    "due",
    "grounding_problems",
    "synthesize",
]
