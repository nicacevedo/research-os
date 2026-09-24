"""The scientific contract: analysis frozen before design, and immutable after.

Three groups, each holding a property the discovery report showed was absent:

- **the analysis language** (no database): a closed set of reductions that
  ordinary Python evaluates, including the regression coefficient §AB.3 needed
  and could not express, and the support requirements that make a design that
  determines its own statistic read INSUFFICIENT rather than SUPPORTS;
- **immutability** (the database): the trigger that refuses an edit to a
  frozen contract or to what an experiment preregistered -- each asserted by
  attempting the edit, so removing the trigger turns the test red;
- **the production route** (a real subprocess): the analysis designer is asked
  first, the experiment designer is not shown its thresholds, a regression
  question is settled by arithmetic, a degenerate grid is refused by its own
  contract, a missing observable is INSUFFICIENT, a second preregistration of
  the same question is refused, and an implementation repair re-executes the
  same frozen science and cannot change it. (Replication lineage and the rule
  that no later analysis is read as the primary are held in
  ``test_portfolio_contract_lineage.py``.)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import analysis as engine
from research_os.portfolio import empirical, scicontract
from research_os.portfolio.config import load_config
from research_os.portfolio.contracts import AnalysisSpec, ContractError
from research_os.portfolio.models import (
    AdjudicationType,
    ContractState,
    EmpiricalConclusion,
    EvidenceStrength,
    ExperimentRole,
    ExperimentState,
    IdeaOrigin,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import DuplicateContractError, PortfolioStore
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.executors import LocalExecutor
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import (
    pg_dsn,
    runtime_db,
    runtime_project,
    runtime_xdg,
    throwaway_dsn,
)

__all__ = [
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "throwaway_dsn",
]


# ================================================================ language --
def _spec(**overrides: Any) -> AnalysisSpec:
    payload: dict[str, Any] = {
        "analysable": True,
        "estimand": "the size-by-difficulty interaction in iteration count",
        "observables": [
            {
                "name": "cells",
                "source": "results/grid.json",
                "kind": "records",
                "path": "cells",
                "fields": ["size", "difficulty", "iterations"],
            }
        ],
        "reductions": [
            {
                "name": "interaction",
                "op": "ols_coefficient",
                "observable": "cells",
                "response": "iterations",
                "terms": ["size", "difficulty", "size:difficulty"],
                "coefficient": "size:difficulty",
            }
        ],
        "primary_statistic": "interaction",
        "success": {"comparator": ">", "threshold": 0.25},
        "failure": {"comparator": "<", "threshold": 0.1},
        "support": [
            {
                "observable": "cells",
                "min_records": 4,
                "min_distinct": {"size": 2, "difficulty": 2},
            }
        ],
    }
    payload.update(overrides)
    spec = AnalysisSpec.model_validate(payload)
    spec.check()
    return spec


def _cells(
    sizes: list[float], difficulties: list[float], *, slope: float = 0.5
) -> dict:
    return {
        "cells": [
            {
                "size": size,
                "difficulty": difficulty,
                "iterations": 10
                + 2 * size
                + 3 * difficulty
                + slope * size * difficulty,
            }
            for size in sizes
            for difficulty in difficulties
        ]
    }


def test_a_regression_coefficient_is_computed_by_arithmetic() -> None:
    """§AB.3's question, which no declared analysis could express.

    ``iteration_count ~ size + difficulty + size:difficulty`` and read the
    interaction. Exact data, so the coefficient is recovered exactly.
    """

    result = engine.evaluate(_spec(), {"results/grid.json": _cells([1, 2, 4], [1, 3])})
    assert result.conclusion is EmpiricalConclusion.SUPPORTS, result.summary
    assert result.statistic == pytest.approx(0.5)


def test_a_degenerate_grid_is_insufficient_not_supportive() -> None:
    """The co-design defence, checked against what was measured.

    One instance size: the interaction is unidentified -- and even where a
    statistic *could* be computed from a collapsed design, the contract said
    what the data must exhibit, and it does not. INSUFFICIENT, whatever
    number the grid would have produced.
    """

    result = engine.evaluate(_spec(), {"results/grid.json": _cells([3], [1, 2, 3, 4])})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "distinct value" in result.summary
    assert result.support and result.support[0]["met"] is False


def test_the_support_requirement_is_what_catches_it_not_the_arithmetic() -> None:
    """The control: remove the support rule and the collapse is caught later.

    Without the requirement the regression is singular and still refused --
    but a statistic that *is* defined on a collapsed design (a mean over one
    level) would pass. The requirement is the general defence.
    """

    collapsed = _cells([3], [1, 2, 3, 4, 5, 6])
    spec = _spec(support=[])
    result = engine.evaluate(spec, {"results/grid.json": collapsed})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "does not identify" in result.summary

    mean_spec = _spec(
        support=[],
        reductions=[
            {"name": "m", "op": "mean", "observable": "cells", "field": "iterations"}
        ],
        primary_statistic="m",
        success={"comparator": ">", "threshold": 0.0},
        failure={"comparator": "<", "threshold": -1.0},
    )
    assert engine.evaluate(mean_spec, {"results/grid.json": collapsed}).conclusion is (
        EmpiricalConclusion.SUPPORTS
    ), "a mean over one level is defined, which is why support has to be declared"
    guarded = _spec(
        reductions=mean_spec.model_dump(mode="json")["reductions"],
        primary_statistic="m",
        success={"comparator": ">", "threshold": 0.0},
        failure={"comparator": "<", "threshold": -1.0},
    )
    assert engine.evaluate(guarded, {"results/grid.json": collapsed}).conclusion is (
        EmpiricalConclusion.INSUFFICIENT
    )


def test_a_missing_observable_is_insufficient_and_says_which() -> None:
    result = engine.evaluate(_spec(), {})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "results/grid.json" in result.summary


def test_a_record_missing_a_field_is_insufficient_unless_exclusion_was_fixed() -> None:
    document = _cells([1, 2, 4], [1, 3])
    del document["cells"][0]["iterations"]
    strict = engine.evaluate(_spec(), {"results/grid.json": document})
    assert strict.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "lack a required field" in strict.summary

    lenient_observable = {
        **_spec().model_dump(mode="json")["observables"][0],
        "incomplete_records": "exclude",
    }
    lenient = engine.evaluate(
        _spec(observables=[lenient_observable]), {"results/grid.json": document}
    )
    assert lenient.conclusion is EmpiricalConclusion.SUPPORTS
    assert lenient.records["cells"]["incomplete"] == 1


def test_a_value_is_the_old_rule_exactly() -> None:
    spec = AnalysisSpec.model_validate(
        {
            "analysable": True,
            "estimand": "overlap",
            "observables": [
                {"name": "m", "source": "r.json", "kind": "scalar", "path": "s.o"}
            ],
            "reductions": [{"name": "v", "op": "value", "observable": "m"}],
            "primary_statistic": "v",
            "success": {"comparator": "<=", "threshold": 0.5},
            "failure": {"comparator": ">", "threshold": 0.5},
        }
    )
    spec.check()
    read = {"r.json": {"s": {"o": 0.4}}}
    assert engine.evaluate(spec, read).conclusion is EmpiricalConclusion.SUPPORTS
    read = {"r.json": {"s": {"o": 0.8}}}
    assert engine.evaluate(spec, read).conclusion is EmpiricalConclusion.CONTRADICTS
    assert "s.o = 0.8 in r.json" in engine.evaluate(spec, read).summary


def test_a_bootstrap_interval_must_clear_the_threshold_whole() -> None:
    """With an uncertainty, SUPPORTS means the whole interval, and it is seeded."""

    noisy = {
        "cells": [
            {"size": 1, "difficulty": 1, "iterations": value}
            for value in (1.0, 1.4, 0.2, 2.2, 0.9, 1.1, 1.8, 0.4, 1.3, 0.7)
        ]
    }
    spec = _spec(
        reductions=[
            {"name": "m", "op": "mean", "observable": "cells", "field": "iterations"}
        ],
        primary_statistic="m",
        uncertainty={"resamples": 400, "seed": 7},
        success={"comparator": ">", "threshold": 1.0},
        failure={"comparator": "<", "threshold": 0.5},
        support=[{"observable": "cells", "min_records": 5}],
    )
    first = engine.evaluate(spec, {"results/grid.json": noisy})
    second = engine.evaluate(spec, {"results/grid.json": noisy})
    assert first.interval == second.interval, "a seeded bootstrap is deterministic"
    assert first.statistic == pytest.approx(1.1)
    # The point estimate clears 1.0, the interval does not: not SUPPORTS.
    assert first.conclusion is EmpiricalConclusion.INCONCLUSIVE
    assert first.interval is not None and first.interval[0] < 1.0 < first.interval[1]


def test_a_zero_denominator_is_insufficient_rather_than_infinite() -> None:
    spec = _spec(
        reductions=[
            {"name": "a", "op": "sum", "observable": "cells", "field": "iterations"},
            {
                "name": "b",
                "op": "count",
                "observable": "cells",
                "where": [{"field": "size", "comparator": ">", "value": 100}],
            },
            {"name": "r", "op": "ratio", "of": ["a", "b"]},
        ],
        primary_statistic="r",
    )
    result = engine.evaluate(spec, {"results/grid.json": _cells([1, 2], [1, 2])})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "denominator is zero" in result.summary


def test_a_nan_in_the_output_is_not_a_number() -> None:
    parsed = engine.parse_document('{"cells": [{"size": NaN}]}', name="grid.json")
    assert isinstance(parsed, engine.Unavailable)
    result = engine.evaluate(_spec(), {"results/grid.json": parsed})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT


def test_a_csv_is_a_list_of_records() -> None:
    rows = engine.parse_document(
        "size,difficulty,iterations\n1,1,15.5\n1,3,22.5\n2,1,18\n2,3,26\n4,1,23\n4,3,33\n",
        name="grid.csv",
    )
    spec = _spec(
        observables=[
            {
                "name": "cells",
                "source": "results/grid.csv",
                "kind": "records",
                "fields": ["size", "difficulty", "iterations"],
            }
        ]
    )
    result = engine.evaluate(spec, {"results/grid.csv": rows})
    assert result.conclusion is EmpiricalConclusion.SUPPORTS
    assert result.statistic == pytest.approx(0.5)


def test_an_incoherent_analysis_is_refused_before_it_is_frozen() -> None:
    with pytest.raises(ContractError, match="EARLIER|earlier"):
        _spec(
            reductions=[{"name": "r", "op": "ratio", "of": ["a", "b"]}],
            primary_statistic="r",
        )
    with pytest.raises(ContractError, match="exact equality"):
        _spec(
            uncertainty={"resamples": 200},
            success={"comparator": "==", "threshold": 1.0},
        )
    with pytest.raises(ContractError, match="both predicates"):
        _spec(failure=None)
    with pytest.raises(ContractError, match="must say why"):
        AnalysisSpec(analysable=False).check()


# ================================================================ project --
GRID_SCRIPT = """\
import json, pathlib, sys
plan = json.loads(pathlib.Path(sys.argv[sys.argv.index("--plan") + 1]).read_text())
out = pathlib.Path("results/grid.json")
out.parent.mkdir(parents=True, exist_ok=True)
cells = [
    {"size": s, "difficulty": d, "iterations": 10 + 2*s + 3*d + 0.5*s*d}
    for s in plan["sizes"] for d in plan["difficulties"]
]
out.write_text(json.dumps({"cells": cells}))
print(len(cells), "cells")
"""

SLOW_SCRIPT = """\
import json, pathlib, sys, time
plan = json.loads(pathlib.Path(sys.argv[sys.argv.index("--plan") + 1]).read_text())
time.sleep(float(plan.get("sleep", 0)))
out = pathlib.Path("results/grid.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"cells": [
    {"size": s, "difficulty": d, "iterations": 10 + 2*s + 3*d + 0.5*s*d}
    for s in plan["sizes"] for d in plan["difficulties"]]}))
