"""INV-07 -- what a replication establishes, and what it cannot.

Two rounds of findings shaped this file.

**H6** (final adversarial review of 37e8afe): whole-file output digests were
the only guard against "the variation did not reach the measurement", so a
replication whose program ignored its new seed but stamped a timestamp was
recorded as an independent REPLICATION with SUPPORTS.

**The independent review of 8e92e8c**: the repair made independence rest on an
``execution_receipt`` the *program* wrote into its own output. A program that
ignored its seed and echoed it into a receipt was accepted (Case C), and a
model-composed plan could carry such a receipt through for a program to echo.

What is established now (``docs/ARCHITECTURE_INVARIANTS.md`` INV-07), three
findings kept apart:

1. **configuration independence** -- proved, from receipts the Research OS
   runner wrote (``portfolio/provenance.py``): a separate execution, bound to
   a manifest frozen before it ran, was delivered a configuration that differs
   from its parent's in what the manifest says it varies;
2. **perturbation validity** -- *attested*, never proved: the researcher's
   ``perturbation_attestation`` in ``experiments.yaml``, frozen into the
   manifest, says the computation uses what was varied. Nothing a program
   writes is read as this, or as anything else;
3. **agreement** -- the scientific readings compared, recorded beside.

A replication counts only with (1) and (2). Every case below runs a real
subprocess.
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

ATTESTATION = '        perturbation_attestation: ["seeds", "seed"]\n'

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

#: Case C: ignores the seed and *claims* to have used it, in the very object
#: the first repair trusted.
ECHOES_THE_SEED_IT_IGNORES = (
    _HEAD
    + """\
out.write_text(json.dumps({
    "summary": {"overlap": 0.4},
    "execution_receipt": {"seeds": [seed], "parameters": {"seed": seed}},
}))
"""
)

#: Uses the seed, and says nothing about it.
USES_THE_SEED = (
    _HEAD
    + """\
out.write_text(json.dumps({
    "summary": {"overlap": 0.9 - 0.1 * (seed % 9)},
    "meta": {"finished_at": time.time_ns()},
}))
"""
)


def _config(runtime_xdg: Any) -> Path:
    return Path(str(runtime_xdg)).parent / "config" / "experiments.yaml"


def _unattest(runtime_xdg: Any) -> None:
    """The same declaration with no perturbation attestation."""

    path = _config(runtime_xdg)
    text = path.read_text(encoding="utf-8")
    assert ATTESTATION in text
    path.write_text(text.replace(ATTESTATION, ""), encoding="utf-8")


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


def _measure(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    *,
    seed: int = 14,
) -> Any:
    return _measured(
        portfolio,
        runtime_db,
        runtime_project,
        project_repo,
        tmp_path,
        replication_seed=seed,
    )


# ------------------------------------------------- not a valid perturbation --
@pytest.mark.parametrize("project_repo", [IGNORES_THE_SEED], indirect=True)
def test_without_an_attestation_a_delivered_seed_is_not_a_valid_perturbation(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    runtime_xdg: Any,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """H6's program: the seed is delivered, ignored, and the bytes differ.

    Configuration independence is proved -- the runner delivered seed 14 to a
    separate execution -- and that is all. With nothing attesting that the
    computation uses the seed, this is not a replication; the differing
    bytes were never the question.
    """

    _unattest(runtime_xdg)
    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    document = _analysis(context, step)
    independence = document["replication"]["independence"]
    assert independence["configuration_independent"] is True
    assert independence["perturbation_validity"] == "UNATTESTED"
    assert independence["counts_as_independent_replication"] is False
    assert set(independence["unattested"]) == {"seeds", "parameter seed"}
    primary = context.portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    theirs = json.loads(context.artifacts.get_text(primary.analysis_artifact_id))
    assert {o["sha256"] for o in theirs["outputs"]} != {
        o["sha256"] for o in document["outputs"]
    }
    assert document["replication"]["agreement"]["identical_scientific_values"] is True
    rows = _replication_rows(portfolio, context)
    assert [row.strength for row in rows] == [EvidenceStrength.INCONCLUSIVE], rows


@pytest.mark.parametrize("project_repo", [ECHOES_THE_SEED_IT_IGNORES], indirect=True)
def test_a_receipt_a_program_writes_is_never_provenance(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    runtime_xdg: Any,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The first repair's evidence, presented again, and read as nothing."""

    _unattest(runtime_xdg)
    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    independence = _analysis(context, step)["replication"]["independence"]
    assert independence["counts_as_independent_replication"] is False
    assert "execution_receipt" not in json.dumps(independence)


