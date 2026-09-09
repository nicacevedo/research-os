"""Human review packets and canonical Review authoring.

The scientific gate lives in :mod:`research_os.validate`; this module only makes
that gate usable by a person. It assembles what a reviewer must see, builds one
canonical ``Review`` from an explicit human decision, and writes it atomically.

It computes no digests of its own: every digest here comes from
:func:`research_os.digests.subject_digest`, the canonical implementation. It also
never mutates the reviewed Claim -- promoting a Claim to ``accepted`` stays a
deliberate human edit.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from research_os.capsule import ProjectValidationReport, resolve_object
from research_os.digests import subject_digest
from research_os.errors import CapsuleError, ReviewBlockedError
from research_os.ids import next_id
from research_os.models import (
    Claim,
    ClaimStatus,
    Evidence,
    Experiment,
    Hypothesis,
    Review,
    ReviewStatus,
    ScientificObject,
    Verdict,
)
from research_os.validate import referenced_experiments, validate_objects

REVIEWS_DIRECTORY = "reviews"

#: The only Claim status a review may be recorded against.
#:
#: R0 acceptance is existential -- ``validate`` accepts a Claim as soon as *a*
#: qualifying approve Review exists -- so a later ``reject`` on an already
#: accepted Claim would be recorded while the earlier approval kept satisfying
#: the gate. Rather than invent review ordering or revocation, the command
#: refuses, and re-review runs through ``accepted`` -> ``evidence_linked`` ->
#: review -> ``accepted``. Claim status is outside the semantic projection, so
#: that round trip does not change the Claim's reviewed scientific identity.
REVIEWABLE_CLAIM_STATUS = ClaimStatus.EVIDENCE_LINKED


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """One linked Evidence object as the reviewer needs to see it."""

    id: str
    status: str
    kind: str
    title: str
    statement: str
    digest: str
    citation: str | None = None
    global_ref: str | None = None
    locator: str | None = None
    experiment: str | None = None
    experiment_status: str | None = None

    def pointers(self) -> tuple[tuple[str, str], ...]:
        """Return every non-null source pointer as ``(label, value)`` pairs.

        Evaluating evidence means following it back to its source, so all four
        pointers are surfaced. Absent ones are omitted rather than rendered as
        placeholders. An experiment pointer carries the referenced Experiment's
        status, because that status decides whether the evidence qualifies.
        """

        pairs: list[tuple[str, str]] = []
        if self.citation is not None:
            pairs.append(("citation", self.citation))
        if self.global_ref is not None:
            pairs.append(("global_ref", self.global_ref))
        if self.locator is not None:
            pairs.append(("locator", self.locator))
        if self.experiment is not None:
            status = self.experiment_status or "missing"
            pairs.append(("experiment", f"{self.experiment}  (status: {status})"))
        return tuple(pairs)


@dataclass(frozen=True, slots=True)
class ExperimentEntry:
    """One Experiment the review will bind, as the reviewer needs to see it.

    Every digest-material field is carried, because the packet's job is to show
    exactly what the digest binds. In real capsules the disclosure that an
    experiment was reconstructed rather than preregistered lives inside these
    fields -- ``notes`` is outside every projection -- so abbreviating them would
    hide the one thing a reviewer most needs to see.
    """

    id: str
    status: str
    title: str
    purpose: str
    digest: str
    hypotheses: tuple[str, ...] = ()
    predictions: tuple[tuple[str, bool, str], ...] = ()
    primary_metrics: tuple[str, ...] = ()
    decision_rule: str | None = None
    provenance: tuple[tuple[str, str], ...] = ()
    result_manifest: str | None = None
    artifacts: tuple[str, ...] = ()

    def fields(self) -> tuple[tuple[str, str], ...]:
        """Return the single-line digest-material fields as label/value pairs.

        Absent optionals are omitted rather than rendered as placeholders, the
        same convention ``EvidenceEntry.pointers`` uses.
        """

        pairs: list[tuple[str, str]] = []
        if self.hypotheses:
            pairs.append(("hypotheses", ", ".join(self.hypotheses)))
        if self.primary_metrics:
            pairs.append(("primary_metrics", ", ".join(self.primary_metrics)))
        if self.decision_rule is not None:
            pairs.append(("decision_rule", self.decision_rule))
        if self.result_manifest is not None:
            pairs.append(("result_manifest", self.result_manifest))
        return tuple(pairs)


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    """Everything a human needs in order to review one Claim knowingly."""

    git_root: Path
    project_id: str
    claim: Claim
    claim_digest: str
    supporting: tuple[EvidenceEntry, ...]
    contrary: tuple[EvidenceEntry, ...]
    evidence_digests: dict[str, str]
    experiments: tuple[ExperimentEntry, ...]
    experiment_digests: dict[str, str]
    hypotheses: tuple[tuple[str, str], ...]
    review_id: str
    objects: tuple[ScientificObject, ...]
    warnings: tuple[object, ...] = ()

    @property
    def evidence_count(self) -> int:
        """Return how many Evidence digests this review will bind.

        Derived from the complete digest map, never counted by hand, so the
        number a reviewer confirms is the number actually written.
        """

        return len(self.evidence_digests)

    @property
    def experiment_count(self) -> int:
        """Return how many Experiment digests this review will bind.

        Derived from the complete digest map for the same reason as
        ``evidence_count``.
        """

        return len(self.experiment_digests)

    def entries(self) -> tuple[EvidenceEntry, ...]:
        return (*self.supporting, *self.contrary)


def build_review_packet(
    report: ProjectValidationReport,
    claim_id: str,
) -> ReviewPacket:
    """Assemble the review packet for ``claim_id``.

    Refuses rather than presenting a packet that cannot be trusted: a capsule
    holding validation errors could render an incomplete evidence list, and a
    Claim outside ``evidence_linked`` is not open for review.
    """

    if report.errors:
        raise ReviewBlockedError(
            f"fix these errors before reviewing {claim_id}",
            report.errors,
        )
    obj = resolve_object(report, claim_id)
    if not isinstance(obj, Claim):
        raise CapsuleError("researchctl review records reviews of claims only.")
    _require_reviewable_status(obj)

    assert report.project is not None  # resolve_object guarantees this
    project_id = report.project.id
    by_id = {item.id: item for item in report.objects}

    supporting = _evidence_entries(obj.supporting_evidence, by_id, project_id)
    contrary = _evidence_entries(obj.contrary_evidence, by_id, project_id)
    evidence_digests = {entry.id: entry.digest for entry in (*supporting, *contrary)}
    _assert_complete_coverage(obj, evidence_digests)

    required = referenced_experiments(obj, by_id)
    experiments = _experiment_entries(required, by_id, project_id)
    experiment_digests = {entry.id: entry.digest for entry in experiments}
    _assert_complete_experiment_coverage(obj, required, experiment_digests)

    return ReviewPacket(
        git_root=report.git_root,
        project_id=project_id,
        claim=obj,
        claim_digest=subject_digest(obj, project_id=project_id),
        supporting=supporting,
        contrary=contrary,
        evidence_digests=evidence_digests,
        experiments=experiments,
        experiment_digests=experiment_digests,
        hypotheses=_hypothesis_titles(obj, by_id),
        review_id=next_id("REV", (item.id for item in report.objects)),
        objects=report.objects,
        warnings=report.warnings,
    )


def build_review(
    packet: ReviewPacket,
    *,
    verdict: Verdict,
    findings: str,
) -> Review:
    """Build the concluded Review recording a human decision on ``packet``.

    ``concluded`` is the only status the acceptance gate consults, and the
    schema then requires findings, a verdict, and a subject digest -- all
    present here. Reviews carry no reviewer name or timestamp: the schema has no
    such fields, and attribution is the Git commit.
    """

    if not findings.strip():
        raise CapsuleError("a review requires non-empty findings")
    return Review.model_validate(
        {
            "id": packet.review_id,
            "type": "review",
            "schema_version": 1,
            "status": ReviewStatus.CONCLUDED.value,
            "title": f"Human review of {packet.claim.id}",
            "subject": packet.claim.id,
            "reviewer_kind": "human",
            "findings": findings,
            "verdict": verdict.value,
            "subject_digest": packet.claim_digest,
            "evidence_digests": dict(sorted(packet.evidence_digests.items())),
            "experiment_digests": dict(sorted(packet.experiment_digests.items())),
        }
    )


def write_review(packet: ReviewPacket, review: Review) -> Path:
    """Validate ``review`` against the capsule, then write it atomically.

    Validation runs against the existing objects plus the new Review, so a
    review that would introduce an error is refused before anything reaches the
    filesystem. The write itself is a temp file in the destination directory
    followed by ``os.replace``, so a failure part-way leaves no partial
    canonical file behind.
    """

    directory = packet.git_root / ".research" / REVIEWS_DIRECTORY
    target = directory / f"{review.id}.yaml"
    if target.exists() or target.is_symlink():
        raise CapsuleError(
            f"refusing to overwrite .research/{REVIEWS_DIRECTORY}/{review.id}.yaml"
        )

    combined = validate_objects((*packet.objects, review), project_id=packet.project_id)
    if combined.errors:
        raise ReviewBlockedError("refusing to write an invalid review", combined.errors)

    document = _dump_review_yaml(review)
    rel = f".research/{REVIEWS_DIRECTORY}/{review.id}.yaml"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CapsuleError(f"cannot write {rel}: {exc}") from exc
    try:
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{review.id}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise CapsuleError(f"cannot write {rel}: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        raise CapsuleError(f"cannot write {rel}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _require_reviewable_status(claim: Claim) -> None:
    if claim.status is REVIEWABLE_CLAIM_STATUS:
        return
    if claim.status is ClaimStatus.DRAFT:
        raise CapsuleError(
            f"{claim.id} is draft; link qualifying supporting evidence and set "
            "status to evidence_linked before reviewing."
        )
    if claim.status is ClaimStatus.ACCEPTED:
        raise CapsuleError(
            f"{claim.id} is already accepted. R0 review is existential, not "
            "ordered, so a new review cannot override the approval that "
            "accepted it: set status back to evidence_linked to re-review."
        )
    raise CapsuleError(f"{claim.id} is {claim.status} and is not open for review.")


def _evidence_entries(
    refs: list[str] | None,
    by_id: dict[str, ScientificObject],
    project_id: str,
) -> tuple[EvidenceEntry, ...]:
    """Project each referenced Evidence object, keeping authored order."""

    entries: list[EvidenceEntry] = []
    seen: set[str] = set()
    for ref in refs or []:
        if ref in seen:
            continue
        seen.add(ref)
        target = by_id.get(ref)
        if not isinstance(target, Evidence):
            raise CapsuleError(
                f"evidence {ref} is missing or is not an evidence object; "
                "run researchctl validate-project"
            )
        experiment_status = None
        if target.experiment is not None:
            referenced = by_id.get(target.experiment)
            if isinstance(referenced, Experiment):
                experiment_status = str(referenced.status)
        entries.append(
            EvidenceEntry(
                id=target.id,
                status=str(target.status),
                kind=str(target.kind),
                title=target.title,
                statement=target.statement,
                digest=subject_digest(target, project_id=project_id),
                citation=target.citation,
                global_ref=target.global_ref,
                locator=target.locator,
                experiment=target.experiment,
                experiment_status=experiment_status,
            )
        )
    return tuple(entries)


def _experiment_entries(
    required: frozenset[str],
    by_id: dict[str, ScientificObject],
    project_id: str,
) -> tuple[ExperimentEntry, ...]:
    """Project each bound Experiment, ordered by id so the packet is stable."""

    entries: list[ExperimentEntry] = []
    for ref in sorted(required):
        target = by_id.get(ref)
        if not isinstance(target, Experiment):
            raise CapsuleError(
                f"experiment {ref} is missing or is not an experiment object; "
                "run researchctl validate-project"
            )
        entries.append(
            ExperimentEntry(
                id=target.id,
                status=str(target.status),
                title=target.title,
                purpose=target.purpose,
                digest=subject_digest(target, project_id=project_id),
                hypotheses=tuple(target.hypotheses or ()),
                predictions=tuple(
                    (item.hypothesis, item.discriminates, item.predicted_outcome)
                    for item in target.predictions or ()
                ),
                primary_metrics=tuple(target.primary_metrics or ()),
                decision_rule=target.decision_rule,
                provenance=_provenance_pairs(target),
                result_manifest=target.result_manifest,
                artifacts=tuple(target.artifacts or ()),
            )
        )
    return tuple(entries)


def _provenance_pairs(experiment: Experiment) -> tuple[tuple[str, str], ...]:
    """Return provenance as label/value pairs, or empty when it is absent."""

    provenance = experiment.provenance
    if provenance is None:
        return ()
    return (
        ("code", provenance.code),
        ("config", provenance.config),
        ("data", provenance.data),
        ("git_commit", provenance.git_commit),
    )


def _assert_complete_experiment_coverage(
    claim: Claim,
    required: frozenset[str],
    experiment_digests: dict[str, str],
) -> None:
    """Guard the experiment coverage rule at the point the map is built.

    The gate requires exactly the Experiments the claim's linked evidence
    reaches, so the map the reviewer confirms must be that set and no other.
    """

    if set(experiment_digests) != required:
        raise CapsuleError(
            f"internal error: experiment digest map for {claim.id} covers "
            f"{sorted(experiment_digests)}, expected {sorted(required)}"
        )


def _assert_complete_coverage(claim: Claim, evidence_digests: dict[str, str]) -> None:
    """Guard the WP-A coverage rule at the point the map is built.

    An approval that did not examine every linked Evidence object must not gate
    acceptance, so the map the reviewer confirms is exactly the Claim's linked
    set in both polarities.
    """

    linked = frozenset(claim.supporting_evidence or []) | frozenset(
        claim.contrary_evidence or []
    )
    if set(evidence_digests) != linked:
        raise CapsuleError(
            f"internal error: evidence digest map for {claim.id} covers "
            f"{sorted(evidence_digests)}, expected {sorted(linked)}"
        )


def _hypothesis_titles(
    claim: Claim,
    by_id: dict[str, ScientificObject],
) -> tuple[tuple[str, str], ...]:
    titles: list[tuple[str, str]] = []
    for ref in claim.hypotheses or []:
        target = by_id.get(ref)
        title = target.title if isinstance(target, Hypothesis) else ""
        titles.append((ref, title))
    return tuple(titles)


def _dump_review_yaml(review: Review) -> str:
    """Serialize a Review as canonical capsule YAML."""

    payload = review.model_dump(mode="json", exclude_none=True)
    dumped = yaml.safe_dump(
        payload,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        explicit_start=False,
        explicit_end=False,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    return dumped
