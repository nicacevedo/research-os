"""Regressions for what three independent reviews found in the autonomous loop.

Each test here reproduces one confirmed finding and fails on the code as it
was before the fix. Grouped by what they hold:

- **the database keeps its records**: a contract, the reading of an
  experiment, a frontier request and an idea's provenance cannot be edited,
  re-pointed or deleted out from under what cites them -- and only deleting
  the project removes them;
- **a reading is one fact**: the evidence row and ``INTERPRETED`` land
  together, a contract with a reading is never retired, and a repair
  replaces a failed execution in one transaction;
- **the state machine decides on the present**: continuation compares and
  sets on the state it read, a temporary lineage ceiling parks an idea only
  until the lineage has room, a follow-up on a full lineage waits rather
  than being declined, and a validated idea holds no slot;
- **generators cite what they were shown**: an explorer shown sources names
  one per direction, seeds are shown with their ids, and a failure-mined
  child meets the same bounds a follow-up does;
- **a replication must be independent and a reading must be legible**: one
  that varies only its resources is refused, one whose outputs are
  byte-identical to the primary's is INSUFFICIENT, and the evidence a
  reviewer reads carries the frozen analysis, the record counts and every
  earlier reading of the question that existed when the contract froze.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import empirical, frontier, runner, stages
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    AdjudicationType,
    ContractState,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentRole,
    ExperimentState,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    RequestState,
    Severity,
)
from research_os.portfolio.stages import STAGE_ORDER as ORDER
from research_os.portfolio.store import DuplicateExperimentError, PortfolioStore
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.interfaces import ExecutionSpec
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import contract_prompt_answers, idea_fields, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import (
    pg_dsn,
    runtime_db,
    runtime_project,
    runtime_xdg,
    throwaway_dsn,
)
from tests.test_portfolio_empirical import (
    CRASHING_SCRIPT,
    _advance,
    _context,
    _idea,
    _router,
    design_answer,
    project_repo,
)
from tests.test_portfolio_scientific_contract import _frozen, grid_repo
from tests.test_portfolio_stages import (
    CONFIG,
    _complete_evidence,
    _complete_reviews,
    objection,
    snapshot,
)

__all__ = [
    "grid_repo",
    "pg_dsn",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "throwaway_dsn",
]


@pytest.fixture
def portfolio(runtime_db: Database) -> PortfolioStore:
    return PortfolioStore(runtime_db)


def _refused(runtime_db: Database, sql: str, params: tuple[Any, ...]) -> None:
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(sql, params)


# ================================================ the database keeps records
def test_a_frozen_contract_keeps_its_documents_and_its_state(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Every edit here succeeded before the trigger was tightened.

    The artifacts are the documents the digests are *of*; re-pointing one is
    how a contract is silently rewritten. FROZEN may become SUPERSEDED and
    nothing else, and a superseded contract is finished: it cannot be given
    a design, a digest or a state afterwards.
    """

    _, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    for column in (
        "analysis_artifact_id",
        "design_artifact_id",
        "contract_artifact_id",
    ):
        _refused(
            runtime_db,
            f"update scientific_contracts set {column} = null where contract_id = %s",
            (contract.contract_id,),
        )
    _refused(
        runtime_db,
        "update scientific_contracts set state = 'BLOCKED_CAPABILITY' "
        "where contract_id = %s",
        (contract.contract_id,),
    )

    # A contract that was never designed, superseded, stays that way.
    idea, version = seed_idea(portfolio, runtime_project)
    bare = portfolio.create_contract(
        project_id=runtime_project,
        idea_id=idea.idea_id,
        idea_version=version.version,
        role=ExperimentRole.PRIMARY,
        hypothesis_digest=version.content_digest,
        analysable=True,
        analysis_digest="panalysis-v1:" + "1" * 64,
        analysis_artifact_id=contract.analysis_artifact_id,
    )
    portfolio.supersede_contract(bare.contract_id, detail="retired")
    for assignment in (
        "state = 'FROZEN'",
        "design_digest = 'pdesign-v1:" + "2" * 64 + "'",
        "contract_digest = 'pcontract-v1:" + "3" * 64 + "'",
        "frozen_at = now()",
    ):
        _refused(
            runtime_db,
            f"update scientific_contracts set {assignment} where contract_id = %s",
            (bare.contract_id,),
        )
    assert portfolio.require_contract(bare.contract_id).state is (
        ContractState.SUPERSEDED
    )


