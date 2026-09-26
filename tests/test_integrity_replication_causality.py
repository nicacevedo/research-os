"""INV-07 -- a replication is independent only if its variation reached the computation.

The HIGH finding this file closes (final adversarial review of 37e8afe, H6):
``empirical._byte_identical_primary`` was the only guard against "the
variation did not reach the measurement", and it compared whole-file
digests. A replication whose program ignored its new seed but stamped a
timestamp produced different bytes and the same measurement, and was
recorded as an independent REPLICATION with SUPPORTS.

The repair separates two questions the byte comparison conflated:

1. **execution independence** -- established only by the computation's own
   consumption receipt (``empirical.RECEIPT_KEY``), checked against an
   execution manifest frozen before the replication ran;
2. **agreement** -- the replication's reading against the primary's, on the
   scientific values the frozen analysis read, recorded separately.

The four cases the brief names, each with a real subprocess:

- the seed changes in the manifest and the program ignores it: not independent;
- timestamps differ but the computation is identical because the variable
  was ignored: not independent;
- the specified seed is consumed and the scientific values are identical:
  independent, and agreeing;
- a genuinely independent execution with a different result: independent,
  and the disagreement recorded separately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import empirical
from research_os.portfolio.models import (
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentRole,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import _advance, project_repo
from tests.test_portfolio_integrity_regressions import _measured

__all__ = [
    "pg_dsn",
    "portfolio",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

_HEAD = """\
import json, pathlib, sys, time
seed = int(sys.argv[sys.argv.index("--seed") + 1])
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
"""

#: Reads --seed and throws it away; the "measurement" is a constant, and a
#: wall-clock stamp makes every run's bytes different.
IGNORES_THE_SEED = (
    _HEAD
    + """\
out.write_text(json.dumps({
    "summary": {"overlap": 0.4},
    "meta": {"finished_at": time.time_ns()},
}))
"""
)

#: Uses the seed and reports what it used.
REPORTS_WHAT_IT_USED = (
    _HEAD
    + """\
out.write_text(json.dumps({
    "summary": {"overlap": 0.9 - 0.1 * (seed % 9)},
    "meta": {"finished_at": time.time_ns()},
    "execution_receipt": {"seeds": [seed], "parameters": {"seed": seed}},
}))
"""
)

#: Ignores the seed it was given, uses 5, and says so honestly.
USES_A_FIXED_SEED = (
    _HEAD
    + """\
used = 5
out.write_text(json.dumps({
    "summary": {"overlap": 0.9 - 0.1 * (used % 9)},
    "execution_receipt": {"parameters": {"seed": used}},
}))
"""
)


def _replicate(context: Any) -> Any:
    step = _advance(context, role=ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    return step


def _analysis(context: Any, step: Any) -> dict[str, Any]:
    experiment = step.experiment
    assert experiment.analysis_artifact_id
    return json.loads(context.artifacts.get_text(experiment.analysis_artifact_id))


def _replication_rows(portfolio: PortfolioStore, context: Any) -> list[Any]:
    return [
        row
        for row in portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)
        if row.kind is EvidenceKind.REPLICATION
    ]


# -------------------------------------------------------- not independent ---
@pytest.mark.parametrize("project_repo", [IGNORES_THE_SEED], indirect=True)
def test_a_seed_the_program_ignores_is_not_an_independent_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Cases 1 and 2: the manifest varies the seed; timestamps differ; nothing else does."""

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    document = _analysis(context, step)
    independence = document["replication"]["independence"]
    assert independence["verified"] is False
    assert "no execution receipt" in independence["basis"]
    # The bytes did differ -- and that was never the question.
    primary = context.portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    theirs = json.loads(context.artifacts.get_text(primary.analysis_artifact_id))
    assert {o["sha256"] for o in theirs["outputs"]} != {
        o["sha256"] for o in document["outputs"]
    }
    # Agreement is still recorded, separately: the same number was read.
    agreement = document["replication"]["agreement"]
    assert agreement["identical_scientific_values"] is True
    rows = _replication_rows(portfolio, context)
    assert [row.strength for row in rows] == [EvidenceStrength.INCONCLUSIVE], rows