"""

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  {project}:
    default_executor: local
    commands:
      grid:
        name: grid
        description: Run an (n, p) x difficulty factorial and write every cell.
        argv: ["python3", "grid.py", "--plan", "{{plan}}"]
        parameters:
          - name: plan
            type: generated
            required: true
            max_bytes: 4096
            input_schema:
              type: object
              additionalProperties: false
              required: ["sizes", "difficulties"]
              properties:
                sizes: {{type: array, minItems: 1, maxItems: 8,
                         items: {{type: number, minimum: 0, maximum: 100}}}}
                difficulties: {{type: array, minItems: 1, maxItems: 8,
                                items: {{type: number, minimum: 0, maximum: 100}}}}
                sleep: {{type: number, minimum: 0, maximum: 30}}
        outputs: ["results/grid.json"]
        timeout_seconds: {timeout}
        checks: []
"""


def _project(
    tmp: Path, runtime_xdg: Path, project: str, *, script: str, timeout: int = 120
) -> Path:
    repo = tmp / "grid-project"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "grid.py").write_text(script, encoding="utf-8")
    (repo / ".research").mkdir()
    (repo / ".research" / "project.yaml").write_text("id: grid\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=project, timeout=timeout), encoding="utf-8"
    )
    return repo


@pytest.fixture
def grid_repo(tmp_path: Path, runtime_xdg: Path, runtime_project: str) -> Path:
    return _project(tmp_path, runtime_xdg, runtime_project, script=GRID_SCRIPT)