def test_a_reading_is_written_once_and_its_contract_is_never_retired(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """What was read under a contract stays what was read, under that contract.

    A second preregistration of the same question on the same version was
    possible because a read contract could be superseded, which freed the
    partial unique index for a new one.
    """

    _, contract = _frozen(portfolio, runtime_db, tmp_path, runtime_project, grid_repo)
    (read,) = [
        item
        for item in portfolio.list_experiments(idea_id=contract.idea_id)
        if item.contract_id == contract.contract_id
    ]
    assert read.state is ExperimentState.INTERPRETED and read.evidence_id
    for assignment, value in (
        ("state = %s", "SUPERSEDED"),
        (
            "conclusion = %s",
            "CONTRADICTS"
            if read.conclusion is EmpiricalConclusion.SUPPORTS
            else "SUPPORTS",
        ),
        ("evidence_id = %s", None),
        ("analysis_artifact_id = %s", None),
        ("job_id = %s", "JOB-forged"),
        ("contract_id = %s", None),
    ):
        _refused(
            runtime_db,
            f"update idea_experiments set {assignment} where experiment_id = %s",
            (value, read.experiment_id),
        )
    _refused(
        runtime_db,
        "update scientific_contracts set state = 'SUPERSEDED' where contract_id = %s",
        (contract.contract_id,),
    )
    # A revision retires the version's open work and leaves this alone.
    portfolio.append_version(
        idea_id=contract.idea_id,
        fields=idea_fields(title="a revised statement of the question"),
        origin_role="scientific_discovery",
    )
    assert portfolio.require_contract(contract.contract_id).state is (
        ContractState.FROZEN
    )


def test_only_deleting_the_project_removes_contracts_and_provenance(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The delete exemption was "any cascade", so deleting one idea erased both.

    Deleting an idea's version took its contracts with it, and deleting the
    idea took its provenance. Now both are refused while the project
    exists, and deleting the project -- an operator's act on operational
    state -- still removes everything.
    """

    _, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    _refused(
        runtime_db,
        "delete from idea_versions where idea_id = %s",
        (contract.idea_id,),
    )
    _refused(runtime_db, "delete from ideas where idea_id = %s", (contract.idea_id,))
    other, _ = seed_idea(portfolio, runtime_project, title="has only provenance")
    _refused(runtime_db, "delete from ideas where idea_id = %s", (other.idea_id,))
    assert portfolio.require_contract(contract.contract_id)
    assert portfolio.provenance_of(other.idea_id)

    with runtime_db.tx() as conn:
        conn.execute("delete from projects where project_id = %s", (runtime_project,))
        left = conn.execute(
            "select (select count(*) from scientific_contracts) as contracts, "
            "(select count(*) from idea_provenance) as provenance"
        ).fetchone()
    assert (left["contracts"], left["provenance"]) == (0, 0)


def test_a_frontier_request_is_a_record_of_an_event(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Children cite a request; it cannot be rewritten or reopened under them."""

    idea, version = seed_idea(portfolio, runtime_project)
    request = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.ANOMALY,
        source_ref="IEVD-20260101T000000Z-00000001",
        question="Why did the second grid disagree?",
        source_idea_id=idea.idea_id,
        source_version=version.version,
    )
    for column, value in (
        ("question", "a different question"),
        ("basis", "RESULT"),
        ("source_ref", "somewhere else"),
        ("source_idea_id", None),
    ):
        _refused(
            runtime_db,
            f"update frontier_requests set {column} = %s where request_id = %s",
            (value, request.request_id),
        )
    portfolio.close_request(
        request.request_id, state=RequestState.CONSUMED, resolution="answered"
    )
    for assignment in ("state = 'OPEN'", "resolution = 'rewritten'"):
        _refused(
            runtime_db,
            f"update frontier_requests set {assignment} where request_id = %s",
            (request.request_id,),
        )
    _refused(
        runtime_db,
        "delete from frontier_requests where request_id = %s",
        (request.request_id,),
    )
    again = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.ANOMALY,
        source_ref="IEVD-20260101T000000Z-00000001",
        question="Why did the second grid disagree?",
        source_idea_id=idea.idea_id,
        source_version=version.version,
    )
    assert again.request_id == request.request_id, "one event, one request, ever"


