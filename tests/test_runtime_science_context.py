"""Completed science must reach the next planner, and must stay noncanonical.

The lifecycle these tests pin, end to end:

.. code-block:: text

    a scientific task completes
      -> an immutable artifact exists
      -> a runtime finding records what was learned
      -> provenance links the finding to the artifact
      -> the NEXT planner sees it, typed and labelled noncanonical
      -> a grounded proposal asks a person
      -> canonical truth changes only when the person acts

Before :mod:`research_os.runtime.sciencecontext`, the fifth step was missing:
the planner was handed ``findings_available: <count>`` and nothing else, so a
finished literature audit was invisible to the cycle after it. The frontier is
derived from capsule files, the capsule cannot move without a person, and the
combination meant the runtime kept planning work it had already done and kept
describing as unavailable material it had already produced.

The distinction that must survive the fix is that a noncanonical result is not
an accepted claim. Several tests below exist only to hold that line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.cycles import start_cycle
from research_os.runtime.db import Database
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.policy import ActionKind
from research_os.runtime.prompts import PLANNER
from research_os.runtime.sciencecontext import (
    MAX_PLANNER_FINDINGS,
    MAX_PLANNER_PROPOSALS,
    MAX_PLANNER_SUMMARY_CHARS,
    noncanonical_science,
)
from research_os.runtime.store import RuntimeStore
from tests.fs_helpers import hypothesis_data, write_yaml
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

FINDING_FENCE_BEGIN = "----- BEGIN RUNTIME FINDINGS (UNTRUSTED AUTONOMOUS OUTPUT) -----"
PROJECT = "alpha-project"


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    artifacts = tmp_path / "artifacts"
    RuntimeStore(runtime_db).upsert_project(project_id=PROJECT, repo_path=str(repo))
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts": artifacts,
        "config": make_config(pg_dsn, artifacts),
        "store": RuntimeStore(runtime_db),
    }


def run_cycle(
    env: dict[str, Any],
    action: ActionKind,
    *,
    objective: str = "find out whether X",
) -> tuple[Any, ScriptedRouter]:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(action)),
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


def planner_prompt(router: ScriptedRouter) -> str:
    calls = router.requests_for("planner")
    assert calls, "the planner was never consulted"
    return calls[0].prompt


def block_json(prompt: str, label: str, *, until: str) -> Any:
    """The one JSON object inside a named block of a rendered prompt."""

    body = prompt.split(f"{label}:", 1)[1].split(until, 1)[0]
    return json.loads(body[body.index("{") : body.rindex("}") + 1])


# --- 1. a completed task reaches a finding, with its provenance -------------
def test_a_completed_action_becomes_a_finding_with_its_provenance(
    env: dict[str, Any],
) -> None:
    """Step one of the lifecycle: work finished, and it was written down."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)

    findings = env["store"].list_findings(project_id=PROJECT, limit=20)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_id
    assert finding.kind is FindingKind.INSPECTION
    assert finding.source_action == str(ActionKind.INSPECT_REPOSITORY)
    assert finding.summary.strip(), "a finding with no summary records nothing"
    # The provenance edge, which is what makes the finding auditable rather
    # than merely present.
    assert finding.capsule_refs == ("Q-0001",)
    assert finding.digest


# --- 2. the next planner sees it --------------------------------------------
def test_the_next_planner_sees_what_the_last_cycle_found(env: dict[str, Any]) -> None:
    """The step that was missing, and the whole reason this module exists."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]

    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)

    assert finding.finding_id in prompt, "the finding's identity never reached it"
    assert finding.summary[:60] in prompt, "the finding's content never reached it"
    assert str(ActionKind.INSPECT_REPOSITORY) in prompt, (
        "the planner cannot tell what action already produced this"
    )


def test_the_planner_is_told_the_census_outside_every_fence(
    env: dict[str, Any],
) -> None:
    """Counts are controller-authored, so no model's output can change them."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)

    census_line = next(
        line for line in prompt.splitlines() if line.startswith("NONCANONICAL CENSUS:")
    )
    assert "1 completed finding(s), 1 shown" in census_line
    assert "no proposal has been put to a human yet" in census_line
    # Before the fenced block, therefore outside it.
    assert prompt.index(census_line) < prompt.index(FINDING_FENCE_BEGIN)