def _analysis_answer(**overrides: Any) -> dict[str, Any]:
    return {**_spec().model_dump(mode="json"), **overrides}


def _design_answer(sizes: list[float], difficulties: list[float], **extra: Any) -> dict:
    plan: dict[str, Any] = {"sizes": sizes, "difficulties": difficulties, **extra}
    return {
        "testable": True,
        "command": "grid",
        "command_parameters": {"plan": plan},
        "variables": [
            {"name": "size", "role": "manipulated", "levels": sizes},
            {"name": "difficulty", "role": "manipulated", "levels": difficulties},
        ],
        "sampling": "a full factorial over the listed levels, one run per cell",
        "falsification_criterion": "no size-by-difficulty interaction",
        "dataset_identity": "synthetic factorial",
    }


def _router(runtime_db: Database, *, analysis: dict, design: dict) -> ScriptedRouter:
    return ScriptedRouter(
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: analysis,
            TEMPLATES["experiment_designer"].identity: design,
        },
        store=RuntimeStore(runtime_db),
    )


def _idea(store: PortfolioStore, project: str) -> str:
    idea, _ = store.create_idea(
        project_id=project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(adjudication_types=[AdjudicationType.EMPIRICAL]),
        origin_role="blind_explorer",
    )
    return idea.idea_id


