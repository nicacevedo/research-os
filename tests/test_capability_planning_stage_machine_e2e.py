"""The run-2 failure class, through the real stage machine, to a system-computed outcome.

No cost: every model role is scripted, the literature index is a fixture
corpus, and nothing else is replaced -- the tick decides and enqueues, the
daemon runs the portfolio's registered work kinds, the queue retries what it
retries, and the executor is the one ``researchd`` builds, with containment
REQUIRED. The science repository declares what one execution of its
capability really holds (one noise seed, five ``x``, five records), and the
idea's evidence requirement is ten records over two independent seeds::

    blind explorer -> dedup -> screen -> falsifier -> discovery (evidence need)
    -> adjudication (empirical) -> literature audit
    -> evidence stage, attempt 1: the analysis author is shown the capability
       envelope, proposes fixed_single_execution, and the pre-freeze check
       refuses it as EXECUTION_SHAPE_MISMATCH -- nothing is frozen, the draft
       is recorded, and the stage fails MODEL_OUTPUT_INVALID
    -> the queue's own retry: the author is shown the refusal and proposes
       fixed_campaign, two units differing in seeds; that is checked, frozen,
       and bound to the envelope
    -> experimental design (two units) -> frozen campaign plan -> two real
       contained executions, each with its own trusted receipt -> each result
       validated -> deterministic combination -> the primary outcome
    -> review board -> a replication campaign, whose inherited analysis is
       checked against the same envelope -> assessment -> HUMAN_READY,

and the frozen v1 qualification evaluator then reads that database.

After the explorer proposes the idea the test inserts nothing and sets no
status.
"""

from __future__ import annotations

import dataclasses
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import extensions as portfolio_extensions
from research_os.portfolio import qualification, scicontract, sciencechain
from research_os.portfolio.config import load_config
from research_os.portfolio.contracts import AnalysisSpec
from research_os.portfolio.models import (
    ExecutionShapeVerdict,
    ExperimentRole,
    ExperimentState,
    IdeaOrigin,
    IdeaStatus,
    PrimaryOutcome,
    Stage,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.daemon import Daemon
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.store import RuntimeStore
from research_os.sandbox import available_backend, unavailable_reason
from tests.portfolio_helpers import portfolio
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_campaign_stage_machine_e2e import CampaignRouter, _router, _trail
from tests.test_capability_planning import FIRST, REVISED, bounded_manifest
from tests.test_portfolio_frontier import checkpoint_tables
from tests.test_science_campaigns import commit_manifest, science_repo

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "science_repo",
]

pytestmark = pytest.mark.skipif(
    available_backend() is None,
    reason=f"no containment technology is available here: {unavailable_reason()}",
)

ANALYST = TEMPLATES["analysis_designer"].identity


class PlanningRouter(CampaignRouter):
    """The campaign E2E's roles, with an analysis author that revises when refused."""

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if request.prompt_version == ANALYST:
            self.answers_by_prompt[ANALYST] = (
                REVISED if "REFUSED ANALYSIS:" in request.prompt else FIRST
            )
        return super().complete(request)


def _planning_router(runtime_db: Database) -> PlanningRouter:
    base = _router(runtime_db)
    router = PlanningRouter(
        **{item.name: getattr(base, item.name) for item in dataclasses.fields(base)}
    )
    router.explorer_calls = 0
    router.children = 0
    router.answers["scientific_discovery"] = {
        **router.answers["scientific_discovery"],
        "evidence_needs": {
            "data": "new_execution",
            "fields": ["x", "y", "seed"],
            "independent_draws": 2,
        },
    }
    return router


