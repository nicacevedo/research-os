"""Tests for Review subject semantic digests.

This is the per-object Review binding digest over scientific content, not a
hash of raw YAML. Every digest is project-scoped and carries an explicit
algorithm version, so the helpers below bind one shared test project; the
project-scope tests call the API directly with two identities.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from research_os.digests import semantic_projection, subject_digest
from research_os.errors import InvalidIdError
from research_os.models import DIGEST_RE, DIGEST_VERSION, Reviewable
from tests.helpers import (
    OTHER_PROJECT_ID,
    TEST_PROJECT_ID,
    make_claim,
    make_decision,
    make_evidence,
    make_experiment,
    make_hypothesis,
    make_idea,
    make_question,
)


def _digest(obj: Reviewable) -> str:
    return subject_digest(obj, project_id=TEST_PROJECT_ID)


def _projection(obj: Reviewable) -> dict[str, Any]:
    return semantic_projection(obj, project_id=TEST_PROJECT_ID)


def _manual_digest(projection: dict[str, object]) -> str:
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{DIGEST_VERSION}:{hashlib.sha256(payload).hexdigest()}"


def test_unchanged_semantic_content_is_stable() -> None:
    claim = make_claim(statement="X holds.", supporting_evidence=["EVI-0001"])
    first = _digest(claim)
    second = _digest(make_claim(statement="X holds.", supporting_evidence=["EVI-0001"]))
    assert first == second
    assert first == _manual_digest(_projection(claim))


def test_digest_carries_algorithm_version() -> None:
    digest = _digest(make_claim())
    version, separator, hex_digest = digest.partition(":")
    assert separator == ":"
    assert version == str(DIGEST_VERSION)
    assert len(hex_digest) == 64
    assert hex_digest == hex_digest.lower()
    assert DIGEST_RE.fullmatch(digest) is not None


def test_project_scope_changes_digest() -> None:
    claim = make_claim(statement="Same scientific content.")
    alpha = subject_digest(claim, project_id=TEST_PROJECT_ID)
    beta = subject_digest(claim, project_id=OTHER_PROJECT_ID)
    assert alpha != beta
    alpha_projection = semantic_projection(claim, project_id=TEST_PROJECT_ID)
    beta_projection = semantic_projection(claim, project_id=OTHER_PROJECT_ID)
    assert alpha_projection["project"] == TEST_PROJECT_ID
    assert beta_projection["project"] == OTHER_PROJECT_ID
    assert alpha_projection["id"] == beta_projection["id"]


@pytest.mark.parametrize(
    "project_id",
    ["", "a", "Alpha", "alpha_project", "1alpha", "-alpha", "a" * 64],
)
def test_invalid_project_id_rejected(project_id: str) -> None:
    claim = make_claim()
    with pytest.raises(InvalidIdError):
        subject_digest(claim, project_id=project_id)
    with pytest.raises(InvalidIdError):
        semantic_projection(claim, project_id=project_id)


def test_id_list_order_and_duplicates_do_not_change_digest() -> None:
    left = make_claim(supporting_evidence=["EVI-0002", "EVI-0001", "EVI-0002"])
    right = make_claim(supporting_evidence=["EVI-0001", "EVI-0002"])
    assert _digest(left) == _digest(right)
    assert _projection(left)["supporting_evidence"] == ["EVI-0001", "EVI-0002"]


def test_claim_status_change_does_not_change_digest() -> None:
    linked = make_claim(status="evidence_linked", statement="X holds.")
    accepted = make_claim(status="accepted", statement="X holds.")
    assert _digest(linked) == _digest(accepted)


def test_claim_statement_change_changes_digest() -> None:
    first = make_claim(statement="X holds.")
    second = make_claim(statement="X does not hold.")
    assert _digest(first) != _digest(second)


def test_claim_supporting_evidence_change_changes_digest() -> None:
    first = make_claim(supporting_evidence=["EVI-0001"])
    second = make_claim(supporting_evidence=["EVI-0001", "EVI-0002"])
    third = make_claim(supporting_evidence=None)
    assert _digest(first) != _digest(second)
    assert _digest(first) != _digest(third)


def test_claim_evidence_polarity_changes_digest() -> None:
    supporting = make_claim(supporting_evidence=["EVI-0001"])
    contrary = make_claim(contrary_evidence=["EVI-0001"])
    assert _digest(supporting) != _digest(contrary)

    addressed = make_claim(
        contrary_evidence=["EVI-0001"],
        contrary_evidence_addressed="The contrary result applies below 200 K.",
    )
    reworded = make_claim(
        contrary_evidence=["EVI-0001"],
        contrary_evidence_addressed="The contrary result applies above 200 K.",
    )
    assert _digest(contrary) != _digest(addressed)
    assert _digest(addressed) != _digest(reworded)
    assert _projection(contrary)["contrary_evidence_addressed"] is None


def test_claim_title_and_hypotheses_change_digest() -> None:
    base = make_claim(title="A claim", hypotheses=None)
    retitled = make_claim(title="Renamed claim", hypotheses=None)
    with_hyp = make_claim(title="A claim", hypotheses=["HYP-0001"])
    assert _digest(base) != _digest(retitled)
    assert _digest(base) != _digest(with_hyp)


def test_experiment_predictions_change_digest() -> None:
    base = make_experiment(status="specified")
    reworded = make_experiment(
        status="specified",
        predictions=[
            {
                "hypothesis": "HYP-0001",
                "predicted_outcome": "Heldout RMSE falls below 0.10.",
                "discriminates": True,
            }
        ],
    )
    non_discriminating = make_experiment(
        status="specified",
        predictions=[
            {
                "hypothesis": "HYP-0001",
                "predicted_outcome": "Heldout RMSE falls below 0.20.",
                "discriminates": False,
            }
        ],
    )
    assert _digest(base) != _digest(reworded)
    assert _digest(base) != _digest(non_discriminating)
    assert _projection(base)["predictions"] == [
        {
            "hypothesis": "HYP-0001",
            "predicted_outcome": "Heldout RMSE falls below 0.20.",
            "discriminates": True,
        }
    ]


def test_experiment_primary_metrics_change_digest() -> None:
    base = make_experiment(status="specified", primary_metrics=["heldout_rmse"])
    extra = make_experiment(
        status="specified", primary_metrics=["heldout_rmse", "auroc"]
    )
    reordered = make_experiment(
        status="specified", primary_metrics=["auroc", "heldout_rmse"]
    )
    assert _digest(base) != _digest(extra)
    assert _digest(extra) != _digest(reordered)
    assert _projection(extra)["primary_metrics"] == ["heldout_rmse", "auroc"]


def test_experiment_decision_rule_changes_digest() -> None:
    base = make_experiment(status="specified", decision_rule="Reject above 0.20.")
    changed = make_experiment(status="specified", decision_rule="Reject above 0.30.")
    assert _digest(base) != _digest(changed)
    assert _projection(base)["decision_rule"] == "Reject above 0.20."


def test_experiment_digest_is_project_scoped() -> None:
    experiment = make_experiment(status="completed")
    alpha = subject_digest(experiment, project_id=TEST_PROJECT_ID)
    beta = subject_digest(experiment, project_id=OTHER_PROJECT_ID)
    assert alpha != beta


def test_administrative_notes_and_lifecycle_fields_excluded() -> None:
    base = make_claim()
    noisy = make_claim(
        notes="internal reminder",
        created_from=["Q-0001"],
        supersedes=["CLAIM-0000"],
    )
    assert _digest(base) == _digest(noisy)
    projection = _projection(base)
    assert projection["id"] == "CLAIM-0001"
    assert "status" not in projection
    assert "schema_version" not in projection
    assert "notes" not in projection
    assert "created_from" not in projection
    assert "supersedes" not in projection


def test_retirement_metadata_excluded_from_digest() -> None:
    active = make_idea(status="active")
    discarded = make_idea(
        status="discarded",
        retire_reason="Existing data cannot identify the mechanism.",
        revisit_if="Facility-level cooling telemetry becomes available.",
    )
    assert _digest(active) == _digest(discarded)
    idea_projection = _projection(discarded)
    assert "retire_reason" not in idea_projection
    assert "revisit_if" not in idea_projection

    testing = make_hypothesis(status="testing")
    withdrawn = make_hypothesis(
        status="withdrawn",
        retire_reason="The instrument was decommissioned mid-campaign.",
        revisit_if="A calibrated replacement sensor is installed.",
    )
    assert _digest(testing) == _digest(withdrawn)
    hypothesis_projection = _projection(withdrawn)
    assert "retire_reason" not in hypothesis_projection
    assert "revisit_if" not in hypothesis_projection


def test_distinct_claim_ids_change_subject_digest() -> None:
    first = make_claim(id="CLAIM-0001", statement="Same scientific content.")
    second = make_claim(id="CLAIM-0002", statement="Same scientific content.")
    assert first.title == second.title
    assert first.statement == second.statement
    assert first.supporting_evidence == second.supporting_evidence
    assert first.hypotheses == second.hypotheses
    assert _projection(first)["id"] != _projection(second)["id"]
    assert _digest(first) != _digest(second)


def test_question_digest_excludes_status() -> None:
    open_q = make_question(status="open", statement="Why?")
    paused = make_question(status="paused", statement="Why?")
    assert _digest(open_q) == _digest(paused)
    changed = make_question(status="open", statement="Why not?")
    assert _digest(open_q) != _digest(changed)


def test_utf8_encoding_is_explicit() -> None:
    claim = make_claim(title="Título", statement="α → β")
    projection = _projection(claim)
    assert _digest(claim) == _manual_digest(projection)
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert b"\\u" not in payload


def test_absent_and_empty_optional_reference_lists_share_digest() -> None:
    absent = make_claim(
        supporting_evidence=None, contrary_evidence=None, hypotheses=None
    )
    empty = make_claim(supporting_evidence=[], contrary_evidence=[], hypotheses=[])
    for claim in (absent, empty):
        projection = _projection(claim)
        assert projection["supporting_evidence"] == []
        assert projection["contrary_evidence"] == []
        assert projection["hypotheses"] == []
    assert _digest(absent) == _digest(empty)

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
    projection = _projection(hyp_absent)
    assert projection["addresses"] == []
    assert projection["assumptions"] == []
    assert projection["supporting_evidence"] == []
    assert projection["contrary_evidence"] == []
    assert _digest(hyp_absent) == _digest(hyp_empty)

    related_absent = make_decision(related=None)
    related_empty = make_decision(related=[])
    assert _projection(related_absent)["related"] == []
    assert _digest(related_absent) == _digest(related_empty)

    exp_absent = make_experiment(hypotheses=None)
    exp_empty = make_experiment(hypotheses=[])
    assert _projection(exp_absent)["hypotheses"] == []
    assert _digest(exp_absent) == _digest(exp_empty)


def test_ordered_optional_lists_are_not_empty_normalized() -> None:
    absent = make_experiment(artifacts=None)
    empty = make_experiment(artifacts=[])
    assert _projection(absent)["artifacts"] is None
    assert _projection(empty)["artifacts"] == []
    assert _digest(absent) != _digest(empty)

    alt_absent = make_decision(alternatives_considered=None)
    alt_empty = make_decision(alternatives_considered=[])
    assert _projection(alt_absent)["alternatives_considered"] is None
    assert _projection(alt_empty)["alternatives_considered"] == []
    assert _digest(alt_absent) != _digest(alt_empty)

    pred_absent = make_experiment(predictions=None, primary_metrics=None)
    pred_empty = make_experiment(predictions=[], primary_metrics=[])
    absent_projection = _projection(pred_absent)
    empty_projection = _projection(pred_empty)
    assert absent_projection["predictions"] is None
    assert absent_projection["primary_metrics"] is None
    assert empty_projection["predictions"] == []
    assert empty_projection["primary_metrics"] == []
    assert _digest(pred_absent) != _digest(pred_empty)


def test_evidence_projection_includes_kind_fields() -> None:
    literature = make_evidence(kind="literature", citation="Smith 2020")
    projection = _projection(literature)
    assert projection["kind"] == "literature"
    assert projection["citation"] == "Smith 2020"
    assert projection["experiment"] is None
    other = make_evidence(kind="literature", citation="Jones 2021")
    assert _digest(literature) != _digest(other)


#: Digests computed at 43e5c8d, before Experiment binding was added.
#:
#: The Experiment-binding patch is additive: it stores Experiment digests in a
#: Review rather than folding them into any projection. These pins fail loudly
#: if a later change moves the algorithm, which would silently invalidate every
#: approval recorded in a real capsule.
PRE_PATCH_DIGESTS = {
    "experiment_completed": (
        "1:114b0a15b8c2b68f6bebfcafcf475167cc3b1850825c4e366e6e89a88f8b2fc9"
    ),
    "experiment_draft": (
        "1:8bea106a0c881401828692c3636fcadbeb232d34b8fc890f72645939263a760b"
    ),
    "evidence_experiment": (
        "1:21f779aeb234af45c570c7e6cce69646ec473a83e2571b1bd37fa4d5cd9be0eb"
    ),
    "evidence_literature": (
        "1:7ea0b7fa63b9105feef766591d172bd2c3198c541be4c3da955d1c65f79cbecc"
    ),
    "claim": "1:1719d3fa3166d917e9358fb71c9bf77ffa132d0a16332cd0ae7fb92017303f7b",
    "hypothesis": (
        "1:8a4b08f9c5e8ec122d23717054bae1b19495d485ed01bfb6eb42ecc061fb48e6"
    ),
    "question": "1:7096800b420f013ff11318c5d7a61920dbe2ea92a8122a5ff8d2b9094c2a19e4",
}


def test_pre_patch_digests_are_byte_identical() -> None:
    """No object digest moved, so no existing capsule needs migrating."""

    current = {
        "experiment_completed": _digest(make_experiment(status="completed")),
        "experiment_draft": _digest(make_experiment(status="draft")),
        "evidence_experiment": _digest(
            make_evidence(kind="experiment", experiment="EXP-0001")
        ),
        "evidence_literature": _digest(make_evidence()),
        "claim": _digest(
            make_claim(status="evidence_linked", supporting_evidence=["EVI-0001"])
        ),
        "hypothesis": _digest(make_hypothesis(status="active")),
        "question": _digest(make_question()),
    }
    assert current == PRE_PATCH_DIGESTS