def _context(
    store: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    idea: str,
    router: Any,
    repo: Path,
) -> Any:
    from research_os.portfolio.runner import TrackContext

    runtime = RuntimeStore(runtime_db)
    run = runtime.create_run(project_id=project, objective="contract-test")
    return TrackContext(
        config=load_config(),
        portfolio=store,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp / "artifacts", store=runtime),
        project_id=project,
        idea_id=idea,
        run_id=run.run_id,
        repo_path=repo,
        executors={"local": LocalExecutor()},
        ledger=InvocationLedger(runtime_db),
        budgets=BudgetLedger(runtime_db),
    )


def _advance(context: Any, role: ExperimentRole = ExperimentRole.PRIMARY) -> Any:
    return empirical.advance(
        context, context.portfolio.require_version(context.idea_id), role=role
    )


# ======================================================== production route --
def test_the_analysis_is_frozen_first_and_the_designer_never_sees_its_thresholds(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The co-design fix, as a data boundary rather than a sentence in a prompt.

    Distinctive thresholds, so their absence from the designer's prompt is a
    statement about the prompt and not about a coincidence of digits.
    """

    idea = _idea(portfolio, runtime_project)
    router = _router(
        runtime_db,
        analysis=_analysis_answer(
            success={"comparator": ">", "threshold": 0.4375},
            failure={"comparator": "<", "threshold": 0.0625},
        ),
        design=_design_answer([1, 2, 4], [1, 3]),
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea, router, grid_repo
    )

    step = _advance(context)
    assert step.ok, step.detail

    order = [request.prompt_version for request in router.requests]
    assert order.index(TEMPLATES["analysis_designer"].identity) < order.index(
        TEMPLATES["experiment_designer"].identity
    ), "the analysis must be frozen before any design is asked for"
    (designer,) = router.requests_for_prompt(TEMPLATES["experiment_designer"].identity)
    assert "0.4375" not in designer.prompt and "0.0625" not in designer.prompt
    assert "deliberately not shown" in designer.prompt
    assert "size:difficulty" in designer.prompt, "it is shown what it must produce"
    # The analysis designer's input boundary is its template: the idea and
    # the catalogue of what can be observed. There is no block a design, a
    # grid or a result could arrive through.
    assert [name for name, _fence in TEMPLATES["analysis_designer"].blocks] == [
        "idea",
        "observable_catalogue",
    ]
    assert router.requests_for_prompt(TEMPLATES["analysis_designer"].identity)

    experiment = portfolio.get_experiment(idea_id=idea, idea_version=1)
    assert experiment is not None and experiment.state is ExperimentState.INTERPRETED
    contract = portfolio.require_contract(experiment.contract_id)
    assert contract.state is ContractState.FROZEN
    assert contract.analysis_prompt == TEMPLATES["analysis_designer"].identity
    assert contract.design_prompt == TEMPLATES["experiment_designer"].identity
    assert experiment.conclusion is EmpiricalConclusion.SUPPORTS, experiment.detail


def test_a_regression_question_is_settled_on_the_production_path(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """§AB.3, closed: the missing *analysis* is no longer human-owned."""

    idea = _idea(portfolio, runtime_project)
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3]),
        ),
        grid_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.SUPPORTS, step.detail
    (row,) = portfolio.list_evidence(idea_id=idea, idea_version=1)
    assert row.strength is EvidenceStrength.SUPPORTS
    document = json.loads(context.artifacts.get_text(row.artifact_id))
    assert document["analysis_result"]["statistics"]["interaction"] == pytest.approx(
        0.5
    )
    assert document["contract"]["contract_id"] == step.experiment.contract_id
    assert "frozen before the design existed" in row.summary


def test_a_design_that_collapses_its_own_statistic_is_insufficient(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The §AB.5 hole, on the production path: the grid cannot choose the answer."""

    idea = _idea(portfolio, runtime_project)
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([3], [1, 2, 3, 4]),
        ),
        grid_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT
    (row,) = portfolio.list_evidence(idea_id=idea, idea_version=1)
    assert row.strength is EvidenceStrength.INCONCLUSIVE
    assert "distinct value" in row.summary