# --- 3. it stays noncanonical -----------------------------------------------
def test_the_finding_reaches_the_planner_as_explicitly_noncanonical(
    env: dict[str, Any],
) -> None:
    """Typed, fenced, and labelled in the entry itself -- three times over."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)

    assert FINDING_FENCE_BEGIN in prompt, "the findings were not fenced as untrusted"
    entry = block_json(prompt, "COMPLETED FINDINGS", until="REPOSITORY:")
    assert entry["noncanonical"] is True
    assert entry["kind"] == str(FindingKind.INSPECTION)

    assert "is NOT canonical" in prompt
    assert "Nobody has accepted any of it" in prompt
    assert "does NOT resolve the frontier" in prompt


def test_a_finding_does_not_resolve_the_frontier_it_discusses(
    env: dict[str, Any],
) -> None:
    """The specific confusion to prevent: observed is not accepted.

    HYP-0001 is listed as untested in the canonical frontier. A cycle runs, a
    finding is recorded, and the hypothesis is *still* listed as untested --
    because only a person can move it.
    """

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)

    frontier = block_json(prompt, "FRONTIER", until="COMPLETED FINDINGS:")
    assert frontier["hypotheses_without_tests"] == ["HYP-0001"]
    assert env["store"].list_findings(project_id=PROJECT, limit=5)


# --- 4. no automatic acceptance ---------------------------------------------
def test_no_cycle_writes_canonical_scientific_state(env: dict[str, Any]) -> None:
    """The authority boundary, checked against the files rather than a promise."""

    capsule = env["repo"] / ".research"
    before = {
        path.relative_to(capsule): path.read_bytes()
        for path in sorted(capsule.rglob("*"))
        if path.is_file()
    }

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    run_cycle(env, ActionKind.ASSESS_FRONTIER)

    after = {
        path.relative_to(capsule): path.read_bytes()
        for path in sorted(capsule.rglob("*"))
        if path.is_file()
    }
    assert after == before, "a cycle changed canonical scientific state"


def test_a_finding_is_never_given_an_accepted_status(env: dict[str, Any]) -> None:
    """There is no kind, field or code path that marks a finding accepted."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    science = noncanonical_science(env["store"], project_id=PROJECT)

    assert science.findings
    for entry in science.findings:
        assert entry["noncanonical"] is True
        assert not any(
            key in entry for key in ("accepted", "status", "verdict", "canonical")
        )
    assert "accepted" not in {str(kind) for kind in FindingKind}
    assert "confirmed" not in {str(kind) for kind in FindingKind}


# --- 5. promotion supersedes the noncanonical view --------------------------
def test_once_a_person_promotes_the_canonical_view_supersedes(
    env: dict[str, Any],
) -> None:
    """A human promotion moves the frontier; the finding does not.

    The promotion here is a capsule file edit, because that is what a promotion
    *is* -- the runtime has no handle that could do it, which is the property
    :func:`test_no_cycle_writes_canonical_scientific_state` checks.
    """

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    _result, before_router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    before = block_json(
        planner_prompt(before_router), "FRONTIER", until="COMPLETED FINDINGS:"
    )
    assert before["hypotheses_without_tests"] == ["HYP-0001"]

    # The human action.
    write_yaml(
        env["repo"] / ".research" / "hypotheses" / "HYP-0001.yaml",
        hypothesis_data(status="retired", addresses=["Q-0001"]),
    )

    _result, after_router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    after_prompt = planner_prompt(after_router)
    after = block_json(after_prompt, "FRONTIER", until="COMPLETED FINDINGS:")
    assert after["hypotheses_without_tests"] == [], (
        "the canonical frontier did not follow the promotion"
    )
    # And the finding is still there, still noncanonical: promotion supersedes
    # the canonical *view*, it does not retroactively accept the observation.
    assert FINDING_FENCE_BEGIN in after_prompt
    assert env["store"].list_findings(project_id=PROJECT, limit=5)


# --- 6. replay does not duplicate -------------------------------------------
def test_the_same_observation_twice_is_one_finding(env: dict[str, Any]) -> None:
    """Content-addressed, so a replayed cycle does not double its evidence.

    The digest is computed from the finding's content and references and not
    from when it was made, which is what makes this true of a *crash replay*
    and not only of a tidy re-run.
    """

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    first = env["store"].list_findings(project_id=PROJECT, limit=20)
    assert len(first) == 1

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    second = env["store"].list_findings(project_id=PROJECT, limit=20)
    assert [item.finding_id for item in second] == [item.finding_id for item in first]

    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert len(science.findings) == 1
    assert science.findings_total == 1


def test_recording_the_same_finding_twice_returns_the_first(
    env: dict[str, Any],
) -> None:
    """The store's own deduplication, stated directly rather than via a cycle."""

    finding = RuntimeFinding(
        project_id=PROJECT,
        kind=FindingKind.LITERATURE,
        summary="the literature was audited; 41 works, 3 directly relevant",
        literature_keys=("smith2020", "jones2021"),
    )
    stored, created = env["store"].record_finding(finding)
    assert created
    again, created_again = env["store"].record_finding(finding)
    assert not created_again
    assert again.finding_id == stored.finding_id