@pytest.mark.parametrize("project_repo", [USES_A_FIXED_SEED], indirect=True)
def test_a_receipt_reporting_the_primarys_seed_is_a_contradiction(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Neighbour: the program is honest that it did not use what it was given."""

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    independence = _analysis(context, step)["replication"]["independence"]
    assert independence["verified"] is False
    assert (
        independence["contradicted"]
        and "parameter seed" in independence["contradicted"][0]
    )
    assert "did not reach it" in independence["basis"]


# ------------------------------------------------------------ independent ---
@pytest.mark.parametrize("project_repo", [REPORTS_WHAT_IT_USED], indirect=True)
def test_a_consumed_seed_with_identical_values_is_independent_and_agrees(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Case 3: seeds 5 and 14 give the same overlap -- and the receipt shows 14 was used."""

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.SUPPORTS, step.detail
    replication = _analysis(context, step)["replication"]
    assert replication["independence"]["verified"] is True
    assert set(replication["independence"]["consumed"]) == {"seeds", "parameter seed"}
    assert replication["agreement"]["agrees"] is True
    assert replication["agreement"]["identical_scientific_values"] is True
    (row,) = _replication_rows(portfolio, context)
    assert row.strength is EvidenceStrength.SUPPORTS
    assert "independent execution" in row.summary and "agrees" in row.summary


@pytest.mark.parametrize("project_repo", [REPORTS_WHAT_IT_USED], indirect=True)
def test_an_independent_execution_that_disagrees_is_recorded_as_both(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Case 4: seed 1 gives 0.8 against the primary's 0.4 -- independent, disagreeing."""

    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=1,
    )
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.CONTRADICTS, step.detail
    replication = _analysis(context, step)["replication"]
    assert replication["independence"]["verified"] is True
    assert replication["agreement"]["agrees"] is False
    assert replication["agreement"]["identical_scientific_values"] is False
    (row,) = _replication_rows(portfolio, context)
    assert row.strength is EvidenceStrength.CONTRADICTS
    assert "disagrees" in row.summary


# ------------------------------------------------------ the frozen manifest --
@pytest.mark.parametrize("project_repo", [REPORTS_WHAT_IT_USED], indirect=True)
def test_the_manifest_is_frozen_before_the_replication_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    context = _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=14,
    )
    step = _replicate(context)
    experiment = step.experiment
    assert experiment.execution_manifest_artifact_id
    manifest = json.loads(
        context.artifacts.get_text(experiment.execution_manifest_artifact_id)
    )
    primary = context.portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert manifest["parent"]["experiment_id"] == primary.experiment_id
    assert manifest["parent"]["spec_digest"] == primary.spec_digest
    assert manifest["execution"]["spec_digest"] == experiment.spec_digest
    assert manifest["execution"]["job_id"] == experiment.job_id
    assert manifest["code"]["base_commit"] and manifest["code"]["command"] == "measure"
    assert manifest["independence_variables"] == {
        "seeds": {"primary": [5], "replication": [14]},
        "parameters": {"seed": {"primary": "5", "replication": "14"}},
    }
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select a.created_at as frozen, j.submitted_at as ran "
            "from artifacts a, external_jobs j "
            "where a.artifact_id = %s and j.job_id = %s",
            (experiment.execution_manifest_artifact_id, experiment.job_id),
        ).fetchone()
    assert row is not None and row["frozen"] <= row["ran"], (
        "the manifest was written after the execution it describes"
    )


# ------------------------------------------------------------- unit level ---
class _Artifacts:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def get_bytes(self, digest: str) -> bytes:
        return self.blobs[digest]


class _Context:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.artifacts = _Artifacts(blobs)


def test_a_receipt_a_model_composed_input_could_carry_is_refused(
    tmp_path: Path,
) -> None:
    """A program that echoes its plan would echo a model-written receipt too."""

    import hashlib

    from research_os.runtime.interfaces import ExecutionSpec

    plan = json.dumps({"ratios": [1, 2], "execution_receipt": {"seeds": [14]}}).encode()
    digest = hashlib.sha256(plan).hexdigest()
    output = json.dumps(
        {"summary": {"spread": 1}, "execution_receipt": {"seeds": [14]}}
    )
    (tmp_path / "out.json").write_text(output)
    analysis = empirical.Analysis(
        conclusion=EmpiricalConclusion.SUPPORTS,
        summary="x",
        outputs=(
            ("out.json", hashlib.sha256(output.encode()).hexdigest(), len(output)),
        ),
    )
    spec = ExecutionSpec(
        name="x", argv=("x",), cwd=str(tmp_path), inputs=(("plan.json", digest),)
    )
    receipt, why = empirical._receipt(
        _Context({digest: plan}), tmp_path, analysis, spec
    )
    assert receipt is None and "model-composed input" in why
    clean = ExecutionSpec(name="x", argv=("x",), cwd=str(tmp_path))
    receipt, where = empirical._receipt(_Context({}), tmp_path, analysis, clean)
    assert receipt == {"seeds": [14]} and where == "out.json"


def test_a_replication_with_no_frozen_manifest_is_not_independent(
    tmp_path: Path,
) -> None:
    class _NoManifest:
        spec_digest = "d"
        execution_manifest_artifact_id = None

    verdict = empirical.assess_independence(
        _Context({}),
        _NoManifest(),  # type: ignore[arg-type]
        analysis=empirical.Analysis(
            conclusion=EmpiricalConclusion.SUPPORTS, summary="x"
        ),
        spec=None,  # type: ignore[arg-type]
        workspace=tmp_path,
    )
    assert not verdict.verified and "no execution manifest" in verdict.basis


def test_the_independence_variables_are_the_frozen_differences_only() -> None:
    primary = {
        "command": "measure",
        "command_parameters": {"seed": 5, "out": "r.json"},
        "spec": {"seeds": [5], "inputs": [["plan.json", "aa"]]},
    }
    same = empirical.independence_variables(primary, primary)
    assert same == {}
    other = {
        "command": "measure-alt",
        "command_parameters": {"seed": 5, "out": "r.json"},
        "spec": {"seeds": [5], "inputs": [["plan.json", "bb"]]},
    }
    assert empirical.independence_variables(primary, other) == {
        "inputs": {"plan.json": {"primary": "aa", "replication": "bb"}},
        "command": {"primary": "measure", "replication": "measure-alt"},
    }
