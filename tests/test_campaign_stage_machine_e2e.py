"""One autonomous idea, through the real stage machine, to HUMAN_READY -- by a campaign.

No cost: every model role is scripted, the literature index is a fixture
corpus, and nothing else is replaced. The tick decides and enqueues; the
daemon runs the portfolio's registered work kinds; the executor is the one
`researchd` builds, with containment REQUIRED. After the explorer proposes the
idea the test inserts nothing and sets no status:

    blind explorer -> dedup -> novelty screen -> falsifier -> discovery
    (its structured evidence need) -> adjudication (empirical) -> literature
    audit -> scientific contract (fixed_campaign) -> experimental design (two
    units) -> capability binding -> frozen campaign plan -> two real contained
    executions, each with its own trusted receipt -> each unit's result
    validated -> deterministic combination -> system-computed primary outcome
    -> review board (three families) -> meta-review -> a replication campaign,
    unit by unit, under manifests frozen before each unit -> assessment
    (VERIFIED, ATTESTED, MEASURED) -> meta-review -> the deterministic gate ->
    HUMAN_READY.

Then the frozen v1 qualification evaluator reads that database, and every gate
the campaign evidence feeds (Q08-Q22, Q25, Q26, and the automated half of Q24)
passes on it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import extensions as portfolio_extensions
from research_os.portfolio import qualification, sciencechain
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ExperimentRole,
    ExperimentState,
    IdeaOrigin,
    IdeaStatus,
    PrimaryOutcome,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.daemon import Daemon
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from research_os.sandbox import available_backend, unavailable_reason
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_frontier import Corpus, _matrix, checkpoint_tables
from tests.test_science_campaigns import analysis, campaign_design, science_repo, unit

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

IDEA = {
    **{
        key: value
        for key, value in idea_fields().items()
        if key != "adjudication_types"
    },
    "title": "The seeded response rises with x",
    "research_question": "Does the response rise with x across independent noise seeds?",
    "core_idea": "The response has a positive slope whatever the noise seed.",
    "mechanism": "The plan's slope is positive and the noise is small and symmetric.",
    "falsifier": (
        "Run the draw over five x for two independent noise seeds and measure the "
        "slope; the idea is wrong if the measured slope is not positive."
    ),
}


CHILD = {
    **IDEA,
    "title": "The slope survives larger noise",
    "research_question": "Does the positive slope persist when the noise is larger?",
    "core_idea": "The slope is a property of the plan, not of the noise level.",
}


class CampaignRouter(ScriptedRouter):
    """Answers each role from the prompt it is given, as the loop test's router does."""

    explorer_calls: int
    children: int

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        role = str(request.role)
        if role == "blind_explorer":
            self.explorer_calls += 1
            self.answers["blind_explorer"] = (
                {"candidates": [IDEA]}
                if self.explorer_calls == 1
                else {"candidates": [], "nothing_to_propose": "one direction is enough"}
            )
        elif role == "follow_up_explorer":
            self.children += 1
            self.answers["follow_up_explorer"] = (
                {"children": [CHILD], "relations": ["DERIVED_FROM"]}
                if self.children == 1
                else {
                    "children": [],
                    "relations": [],
                    "nothing_to_propose": "one follow-up is enough here",
                }
            )
        elif role == "literature_scout":
            keys = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt)))
            self.answers["literature_scout"] = _matrix(keys)
        return super().complete(request)


def _pass(who: str) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "summary": f"{who} found nothing to object to",
        "objections": [],
    }


def _router(runtime_db: Database) -> CampaignRouter:
    router = CampaignRouter(
        answers={
            "duplicate_adjudicator": {"verdict": "distinct", "rationale": "different"},
            "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
            "falsifier": {
                "summary": "no cheap kill",
                "objections": [],
                "attempted": ["a known closed form for this response"],
            },
            "scientific_discovery": {
                "can_be_made_precise": True,
                "minimum_decisive_action": "measure the slope over two seeds",
                "refined": IDEA,
                "evidence_needs": {
                    "data": "new_execution",
                    "fields": ["x", "y", "seed"],
                    "independent_draws": 2,
                },
            },
            "methodology_reviewer": _pass("the methodologist"),
            "novelty_reviewer": _pass("the novelty reviewer"),
            "skeptic_reviewer": _pass("the skeptic"),
            "meta_reviewer": {
                "recommendation": "HUMAN_READY",
                "summary": "the board was satisfied and the replication is on record",
                "unresolved_disagreements": [],
                "follow_up_questions": [
                    "Does the positive slope persist when the noise is larger?"
                ],
            },
            "brancher": {"children": [], "relations": []},
            "follow_up_explorer": {
                "children": [],
                "relations": [],
                "nothing_to_propose": "this scenario follows one direction",
            },
            "failure_mining_explorer": {"candidates": [], "nothing_to_propose": "none"},
            "literature_explorer": {"candidates": [], "nothing_to_propose": "none"},
        },
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: analysis(),
            TEMPLATES["experiment_designer"].identity: campaign_design(
                [unit(7), unit(8)]
            ),
            TEMPLATES["replication_designer"].identity: campaign_design(
                [unit(17), unit(18)], variation="seeds"
            ),
        },
        store=RuntimeStore(runtime_db),
        providers={
            "methodology_reviewer": "alpha",
            "novelty_reviewer": "beta",
            "skeptic_reviewer": "gamma",
        },
        models_by_role={
            "methodology_reviewer": "alpha-1",
            "novelty_reviewer": "beta-1",
            "skeptic_reviewer": "gamma-1",
        },
    )
    router.explorer_calls = 0
    router.children = 0
    return router


