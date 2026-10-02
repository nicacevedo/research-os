"""The second analysis language on the production route, with a real contained subprocess.

``docs/SCIENCE_EXECUTION.md`` §4a. The synthetic science repository of
``tests/test_capability_planning.py`` declares what its program really holds:
one execution draws one noise seed (``RESEARCH_OS_SEED_0``) over at most five
``x``. What these tests prove:

- an analysis in the second language -- a table grouped by seed, a per-seed
  regression, a statistic over the table -- is frozen under its own digest
  version, its table's support reaches the pre-freeze check, the design and
  the campaign proceed, every unit runs contained, and the reading the
  system records is the one a re-verified analysis gives on the stored
  combined result;
- a crash between freezing the analysis and designing it resumes from the
  frozen analysis without asking anyone again, and reads it the same way;
- a contract Cycle 002 left blocked -- frozen unanalysable by
  ``analysis_designer@4`` and blocked on capability, with nothing about the
  declared commands changed -- is retired, unread, the next time its idea
  reaches the evidence stage, and is analysed again by ``@5`` in the
  language that can express it; under an unchanged prompt the same contract
  costs nothing and stays blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_os.portfolio import analysis as engine
from research_os.portfolio import empirical, scicontract, sciencechain, shape
from research_os.portfolio.contracts import AnalysisSpec
from research_os.portfolio.models import (
    ContractState,
    EmpiricalConclusion,
    ExecutionShapeVerdict,
    ExperimentRole,
    ExperimentState,
    PrimaryOutcome,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from tests.portfolio_helpers import portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_capability_planning import (
    ANALYST,
    DESIGNER,
    REF,
    analyst_requests,
    bounded_repo,
    contract_of,
    revising_router,
    stored_analysis,
)
from tests.test_science_campaigns import (
    advance,
    campaign_design,
    context_for,
    outcomes,
    science_repo,
    unit,
)

__all__ = [
    "bounded_repo",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "science_repo",
]


def per_seed_analysis(units: int = 3) -> dict[str, Any]:
    """The slope of each seed's line, and their median: a statistic over a table."""

    return {
        "analysable": True,
        "estimand": "the typical per-seed slope of the response in x",
        "population": "the synthetic draws the campaign makes",
        "target_claim": "every independent draw rises with x",
        "observables": [
            {
                "name": "points",
                "source": "results/draw.json",
                "kind": "records",
                "path": "records",
                "fields": ["x", "y", "seed"],
            }
        ],
        "tables": [
            {
                "name": "per_seed",
                "source": "points",
                "by": ["seed"],
                "aggregates": [
                    {
                        "name": "slope",
                        "op": "ols_coefficient",
                        "response": "y",
                        "terms": ["x"],
                        "coefficient": "x",
                    },
                    {
                        "name": "where_half",
                        "op": "crossing",
                        "field": "x",
                        "other_field": "y",
                        "crossing": {
                            "level": 2.5,
                            "pick": "single",
                            "direction": "rising",
                        },
                    },
                ],
            }
        ],
        "reductions": [
            {
                "name": "typical",
                "op": "median",
                "observable": "per_seed",
                "field": "slope",
            },
            {
                "name": "spread",
                "op": "std",
                "observable": "per_seed",
                "field": "where_half",
            },
        ],
        "primary_statistic": "typical",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": 0.1},
        "support": [
            {
                "observable": "points",
                "min_records": 5 * units,
                "min_distinct": {"x": 5},
            },
            {"observable": "per_seed", "min_records": units},
        ],
        "stopping_rule": "fixed_campaign",
        "execution_shape": {
            "capability": REF,
            "units": units,
            "unit_varies": ["seeds"],
            "rationale": "one execution draws one seed; the table needs one per unit",
        },
    }


def three_units() -> dict[str, Any]:
    return campaign_design([unit(7), unit(8), unit(9)])