def test_a_sample_larger_than_one_execution_is_planned_as_a_campaign_before_it_freezes(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    science_repo: Path,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_manifest(science_repo, bounded_manifest())
    portfolio_extensions.register()
    from tests.test_portfolio_frontier import Corpus

    monkeypatch.setattr(portfolio_extensions, "_literature", lambda: Corpus())
    router = _planning_router(runtime_db)
    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(science_repo)
    )
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(30),
        explicit=True,
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    zero = qualification.zero_state_snapshot(
        runtime_db, project_id=runtime_project, repo_path=science_repo
    )
    (evidence_dir / "00-zero-state.json").write_text(json.dumps(zero), encoding="utf-8")
    runtime_config = make_config(pg_dsn, tmp_path / "artifacts")
    daemon = Daemon(
        config=runtime_config,
        db=runtime_db,
        repo_for=lambda _project: science_repo,
        models=lambda _run, _project, _work: router,
        owner="planning-e2e-worker",
    )
    config = load_config().with_overrides(
        {"candidate_pool_floor": 1, "max_active_tracks": 3}
    )

    def root() -> Any:
        ideas = [
            item
            for item in portfolio.list_ideas(project_id=runtime_project, limit=20)
            if item.depth == 0
        ]
        return ideas[0] if ideas else None

    def read() -> Any:
        current = root()
        if current is None:
            return None
        found = portfolio.get_experiment(
            idea_id=current.idea_id,
            idea_version=current.current_version,
            role=ExperimentRole.PRIMARY,
        )
        return found if found and found.state is ExperimentState.INTERPRETED else None

    def run_until(done: Any, *, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not done() and time.monotonic() < deadline:
            tick(
                db=runtime_db,
                project_id=runtime_project,
                runtime_config=runtime_config,
                portfolio_config=config,
            )
            worked = False
            for _ in range(60):
                if not daemon.tick().did_something:
                    break
                worked = True
            if not worked:
                # The queue's retry of the refused proposal is scheduled a few
                # seconds out (`failures.retry_delay_seconds`); wait for it
                # rather than reach into the queue.
                time.sleep(0.5)

    run_until(lambda: read() is not None, seconds=600)

    current = root()
    trail = _trail(portfolio, runtime_db, runtime_project)
    experiment = read()
    assert current is not None and experiment is not None, trail
    assert current.origin is IdeaOrigin.BLIND_EXPLORER and current.depth == 0
    version = current.current_version

    # Attempt 1: shown the envelope, proposed one execution, refused before
    # freezing. The stage failed as a malformed answer, which the queue retries.
    asks = router.requests_for_prompt(ANALYST)
    assert len(asks) == 2, trail
    assert "CAPABILITY ENVELOPE:" in asks[0].prompt
    assert "1 draws of observable points" in asks[0].prompt
    assert "REFUSED ANALYSIS:" not in asks[0].prompt
    assert "REFUSED ANALYSIS:" in asks[1].prompt
    evidence = [
        item
        for item in portfolio.list_actions(idea_id=current.idea_id)
        if item.stage is Stage.EVIDENCE
    ]
    assert str(evidence[0].status) == "FAILED", trail
    assert evidence[0].failure_class == "model_output_invalid"
    assert "EXECUTION_SHAPE_MISMATCH" in (evidence[0].detail or "")
    (draft,) = portfolio.analysis_drafts(
        idea_id=current.idea_id, idea_version=version, role=ExperimentRole.PRIMARY
    )
    assert draft.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert draft.check_record["stopping_rule"] == "fixed_single_execution"
    assert draft.check_record["campaign"]["min_units"] == 2
    with runtime_db.tx() as conn:
        retried = conn.execute(
            "select attempts, status from work_items where kind = "
            "'portfolio_advance_idea' and payload->>'stage' = 'evidence' "
            "order by created_at"
        ).fetchall()
    assert retried[0]["attempts"] >= 2 and retried[0]["status"] == "SUCCEEDED", retried

    # Only the campaign form was frozen, bound to the envelope it was shown.
    contract = portfolio.require_contract(experiment.contract_id or "")
    assert contract.analysis_digest == scicontract.analysis_digest(
        AnalysisSpec.model_validate(REVISED)
    )
    assert contract.analysis_digest != draft.analysis_digest
    assert contract.envelope_digest == draft.envelope_digest
    artifacts = FilesystemArtifactStore(
        tmp_path / "artifacts", store=RuntimeStore(runtime_db)
    )
    stored = json.loads(artifacts.get_text(contract.analysis_artifact_id))
    assert stored["execution_shape_check"]["verdict"] == "VALID_CAMPAIGN"
    assert stored["revises"][0]["draft_id"] == draft.draft_id

    # Design -> a frozen two-unit campaign -> two contained executions, each
    # with its own trusted receipt -> the deterministic primary outcome.
    chain = sciencechain.verify_plan(portfolio, artifacts, experiment)
    assert chain.is_campaign and len(chain.units) == 2
    assert chain.contract.payload["stopping_rule"] == "fixed_campaign"
    assert chain.contract.payload["execution_shape"]["units"] == 2
    receipts = portfolio.unit_receipts(experiment.experiment_id)
    assert [item.unit_index for item in receipts] == [0, 1]
    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select contained, status from external_jobs where job_id = any(%s)",
            ([item.job_id for item in receipts],),
        ).fetchall()
    assert len(rows) == 2
    assert all(
        row["contained"] is True and row["status"] == "COMPLETED" for row in rows
    )
    (reading,) = [
        item
        for item in portfolio.outcomes(idea_id=current.idea_id, idea_version=version)
        if item.receipt_id and item.role is ExperimentRole.PRIMARY
    ]
    assert reading.state is PrimaryOutcome.SUPPORTED
    assert reading.unit_count == 2
    assert len(portfolio.outcome_units(reading.outcome_id)) == 2
    (record,) = [
        item
        for item in sciencechain.science_chains(
            portfolio,
            artifacts,
            idea_id=current.idea_id,
            idea_version=version,
            evidence=portfolio.list_evidence(
                idea_id=current.idea_id, idea_version=version
            ),
        )
        if item.role is ExperimentRole.PRIMARY
    ]
    assert record.admissible, record.problems
    # And no model was paid for anything but the two analyses, one design and
    # the stages before them: the refusal was mechanical.
    assert (
        len(router.requests_for_prompt(TEMPLATES["experiment_designer"].identity)) == 1
    )

    # Onward, unchanged: board, a replication campaign whose inherited
    # analysis is checked against the same envelope, assessment, HUMAN_READY.
    def ready() -> bool:
        head = root()
        return (
            head is not None
            and head.status is IdeaStatus.HUMAN_READY
            and any(
                item.depth >= 1
                for item in portfolio.list_ideas(project_id=runtime_project, limit=20)
            )
        )

    run_until(ready, seconds=600)
    assert ready(), _trail(portfolio, runtime_db, runtime_project)
    replicated = portfolio.live_contract(
        idea_id=current.idea_id, idea_version=version, role=ExperimentRole.REPLICATION
    )
    assert replicated is not None
    assert replicated.analysis_digest == contract.analysis_digest
    assert replicated.envelope_digest == contract.envelope_digest
    assert len(router.requests_for_prompt(ANALYST)) == 2, (
        "a replication authors nothing"
    )

    (evidence_dir / "50-isolation.txt").write_text(
        qualification.isolation_report(zero_state=zero, repo_path=science_repo),
        encoding="utf-8",
    )
    report = qualification.evaluate(
        runtime_db,
        project_id=runtime_project,
        artifacts=artifacts,
        evidence_dir=evidence_dir,
        repo_path=science_repo,
        expect_spec_digest=qualification.spec_digest(qualification.SPEC_PATH),
    )
    gates = {item["id"]: item for item in report.record()["gates"]}
    wanted = [f"Q{n:02d}" for n in range(1, 24)] + ["Q25", "Q26"]
    failed = {
        key: gates[key]["detail"] for key in wanted if gates[key]["status"] != "PASS"
    }
    assert not failed, failed
    assert "0 not contained" in gates["Q24"]["detail"]
