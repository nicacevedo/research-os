"""Fixtures and builders for the discovery portfolio's tests.

Separate from ``runtime_helpers`` because the portfolio is a separate layer,
and shaped the same way so that a reader who knows one knows the other.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from research_os.portfolio.models import (
    AdjudicationType,
    ContextClass,
    IdeaOrigin,
    QualityDimensions,
    ReviewerRole,
    ReviewVerdict,
    Severity,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.interfaces import Independence

#: A complete, plausible idea. Tests override the one field they are about,
#: so a test reads as its own subject rather than as a wall of setup.
IDEA_FIELDS: dict[str, Any] = {
    "title": "Column generation and working sets may not coincide",
    "research_question": (
        "Do column-generation and working-set methods for L1-regularised "
        "regression visit the same sequence of supports?"
    ),
    "core_idea": (
        "Column generation prices out a single coordinate per iteration while a "
        "working-set method may add several, so their support trajectories can "
        "diverge even when both converge to the same optimum."
    ),
    "mechanism": (
        "The pricing subproblem selects by dual violation; the working-set rule "
        "selects by a screening bound. The two orderings differ whenever the "
        "screening bound is not monotone in the dual violation."
    ),
    "why_it_matters": (
        "If the trajectories differ, results reported for one are not evidence "
        "about the other, and several comparisons in the literature assume they "
        "are interchangeable."
    ),
    "falsifier": (
        "Exhibit a problem instance on which both methods visit an identical "
        "sequence of supports for every regularisation level in a sweep, or "
        "prove the orderings coincide."
    ),
    "adjudication_types": [AdjudicationType.MATHEMATICAL],
    "closest_prior_work": "Screening rules for the Lasso (unverified).",
    "claimed_difference": "The trajectory, rather than the optimum, is compared.",
    "assumptions": ["The design matrix has full column rank on the active set."],
    "alternative_explanations": ["The difference is an artefact of tie-breaking."],
    "open_uncertainties": ["Whether the divergence survives exact arithmetic."],
    "next_best_action": "Search for a two-coordinate counterexample.",
}


def idea_fields(**overrides: Any) -> dict[str, Any]:
    return {**IDEA_FIELDS, **overrides}


@pytest.fixture
def portfolio(runtime_db: Database) -> Iterator[PortfolioStore]:
    yield PortfolioStore(runtime_db)


def record_review(
    store: PortfolioStore,
    *,
    idea_id: str,
    version: int,
    role: ReviewerRole,
    verdict: ReviewVerdict = ReviewVerdict.PASS,
    severity: Severity = Severity.NONE,
    provider: str = "codex",
    family: str = "openai",
    model: str | None = "gpt-x",
    prompt_version: str | None = None,
    independence: Independence = Independence.DIFFERENT_FAMILY,
    call_id: str | None = None,
    summary: str = "no objection",
    recommendation: Any = None,
) -> Any:
    """Record a review bound to the idea's *current* content and evidence.

    Binding to the current digests is the default because a test that wants a
    stale review should have to say so; the hazard this layer exists to avoid
    is a review that silently stops being about what it read.
    """

    head = store.require_version(idea_id, version)
    review, _ = store.record_review(
        idea_id=idea_id,
        idea_version=version,
        reviewer_role=role,
        verdict=verdict,
        severity=severity,
        summary=summary,
        recommendation=recommendation,
        reviewed_content_digest=head.content_digest,
        reviewed_evidence_digest=store.evidence_digest(
            idea_id=idea_id, idea_version=version
        ),
        packet_digest=f"packet:{idea_id}:{version}",
        prompt_version=prompt_version or f"{role}@1",
        provider=provider,
        provider_family=family,
        model=model,
        independence_vs_origin=independence,
        context_class=str(ContextClass.FROZEN_PACKET),
        call_id=call_id,
    )
    return review


def seed_idea(
    store: PortfolioStore,
    project_id: str,
    *,
    origin: IdeaOrigin = IdeaOrigin.BLIND_EXPLORER,
    dimensions: QualityDimensions | None = None,
    **overrides: Any,
) -> tuple[Any, Any]:
    return store.create_idea(
        project_id=project_id,
        origin=origin,
        fields=idea_fields(**overrides),
        origin_role="blind_explorer",
        dimensions=dimensions,
    )


# ------------------------------------------------- the scientific contract --
#: Keys a pre-contract design carried that a contract-bound design does not:
#: the rule moved to the analysis, and the endpoint became its estimand.
_RULE_KEYS = frozenset(
    {
        "decision_rule",
        "no_decision_rule_reason",
        "primary_endpoint",
        "secondary_endpoints",
    }
)


def analysis_answer(design: Mapping[str, Any]) -> dict[str, Any]:
    """The analysis a pre-contract design's rule *was*, in the contract's shape.

    A design written the old way named one number in one file and two
    thresholds on it. That is exactly the analysis ``value`` of one scalar
    observable, so a test scripted in the old shape keeps meaning what it
    meant: the same number, read by the same predicates -- now frozen first,
    by the analysis designer, rather than by the design.
    """

    rule = design.get("decision_rule")
    if not rule:
        return {
            "analysable": False,
            "unanalysable_reason": design.get("no_decision_rule_reason")
            or "no single machine-checkable number settles this",
        }
    return {
        "analysable": True,
        "estimand": rule.get("metric_description") or rule["metric_path"],
        "observables": [
            {
                "name": "metric",
                "source": rule["output_path"],
                "kind": "scalar",
                "path": rule["metric_path"],
            }
        ],
        "reductions": [{"name": "statistic", "op": "value", "observable": "metric"}],
        "primary_statistic": "statistic",
        "success": rule["success"],
        "failure": rule["failure"],
    }


def design_spec_answer(design: Mapping[str, Any]) -> dict[str, Any]:
    """The same design with its rule removed: what a v7 designer returns."""

    shaped = {key: value for key, value in design.items() if key not in _RULE_KEYS}
    if shaped.get("testable", True):
        shaped.setdefault(
            "falsification_criterion", "the clause of the falsifier this measures"
        )
    return shaped


def contract_answers(design: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Role -> answer for both halves of a contract, from one old-shape design."""

    return {
        "analysis_designer": analysis_answer(design),
        "experimentalist": design_spec_answer(design),
    }


def contract_prompt_answers(
    *,
    primary: Mapping[str, Any],
    replication: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Prompt identity -> answer, for scripting both contract roles by template.

    The replication designer shares the ``replicator`` role, so a double
    keyed by role cannot tell it from the replicator; keyed by prompt it can.
    A replication inherits the primary's frozen analysis, so only its design
    is scripted.
    """

    from research_os.portfolio.prompts import TEMPLATES

    answers = {
        TEMPLATES["analysis_designer"].identity: analysis_answer(primary),
        TEMPLATES["experiment_designer"].identity: design_spec_answer(primary),
    }
    if replication is not None:
        answers[TEMPLATES["replication_designer"].identity] = design_spec_answer(
            replication
        )
    return answers
