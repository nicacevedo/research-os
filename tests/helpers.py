"""In-memory builders for M2 scientific-object tests."""

from __future__ import annotations

from typing import Any

from research_os.models import (
    DIGEST_VERSION,
    Assumption,
    Claim,
    Decision,
    Evidence,
    Experiment,
    Hypothesis,
    Idea,
    Provenance,
    Question,
    Review,
)

TEST_PROJECT_ID = "alpha-project"
OTHER_PROJECT_ID = "beta-project"

_PREREGISTERED_STATUSES = frozenset(
    {"specified", "running", "completed", "failed", "superseded"}
)
_RETIRED_HYPOTHESIS_STATUSES = frozenset({"rejected", "withdrawn"})


def _merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    data = dict(base)
    data.update(updates)
    return data


def make_question(**updates: Any) -> Question:
    return Question.model_validate(
        _merge(
            {
                "id": "Q-0001",
                "type": "question",
                "schema_version": 1,
                "status": "open",
                "title": "A question",
                "statement": "Why does this happen?",
            },
            updates,
        )
    )


def make_idea(**updates: Any) -> Idea:
    status = updates.get("status", "draft")
    base: dict[str, Any] = {
        "id": "IDEA-0001",
        "type": "idea",
        "schema_version": 1,
        "status": status,
        "title": "An idea",
        "statement": "Perhaps a mechanism exists.",
    }
    if status == "discarded":
        base["retire_reason"] = "Existing data cannot identify the mechanism."
    return Idea.model_validate(_merge(base, updates))


def make_hypothesis(**updates: Any) -> Hypothesis:
    status = updates.get("status", "draft")
    base: dict[str, Any] = {
        "id": "HYP-0001",
        "type": "hypothesis",
        "schema_version": 1,
        "status": status,
        "title": "A hypothesis",
        "statement": "X causes Y.",
    }
    if status != "draft":
        base["falsification"] = "Observe not-Y after X."
    if status in _RETIRED_HYPOTHESIS_STATUSES:
        base["retire_reason"] = "The mechanism is not identifiable from this data."
    return Hypothesis.model_validate(_merge(base, updates))


def make_assumption(**updates: Any) -> Assumption:
    return Assumption.model_validate(
        _merge(
            {
                "id": "ASM-0001",
                "type": "assumption",
                "schema_version": 1,
                "status": "active",
                "title": "An assumption",
                "statement": "The system is closed.",
                "scope": "This project",
            },
            updates,
        )
    )


def make_claim(**updates: Any) -> Claim:
    return Claim.model_validate(
        _merge(
            {
                "id": "CLAIM-0001",
                "type": "claim",
                "schema_version": 1,
                "status": "draft",
                "title": "A claim",
                "statement": "X is supported.",
            },
            updates,
        )
    )


def make_decision(**updates: Any) -> Decision:
    status = updates.get("status", "proposed")
    base: dict[str, Any] = {
        "id": "DEC-0001",
        "type": "decision",
        "schema_version": 1,
        "status": status,
        "title": "A decision",
        "statement": "Use method A.",
        "rationale": "It is simpler.",
    }
    if status == "accepted":
        base["alternatives_considered"] = ["method B"]
    return Decision.model_validate(_merge(base, updates))


def make_provenance(**updates: Any) -> Provenance:
    return Provenance.model_validate(
        _merge(
            {
                "code": "src/experiment.py",
                "config": "configs/run.toml",
                "data": "data/raw/",
                "git_commit": "deadbeef",
            },
            updates,
        )
    )


def make_experiment(**updates: Any) -> Experiment:
    status = updates.get("status", "draft")
    base: dict[str, Any] = {
        "id": "EXP-0001",
        "type": "experiment",
        "schema_version": 1,
        "status": status,
        "title": "An experiment",
        "purpose": "Test the hypothesis.",
    }
    if status != "draft":
        base["hypotheses"] = ["HYP-0001"]
    if status in _PREREGISTERED_STATUSES:
        hypotheses = updates.get("hypotheses") or base.get("hypotheses") or ["HYP-0001"]
        base["predictions"] = [
            {
                "hypothesis": hypotheses[0],
                "predicted_outcome": "Heldout RMSE falls below 0.20.",
                "discriminates": True,
            }
        ]
        base["primary_metrics"] = ["heldout_rmse"]
        base["decision_rule"] = (
            "Reject the hypothesis if heldout_rmse is at least 0.20."
        )
    if status == "completed":
        base["provenance"] = make_provenance().model_dump()
    return Experiment.model_validate(_merge(base, updates))


def make_review(**updates: Any) -> Review:
    status = updates.get("status", "draft")
    base: dict[str, Any] = {
        "id": "REV-0001",
        "type": "review",
        "schema_version": 1,
        "status": status,
        "title": "A review",
        "subject": "CLAIM-0001",
        "reviewer_kind": "human",
    }
    if status == "concluded":
        base["findings"] = "Looks correct."
        base["verdict"] = "approve"
        base["subject_digest"] = f"{DIGEST_VERSION}:{'a' * 64}"
    return Review.model_validate(_merge(base, updates))


def make_evidence(**updates: Any) -> Evidence:
    kind = updates.get("kind", "literature")
    base: dict[str, Any] = {
        "id": "EVI-0001",
        "type": "evidence",
        "schema_version": 1,
        "status": "active",
        "title": "Evidence",
        "statement": "The source supports X.",
        "kind": kind,
    }
    if kind == "literature":
        base["citation"] = "Smith 2020"
    elif kind == "experiment":
        base["experiment"] = "EXP-0001"
    else:
        base["notes"] = "Lab notebook observation."
    return Evidence.model_validate(_merge(base, updates))
