"""The whole path from an action to a human decision, for four kinds of science.

.. code-block:: text

    action
      -> immutable artifact
      -> typed runtime finding
      -> bounded scientific excerpt
      -> next planner / proposal context
      -> grounded proposal
      -> human scientific boundary
      -> canonical state, only if a person acts

The individual seams are tested elsewhere -- ``test_runtime_finding_excerpts``
for the excerpt, ``test_runtime_adjudication_routing`` for the routing,
``test_runtime_science_context`` for the planner's three categories. What this
file holds is the *conjunction*, across the four kinds of scientific work the
system can do, plus the one invariant that spans all of them:

    completed noncanonical science must stay visible to future reasoning,
    and must never silently become canonical science.

The four failure modes below are not hypotheticals. Each one happened on the
live thesis project, and each is asserted here as impossible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.cycles import start_cycle
from research_os.runtime.db import Database
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.policy import ActionKind
from research_os.runtime.sciencecontext import noncanonical_science
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

PROJECT = "lifecycle-project"

CRITIQUE_ANSWER: dict[str, Any] = {
    "proposals": [
        {
            "statement": (
                "The observed speed advantage is an artefact of an inert "
                "tolerance ladder in the harness rather than a property of the "
                "method under test."
            ),
            "mechanism": "An inert ladder measures the harness.",
            "predictions": ["Repairing the ladder removes the advantage."],
            "falsifiers": ["A repaired ladder leaves the advantage intact."],
            "required_data": ["the harness configuration"],
            "proposed_test": "Re-run four cells with a working ladder.",
            "confounds": ["Four cells fail for a different reason."],
            "novelty": "known",
            "confidence": "medium",
        }
    ]
}

DERIVATION_ANSWER: dict[str, Any] = {
    "target": "HYP-0001",
    "convention": "the scaling convention fixed by the project's assumption",
    "assumptions_used": ["ASM-0001"],
    "steps": [
        {
            "claim": "The infimum is finite exactly on the dual feasible set.",
            "justification": "Conjugate of the indicator of the dual ball.",
        }
    ],
    "result": "The biconditional holds under this convention.",
    "outcome": "DERIVED",
    "residual_gaps": ["the degenerate all-zero design"],
    "numerical_witness": {
        "suggested": "Sweep the dual over a grid and look for a counterexample.",
        "what_it_would_show": (
            "That no counterexample is easy to find over the swept range. It "
            "would not establish the biconditional."
        ),
    },
}


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    artifacts = tmp_path / "artifacts"
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    return {
        "db": runtime_db,
        "repo": repo,
        "config": make_config(pg_dsn, artifacts),
        "store": store,
        "artifacts_store": FilesystemArtifactStore(artifacts, store=store),
    }


def run(env: dict[str, Any], action: ActionKind, **answers: Any) -> Any:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(action)),
            "scientific_reviewer": review_answer(),
            **answers,
        }
    )
    result = start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=env["repo"],
        objective="settle what can be settled",
        models=router,
    )
    return result, router


def findings_for(env: dict[str, Any], action: ActionKind) -> list[Any]:
    return [
        env["store"].get_finding(item.finding_id)
        for item in env["store"].list_findings(project_id=PROJECT, limit=50)
        if item.source_action == str(action)
    ]


def capsule_bytes(repo: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in sorted((repo / ".research").rglob("*"))
        if path.is_file()
    }


# --- the four kinds, each end to end ----------------------------------------
@pytest.mark.parametrize(
    ("action", "answers"),
    [
        (ActionKind.CRITIQUE_HYPOTHESES, {"skeptic": CRITIQUE_ANSWER}),
        (ActionKind.DERIVE_MATHEMATICS, {"deriver": DERIVATION_ANSWER}),
        (ActionKind.INSPECT_REPOSITORY, {}),
        (ActionKind.ASSESS_FRONTIER, {}),
    ],
    ids=["critique", "derivation", "inspection", "frontier"],
)
def test_each_kind_of_work_reaches_a_citable_finding_with_its_artifact(
    env: dict[str, Any], action: ActionKind, answers: dict[str, Any]
) -> None:
    result, _router = run(env, action, **answers)
    assert not result.state.get("plan_refusal"), result.state.get("plan_refusal")

    found = findings_for(env, action)
    assert found, f"{action} produced no finding"
    finding = found[0]
    assert finding.finding_id
    assert finding.summary
    # Every artifact the finding names must actually resolve to bytes.
    for artifact_id in finding.artifact_ids:
        assert env["artifacts_store"].exists(artifact_id)
        assert env["artifacts_store"].get_text(artifact_id)


@pytest.mark.parametrize(
    ("action", "answers", "needle"),
    [
        (
            ActionKind.CRITIQUE_HYPOTHESES,
            {"skeptic": CRITIQUE_ANSWER},
            "inert tolerance ladder",
        ),
        (
            ActionKind.DERIVE_MATHEMATICS,
            {"deriver": DERIVATION_ANSWER},
            "would not establish",
        ),
    ],
    ids=["critique", "derivation"],
)
def test_the_substance_survives_into_the_proposal_layer(
    env: dict[str, Any],
    action: ActionKind,
    answers: dict[str, Any],
    needle: str,
) -> None:
    """The producer's own words, not a count of them."""

    from research_os.proposal.planner import render_supplied_findings
    from research_os.runtime.actions.proposals import _supplied

    run(env, action, **answers)
    finding = findings_for(env, action)[0]
    assert needle in finding.excerpt
    assert needle in render_supplied_findings(_supplied((finding,)))


