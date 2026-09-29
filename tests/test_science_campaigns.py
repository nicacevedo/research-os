"""Multi-execution scientific campaigns, end to end, on a synthetic science repository.

contract (stopping rule fixed_campaign) -> design (its units) -> frozen
campaign plan -> each unit by the trusted runner, its own receipt -> each
unit's result validated -> deterministic aggregation -> one outcome bound to
every receipt -> a campaign replication, unit by unit -> the gate re-verifies
all of it. ``docs/SCIENCE_EXECUTION.md`` §3a is the specification.

The science repository declares one capability whose program draws a seeded
linear response over a composed grid of ``x``: its records carry the seed that
drew them, so a campaign's units are different observations exactly when the
seeds (or the grids) differ -- and the capability declares that, with the
record identity ``(x, seed)``, in a ``campaign`` block. The first final
qualification needed exactly this: a sample larger than one bounded
execution holds, without padding it with repeated identical observations.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import campaign as campaigns
from research_os.portfolio import empirical, sciencechain
from research_os.portfolio.models import (
    EmpiricalConclusion,
    ExperimentRole,
    ExperimentState,
    PrimaryOutcome,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import BudgetScope
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import _context, _idea

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


# ------------------------------------------------------ the science repo --
DRAW_SCRIPT = """\
import json, os, pathlib, random, sys
plan = json.loads(pathlib.Path(sys.argv[sys.argv.index("--plan") + 1]).read_text())
seed = int(os.environ.get("RESEARCH_OS_SEED_0", "0"))
rng = random.Random(seed)
out = pathlib.Path("results/draw.json")
out.parent.mkdir(parents=True, exist_ok=True)
mode = plan.get("mode", "normal")
if mode == "crash":
    raise SystemExit(3)
if mode == "malformed":
    out.write_text("{not json")
    raise SystemExit(0)
records = [
    {"x": x, "y": plan["slope"] * x + rng.uniform(-0.01, 0.01), "seed": seed}
    for x in plan["xs"]
]
out.write_text(json.dumps({"records": records, "summary": {"count": len(records)}}))
"""

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  {project}:
    default_executor: local
    commands:
      draw:
        name: draw
        description: Draw a seeded linear response over a composed grid of x.
        argv: ["python3", "draw.py", "--plan", "{{plan}}"]
        parameters:
          - name: plan
            type: generated
            required: true
            max_bytes: 4096
            input_schema:
              type: object
              additionalProperties: false
              required: ["slope", "xs"]
              properties:
                mode:
                  type: string
                  enum: ["normal", "crash", "malformed"]
                slope: {{type: number, minimum: -10, maximum: 10}}
                xs:
                  type: array
                  minItems: 1
                  maxItems: 5
                  items: {{type: number, minimum: -100, maximum: 100}}
        outputs: ["results/draw.json"]
        timeout_seconds: 120
        checks: []
"""


