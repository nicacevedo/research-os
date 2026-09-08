"""Tests for Layer B status-transition graphs."""

from __future__ import annotations

import pytest

from research_os.models import (
    AssumptionStatus,
    ClaimStatus,
    DecisionStatus,
    EvidenceStatus,
    ExperimentStatus,
    HypothesisStatus,
    IdeaStatus,
    ObjectType,
    ProjectStatus,
    QuestionStatus,
    ReviewStatus,
)
from research_os.transitions import PROJECT_TYPE, is_valid_transition

LEGAL_EDGES: dict[str, set[tuple[str, str]]] = {
    ObjectType.QUESTION: {
        (QuestionStatus.OPEN, QuestionStatus.PAUSED),
        (QuestionStatus.PAUSED, QuestionStatus.OPEN),
        (QuestionStatus.OPEN, QuestionStatus.ANSWERED),
        (QuestionStatus.OPEN, QuestionStatus.WITHDRAWN),
        (QuestionStatus.OPEN, QuestionStatus.SUPERSEDED),
        (QuestionStatus.PAUSED, QuestionStatus.ANSWERED),
        (QuestionStatus.PAUSED, QuestionStatus.WITHDRAWN),
        (QuestionStatus.PAUSED, QuestionStatus.SUPERSEDED),
        (QuestionStatus.ANSWERED, QuestionStatus.WITHDRAWN),
        (QuestionStatus.ANSWERED, QuestionStatus.SUPERSEDED),
    },
    ObjectType.IDEA: {
        (IdeaStatus.DRAFT, IdeaStatus.ACTIVE),
        (IdeaStatus.DRAFT, IdeaStatus.DISCARDED),
        (IdeaStatus.DRAFT, IdeaStatus.SUPERSEDED),
        (IdeaStatus.ACTIVE, IdeaStatus.PROMOTED),
        (IdeaStatus.ACTIVE, IdeaStatus.DISCARDED),
        (IdeaStatus.ACTIVE, IdeaStatus.SUPERSEDED),
        (IdeaStatus.PROMOTED, IdeaStatus.SUPERSEDED),
    },
    ObjectType.HYPOTHESIS: {
        (HypothesisStatus.DRAFT, HypothesisStatus.ACTIVE),
        (HypothesisStatus.DRAFT, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.DRAFT, HypothesisStatus.SUPERSEDED),
        (HypothesisStatus.ACTIVE, HypothesisStatus.TESTING),
        (HypothesisStatus.ACTIVE, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.ACTIVE, HypothesisStatus.SUPERSEDED),
        (HypothesisStatus.TESTING, HypothesisStatus.SUPPORTED),
        (HypothesisStatus.TESTING, HypothesisStatus.REJECTED),
        (HypothesisStatus.TESTING, HypothesisStatus.INCONCLUSIVE),
        (HypothesisStatus.TESTING, HypothesisStatus.ACTIVE),
        (HypothesisStatus.TESTING, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.TESTING, HypothesisStatus.SUPERSEDED),
        (HypothesisStatus.SUPPORTED, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.SUPPORTED, HypothesisStatus.SUPERSEDED),
        (HypothesisStatus.REJECTED, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.REJECTED, HypothesisStatus.SUPERSEDED),
        (HypothesisStatus.INCONCLUSIVE, HypothesisStatus.WITHDRAWN),
        (HypothesisStatus.INCONCLUSIVE, HypothesisStatus.SUPERSEDED),
    },
    ObjectType.ASSUMPTION: {
        (AssumptionStatus.ACTIVE, AssumptionStatus.RELAXED),
        (AssumptionStatus.ACTIVE, AssumptionStatus.WITHDRAWN),
        (AssumptionStatus.ACTIVE, AssumptionStatus.SUPERSEDED),
        (AssumptionStatus.RELAXED, AssumptionStatus.WITHDRAWN),
        (AssumptionStatus.RELAXED, AssumptionStatus.SUPERSEDED),
    },
    ObjectType.CLAIM: {
        (ClaimStatus.DRAFT, ClaimStatus.EVIDENCE_LINKED),
        (ClaimStatus.DRAFT, ClaimStatus.WITHDRAWN),
        (ClaimStatus.DRAFT, ClaimStatus.SUPERSEDED),
        (ClaimStatus.EVIDENCE_LINKED, ClaimStatus.ACCEPTED),
        (ClaimStatus.EVIDENCE_LINKED, ClaimStatus.WITHDRAWN),
        (ClaimStatus.EVIDENCE_LINKED, ClaimStatus.SUPERSEDED),
        (ClaimStatus.ACCEPTED, ClaimStatus.WITHDRAWN),
        (ClaimStatus.ACCEPTED, ClaimStatus.SUPERSEDED),
    },
    ObjectType.DECISION: {
        (DecisionStatus.PROPOSED, DecisionStatus.ACCEPTED),
        (DecisionStatus.PROPOSED, DecisionStatus.WITHDRAWN),
        (DecisionStatus.PROPOSED, DecisionStatus.SUPERSEDED),
        (DecisionStatus.ACCEPTED, DecisionStatus.WITHDRAWN),
        (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED),
    },
    ObjectType.EXPERIMENT: {
        (ExperimentStatus.DRAFT, ExperimentStatus.SPECIFIED),
        (ExperimentStatus.DRAFT, ExperimentStatus.WITHDRAWN),
        (ExperimentStatus.DRAFT, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.SPECIFIED, ExperimentStatus.RUNNING),
        (ExperimentStatus.SPECIFIED, ExperimentStatus.FAILED),
        (ExperimentStatus.SPECIFIED, ExperimentStatus.WITHDRAWN),
        (ExperimentStatus.SPECIFIED, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.RUNNING, ExperimentStatus.COMPLETED),
        (ExperimentStatus.RUNNING, ExperimentStatus.FAILED),
        (ExperimentStatus.RUNNING, ExperimentStatus.SPECIFIED),
        (ExperimentStatus.RUNNING, ExperimentStatus.WITHDRAWN),
        (ExperimentStatus.RUNNING, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.FAILED, ExperimentStatus.SPECIFIED),
        (ExperimentStatus.FAILED, ExperimentStatus.WITHDRAWN),
        (ExperimentStatus.FAILED, ExperimentStatus.SUPERSEDED),
        (ExperimentStatus.COMPLETED, ExperimentStatus.WITHDRAWN),
        (ExperimentStatus.COMPLETED, ExperimentStatus.SUPERSEDED),
    },
    ObjectType.REVIEW: {
        (ReviewStatus.DRAFT, ReviewStatus.SUBMITTED),
        (ReviewStatus.DRAFT, ReviewStatus.WITHDRAWN),
        (ReviewStatus.SUBMITTED, ReviewStatus.CONCLUDED),
        (ReviewStatus.SUBMITTED, ReviewStatus.DRAFT),
        (ReviewStatus.SUBMITTED, ReviewStatus.WITHDRAWN),
        (ReviewStatus.CONCLUDED, ReviewStatus.WITHDRAWN),
    },
    ObjectType.EVIDENCE: {
        (EvidenceStatus.ACTIVE, EvidenceStatus.WITHDRAWN),
        (EvidenceStatus.ACTIVE, EvidenceStatus.SUPERSEDED),
    },
    PROJECT_TYPE: {
        (ProjectStatus.ACTIVE, ProjectStatus.PAUSED),
        (ProjectStatus.PAUSED, ProjectStatus.ACTIVE),
        (ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED),
        (ProjectStatus.PAUSED, ProjectStatus.ARCHIVED),
        (ProjectStatus.ARCHIVED, ProjectStatus.ACTIVE),
        (ProjectStatus.ARCHIVED, ProjectStatus.PAUSED),
    },
}

