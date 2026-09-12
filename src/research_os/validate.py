"""Cross-object validation over an explicitly supplied object collection.

Pydantic models enforce local field semantics. This module enforces
relationships among already-parsed objects. It does not discover projects,
read ``.research/``, or consult SQLite.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from research_os.digests import subject_digest
from research_os.errors import (
    E_ACCEPTED_WITHOUT_EVIDENCE,
    E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
    E_CREATED_FROM_CYCLE,
    E_DANGLING_REF,
    E_DUP_ID,
    E_EVIDENCE_DIGEST_UNLINKED,
    E_EVIDENCE_DIGESTS_INCOMPLETE,
    E_EXPERIMENT_DIGEST_UNLINKED,
    E_EXPERIMENT_DIGESTS_INCOMPLETE,
    E_NONQUALIFYING_EVIDENCE,
    E_REVIEW_OF_REVIEW,
    E_STALE_REVIEW_DIGEST,
    E_SUPERSEDED_WITHOUT_SUCCESSOR,
    E_SUPERSEDES_NON_SUPERSEDED,
    E_SUPERSESSION_CYCLE,
    E_WITHDRAWN_SUPERSEDED,
    E_WRONG_REF_TYPE,
    W_PROMOTED_WITHOUT_HYPOTHESIS,
    W_STALE_EVIDENCE_DIGEST,
    W_STALE_EXPERIMENT_DIGEST,
    W_STALE_SUBJECT_DIGEST,
    Finding,
    Severity,
    ValidationReport,
)
from research_os.ids import PREFIX_TO_TYPE, parse_id, validate_project_id
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
    (Claim, "supporting_evidence", ObjectType.EVIDENCE),
    (Claim, "contrary_evidence", ObjectType.EVIDENCE),
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


def referenced_experiments(
    claim: Claim,
    objects: Mapping[str, ScientificObject],
) -> frozenset[str]:
    """Return the Experiments a Claim reaches through its linked Evidence.

    The set a Claim review must bind. Keyed by Experiment id, so an Experiment
    reached through several Evidence objects -- or through one supporting and
    one contrary Evidence -- is bound exactly once, matching how a doubly
    referenced Evidence object is bound once in ``evidence_digests``. Polarity
    and Evidence status are both ignored: a review examined the Experiment
    either way, and the qualification rules live elsewhere.

    An Evidence id that does not resolve contributes nothing, because its
    ``E_DANGLING_REF`` is already reported against the Claim. A resolvable
    Evidence object naming a *missing* Experiment does contribute, so the
    binding cannot silently shrink to fit a broken pointer.
    """

    found: set[str] = set()
    for evidence_id in _linked_evidence(claim):
        target = objects.get(evidence_id)
        if not isinstance(target, Evidence):
            continue
        if target.kind is not EvidenceKind.EXPERIMENT:
            continue
        if target.experiment:
            found.add(target.experiment)
    return frozenset(found)


def _linked_evidence(claim: Claim) -> frozenset[str]:
    """Return every Evidence id a claim links, in either polarity."""

    return frozenset(claim.supporting_evidence or []) | frozenset(
        claim.contrary_evidence or []
    )


def _evidence_digests_match(
    stored: Mapping[str, str],
    by_id: Mapping[str, ScientificObject],
    project_id: str,
) -> bool:
    """Return whether every stored evidence digest matches current content.

    An unresolvable evidence id cannot match; the claim's own reference check
    already reports it as ``E_DANGLING_REF``.
    """

    for evidence_id, digest in stored.items():
        target = by_id.get(evidence_id)
        if not isinstance(target, Evidence):
            return False
        if digest != subject_digest(target, project_id=project_id):
            return False
    return True


def _experiment_digests_match(
    stored: Mapping[str, str],
    by_id: Mapping[str, ScientificObject],
    project_id: str,
) -> bool:
    """Return whether every stored experiment digest matches current content.

    An unresolvable experiment id cannot match; the referring Evidence object's
    own reference check already reports it as ``E_DANGLING_REF``.
    """

    for experiment_id, digest in stored.items():
        target = by_id.get(experiment_id)
        if not isinstance(target, Experiment):
            return False
        if digest != subject_digest(target, project_id=project_id):
            return False
    return True


def validate_objects(
    objects: Iterable[ScientificObject],
    *,
    project_id: str,
) -> ValidationReport:
    """Validate relationships among already-parsed scientific objects.

    ``project_id`` is required and validated. Reviewed scientific identity is
    project-local, so there is deliberately no call shape that validates
    claim acceptance while leaving project-scoped digest binding out of the
    picture. Callers pass project identity explicitly; this module never
    looks it up.
    """

    validate_project_id(project_id)
    items = list(objects)
    findings: list[Finding] = []
    grouped: dict[str, list[ScientificObject]] = defaultdict(list)
    for obj in items:
        grouped[obj.id].append(obj)

    by_id: dict[str, ScientificObject] = {}
    for obj_id in sorted(grouped):
        group = grouped[obj_id]
        if len(group) != 1:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_DUP_ID,
                    message=f"duplicate object id {obj_id}",
                    object_id=obj_id,
                )
            )
            continue
        by_id[obj_id] = group[0]

    _check_created_from(by_id, findings)
    _check_supersession(by_id, findings)
    _check_typed_references(by_id, findings)
    _check_reviews(by_id, findings, project_id)
    _check_evidence_gates(by_id, findings, project_id)
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
            target_status = str(target.status)
            if target_status == "withdrawn":
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
            elif target_status != "superseded":
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_SUPERSEDES_NON_SUPERSEDED,
                        message=(
                            f"{obj.id} supersedes {ref} whose status is "
                            f"{target_status}, not superseded"
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
    project_id: str,
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
        current = subject_digest(subject, project_id=project_id)
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
        _check_review_evidence_digests(obj, subject, by_id, findings, project_id)
        _check_review_experiment_digests(obj, subject, by_id, findings, project_id)


def _check_review_evidence_digests(
    review: Review,
    subject: ScientificObject,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
    project_id: str,
) -> None:
    """Bind a review to the Evidence content it recorded having examined.

    The map is explicit and inspectable rather than a recursive hash of the
    subject's referenced objects.
    """

    if review.evidence_digests is None or not isinstance(subject, Claim):
        return
    linked = _linked_evidence(subject)
    for evidence_id in sorted(review.evidence_digests):
        stored = review.evidence_digests[evidence_id]
        if evidence_id not in linked:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_EVIDENCE_DIGEST_UNLINKED,
                    message=(
                        f"{review.id} evidence_digests names {evidence_id}, "
                        f"which {subject.id} does not link"
                    ),
                    object_id=review.id,
                    field="evidence_digests",
                    reference=evidence_id,
                )
            )
            continue
        target = by_id.get(evidence_id)
        if not isinstance(target, Evidence):
            continue
        if review.status is not ReviewStatus.CONCLUDED:
            continue
        if stored != subject_digest(target, project_id=project_id):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_STALE_EVIDENCE_DIGEST,
                    message=(
                        f"{review.id} evidence digest for {evidence_id} no "
                        "longer matches that evidence"
                    ),
                    object_id=review.id,
                    field="evidence_digests",
                    reference=evidence_id,
                )
            )


def _check_review_experiment_digests(
    review: Review,
    subject: ScientificObject,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
    project_id: str,
) -> None:
    """Bind a review to the Experiment content behind its experiment evidence.

    The Claim -> Evidence binding covers an experiment-kind Evidence object's
    own statement and pointers, but its digest carries the Experiment only as an
    id. This map closes that hop explicitly and flatly: the reviewed Experiment
    content is named and hashed here, never folded recursively into the Evidence
    or Claim digest.
    """

    if review.experiment_digests is None or not isinstance(subject, Claim):
        return
    required = referenced_experiments(subject, by_id)
    for experiment_id in sorted(review.experiment_digests):
        stored = review.experiment_digests[experiment_id]
        if experiment_id not in required:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_EXPERIMENT_DIGEST_UNLINKED,
                    message=(
                        f"{review.id} experiment_digests names {experiment_id}, "
                        f"which {subject.id} does not reach through linked "
                        "experiment evidence"
                    ),
                    object_id=review.id,
                    field="experiment_digests",
                    reference=experiment_id,
                )
            )
            continue
        target = by_id.get(experiment_id)
        if not isinstance(target, Experiment):
            continue
        if review.status is not ReviewStatus.CONCLUDED:
            continue
        if stored != subject_digest(target, project_id=project_id):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_STALE_EXPERIMENT_DIGEST,
                    message=(
                        f"{review.id} experiment digest for {experiment_id} no "
                        "longer matches that experiment"
                    ),
                    object_id=review.id,
                    field="experiment_digests",
                    reference=experiment_id,
                )
            )


def _check_evidence_gates(
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
    project_id: str,
) -> None:
    for obj in by_id.values():
        if isinstance(obj, Claim):
            _check_claim_gates(obj, by_id, findings, project_id)
        elif isinstance(obj, Hypothesis):
            _check_hypothesis_gates(obj, by_id, findings)


def _check_claim_gates(
    claim: Claim,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
    project_id: str,
) -> None:
    if claim.status not in {ClaimStatus.EVIDENCE_LINKED, ClaimStatus.ACCEPTED}:
        return
    refs = list(claim.supporting_evidence or [])
    qualifying = [ref for ref in refs if _id_qualifies(ref, by_id)]
    if claim.status is ClaimStatus.ACCEPTED and not refs:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_ACCEPTED_WITHOUT_EVIDENCE,
                message=f"{claim.id} is accepted without supporting evidence",
                object_id=claim.id,
                field="supporting_evidence",
            )
        )
    elif not qualifying:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_NONQUALIFYING_EVIDENCE,
                message=(
                    f"{claim.id} status {claim.status} requires at least one "
                    "qualifying active supporting evidence reference"
                ),
                object_id=claim.id,
                field="supporting_evidence",
            )
        )
    if claim.status is ClaimStatus.ACCEPTED:
        _check_claim_review_gate(claim, by_id, findings, project_id)


@dataclass(frozen=True, slots=True)
class ClaimApproval:
    """What a search for a Claim's qualifying human approval actually found.

    ``review`` is the approval that qualifies, or ``None``. The three flags say
    why the ones that did not qualify did not, which is what turns "this claim
    is not approved" into a message a researcher can act on.
    """

    review: Review | None = None
    stale: bool = False
    incomplete_evidence: bool = False
    incomplete_experiments: bool = False

    @property
    def qualifies(self) -> bool:
        return self.review is not None


def claim_approval(
    claim: Claim,
    by_id: Mapping[str, ScientificObject],
    project_id: str,
) -> ClaimApproval:
    """Return the human approval that lets ``claim`` be accepted, if there is one.

    A qualifying review matches the claim's current subject digest, covers every
    currently linked Evidence object and every Experiment those objects reach,
    and stores the current digest of each. Anything less leaves an approval that
    could outlive the science it examined.

    Public and returning the review itself, rather than only reporting findings,
    because more than one caller needs this answer: the validator, which turns a
    missing approval into an error, and anything that has to show a reader *which*
    approval stands behind an accepted Claim. Two implementations of this rule
    would eventually disagree, and the one that disagreed quietly would be the
    one that let an unapproved claim be quoted.
    """

    current = subject_digest(claim, project_id=project_id)
    linked = _linked_evidence(claim)
    experiments = referenced_experiments(claim, by_id)
    stale = False
    incomplete_coverage = False
    incomplete_experiments = False
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
        if obj.subject_digest != current:
            stale = True
            continue
        stored = obj.evidence_digests or {}
        if set(stored) != linked:
            incomplete_coverage = True
            continue
        stored_experiments = obj.experiment_digests or {}
        if set(stored_experiments) != experiments:
            incomplete_experiments = True
            continue
        if _evidence_digests_match(
            stored, by_id, project_id
        ) and _experiment_digests_match(stored_experiments, by_id, project_id):
            return ClaimApproval(review=obj)
        stale = True
    return ClaimApproval(
        stale=stale,
        incomplete_evidence=incomplete_coverage,
        incomplete_experiments=incomplete_experiments,
    )


def _check_claim_review_gate(
    claim: Claim,
    by_id: Mapping[str, ScientificObject],
    findings: list[Finding],
    project_id: str,
) -> None:
    """Report why an accepted claim has no qualifying human approval."""

    approval = claim_approval(claim, by_id, project_id)
    if approval.qualifies:
        return
    stale_human_approve = approval.stale
    incomplete_coverage = approval.incomplete_evidence
    incomplete_experiments = approval.incomplete_experiments
    if incomplete_coverage:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_EVIDENCE_DIGESTS_INCOMPLETE,
                message=(
                    f"{claim.id} is accepted but its human approval review "
                    "does not bind every linked evidence object"
                ),
                object_id=claim.id,
                field="evidence_digests",
            )
        )
    if incomplete_experiments:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_EXPERIMENT_DIGESTS_INCOMPLETE,
                message=(
                    f"{claim.id} is accepted but its human approval review "
                    "does not bind every experiment its evidence relies on"
                ),
                object_id=claim.id,
                field="experiment_digests",
            )
        )
    if stale_human_approve:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_STALE_REVIEW_DIGEST,
                message=(
                    f"{claim.id} is accepted but its human approval review "
                    "does not match the current subject, evidence, and "
                    "experiment digests"
                ),
                object_id=claim.id,
                field="subject_digest",
            )
        )
    if incomplete_coverage or incomplete_experiments or stale_human_approve:
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