def manifest(
    *, campaign: str | None = "default", unit_varies: str = "[seeds, plan]"
) -> str:
    block = ""
    if campaign == "default":
        block = f"""\
    campaign:
      max_units: 6
      unit_varies: {unit_varies}
      aggregation:
        - {{observable: points, rule: concatenate, identity: [x, seed]}}
        - {{observable: count, rule: sum}}
"""
    return f"""\
schema: research-os-capabilities-v1
capabilities:
  - id: synthetic.draw
    version: 1
    title: A seeded linear response, one record per x
    command: draw
    parameters:
      - name: plan
        description: the composed grid and slope
    result:
      artifact: results/draw.json
      format: json
      schema:
        type: object
        required: [records, summary]
        properties:
          records:
            type: array
            items:
              type: object
              required: [x, y, seed]
              properties:
                x: {{type: number}}
                y: {{type: number}}
                seed: {{type: integer}}
          summary:
            type: object
            required: [count]
            properties:
              count: {{type: integer}}
    observables:
      - name: points
        kind: records
        path: records
        fields:
          - {{name: x, type: number}}
          - {{name: y, type: number}}
          - {{name: seed, type: integer}}
      - name: count
        kind: scalar
        path: summary.count
        type: integer
    determinism: seeded
    determinism_notes: RESEARCH_OS_SEED_0 seeds the noise
    replication:
      perturbations:
        - kind: seeds
          description: RESEARCH_OS_SEED_0 seeds the noise every y is drawn with
        - kind: parameter
          name: plan
          description: the plan chooses every x and the slope
{block}    resources:
      timeout_seconds: 60
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_manifest(repo: Path, text: str) -> str:
    (repo / "research-capabilities.yaml").write_text(text, encoding="utf-8")
    _git(repo, "add", "research-capabilities.yaml")
    _git(repo, "commit", "-qm", "capabilities")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def science_repo(tmp_path: Path, runtime_xdg: Path, runtime_project: str) -> Path:
    repo = tmp_path / "science"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "draw.py").write_text(DRAW_SCRIPT, encoding="utf-8")
    capsule = repo / ".research"
    capsule.mkdir()
    (capsule / "project.yaml").write_text("id: science\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    commit_manifest(repo, manifest())
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=runtime_project), encoding="utf-8"
    )
    return repo


# ------------------------------------------------------------ the answers --
def analysis(
    *, stopping_rule: str = "fixed_campaign", min_records: int = 10
) -> dict[str, Any]:
    return {
        "analysable": True,
        "estimand": "the slope of the response in x",
        "population": "the synthetic instances the campaign draws",
        "target_claim": "the response rises with x",
        "observables": [
            {
                "name": "points",
                "source": "results/draw.json",
                "kind": "records",
                "path": "records",
                "fields": ["x", "y", "seed"],
            }
        ],
        "reductions": [
            {
                "name": "slope",
                "op": "ols_coefficient",
                "observable": "points",
                "response": "y",
                "terms": ["x"],
                "coefficient": "x",
            }
        ],
        "primary_statistic": "slope",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": 0.1},
        # Ten records and two independent seeds: more than one execution --
        # at most five x -- can hold.
        "support": [
            {
                "observable": "points",
                "min_records": min_records,
                "min_distinct": {"x": 5, "seed": 2},
            }
        ],
        "stopping_rule": stopping_rule,
    }


def plan(
    slope: float = 1.0, *, mode: str = "normal", xs: list[float] | None = None
) -> dict[str, Any]:
    return {
        "mode": mode,
        "slope": slope,
        "xs": xs if xs is not None else [0, 1, 2, 3, 4],
    }


def unit(seed: int, **plan_fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"label": f"seed {seed}", "seeds": [seed]}
    if plan_fields:
        entry["command_parameters"] = {"plan": plan(**plan_fields)}
    return entry


def campaign_design(
    units: list[dict[str, Any]], *, slope: float = 1.0, variation: str = ""
) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "testable": True,
        "command": "draw",
        "command_parameters": {"plan": plan(slope)},
        "seeds": [7],
        "variables": [
            {"name": "x", "role": "manipulated", "levels": [0, 1, 2, 3, 4]},
            {"name": "noise seed", "role": "blocking", "levels": [7, 8]},
        ],
        "sampling": "five x per unit, one independent noise seed per unit",
        "dataset_identity": "synthetic linear response",
        "falsification_criterion": "no positive slope",
        "repetitions": 1,
        "campaign": {
            "units": units,
            "rationale": "one execution holds five x; the design needs two seeds",
        },
    }
    if variation:
        answer["variation_kind"] = variation
        answer["variation_detail"] = f"{variation}: fresh seeds for every unit"
    return answer


def router(
    runtime_db: Database,
    *,
    primary: dict[str, Any],
    the_analysis: dict[str, Any] | None = None,
    replication: dict[str, Any] | None = None,
) -> ScriptedRouter:
    answers = {
        TEMPLATES["analysis_designer"].identity: the_analysis or analysis(),
        TEMPLATES["experiment_designer"].identity: primary,
    }
    if replication is not None:
        answers[TEMPLATES["replication_designer"].identity] = replication
    return ScriptedRouter(
        answers={}, answers_by_prompt=answers, store=RuntimeStore(runtime_db)
    )


def context_for(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    scripted: ScriptedRouter,
) -> Any:
    return _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=_idea(portfolio, runtime_project),
        router=scripted,
        repo=repo,
    )


def advance(context: Any, role: ExperimentRole = ExperimentRole.PRIMARY) -> Any:
    version = context.portfolio.require_version(context.idea_id)
    return empirical.advance(context, version, role=role)


def chains(context: Any) -> tuple[Any, ...]:
    return sciencechain.science_chains(
        context.portfolio,
        context.artifacts,
        idea_id=context.idea_id,
        idea_version=1,
        evidence=context.portfolio.list_evidence(
            idea_id=context.idea_id, idea_version=1
        ),
    )


def outcomes(context: Any, role: ExperimentRole | None = None) -> list[Any]:
    return list(context.portfolio.outcomes(idea_id=context.idea_id, role=role))


def jobs(runtime_db: Database) -> list[Any]:
    with runtime_db.tx() as conn:
        return list(
            conn.execute(
                "select job_id, status, contained from external_jobs"
            ).fetchall()
        )


def measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    units: list[dict[str, Any]] | None = None,
    replication: dict[str, Any] | None = None,
    slope: float = 1.0,
) -> Any:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design(units or [unit(7), unit(8)], slope=slope),
            replication=replication,
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    return context


# ================================================ one design, N executions --
def test_a_campaign_is_frozen_once_and_every_unit_runs_with_its_own_receipt(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    head = _git(science_repo, "rev-parse", "HEAD")
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.state is ExperimentState.INTERPRETED
    assert experiment.conclusion is EmpiricalConclusion.SUPPORTS

    # One campaign plan, its units frozen in its bytes and row by row.
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert chain.is_campaign
    assert chain.plan.payload["schema"] == sciencechain.CAMPAIGN_SCHEMA
    units = portfolio.campaign_units(experiment.plan_digest)
    assert [item.unit_index for item in units] == [0, 1]
    assert [item["spec_digest"] for item in chain.units] == [
        item.spec_digest for item in units
    ]
    assert experiment.spec_digest == campaigns.campaign_spec_digest(
        [item.spec_digest for item in units]
    )
    assert chain.plan.payload["aggregation"]["missing_units"] == "refuse"
    assert chain.plan.payload["resources"]["units"] == 2
    assert [item["configuration"]["seeds"] for item in chain.units] == [[7], [8]]
    assert chain.units[1]["varies_from_first_unit"] == ["seeds"]
    assert chain.code_commit == head
    # The design froze both units too, before any plan bound them.
    assert [item["seeds"] for item in chain.design.payload["campaign"]["units"]] == [
        [7],
        [8],
    ]
    assert chain.contract.payload["stopping_rule"] == "fixed_campaign"

    # Two separate trusted executions: two jobs, two receipts, each naming its
    # unit of this plan and the plan's commit.
    receipts = portfolio.unit_receipts(experiment.experiment_id, attempt=0)
    assert [item.unit_index for item in receipts] == [0, 1]
    assert len({item.job_id for item in receipts}) == 2
    for receipt, frozen in zip(receipts, units, strict=True):
        assert receipt.plan_digest == experiment.plan_digest
        assert receipt.spec_digest == frozen.spec_digest
        document = json.loads(context.artifacts.get_text(receipt.receipt_artifact_id))
        assert document["unit"] == {
            "index": receipt.unit_index,
            "attempt": 0,
            "count": 2,
        }
        assert document["code"]["base_commit"] == head
        assert document["delivered"]["seeds"] == [7 + receipt.unit_index]
    assert all(row["status"] == "COMPLETED" for row in jobs(runtime_db))

    # One outcome, anchored on the last unit and naming both.
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.SUPPORTED
    assert outcome.unit_count == 2
    assert outcome.receipt_id == receipts[-1].receipt_id
    named = portfolio.outcome_units(outcome.outcome_id)
    assert [item.receipt_id for item in named] == [item.receipt_id for item in receipts]
    # The combined result is the deterministic combination of the stored units.
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
    assert combined.ok
    assert combined.sha256 == outcome.result_sha256
    assert len(combined.document["records"]) == 10
    # Only what the frozen analysis reads is combined: it read the records.
    assert set(combined.document) == {"records"}
    assert [item["capability_observable"] for item in chain.aggregation] == ["points"]
    assert outcome.estimate == pytest.approx(1.0, abs=0.02)

    (record,) = chains(context)
    assert record.admissible, record.problems
    # And the canonical checkout is exactly what it was.
    assert _git(science_repo, "status", "--porcelain") == ""
    assert "automation/" not in _git(science_repo, "branch", "--list")


def test_one_execution_could_not_have_held_the_sample(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Not vacuous: the same analysis over one execution is short of its support.

    Frozen as a single execution, since a campaign contract refuses one
    (:func:`test_a_campaign_contract_refuses_a_single_execution`).
    """

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary={
                **{
                    key: value
                    for key, value in campaign_design([]).items()
                    if key != "campaign"
                },
            },
            the_analysis=analysis(stopping_rule="fixed_single_execution"),
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.conclusion is EmpiricalConclusion.INSUFFICIENT
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.INCONCLUSIVE
    assert outcome.unit_count is None