STATUSES: dict[str, tuple[str, ...]] = {
    ObjectType.QUESTION: tuple(QuestionStatus),
    ObjectType.IDEA: tuple(IdeaStatus),
    ObjectType.HYPOTHESIS: tuple(HypothesisStatus),
    ObjectType.ASSUMPTION: tuple(AssumptionStatus),
    ObjectType.CLAIM: tuple(ClaimStatus),
    ObjectType.DECISION: tuple(DecisionStatus),
    ObjectType.EXPERIMENT: tuple(ExperimentStatus),
    ObjectType.REVIEW: tuple(ReviewStatus),
    ObjectType.EVIDENCE: tuple(EvidenceStatus),
    PROJECT_TYPE: tuple(ProjectStatus),
}

ILLEGAL_EXAMPLES = (
    (ObjectType.QUESTION, QuestionStatus.ANSWERED, QuestionStatus.OPEN),
    (ObjectType.IDEA, IdeaStatus.DRAFT, IdeaStatus.PROMOTED),
    (ObjectType.HYPOTHESIS, HypothesisStatus.DRAFT, HypothesisStatus.TESTING),
    (ObjectType.HYPOTHESIS, HypothesisStatus.SUPPORTED, HypothesisStatus.ACTIVE),
    (ObjectType.ASSUMPTION, AssumptionStatus.RELAXED, AssumptionStatus.ACTIVE),
    (ObjectType.CLAIM, ClaimStatus.DRAFT, ClaimStatus.ACCEPTED),
    (ObjectType.CLAIM, ClaimStatus.ACCEPTED, ClaimStatus.EVIDENCE_LINKED),
    (ObjectType.DECISION, DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED),
    (ObjectType.EXPERIMENT, ExperimentStatus.SPECIFIED, ExperimentStatus.COMPLETED),
    (ObjectType.EXPERIMENT, ExperimentStatus.DRAFT, ExperimentStatus.RUNNING),
    (ObjectType.EXPERIMENT, ExperimentStatus.COMPLETED, ExperimentStatus.RUNNING),
    (ObjectType.REVIEW, ReviewStatus.DRAFT, ReviewStatus.CONCLUDED),
    (ObjectType.REVIEW, ReviewStatus.CONCLUDED, ReviewStatus.SUBMITTED),
    (ObjectType.EVIDENCE, EvidenceStatus.WITHDRAWN, EvidenceStatus.ACTIVE),
    (PROJECT_TYPE, ProjectStatus.ARCHIVED, "deleted"),
)