# --- the bounds, which are what keep this from becoming a context dump ------
def test_the_planner_view_is_bounded_and_reports_what_it_hid(
    env: dict[str, Any],
) -> None:
    """Twelve shown, the true total reported, so a big project looks big."""

    for index in range(MAX_PLANNER_FINDINGS + 5):
        env["store"].record_finding(
            RuntimeFinding(
                project_id=PROJECT,
                kind=FindingKind.LITERATURE,
                summary=f"observation number {index}",
            )
        )
    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert len(science.findings) == MAX_PLANNER_FINDINGS
    assert science.findings_total == MAX_PLANNER_FINDINGS + 5
    assert f"{MAX_PLANNER_FINDINGS + 5} completed finding(s)" in science.census()
    assert f"{MAX_PLANNER_FINDINGS} shown" in science.census()


def test_a_long_summary_is_truncated_visibly(env: dict[str, Any]) -> None:
    """Truncated in the entry, not at the fence, so the block shows the cut."""

    env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.INTERPRETATION,
            summary="x" * (MAX_PLANNER_SUMMARY_CHARS + 500),
        )
    )
    science = noncanonical_science(env["store"], project_id=PROJECT)
    summary = str(science.findings[0]["summary"])
    assert summary.endswith("...")
    assert len(summary) == MAX_PLANNER_SUMMARY_CHARS + 3


def test_an_empty_project_says_so_rather_than_rendering_nothing(
    env: dict[str, Any],
) -> None:
    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert science.empty
    assert science.findings_total == 0
    assert "0 completed finding(s)" in science.census()
    assert "no proposal has been put to a human yet" in science.census()


def test_the_planner_template_declares_the_new_context(env: dict[str, Any]) -> None:
    """A version bump, because a prompt is code and its identity is provenance."""

    del env
    assert PLANNER.identity == "planner@4"
    assert "noncanonical_census" in PLANNER.fields
    declared = {name for name, _fence in PLANNER.blocks}
    assert {"completed_findings", "outstanding_proposals"} <= declared


def test_outstanding_proposals_are_bounded_too() -> None:
    assert MAX_PLANNER_PROPOSALS <= MAX_PLANNER_FINDINGS


# --- outstanding proposals -------------------------------------------------
def _put_a_proposal_in_front_of_a_person(
    env: dict[str, Any], *, proposal_id: str, finding_ids: tuple[str, ...]
) -> None:
    """Reserve, settle CREATED, and link -- the runtime's side of a proposal.

    The v1 ``ProposalController`` writes the directory; what the runtime records
    is the reservation and the citation edges, and those are what
    ``sciencecontext`` reads. Driving them directly keeps this test about
    visibility rather than about the proposal worker.
    """

    env["store"].reserve_proposal(
        reservation_key=f"propose_capsule_change:{proposal_id}",
        proposal_id=proposal_id,
        project_id=PROJECT,
    )
    env["store"].settle_proposal_reservation(
        f"propose_capsule_change:{proposal_id}", status="CREATED"
    )
    env["store"].link_proposal_findings(
        proposal_id=proposal_id, finding_ids=finding_ids, cited_ids=finding_ids
    )