# ================================================ refused before anything runs --
def test_a_campaign_contract_refuses_a_single_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The frozen rule says the sample needs several executions; one is not it."""

    single = {
        key: value for key, value in campaign_design([]).items() if key != "campaign"
    }
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=single),
    )
    step = advance(context)
    assert not step.ok
    assert "stopping rule is fixed_campaign" in step.detail
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert not portfolio.list_experiments(idea_id=context.idea_id)
    assert not jobs(runtime_db)


@pytest.mark.parametrize(
    ("units", "text"),
    [
        ([unit(7), unit(7)], "are the same execution"),
    ],
)
def test_units_that_are_the_same_execution_are_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    units: list[dict[str, Any]],
    text: str,
) -> None:
    """Repeating one observation does not make a sample larger."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design(units)),
    )
    step = advance(context)
    assert not step.ok
    assert text in step.detail
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert outcome.reason == "campaign_not_compiled"
    assert jobs(runtime_db) == []
    assert portfolio.list_experiments(idea_id=context.idea_id) == ()


def test_units_that_differ_only_in_what_the_capability_does_not_attest_are_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A seed the capability does not attest between units is not a new observation."""

    commit_manifest(science_repo, manifest(unit_varies="[plan]"))
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    step = advance(context)
    assert not step.ok
    assert "differ only in seeds" in step.detail
    assert "duplicate observations" in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert jobs(runtime_db) == []


def test_a_capability_without_campaign_support_cannot_be_combined(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    commit_manifest(science_repo, manifest(campaign=None))
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    step = advance(context)
    assert not step.ok
    assert "declares no campaign support" in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert jobs(runtime_db) == []


def test_a_campaign_is_not_run_under_a_single_execution_contract(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design([unit(7), unit(8)]),
            the_analysis=analysis(stopping_rule="fixed_single_execution"),
        ),
    )
    step = advance(context)
    assert not step.ok
    assert "fixed_single_execution" in step.detail
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert jobs(runtime_db) == []


def test_a_campaign_beyond_the_human_set_bounds_is_budget_limited_before_it_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8), unit(9)])),
    )
    context.config = context.config.with_overrides({"max_campaign_seconds": 200})
    step = advance(context)
    assert not step.ok
    assert "bounds.max_campaign_seconds" in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.BUDGET_LIMITED
    assert jobs(runtime_db) == []


def test_an_execution_budget_that_cannot_cover_every_unit_starts_none(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The whole campaign's execution authority is reserved before unit 0 runs."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
        limit_value=1,
        explicit=True,
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.BUDGET_EXHAUSTED
    assert "cannot authorise them all" in step.detail
    assert jobs(runtime_db) == []
    assert any(
        item.state is PrimaryOutcome.BUDGET_LIMITED for item in outcomes(context)
    )


def test_changing_one_unit_is_a_new_campaign_identity(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    first = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (one,) = portfolio.list_experiments(idea_id=first.idea_id)
    second = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        units=[unit(7), unit(9)],
    )
    (two,) = portfolio.list_experiments(idea_id=second.idea_id)
    assert one.plan_digest != two.plan_digest
    assert one.spec_digest != two.spec_digest


# =========================================================== executing --
def test_a_unit_that_crashes_fails_the_attempt_and_nothing_is_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design([unit(7), unit(8, mode="crash")]),
        ),
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.state is ExperimentState.OPERATIONALLY_FAILED
    assert experiment.attempts == 1
    assert not portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)
    assert [item.state for item in outcomes(context)] == [
        PrimaryOutcome.EXECUTION_FAILED
    ]
    # Both units did run, each with a receipt of attempt 0.
    assert [
        item.unit_index for item in portfolio.unit_receipts(experiment.experiment_id)
    ] == [0, 1]


def test_an_invalid_unit_result_makes_the_campaign_invalid_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design([unit(7, mode="malformed"), unit(8), unit(9)]),
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.conclusion is EmpiricalConclusion.INSUFFICIENT
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert outcome.reason == "result_malformed"
    # It stopped at the unit that could not be evidence: nothing more ran.
    assert len(portfolio.unit_receipts(experiment.experiment_id)) == 1
    (record,) = chains(context)
    assert not record.admissible


def test_an_observation_two_units_both_produced_is_refused_when_combined(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Different plans, one seed, overlapping x: the same observation twice."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design(
                [unit(7, xs=[0, 1, 2]), unit(7, xs=[2, 3, 4])],
            ),
            the_analysis=analysis(min_records=6),
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert outcome.reason == "duplicate_observation"


# ============================================================ replication --
def test_a_campaign_replication_is_established_unit_by_unit(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(17), unit(18)], variation="seeds"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    replication = portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
    )
    assert replication.state is ExperimentState.INTERPRETED
    receipts = portfolio.unit_receipts(replication.experiment_id)
    primary = portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    parents = portfolio.unit_receipts(primary.experiment_id)
    # Each replication unit's parent is the primary's execution of that unit,
    # and each ran under its own manifest, frozen before it.
    assert [item.parent_receipt_id for item in receipts] == [
        item.receipt_id for item in parents
    ]
    assert len({item.manifest_artifact_id for item in receipts}) == 2
    for receipt in receipts:
        manifest_doc = json.loads(
            context.artifacts.get_text(receipt.manifest_artifact_id)
        )
        assert manifest_doc["execution"]["unit_index"] == receipt.unit_index
        assert "seeds" in manifest_doc["independence_variables"]
    (assessment,) = portfolio.replication_assessments(
        idea_id=context.idea_id, idea_version=1
    )
    assert assessment.configuration_independent
    assert assessment.perturbation_attested
    assert assessment.agrees
    records = {item.role: item for item in chains(context)}
    assert records[ExperimentRole.REPLICATION].admissible, records[
        ExperimentRole.REPLICATION
    ].problems
    assert records[ExperimentRole.REPLICATION].agrees_with_primary is True
    from research_os.portfolio import provenance

    (provenance_record,) = provenance.replication_provenance(
        portfolio,
        context.artifacts,
        idea_id=context.idea_id,
        idea_version=1,
        evidence=portfolio.list_evidence(idea_id=context.idea_id, idea_version=1),
    )
    assert provenance_record.admissible, provenance_record.problems


def test_a_replication_that_shifts_the_primary_seeds_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Pair by pair each unit differs; together they re-measure the primary.

    The primary ran seeds 7 and 8. Seeds 8 and 9 differ from 7 and 8 at every
    index, and the replication's unit 0 is the primary's unit 1 again: its
    observations would be counted as an independent replication of
    themselves.
    """

    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(8), unit(9)], variation="seeds"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert "unit 0 is the primary's unit 1 run again" in step.detail
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert (
        portfolio.get_experiment(
            idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
        )
        is None
    )