@pytest.mark.parametrize("object_type", sorted(LEGAL_EDGES))
def test_every_legal_edge(object_type: str) -> None:
    for old, new in LEGAL_EDGES[object_type]:
        assert is_valid_transition(object_type, old, new) is True


@pytest.mark.parametrize("object_type", sorted(STATUSES))
def test_same_status_is_always_valid(object_type: str) -> None:
    for status in STATUSES[object_type]:
        assert is_valid_transition(object_type, status, status) is True


@pytest.mark.parametrize(("object_type", "old", "new"), ILLEGAL_EXAMPLES)
def test_representative_illegal_edges(object_type: str, old: str, new: str) -> None:
    assert is_valid_transition(object_type, old, new) is False


def test_claim_draft_to_accepted_is_illegal() -> None:
    assert is_valid_transition("claim", "draft", "accepted") is False
    assert is_valid_transition("claim", "draft", "evidence_linked") is True
    assert is_valid_transition("claim", "evidence_linked", "accepted") is True


def test_no_unlisted_cross_status_edges() -> None:
    for object_type, statuses in STATUSES.items():
        legal = LEGAL_EDGES[object_type]
        for old in statuses:
            for new in statuses:
                expected = old == new or (old, new) in legal
                assert is_valid_transition(object_type, old, new) is expected


def test_unknown_object_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown object type"):
        is_valid_transition("paper", "draft", "draft")
