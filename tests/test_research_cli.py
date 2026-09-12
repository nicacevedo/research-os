"""Tests for the ``researchctl research`` command group.

The whole architecture is supposed to be reachable from a handful of verbs, so
these drive the real CLI: real argument parsing, real run directories, real
exit codes, and the provider registry swapped for a fake through the same seam
the automation CLI tests use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.automation import commands as auto_commands
from research_os.automation.config import config_path
from research_os.cli import main
from research_os.research.models import ResearchState
from research_os.research.store import ResearchStore
from tests.fake_providers import FakeProvider
from tests.research_helpers import (
    checkpoint_task,
    init_repo,
    plan_payload,
    scripted,
)

FAKE_CONFIG = """planner:
  provider: fake
  model: fake-planner
  read_only: true
analyst:
  provider: fake
  model: fake-analyst
  read_only: true
  access: snapshot_read
  tools: [Read, Glob, Grep]
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
    auto_commands.set_registry_factory(lambda: {"fake": provider})
    try:
        yield provider
    finally:
        auto_commands.set_registry_factory(None)


def test_start_plans_and_says_how_to_run_it(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path / "project")
    code = run_cli(
        monkeypatch,
        "research",
        "start",
        str(repo),
        "--goal",
        "Find out whether deformation stays linear above 10N.",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "PLAN_READY" in out
    assert "researchctl research run RR-" in out
    run_id = ResearchStore.list_run_ids()[-1]
    assert ResearchStore.open(run_id).load().state is ResearchState.PLAN_READY


def test_start_with_run_executes_in_one_command(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path / "project")
    code = run_cli(
        monkeypatch,
        "research",
        "start",
        str(repo),
        "--goal",
        "Read the field.",
        "--run",
    )
    assert code == 0
    assert "READY_FOR_HUMAN" in capsys.readouterr().out


def test_status_report_list_and_events_all_render(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "research", "start", str(repo), "--goal", "Read the field.")
    capsys.readouterr()
    run_id = ResearchStore.list_run_ids()[-1]

    assert run_cli(monkeypatch, "research", "status", run_id) == 0
    assert run_id in capsys.readouterr().out

    assert run_cli(monkeypatch, "research", "report", run_id, "--events") == 0
    report = capsys.readouterr().out
    assert "budget" in report
    assert "plan_accepted" in report

    assert run_cli(monkeypatch, "research", "list") == 0
    assert run_id in capsys.readouterr().out

    assert run_cli(monkeypatch, "research", "events", run_id) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["event"] == "run_created"


def test_answering_a_checkpoint_from_the_cli_continues_the_run(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = scripted(plan=plan_payload(tasks=[checkpoint_task()]))
    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    auto_commands.set_registry_factory(lambda: {"fake": provider})
    try:
        repo = init_repo(tmp_path / "project")
        run_cli(
            monkeypatch,
            "research",
            "start",
            str(repo),
            "--goal",
            "Decide before spending.",
            "--run",
        )
        assert "WAITING_FOR_HUMAN" in capsys.readouterr().out
        run_id = ResearchStore.list_run_ids()[-1]

        assert (
            run_cli(
                monkeypatch, "research", "answer", run_id, "--answer", "Yes, proceed."
            )
            == 0
        )
        assert "researchctl research run" in capsys.readouterr().out
        assert run_cli(monkeypatch, "research", "run", run_id) == 0
        assert "READY_FOR_HUMAN" in capsys.readouterr().out
    finally:
        auto_commands.set_registry_factory(None)


def test_declining_a_checkpoint_from_the_cli_ends_the_run(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = scripted(plan=plan_payload(tasks=[checkpoint_task()]))
    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    auto_commands.set_registry_factory(lambda: {"fake": provider})
    try:
        repo = init_repo(tmp_path / "project")
        run_cli(
            monkeypatch,
            "research",
            "start",
            str(repo),
            "--goal",
            "Decide before spending.",
            "--run",
        )
        capsys.readouterr()
        run_id = ResearchStore.list_run_ids()[-1]
        assert (
            run_cli(
                monkeypatch,
                "research",
                "answer",
                run_id,
                "--answer",
                "No, too expensive.",
                "--stop",
            )
            == 0
        )
        assert "Nothing further will run." in capsys.readouterr().out
        assert ResearchStore.open(run_id).load().state is ResearchState.CANCELLED
    finally:
        auto_commands.set_registry_factory(None)


def test_cancel_stops_an_unfinished_run(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "research", "start", str(repo), "--goal", "Read the field.")
    capsys.readouterr()
    run_id = ResearchStore.list_run_ids()[-1]
    assert run_cli(monkeypatch, "research", "cancel", run_id) == 0
    assert ResearchStore.open(run_id).load().state is ResearchState.CANCELLED


def test_an_unknown_run_id_exits_with_an_error(
    wired: FakeProvider, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    code = run_cli(monkeypatch, "research", "status", "RR-20260912T101500Z-deadbeef")
    assert code != 0
    assert "no research run" in capsys.readouterr().err


def test_the_group_prints_help_when_given_no_subcommand(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert run_cli(monkeypatch, "research") == 0
    assert "start" in capsys.readouterr().out


def test_cleanup_reports_when_there_is_nothing_to_remove(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "research", "start", str(repo), "--goal", "Read the field.")
    capsys.readouterr()
    run_id = ResearchStore.list_run_ids()[-1]
    assert run_cli(monkeypatch, "research", "cleanup", run_id) == 0
    out = capsys.readouterr().out
    assert "Nothing to remove" in out
    assert "Nothing scientific was touched." in out


def test_resume_recovers_an_interrupted_run_from_the_cli(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from research_os.research.models import TaskStatus

    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "research", "start", str(repo), "--goal", "Read the field.")
    capsys.readouterr()
    run_id = ResearchStore.list_run_ids()[-1]

    # Leave it exactly as a killed process would.
    store = ResearchStore.open(run_id)
    run = store.load()
    store.save(
        run.model_copy(
            update={
                "state": ResearchState.EXECUTING,
                "tasks": [
                    item.model_copy(update={"status": TaskStatus.RUNNING})
                    for item in run.tasks
                ],
            }
        )
    )

    assert run_cli(monkeypatch, "research", "run", run_id) != 0
    assert "resume" in capsys.readouterr().err

    assert run_cli(monkeypatch, "research", "resume", run_id, "--retry") == 0
    assert "researchctl research run" in capsys.readouterr().out
    assert run_cli(monkeypatch, "research", "run", run_id) == 0
    assert "READY_FOR_HUMAN" in capsys.readouterr().out


def test_doctor_names_an_interrupted_research_run(
    wired: FakeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The one stuck state a researcher would otherwise never find out about."""

    repo = init_repo(tmp_path / "project")
    run_cli(monkeypatch, "research", "start", str(repo), "--goal", "Read the field.")
    capsys.readouterr()
    run_id = ResearchStore.list_run_ids()[-1]
    store = ResearchStore.open(run_id)
    store.save(store.load().model_copy(update={"state": ResearchState.EXECUTING}))

    assert run_cli(monkeypatch, "doctor", "--no-storage") == 0
    out = capsys.readouterr().out
    assert run_id in out
    assert "researchctl research resume" in out
