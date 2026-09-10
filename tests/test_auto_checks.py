"""Tests for controller-executed acceptance commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os.automation.checks import (
    assert_programs_allowed,
    run_acceptance_command,
    tail,
)
from research_os.automation.models import AcceptanceCommand


def test_a_passing_command_is_recorded_as_observed(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["python", "-c", "print('hello')"]),
        cwd=tmp_path,
        timeout_seconds=60,
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.cwd == str(tmp_path)
    assert result.required is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello\n"


def test_a_failing_command_is_not_ok(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["python", "-c", "raise SystemExit(3)"]),
        cwd=tmp_path,
        timeout_seconds=60,
    )

    assert result.ok is False
    assert result.exit_code == 3


def test_a_timeout_is_a_failure_with_no_exit_code(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["python", "-c", "import time; time.sleep(30)"]),
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.ok is False
    assert result.error is not None
    assert "timed out" in result.error


def test_a_missing_program_is_reported_rather_than_raised(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["definitely-not-a-real-program-xyz"]),
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert result.ok is False
    assert result.exit_code is None
    assert result.error is not None
    assert "not on PATH" in result.error


def test_an_advisory_command_records_its_own_requirement(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["python", "-c", "raise SystemExit(1)"], required=False),
        cwd=tmp_path,
        timeout_seconds=60,
    )

    assert result.required is False
    assert result.ok is False


def test_output_is_captured_to_the_named_artifacts(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(
            argv=["python", "-c", "import sys; sys.stderr.write('bad\\n')"]
        ),
        cwd=tmp_path,
        timeout_seconds=60,
        stdout_path=tmp_path / "o.txt",
        stderr_path=tmp_path / "e.txt",
    )

    assert result.stdout_path == str(tmp_path / "o.txt")
    assert (tmp_path / "e.txt").read_text(encoding="utf-8") == "bad\n"


def test_commands_run_in_the_directory_they_are_given(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here\n", encoding="utf-8")
    result = run_acceptance_command(
        AcceptanceCommand(
            argv=["python", "-c", "print(open('marker.txt').read().strip())"]
        ),
        cwd=tmp_path,
        timeout_seconds=60,
        stdout_path=tmp_path / "out.txt",
    )

    assert result.ok is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "here\n"


def test_shell_metacharacters_are_not_interpreted(tmp_path: Path) -> None:
    result = run_acceptance_command(
        AcceptanceCommand(argv=["python", "-c", "print('a && b')"]),
        cwd=tmp_path,
        timeout_seconds=60,
        stdout_path=tmp_path / "out.txt",
    )

    assert result.ok is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "a && b\n"


def test_the_program_allowlist_is_enforced() -> None:
    assert_programs_allowed(
        [AcceptanceCommand(argv=["pytest", "-q"])],
        ("pytest", "ruff"),
    )
    with pytest.raises(ValueError, match="not allowed"):
        assert_programs_allowed(
            [AcceptanceCommand(argv=["curl", "http://example.invalid"])],
            ("pytest", "ruff"),
        )


def test_tail_marks_what_it_dropped() -> None:
    assert tail("short", limit=10) == "short"
    trimmed = tail("x" * 50, limit=10)
    assert trimmed.startswith("[...truncated...]")
    assert trimmed.endswith("x" * 10)
