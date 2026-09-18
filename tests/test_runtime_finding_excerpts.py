"""Evidence the system produced must not be hidden from the system.

The defect, in the words of the system that hit it. A ``critique_hypotheses``
cycle on the thesis project wrote an eleven-kilobyte artifact holding six
alternative explanations, recorded a finding for it, and linked the provenance.
All of that was correct. But the finding's only text was the handler's own
sentence --

    6 alternative explanation(s) for 5 target(s)

-- and that sentence was all the proposal layer could read. The proposal
grounded in it says so itself, in ``PR-002``:

    Only that summary is available to this proposal; the text of the six
    alternatives is not.

and lists ``full text of FIND-...-9b053d6e`` under ``required_inputs``. The
worker refused to reason from content it could not see. That refusal was right
and the architecture around it was wrong.

The fix is a bounded, *producer-authored* excerpt: the handler that built the
structure chooses what to quote, because it is the only layer that knows which
part of its own result is the finding. The generic prompt layer still reads no
artifacts.

What these tests hold:

.. code-block:: text

    substance    the excerpt carries what the summary could not
    bounded      a full packet of them stays a fraction of a prompt
    noncanonical it is quoted, fenced, and labelled, and never a claim
    stable       a replay reuses the excerpt rather than reauthoring it
    optional     a producer that authors none degrades to the old behaviour
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.actions.base import EXCERPT_KEY, bounded_excerpt
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.cycles import start_cycle
from research_os.runtime.db import Database
from research_os.runtime.findings import MAX_EXCERPT_CHARS, FindingKind, RuntimeFinding
from research_os.runtime.policy import ActionKind
from research_os.runtime.sciencecontext import (
    MAX_PLANNER_EXCERPT_CHARS,
    noncanonical_science,
)
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

PROJECT = "excerpt-project"

#: A real-shaped skeptic answer, with the structure and the length the live
#: thesis critique actually had. Not a trivial fixture: the whole failure was
#: that a *substantive* result arrived as a count, and a two-word alternative
#: would not have exposed it.
REAL_CRITIQUE: dict[str, Any] = {
    "proposals": [
        {
            "statement": (
                "HYP-0002's dual-feasibility characterization is mathematically "
                "unremarkable and not attributable to the 2023 paper as a "
                "discovery: it is the standard LASSO KKT / dual-feasibility "
                "condition, well established in pre-2023 literature such as "
                "safe-screening-rule papers."
            ),
            "mechanism": (
                "The pricing subproblem's conic reformulation and the classical "
                "LASSO dual are the same convex program in different notation."
            ),
            "predictions": ["A pre-2023 source stating the identical condition."],
            "falsifiers": ["The 2023 paper claims this identity as novel."],
            "required_data": ["LITERATURE.md", "the 2023 claims section"],
            "proposed_test": "Search the archived paper text for 'dual feasib'.",
            "confounds": ["Papers cite known conditions as motivation."],
            "novelty": "known",
            "confidence": "medium",
        },
        {
            "statement": (
                "Algorithmic equivalence in one special case does not preclude a "
                "genuine advance in a more general conic setting where that "
                "equivalence breaks down."
            ),
            "mechanism": "Narrow-case equivalence and general-case novelty are "
            "logically independent claims.",
            "predictions": ["The paper's claims were narrowed to plain LASSO."],
            "falsifiers": ["TIMELINE.md shows no generalization claim."],
            "required_data": ["TIMELINE.md"],
            "proposed_test": "Read the claims section.",
            "confounds": ["EXP-0001 covers only LASSO."],
            "novelty": "unknown",
            "confidence": "medium",
        },
        {
            "statement": (
                "The certificate failure on 26 of 30 cells is a harness artefact "
                "rather than a property of the method, because the tolerance "
                "ladder is inert."
            ),
            "mechanism": "An inert ladder measures the harness, not the solver.",
            "predictions": ["Repairing the ladder converts the 26 cells."],
            "falsifiers": ["A repaired ladder leaves the cells uncertified."],
            "required_data": ["EVI-0002 notes"],
            "proposed_test": "Re-run four cells with a working ladder.",
            "confounds": ["The four fundamentally failing cells differ."],
            "novelty": "unknown",
            "confidence": "low",
        },
    ]
}


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    artifacts = tmp_path / "artifacts"
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "config": make_config(pg_dsn, artifacts),
        "store": store,
        "artifacts_store": FilesystemArtifactStore(artifacts, store=store),
    }


def run_critique(env: dict[str, Any], *, objective: str = "attack it") -> Any:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.CRITIQUE_HYPOTHESES)),
            "skeptic": REAL_CRITIQUE,
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=env["repo"],
        objective=objective,
        models=router,
    )
    return result, router


def only_finding(env: dict[str, Any]) -> RuntimeFinding:
    findings = env["store"].list_findings(project_id=PROJECT, limit=10)
    critiques = [f for f in findings if f.kind is FindingKind.REVIEW]
    assert critiques, f"no critique finding was recorded; got {findings}"
    return env["store"].get_finding(critiques[0].finding_id)


# --- 1. the handler writes a substantive artifact ---------------------------
def test_the_critique_handler_writes_the_full_alternatives_to_an_artifact(
    env: dict[str, Any],
) -> None:
    """The artifact was never the problem. It is asserted so the test that
    follows is measuring the gap between the artifact and the finding."""

    run_critique(env)
    finding = only_finding(env)
    assert finding.artifact_ids, "the critique recorded no artifact"
    document = json.loads(env["artifacts_store"].get_text(finding.artifact_ids[0]))
    assert len(document["proposals"]) == 3
    assert "safe-screening" in json.dumps(document)


# --- 2. the finding carries summary AND excerpt AND provenance --------------
def test_the_finding_carries_substance_beside_its_count(env: dict[str, Any]) -> None:
    run_critique(env)
    finding = only_finding(env)

    # The old summary is unchanged -- this adds, it does not replace.
    assert "alternative explanation(s) for" in finding.summary
    # And the substance the summary could not carry is now there.
    assert finding.excerpt
    assert "dual-feasibility" in finding.excerpt
    assert "does not preclude a genuine advance" in finding.excerpt
    assert "tolerance ladder is inert" in finding.excerpt
    # Provenance is intact and unchanged.
    assert finding.artifact_ids
    assert finding.capsule_refs


def test_the_excerpt_keeps_the_novelty_verdict_of_each_alternative(
    env: dict[str, Any],
) -> None:
    """The field the whole thesis novelty gate turns on."""

    run_critique(env)
    assert "[novelty: known]" in only_finding(env).excerpt


# --- 3 & 4. it reaches the proposal worker, which can ground on it ----------
def test_the_proposal_worker_is_given_the_substance_not_the_count(
    env: dict[str, Any],
) -> None:
    """The exact failure PR-002 reported, inverted into an assertion."""

    from research_os.proposal.planner import render_supplied_findings
    from research_os.runtime.actions.proposals import _supplied

    run_critique(env)
    rendered = render_supplied_findings(_supplied((only_finding(env),)))

    assert "dual-feasibility" in rendered
    assert "tolerance ladder is inert" in rendered
    # And it is offered as quotable, citable evidence rather than as narrative.
    assert only_finding(env).finding_id in rendered


def test_the_rendered_excerpt_is_labelled_noncanonical_where_it_is_shown(
    env: dict[str, Any],
) -> None:
    from research_os.proposal.planner import render_supplied_findings
    from research_os.runtime.actions.proposals import _supplied

    run_critique(env)
    rendered = render_supplied_findings(_supplied((only_finding(env),)))
    assert "NONCANONICAL" in rendered
    assert "not a claim" in rendered
    # The fence that marks the whole block untrusted is still there.
    assert "UNTRUSTED AUTONOMOUS OUTPUT" in rendered


def test_the_excerpt_is_in_the_grounding_digest(env: dict[str, Any]) -> None:
    """A finding whose excerpt changed is not the finding that was cited.

    Without this, a proposal promoted later could rest on a reading that has
    since been superseded, and the staleness check would not notice.
    """

    from research_os.proposal.models import SuppliedFinding
    from research_os.proposal.planner import supplied_findings_digest

    base = SuppliedFinding(
        finding_id="FIND-1", kind="review", statement="s", excerpt="alpha"
    )
    changed = base.model_copy(update={"excerpt": "beta"})
    assert supplied_findings_digest([base]) != supplied_findings_digest([changed])


# --- 5. the prompt stays bounded --------------------------------------------
def test_a_full_packet_of_excerpts_stays_a_bounded_fraction_of_a_prompt() -> None:
    """Twelve findings, each at the storage bound, rendered together."""

    from research_os.proposal.models import SuppliedFinding
    from research_os.proposal.planner import (
        MAX_EXCERPT_IN_PROMPT,
        MAX_FINDING_CHARS,
        render_supplied_findings,
    )
    from research_os.runtime.findings import MAX_PACKET_FINDINGS

    packet = [
        SuppliedFinding(
            finding_id=f"FIND-{index}",
            kind="review",
            statement="S" * MAX_FINDING_CHARS * 2,
            excerpt="E" * MAX_EXCERPT_CHARS * 2,
        )
        for index in range(MAX_PACKET_FINDINGS)
    ]
    rendered = render_supplied_findings(packet)
    ceiling = MAX_PACKET_FINDINGS * (MAX_FINDING_CHARS + MAX_EXCERPT_IN_PROMPT) * 2
    assert len(rendered) < ceiling
    # And no single excerpt got through at more than its clip.
    assert "E" * (MAX_EXCERPT_IN_PROMPT + 1) not in rendered


def test_a_rendered_packet_clips_below_the_stored_bound() -> None:
    """The stored form is the record; a prompt is a working set.

    ``rendered()`` feeds the nominator's block, and twelve stored excerpts
    would be twenty-four thousand characters of it.
    """

    from research_os.runtime.findings import (
        MAX_RENDERED_EXCERPT_CHARS,
        FindingPacket,
    )

    packet = FindingPacket(
        findings=tuple(
            RuntimeFinding(
                finding_id=f"FIND-{index}",
                project_id=PROJECT,
                kind=FindingKind.REVIEW,
                summary="s",
                excerpt="E" * MAX_EXCERPT_CHARS,
            )
            for index in range(3)
        )
    )
    for entry in packet.rendered():
        excerpt = str(entry["excerpt"])
        assert len(excerpt) <= MAX_RENDERED_EXCERPT_CHARS + len("...")
        assert entry["excerpt_is_noncanonical"] is True
    assert MAX_RENDERED_EXCERPT_CHARS < MAX_EXCERPT_CHARS


def test_the_stored_excerpt_is_clipped_to_the_stored_bound() -> None:
    finding = RuntimeFinding(
        project_id=PROJECT,
        kind=FindingKind.REVIEW,
        summary="s",
        excerpt="x" * (MAX_EXCERPT_CHARS * 3),
    )
    assert len(finding.excerpt) == MAX_EXCERPT_CHARS


def test_the_planner_sees_a_shorter_excerpt_than_the_proposal_worker(
    env: dict[str, Any],
) -> None:
    """The planner picks a verb. It does not weigh evidence."""

    run_critique(env)
    science = noncanonical_science(env["store"], project_id=PROJECT)
    shown = [entry for entry in science.findings if entry.get("excerpt")]
    assert shown, "no finding reached the planner with an excerpt"
    for entry in shown:
        excerpt = str(entry["excerpt"])
        assert len(excerpt) <= MAX_PLANNER_EXCERPT_CHARS + len("...")
        assert entry["noncanonical"] is True


# --- 6. it never becomes canonical ------------------------------------------
def test_an_excerpt_does_not_change_the_capsule(env: dict[str, Any]) -> None:
    before = sorted(
        path.name for path in (env["repo"] / ".research").rglob("*") if path.is_file()
    )
    before_bytes = {
        path: path.read_bytes()
        for path in (env["repo"] / ".research").rglob("*")
        if path.is_file()
    }
    run_critique(env)
    after = sorted(
        path.name for path in (env["repo"] / ".research").rglob("*") if path.is_file()
    )
    assert before == after
    for path, content in before_bytes.items():
        assert path.read_bytes() == content


def test_an_excerpt_does_not_resolve_the_frontier_it_discusses(
    env: dict[str, Any],
) -> None:
    """A hypothesis the excerpt argues against is still an open hypothesis."""

    from research_os.runtime.kernel import ScientificKernelAdapter

    run_critique(env)
    frontier = ScientificKernelAdapter(env["repo"]).frontier()
    assert frontier.actionable_hypotheses, (
        "the critique retired a hypothesis, which no runtime may do"
    )


# --- 7. replay is stable -----------------------------------------------------
def test_the_same_critique_twice_is_one_finding_with_one_excerpt(
    env: dict[str, Any],
) -> None:
    """Content-addressed dedup still holds with the excerpt in the digest."""

    run_critique(env)
    first = only_finding(env)
    stored, created = env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=first.kind,
            summary=first.summary,
            excerpt=first.excerpt,
            source_action=first.source_action,
            artifact_ids=first.artifact_ids,
            capsule_refs=first.capsule_refs,
        )
    )
    assert created is False
    assert stored.finding_id == first.finding_id
    assert stored.excerpt == first.excerpt


def test_an_excerpt_appearing_later_does_not_restate_an_existing_finding() -> None:
    """The backward-compatibility property migration 0016 relies on.

    A finding recorded before excerpts existed keeps the digest it was cited
    under -- so a proposal already resting on it still resolves to it.
    """

    without = RuntimeFinding(project_id=PROJECT, kind=FindingKind.REVIEW, summary="s")
    with_empty = RuntimeFinding(
        project_id=PROJECT, kind=FindingKind.REVIEW, summary="s", excerpt=""
    )
    with_text = with_empty.model_copy(update={"excerpt": "something substantive"})
    assert without.digest == with_empty.digest
    assert without.digest != with_text.digest


def test_bounded_excerpt_is_deterministic() -> None:
    entries = ["alpha", "beta", "gamma"]
    assert (
        bounded_excerpt(entries, limit=200)
        == bounded_excerpt(entries, limit=200)
        == bounded_excerpt(list(entries), limit=200)
    )


def test_a_clipped_excerpt_says_what_it_dropped() -> None:
    """Silent truncation reads as a handler that found less than it did."""

    out = bounded_excerpt(
        [f"entry number {n} with padding" * 3 for n in range(20)], limit=300
    )
    assert len(out) <= 300
    assert "omitted to stay within the excerpt bound" in out


# --- 8. a producer that authors none still works ----------------------------
def test_a_finding_with_no_excerpt_falls_back_to_summary_and_artifact(
    env: dict[str, Any],
) -> None:
    """The behaviour that existed before this field, preserved exactly."""

    from research_os.proposal.planner import render_supplied_findings
    from research_os.runtime.actions.proposals import _supplied

    recorded, _created = env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.INSPECTION,
            summary="HEAD 9d5474e89f8a on research/2026-reassessment",
            artifact_ids=("a" * 64,),
            capsule_refs=("HYP-0001",),
        )
    )
    # Through `get_finding`, because that is the documented way to get a
    # finding with its provenance edges loaded; `record_finding` returns the
    # row alone and reports empty edges rather than pretending to none.
    stored = env["store"].get_finding(recorded.finding_id)
    assert stored.excerpt == ""
    rendered = render_supplied_findings(_supplied((stored,)))
    assert "HEAD 9d5474e89f8a" in rendered
    assert "HYP-0001" in rendered
    # No empty "excerpt:" heading dangling in the block.
    assert "excerpt (NONCANONICAL" not in rendered


def test_a_handler_that_reports_no_excerpt_key_records_a_finding_anyway(
    env: dict[str, Any],
) -> None:
    """A cycle whose action authored no excerpt still produces a citable finding."""

    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.INSPECT_REPOSITORY)),
            "scientific_reviewer": review_answer(),
        }
    )
    start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=env["repo"],
        objective="look at it",
        models=router,
    )
    findings = env["store"].list_findings(project_id=PROJECT, limit=10)
    assert findings, "no finding was recorded"
    assert all(isinstance(item.excerpt, str) for item in findings)


def test_the_excerpt_key_is_not_mistaken_for_a_reference() -> None:
    """The lifted key must not leak into provenance as an identifier."""

    assert EXCERPT_KEY not in {"ranked", "job_id", "spec_digest"}


# --- 9. the handler an acceptance run found missing -------------------------
def run_frontier(env: dict[str, Any], *, objective: str = "rank what is next") -> Any:
    """Drive a cycle whose action is the frontier ranking."""

    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "frontier": (
                {
                    "recommendation": "WAIT_HUMAN",
                    "recommendation_rationale": (
                        "Two proposals already put these questions to the "
                        "researcher; nothing read-only would move the frontier."
                    ),
                    "ranked_actions": [
                        {
                            "action": "propose_capsule_change",
                            "addresses": ["HYP-0002", "Q-0001"],
                            "importance": "high",
                            "information_gain": "medium",
                            "feasibility": "high",
                            "cost": "low",
                            "rationale": (
                                "HYP-0002 is a mathematical proposition and needs "
                                "a derivation, not another experiment."
                            ),
                        },
                        {
                            "action": "search_literature",
                            "addresses": ["Q-0003"],
                            "importance": "medium",
                            "information_gain": "low",
                            "feasibility": "high",
                            "cost": "low",
                            "rationale": "the working-set priority question is a "
                            "literature determination.",
                        },
                    ],
                }
            ),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=env["repo"],
        objective=objective,
        models=router,
    )
    return result, router


def only_frontier_finding(env: dict[str, Any]) -> RuntimeFinding:
    findings = env["store"].list_findings(project_id=PROJECT, limit=10)
    ranked = [f for f in findings if f.kind is FindingKind.FRONTIER]
    assert ranked, f"no frontier finding was recorded; got {findings}"
    assert len(ranked) == 1
    return ranked[0]


def test_the_frontier_ranking_carries_its_candidates_not_only_their_count(
    env: dict[str, Any],
) -> None:
    """The gap a real acceptance run exposed.

    Exercising the repaired pipeline on the thesis project produced exactly one
    finding -- ``7 ranked candidate(s); recommends WAIT_HUMAN`` -- with an empty
    excerpt. Not which seven, not why, and not why waiting was the answer. The
    handler that decides what happens next was the one left out when excerpts
    were added, which is the same blindness the excerpt work set out to remove,
    in the finding whose content bears most directly on the next decision.
    """

    run_frontier(env)
    finding = only_frontier_finding(env)

    assert "ranked candidate(s); recommends WAIT_HUMAN" in finding.summary
    assert finding.excerpt, "the frontier ranking recorded no substance"
    # The recommendation and its reason.
    assert "recommendation: WAIT_HUMAN" in finding.excerpt
    assert "already put these questions" in finding.excerpt
    # And each candidate, with what it addresses and why it ranked there.
    assert "propose_capsule_change" in finding.excerpt
    assert "HYP-0002" in finding.excerpt
    assert "needs a derivation, not another experiment" in finding.excerpt
    assert "search_literature" in finding.excerpt
    assert "importance high" in finding.excerpt


def test_the_frontier_excerpt_reaches_a_later_proposal(env: dict[str, Any]) -> None:
    """An excerpt nothing reads is a longer summary."""

    run_frontier(env)
    finding = only_frontier_finding(env)

    from research_os.runtime.actions.proposals import _supplied

    supplied = _supplied((finding,))
    assert supplied, "the finding did not convert for the proposal layer"
    assert "WAIT_HUMAN" in supplied[0].excerpt
    assert "HYP-0002" in supplied[0].excerpt
