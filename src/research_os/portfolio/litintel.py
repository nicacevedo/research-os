"""Literature intelligence: the published record as a persistent, cited resource.

The shared index (``research_os.literature``) already discovers, retrieves,
deduplicates, stores and ranks *works*. What the portfolio lacked was a
memory of what those works *say* for the questions it asks -- every audit
read the index afresh and kept only a matrix artifact -- and any way for an
idea to ask the record a precise question, or for the record to raise a
question of its own.

Four functions, one module, no crawler:

``answer_request``
    An idea asks a precise question (a frontier request of kind
    ``LITERATURE``). Discovery runs first when this deployment can reach the
    providers -- ``LiteratureService.retrieve`` ingesting into the shared
    index, the existing A0 retrieval -- then the index is searched, a reader
    answers from the packet alone, and ordinary code verifies every citation
    and every quotation before anything is stored. Verified claims become
    literature claims; those bearing on the idea become its evidence; a
    disagreement or a gap becomes a frontier request for new ideas.

``verify``
    The part that makes a claim a claim about the literature rather than a
    model's recollection: every key was supplied in the packet, and a quoted
    excerpt appears in the cited source's stored text.

literature-driven discovery
    Gaps, disagreements, limitations and open questions are what the
    literature explorer reads (``runner._literature_claims``), and what a
    follow-up explorer is asked about when one is raised against an idea.

``watch``
    Scheduled, through the existing schedule machinery: a watch pass raises
    targeted requests for the project's live ideas. Not enabled by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from research_os.portfolio import packets
from research_os.portfolio.contracts import ContractError, LiteratureAnswer, parse
from research_os.portfolio.frontier import FrontierContext, raise_request
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaStatus,
    LiteratureClaim,
    OperationalState,
    RequestBasis,
    RequestKind,
    RequestState,
    Stage,
)
from research_os.portfolio.prompts import TEMPLATES as PORTFOLIO_TEMPLATES
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.routing import ProviderCallFailedError

LOG = logging.getLogger("research_os.portfolio.litintel")

#: The work kind the allocator buys for one open literature request.
LITERATURE_REQUEST = "portfolio_literature"
#: The work kind a literature watch schedule produces.
LITERATURE_WATCH = "portfolio_literature_watch"
LITERATURE_WATCH_EVENT = "LITERATURE_WATCH_DUE"

#: How many retrieved works one answer reads.
MAX_WORKS = 12

_STRENGTH = {
    "SUPPORTS": EvidenceStrength.SUPPORTS,
    "CONTRADICTS": EvidenceStrength.CONTRADICTS,
    "CONSISTENT_WITH": EvidenceStrength.CONSISTENT_WITH,
}


class LiteratureRetriever(Protocol):
    """Discovery: bring works matching a query into the shared index.

    Injected, like :class:`research_os.portfolio.runner.LiteratureSource`, so a
    deployment without provider access is a reported capability rather than
    a crash, and a deterministic test needs no credentials.
    """

    def retrieve(self, query: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AnswerResult:
    ok: bool
    detail: str
    claims: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    raised: tuple[str, ...] = ()
    cost_usd: Decimal = Decimal(0)
    model_calls: int = 0
    failure_class: FailureClass | None = None


# ------------------------------------------------------------- verify --
def normalised(text: str) -> str:
    """Whitespace-folded, NFKC, casefolded -- for quotation matching only."""

    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def source_texts(packet: Any) -> dict[str, str]:
    """Each supplied work's stored text, by key: title, abstract, excerpt."""

    found: dict[str, str] = {}
    for entry in getattr(packet, "entries", ()):
        work = entry.work
        found[str(work.key)] = " ".join(
            str(part or "")
            for part in (
                getattr(work, "title", ""),
                getattr(work, "abstract", ""),
                getattr(entry, "excerpt", ""),
            )
        )
    return found


