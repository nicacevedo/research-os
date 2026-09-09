"""Semantic subject digests for Review binding.

This hashes scientifically material content of a reviewed object. It must not
hash raw YAML: two files whose bytes differ but whose science is identical have
the same digest, and a reformatted file does not invalidate a review.

Digests carry an explicit algorithm version and are project-scoped, so a
changed digest distinguishes changed science from a changed algorithm, and an
object copied into another project does not inherit the first project's
reviewed identity. Project identity is always passed explicitly: this module
never consults a registry, the filesystem, or ambient state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from research_os.ids import validate_project_id
from research_os.models import (
    DIGEST_VERSION,
    Assumption,
    Claim,
    Decision,
    Evidence,
    Experiment,
    Hypothesis,
    Idea,
    Prediction,
    Provenance,
    Question,
    Reviewable,
)


def _id_list(values: list[str] | None) -> list[str]:
    """Canonicalize optional reference collections as sorted unique ID sets.

    ``None`` and ``[]`` both mean "no references" and project to ``[]``.
    """

    if not values:
        return []
    return sorted(set(values))


def _provenance(value: Provenance | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "code": value.code,
        "config": value.config,
        "data": value.data,
        "git_commit": value.git_commit,
    }


def _predictions(values: list[Prediction] | None) -> list[dict[str, Any]] | None:
    """Project preregistered predictions, preserving authored order."""

    if values is None:
        return None
    return [
        {
            "hypothesis": item.hypothesis,
            "predicted_outcome": item.predicted_outcome,
            "discriminates": item.discriminates,
        }
        for item in values
    ]


def _type_projection(obj: Reviewable) -> dict[str, Any]:
    """Return the fixed, type-specific scientific key set for ``obj``."""

    if isinstance(obj, Question):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
        }
    if isinstance(obj, Idea):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
        }
    if isinstance(obj, Hypothesis):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
            "falsification": obj.falsification,
            "mechanism": obj.mechanism,
            "addresses": _id_list(obj.addresses),
            "assumptions": _id_list(obj.assumptions),
            "supporting_evidence": _id_list(obj.supporting_evidence),
            "contrary_evidence": _id_list(obj.contrary_evidence),
            "confidence": obj.confidence,
            "confidence_basis": obj.confidence_basis,
        }
    if isinstance(obj, Assumption):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
            "scope": obj.scope,
        }
    if isinstance(obj, Claim):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
            "supporting_evidence": _id_list(obj.supporting_evidence),
            "contrary_evidence": _id_list(obj.contrary_evidence),
            "contrary_evidence_addressed": obj.contrary_evidence_addressed,
            "hypotheses": _id_list(obj.hypotheses),
        }
    if isinstance(obj, Decision):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "statement": obj.statement,
            "rationale": obj.rationale,
            "alternatives_considered": obj.alternatives_considered,
            "related": _id_list(obj.related),
        }
    if isinstance(obj, Experiment):
        artifacts = None if obj.artifacts is None else list(obj.artifacts)
        metrics = None if obj.primary_metrics is None else list(obj.primary_metrics)
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "purpose": obj.purpose,
            "hypotheses": _id_list(obj.hypotheses),
            "predictions": _predictions(obj.predictions),
            "primary_metrics": metrics,
            "decision_rule": obj.decision_rule,
            "provenance": _provenance(obj.provenance),
            "result_manifest": obj.result_manifest,
            "artifacts": artifacts,
        }
    if isinstance(obj, Evidence):
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "kind": obj.kind,
            "statement": obj.statement,
            "citation": obj.citation,
            "global_ref": obj.global_ref,
            "locator": obj.locator,
            "experiment": obj.experiment,
        }
    raise TypeError(f"object type is not reviewable: {type(obj)!r}")


def semantic_projection(obj: Reviewable, *, project_id: str) -> dict[str, Any]:
    """Return the fixed-key scientific projection for ``obj`` in a project.

    ``project_id`` is required and validated: reviewed scientific identity is
    project-local, so the project slug is a key of every projection. Absent
    scalar optionals are JSON ``null``. Optional reference collections whose
    semantics are "no references" project ``None`` and ``[]`` to the same
    empty list; remaining ID lists are de-duplicated and sorted
    lexicographically. Administrative fields (``status``, ``schema_version``,
    ``notes``, ``created_from``, ``supersedes``) and retirement metadata
    (``retire_reason``, ``revisit_if``) are excluded. Immutable object ``id``
    is included. Artifact, primary-metric, and prediction lists keep parsed
    order.
    """

    validate_project_id(project_id)
    return {"project": project_id, **_type_projection(obj)}


def subject_digest(obj: Reviewable, *, project_id: str) -> str:
    """Return the versioned, project-scoped digest of ``obj``.

    Canonical serialization is UTF-8 JSON with sorted keys and compact
    separators ``(",", ":")``. The return value matches Review.subject_digest
    (``"<version>:<64-char lowercase hex>"``), not a bare hex digest, not an
    ``sha256:`` prefixed form, and not the M4 capsule source digest.
    """

    payload = json.dumps(
        semantic_projection(obj, project_id=project_id),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{DIGEST_VERSION}:{hashlib.sha256(payload).hexdigest()}"
