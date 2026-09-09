"""Local Pydantic schema tests for Research Capsule objects."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_os.models import (
    DIGEST_VERSION,
    AssumptionStatus,
    ClaimStatus,
    DecisionStatus,
    EvidenceStatus,
    ExperimentStatus,
    HypothesisStatus,
    IdeaStatus,
    ObjectType,
    Prediction,
    Project,
    ProjectStatus,
    Provenance,
    Question,
    QuestionStatus,
    ReviewStatus,
    parse_object,
)
from tests.helpers import (
    make_assumption,
    make_claim,
    make_decision,
    make_evidence,
    make_experiment,
    make_hypothesis,
    make_idea,
    make_provenance,
    make_question,
    make_review,
)


def test_required_fields_and_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make_question(statement=None)
    with pytest.raises(ValidationError):
        make_question(extra="nope")
    with pytest.raises(ValidationError):
        make_question(title="")
    with pytest.raises(ValidationError):
        make_question(title="   ")
    with pytest.raises(ValidationError):
        make_question(statement="\n\t")


def test_superseded_by_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        make_question(superseded_by=["Q-0002"])
    with pytest.raises(ValidationError):
        make_claim(superseded_by=["CLAIM-0002"])


@pytest.mark.parametrize("status", list(QuestionStatus))
def test_question_statuses(status: QuestionStatus) -> None:
    obj = make_question(status=status)
    assert obj.status is status
    assert obj.type is ObjectType.QUESTION


@pytest.mark.parametrize("status", list(IdeaStatus))
def test_idea_statuses(status: IdeaStatus) -> None:
    assert make_idea(status=status).status is status


@pytest.mark.parametrize("status", list(HypothesisStatus))
def test_hypothesis_statuses(status: HypothesisStatus) -> None:
    assert make_hypothesis(status=status).status is status


@pytest.mark.parametrize("status", list(AssumptionStatus))
def test_assumption_statuses(status: AssumptionStatus) -> None:
    assert make_assumption(status=status).status is status


@pytest.mark.parametrize("status", list(ClaimStatus))
def test_claim_statuses(status: ClaimStatus) -> None:
    assert make_claim(status=status).status is status


@pytest.mark.parametrize("status", list(DecisionStatus))
def test_decision_statuses(status: DecisionStatus) -> None:
    assert make_decision(status=status).status is status


@pytest.mark.parametrize("status", list(ExperimentStatus))
def test_experiment_statuses(status: ExperimentStatus) -> None:
    assert make_experiment(status=status).status is status


@pytest.mark.parametrize("status", list(ReviewStatus))
def test_review_statuses(status: ReviewStatus) -> None:
    assert make_review(status=status).status is status


@pytest.mark.parametrize("status", list(EvidenceStatus))
def test_evidence_statuses(status: EvidenceStatus) -> None:
    assert make_evidence(status=status).status is status


def test_invalid_schema_version_rejected() -> None:
    with pytest.raises(ValidationError):
        make_question(schema_version=2)
    with pytest.raises(ValidationError):
        make_question(schema_version="1")


def test_id_prefix_must_match_declared_type() -> None:
    with pytest.raises(ValidationError):
        make_question(id="IDEA-0001")
    with pytest.raises(ValidationError):
        parse_object(
            {
                "id": "Q-0001",
                "type": "idea",
                "schema_version": 1,
                "status": "draft",
                "title": "Mismatch",
                "statement": "no",
            }
        )


def test_confidence_bounds() -> None:
    make_hypothesis(confidence=0, confidence_basis="guess")
    make_hypothesis(confidence=1, confidence_basis="strong")
    make_hypothesis(confidence=0.5, confidence_basis="mixed")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=-0.1, confidence_basis="no")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=1.1, confidence_basis="no")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=True, confidence_basis="no")


def test_confidence_rejects_numeric_strings_and_bools() -> None:
    base = {
        "id": "HYP-0001",
        "type": "hypothesis",
        "schema_version": 1,
        "status": "draft",
        "title": "A hypothesis",
        "statement": "X causes Y.",
        "confidence_basis": "prior work",
    }
    with pytest.raises(ValidationError):
        parse_object({**base, "confidence": "0.5"})
    with pytest.raises(ValidationError):
        parse_object({**base, "confidence": "1"})
    with pytest.raises(ValidationError):
        parse_object({**base, "confidence": True})
    parsed_zero = parse_object({**base, "confidence": 0})
    parsed_one = parse_object({**base, "confidence": 1})
    parsed_half = parse_object({**base, "confidence": 0.5})
    assert parsed_zero.confidence == 0.0
    assert parsed_one.confidence == 1.0
    assert parsed_half.confidence == 0.5


def test_confidence_basis_coupling() -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=0.4)
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=0.4, confidence_basis="")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=0.4, confidence_basis="   ")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence_basis="stated without a number")
    obj = make_hypothesis(confidence=0.2, confidence_basis="prior work")
    assert obj.confidence == 0.2


def test_non_draft_hypothesis_requires_falsification() -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(status="active", falsification=None)
    with pytest.raises(ValidationError):
        make_hypothesis(status="active", falsification="")
    with pytest.raises(ValidationError):
        make_hypothesis(status="active", falsification="   ")
    make_hypothesis(status="draft", falsification=None)


def test_completed_experiment_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        make_experiment(status="completed", provenance=None)
    with pytest.raises(ValidationError):
        make_experiment(
            status="completed",
            provenance={"code": "x", "config": "y", "data": "", "git_commit": "z"},
        )
    with pytest.raises(ValidationError):
        make_experiment(
            status="completed",
            provenance={"code": "x", "config": "y", "data": "   ", "git_commit": "z"},
        )
    obj = make_experiment(status="completed")
    assert obj.provenance is not None
    assert obj.hypotheses == ["HYP-0001"]


def test_non_draft_experiment_requires_hypotheses() -> None:
    with pytest.raises(ValidationError):
        make_experiment(status="specified", hypotheses=None)
    with pytest.raises(ValidationError):
        make_experiment(status="running", hypotheses=[])
    make_experiment(status="draft", hypotheses=None)


def test_accepted_decision_requires_alternatives() -> None:
    with pytest.raises(ValidationError):
        make_decision(status="accepted", alternatives_considered=None)
    with pytest.raises(ValidationError):
        make_decision(status="accepted", alternatives_considered=[])
    with pytest.raises(ValidationError):
        make_decision(status="accepted", alternatives_considered=["   "])
    make_decision(status="proposed", alternatives_considered=None)


def test_concluded_review_requires_findings_verdict_and_digest() -> None:
    with pytest.raises(ValidationError):
        make_review(status="concluded", findings=None)
    with pytest.raises(ValidationError):
        make_review(status="concluded", findings="   ")
    with pytest.raises(ValidationError):
        make_review(status="concluded", verdict=None)
    with pytest.raises(ValidationError):
        make_review(status="concluded", subject_digest=None)
    make_review(status="draft", findings=None, verdict=None, subject_digest=None)


@pytest.mark.parametrize(
    "digest",
    [
        "a" * 64,
        f"{DIGEST_VERSION}:{'A' * 64}",
        f"{DIGEST_VERSION}:{'a' * 63}",
        f"{DIGEST_VERSION}:{'a' * 65}",
        f"{DIGEST_VERSION}:not-hex",
        f":{'a' * 64}",
        f"{DIGEST_VERSION + 1}:{'a' * 64}",
        f"sha256:{'a' * 64}",
    ],
)
def test_versioned_digest_format_is_enforced(digest: str) -> None:
    with pytest.raises(ValidationError):
        make_review(status="concluded", subject_digest=digest)
    with pytest.raises(ValidationError):
        make_review(status="concluded", evidence_digests={"EVI-0001": digest})


def test_versioned_digest_format_is_accepted() -> None:
    valid = f"{DIGEST_VERSION}:{'a' * 64}"
    review = make_review(status="concluded", subject_digest=valid)
    assert review.subject_digest == valid
    bound = make_review(
        status="concluded",
        subject_digest=valid,
        evidence_digests={"EVI-0001": valid},
    )
    assert bound.evidence_digests == {"EVI-0001": valid}


def test_evidence_digest_keys_must_be_evidence_ids() -> None:
    valid = f"{DIGEST_VERSION}:{'a' * 64}"
    for key in ("HYP-0001", "CLAIM-0002", "EVI-1", "evi-0001", "not-an-id"):
        with pytest.raises(ValidationError):
            make_review(status="concluded", evidence_digests={key: valid})


def test_evidence_digests_only_valid_on_claim_subjects() -> None:
    valid = f"{DIGEST_VERSION}:{'a' * 64}"
    for subject in ("HYP-0001", "EXP-0001", "EVI-0001", "Q-0001"):
        with pytest.raises(ValidationError):
            make_review(
                status="concluded",
                subject=subject,
                evidence_digests={"EVI-0001": valid},
            )
    make_review(
        status="concluded",
        subject="CLAIM-0001",
        evidence_digests={"EVI-0001": valid},
    )


def test_review_of_review_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        make_review(subject="REV-0002")


def test_review_cannot_use_supersedes() -> None:
    with pytest.raises(ValidationError):
        make_review(supersedes=["REV-0002"])


def test_evidence_kind_conditioned_fields() -> None:
    make_evidence(kind="literature", citation="Doe 2021")
    with pytest.raises(ValidationError):
        make_evidence(kind="literature", citation=None)
    with pytest.raises(ValidationError):
        make_evidence(kind="literature", citation="   ")
    make_evidence(kind="experiment", experiment="EXP-0002")
    with pytest.raises(ValidationError):
        make_evidence(kind="experiment", experiment=None)
    make_evidence(kind="other", notes="seen in the lab", citation=None)
    make_evidence(kind="other", citation="internal memo", notes=None)
    with pytest.raises(ValidationError):
        make_evidence(kind="other", citation=None, notes=None)
    with pytest.raises(ValidationError):
        make_evidence(kind="other", citation="   ", notes="   ")


def test_experiment_pointer_only_allowed_for_experiment_kind() -> None:
    with pytest.raises(ValidationError):
        make_evidence(kind="literature", citation="Smith 2020", experiment="EXP-0001")
    with pytest.raises(ValidationError):
        make_evidence(kind="other", notes="lab notebook", experiment="EXP-0001")
    obj = make_evidence(kind="experiment", experiment="EXP-0001")
    assert obj.experiment == "EXP-0001"


def test_other_evidence_requires_citation_or_notes() -> None:
    with pytest.raises(ValidationError):
        parse_object(
            {
                "id": "EVI-0002",
                "type": "evidence",
                "schema_version": 1,
                "status": "active",
                "title": "Other",
                "statement": "Something happened.",
                "kind": "other",
            }
        )


def test_assumption_requires_scope() -> None:
    with pytest.raises(ValidationError):
        make_assumption(scope="")
    with pytest.raises(ValidationError):
        make_assumption(scope="   ")


def test_whitespace_only_required_scientific_text_rejected() -> None:
    with pytest.raises(ValidationError):
        make_idea(statement=" ")
    with pytest.raises(ValidationError):
        make_claim(statement="\t")
    with pytest.raises(ValidationError):
        make_decision(rationale="   ")
    with pytest.raises(ValidationError):
        make_experiment(purpose="\n")
    with pytest.raises(ValidationError):
        make_evidence(statement="   ")
    with pytest.raises(ValidationError):
        make_experiment(result_manifest="   ")
    with pytest.raises(ValidationError):
        make_experiment(artifacts=["   "])
    with pytest.raises(ValidationError):
        make_experiment(status="specified", decision_rule="   ")
    with pytest.raises(ValidationError):
        make_claim(contrary_evidence=["EVI-0001"], contrary_evidence_addressed="  ")
    with pytest.raises(ValidationError):
        make_idea(status="discarded", retire_reason="   ")
    with pytest.raises(ValidationError):
        make_idea(revisit_if="   ")
    with pytest.raises(ValidationError):
        make_hypothesis(status="rejected", retire_reason="\t")
    with pytest.raises(ValidationError):
        make_provenance(data="   ")
    Project.model_validate(
        {
            "id": "demo-project",
            "title": "Demo",
            "capsule_version": 1,
            "status": "active",
        }
    )
    with pytest.raises(ValidationError):
        Project.model_validate(
            {
                "id": "demo-project",
                "title": "   ",
                "capsule_version": 1,
                "status": "active",
            }
        )


def test_duplicate_supersedes_rejected() -> None:
    with pytest.raises(ValidationError):
        make_question(id="Q-0003", supersedes=["Q-0001", "Q-0001"])


def test_supersedes_must_be_same_type() -> None:
    with pytest.raises(ValidationError):
        make_question(supersedes=["IDEA-0001"])


def test_mutable_list_defaults_are_not_shared() -> None:
    first = make_question()
    second = make_question(id="Q-0002")
    first.created_from.append("Q-0099")
    assert second.created_from == []


def test_project_schema() -> None:
    project = Project.model_validate(
        {
            "id": "demo-project",
            "title": "Demo",
            "capsule_version": 1,
            "status": ProjectStatus.ACTIVE,
        }
    )
    assert project.description is None
    with pytest.raises(ValidationError):
        Project.model_validate(
            {
                "id": "Bad",
                "title": "Demo",
                "capsule_version": 1,
                "status": "active",
            }
        )
    with pytest.raises(ValidationError):
        Project.model_validate(
            {
                "id": "demo-project",
                "title": "Demo",
                "capsule_version": 1,
                "status": "active",
                "secrets": "no",
            }
        )


def test_parse_object_dispatches_on_type() -> None:
    obj = parse_object(
        {
            "id": "Q-0001",
            "type": "question",
            "schema_version": 1,
            "status": "open",
            "title": "T",
            "statement": "S",
        }
    )
    assert isinstance(obj, Question)
    with pytest.raises(ValueError, match="unknown object type"):
        parse_object({"type": "paper", "id": "PAPER-0001"})


PREREGISTERED_STATUSES = ("specified", "running", "completed", "failed", "superseded")


@pytest.mark.parametrize("status", PREREGISTERED_STATUSES)
@pytest.mark.parametrize("missing", ["predictions", "primary_metrics", "decision_rule"])
def test_preregistered_experiment_requires_ex_ante_commitments(
    status: str,
    missing: str,
) -> None:
    make_experiment(status=status)
    with pytest.raises(ValidationError):
        make_experiment(status=status, **{missing: None})


@pytest.mark.parametrize("status", PREREGISTERED_STATUSES)
def test_preregistered_experiment_rejects_empty_commitments(status: str) -> None:
    with pytest.raises(ValidationError):
        make_experiment(status=status, predictions=[])
    with pytest.raises(ValidationError):
        make_experiment(status=status, primary_metrics=[])


@pytest.mark.parametrize("status", ["draft", "withdrawn"])
def test_draft_and_withdrawn_experiments_are_exempt(status: str) -> None:
    experiment = make_experiment(status=status)
    assert experiment.predictions is None
    assert experiment.primary_metrics is None
    assert experiment.decision_rule is None


def test_prediction_hypothesis_must_be_listed_on_experiment() -> None:
    with pytest.raises(ValidationError):
        make_experiment(
            status="specified",
            hypotheses=["HYP-0001"],
            predictions=[
                {
                    "hypothesis": "HYP-0002",
                    "predicted_outcome": "Heldout RMSE falls below 0.20.",
                    "discriminates": True,
                }
            ],
        )
    experiment = make_experiment(
        status="specified",
        hypotheses=["HYP-0001", "HYP-0002"],
        predictions=[
            {
                "hypothesis": "HYP-0002",
                "predicted_outcome": "Heldout RMSE falls below 0.20.",
                "discriminates": True,
            }
        ],
    )
    assert experiment.predictions is not None
    assert experiment.predictions[0].hypothesis == "HYP-0002"


def test_prediction_requires_outcome_and_discriminates() -> None:
    Prediction.model_validate(
        {
            "hypothesis": "HYP-0001",
            "predicted_outcome": "Heldout RMSE falls below 0.20.",
            "discriminates": False,
        }
    )
    with pytest.raises(ValidationError):
        Prediction.model_validate({"hypothesis": "HYP-0001", "predicted_outcome": "x"})
    with pytest.raises(ValidationError):
        Prediction.model_validate(
            {
                "hypothesis": "HYP-0001",
                "predicted_outcome": "   ",
                "discriminates": True,
            }
        )
    with pytest.raises(ValidationError):
        Prediction.model_validate(
            {
                "hypothesis": "CLAIM-0001",
                "predicted_outcome": "x",
                "discriminates": True,
            }
        )
    with pytest.raises(ValidationError):
        Prediction.model_validate(
            {
                "hypothesis": "HYP-0001",
                "predicted_outcome": "x",
                "discriminates": True,
                "confidence": 0.5,
            }
        )


def test_primary_metrics_reject_blank_and_duplicate_entries() -> None:
    with pytest.raises(ValidationError):
        make_experiment(status="specified", primary_metrics=["heldout_rmse", "  "])
    with pytest.raises(ValidationError):
        make_experiment(
            status="specified", primary_metrics=["heldout_rmse", "heldout_rmse"]
        )
    experiment = make_experiment(
        status="specified", primary_metrics=["heldout_rmse", "auroc"]
    )
    assert experiment.primary_metrics == ["heldout_rmse", "auroc"]


def test_accepted_claim_with_contrary_evidence_requires_a_response() -> None:
    with pytest.raises(ValidationError):
        make_claim(
            status="accepted",
            supporting_evidence=["EVI-0001"],
            contrary_evidence=["EVI-0002"],
        )
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        contrary_evidence=["EVI-0002"],
        contrary_evidence_addressed="The contrary result applies below 200 K.",
    )
    assert claim.contrary_evidence_addressed is not None


def test_unaccepted_claim_may_carry_unaddressed_contrary_evidence() -> None:
    claim = make_claim(
        status="evidence_linked",
        supporting_evidence=["EVI-0001"],
        contrary_evidence=["EVI-0002"],
    )
    assert claim.contrary_evidence == ["EVI-0002"]
    assert claim.contrary_evidence_addressed is None


def test_contrary_evidence_addressed_requires_contrary_evidence() -> None:
    with pytest.raises(ValidationError):
        make_claim(contrary_evidence_addressed="Nothing to address.")
    with pytest.raises(ValidationError):
        make_claim(
            contrary_evidence=[],
            contrary_evidence_addressed="Nothing to address.",
        )


def test_claim_rejects_the_old_flat_evidence_field() -> None:
    with pytest.raises(ValidationError):
        make_claim(evidence=["EVI-0001"])


def test_discarded_idea_requires_retire_reason() -> None:
    with pytest.raises(ValidationError):
        make_idea(status="discarded", retire_reason=None)
    idea = make_idea(
        status="discarded",
        retire_reason="Existing data cannot identify the mechanism.",
        revisit_if="Facility-level cooling telemetry becomes available.",
    )
    assert idea.retire_reason is not None
    assert idea.revisit_if is not None
    for status in ("draft", "active", "promoted"):
        assert make_idea(status=status).retire_reason is None


@pytest.mark.parametrize("status", ["rejected", "withdrawn"])
def test_retired_hypothesis_requires_retire_reason(status: str) -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(status=status, retire_reason=None)
    hypothesis = make_hypothesis(
        status=status,
        retire_reason="The mechanism is not identifiable from this data.",
    )
    assert hypothesis.retire_reason is not None


@pytest.mark.parametrize("status", ["draft", "active", "testing", "supported"])
def test_live_hypothesis_does_not_require_retire_reason(status: str) -> None:
    assert make_hypothesis(status=status).retire_reason is None


def test_superseded_objects_are_exempt_from_retire_reason() -> None:
    assert make_idea(status="superseded").retire_reason is None
    assert make_hypothesis(status="superseded").retire_reason is None


@pytest.mark.parametrize(
    "git_commit",
    [
        "deadbee",
        "deadbeef",
        "0123456789abcdef0123456789abcdef01234567",
        "a1b2c3d",
    ],
)
def test_valid_git_commit_shapes(git_commit: str) -> None:
    assert make_provenance(git_commit=git_commit).git_commit == git_commit


@pytest.mark.parametrize(
    "git_commit",
    [
        "DEADBEEF",
        "deadbe",
        "not-a-commit",
        "0123456789abcdef0123456789abcdef012345678",
        "HEAD",
        "deadbeef ",
    ],
)
def test_invalid_git_commit_shapes_fail(git_commit: str) -> None:
    with pytest.raises(ValidationError):
        make_provenance(git_commit=git_commit)


@pytest.mark.parametrize("field", ["code", "config"])
@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "/scratch/nic/run.py",
        "../shared/code.py",
        "src/../../escape.py",
        "~/code.py",
        "src\\windows.py",
        "C:/code.py",
        "./src/code.py",
        "src//code.py",
        ".",
        "..",
    ],
)
def test_absolute_and_traversal_code_paths_fail(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_provenance(**{field: value})


@pytest.mark.parametrize("field", ["code", "config"])
@pytest.mark.parametrize(
    "value",
    ["src/experiment.py", "configs/run.toml", "data/raw/", "a", "deep/nested/path.py"],
)
def test_repository_relative_code_paths_pass(field: str, value: str) -> None:
    assert getattr(make_provenance(**{field: value}), field) == value


@pytest.mark.parametrize(
    "value",
    [
        "data/raw/",
        "/scratch/nic/runs/2026",
        "/mnt/institutional/share/dataset",
        "s3://bucket/dataset",
        "https://example.org/dataset.csv",
        "doi:10.5281/zenodo.1234567",
        "warehouse.analytics.runs_2026",
        "../shared/data",
        "C:\\data\\raw",
    ],
)
def test_data_accepts_any_nonblank_opaque_locator(value: str) -> None:
    """Real datasets live outside Git; WP-A resolves and restricts nothing."""

    assert make_provenance(data=value).data == value


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_data_locator_fails(value: str) -> None:
    with pytest.raises(ValidationError):
        make_provenance(data=value)


def test_provenance_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate(
            {
                "code": "src/experiment.py",
                "config": "configs/run.toml",
                "data": "data/raw/",
                "git_commit": "deadbeef",
                "dataset_sha256": "a" * 64,
            }
        )
