"""Cross-object validation over an explicitly supplied object collection.

Pydantic models enforce local field semantics. This module enforces
relationships among already-parsed objects. It does not discover projects,
read ``.research/``, or consult SQLite.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from research_os.digests import subject_digest
from research_os.errors import (
    E_ACCEPTED_WITHOUT_EVIDENCE,
    E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
    E_CREATED_FROM_CYCLE,
    E_DANGLING_REF,
    E_DUP_ID,
    E_NONQUALIFYING_EVIDENCE,
    E_REVIEW_OF_REVIEW,
    E_STALE_REVIEW_DIGEST,
    E_SUPERSEDED_WITHOUT_SUCCESSOR,
    E_SUPERSESSION_CYCLE,
    E_WITHDRAWN_SUPERSEDED,
    E_WRONG_REF_TYPE,
    W_PROMOTED_WITHOUT_HYPOTHESIS,
    W_STALE_SUBJECT_DIGEST,
    Finding,
    Severity,
    ValidationReport,
)
from research_os.ids import PREFIX_TO_TYPE, parse_id
from research_os.models import (
    BaseScientificObject,
    Claim,
    ClaimStatus,
    Decision,
    Evidence,
    EvidenceKind,
    EvidenceStatus,
    Experiment,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    Idea,
    IdeaStatus,
    ObjectEnvelope,
    ObjectType,
    Review,
    Reviewable,
    ReviewerKind,
    ReviewStatus,
    ScientificObject,
    Verdict,
)

_TYPED_REFS: tuple[tuple[type, str, str | None], ...] = (
    (Hypothesis, "addresses", ObjectType.QUESTION),
    (Hypothesis, "assumptions", ObjectType.ASSUMPTION),
    (Hypothesis, "supporting_evidence", ObjectType.EVIDENCE),
    (Hypothesis, "contrary_evidence", ObjectType.EVIDENCE),
    (Claim, "evidence", ObjectType.EVIDENCE),
    (Claim, "hypotheses", ObjectType.HYPOTHESIS),
    (Experiment, "hypotheses", ObjectType.HYPOTHESIS),
    (Decision, "related", None),
    (Evidence, "experiment", ObjectType.EXPERIMENT),
)

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def is_qualifying_evidence(
    evidence: Evidence,
    objects: Mapping[str, ScientificObject],
) -> bool:
    """Return whether ``evidence`` can justify a terminal/accepted assessment."""

    if evidence.status is not EvidenceStatus.ACTIVE:
        return False
    if evidence.kind is EvidenceKind.EXPERIMENT:
        experiment = objects.get(evidence.experiment or "")
        if not isinstance(experiment, Experiment):
            return False
        if experiment.status is not ExperimentStatus.COMPLETED:
            return False
    return True


def validate_objects(objects: Iterable[ScientificObject]) -> ValidationReport:
    """Validate relationships among already-parsed scientific objects."""

    items = list(objects)
    findings: list[Finding] = []
    by_id: dict[str, ScientificObject] = {}

    for obj in items:
        if obj.id in by_id:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_DUP_ID,
                    message=f"duplicate object id {obj.id}",
                    object_id=obj.id,
                )
            )
            continue
        by_id[obj.id] = obj

    _check_created_from(by_id, findings)
    _check_supersession(by_id, findings)
    _check_typed_references(by_id, findings)
    _check_reviews(by_id, findings)
    _check_evidence_gates(by_id, findings)
    _check_promoted_ideas(by_id, findings)

    return ValidationReport(findings=_sorted_findings(findings))


def _check_created_from(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    graph: dict[str, list[str]] = {obj_id: [] for obj_id in by_id}
    for obj in by_id.values():
        for ref in obj.created_from:
            _resolve_ref(
                obj,
                field="created_from",
                reference=ref,
                expected_type=None,
                by_id=by_id,
                findings=findings,
            )
            if ref in by_id:
                graph[obj.id].append(ref)
    _emit_cycles(graph, E_CREATED_FROM_CYCLE, "created_from", findings)


def _check_supersession(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    graph: dict[str, list[str]] = {obj_id: [] for obj_id in by_id}
    successors: dict[str, list[str]] = defaultdict(list)

    for obj in by_id.values():
        if not isinstance(obj, BaseScientificObject):
            continue
        for ref in obj.supersedes:
            _resolve_ref(
                obj,
                field="supersedes",
                reference=ref,
                expected_type=obj.type,
                by_id=by_id,
                findings=findings,
            )
            target = by_id.get(ref)
            if target is None:
                continue
            graph[obj.id].append(ref)
            successors[ref].append(obj.id)
            if str(target.status) == "withdrawn":
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_WITHDRAWN_SUPERSEDED,
                        message=(
                            f"{obj.id} supersedes withdrawn object {ref}; "
                            "withdrawn means abandoned without replacement"
                        ),
                        object_id=obj.id,
                        field="supersedes",
                        reference=ref,
                    )
                )

    for obj in by_id.values():
        if str(obj.status) == "superseded" and not successors.get(obj.id):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_SUPERSEDED_WITHOUT_SUCCESSOR,
                    message=f"{obj.id} is superseded but has no successor",
                    object_id=obj.id,
                    field="supersedes",
                )
            )

    _emit_cycles(graph, E_SUPERSESSION_CYCLE, "supersedes", findings)


def _check_typed_references(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    for obj in by_id.values():
        if isinstance(obj, Evidence) and obj.kind is not EvidenceKind.EXPERIMENT:
            continue
        for cls, field_name, expected in _TYPED_REFS:
            if not isinstance(obj, cls):
                continue
            if cls is Evidence and field_name == "experiment":
                value = obj.experiment
                refs = [] if value is None else [value]
            else:
                raw = getattr(obj, field_name)
                refs = [] if raw is None else list(raw)
            for ref in refs:
                _resolve_ref(
                    obj,
                    field=field_name,
                    reference=ref,
                    expected_type=expected,
                    by_id=by_id,
                    findings=findings,
                )


def _check_reviews(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    for obj in by_id.values():
        if not isinstance(obj, Review):
            continue
        subject = by_id.get(obj.subject)
        if subject is None:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_DANGLING_REF,
                    message=f"{obj.id} subject {obj.subject} does not exist",
                    object_id=obj.id,
                    field="subject",
                    reference=obj.subject,
                )
            )
            continue
        if isinstance(subject, Review):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_REVIEW_OF_REVIEW,
                    message=f"{obj.id} cannot review another review",
                    object_id=obj.id,
                    field="subject",
                    reference=obj.subject,
                )
            )
            continue
        if not isinstance(subject, Reviewable):
            continue
        current = subject_digest(subject)
        if (
            obj.status is ReviewStatus.CONCLUDED
            and obj.subject_digest is not None
            and obj.subject_digest != current
        ):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_STALE_SUBJECT_DIGEST,
                    message=(
                        f"{obj.id} subject_digest no longer matches {obj.subject}"
                    ),
                    object_id=obj.id,
                    field="subject_digest",
                    reference=obj.subject,
                )
            )


def _check_evidence_gates(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    for obj in by_id.values():
        if isinstance(obj, Claim):
            _check_claim_gates(obj, by_id, findings)
        elif isinstance(obj, Hypothesis):
            _check_hypothesis_gates(obj, by_id, findings)


def _check_claim_gates(
    claim: Claim,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    if claim.status not in {ClaimStatus.EVIDENCE_LINKED, ClaimStatus.ACCEPTED}:
        return
    refs = list(claim.evidence or [])
    qualifying = [ref for ref in refs if _id_qualifies(ref, by_id)]
    if claim.status is ClaimStatus.ACCEPTED and not refs:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_ACCEPTED_WITHOUT_EVIDENCE,
                message=f"{claim.id} is accepted without evidence",
                object_id=claim.id,
                field="evidence",
            )
        )
    elif not qualifying:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_NONQUALIFYING_EVIDENCE,
                message=(
                    f"{claim.id} status {claim.status} requires at least one "
                    "qualifying active evidence reference"
                ),
                object_id=claim.id,
                field="evidence",
            )
        )
    if claim.status is ClaimStatus.ACCEPTED:
        _check_claim_review_gate(claim, by_id, findings)


def _check_claim_review_gate(
    claim: Claim,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    current = subject_digest(claim)
    stale_human_approve = False
    for obj in by_id.values():
        if not isinstance(obj, Review):
            continue
        if obj.subject != claim.id:
            continue
        if obj.status is not ReviewStatus.CONCLUDED:
            continue
        if obj.verdict is not Verdict.APPROVE:
            continue
        if obj.reviewer_kind is not ReviewerKind.HUMAN:
            continue
        if obj.subject_digest == current:
            return
        stale_human_approve = True
    if stale_human_approve:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_STALE_REVIEW_DIGEST,
                message=(
                    f"{claim.id} is accepted but its human approval review "
                    "does not match the current subject digest"
                ),
                object_id=claim.id,
                field="subject_digest",
            )
        )
        return
    findings.append(
        Finding(
            severity=Severity.ERROR,
            code=E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
            message=(
                f"{claim.id} is accepted without a concluded human approve "
                "review bound to the current subject digest"
            ),
            object_id=claim.id,
        )
    )


def _check_hypothesis_gates(
    hypothesis: Hypothesis,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    status = hypothesis.status
    if status is HypothesisStatus.SUPPORTED:
        refs = list(hypothesis.supporting_evidence or [])
        field_name = "supporting_evidence"
        ok = any(_id_qualifies(ref, by_id) for ref in refs)
    elif status is HypothesisStatus.REJECTED:
        refs = list(hypothesis.contrary_evidence or [])
        field_name = "contrary_evidence"
        ok = any(_id_qualifies(ref, by_id) for ref in refs)
    elif status is HypothesisStatus.INCONCLUSIVE:
        refs = list(hypothesis.supporting_evidence or []) + list(
            hypothesis.contrary_evidence or []
        )
        field_name = "supporting_evidence"
        ok = any(_id_qualifies(ref, by_id) for ref in refs)
    else:
        return
    if not ok:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_NONQUALIFYING_EVIDENCE,
                message=(
                    f"{hypothesis.id} status {status} requires at least one "
                    "qualifying active evidence reference"
                ),
                object_id=hypothesis.id,
                field=field_name,
            )
        )


def _check_promoted_ideas(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    for obj in by_id.values():
        if not isinstance(obj, Idea) or obj.status is not IdeaStatus.PROMOTED:
            continue
        linked = any(
            isinstance(other, Hypothesis) and obj.id in other.created_from
            for other in by_id.values()
        )
        if not linked:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_PROMOTED_WITHOUT_HYPOTHESIS,
                    message=(
                        f"{obj.id} is promoted but no hypothesis lists it in "
                        "created_from"
                    ),
                    object_id=obj.id,
                )
            )


def _id_qualifies(evidence_id: str, by_id: Mapping[str, ScientificObject]) -> bool:
    obj = by_id.get(evidence_id)
    if not isinstance(obj, Evidence):
        return False
    return is_qualifying_evidence(obj, by_id)


def _resolve_ref(
    obj: ObjectEnvelope,
    *,
    field: str,
    reference: str,
    expected_type: str | None,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
) -> None:
    target = by_id.get(reference)
    if target is None:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_DANGLING_REF,
                message=f"{obj.id} {field} references missing id {reference}",
                object_id=obj.id,
                field=field,
                reference=reference,
            )
        )
        return
    actual = target.type
    if expected_type is None:
        return
    if actual != expected_type:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_WRONG_REF_TYPE,
                message=(
                    f"{obj.id} {field} references {reference} of type "
                    f"{actual}, expected {expected_type}"
                ),
                object_id=obj.id,
                field=field,
                reference=reference,
            )
        )
        return
    prefix_type = PREFIX_TO_TYPE[parse_id(reference).prefix]
    if prefix_type != expected_type:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_WRONG_REF_TYPE,
                message=(
                    f"{obj.id} {field} references {reference} with type "
                    f"{prefix_type}, expected {expected_type}"
                ),
                object_id=obj.id,
                field=field,
                reference=reference,
            )
        )


def _emit_cycles(
    graph: Mapping[str, Sequence[str]],
    code: str,
    field: str,
    findings: list[Finding],
) -> None:
    for cycle in _directed_cycles(graph):
        rendered = " -> ".join([*cycle, cycle[0]])
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=code,
                message=f"{field} cycle: {rendered}",
                object_id=cycle[0],
                field=field,
            )
        )


def _directed_cycles(graph: Mapping[str, Sequence[str]]) -> list[tuple[str, ...]]:
    cycles: set[tuple[str, ...]] = set()
    visiting: list[str] = []
    visiting_set: set[str] = set()
    done: set[str] = set()

    def dfs(node: str) -> None:
        visiting.append(node)
        visiting_set.add(node)
        for neighbor in graph.get(node, ()):
            if neighbor not in graph:
                continue
            if neighbor in visiting_set:
                start = visiting.index(neighbor)
                cycles.add(_normalize_cycle(visiting[start:]))
            elif neighbor not in done:
                dfs(neighbor)
        visiting.pop()
        visiting_set.remove(node)
        done.add(node)

    for node in sorted(graph):
        if node not in done:
            dfs(node)
    return sorted(cycles)


def _normalize_cycle(nodes: Sequence[str]) -> tuple[str, ...]:
    rotated = min(range(len(nodes)), key=lambda index: nodes[index])
    return tuple(nodes[rotated:] + nodes[:rotated])


def _sorted_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                _SEVERITY_ORDER[item.severity],
                item.object_id or "",
                item.code,
                item.field or "",
                item.reference or "",
                item.message,
            ),
        )
    )
