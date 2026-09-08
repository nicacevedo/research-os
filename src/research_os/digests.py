"""Semantic subject digests for Review binding.

This hashes scientifically material content of a reviewed object. It is not
the M4 whole-capsule ``canonical_source_digest`` and must not hash raw YAML.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from research_os.models import (
    Assumption,
    Claim,
    Decision,
    Evidence,
    Experiment,
    Hypothesis,
    Idea,
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


def semantic_projection(obj: Reviewable) -> dict[str, Any]:
    """Return the fixed-key scientific projection for ``obj``.

    Absent scalar optionals are JSON ``null``. Optional reference collections
    whose semantics are "no references" project ``None`` and ``[]`` to the
    same empty list; remaining ID lists are de-duplicated and sorted
    lexicographically. Administrative fields (``status``, ``schema_version``,
    ``notes``, ``created_from``, ``supersedes``) are excluded. Immutable
    object ``id`` is included. Artifact pointer lists keep parsed order.
    """

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
            "evidence": _id_list(obj.evidence),
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
        return {
            "id": obj.id,
            "type": obj.type,
            "title": obj.title,
            "purpose": obj.purpose,
            "hypotheses": _id_list(obj.hypotheses),
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


def subject_digest(obj: Reviewable) -> str:
    """Return the lowercase hex SHA-256 of ``obj``'s semantic projection.

    Canonical serialization is UTF-8 JSON with sorted keys and compact
    separators ``(",", ":")``. The return value matches Review.subject_digest
    (64-char lowercase hex), not an ``sha256:`` prefixed form and not the M4
    capsule source digest.
    """

    payload = json.dumps(
        semantic_projection(obj),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