def context_here(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    design: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    router = revising_router(
        runtime_db, first=per_seed_analysis(), design=design or three_units()
    )
    return context_for(
        portfolio, runtime_db, runtime_project, repo, tmp_path, router
    ), router


def interpreted(portfolio: PortfolioStore, context: Any) -> Any:
    (experiment,) = [
        item
        for item in portfolio.list_experiments(idea_id=context.idea_id)
        if item.state is ExperimentState.INTERPRETED
    ]
    return experiment


def reread(
    portfolio: PortfolioStore, context: Any, experiment: Any
) -> tuple[engine.AnalysisResult, Any]:
    """The frozen analysis, re-verified from its bytes, on the combined result rebuilt.

    The combination is rebuilt from the stored unit results as the gate does
    it, and must hash to what the outcome names.
    """

    import hashlib

    from research_os.portfolio import campaign as campaigns

    contract = portfolio.require_contract(experiment.contract_id or "")
    verified = scicontract.verify(context.artifacts, contract)
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    receipts = portfolio.unit_receipts(experiment.experiment_id, attempt=0)
    documents = [
        (
            item.unit_index,
            json.loads(
                context.artifacts.get_bytes(
                    portfolio.unit_result(item.receipt_id).result_artifact_id
                )
            ),
        )
        for item in receipts
    ]
    combined = campaigns.combine(chain.aggregation, documents)
    assert combined.ok and combined.data is not None
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert hashlib.sha256(combined.data).hexdigest() == outcome.result_sha256
    result = engine.evaluate(
        verified.analysis,
        {
            "results/draw.json": engine.parse_bytes(
                combined.data, name="results/draw.json"
            )
        },
    )
    return result, outcome


# ======================================================== frozen and read --
def test_a_second_language_analysis_is_frozen_run_as_a_campaign_and_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    context, router = context_here(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path
    )
    step = advance(context)
    assert step.ok, step.detail
    experiment = interpreted(portfolio, context)
    assert experiment.conclusion is EmpiricalConclusion.SUPPORTS

    contract = portfolio.require_contract(experiment.contract_id or "")
    assert contract.analysis_prompt == ANALYST == "analysis_designer@5"
    assert contract.analysis_digest.startswith("panalysis-v2:")
    document = stored_analysis(context, contract)
    assert document["schema"] == "portfolio-analysis-v2"
    assert document["analysis"]["tables"][0]["by"] == ["seed"]
    # Checked against the envelope before it was frozen: a table of three
    # seeds needs three seed-differing units, and the stated campaign has them.
    check = document["execution_shape_check"]
    assert check["verdict"] == str(ExecutionShapeVerdict.VALID_CAMPAIGN)
    assert any(
        item["field"] == "seed" and item["needed"] == 3 for item in check["demands"]
    ), check["demands"]

    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    primary = chain.design.payload["primary_analysis"]
    assert primary["analysis_digest"] == contract.analysis_digest
    assert primary["tables"] == document["analysis"]["tables"]
    (need,) = chain.design.payload["required_observables"]
    assert set(need["fields"]) == {"x", "y", "seed"}
    assert set(need["numeric_fields"]) == {"x", "y"}

    reading, outcome = reread(portfolio, context, experiment)
    assert outcome.state is PrimaryOutcome.SUPPORTED
    assert outcome.unit_count == 3
    assert reading.conclusion is EmpiricalConclusion.SUPPORTS
    assert reading.records["per_seed"]["included"] == 3
    assert reading.statistic == outcome.estimate
    assert len(analyst_requests(router)) == 1


def test_a_crash_between_freezing_and_designing_resumes_the_same_frozen_analysis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    context, router = context_here(
        portfolio,
        runtime_db,
        runtime_project,
        bounded_repo,
        tmp_path,
        design={"testable": True, "command": "draw"},  # malformed: a retry
    )
    first = advance(context)
    assert not first.ok and first.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    held = contract_of(portfolio, context)
    assert held is not None and held.state is ContractState.ANALYSIS_FROZEN
    frozen_bytes = context.artifacts.get_text(held.analysis_artifact_id)

    router.answers_by_prompt[DESIGNER] = three_units()
    second = advance(context)
    assert second.ok, second.detail
    experiment = interpreted(portfolio, context)
    assert experiment.contract_id == held.contract_id
    assert len(analyst_requests(router)) == 1, (
        "the frozen analysis is not asked for again"
    )
    after = portfolio.require_contract(held.contract_id)
    assert after.analysis_digest == held.analysis_digest
    assert context.artifacts.get_text(after.analysis_artifact_id) == frozen_bytes
    verified = scicontract.verify(context.artifacts, after)
    assert scicontract.analysis_digest(verified.analysis) == held.analysis_digest
    reading, outcome = reread(portfolio, context, experiment)
    assert reading.conclusion is experiment.conclusion
    assert reading.statistic == outcome.estimate


# ===================================================== cycle 002's blocks --
def plant_cycle002_block(context: Any, *, prompt: str) -> Any:
    """A contract as Cycle 002 left four: frozen unanalysable, then blocked on capability."""

    version = context.portfolio.require_version(context.idea_id)
    loaded, error = empirical.capability_manifest(context)
    commands = empirical.declared_commands(context.project_id)
    planning = shape.planning_for(loaded, commands, context.config.bounds)
    assert planning is not None
    unanalysable = AnalysisSpec.model_validate(
        {
            "analysable": False,
            "unanalysable_reason": (
                "The reduction vocabulary (value, count, fraction, mean, median, std, "
                "min, max, sum, quantile, difference, ratio, correlation, "
                "ols_coefficient) has no grouping construct, so a per-seed slope "
                "cannot be formed."
            ),
        }
    )
    checked = planning.check(unanalysable)
    assert checked.verdict is ExecutionShapeVerdict.UNRESOLVED
    contract = empirical._freeze_analysis(
        context,
        version,
        role=ExperimentRole.PRIMARY,
        spec=unanalysable,
        provenance={"analysis_prompt": prompt, "analysis_role": "analysis_designer"},
        analysis_prompt=prompt,
        analysis_call_id=None,
        checked=checked,
    )
    current = scicontract.capability_set_digest(
        commands, manifest=empirical._manifest_identity(loaded, error)
    )
    return context.portfolio.block_contract_on_capability(
        contract.contract_id,
        capability_request={
            "unmet": ["observables: " + unanalysable.unanalysable_reason]
        },
        command_set_digest=current,
        detail="CAPABILITY_LIMITED: observables",
    )


def test_a_contract_cycle_002_left_blocked_is_analysed_again_in_the_new_language(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    context, router = context_here(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path
    )
    blocked = plant_cycle002_block(context, prompt="analysis_designer@4")
    assert blocked.state is ContractState.BLOCKED_CAPABILITY
    assert empirical._contract_is_stale(context, blocked)

    step = advance(context)
    assert step.ok, step.detail
    retired = portfolio.require_contract(blocked.contract_id)
    assert retired.state is ContractState.SUPERSEDED
    assert "analysis_designer@4" in (retired.detail or "")
    assert "retired, before anything was measured" in (retired.detail or "")
    experiment = interpreted(portfolio, context)
    successor = portfolio.require_contract(experiment.contract_id or "")
    assert successor.contract_id != blocked.contract_id
    assert successor.analysis_prompt == "analysis_designer@5"
    assert successor.analysis_digest.startswith("panalysis-v2:")
    assert experiment.conclusion is EmpiricalConclusion.SUPPORTS
    assert len(analyst_requests(router)) == 1


def test_under_an_unchanged_prompt_the_same_block_costs_nothing_and_holds(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """The control: nothing but the retired prompt reopens a blocked contract."""

    context, router = context_here(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path
    )
    blocked = plant_cycle002_block(context, prompt=ANALYST)
    assert not empirical._contract_is_stale(context, blocked)
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert "still waiting for a capability" in step.detail
    assert step.model_calls == 0 and not analyst_requests(router)
    assert portfolio.require_contract(blocked.contract_id).state is (
        ContractState.BLOCKED_CAPABILITY
    )