def test_the_upgrade_backfill_names_merges_and_convergences(
    throwaway_dsn: str,
) -> None:
    """Existing ideas got provenance from their origin, and two were wrong.

    A MERGE was recorded as a BRANCH, and every DUPLICATE_OF edge -- a
    direction proposed twice -- was dropped rather than becoming the
    CONVERGENCE row the live code writes.
    """

    from research_os.runtime.migrations import discover, migrate

    files = discover()
    with Database(throwaway_dsn) as db:
        with db.tx() as conn:
            for migration in (m for m in files if m.version <= "0031"):
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )
            conn.execute(
                "insert into projects (project_id, repo_path) values ('old', '/tmp/o')"
            )
            ideas = {
                "merged": ("PIDEA-20260101T000000Z-00000001", "MERGE"),
                "survivor": ("PIDEA-20260101T000000Z-00000002", "BLIND_EXPLORER"),
                "duplicate": ("PIDEA-20260101T000000Z-00000003", "BLIND_EXPLORER"),
            }
            for idea_id, origin in ideas.values():
                conn.execute(
                    "insert into ideas (idea_id, project_id, depth, lineage_root, "
                    "origin, current_version, status, operational_state, "
                    "quality_tier) values (%s, 'old', 0, %s, %s, 1, 'CANDIDATE', "
                    "'IDLE', 'NONE')",
                    (idea_id, idea_id, origin),
                )
            conn.execute(
                "insert into idea_edges (parent_idea_id, child_idea_id, kind, "
                "parent_depth, child_depth, detail) values "
                "(%s, %s, 'DUPLICATE_OF', 0, 0, 'same direction')",
                (ideas["survivor"][0], ideas["duplicate"][0]),
            )
        migrate(db)
        store = PortfolioStore(db)
        merged = store.provenance_of(ideas["merged"][0])
        survivor = store.provenance_of(ideas["survivor"][0])
    assert [item.basis for item in merged] == [ProvenanceBasis.MERGE]
    assert {item.basis for item in survivor} == {
        ProvenanceBasis.BLIND_EXPLORATION,
        ProvenanceBasis.CONVERGENCE,
    }
    (convergence,) = [
        item for item in survivor if item.basis is ProvenanceBasis.CONVERGENCE
    ]
    assert convergence.source_ref == ideas["duplicate"][0]


