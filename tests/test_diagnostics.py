"""What ``researchctl doctor`` and ``researchctl storage`` actually establish.

Everything here runs against a relocated Research OS home, so the checks
inspect a real (empty or populated) machine rather than the developer's own.
Nothing in the module under test makes a network request, submits a job, or
invokes a model, and one test asserts exactly that by removing every provider
from PATH.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_os import diagnostics
from research_os.cli import main
from research_os.diagnostics import Report, Status, collect, render, storage_usage


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


def statuses(report: Report) -> dict[str, Status]:
    return {
        f"{section.title}/{check.name}": check.status
        for section in report.sections
        for check in section.checks
    }


def test_a_clean_machine_reports_sections_in_reading_order(
    automation_home: Path,
) -> None:
    report = collect()
    assert [section.title for section in report.sections] == [
        "environment",
        "providers",
        "literature",
        "experiments",
        "runs",
        "disk",
    ]


def test_an_absent_capability_is_a_warning_not_a_failure(
    automation_home: Path,
) -> None:
    """A machine with no cluster and no declared experiments is a fine machine.

    If that exited non-zero, every researcher would learn to ignore the exit
    code, and then it would tell them nothing when something was actually wrong.
    """

    report = collect()
    found = statuses(report)
    assert found["experiments/declared commands"] is Status.WARN
    assert found["experiments/scheduler"] is Status.WARN
    assert report.ok


def test_a_broken_directory_is_a_failure(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "not-a-directory"
    broken.write_text("I am a file\n", encoding="utf-8")
    monkeypatch.setenv("RESEARCH_OS_DATA_HOME", str(broken))
    report = collect()
    assert statuses(report)["environment/data"] is Status.FAIL
    assert not report.ok


def test_no_provider_installed_is_reported_rather_than_raised(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor must answer even when nothing is installed. That is the case it is for."""

    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    report = collect()
    providers = next(
        section for section in report.sections if section.title == "providers"
    )
    assert providers.status is Status.FAIL
    assert any("install" in check.advice.lower() for check in providers.checks)


def test_every_failing_check_says_what_to_do(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "file-not-directory"
    broken.write_text("x", encoding="utf-8")
    monkeypatch.setenv("RESEARCH_OS_CACHE_HOME", str(broken))
    report = collect()
    for section in report.sections:
        for check in section.checks:
            if check.status is Status.FAIL:
                assert check.advice, f"{section.title}/{check.name} has no advice"


def test_storage_measures_every_store(automation_home: Path) -> None:
    names = {item.name for item in storage_usage()}
    assert {
        "research runs",
        "automation runs",
        "worktrees",
        "proposals",
        "experiment runs",
        "drafts",
        "insights",
        "nominations",
        "literature index",
        "literature files",
    } <= names


def test_measuring_never_follows_a_symlink_out_of_the_store(
    automation_home: Path, tmp_path: Path
) -> None:
    """A store containing a link must not make the walk wander into a project."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 10_000)
    store = automation_home / "runs"
    store.mkdir(parents=True, exist_ok=True)
    (store / "link").symlink_to(outside, target_is_directory=True)

    measured = {item.name: item for item in storage_usage()}
    assert measured["automation runs"].bytes_used < 10_000


def test_the_rendered_report_escapes_control_characters() -> None:
    from research_os.diagnostics import Check, Section

    report = Report(
        sections=[
            Section(
                title="providers",
                checks=(
                    Check(
                        name="hostile",
                        status=Status.WARN,
                        detail="version \x1b[31m1.0",
                        advice="do \x07 something",
                    ),
                ),
            )
        ]
    )
    text = render(report)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "\\x1b" in text


def test_doctor_exits_zero_on_a_working_machine(
    automation_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert run_cli(monkeypatch, "doctor", "--no-storage") == 0
    out = capsys.readouterr().out
    assert "environment" in out
    assert "summary" in out


def test_doctor_json_is_machine_readable(
    automation_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert run_cli(monkeypatch, "doctor", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {section["title"] for section in payload["sections"]} >= {"environment"}
    assert payload["storage"]


def test_doctor_exits_non_zero_when_something_is_broken(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    broken = tmp_path / "file-not-dir"
    broken.write_text("x", encoding="utf-8")
    monkeypatch.setenv("RESEARCH_OS_STATE_HOME", str(broken))
    assert run_cli(monkeypatch, "doctor", "--no-storage") != 0
    assert "FAIL" in capsys.readouterr().out


def test_storage_reports_and_reclaims_nothing_on_a_clean_machine(
    automation_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert run_cli(monkeypatch, "storage") == 0
    assert "runtime storage" in capsys.readouterr().out

    assert run_cli(monkeypatch, "storage", "--reclaim") == 0
    out = capsys.readouterr().out
    assert "Nothing to reclaim" in out
    assert "Nothing scientific was touched." in out


def test_reclaim_releases_a_finished_run_worktree(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A real run, a real worktree, and the real cleanup path."""

    from research_os.automation import commands as auto_commands
    from research_os.automation.config import config_path
    from research_os.automation.store import RunStore
    from tests.automation_helpers import init_repo
    from tests.test_auto_cli import FAKE_CONFIG
    from tests.test_auto_controller import scripted

    provider = scripted()
    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    auto_commands.set_registry_factory(lambda: {"fake": provider})
    try:
        repo = init_repo(tmp_path / "project")
        assert (
            run_cli(
                monkeypatch,
                "auto",
                "start",
                str(repo),
                "--goal",
                "Implement the missing function so the supplied tests pass.",
            )
            == 0
        )
        capsys.readouterr()
        run_id = RunStore.list_run_ids()[-1]
        assert run_cli(monkeypatch, "auto", "run", run_id) == 0
        capsys.readouterr()
    finally:
        auto_commands.set_registry_factory(None)

    held = [
        Path(record.path)
        for record in RunStore.open(run_id).load().worktrees
        if record.removed_at is None
    ]
    assert held and all(path.exists() for path in held)

    before = {item.name: item for item in storage_usage()}
    assert before["worktrees"].reclaimable > 0

    result = diagnostics.reclaim()
    assert run_id in result.run_ids
    assert not result.failures
    assert all(not path.exists() for path in held)

    # The record survives. Only the rebuildable checkout is gone.
    assert RunStore.open(run_id).load().work_orders
    assert {item.name: item for item in storage_usage()}["worktrees"].reclaimable == 0