def test_an_outstanding_proposal_reaches_the_planner(env: dict[str, Any]) -> None:
    """So the planner can tell "nobody was asked" from "a person is deciding"."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]
    _put_a_proposal_in_front_of_a_person(
        env,
        proposal_id="PROP-20260917T000000Z-abcd1234",
        finding_ids=(finding.finding_id,),
    )

    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert science.proposals_total == 1
    assert science.proposals[0]["proposal_id"] == "PROP-20260917T000000Z-abcd1234"
    assert science.findings[0]["already_cited_by_proposals"] == [
        "PROP-20260917T000000Z-abcd1234"
    ]
    assert "1 proposal(s) already awaiting a human decision" in science.census()

    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)
    assert "PROP-20260917T000000Z-abcd1234" in prompt
    assert "OUTSTANDING PROPOSALS" in prompt
    assert "asks the same question twice" in prompt


def test_a_reserved_or_failed_proposal_is_not_outstanding(env: dict[str, Any]) -> None:
    """Only CREATED counts: nobody is waiting on an attempt that made nothing."""

    env["store"].reserve_proposal(
        reservation_key="propose_capsule_change:r:1",
        proposal_id="PROP-20260917T000001Z-deadbeef",
        project_id=PROJECT,
    )
    assert noncanonical_science(env["store"], project_id=PROJECT).proposals_total == 0

    env["store"].settle_proposal_reservation(
        "propose_capsule_change:r:1", status="FAILED"
    )
    assert noncanonical_science(env["store"], project_id=PROJECT).proposals_total == 0

    env["store"].settle_proposal_reservation(
        "propose_capsule_change:r:1", status="CREATED"
    )
    assert noncanonical_science(env["store"], project_id=PROJECT).proposals_total == 1


# --- the document inventory: how completed work enters the graph ------------
def test_a_completed_document_reaches_the_next_planner(env: dict[str, Any]) -> None:
    """The thesis-pilot escape, reproduced and then closed.

    That project held a finished literature audit, a scientific report and a
    verdict document under ``docs/``. ``inspect_repository`` reported ``HEAD
    <sha> on <branch>``, its finding said exactly that, and the next planner
    went on proposing the literature work that had already been done. The
    document inventory is what makes finished work visible without putting the
    documents themselves in the prompt.
    """

    docs = env["repo"] / "docs" / "2026"
    docs.mkdir(parents=True)
    (docs / "LITERATURE.md").write_text(
        "# Literature audit\n\n41 works screened, 3 directly relevant.\n",
        encoding="utf-8",
    )
    (docs / "VERDICT.md").write_text("# Verdict\n\nThe claim does not hold.\n", "utf-8")

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]

    assert "docs/2026/LITERATURE.md" in finding.summary
    assert "not referenced by any capsule object" in finding.summary
    # Digest-pinned, so what the finding cites cannot change after citation.
    assert len(finding.artifact_ids) == 2
    for artifact_id in finding.artifact_ids:
        assert env["store"].get_finding(finding.finding_id)
        assert len(artifact_id) == 64

    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    prompt = planner_prompt(router)
    assert "docs/2026/LITERATURE.md" in prompt, (
        "the finished literature audit is still invisible to the next planner"
    )


def test_an_unreferenced_document_is_not_thereby_canonical(
    env: dict[str, Any],
) -> None:
    """A document in the repository is not evidence the project has accepted.

    The inventory says "this exists and the capsule does not reference it",
    which is a statement about the repository. It puts nothing in the capsule
    and moves nothing on the frontier.
    """

    docs = env["repo"] / "docs"
    docs.mkdir()
    (docs / "REPORT.md").write_text("# Report\n\nHYP-0001 is settled.\n", "utf-8")

    capsule = env["repo"] / ".research"
    before = {
        path.relative_to(capsule): path.read_bytes()
        for path in sorted(capsule.rglob("*"))
        if path.is_file()
    }
    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    after = {
        path.relative_to(capsule): path.read_bytes()
        for path in sorted(capsule.rglob("*"))
        if path.is_file()
    }
    assert after == before

    _result, router = run_cycle(env, ActionKind.ASSESS_FRONTIER)
    frontier = block_json(
        planner_prompt(router), "FRONTIER", until="COMPLETED FINDINGS:"
    )
    assert frontier["hypotheses_without_tests"] == ["HYP-0001"], (
        "a document claiming a hypothesis is settled must not settle it"
    )


def test_source_code_and_data_are_not_scientific_documents(
    env: dict[str, Any],
) -> None:
    """Bounded by convention, not by a search of the whole repository."""

    from research_os.runtime.actions.inspect import DOCUMENT_ROOTS

    (env["repo"] / "src").mkdir()
    (env["repo"] / "src" / "solver.py").write_text("x = 1\n", encoding="utf-8")
    (env["repo"] / "README.md").write_text("# project\n", encoding="utf-8")
    docs = env["repo"] / DOCUMENT_ROOTS[0]
    docs.mkdir()
    (docs / "NOTE.md").write_text("# note\n", encoding="utf-8")
    (docs / "script.py").write_text("y = 2\n", encoding="utf-8")

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]
    assert "1 project document(s)" in finding.summary
    assert "solver.py" not in finding.summary
    assert "script.py" not in finding.summary


def test_a_project_with_no_documents_says_nothing_extra(env: dict[str, Any]) -> None:
    """The common case stays quiet rather than reporting an empty inventory."""

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]
    assert "project document(s)" not in finding.summary
    assert finding.summary.startswith("HEAD ")


def test_the_document_inventory_is_bounded(env: dict[str, Any]) -> None:
    """A project with a thousand notes does not become the whole prompt."""

    from research_os.runtime.actions.inspect import MAX_DOCUMENTS

    docs = env["repo"] / "docs"
    docs.mkdir()
    for index in range(MAX_DOCUMENTS + 7):
        (docs / f"NOTE-{index:03d}.md").write_text(f"# {index}\n", encoding="utf-8")

    run_cycle(env, ActionKind.INSPECT_REPOSITORY)
    finding = env["store"].list_findings(project_id=PROJECT, limit=1)[0]
    # The true total is reported; the listing is not.
    assert f"{MAX_DOCUMENTS + 7} project document(s)" in finding.summary
    assert len(finding.artifact_ids) <= MAX_DOCUMENTS
