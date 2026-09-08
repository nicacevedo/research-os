"""Local Pydantic schema tests for Research Capsule objects."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_os.models import (
    AssumptionStatus,
    ClaimStatus,
    DecisionStatus,
    EvidenceStatus,
    ExperimentStatus,
    HypothesisStatus,
    IdeaStatus,
    ObjectType,
    Project,
    ProjectStatus,
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


def test_confidence_basis_coupling() -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=0.4)
    with pytest.raises(ValidationError):
        make_hypothesis(confidence=0.4, confidence_basis="")
    with pytest.raises(ValidationError):
        make_hypothesis(confidence_basis="stated without a number")
    obj = make_hypothesis(confidence=0.2, confidence_basis="prior work")
    assert obj.confidence == 0.2


def test_non_draft_hypothesis_requires_falsification() -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(status="active", falsification=None)
    with pytest.raises(ValidationError):
        make_hypothesis(status="active", falsification="")
    make_hypothesis(status="draft", falsification=None)


def test_completed_experiment_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        make_experiment(status="completed", provenance=None)
    with pytest.raises(ValidationError):
        make_experiment(
            status="completed",
            provenance={"code": "x", "config": "y", "data": "", "git_commit": "z"},
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
    make_decision(status="proposed", alternatives_considered=None)


def test_concluded_review_requires_findings_verdict_and_digest() -> None:
    with pytest.raises(ValidationError):
        make_review(status="concluded", findings=None)
    with pytest.raises(ValidationError):
        make_review(status="concluded", verdict=None)
    with pytest.raises(ValidationError):
        make_review(status="concluded", subject_digest=None)
    with pytest.raises(ValidationError):
        make_review(status="concluded", subject_digest="A" * 64)
    make_review(status="draft", findings=None, verdict=None, subject_digest=None)


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
    make_evidence(kind="experiment", experiment="EXP-0002")
    with pytest.raises(ValidationError):
        make_evidence(kind="experiment", experiment=None)
    make_evidence(kind="other", notes="seen in the lab", citation=None)
    make_evidence(kind="other", citation="internal memo", notes=None)
    with pytest.raises(ValidationError):
        make_evidence(kind="other", citation=None, notes=None)


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