# --- the invariant that spans all of them -----------------------------------
@pytest.mark.parametrize(
    ("action", "answers"),
    [
        (ActionKind.CRITIQUE_HYPOTHESES, {"skeptic": CRITIQUE_ANSWER}),
        (ActionKind.DERIVE_MATHEMATICS, {"deriver": DERIVATION_ANSWER}),
    ],
    ids=["critique", "derivation"],
)
def test_completed_science_is_visible_and_still_not_canonical(
    env: dict[str, Any], action: ActionKind, answers: dict[str, Any]
) -> None:
    before = capsule_bytes(env["repo"])
    frontier_before = ScientificKernelAdapter(env["repo"]).frontier()

    run(env, action, **answers)

    # Visible to future reasoning.
    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert science.findings_total >= 1
    assert any(entry.get("excerpt") for entry in science.findings)

    # And canonical state is byte-identical.
    assert capsule_bytes(env["repo"]) == before
    frontier_after = ScientificKernelAdapter(env["repo"]).frontier()
    assert frontier_after.actionable_hypotheses == frontier_before.actionable_hypotheses
    assert (
        frontier_after.hypotheses_without_tests
        == frontier_before.hypotheses_without_tests
    )
    # Every entry the planner reads says so in its own body.
    assert all(entry["noncanonical"] is True for entry in science.findings)


def test_a_derivation_reporting_DERIVED_does_not_retire_the_hypothesis(
    env: dict[str, Any],
) -> None:
    """The sharpest version of the invariant.

    A model reporting ``DERIVED`` is the closest this system gets to an
    automated claim of proof. It must still change nothing.
    """

    run(env, ActionKind.DERIVE_MATHEMATICS, deriver=DERIVATION_ANSWER)
    finding = findings_for(env, ActionKind.DERIVE_MATHEMATICS)[0]
    assert "DERIVED" in finding.summary

    hypothesis = ScientificKernelAdapter(env["repo"]).object("HYP-0001")
    assert str(hypothesis.status) == "active"
    assert ScientificKernelAdapter(env["repo"]).frontier().actionable_hypotheses


# --- the four failure modes this project actually exhibited -----------------
def test_work_that_was_done_is_not_described_as_missing(env: dict[str, Any]) -> None:
    """Failure mode 1: the artifact exists and the proposal says it does not.

    The live shape was a literature audit: artifact on disk, finding recorded,
    and the next proposal calling the literature unavailable -- because the
    planner was handed a *count* of findings and nothing else.
    """

    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    _result, router = run(env, ActionKind.ASSESS_FRONTIER)
    prompt = router.requests_for("planner")[0].prompt

    assert "COMPLETED FINDINGS" in prompt
    assert "alternative explanation" in prompt
    # The census is controller-authored and outside every fence.
    assert "completed finding(s)" in prompt


def test_a_settled_mathematical_target_is_reported_as_mathematical(
    env: dict[str, Any],
) -> None:
    """Failure mode 2: an adjudication exists and the frontier says "untested".

    The frontier is right to keep saying untested -- only a person moves it.
    What was missing is that nothing told the planner the target was the kind
    of thing an adjudication, rather than a test, settles.
    """

    _result, router = run(env, ActionKind.INSPECT_REPOSITORY)
    prompt = router.requests_for("planner")[0].prompt
    assert "ADJUDICATION" in prompt
    assert "settled_by" in prompt
    # Canonical and noncanonical remain distinguishable in the same prompt.
    assert "FRONTIER" in prompt


def test_a_second_derivation_of_a_settled_target_is_refused(
    env: dict[str, Any],
) -> None:
    """Failure mode 3: the work exists and the planner keeps redoing it.

    The live shape was six ``design_experiment`` calls for one hypothesis, each
    with a different spec digest. Suppression is by proposition, so a differing
    document cannot bypass it.
    """

    run(env, ActionKind.DERIVE_MATHEMATICS, deriver=DERIVATION_ANSWER)
    result, router = run(env, ActionKind.DERIVE_MATHEMATICS, deriver=DERIVATION_ANSWER)

    refusal = str(result.state.get("plan_refusal") or "")
    assert "already been run" in refusal
    assert len(router.requests_for("deriver")) == 0, "a model was paid to redo it"


def test_a_critique_is_not_reduced_to_a_count(env: dict[str, Any]) -> None:
    """Failure mode 4: the artifact exists and the proposal sees one line.

    The exact defect ``PROP-19700101T000000Z-33ec307e`` reported about itself.
    """

    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    finding = findings_for(env, ActionKind.CRITIQUE_HYPOTHESES)[0]

    # The count is still there, and is no longer all there is.
    assert "alternative explanation(s) for" in finding.summary
    assert len(finding.excerpt) > len(finding.summary)
    assert "inert tolerance ladder" in finding.excerpt


# --- replay and deduplication across the whole path -------------------------
def test_repeating_the_same_work_produces_one_finding(env: dict[str, Any]) -> None:
    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    first = findings_for(env, ActionKind.CRITIQUE_HYPOTHESES)
    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    second = findings_for(env, ActionKind.CRITIQUE_HYPOTHESES)

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].finding_id == second[0].finding_id
    assert first[0].excerpt == second[0].excerpt


def test_an_artifact_is_addressed_by_its_content(env: dict[str, Any]) -> None:
    """Two identical results write one set of bytes, under one address."""

    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    finding = findings_for(env, ActionKind.CRITIQUE_HYPOTHESES)[0]
    document = json.loads(env["artifacts_store"].get_text(finding.artifact_ids[0]))
    assert document["proposals"][0]["statement"].startswith("The observed speed")

    run(env, ActionKind.CRITIQUE_HYPOTHESES, skeptic=CRITIQUE_ANSWER)
    again = findings_for(env, ActionKind.CRITIQUE_HYPOTHESES)[0]
    assert again.artifact_ids == finding.artifact_ids