# ==================================================== a reading is one fact
def test_a_contract_is_not_stale_once_any_part_of_a_reading_exists(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash window: evidence written, the state not yet INTERPRETED.

    `_contract_is_stale` read "read" as the state alone, so a prompt bump in
    that window retired the contract and froze a second preregistration of
    the same question. `record_reading` closes the window; this holds the
    guard for rows written before it did.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    (designed,) = [
        item
        for item in portfolio.list_experiments(idea_id=contract.idea_id)
        if item.contract_id == contract.contract_id
    ]
    submitted = empirical.submit(context, designed)
    assert submitted.ok, submitted.detail
    execution = portfolio.require_experiment(designed.experiment_id)
    assert execution.state is not ExperimentState.INTERPRETED and execution.job_id
    evidence = portfolio.add_evidence(
        idea_id=execution.idea_id,
        idea_version=execution.idea_version,
        kind=empirical._evidence_kind(context, execution),
        strength=empirical.EVIDENCE_STRENGTH_FOR_CONCLUSION[
            EmpiricalConclusion.SUPPORTS
        ],
        summary="written before the crash",
        job_id=execution.job_id,
        artifact_id=contract.analysis_artifact_id,
    )
    portfolio.update_experiment(
        execution.experiment_id,
        state=execution.state,
        evidence_id=evidence.evidence_id,
    )

    real = empirical._analysis_designer()

    class Retired:
        name = real.name
        identity = f"{real.name}@999"

    monkeypatch.setattr(empirical, "_analysis_designer", lambda: Retired())
    unread = portfolio.require_contract(contract.contract_id)
    assert unread.analysis_prompt.startswith(f"{real.name}@"), (
        "the retirement below must be one the guard would otherwise apply"
    )
    assert not empirical._contract_is_stale(
        context, portfolio.require_contract(contract.contract_id)
    )


def test_a_repair_that_cannot_record_its_successor_leaves_the_failure_repairable(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring the failed run and recording its successor are one write.

    Apart, a failure between them left the contract with only a SUPERSEDED
    execution, which `design` refuses to redesign: permanently stuck.
    """

    context = _crashed(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    failed = portfolio.get_experiment(idea_id=context.idea_id, idea_version=1)
    assert failed is not None and failed.state is ExperimentState.OPERATIONALLY_FAILED

    # The successor's id collides with the failed row's own: the insert fails.
    monkeypatch.setattr(empirical, "_reserve_id", lambda: failed.experiment_id)
    with pytest.raises((RuntimeDatabaseError, DuplicateExperimentError)):
        empirical.repair_implementation(context, failed, timeout_seconds=60)
    assert portfolio.require_experiment(failed.experiment_id).state is (
        ExperimentState.OPERATIONALLY_FAILED
    ), "the failed execution was retired without a successor"

    monkeypatch.undo()
    repaired = empirical.repair_implementation(context, failed, timeout_seconds=60)
    assert repaired.contract_id == failed.contract_id
    assert portfolio.require_experiment(failed.experiment_id).state is (
        ExperimentState.SUPERSEDED
    )


def test_retiring_a_contract_retires_its_failed_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A stale prompt plus an operationally failed run was a dead end.

    The failed run is not "open", so retiring the contract left it holding
    the version's slot; the successor's design paid two calls and then met
    `DuplicateExperimentError`, and every later attempt met the superseded
    contract.
    """

    context = _crashed(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    failed = portfolio.get_experiment(idea_id=context.idea_id, idea_version=1)
    assert failed is not None and failed.contract_id
    empirical._retire_contract(
        context, portfolio.require_contract(failed.contract_id), detail="retired"
    )
    assert portfolio.require_experiment(failed.experiment_id).state is (
        ExperimentState.SUPERSEDED
    )
    assert portfolio.get_experiment(idea_id=context.idea_id, idea_version=1) is None, (
        "the version's slot is free for the successor"
    )


def _crashed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    repo: Path,
    tmp_path: Path,
) -> Any:
    """An idea whose contract-bound execution failed operationally."""

    (repo / "measure.py").write_text(CRASHING_SCRIPT, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "break it"], cwd=repo, check=True)
    idea_id = _idea(portfolio, project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=repo,
    )
    step = _advance(context)
    assert not step.ok
    return context


# ================================================ decisions on the present
def _branch_only(status: IdeaStatus, *, lineage: int) -> Any:
    """A snapshot whose one remaining stage is BRANCH, at a given lineage count."""

    done = frozenset(ORDER) - {stages.Stage.BRANCH, stages.Stage.REPLICATE}
    return snapshot(
        status=status,
        succeeded_stages=done,
        basis_stages=done,
        evidence=_complete_evidence(),
        live_reviews=_complete_reviews(),
        revision_count=1,
        lineage_active=lineage,
    )


def test_continuation_does_not_act_on_a_state_that_is_gone(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The tick read INVESTIGATING; a worker moved the idea to REVIEW since.

    `settle` wrote PARKED over whatever the idea had become -- a VALIDATED
    status a meta-review had just set, or a stage in flight.
    """

    ceiling = CONFIG.bounds.max_active_per_lineage
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.REVIEW)
    stale = portfolio.require_idea(idea.idea_id).model_copy(
        update={"status": IdeaStatus.INVESTIGATING}
    )
    assert (
        frontier.settle(
            portfolio,
            stale,
            _branch_only(IdeaStatus.INVESTIGATING, lineage=ceiling),
            CONFIG,
        )
        is None
    )
    portfolio.set_operational_state(idea_id=idea.idea_id, state=OperationalState.ACTIVE)
    current = portfolio.require_idea(idea.idea_id)
    assert (
        frontier.settle(
            portfolio, current, _branch_only(IdeaStatus.REVIEW, lineage=ceiling), CONFIG
        )
        is None
    ), "an idea with a stage in flight is not settled"
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.REVIEW


def test_continuation_rejects_and_parks_only_the_state_it_read(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The two ordinary outcomes of `settle`, each a compare-and-set.

    A fatal objection to the claim rejects; a track with every stage run
    parks. Both used to be written whatever the idea had become since the
    tick read it -- including over a VALIDATED status a meta-review had
    just set.
    """

    fatal = objection(Severity.FATAL)
    finished = frozenset(ORDER) - {stages.Stage.REPLICATE}
    cases = [
        (
            snapshot(
                status=IdeaStatus.INVESTIGATING,
                succeeded_stages=frozenset(ORDER),
                open_objections=(fatal,),
            ),
            IdeaStatus.REJECTED,
        ),
        (
            snapshot(
                status=IdeaStatus.REVIEW,
                succeeded_stages=finished,
                basis_stages=finished,
                evidence=_complete_evidence(),
                live_reviews=_complete_reviews(),
                revision_count=1,
            ),
            IdeaStatus.PARKED,
        ),
    ]
    for decided, outcome in cases:
        assert stages.select_stage(decided, CONFIG)[0] is None
        idea, _ = seed_idea(portfolio, runtime_project, title=f"settled as {outcome}")
        portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
        stale = portfolio.require_idea(idea.idea_id).model_copy(
            update={"status": decided.status}
        )
        assert frontier.settle(portfolio, stale, decided, CONFIG) is None
        assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.VALIDATED

        fresh, _ = seed_idea(
            portfolio, runtime_project, title=f"really settled as {outcome}"
        )
        portfolio.set_status(idea_id=fresh.idea_id, status=decided.status)
        assert frontier.settle(
            portfolio, portfolio.require_idea(fresh.idea_id), decided, CONFIG
        ) == str(outcome)


def test_a_reading_taken_before_the_frontier_existed_still_raises_its_question(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Found on a snapshot of the real cg portfolio after migration.

    Its INSUFFICIENT measurement was read before frontier requests existed,
    so none was raised; the tick then parked the idea with "a follow-up idea
    answers what the measurement left open" -- a condition nothing would ever
    meet. Continuation now raises the question itself, once, by the same key
    interpretation uses.
    """

    from research_os.portfolio.models import ActionStatus, Stage
    from tests.test_portfolio_scientific_contract import (
        _analysis_answer,
        _context,
        _design_answer,
    )
    from tests.test_portfolio_scientific_contract import _router as contract_router

    idea, _ = seed_idea(portfolio, runtime_project)
    version = portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(title="sharpened"),
        origin_role="scientific_discovery",
        origin_stage=str(Stage.DISCOVER),
    )
    portfolio.set_adjudication_types(
        idea_id=idea.idea_id,
        version=version.version,
        types=[str(AdjudicationType.EMPIRICAL)],
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    for stage in (
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.ADJUDICATE,
        Stage.LITERATURE_AUDIT,
    ):
        action = portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=version.version,
            stage=stage,
            basis_digest=f"b:{stage}",
        )
        portfolio.complete_action(
            action_id=action.action_id,
            status=ActionStatus.SUCCEEDED,
            operational_state=OperationalState.IDLE,
        )
    for index in range(3):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=version.version,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.SUPPORTS,
            summary=f"retrieved work {index}",
            literature_key=f"openalex:W{index}",
        )
    # Measured straight through the empirical route, as a build before the
    # frontier did: nothing here raises a request.
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea.idea_id,
        contract_router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([3], [1, 2, 3, 4, 5, 6]),
        ),
        grid_repo,
    )
    read = empirical.advance(context, portfolio.require_version(idea.idea_id))
    assert read.conclusion is EmpiricalConclusion.INSUFFICIENT, read.detail
    assert portfolio.list_requests(project_id=runtime_project) == ()

    taken = stages.snapshot_for(portfolio, portfolio.require_idea(idea.idea_id))
    assert taken is not None and taken.settled_measurement is not None
    settled = frontier.settle(
        portfolio, portfolio.require_idea(idea.idea_id), taken, CONFIG
    )
    assert settled == str(IdeaStatus.PARKED)
    (raised,) = portfolio.list_requests(project_id=runtime_project)
    assert raised.basis is RequestBasis.INSUFFICIENT
    assert raised.source_ref == read.experiment.experiment_id
    assert raised.source_idea_id == idea.idea_id


def test_the_snapshot_reads_the_status_now_not_the_callers_copy(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The tick lists ideas first and snapshots each later.

    The snapshot paired the list's status with rows read afterwards, which
    selected a stage the idea had moved past.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    listed = portfolio.require_idea(idea.idea_id)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.REVIEW)
    taken = stages.snapshot_for(portfolio, listed)
    assert taken is not None and taken.status is IdeaStatus.REVIEW


def test_a_full_lineage_parks_an_idea_only_until_it_has_room(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The lineage ceiling is a bound on concurrency, not a finding.

    Parked permanently before, with nothing to reopen it. Now parked with a
    condition the tick reads, and returned when the lineage has room for the
    idea and a child.
    """

    ceiling = CONFIG.bounds.max_active_per_lineage
    assert stages.waits_only_for_lineage_room(
        _branch_only(IdeaStatus.REVIEW, lineage=ceiling), CONFIG
    )
    assert not stages.waits_only_for_lineage_room(
        _branch_only(IdeaStatus.REVIEW, lineage=0), CONFIG
    )
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.REVIEW)
    settled = frontier.settle(
        portfolio,
        portfolio.require_idea(idea.idea_id),
        _branch_only(IdeaStatus.REVIEW, lineage=ceiling),
        CONFIG,
    )
    assert settled == str(IdeaStatus.PARKED)
    parked = portfolio.require_idea(idea.idea_id)
    assert (parked.revisit_if or "").startswith(frontier.LINEAGE_ROOM)

    assert frontier.revive_for_lineage_room(portfolio, runtime_project, CONFIG) == 1
    revived = portfolio.require_idea(idea.idea_id)
    assert revived.status is IdeaStatus.REVIEW
    assert revived.retire_reason is None and revived.revisit_if is None


def test_a_validated_idle_idea_holds_no_lineage_slot(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Closed for synthesis, it waits on nothing the lineage does."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    assert (
        portfolio.lineage_active_counts(runtime_project).get(idea.lineage_root, 0) == 0
    )
    portfolio.set_operational_state(idea_id=idea.idea_id, state=OperationalState.ACTIVE)
    assert portfolio.lineage_active_counts(runtime_project)[idea.lineage_root] == 1


def test_a_follow_up_on_a_full_lineage_waits_and_does_not_block_the_queue(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """Declined before: a question raised while the lineage was busy was lost.

    Now it stays OPEN, costs nothing while it waits, and the tick serves the
    next request instead of re-buying the one at the head of the queue.
    """

    from research_os.portfolio.tick import _servable_requests

    parent, version = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=parent.idea_id, status=IdeaStatus.INVESTIGATING)
    for index in range(CONFIG.bounds.max_active_per_lineage - 1):
        portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.BRANCH,
            fields=idea_fields(title=f"sibling {index}", research_question=f"q{index}"),
            parent_idea_id=parent.idea_id,
            origin_role="brancher",
        )
    busy = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.RESULT,
        source_ref="busy-lineage",
        question="What does the result open next?",
        source_idea_id=parent.idea_id,
        source_version=version.version,
    )
    free = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.EVIDENCE_GAP,
        source_ref="no-lineage",
        question="What evidence is missing?",
    )
    served, _literature, generation = _servable_requests(
        portfolio,
        runtime_project,
        CONFIG,
        portfolio.list_requests(
            project_id=runtime_project, kinds=(RequestKind.FOLLOW_UP,)
        ),
        (),
    )
    assert [item.request_id for item in served] == [free.request_id]
    assert generation[free.request_id] == 0

    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(answers={}, store=runtime)
    result = frontier.run_follow_up(
        frontier.FrontierContext(
            config=CONFIG,
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="f").run_id,
        ),
        busy.request_id,
    )
    assert result.ok and "deferred" in result.detail
    assert portfolio.require_request(busy.request_id).state is RequestState.OPEN
    assert router.requests == [], "waiting costs nothing"