def test_a_replication_unit_that_is_another_primary_unit_with_new_resources_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Resources are not science: a shifted seed under more memory is the same draw."""

    shifted = campaign_design([unit(8), unit(9)], variation="seeds")
    shifted["resources"] = {"memory": "8G"}
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=shifted,
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert (
        "unit 0 differs from the primary's unit 1 only in its resources" in step.detail
    )


def test_a_replication_of_a_campaign_with_another_unit_count_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(17), unit(18), unit(19)], variation="seeds"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert "the primary campaign has 2 units" in step.detail
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID


def test_a_single_replication_of_a_campaign_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    single = {
        key: value
        for key, value in campaign_design([], variation="seeds").items()
        if key != "campaign"
    }
    single["seeds"] = [99]
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=single,
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert "a replication of it is a campaign of 2" in step.detail


# ========================================================== the gate reads --
def test_the_gate_re_verifies_every_unit_result_against_what_is_stored(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A unit result whose stored bytes changed breaks the campaign's chain."""

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (record,) = chains(context)
    assert record.admissible
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    first = portfolio.unit_receipts(experiment.experiment_id)[0]
    stored = portfolio.unit_result(first.receipt_id)
    target = context.artifacts.path_for(stored.result_artifact_id)
    target.chmod(0o644)
    target.write_bytes(target.read_bytes().replace(b'"seed": 7', b'"seed": 70'))
    (record,) = chains(context)
    assert not record.chain_intact
    assert any("unit 0" in item for item in record.problems)


