"""Tests for the ``researchctl auto`` command group."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.automation import commands
from research_os.automation.config import config_path
from research_os.automation.models import Role, RunState
from research_os.automation.store import RunStore, worktrees_root
from research_os.cli import main
from research_os.registry import register_project
from tests.automation_helpers import BROKEN_MODULE, FIXED_MODULE, commit_all, init_repo
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.fs_helpers import make_git_repo, write_minimal_capsule
from tests.test_auto_controller import scripted, working_snapshot

FAKE_CONFIG = """planner:
  provider: fake
  model: fake-planner
  read_only: true
coder:
  provider: fake
  model: fake-coder
  read_only: false
  tools: [Read, Write, Edit]
reviewer:
  provider: fake
  model: fake-reviewer
  read_only: true
"""


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


@pytest.fixture
def wired(automation_home: Path) -> Iterator[FakeProvider]:
    """Install a fake provider registry and a config that names it."""

    provider = scripted()
    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    commands.set_registry_factory(lambda: {"fake": provider})
    try:
        yield provider
    finally:
        commands.set_registry_factory(None)


def test_auto_without_a_subcommand_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], wired: object
) -> None:
    code = run_cli(monkeypatch, "auto")
    out = capsys.readouterr().out

    assert code == 0
    assert "start" in out
    assert "cleanup" in out


def test_providers_reports_local_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], wired: object
) -> None:
    code = run_cli(monkeypatch, "auto", "providers")
    out = capsys.readouterr().out

    assert code == 0
    assert "Local provider discovery" in out
    assert "fake" in out
    assert "Role assignment" in out
    assert "review independence" in out
    assert str(worktrees_root()) in out


def test_dry_run_plans_without_touching_the_project(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    before = working_snapshot(repo)

    code = run_cli(
        monkeypatch, "auto", "start", str(repo), "--goal", "implement add", "--dry-run"
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "DRY RUN" in out
    assert "T-001" in out
    assert "pytest -q" in out
    assert "git diff --check HEAD   (added by the controller)" in out
    assert "no write worker was invoked" in out
    assert working_snapshot(repo) == before
    assert wired.requests_for(Role.CODER) == []
    assert not worktrees_root().exists()

    run = RunStore.open(RunStore.list_run_ids()[0]).load()
    assert run.dry_run is True
    assert run.state is RunState.PLAN_READY


def test_a_dry_run_cannot_be_executed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(
        monkeypatch, "auto", "start", str(repo), "--goal", "implement add", "--dry-run"
    )
    capsys.readouterr()
    run_id = RunStore.list_run_ids()[0]

    code = run_cli(monkeypatch, "auto", "run", run_id)
    captured = capsys.readouterr()

    assert code == 1
    assert "--dry-run" in captured.err


def test_the_full_slice_runs_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    assert (
        run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add") == 0
    )
    started = capsys.readouterr().out
    assert "Execute it with" in started
    run_id = RunStore.list_run_ids()[0]

    code = run_cli(monkeypatch, "auto", "run", run_id)
    out = capsys.readouterr().out

    assert code == 0
    assert "READY_FOR_HUMAN" in out
    assert "next human action" in out
    assert "Nothing has been merged" in out
    assert "git -C" in out
    assert "merge --no-ff" in out
    assert f"researchctl auto cleanup {run_id}" in out
    assert "review quality" in out
    assert "PASS" in out
    assert (repo / "adder.py").read_text(encoding="utf-8") == BROKEN_MODULE

    order = RunStore.open(run_id).load().work_orders[0]
    assert (Path(order.worktree_path or "") / "adder.py").read_text(
        encoding="utf-8"
    ) == FIXED_MODULE


def test_status_report_and_events_describe_a_finished_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add")
    run_id = RunStore.list_run_ids()[0]
    run_cli(monkeypatch, "auto", "run", run_id)
    capsys.readouterr()

    assert run_cli(monkeypatch, "auto", "status", run_id) == 0
    status = capsys.readouterr().out
    assert "READY_FOR_HUMAN" in status
    assert "model_calls  3/8" in status

    assert run_cli(monkeypatch, "auto", "status", run_id, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "READY_FOR_HUMAN"
    assert payload["work_orders"][0]["task_id"] == "T-001"

    assert run_cli(monkeypatch, "auto", "report", run_id) == 0
    assert "deterministic checks run by the controller" in capsys.readouterr().out

    assert run_cli(monkeypatch, "auto", "events", run_id, "--limit", "1") == 0
    events = capsys.readouterr().out.strip().splitlines()
    assert len(events) == 1
    assert json.loads(events[0])["event"] == "ready_for_human"

    assert run_cli(monkeypatch, "auto", "runs") == 0
    assert run_id in capsys.readouterr().out


def test_a_failing_run_exits_nonzero_and_still_reports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    automation_home: Path,
) -> None:
    repo = init_repo(tmp_path / "project")
    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    provider = scripted(
        coder=ScriptedResponse(
            text="done", write_files={"adder.py": "def add(a, b):\n    return 0\n"}
        )
    )
    commands.set_registry_factory(lambda: {"fake": provider})
    try:
        run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add")
        run_id = RunStore.list_run_ids()[0]
        capsys.readouterr()
        code = run_cli(monkeypatch, "auto", "run", run_id)
    finally:
        commands.set_registry_factory(None)
    captured = capsys.readouterr()

    assert code == 1
    assert "FAILED" in captured.out
    assert "Blockers:" in captured.out
    assert "left in place for you to" in captured.out
    assert "required acceptance commands failed" in captured.err


def test_cancel_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add")
    run_id = RunStore.list_run_ids()[0]
    run_cli(monkeypatch, "auto", "run", run_id)
    capsys.readouterr()

    assert run_cli(monkeypatch, "auto", "cleanup", run_id) == 0
    out = capsys.readouterr().out
    assert "removed worktree" in out
    assert "Branches were kept" in out

    assert run_cli(monkeypatch, "auto", "cleanup", run_id) == 0
    assert "no live worktrees" in capsys.readouterr().out


def test_cancelling_a_planned_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add")
    run_id = RunStore.list_run_ids()[0]
    capsys.readouterr()

    assert run_cli(monkeypatch, "auto", "cancel", run_id, "--reason", "not now") == 0
    assert "CANCELLED" in capsys.readouterr().out
    assert RunStore.open(run_id).load().state is RunState.CANCELLED


def test_a_registered_project_id_can_be_used(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = make_git_repo(tmp_path / "science")
    project = write_minimal_capsule(repo, project_id="demo-project")
    (repo / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (repo / "test_adder.py").write_text(
        "from adder import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    commit_all(repo)
    register_project(project, repo)

    code = run_cli(
        monkeypatch,
        "auto",
        "start",
        "demo-project",
        "--goal",
        "implement add",
        "--dry-run",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "demo-project" in out
    assert str(repo) in out


def test_an_unknown_project_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wired: FakeProvider,
) -> None:
    code = run_cli(monkeypatch, "auto", "start", "no-such-project", "--goal", "x")

    assert code == 1
    assert "neither a registered project id" in capsys.readouterr().err


def test_an_unknown_run_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wired: FakeProvider,
) -> None:
    code = run_cli(monkeypatch, "auto", "status", "RUN-20260909T101500Z-deadbeef")

    assert code == 1
    assert "no automation run" in capsys.readouterr().err


def test_an_out_of_range_budget_override_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    code = run_cli(
        monkeypatch,
        "auto",
        "start",
        str(repo),
        "--goal",
        "implement add",
        "--max-model-calls",
        "0",
    )

    assert code == 1
    assert "invalid budget override" in capsys.readouterr().err


def test_a_budget_override_is_recorded_on_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(
        monkeypatch,
        "auto",
        "start",
        str(repo),
        "--goal",
        "implement add",
        "--max-model-calls",
        "5",
        "--dry-run",
    )
    capsys.readouterr()

    assert RunStore.open(RunStore.list_run_ids()[0]).load().budget.max_model_calls == 5


def test_skipping_the_planner_spends_no_model_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    code = run_cli(
        monkeypatch,
        "auto",
        "start",
        str(repo),
        "--goal",
        "implement add",
        "--skip-planner",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "no plan was produced" in out
    assert wired.calls == []


def test_a_dirty_project_is_refused_with_a_usable_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    wired: FakeProvider,
) -> None:
    repo = init_repo(tmp_path / "project")
    (repo / "adder.py").write_text("# edited\n", encoding="utf-8")

    code = run_cli(monkeypatch, "auto", "start", str(repo), "--goal", "implement add")

    assert code == 1
    assert "Commit or stash first" in capsys.readouterr().err
