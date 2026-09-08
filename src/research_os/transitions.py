"""Lifecycle transition graphs (Layer B).

These functions encode approved status-transition semantics only. They do not
inspect Git, mutate files, or drive a workflow engine. Ordinary validate
(Layer A) does not call this module.
"""

from __future__ import annotations

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

PROJECT_TYPE = "project"

_GRAPH: dict[str, dict[str, frozenset[str]]] = {
    ObjectType.QUESTION: {
        QuestionStatus.OPEN: frozenset(
            {
                QuestionStatus.PAUSED,
                QuestionStatus.ANSWERED,
                QuestionStatus.WITHDRAWN,
                QuestionStatus.SUPERSEDED,
            }
        ),
        QuestionStatus.PAUSED: frozenset(
            {
                QuestionStatus.OPEN,
                QuestionStatus.ANSWERED,
                QuestionStatus.WITHDRAWN,
                QuestionStatus.SUPERSEDED,
            }
        ),
        QuestionStatus.ANSWERED: frozenset(
            {QuestionStatus.WITHDRAWN, QuestionStatus.SUPERSEDED}
        ),
        QuestionStatus.WITHDRAWN: frozenset(),
        QuestionStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.IDEA: {
        IdeaStatus.DRAFT: frozenset(
            {IdeaStatus.ACTIVE, IdeaStatus.DISCARDED, IdeaStatus.SUPERSEDED}
        ),
        IdeaStatus.ACTIVE: frozenset(
            {IdeaStatus.PROMOTED, IdeaStatus.DISCARDED, IdeaStatus.SUPERSEDED}
        ),
        IdeaStatus.PROMOTED: frozenset({IdeaStatus.SUPERSEDED}),
        IdeaStatus.DISCARDED: frozenset(),
        IdeaStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.HYPOTHESIS: {
        HypothesisStatus.DRAFT: frozenset(
            {
                HypothesisStatus.ACTIVE,
                HypothesisStatus.WITHDRAWN,
                HypothesisStatus.SUPERSEDED,
            }
        ),
        HypothesisStatus.ACTIVE: frozenset(
            {
                HypothesisStatus.TESTING,
                HypothesisStatus.WITHDRAWN,
                HypothesisStatus.SUPERSEDED,
            }
        ),
        HypothesisStatus.TESTING: frozenset(
            {
                HypothesisStatus.SUPPORTED,
                HypothesisStatus.REJECTED,
                HypothesisStatus.INCONCLUSIVE,
                HypothesisStatus.ACTIVE,
                HypothesisStatus.WITHDRAWN,
                HypothesisStatus.SUPERSEDED,
            }
        ),
        HypothesisStatus.SUPPORTED: frozenset(
            {HypothesisStatus.WITHDRAWN, HypothesisStatus.SUPERSEDED}
        ),
        HypothesisStatus.REJECTED: frozenset(
            {HypothesisStatus.WITHDRAWN, HypothesisStatus.SUPERSEDED}
        ),
        HypothesisStatus.INCONCLUSIVE: frozenset(
            {HypothesisStatus.WITHDRAWN, HypothesisStatus.SUPERSEDED}
        ),
        HypothesisStatus.WITHDRAWN: frozenset(),
        HypothesisStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.ASSUMPTION: {
        AssumptionStatus.ACTIVE: frozenset(
            {
                AssumptionStatus.RELAXED,
                AssumptionStatus.WITHDRAWN,
                AssumptionStatus.SUPERSEDED,
            }
        ),
        AssumptionStatus.RELAXED: frozenset(
            {AssumptionStatus.WITHDRAWN, AssumptionStatus.SUPERSEDED}
        ),
        AssumptionStatus.WITHDRAWN: frozenset(),
        AssumptionStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.CLAIM: {
        ClaimStatus.DRAFT: frozenset(
            {
                ClaimStatus.EVIDENCE_LINKED,
                ClaimStatus.WITHDRAWN,
                ClaimStatus.SUPERSEDED,
            }
        ),
        ClaimStatus.EVIDENCE_LINKED: frozenset(
            {ClaimStatus.ACCEPTED, ClaimStatus.WITHDRAWN, ClaimStatus.SUPERSEDED}
        ),
        ClaimStatus.ACCEPTED: frozenset(
            {ClaimStatus.WITHDRAWN, ClaimStatus.SUPERSEDED}
        ),
        ClaimStatus.WITHDRAWN: frozenset(),
        ClaimStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.DECISION: {
        DecisionStatus.PROPOSED: frozenset(
            {
                DecisionStatus.ACCEPTED,
                DecisionStatus.WITHDRAWN,
                DecisionStatus.SUPERSEDED,
            }
        ),
        DecisionStatus.ACCEPTED: frozenset(
            {DecisionStatus.WITHDRAWN, DecisionStatus.SUPERSEDED}
        ),
        DecisionStatus.WITHDRAWN: frozenset(),
        DecisionStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.EXPERIMENT: {
        ExperimentStatus.DRAFT: frozenset(
            {
                ExperimentStatus.SPECIFIED,
                ExperimentStatus.WITHDRAWN,
                ExperimentStatus.SUPERSEDED,
            }
        ),
        ExperimentStatus.SPECIFIED: frozenset(
            {
                ExperimentStatus.RUNNING,
                ExperimentStatus.FAILED,
                ExperimentStatus.WITHDRAWN,
                ExperimentStatus.SUPERSEDED,
            }
        ),
        ExperimentStatus.RUNNING: frozenset(
            {
                ExperimentStatus.COMPLETED,
                ExperimentStatus.FAILED,
                ExperimentStatus.SPECIFIED,
                ExperimentStatus.WITHDRAWN,
                ExperimentStatus.SUPERSEDED,
            }
        ),
        ExperimentStatus.FAILED: frozenset(
            {
                ExperimentStatus.SPECIFIED,
                ExperimentStatus.WITHDRAWN,
                ExperimentStatus.SUPERSEDED,
            }
        ),
        ExperimentStatus.COMPLETED: frozenset(
            {ExperimentStatus.WITHDRAWN, ExperimentStatus.SUPERSEDED}
        ),
        ExperimentStatus.WITHDRAWN: frozenset(),
        ExperimentStatus.SUPERSEDED: frozenset(),
    },
    ObjectType.REVIEW: {
        ReviewStatus.DRAFT: frozenset(
            {ReviewStatus.SUBMITTED, ReviewStatus.WITHDRAWN}
        ),
        ReviewStatus.SUBMITTED: frozenset(
            {ReviewStatus.CONCLUDED, ReviewStatus.DRAFT, ReviewStatus.WITHDRAWN}
        ),
        ReviewStatus.CONCLUDED: frozenset({ReviewStatus.WITHDRAWN}),
        ReviewStatus.WITHDRAWN: frozenset(),
    },
    ObjectType.EVIDENCE: {
        EvidenceStatus.ACTIVE: frozenset(
            {EvidenceStatus.WITHDRAWN, EvidenceStatus.SUPERSEDED}
        ),
        EvidenceStatus.WITHDRAWN: frozenset(),
        EvidenceStatus.SUPERSEDED: frozenset(),
    },
    PROJECT_TYPE: {
        ProjectStatus.ACTIVE: frozenset(
            {ProjectStatus.PAUSED, ProjectStatus.ARCHIVED}
        ),
        ProjectStatus.PAUSED: frozenset(
            {ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}
        ),
        ProjectStatus.ARCHIVED: frozenset(
            {ProjectStatus.ACTIVE, ProjectStatus.PAUSED}
        ),
    },
}


def is_valid_transition(object_type: str, old_status: str, new_status: str) -> bool:
    """Return whether ``old_status`` → ``new_status`` is legal for ``object_type``.

    Same-status is always valid when both values are known statuses for the
    type. ``draft`` → ``accepted`` is illegal for claims.
    """

    graph = _GRAPH.get(object_type)
    if graph is None:
        raise ValueError(f"unknown object type: {object_type!r}")
    if old_status not in graph:
        return False
    if old_status == new_status:
        return True
    return new_status in graph[old_status]