# ======================================================== the database --
#: Every column a receipt row carries that a forged copy keeps unchanged.
RECEIPT_FIELDS = (
    "experiment_id",
    "idea_id",
    "idea_version",
    "role",
    "action_id",
    "run_id",
    "work_id",
    "command",
    "command_digest",
    "base_commit",
    "delivered_digest",
    "inputs_digest",
    "outputs_digest",
    "exit_code",
    "manifest_artifact_id",
    "manifest_digest",
    "parent_receipt_id",
    "receipt_artifact_id",
    "plan_digest",
)


def copy_receipt(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    receipt: Any,
    name: str,
    **changes: Any,
) -> Any:
    """Write a copy of ``receipt`` under a new job, differing only in ``changes``.

    Everything else is the real receipt's, so the database's refusal can only
    be of what changed.
    """

    spec = changes.get("spec_digest", receipt.spec_digest)
    with runtime_db.tx() as conn:
        conn.execute(
            "insert into external_jobs (job_id, project_id, executor, spec_digest, "
            "run_dir, status) values (%s, %s, 'local', %s, '/tmp', 'COMPLETED') "
            "on conflict do nothing",
            (f"JOB-{name}", runtime_project, spec),
        )
    fields = {key: getattr(receipt, key) for key in RECEIPT_FIELDS}
    fields.update(
        spec_digest=receipt.spec_digest,
        unit_index=receipt.unit_index,
        unit_attempt=receipt.unit_attempt,
    )
    fields.update(changes)
    return portfolio.record_execution_receipt(
        receipt_id=f"RCPT-{name}", job_id=f"JOB-{name}", **fields
    )