# ============================================= generators cite what they saw
def _explorer_context(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    tmp: Path,
    router: Any,
) -> Any:
    runtime = RuntimeStore(runtime_db)
    return runner.ExplorerContext(
        config=load_config(),
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp / "a", store=runtime),
        project_id=project,
        run_id=runtime.create_run(project_id=project, objective="x").run_id,
    )


def _candidate(**overrides: Any) -> dict[str, Any]:
    payload = {
        "title": "What the refutation revealed",
        "research_question": "Is the hidden assumption the real driver?",
        "core_idea": "The refutation exposed an assumption about ties.",
        "falsifier": "Search the literature for the assumption.",
    }
    payload.update(overrides)
    return payload


def test_an_explorer_shown_sources_must_cite_one_per_direction(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """A failure-mined idea that names no failure is not failure-mined.

    `derived_from` defaulted to empty and only *unshown* ids were refused, so
    a literature or failure-mining direction citing nothing was admitted with
    no source and, for failure mining, no parent.
    """

    failed, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=failed.idea_id, status=IdeaStatus.REJECTED, retire_reason="refuted"
    )
    router = ScriptedRouter(
        answers={"failure_mining_explorer": {"candidates": [_candidate()]}},
        store=RuntimeStore(runtime_db),
    )
    outcome = runner.run_explorer(
        _explorer_context(portfolio, runtime_db, runtime_project, tmp_path, router),
        "failure_mining_explorer",
    )
    assert not outcome.ok and "citing nothing" in outcome.detail
    assert [
        item.idea_id for item in portfolio.list_ideas(project_id=runtime_project)
    ] == [failed.idea_id]


