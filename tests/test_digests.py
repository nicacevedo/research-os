"""Tests for Review subject semantic digests.

This is the per-object Review binding digest, not the M4 capsule
``canonical_source_digest``.
"""

from __future__ import annotations

import hashlib
import json

from research_os.digests import semantic_projection, subject_digest
from tests.helpers import (
    make_claim,
    make_decision,
    make_evidence,
    make_experiment,
    make_hypothesis,
    make_question,
)


def _manual_digest(projection: dict[str, object]) -> str:
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_unchanged_semantic_content_is_stable() -> None:
    claim = make_claim(statement="X holds.", evidence=["EVI-0001"])
    first = subject_digest(claim)
    second = subject_digest(make_claim(statement="X holds.", evidence=["EVI-0001"]))
    assert first == second
    assert first == _manual_digest(semantic_projection(claim))
    assert len(first) == 64
    assert first == first.lower()


def test_id_list_order_and_duplicates_do_not_change_digest() -> None:
    left = make_claim(evidence=["EVI-0002", "EVI-0001", "EVI-0002"])
    right = make_claim(evidence=["EVI-0001", "EVI-0002"])
    assert subject_digest(left) == subject_digest(right)
    assert semantic_projection(left)["evidence"] == ["EVI-0001", "EVI-0002"]


def test_claim_status_change_does_not_change_digest() -> None:
    linked = make_claim(status="evidence_linked", statement="X holds.")
    accepted = make_claim(status="accepted", statement="X holds.")
    assert subject_digest(linked) == subject_digest(accepted)


def test_claim_statement_change_changes_digest() -> None:
    first = make_claim(statement="X holds.")
    second = make_claim(statement="X does not hold.")
    assert subject_digest(first) != subject_digest(second)


def test_claim_evidence_change_changes_digest() -> None:
    first = make_claim(evidence=["EVI-0001"])
    second = make_claim(evidence=["EVI-0001", "EVI-0002"])
    third = make_claim(evidence=None)
    assert subject_digest(first) != subject_digest(second)
    assert subject_digest(first) != subject_digest(third)


def test_claim_title_and_hypotheses_change_digest() -> None:
    base = make_claim(title="A claim", hypotheses=None)
    retitled = make_claim(title="Renamed claim", hypotheses=None)
    with_hyp = make_claim(title="A claim", hypotheses=["HYP-0001"])
    assert subject_digest(base) != subject_digest(retitled)
    assert subject_digest(base) != subject_digest(with_hyp)


def test_administrative_notes_and_lifecycle_fields_excluded() -> None:
    base = make_claim()
    noisy = make_claim(
        notes="internal reminder",
        created_from=["Q-0001"],
        supersedes=["CLAIM-0000"],
    )
    assert subject_digest(base) == subject_digest(noisy)
    projection = semantic_projection(base)
    assert projection["id"] == "CLAIM-0001"
    assert "status" not in projection
    assert "schema_version" not in projection
    assert "notes" not in projection
    assert "created_from" not in projection
    assert "supersedes" not in projection


def test_distinct_claim_ids_change_subject_digest() -> None:
    first = make_claim(id="CLAIM-0001", statement="Same scientific content.")
    second = make_claim(id="CLAIM-0002", statement="Same scientific content.")
    assert first.title == second.title
    assert first.statement == second.statement
    assert first.evidence == second.evidence
    assert first.hypotheses == second.hypotheses
    assert semantic_projection(first)["id"] != semantic_projection(second)["id"]
    assert subject_digest(first) != subject_digest(second)


def test_question_digest_excludes_status() -> None:
    open_q = make_question(status="open", statement="Why?")
    paused = make_question(status="paused", statement="Why?")
    assert subject_digest(open_q) == subject_digest(paused)
    changed = make_question(status="open", statement="Why not?")
    assert subject_digest(open_q) != subject_digest(changed)


def test_utf8_encoding_is_explicit() -> None:
    claim = make_claim(title="Título", statement="α → β")
    projection = semantic_projection(claim)
    assert subject_digest(claim) == _manual_digest(projection)
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert b"\\u" not in payload


def test_absent_and_empty_optional_reference_lists_share_digest() -> None:
    absent = make_claim(evidence=None, hypotheses=None)
    empty = make_claim(evidence=[], hypotheses=[])
    assert semantic_projection(absent)["evidence"] == []
    assert semantic_projection(absent)["hypotheses"] == []
    assert semantic_projection(empty)["evidence"] == []
    assert semantic_projection(empty)["hypotheses"] == []
    assert subject_digest(absent) == subject_digest(empty)

    hyp_absent = make_hypothesis(
        addresses=None,
        assumptions=None,
        supporting_evidence=None,
        contrary_evidence=None,
    )
    hyp_empty = make_hypothesis(
        addresses=[],
        assumptions=[],
        supporting_evidence=[],
        contrary_evidence=[],
    )
    projection = semantic_projection(hyp_absent)
    assert projection["addresses"] == []
    assert projection["assumptions"] == []
    assert projection["supporting_evidence"] == []
    assert projection["contrary_evidence"] == []
    assert subject_digest(hyp_absent) == subject_digest(hyp_empty)

    related_absent = make_decision(related=None)
    related_empty = make_decision(related=[])
    assert semantic_projection(related_absent)["related"] == []
    assert subject_digest(related_absent) == subject_digest(related_empty)

    exp_absent = make_experiment(hypotheses=None)
    exp_empty = make_experiment(hypotheses=[])
    assert semantic_projection(exp_absent)["hypotheses"] == []
    assert subject_digest(exp_absent) == subject_digest(exp_empty)


def test_ordered_optional_lists_are_not_empty_normalized() -> None:
    absent = make_experiment(artifacts=None)
    empty = make_experiment(artifacts=[])
    assert semantic_projection(absent)["artifacts"] is None
    assert semantic_projection(empty)["artifacts"] == []
    assert subject_digest(absent) != subject_digest(empty)

    alt_absent = make_decision(alternatives_considered=None)
    alt_empty = make_decision(alternatives_considered=[])
    assert semantic_projection(alt_absent)["alternatives_considered"] is None
    assert semantic_projection(alt_empty)["alternatives_considered"] == []
    assert subject_digest(alt_absent) != subject_digest(alt_empty)


def test_evidence_projection_includes_kind_fields() -> None:
    literature = make_evidence(kind="literature", citation="Smith 2020")
    projection = semantic_projection(literature)
    assert projection["kind"] == "literature"
    assert projection["citation"] == "Smith 2020"
    assert projection["experiment"] is None
    other = make_evidence(kind="literature", citation="Jones 2021")
    assert subject_digest(literature) != subject_digest(other)