# ------------------------------------ what an attestation does, and doesn't --
@pytest.mark.parametrize("project_repo", [ECHOES_THE_SEED_IT_IGNORES], indirect=True)
def test_case_c_an_attested_seed_the_program_ignores_is_attested_not_proven(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The review's Case C, with the researcher's attestation in place.

    Research OS proves the seed was delivered to a separate execution; the
    researcher's declaration says the computation uses it; this program does
    not. That is outside what any generic runtime can detect, and the record
    says exactly so: the perturbation is ``ATTESTED_BY_RESEARCHER``, the
    unproved part is written down, and the identical values are recorded
    beside it for a person to read.
    """

    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.SUPPORTS, step.detail
    replication = _analysis(context, step)["replication"]
    independence = replication["independence"]
    assert independence["configuration_independent"] is True
    assert independence["perturbation_validity"] == "ATTESTED_BY_RESEARCHER"
    assert "not" in independence["what_is_not_proved"]
    assert "attested, not proved" in independence["basis"]
    assert replication["agreement"]["identical_scientific_values"] is True


@pytest.mark.parametrize("project_repo", [USES_THE_SEED], indirect=True)
def test_a_used_seed_with_identical_values_is_independent_and_agrees(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Seeds 5 and 14 give the same overlap from a program that uses them."""

    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.SUPPORTS, step.detail
    replication = _analysis(context, step)["replication"]
    assert replication["independence"]["counts_as_independent_replication"] is True
    assert set(replication["independence"]["attested"]) == {"seeds", "parameter seed"}
    assert replication["agreement"]["agrees"] is True
    assert replication["agreement"]["identical_scientific_values"] is True
    (row,) = _replication_rows(portfolio, context)
    assert row.strength is EvidenceStrength.SUPPORTS
    assert "independent replication" in row.summary and "agrees" in row.summary


@pytest.mark.parametrize("project_repo", [USES_THE_SEED], indirect=True)
def test_an_independent_execution_that_disagrees_is_recorded_as_both(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Seed 1 gives 0.8 against the primary's 0.4 -- independent, disagreeing."""

    context = _measure(
        portfolio, runtime_db, runtime_project, project_repo, tmp_path, seed=1
    )
    step = _replicate(context)
    assert step.conclusion is EmpiricalConclusion.CONTRADICTS, step.detail
    replication = _analysis(context, step)["replication"]
    assert replication["independence"]["counts_as_independent_replication"] is True
    assert replication["agreement"]["agrees"] is False
    assert replication["agreement"]["identical_scientific_values"] is False
    (row,) = _replication_rows(portfolio, context)
    assert row.strength is EvidenceStrength.CONTRADICTS
    assert "disagrees" in row.summary


# ------------------------------------------ the manifest and the receipts ----
@pytest.mark.parametrize("project_repo", [USES_THE_SEED], indirect=True)
def test_the_manifest_is_frozen_before_the_replication_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    experiment = step.experiment
    assert experiment.execution_manifest_artifact_id
    manifest = json.loads(
        context.artifacts.get_text(experiment.execution_manifest_artifact_id)
    )
    primary = context.portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    parent_receipt = context.portfolio.receipt_for_job(primary.job_id)
    assert manifest["schema"] == "portfolio-replication-manifest-v2"
    assert manifest["parent"]["experiment_id"] == primary.experiment_id
    assert manifest["parent"]["receipt_id"] == parent_receipt.receipt_id
    assert manifest["parent"]["evidence_id"] == primary.evidence_id
    assert manifest["parent"]["analysis_artifact_id"] == primary.analysis_artifact_id
    assert manifest["execution"]["spec_digest"] == experiment.spec_digest
    assert manifest["execution"]["job_id"] == experiment.job_id
    assert manifest["code"]["base_commit"] and manifest["code"]["command"] == "measure"
    assert manifest["capability"]["perturbation_attestation"] == ["seed", "seeds"]
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


@pytest.mark.parametrize("project_repo", [USES_THE_SEED], indirect=True)
def test_the_runner_writes_each_receipt_from_what_it_delivered(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    from research_os.portfolio import provenance

    context = _measure(portfolio, runtime_db, runtime_project, project_repo, tmp_path)
    step = _replicate(context)
    replication = step.experiment
    receipt = context.portfolio.receipt_for_job(replication.job_id)
    document = provenance.verified_receipt(context.artifacts, receipt)
    assert document["written_by"] == provenance.RECEIPT_WRITER
    argv = document["delivered"]["argv"]
    assert argv[argv.index("--seed") + 1] == "14"
    assert document["delivered"]["env"]["RESEARCH_OS_SEED_0"] == "14"
    assert document["execution_id"] == replication.job_id
    analysis = _analysis(context, step)
    assert analysis["execution_receipt"]["receipt_id"] == receipt.receipt_id
    recorded = {path: sha for path, sha, _size in document["outputs"]}
    assert {o["path"]: o["sha256"] for o in analysis["outputs"]}.items() <= (
        recorded.items()
    )
    parent = context.portfolio.get_receipt(receipt.parent_receipt_id)
    assert parent is not None and parent.role is ExperimentRole.PRIMARY
    assert parent.job_id != receipt.job_id


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