def test_a_failure_mined_child_meets_the_depth_bound(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """The bound a follow-up child meets, which failure mining stepped around."""

    failed, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=failed.idea_id, status=IdeaStatus.REJECTED, retire_reason="refuted"
    )
    with portfolio.db.tx() as conn:
        conn.execute(
            "update ideas set depth = %s where idea_id = %s",
            (load_config().bounds.max_lineage_depth, failed.idea_id),
        )
    router = ScriptedRouter(
        answers={
            "failure_mining_explorer": {
                "candidates": [_candidate(derived_from=[failed.idea_id])]
            }
        },
        store=RuntimeStore(runtime_db),
    )
    outcome = runner.run_explorer(
        _explorer_context(portfolio, runtime_db, runtime_project, tmp_path, router),
        "failure_mining_explorer",
    )
    assert outcome.ok
    assert outcome.data["created"] == [] and "depth bound" in outcome.detail


def test_seeds_are_shown_with_ids_and_credited_by_citation(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """The seeded explorer saw seed text only, so it could not cite a seed.

    Every idea was then credited to every pending seed at once, or to none.
    """

    first = portfolio.add_seed(project_id=runtime_project, text="look at ties")
    second = portfolio.add_seed(project_id=runtime_project, text="look at warm starts")
    router = ScriptedRouter(
        answers={
            "seeded_explorer": {
                "candidates": [_candidate(derived_from=[second.seed_id])]
            }
        },
        store=RuntimeStore(runtime_db),
    )
    outcome = runner.run_explorer(
        _explorer_context(portfolio, runtime_db, runtime_project, tmp_path, router),
        "seeded_explorer",
    )
    assert outcome.ok, outcome.detail
    (prompt,) = [item.prompt for item in router.requests]
    assert (
        f"seed id: {first.seed_id}" in prompt and f"seed id: {second.seed_id}" in prompt
    )
    assert set(outcome.data["seeds_shown"]) == {first.seed_id, second.seed_id}
    (created,) = outcome.data["created"]
    (reason,) = portfolio.provenance_of(created)
    assert reason.source_ref == second.seed_id


# ============================================ independence and legibility
def test_a_replication_that_varies_only_its_resources_is_refused() -> None:
    """Resources are implementation, and a repair may change them freely."""

    base = ExecutionSpec(
        name="m",
        argv=("python3", "measure.py", "--seed", "5"),
        cwd="/tmp/a",
        env={"RESEARCH_OS_SEED_0": "5"},
        outputs=("results/run.json",),
        seeds=(5,),
    )
    bigger = ExecutionSpec(
        name="m",
        argv=base.argv,
        cwd="/tmp/b",
        env=dict(base.env),
        outputs=base.outputs,
        seeds=base.seeds,
        resources={"mem": "64G"},
        timeout_seconds=7200,
    )
    assert empirical.variation_digest(bigger) != empirical.variation_digest(base)
    with pytest.raises(empirical.EmpiricalError, match="not an independent"):
        empirical.assert_varies(
            replication=empirical.scientific_variation(bigger),
            primary=empirical.scientific_variation(base),
        )


def _measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    repo: Path,
    tmp_path: Path,
    *,
    replication_seed: int,
) -> Any:
    idea_id = _idea(portfolio, project)
    router = ScriptedRouter(
        answers={},
        answers_by_prompt=contract_prompt_answers(
            primary=design_answer(seed=5),
            replication=design_answer(seed=replication_seed, variation_kind="seed"),
        ),
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=project,
        idea_id=idea_id,
        router=router,
        repo=repo,
    )
    primary = _advance(context)
    assert primary.ok, primary.detail
    return context