def test_the_database_refuses_a_campaign_reading_that_omits_a_unit(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A reading of a two-unit campaign over one unit's execution is refused at commit.

    The unit receipt it reads is genuine and read by nothing else, so the only
    thing wrong with the reading is the unit it leaves out.
    """

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    first = portfolio.unit_receipts(experiment.experiment_id)[0]
    lone = copy_receipt(
        portfolio, runtime_db, runtime_project, first, "lone", unit_attempt=5
    )
    reading = {
        "project_id": outcome.project_id,
        "idea_id": outcome.idea_id,
        "idea_version": outcome.idea_version,
        "role": outcome.role,
        "state": PrimaryOutcome.SUPPORTED,
        "reason": "forged",
        "contract_id": outcome.contract_id,
        "experiment_id": outcome.experiment_id,
        "contract_digest": outcome.contract_digest,
        "design_digest": outcome.design_digest,
        "plan_digest": outcome.plan_digest,
        "capability_ref": outcome.capability_ref,
        "capability_digest": outcome.capability_digest,
        "receipt_id": lone.receipt_id,
        "result_sha256": "0" * 64,
        "record_artifact_id": outcome.record_artifact_id,
    }
    with pytest.raises(RuntimeDatabaseError, match="over every unit or not at all"):
        portfolio.record_outcome(
            **reading, unit_count=2, units=[(0, lone.receipt_id, "0" * 64)]
        )
    with pytest.raises(RuntimeDatabaseError, match="over every unit or not at all"):
        portfolio.record_outcome(
            **reading, unit_count=1, units=[(0, lone.receipt_id, "0" * 64)]
        )


def test_the_database_refuses_a_unit_receipt_of_a_specification_not_frozen(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A unit receipt must describe that unit's frozen specification, and name its unit.

    The control is the same copy with the unit's own specification, which the
    database accepts: the refusals are of the specification and the unit alone.
    """

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    first, second = portfolio.unit_receipts(experiment.experiment_id)[:2]
    assert first.spec_digest != second.spec_digest
    with pytest.raises(RuntimeDatabaseError, match="did not freeze"):
        copy_receipt(
            portfolio,
            runtime_db,
            runtime_project,
            first,
            "forged",
            spec_digest="f" * 64,
            unit_attempt=5,
        )
    with pytest.raises(RuntimeDatabaseError, match="did not freeze"):
        copy_receipt(
            portfolio,
            runtime_db,
            runtime_project,
            first,
            "swapped",
            spec_digest=second.spec_digest,
            unit_attempt=5,
        )
    with pytest.raises(RuntimeDatabaseError, match="names no unit"):
        copy_receipt(
            portfolio,
            runtime_db,
            runtime_project,
            first,
            "unnamed",
            unit_index=None,
            unit_attempt=None,
        )
    honest = copy_receipt(
        portfolio, runtime_db, runtime_project, first, "honest", unit_attempt=5
    )
    assert (honest.unit_index, honest.unit_attempt) == (0, 5)
    assert honest.spec_digest == first.spec_digest


def test_a_single_execution_is_untouched_by_campaigns(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A design with no campaign still freezes an ordinary plan with no units."""

    single = {
        key: value for key, value in campaign_design([]).items() if key != "campaign"
    }
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(
            runtime_db,
            primary=single,
            the_analysis=analysis(
                stopping_rule="fixed_single_execution", min_records=5
            ),
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert not chain.is_campaign
    assert chain.plan.payload["schema"] == sciencechain.PLAN_SCHEMA
    assert portfolio.campaign_units(experiment.plan_digest) == ()
    receipt = portfolio.receipt_for_job(experiment.job_id)
    assert receipt.unit_index is None
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.unit_count is None
    assert portfolio.outcome_units(outcome.outcome_id) == ()


def test_a_campaign_longer_than_its_capability_allows_is_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Seven units against a declaration of at most six -- whatever the portfolio allows."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(seed) for seed in range(7)])),
    )
    context.config = context.config.with_overrides(
        {"max_campaign_units": 10, "max_campaign_seconds": 24 * 3600}
    )
    step = advance(context)
    assert not step.ok
    assert "allows at most 6" in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert jobs(runtime_db) == []


# ======================================================= recovery --
class _FailingOnce:
    """A local executor whose ``n``-th submission raises, as a lost host would."""

    def __init__(self, inner: Any, *, n: int) -> None:
        self._inner = inner
        self._n = n
        self.calls = 0

    def submit(self, spec: Any, **kwargs: Any) -> Any:
        from research_os.runtime.executors import ExecutorError

        self.calls += 1
        if self.calls == self._n:
            raise ExecutorError("the host went away (injected)")
        return self._inner.submit(spec, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_unit_whose_result_was_not_stored_before_a_crash_is_stored_on_resume(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process stopped between a unit's receipt and its stored result.

    On resume the unit is not "done": its bytes are still where the runner
    hashed them, they are stored exactly as they would have been, and the
    campaign continues. Skipping it would let the next unit's checkout
    delete them and turn a crash into a permanent INVALID_EVIDENCE reading.
    """

    real = empirical._store_unit_result
    crashed: list[int] = []

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        if not crashed:
            crashed.append(kwargs["index"])
            raise RuntimeError("the process stopped (injected)")
        return real(*args, **kwargs)

    monkeypatch.setattr(empirical, "_store_unit_result", crash_once)
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    with pytest.raises(RuntimeError, match="injected"):
        advance(context)
    assert crashed == [0]
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    (first,) = portfolio.unit_receipts(experiment.experiment_id)
    assert portfolio.unit_result(first.receipt_id) is None

    step = advance(context)
    assert step.ok, step.detail
    receipts = portfolio.unit_receipts(experiment.experiment_id)
    assert [item.unit_index for item in receipts] == [0, 1]
    assert all(portfolio.unit_result(item.receipt_id) for item in receipts)
    (reading,) = [item for item in outcomes(context) if item.receipt_id]
    assert reading.state is PrimaryOutcome.SUPPORTED
    assert len(portfolio.outcome_units(reading.outcome_id)) == 2


def test_a_retried_unit_never_recovers_an_earlier_attempts_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The unit's specification digest is the same in every attempt; its job is not.

    Attempt 0 fails at unit 1. Attempt 1's unit 0 then fails before it
    creates a job, and the ledger asks the reconciler what happened. Attempt
    0's completed execution of unit 0 has the same specification digest, and
    it is not this attempt's: the unit runs again, and the campaign is read
    over attempt 1 alone.
    """

    from research_os.runtime.idempotency import idempotency_key

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    flaky = _FailingOnce(context.executors["local"], n=2)
    context.executors["local"] = flaky
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.attempts == 1
    (earlier,) = portfolio.unit_receipts(experiment.experiment_id, attempt=0)

    def lost() -> dict[str, Any]:
        raise RuntimeError("the worker died before it created a job (injected)")

    key = idempotency_key(
        "portfolio.campaign.unit",
        experiment.experiment_id,
        earlier.spec_digest,
        1,
        0,
    )
    with pytest.raises(RuntimeError, match="injected"):
        context.ledger.run(
            key=key,
            kind="portfolio.campaign.unit",
            run_id=context.run_id,
            request={"experiment_id": experiment.experiment_id, "unit_index": 0},
            perform=lost,
        )

    step = advance(context)
    assert step.ok, step.detail
    current = portfolio.unit_receipts(experiment.experiment_id, attempt=1)
    assert [item.unit_index for item in current] == [0, 1]
    assert current[0].job_id != earlier.job_id
    (reading,) = [
        item
        for item in outcomes(context)
        if item.state
        in {
            PrimaryOutcome.SUPPORTED,
            PrimaryOutcome.REFUTED,
            PrimaryOutcome.INCONCLUSIVE,
        }
    ]
    assert reading.state is PrimaryOutcome.SUPPORTED
    assert {
        item.receipt_id for item in portfolio.outcome_units(reading.outcome_id)
    } == {item.receipt_id for item in current}


def test_a_job_found_after_a_crash_without_its_receipt_is_not_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """This attempt's job, completed, and no receipt: the runner never recorded it.

    The reconciler finds it -- it is this unit's, submitted in this attempt
    -- and it is still not a unit execution anything may read. The attempt
    ends as an operational failure, and the next one runs every unit.
    """

    from research_os.runtime.idempotency import idempotency_key
    from research_os.runtime.ids import new_external_job_id
    from research_os.runtime.models import ExternalJobStatus

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=campaign_design([unit(7), unit(8)])),
    )
    context.executors["local"] = _FailingOnce(context.executors["local"], n=2)
    assert not advance(context).ok
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    (earlier,) = portfolio.unit_receipts(experiment.experiment_id, attempt=0)

    def unrecorded() -> dict[str, Any]:
        job = context.runtime.create_external_job(
            job_id=new_external_job_id(),
            project_id=runtime_project,
            executor="local",
            spec_digest=earlier.spec_digest,
            run_dir=str(tmp_path / "lost-run"),
        )
        context.runtime.update_external_job(
            job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0
        )
        raise RuntimeError("the worker died before the receipt (injected)")

    with pytest.raises(RuntimeError, match="injected"):
        context.ledger.run(
            key=idempotency_key(
                "portfolio.campaign.unit",
                experiment.experiment_id,
                earlier.spec_digest,
                1,
                0,
            ),
            kind="portfolio.campaign.unit",
            run_id=context.run_id,
            request={"experiment_id": experiment.experiment_id, "unit_index": 0},
            perform=unrecorded,
        )

    step = advance(context)
    assert not step.ok
    assert "without its trusted receipt" in step.detail
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    assert not portfolio.unit_receipts(experiment.experiment_id, attempt=1)
    assert portfolio.require_experiment(experiment.experiment_id).attempts == 2

    step = advance(context)
    assert step.ok, step.detail
    assert [
        item.unit_index
        for item in portfolio.unit_receipts(experiment.experiment_id, attempt=2)
    ] == [0, 1]


def test_records_at_the_top_level_are_combined_there() -> None:
    """A capability whose result *is* the list of records: the combination is the list."""

    rule = {
        "analysis_observable": "points",
        "capability_observable": "points",
        "kind": "records",
        "path": "",
        "rule": "concatenate",
        "identity": ["x", "seed"],
    }
    combined = campaigns.combine(
        [rule],
        [
            (0, [{"x": 0, "seed": 7}, {"x": 1, "seed": 7}]),
            (1, [{"x": 0, "seed": 8}]),
        ],
    )
    assert combined.ok, combined.detail
    assert combined.document == [
        {"x": 0, "seed": 7},
        {"x": 1, "seed": 7},
        {"x": 0, "seed": 8},
    ]
    scalar = {**rule, "capability_observable": "n", "kind": "scalar", "rule": "sum"}
    beside = campaigns.combine(
        [rule, {**scalar, "path": "n"}], [(0, [{"x": 0, "seed": 7}])]
    )
    assert not beside.ok


def test_a_combined_total_that_is_not_finite_is_refused() -> None:
    rule = {
        "analysis_observable": "total",
        "capability_observable": "total",
        "kind": "scalar",
        "path": "total",
        "rule": "sum",
        "identity": [],
    }
    combined = campaigns.combine(
        [rule], [(0, {"total": 1.5e308}), (1, {"total": 1.5e308})]
    )
    assert not combined.ok
    assert "not a finite number" in combined.detail


def test_the_designer_is_told_what_the_human_bounds_permit() -> None:
    """Six declared units, 1800 s each, under a 7200 s bound: four fit, and it says so."""

    from types import SimpleNamespace

    capability = SimpleNamespace(
        id="cg.cells",
        ref="cg.cells@1",
        command="cells",
        campaign=SimpleNamespace(max_units=6),
    )
    loaded = SimpleNamespace(manifest=SimpleNamespace(capabilities=(capability,)))
    bounds = SimpleNamespace(
        max_experiment_seconds=1800, max_campaign_units=6, max_campaign_seconds=7200
    )
    (line,) = empirical.campaign_bound_lines(
        loaded, {"cells": SimpleNamespace(timeout_seconds=5400)}, bounds
    )
    assert "at most 4 unit(s)" in line
    assert "up to 1800s" in line
    roomy = SimpleNamespace(
        max_experiment_seconds=1800, max_campaign_units=6, max_campaign_seconds=10800
    )
    (line,) = empirical.campaign_bound_lines(
        loaded, {"cells": SimpleNamespace(timeout_seconds=1800)}, roomy
    )
    assert "at most 6 unit(s)" in line
    assert empirical.campaign_bound_lines(loaded, {}, bounds) == []


def test_every_campaign_and_allocation_mutant_still_applies() -> None:
    """The harness in ``tests/campaign_allocation_mutations.py`` cannot rot silently."""

    import ast

    from tests.campaign_allocation_mutations import MUTANTS

    root = Path(__file__).resolve().parents[1]
    ids = [item.id for item in MUTANTS]
    assert len(ids) == len(set(ids))
    for mutant in MUTANTS:
        source = (root / mutant.file).read_text(encoding="utf-8")
        assert source.count(mutant.find) == 1, mutant.id
        for test in mutant.tests:
            path, _, name = test.partition("::")
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
            }
            assert name in names, (mutant.id, test)
