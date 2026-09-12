"""The one door between proposed work and a project's scientific record.

Everything on the proposal side of this module is runtime state a researcher can
delete without consequence. Everything on the other side is a Git-tracked file
inside their capsule. Four rules govern the crossing, and all four are enforced
here rather than asked for:

**Only a human opens it.** No controller path calls this. The CLI command that
does requires an interactive terminal and an explicit confirmation, exactly like
``researchctl review``, because a non-human process must not put science into a
project.

**It only ever produces a draft.** :data:`~research_os.proposal.models.
PROMOTION_TARGETS` maps each proposal kind to the weakest status that type has:
an open Question, a draft Hypothesis, a draft Experiment, a draft Claim. There is
no path here that produces an accepted Claim, and there is no path here that
produces a Review at all -- a Review is the human act this module exists to
protect, and an object type an automated pipeline can draft is one it could
eventually be mistaken for performing.

**A historical experiment never becomes a preregistered one.** R0 reads
``predictions`` and ``decision_rule`` on a non-draft Experiment as an ex-ante
commitment. A proposal whose basis is ``historical`` describes work already
done, so promotion writes it as a draft with the decision rule recorded in
``notes`` rather than in the preregistration fields. Turning a description into
a preregistration is the most effective way to manufacture confidence nobody
earned, and it is the one transformation this module refuses outright.

**Provenance survives.** Every promoted object records which proposal and which
item it came from, in its ``notes``, so a draft in a project can always be traced
back to the run that suggested it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from research_os.capsule import (
    EXPERIMENT_DIRECTORY,
    TYPED_DIRECTORIES,
    validate_project,
)
from research_os.errors import CapsuleError, ProposalValidationError
from research_os.ids import TYPE_TO_PREFIX, next_id
from research_os.models import ScientificObject, parse_object
from research_os.proposal.models import (
    NON_PROMOTABLE_KINDS,
    PROMOTION_TARGETS,
    EvidenceBasis,
    PromotionRecord,
    ProposedItem,
    ResearchProposal,
)
from research_os.validate import validate_objects

#: The filename an Experiment's canonical document has inside its own directory.
EXPERIMENT_MANIFEST = "manifest.yaml"

#: Where each promotable object type is written, relative to ``.research/``.
TYPE_TO_DIRECTORY: dict[str, str] = {
    **{value: key for key, value in TYPED_DIRECTORIES.items()},
    "experiment": EXPERIMENT_DIRECTORY,
}


class PreparedPromotion:
    """A draft object built from a proposal, not yet written anywhere.

    Deliberately a separate step from writing it. The CLI renders this for the
    researcher, they read exactly what would be created, and only then does
    anything reach the filesystem.
    """

    __slots__ = ("document", "git_root", "item", "obj", "proposal", "target")

    def __init__(
        self,
        *,
        proposal: ResearchProposal,
        item: ProposedItem,
        obj: ScientificObject,
        document: str,
        git_root: Path,
        target: Path,
    ) -> None:
        self.proposal = proposal
        self.item = item
        self.obj = obj
        self.document = document
        self.git_root = git_root
        self.target = target

    @property
    def relative_target(self) -> str:
        return self.target.relative_to(self.git_root).as_posix()


def prepare_promotion(
    proposal: ResearchProposal,
    item_id: str,
    *,
    project_path: Path | None = None,
) -> PreparedPromotion:
    """Build the draft capsule object one proposed item would become.

    Validates the result against the project's existing objects before it is
    ever offered, so a promotion that would make the capsule invalid is refused
    while it is still hypothetical.
    """

    try:
        item = proposal.item(item_id)
    except KeyError as exc:
        raise ProposalValidationError(
            f"{proposal.proposal_id} has no proposed item {item_id}"
        ) from exc

    if item.kind in NON_PROMOTABLE_KINDS:
        raise ProposalValidationError(
            f"{item_id} is a {item.kind}, which is a reading of results for the "
            "researcher rather than something the project asserts. It has no "
            "capsule object type and cannot be promoted; act on it instead."
        )
    target_type, target_status = PROMOTION_TARGETS[item.kind]

    root = (
        Path(project_path) if project_path is not None else Path(proposal.project_path)
    )
    report = validate_project(root)
    if report.project is None or report.capsule is None:
        raise ProposalValidationError(
            f"{root} has no Research Capsule, so there is nothing to promote into. "
            "Run 'researchctl init-project' first."
        )

    existing = [obj.id for obj in report.objects]
    object_id = next_id(TYPE_TO_PREFIX[target_type], existing)
    payload = _build_payload(
        item,
        proposal=proposal,
        object_id=object_id,
        object_type=target_type,
        status=target_status,
        existing=set(existing),
    )
    try:
        obj = parse_object(payload)
    except (ValidationError, ValueError) as exc:
        raise ProposalValidationError(
            f"{item_id} cannot be promoted into a valid {target_type}: {exc}"
        ) from exc

    combined = validate_objects((*report.objects, obj), project_id=report.project.id)
    if combined.errors:
        details = "; ".join(finding.message for finding in combined.errors[:5])
        raise ProposalValidationError(
            f"promoting {item_id} would make this capsule invalid: {details}"
        )

    # An Experiment's canonical file is its directory's ``manifest.yaml``, not a
    # file named after the id: R0 gives an experiment a directory because a
    # completed one carries artifacts beside its manifest.
    directory = root / ".research" / TYPE_TO_DIRECTORY[target_type]
    target = (
        directory / object_id / EXPERIMENT_MANIFEST
        if target_type == "experiment"
        else directory / f"{object_id}.yaml"
    )
    return PreparedPromotion(
        proposal=proposal,
        item=item,
        obj=obj,
        document=_dump(payload),
        git_root=root,
        target=target,
    )


def write_promotion(prepared: PreparedPromotion) -> PromotionRecord:
    """Write the prepared draft into the capsule, atomically, once.

    Refuses to overwrite. An id collision here means the capsule changed between
    preparing and writing, and silently replacing a scientific file is never the
    right response to that.
    """

    target = prepared.target
    if target.exists() or target.is_symlink():
        raise CapsuleError(
            f"refusing to overwrite {prepared.relative_target}; the capsule "
            "changed since this promotion was prepared"
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{prepared.obj.id}.", suffix=".tmp"
        )
    except OSError as exc:
        raise CapsuleError(f"cannot write {prepared.relative_target}: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(prepared.document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        raise CapsuleError(f"cannot write {prepared.relative_target}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    return PromotionRecord(
        proposal_id=prepared.proposal.proposal_id,
        item_id=prepared.item.item_id,
        object_id=prepared.obj.id,
        object_type=str(prepared.obj.type),
        object_status=str(prepared.obj.status),
        project_path=str(prepared.git_root),
        written_path=prepared.relative_target,
        basis=prepared.item.basis,
        note=("promoted as a draft; a human must still complete and accept it"),
    )


def _build_payload(
    item: ProposedItem,
    *,
    proposal: ResearchProposal,
    object_id: str,
    object_type: str,
    status: str,
    existing: set[str],
) -> dict[str, Any]:
    """Return the capsule document one proposed item becomes.

    Only fields the proposal actually established are set. Everything a human
    still has to decide -- confidence, accepted evidence, a completed
    experiment's provenance -- is deliberately left unset, because a promoted
    draft that arrives pre-filled invites being accepted without being read.
    """

    linked = [entry for entry in item.addresses if entry in existing]
    payload: dict[str, Any] = {
        "id": object_id,
        "type": object_type,
        "schema_version": 1,
        "status": status,
        "title": item.title.strip(),
        "notes": _provenance_note(item, proposal),
        "created_from": linked,
    }

    if object_type == "question":
        payload["statement"] = item.statement.strip()
    elif object_type == "hypothesis":
        payload["statement"] = item.statement.strip()
        if (item.falsification or "").strip():
            payload["falsification"] = item.falsification.strip()
        if linked:
            payload["addresses"] = linked
    elif object_type == "claim":
        payload["statement"] = item.statement.strip()
        hypotheses = [entry for entry in linked if entry.startswith("HYP-")]
        if hypotheses:
            payload["hypotheses"] = hypotheses
    elif object_type == "experiment":
        payload["purpose"] = item.statement.strip()
        hypotheses = [entry for entry in linked if entry.startswith("HYP-")]
        if hypotheses:
            payload["hypotheses"] = hypotheses
        if item.primary_metrics:
            payload["primary_metrics"] = list(dict.fromkeys(item.primary_metrics))
        if (
            item.basis is EvidenceBasis.PROSPECTIVE
            and (item.decision_rule or "").strip()
        ):
            # A prospective proposal may carry its decision rule into the draft,
            # because it was written before the answer was known. A historical
            # one may not: the same sentence, added after the results exist, is
            # a description wearing a preregistration's clothes.
            payload["decision_rule"] = item.decision_rule.strip()
    return payload


def _provenance_note(item: ProposedItem, proposal: ResearchProposal) -> str:
    """Return the note that lets a draft be traced back to what suggested it."""

    lines = [
        f"Promoted from proposal {proposal.proposal_id}, item {item.item_id}.",
        f"Basis: {item.basis}.",
        f"Proposed by {proposal.provider} / {proposal.model or 'provider default'}.",
        "",
        "Rationale as proposed:",
        item.rationale.strip(),
    ]
    if item.basis is EvidenceBasis.HISTORICAL:
        lines.extend(
            [
                "",
                (
                    "This item was proposed as HISTORICAL: it describes work whose "
                    "results were already known when it was written. It has been "
                    "promoted as a draft without preregistration fields, because a "
                    "decision rule written after the outcome is not a prediction."
                ),
            ]
        )
        if (item.decision_rule or "").strip():
            lines.extend(
                [
                    "",
                    "Decision rule as proposed (retrospective):",
                    item.decision_rule.strip(),
                ]
            )
    if item.expected_direction:
        lines.extend(
            ["", "Expected direction as proposed:", item.expected_direction.strip()]
        )
    if item.grounded_in_literature:
        lines.extend(
            ["", "Grounded in retrieved works:", ", ".join(item.grounded_in_literature)]
        )
    if item.risks:
        lines.extend(["", "Risks as proposed:", "; ".join(item.risks)])
    lines.extend(
        [
            "",
            (
                "This is a DRAFT created by a human promoting an automated "
                "proposal. Nothing about it has been scientifically reviewed or "
                "accepted."
            ),
        ]
    )
    return "\n".join(lines)


def _dump(payload: dict[str, Any]) -> str:
    """Return the capsule YAML document, in the field order a human reads."""

    dumped = yaml.safe_dump(
        payload,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        explicit_start=False,
        explicit_end=False,
        width=88,
    )
    return dumped if dumped.endswith("\n") else dumped + "\n"