def test_an_autonomous_idea_reaches_human_ready_through_a_contained_campaign(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    science_repo: Path,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio_extensions.register()
    # The one seam: the literature index is a fixture corpus.
    monkeypatch.setattr(portfolio_extensions, "_literature", lambda: Corpus())
    router = _router(runtime_db)
    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(science_repo)
    )
    from decimal import Decimal

    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope

    # A person's ceiling, as a qualification sets one.
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(30),
        explicit=True,
    )
    evidence = tmp_path / "evidence"
    zero = qualification.zero_state_snapshot(
        runtime_db, project_id=runtime_project, repo_path=science_repo
    )
    evidence.mkdir()
    import json

    (evidence / "00-zero-state.json").write_text(json.dumps(zero), encoding="utf-8")
    daemon = Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: science_repo,
        models=lambda _run, _project, _work: router,
        owner="campaign-e2e-worker",
    )
    config = load_config().with_overrides(
        {"candidate_pool_floor": 1, "max_active_tracks": 3}
    )

    def idea() -> Any:
        roots = [
            item
            for item in portfolio.list_ideas(project_id=runtime_project, limit=20)
            if item.depth == 0
        ]
        return roots[0] if roots else None

    def child_exists() -> bool:
        return any(
            item.depth >= 1
            for item in portfolio.list_ideas(project_id=runtime_project, limit=20)
        )

    for _ in range(80):
        tick(
            db=runtime_db,
            project_id=runtime_project,
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=config,
        )
        for _ in range(60):
            if not daemon.tick().did_something:
                break
        current = idea()
        if (
            current is not None
            and current.status is IdeaStatus.HUMAN_READY
            and child_exists()
        ):
            break

    current = idea()
    trail = _trail(portfolio, runtime_db, runtime_project)
    assert current is not None and current.status is IdeaStatus.HUMAN_READY, trail
    assert current.origin is IdeaOrigin.BLIND_EXPLORER and current.depth == 0

    version = current.current_version
    primary = portfolio.get_experiment(
        idea_id=current.idea_id, idea_version=version, role=ExperimentRole.PRIMARY
    )
    replication = portfolio.get_experiment(
        idea_id=current.idea_id, idea_version=version, role=ExperimentRole.REPLICATION
    )
    assert primary.state is ExperimentState.INTERPRETED
    assert replication.state is ExperimentState.INTERPRETED
    artifacts = FilesystemArtifactStore(
        tmp_path / "artifacts", store=RuntimeStore(runtime_db)
    )

    # A frozen campaign, and two real contained executions of it per role,
    # each with its own trusted receipt.
    for experiment in (primary, replication):
        chain = sciencechain.verify_plan(portfolio, artifacts, experiment)
        assert chain.is_campaign
        assert chain.contract.payload["stopping_rule"] == "fixed_campaign"
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
    # The deterministic outcomes, each naming every unit.
    readings = [
        item
        for item in portfolio.outcomes(idea_id=current.idea_id, idea_version=version)
        if item.receipt_id and item.unit_count
    ]
    assert {item.role for item in readings} == {
        ExperimentRole.PRIMARY,
        ExperimentRole.REPLICATION,
    }
    assert all(item.state is PrimaryOutcome.SUPPORTED for item in readings)
    assert all(len(portfolio.outcome_units(item.outcome_id)) == 2 for item in readings)
    # The replication, unit by unit: each unit's parent is the primary's unit.
    assert [
        item.parent_receipt_id
        for item in portfolio.unit_receipts(replication.experiment_id)
    ] == [item.receipt_id for item in portfolio.unit_receipts(primary.experiment_id)]
    (assessment,) = portfolio.replication_assessments(
        idea_id=current.idea_id, idea_version=version
    )
    assert assessment.configuration_independent and assessment.perturbation_attested
    assert assessment.agrees is True
    # The feasibility signal the discovery stage made possible.
    assert (
        portfolio.evidence_needs(project_id=runtime_project)[
            (current.idea_id, version)
        ]["independent_draws"]
        == 2
    )

    # And the frozen qualification contract, read over this database.
    (evidence / "50-isolation.txt").write_text(
        qualification.isolation_report(zero_state=zero, repo_path=science_repo),
        encoding="utf-8",
    )
    report = qualification.evaluate(
        runtime_db,
        project_id=runtime_project,
        artifacts=artifacts,
        evidence_dir=evidence,
        repo_path=science_repo,
        expect_spec_digest=qualification.spec_digest(qualification.SPEC_PATH),
    )
    gates = {item["id"]: item for item in report.record()["gates"]}
    needs_campaign_evidence = [f"Q{n:02d}" for n in range(1, 24)] + ["Q25", "Q26"]
    failed = {
        key: gates[key]["detail"]
        for key in needs_campaign_evidence
        if gates[key]["status"] != "PASS"
    }
    assert not failed, failed
    # Q24 is `both`: its automated half is the executions, all contained. Its
    # evidence half is a real host containment audit, which this test does not
    # fabricate.
    assert "0 not contained" in gates["Q24"]["detail"]


def _trail(portfolio: PortfolioStore, runtime_db: Database, project: str) -> str:
    lines = []
    for item in portfolio.list_ideas(project_id=project, limit=20):
        lines.append(f"{item.idea_id} {item.status} {item.operational_state}")
        for action in portfolio.list_actions(idea_id=item.idea_id)[-14:]:
            lines.append(
                f"    {action.stage} {action.status} {action.failure_class or ''} "
                f"{(action.detail or '')[:200]}"
            )
    with runtime_db.tx() as conn:
        failed = conn.execute(
            "select kind, last_error from work_items where status = 'FAILED' "
            "order by created_at"
        ).fetchall()
    for row in failed:
        lines.append(f"  FAILED {row['kind']}: {(row['last_error'] or '')[:300]}")
    return "\n".join(lines)