def test_a_missing_observable_on_the_production_path_is_insufficient(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """A field the command never writes: INSUFFICIENT, not an invented statistic."""

    idea = _idea(portfolio, runtime_project)
    analysis = _analysis_answer()
    analysis["observables"][0]["fields"] = [
        "size",
        "difficulty",
        "iterations",
        "wall_clock",
    ]
    analysis["reductions"] = [
        {"name": "t", "op": "median", "observable": "cells", "field": "wall_clock"}
    ]
    analysis["primary_statistic"] = "t"
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db, analysis=analysis, design=_design_answer([1, 2, 4], [1, 3])
        ),
        grid_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "lack a required field" in step.detail


# ============================================================ immutability --
def _frozen(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    repo: Path,
    *,
    run: bool = True,
) -> tuple[Any, Any]:
    idea = _idea(portfolio, project)
    context = _context(
        portfolio,
        runtime_db,
        tmp,
        project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3]),
        ),
        repo,
    )
    step = (
        _advance(context)
        if run
        else empirical.design(context, portfolio.require_version(idea))
    )
    assert step.ok, step.detail
    return context, portfolio.require_contract(step.experiment.contract_id)


def test_the_database_refuses_an_edit_to_a_frozen_contract(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Mutation-style: every one of these edits succeeds without the trigger."""

    _, contract = _frozen(portfolio, runtime_db, tmp_path, runtime_project, grid_repo)
    edits = [
        ("analysis_digest", "panalysis-v1:" + "0" * 64),
        ("contract_digest", "pcontract-v1:" + "0" * 64),
        ("design_digest", "pdesign-v1:" + "0" * 64),
        ("hypothesis_digest", "pidea-content-v1:" + "0" * 64),
        ("state", "ANALYSIS_FROZEN"),
    ]
    for column, value in edits:
        with (
            pytest.raises(RuntimeDatabaseError, match="frozen|cannot return"),
            runtime_db.tx() as conn,
        ):
            conn.execute(
                f"update scientific_contracts set {column} = %s where contract_id = %s",
                (value, contract.contract_id),
            )
    with (
        pytest.raises(RuntimeDatabaseError, match="never deleted"),
        runtime_db.tx() as conn,
    ):
        conn.execute(
            "delete from scientific_contracts where contract_id = %s",
            (contract.contract_id,),
        )
    assert portfolio.require_contract(contract.contract_id) == contract


def test_a_tampered_contract_is_refused_at_interpretation_when_the_trigger_is_bypassed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The second layer: re-hashing, which does not trust the row at all."""

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    other = context.artifacts.put_text(
        json.dumps(
            {"analysis": _analysis_answer(success={"comparator": ">", "threshold": -9})}
        ),
        media_type="application/json",
        role="forged",
        producer="test",
    )
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update scientific_contracts set contract_artifact_id = %s where contract_id = %s",
            (other.artifact_id, contract.contract_id),
        )
    with pytest.raises(scicontract.ContractIntegrityError):
        scicontract.verify(
            context.artifacts, portfolio.require_contract(contract.contract_id)
        )


