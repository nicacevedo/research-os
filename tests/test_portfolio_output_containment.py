"""A declared output is this run's only if the run wrote it, in its workspace.

The final hostile review of the autonomous build found three ways a program
that wrote *nothing* had a file it never saw read as its contained
measurement. Every reader asked ``is_symlink()`` of the output file alone, so
a *directory* on its path that was a link was followed by the unsandboxed
reader:

- ``results -> specimens``, committed as a "latest results" convenience, put
  a committed specimen behind the declared path -- and Git does not follow a
  link inside a tree, so the committed-specimen guard, asking for
  ``HEAD:results/run.json``, found nothing and called the file new;
- ``results -> /host/scratch``, committed, pointed at an older result written
  outside any sandbox;
- a link the contained program itself created on its way out pointed at a
  host file the sandbox never saw.

Each came out SUPPORTS, with a preregistration, a frozen contract, a job id
and a "bubblewrap, network denied" containment record. These tests run the
whole empirical route -- real bubblewrap, real Git, real PostgreSQL -- and
the program in every case is one that claims success and writes nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from research_os.portfolio.models import EmpiricalConclusion
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.test_portfolio_empirical import (
    EXPERIMENTS_YAML,
    _advance,
    _context,
    _git_project,
    _idea,
    _router,
    design_answer,
    pg_dsn,
    portfolio,
    project_repo,
    runtime_db,
    runtime_project,
    runtime_xdg,
)

__all__ = [
    "pg_dsn",
    "portfolio",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

SILENT = 'print("I claim success and write nothing")\n'


def _host_result(tmp_path: Path, overlap: float) -> Path:
    """Another run's result, written outside any sandbox, some time ago."""

    elsewhere = tmp_path / "host-scratch"
    elsewhere.mkdir()
    (elsewhere / "run.json").write_text(
        json.dumps({"summary": {"overlap": overlap}}), encoding="utf-8"
    )
    return elsewhere


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True
    )


def _run(portfolio, runtime_db, runtime_project, tmp_path, repo):
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=1)),
        repo=repo,
    )
    step = _advance(context)
    rows = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    return step, rows


@pytest.mark.parametrize("project_repo", [SILENT], indirect=True)
def test_a_committed_link_to_an_in_tree_specimen_is_not_a_measurement(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The committed-specimen guard, bypassed without leaving the workspace."""

    specimens = project_repo / "specimens"
    specimens.mkdir()
    (specimens / "run.json").write_text(
        json.dumps({"summary": {"overlap": 0.4}}), encoding="utf-8"
    )
    (project_repo / "results").symlink_to("specimens")
    _commit_all(project_repo, "specimen and a latest-results link")

    step, rows = _run(portfolio, runtime_db, runtime_project, tmp_path, project_repo)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    assert "symbolic link" in step.detail
    assert all(str(row.strength) != "SUPPORTS" for row in rows)


@pytest.mark.parametrize("project_repo", [SILENT], indirect=True)
def test_a_committed_link_to_a_host_directory_is_not_a_measurement(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    (project_repo / "results").symlink_to(_host_result(tmp_path, 0.4))
    _commit_all(project_repo, "results on scratch")

    step, rows = _run(portfolio, runtime_db, runtime_project, tmp_path, project_repo)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    assert all(str(row.strength) != "SUPPORTS" for row in rows)


def test_a_link_the_contained_program_made_is_not_a_measurement(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    runtime_xdg: Path,
    tmp_path: Path,
) -> None:
    """The sandbox never sees the target; the reader must not follow it either."""

    elsewhere = _host_result(tmp_path, 0.4)
    script = (
        "import os\n"
        "# Writes nothing. Points `results` at a host directory and exits 0.\n"
        f"os.symlink({str(elsewhere)!r}, 'results')\n"
        "print('linked')\n"
    )
    repo = _git_project(tmp_path / "linker-project", script=script)
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=runtime_project), encoding="utf-8"
    )

    step, rows = _run(portfolio, runtime_db, runtime_project, tmp_path, repo)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    assert all(str(row.strength) != "SUPPORTS" for row in rows)


@pytest.mark.parametrize("project_repo", [SILENT], indirect=True)
def test_the_control_a_specimen_at_the_declared_path_is_refused_as_before(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Without the link, the committed-specimen guard already caught it."""

    results = project_repo / "results"
    results.mkdir()
    (results / "run.json").write_text(
        json.dumps({"summary": {"overlap": 0.4}}), encoding="utf-8"
    )
    _commit_all(project_repo, "specimen")

    step, _rows = _run(portfolio, runtime_db, runtime_project, tmp_path, project_repo)
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