def verify(answer: LiteratureAnswer, packet: Any) -> list[str]:
    """Every reason this reading cannot be stored. Empty means it can.

    Fail-closed, like ``literature.analyst.parse_literature_report``: one
    invented key or one quotation not found in its source invalidates the
    whole reading, because the statements around it were reached the same
    way and there is no telling which.
    """

    texts = source_texts(packet)
    problems: list[str] = []
    for item in [*answer.claims, *answer.disagreements, *answer.gaps]:
        invented = [key for key in item.work_keys if key not in texts]
        if invented:
            problems.append(
                f"cites {', '.join(invented[:3])}, which was not retrieved for this question"
            )
            continue
        excerpt = getattr(item, "excerpt", "")
        if excerpt and not any(
            normalised(excerpt) in normalised(texts[key]) for key in item.work_keys
        ):
            problems.append(
                f"quotes {excerpt[:80]!r}, which does not appear in "
                f"{', '.join(item.work_keys)}"
            )
    return problems


def claim_digest(
    project_id: str, kind: str, statement: str, keys: Sequence[str]
) -> str:
    payload = json.dumps(
        {
            "project": project_id,
            "kind": kind,
            "statement": normalised(statement),
            "keys": sorted(set(keys)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "pclaim-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- answer --
def answer_request(
    context: FrontierContext,
    request_id: str,
    *,
    literature: Any | None,
    retriever: LiteratureRetriever | None = None,
) -> AnswerResult:
    """Answer one targeted literature request from retrieved sources only."""

    store = context.portfolio
    request = store.require_request(request_id)
    if request.state is not RequestState.OPEN:
        return AnswerResult(ok=True, detail=f"{request_id} is already {request.state}")
    idea = store.get_idea(request.source_idea_id) if request.source_idea_id else None
    discovered = ""
    if retriever is not None:
        try:
            summary = retriever.retrieve(request.question)
            discovered = f"; discovery ingested {summary.get('ingested', 0)} work(s)"
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            discovered = f"; discovery could not run ({exc})"
    if literature is None:
        _release(store, idea)
        store.close_request(
            request_id,
            state=RequestState.DECLINED,
            resolution=(
                "no literature index is available on this deployment, so the "
                "question cannot be answered from retrieved sources" + discovered
            ),
        )
        return AnswerResult(ok=True, detail="declined: no literature source")
    packet = literature.search(request.question, limit=MAX_WORKS)
    keys = tuple(getattr(packet, "work_keys", ()))
    if not keys:
        _release(store, idea)
        store.close_request(
            request_id,
            state=RequestState.DECLINED,
            resolution="nothing was retrieved for this question" + discovered,
        )
        return AnswerResult(ok=True, detail="declined: nothing retrieved")

    template = PORTFOLIO_TEMPLATES["literature_reader"]
    idea_block = ["(no idea asked this; it is a watch of the published record)"]
    if idea is not None:
        version = store.require_version(
            idea.idea_id, request.source_version or idea.current_version
        )
        idea_block = packets.build_packet(
            version=version, evidence=(), objections=()
        ).idea_block()
    sources = [
        f"key: {entry.work.key}\ntitle: {getattr(entry.work, 'title', '')}\n"
        f"abstract: {getattr(entry.work, 'abstract', '') or getattr(entry, 'excerpt', '')}"
        for entry in getattr(packet, "entries", ())
    ]
    try:
        response = context.models.complete(
            ModelRequest(
                role=template.role,
                capability=template.capability,
                prompt=template.render(
                    blocks={
                        "question": [request.question],
                        "idea": idea_block,
                        "sources": sources,
                    }
                ),
                prompt_version=template.identity,
                criticality=template.criticality,
                independence=template.independence,
                json_schema=template.output_schema,
                max_cost_usd=float(context.config.cost_for(Stage.LITERATURE_AUDIT)),
            )
        )
    except ProviderCallFailedError as exc:
        return AnswerResult(
            ok=False,
            detail=f"the literature reader could not be reached: {exc}",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )
    cost = Decimal(str(response.cost_usd or 0))
    try:
        if not response.ok:
            raise ContractError(f"nothing usable: {response.error}")
        answer = parse(
            LiteratureAnswer,
            structured=response.structured,
            text=response.text,
            role=template.name,
        )
        problems = verify(answer, packet)
        if problems:
            raise ContractError(
                "the reading was not stored because it is not grounded in what "
                "was retrieved: " + "; ".join(problems[:3])
            )
    except ContractError as exc:
        attempts = store.count_request_attempt(request_id)
        if attempts >= context.config.bounds.max_stage_failures:
            _release(store, idea)
            store.close_request(
                request_id,
                state=RequestState.DECLINED,
                resolution=f"the reader failed {attempts} times: {exc}",
            )
            return AnswerResult(
                ok=True,
                detail="declined after repeated failures",
                cost_usd=cost,
                model_calls=1,
            )
        return AnswerResult(
            ok=False,
            detail=str(exc),
            cost_usd=cost,
            model_calls=1,
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    artifact = context.artifacts.put_bytes(
        json.dumps(
            {
                "schema": "portfolio-literature-answer-v1",
                "request_id": request_id,
                "question": request.question,
                "supplied_keys": list(keys),
                "answer": answer.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
        media_type="application/json",
        role="literature_answer",
        producer=template.identity,
    )
    texts = source_texts(packet)
    claims: list[str] = []
    evidence: list[str] = []
    raised: list[str] = []
    stored = [(item.kind, item, item.relation_to_idea) for item in answer.claims]
    stored += [("DISAGREEMENT", item, "NONE") for item in answer.disagreements]
    stored += [("GAP", item, "NONE") for item in answer.gaps]
    for kind, item, relation in stored:
        excerpt = getattr(item, "excerpt", "")
        claim = store.record_literature_claim(
            project_id=context.project_id,
            kind=str(kind),
            statement=item.statement,
            work_keys=tuple(item.work_keys),
            excerpt=excerpt,
            verification="QUOTED" if excerpt else "CITED",
            query=request.question,
            request_id=request_id,
            idea_id=idea.idea_id if idea is not None else None,
            source_call_id=response.call_id,
            artifact_id=artifact.artifact_id,
            digest=claim_digest(
                context.project_id, str(kind), item.statement, item.work_keys
            ),
        )
        claims.append(claim.claim_id)
        if idea is not None and relation in _STRENGTH and _open(idea):
            row = store.add_evidence(
                idea_id=idea.idea_id,
                idea_version=request.source_version or idea.current_version,
                kind=EvidenceKind.LITERATURE,
                strength=_STRENGTH[relation],
                summary=f"claim {claim.claim_id} [{kind}]: {item.statement}"[:4_000],
                literature_key=item.work_keys[0],
                artifact_id=artifact.artifact_id,
                source_call_id=response.call_id,
            )
            evidence.append(row.evidence_id)
        if kind in {"DISAGREEMENT", "GAP"}:
            # The record raising a question of its own. Recorded as a request
            # for new ideas, with the claim as its source, so the child's
            # provenance names the verified statement it came from.
            request_row = raise_request(
                store,
                project_id=context.project_id,
                basis=RequestBasis.LITERATURE,
                source_ref=claim.claim_id,
                question=(
                    f"The literature {'disagrees' if kind == 'DISAGREEMENT' else 'leaves a gap'}: "
                    f"{item.statement}"
                ),
                source_idea_id=idea.idea_id if idea is not None else None,
                source_version=request.source_version if idea is not None else None,
                detail=f"sources: {', '.join(item.work_keys)}",
            )
            raised.append(request_row.request_id)
    del texts
    _release(store, idea)
    store.close_request(
        request_id,
        state=RequestState.CONSUMED,
        resolution=(
            f"{len(claims)} verified claim(s), {len(evidence)} evidence row(s), "
            f"{len(raised)} new question(s){discovered}"
        ),
        resolved_by=response.call_id,
    )
    return AnswerResult(
        ok=True,
        detail=f"answered from {len(keys)} retrieved work(s): {answer.answer[:200]}",
        claims=tuple(claims),
        evidence=tuple(evidence),
        raised=tuple(raised),
        cost_usd=cost,
        model_calls=1,
    )


def _open(idea: Any) -> bool:
    return idea.status not in {IdeaStatus.REJECTED, IdeaStatus.SUPERSEDED}


def _release(store: Any, idea: Any | None) -> None:
    """An idea waiting on this request may be allocated again."""

    if idea is None:
        return
    current = store.require_idea(idea.idea_id)
    if current.operational_state is OperationalState.BLOCKED_DEPENDENCY:
        store.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)


def ask(
    store: Any,
    *,
    project_id: str,
    idea_id: str,
    version: int,
    question: str,
    source_ref: str,
    basis: RequestBasis = RequestBasis.REVIEWER_CRITICISM,
    wait: bool = False,
) -> Any:
    """An idea asks the literature a precise question. Idempotent per source."""

    request = raise_request(
        store,
        project_id=project_id,
        kind=RequestKind.LITERATURE,
        basis=basis,
        source_ref=source_ref,
        question=question,
        source_idea_id=idea_id,
        source_version=version,
    )
    if wait and request.state is RequestState.OPEN:
        store.set_operational_state(
            idea_id=idea_id, state=OperationalState.BLOCKED_DEPENDENCY
        )
    return request


# -------------------------------------------------------------- watch --
def watch(store: Any, *, project_id: str, bucket: str, limit: int = 5) -> list[str]:
    """One watch pass: a targeted request for each of the liveliest ideas.

    Deterministic and model-free, like the tick: it decides which questions
    to put to the record, and the requests it raises are answered by the
    ordinary bounded route. ``bucket`` (a date) makes a pass idempotent.
    """

    raised: list[str] = []
    live = store.list_ideas(
        project_id=project_id,
        statuses=[IdeaStatus.PROMISING, IdeaStatus.INVESTIGATING, IdeaStatus.REVIEW],
        limit=limit,
    )
    for idea in live:
        version = store.require_version(idea.idea_id)
        request = raise_request(
            store,
            project_id=project_id,
            kind=RequestKind.LITERATURE,
            basis=RequestBasis.LITERATURE,
            source_ref=f"watch:{bucket}:{idea.idea_id}",
            question=version.research_question,
            source_idea_id=idea.idea_id,
            source_version=version.version,
            detail="a scheduled watch of the published record",
        )
        raised.append(request.request_id)
    return raised


def ensure_watch(db: Any, *, project_id: str, interval_seconds: int) -> str:
    """Put this project's literature watch on the runtime's schedule table."""

    from research_os.runtime.store import RuntimeStore

    runtime = RuntimeStore(db)
    for existing in runtime.list_schedules():
        if (
            existing.project_id == project_id
            and existing.kind == LITERATURE_WATCH_EVENT
        ):
            return existing.schedule_id
    return runtime.create_schedule(
        project_id=project_id,
        kind=LITERATURE_WATCH_EVENT,
        interval_seconds=interval_seconds,
        payload={"project_id": project_id},
    ).schedule_id


def claims_for_explorer(
    store: Any, project_id: str, *, limit: int = 24
) -> list[LiteratureClaim]:
    """The claims that point at the edge of the record, for the literature explorer."""

    return list(
        store.list_literature_claims(
            project_id=project_id, frontier_only=True, limit=limit
        )
    )


__all__ = [
    "LITERATURE_REQUEST",
    "LITERATURE_WATCH",
    "LITERATURE_WATCH_EVENT",
    "AnswerResult",
    "LiteratureRetriever",
    "answer_request",
    "ask",
    "claim_digest",
    "ensure_watch",
    "verify",
    "watch",
]