def test_a_second_preregistered_contract_for_the_same_question_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """A threshold change after the result cannot masquerade as a preregistration."""

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    version = portfolio.require_version(contract.idea_id)
    loosened = AnalysisSpec.model_validate(
        _analysis_answer(success={"comparator": ">", "threshold": 0.0})
    )
    with pytest.raises(DuplicateContractError):
        empirical._freeze_analysis(
            context,
            version,
            role=ExperimentRole.PRIMARY,
            spec=loosened,
            provenance={},
            analysis_prompt="test",
            analysis_call_id=None,
        )


# ========================================================== implementation --
def test_an_implementation_repair_re_executes_the_same_frozen_science(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
    runtime_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout under a tighter limit than now permitted is repaired, once.

    The contract, its digest and the analysis are unchanged; the execution
    specification differs only in its time limit. That is the whole of what
    a repair may do.
    """

    repo = _project(
        tmp_path, runtime_xdg, runtime_project, script=SLOW_SCRIPT, timeout=60
    )
    idea = _idea(portfolio, runtime_project)
    tight = load_config().model_copy(
        update={
            "bounds": load_config().bounds.model_copy(
                update={"max_experiment_seconds": 1}
            )
        }
    )
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3], sleep=2),
        ),
        repo,
    )
    context.config = tight
    first = _advance(context)
    assert not first.ok
    failed = portfolio.require_experiment(first.experiment.experiment_id)
    assert failed.state is ExperimentState.OPERATIONALLY_FAILED
    assert "timed out" in (failed.detail or "")

    context.config = load_config()  # a person raised the ceiling
    second = _advance(context)
    assert second.ok, second.detail
    assert second.conclusion is EmpiricalConclusion.SUPPORTS
    repaired = second.experiment
    assert repaired.experiment_id != failed.experiment_id
    assert repaired.contract_id == failed.contract_id
    assert repaired.spec_digest != failed.spec_digest
    assert portfolio.require_experiment(failed.experiment_id).state is (
        ExperimentState.SUPERSEDED
    )
    prereg = json.loads(
        context.artifacts.get_text(repaired.preregistration_artifact_id)
    )
    assert prereg["repair_of"] == failed.experiment_id
    assert (
        prereg["contract_digest"]
        == portfolio.require_contract(repaired.contract_id).contract_digest
    )


def test_a_repair_cannot_change_what_is_measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Mutation-style: a specification whose argv moved is refused under the contract."""

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    verified = scicontract.verify(context.artifacts, contract)
    experiment = portfolio.get_experiment(idea_id=contract.idea_id, idea_version=1)
    assert experiment is not None
    spec, _rule = empirical._preregistered(context, experiment)
    empirical._assert_realises(verified, replace(spec, timeout_seconds=7))
    for moved in (
        replace(spec, argv=(*spec.argv, "--sizes", "3")),
        replace(spec, seeds=(1, 2, 3)),
        replace(spec, inputs=(("x.json", "0" * 64),)),
        replace(spec, outputs=("elsewhere.json",)),
    ):
        with pytest.raises(empirical.EmpiricalError, match="frozen design"):
            empirical._assert_realises(verified, moved)


def test_a_repair_takes_no_scientific_argument() -> None:
    """The signature is part of the guarantee: there is nowhere to put a grid."""

    import inspect

    parameters = set(inspect.signature(empirical.repair_implementation).parameters)
    assert parameters == {"context", "experiment", "timeout_seconds", "resources"}
    assert scicontract.IMPLEMENTATION_FIELDS == frozenset(
        {"timeout_seconds", "resources"}
    )


def test_the_contract_digest_names_no_provider_or_model(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Provider neutrality of the science: the same content is the same contract."""

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    document = json.loads(context.artifacts.get_text(contract.contract_artifact_id))
    payload = scicontract.contract_payload(
        idea_id=contract.idea_id,
        idea_version=contract.idea_version,
        hypothesis_digest=contract.hypothesis_digest,
        role=str(contract.role),
        kind=contract.kind,
        analysis_digest=contract.analysis_digest,
        design_digest=contract.design_digest or "",
    )
    rendered = json.dumps(payload).lower()
    for word in ("scripted", "claude", "anthropic", "openai", "provider", "model"):
        assert word not in rendered
    for word in ("claude", "anthropic", "provider"):
        assert word not in json.dumps(document["analysis"]).lower()
        assert word not in json.dumps(document["design"]).lower()
    # Recorded, as provenance, beside the digest.
    assert document["provenance"]["design_provider"] == "scripted"


def test_an_undeclared_capability_is_refused_and_the_contract_is_kept(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
    runtime_xdg: Path,
) -> None:
    """A project with no declared command: the analysis is frozen, the refusal kept."""

    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        "schema_version: 1\nprojects: {}\n", encoding="utf-8"
    )
    idea = _idea(portfolio, runtime_project)
    router = _router(
        runtime_db, analysis=_analysis_answer(), design=_design_answer([1], [1])
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea, router, tmp_path
    )
    step = _advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    contract = portfolio.live_contract(
        idea_id=idea, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert contract is not None and contract.state is ContractState.BLOCKED_CAPABILITY
    assert contract.capability_request is not None
    assert contract.capability_request["outputs"][0]["source"] == "results/grid.json"
    assert router.requests_for_prompt(TEMPLATES["experiment_designer"].identity) == []

    # Asked again with nothing changed: no model call, the same answer.
    calls = len(router.requests)
    again = _advance(context)
    assert again.failure_class is FailureClass.CAPABILITY_DENIED
    assert len(router.requests) == calls, "a refusal that cannot change costs nothing"


# ================================================================ upgrade --
def test_the_contract_migration_applies_over_a_database_with_old_experiments(
    throwaway_dsn: str,
) -> None:
    """An existing deployment has experiments that carried their own rule.

    `0031` adds a nullable `contract_id` and an immutability trigger; a row
    written before it must keep reading exactly as it did, and the trigger
    must hold on it too -- a pre-contract preregistration is no less frozen.
    """

    from research_os.runtime.migrations import discover, migrate

    files = discover()
    before = [m for m in files if m.version <= "0030"]
    after = [m for m in files if m.version > "0030"]
    with Database(throwaway_dsn) as db:
        with db.tx() as conn:
            for migration in before:
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )
        # Raw SQL naming only columns 0030 had: the store's column lists
        # describe today's schema, which is the point of an upgrade test.
        idea_id = "PIDEA-20260101T000000Z-aaaaaaaa"
        with db.tx() as conn:
            conn.execute(
                "insert into projects (project_id, repo_path) values ('legacy', '/tmp/l')"
            )
            conn.execute(
                "insert into ideas (idea_id, project_id, depth, lineage_root, origin, "
                "current_version, status, operational_state, quality_tier) values "
                "(%s, 'legacy', 0, %s, 'BLIND_EXPLORER', 1, 'CANDIDATE', 'IDLE', 'NONE')",
                (idea_id, idea_id),
            )
            conn.execute(
                "insert into idea_versions (idea_id, version, title, "
                "research_question, core_idea, content_digest, canonical_digest, "
                "origin_role) values (%s, 1, 't', 'q', 'c', %s, %s, 'blind_explorer')",
                (
                    idea_id,
                    "pidea-content-v1:" + "c" * 64,
                    "pidea-canonical-v1:" + "d" * 64,
                ),
            )
        store = PortfolioStore(db)
        with db.tx() as conn:
            conn.execute(
                "insert into idea_experiments (experiment_id, idea_id, idea_version, "
                "project_id, command, spec_digest, variation_digest, workspace_path, "
                "decision_rule) values (%s, %s, 1, 'legacy', 'measure', %s, %s, "
                "'/tmp/w', %s::jsonb)",
                (
                    "PEXP-20260101T000000Z-aaaaaaaa",
                    idea_id,
                    "a" * 64,
                    "b" * 64,
                    json.dumps(
                        {
                            "output_path": "r.json",
                            "metric_path": "m",
                            "success": {"comparator": "<", "threshold": 1.0},
                            "failure": {"comparator": ">=", "threshold": 1.0},
                        }
                    ),
                ),
            )

        applied = migrate(db)
        assert applied == tuple(m.version for m in after)
        legacy = store.require_experiment("PEXP-20260101T000000Z-aaaaaaaa")
        assert legacy.contract_id is None
        assert legacy.decision_rule is not None
        with (
            pytest.raises(RuntimeDatabaseError, match="preregistered is frozen"),
            db.tx() as conn,
        ):
            conn.execute(
                "update idea_experiments set decision_rule = null, "
                "no_rule_reason = 'moved' where experiment_id = %s",
                ("PEXP-20260101T000000Z-aaaaaaaa",),
            )


def test_an_execution_that_moved_under_its_contract_does_not_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """A buggy or hostile repair, on the production path.

    Every digest is self-consistent -- the forged preregistration hashes to
    the row, the contract still verifies -- and the argv is not the frozen
    design's. `submit` must refuse it before anything runs, which it does only
    because `_verified_contract` compares the specification with the design.
    """

    from research_os.runtime.executors import spec_digest

    # Designed, not yet run: once something has been read under the
    # experiment, the database refuses to retire it at all.
    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    original = portfolio.get_experiment(idea_id=contract.idea_id, idea_version=1)
    assert original is not None
    spec, _rule = empirical._preregistered(context, original)
    forged_id = "PEXP-20260923T000000Z-f0f0f0f0"
    moved = replace(
        spec,
        argv=(*spec.argv, "--quietly-different"),
        cwd=str(empirical.workspace_for(forged_id)),
    )
    record = json.loads(
        context.artifacts.get_text(original.preregistration_artifact_id)
    )
    record.update(
        {
            "experiment_id": forged_id,
            "spec": empirical._spec_record(moved),
            "spec_digest": spec_digest(moved),
        }
    )
    ref = context.artifacts.put_text(
        json.dumps(record), media_type="application/json", role="forged", producer="t"
    )
    portfolio.update_experiment(
        original.experiment_id, state=ExperimentState.SUPERSEDED, detail="make room"
    )
    forged = portfolio.create_experiment(
        idea_id=original.idea_id,
        idea_version=1,
        project_id=runtime_project,
        role=ExperimentRole.PRIMARY,
        command=original.command,
        spec_digest=spec_digest(moved),
        variation_digest=empirical.variation_digest(moved),
        workspace_path=moved.cwd,
        decision_rule=original.decision_rule,
        no_rule_reason=original.no_rule_reason,
        preregistration_artifact_id=ref.artifact_id,
        experiment_id=forged_id,
        contract_id=contract.contract_id,
    )
    jobs_before = len(RuntimeStore(runtime_db).list_external_jobs())

    step = empirical.submit(context, forged)

    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "frozen design" in step.detail
    assert len(RuntimeStore(runtime_db).list_external_jobs()) == (jobs_before), (
        "nothing may run under a contract it does not realise"
    )


def test_a_declared_capability_resumes_the_blocked_contract_from_its_frozen_analysis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    runtime_project: str,
    tmp_path: Path,
    runtime_xdg: Path,
) -> None:
    """Refusal kept; a person declares the command; the tick notices; it resumes.

    The analysis frozen while no command could produce it is the analysis the
    eventual design is judged against -- same contract, same analysis digest.
    Nothing scientific is re-decided because a capability arrived.
    """

    from research_os.portfolio.models import OperationalState
    from research_os.portfolio.tick import tick
    from tests.runtime_graph_helpers import make_config

    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        "schema_version: 1\nprojects: {}\n", encoding="utf-8"
    )
    idea = _idea(portfolio, runtime_project)
    router = _router(
        runtime_db,
        analysis=_analysis_answer(),
        design=_design_answer([1, 2, 4], [1, 3]),
    )
    refused = _advance(
        _context(
            portfolio, runtime_db, tmp_path, runtime_project, idea, router, tmp_path
        )
    )
    assert refused.failure_class is FailureClass.CAPABILITY_DENIED
    blocked = portfolio.live_contract(
        idea_id=idea, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert blocked is not None and blocked.state is ContractState.BLOCKED_CAPABILITY
    # What the track does with a refusal (tested in test_portfolio_track).
    portfolio.set_operational_state(
        idea_id=idea, state=OperationalState.BLOCKED_EXTERNAL
    )

    def run_tick() -> Any:
        return tick(
            db=runtime_db,
            project_id=runtime_project,
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
        )

    assert run_tick().capability_unblocked == 0  # first observation records only
    assert (
        portfolio.require_idea(idea).operational_state
        is OperationalState.BLOCKED_EXTERNAL
    )

    repo = _project(
        tmp_path / "declared", runtime_xdg, runtime_project, script=GRID_SCRIPT
    )
    report = run_tick()
    assert report.capability_unblocked == 1, report.notes
    assert portfolio.require_idea(idea).operational_state is OperationalState.IDLE

    resumed = _advance(
        _context(portfolio, runtime_db, tmp_path, runtime_project, idea, router, repo)
    )
    assert resumed.ok, resumed.detail
    frozen = portfolio.require_contract(resumed.experiment.contract_id)
    assert frozen.contract_id == blocked.contract_id
    assert frozen.analysis_digest == blocked.analysis_digest
    assert frozen.state is ContractState.FROZEN
    assert resumed.conclusion is EmpiricalConclusion.SUPPORTS
    assert (
        len(router.requests_for_prompt(TEMPLATES["analysis_designer"].identity)) == 1
    ), "the analysis is not re-designed when a capability arrives"