def test_a_replication_that_reproduces_the_bytes_is_insufficient(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Seeds 5 and 14 give the same number from this program: nothing varied.

    The seed did vary, so the specification check passes -- and the program
    maps both seeds to the same output. A replication that reproduces the
    primary's bytes establishes nothing a rerun would not, whatever it
    concluded.
    """

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    step = _advance(context, role=ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "byte-identical" in step.detail

    other = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path / "second",
        replication_seed=15,
    )
    varied = _advance(other, role=ExperimentRole.REPLICATION)
    assert varied.conclusion is EmpiricalConclusion.SUPPORTS, varied.detail


def test_the_evidence_a_reviewer_reads_carries_the_frozen_analysis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Reviewers saw a verdict sentence and could not see what it was over."""

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=15,
    )
    (row,) = portfolio.list_evidence(idea_id=context.idea_id)
    assert "frozen analysis: estimand:" in row.summary
    assert "records:" in row.summary or "observable" in row.summary
    assert "decision (fixed before the design)" in row.summary


def test_a_contract_frozen_after_an_earlier_reading_says_so(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A revision gets a fresh preregistration, written after a result existed.

    Not prevented -- revising is how an idea answers its critics -- but no
    longer silent: the evidence names the reading that preceded it.
    """

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=15,
    )
    (first,) = portfolio.list_experiments(idea_id=context.idea_id)
    portfolio.append_version(
        idea_id=context.idea_id,
        fields=idea_fields(
            title="the same question, restated after the first measurement",
            adjudication_types=[AdjudicationType.EMPIRICAL],
        ),
        origin_role="scientific_discovery",
    )
    second = _advance(context)
    assert second.ok, second.detail
    rows = portfolio.list_evidence(idea_id=context.idea_id, idea_version=2)
    (row,) = rows
    assert "frozen after 1 earlier reading" in row.summary
    assert first.experiment_id in row.summary


def test_the_reading_record_names_the_engine_that_computed_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A later fix to the engine must not silently change an old reading."""

    from research_os.portfolio.analysis import ENGINE_VERSION

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=15,
    )
    (execution,) = portfolio.list_experiments(idea_id=context.idea_id)
    document = json.loads(context.artifacts.get_text(execution.analysis_artifact_id))
    assert document["analysis_result"]["engine"] == ENGINE_VERSION
