"""Adversarial tests for the planner acceptance-command grammar.

The controller runs acceptance commands itself, after a write-enabled worker has
edited the code, so a planner that can name the program can run code of its
choosing as the researcher. These tests are written from the attacker's side:
each one is a command a subverted or confused planner might emit, and the
assertion is that it never reaches ``subprocess.run``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.checks import run_acceptance_command
from research_os.automation.command_policy import (
    SUPPORTED_COMMAND_FORMS,
    SUPPORTED_PROGRAMS,
    authorize_planner_argv,
    authorize_planner_commands,
)
from research_os.automation.config import DEFAULT_ALLOWED_CHECK_PROGRAMS
from research_os.automation.models import AcceptanceCommand
from research_os.errors import CommandPolicyError

PROGRAMS = DEFAULT_ALLOWED_CHECK_PROGRAMS

REFUSED: tuple[tuple[str, ...], ...] = (
    ("python", "-c", "print(1)"),
    ("python3", "-c", "print(1)"),
    ("git", "push", "origin", "HEAD"),
    ("git", "commit", "-am", "x"),
    ("uv", "run", "python", "-c", "print(1)"),
    ("uv", "run", "python3", "-c", "print(1)"),
    ("uv", "run", "git", "push", "origin", "HEAD"),
    ("bash", "-c", "echo x"),
    ("sh", "-c", "echo x"),
    ("zsh", "-c", "echo x"),
    ("uv", "run", "bash", "-c", "echo x"),
    ("uv", "run", "sh", "-c", "echo x"),
    ("pytest;", "touch", "/tmp/pwned"),
    ("pytest", ";", "touch", "/tmp/pwned"),
    ("pytest", "&&", "curl", "example.com"),
    ("pytest", "|", "sh"),
    ("pytest", ">", "/etc/passwd"),
    ("pytest", "$(id)"),
    ("curl", "http://example.invalid"),
)

ALLOWED: tuple[tuple[str, ...], ...] = (
    ("pytest",),
    ("pytest", "-q"),
    ("pytest", "-q", "test_adder.py"),
    ("pytest", "-x", "--maxfail=1", "--tb=short"),
    ("pytest", "-q", "tests/test_adder.py::test_add_small"),
    ("ruff", "check", "."),
    ("ruff", "check", "--no-cache", "src", "tests"),
    ("ruff", "format", "--check", "."),
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ruff", "format", "--check", "."),
)


@pytest.mark.parametrize("argv", REFUSED, ids=lambda item: " ".join(item))
def test_a_planner_may_not_name_an_arbitrary_program(argv: tuple[str, ...]) -> None:
    with pytest.raises(CommandPolicyError):
        authorize_planner_argv(argv, allowed_programs=PROGRAMS)


@pytest.mark.parametrize("argv", ALLOWED, ids=lambda item: " ".join(item))
def test_the_supported_verification_commands_are_authorised(
    argv: tuple[str, ...],
) -> None:
    authorize_planner_argv(argv, allowed_programs=PROGRAMS)


def test_an_executable_outside_the_worktree_is_refused() -> None:
    """``../../bin/python`` is refused twice: as a path, and as a program."""

    with pytest.raises(ValueError, match="bare program name"):
        AcceptanceCommand(argv=["../../bin/python", "-c", "print(1)"])
    with pytest.raises(CommandPolicyError):
        authorize_planner_argv(
            ("../../bin/python", "-c", "print(1)"), allowed_programs=PROGRAMS
        )


@pytest.mark.parametrize(
    "target",
    ["/etc/passwd", "../outside", "../../outside", "~/secrets", "~"],
)
def test_a_path_argument_may_not_leave_the_worktree(target: str) -> None:
    with pytest.raises(CommandPolicyError):
        authorize_planner_argv(("pytest", target), allowed_programs=PROGRAMS)


@pytest.mark.parametrize(
    "argv",
    [
        ("pytest", "-p", "attacker_plugin"),
        ("pytest", "--rootdir=/"),
        ("pytest", "--maxfail=lots"),
        ("pytest", "--tb=exec"),
        ("ruff", "check", "--fix", "."),
        ("ruff", "format", "."),
        ("ruff", "lint", "."),
        ("ruff",),
        ("uv", "pytest"),
        ("uv", "run"),
        ("uv",),
        (),
    ],
    ids=lambda item: " ".join(item) or "empty",
)
def test_an_unrecognised_option_or_shape_is_refused(argv: tuple[str, ...]) -> None:
    """An option this grammar has no rule for is rejected, never passed through."""

    with pytest.raises(CommandPolicyError):
        authorize_planner_argv(argv, allowed_programs=PROGRAMS)


def test_configuration_may_narrow_the_grammar_but_never_widen_it() -> None:
    authorize_planner_argv(("ruff", "check", "."), allowed_programs=("ruff",))
    with pytest.raises(CommandPolicyError, match="allowed check programs"):
        authorize_planner_argv(("pytest", "-q"), allowed_programs=("ruff",))
    with pytest.raises(CommandPolicyError, match="allowed check programs"):
        authorize_planner_argv(("uv", "run", "pytest"), allowed_programs=("pytest",))

    # Naming a program the grammar has no rule for does not make it available.
    with pytest.raises(CommandPolicyError, match="not an authorised"):
        authorize_planner_argv(
            ("python", "-c", "print(1)"),
            allowed_programs=("python", "pytest", "ruff", "uv"),
        )


def test_the_program_allowlist_is_enforced_across_a_whole_plan() -> None:
    authorize_planner_commands(
        [AcceptanceCommand(argv=["pytest", "-q"])],
        ("pytest", "ruff"),
    )
    with pytest.raises(CommandPolicyError):
        authorize_planner_commands(
            [
                AcceptanceCommand(argv=["pytest", "-q"]),
                AcceptanceCommand(argv=["curl", "http://example.invalid"]),
            ],
            ("pytest", "ruff"),
        )


def test_a_refused_command_never_reaches_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate is before execution, not a check on the way out.

    The whole point of the policy is that an unauthorised argument vector is
    never handed to the operating system, so this asserts on ``subprocess.run``
    itself rather than on an exit code.
    """

    seen: list[list[str]] = []

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.append(list(argv))
        raise AssertionError(f"a refused command was executed: {argv}")

    monkeypatch.setattr(subprocess, "run", spy)

    marker = tmp_path / "pwned.txt"
    attack = AcceptanceCommand(
        argv=["python", "-c", f"open({str(marker)!r}, 'w').write('pwned')"]
    )
    with pytest.raises(CommandPolicyError):
        authorize_planner_argv(attack.argv, allowed_programs=PROGRAMS)
        run_acceptance_command(attack, cwd=tmp_path, timeout_seconds=5)

    assert seen == []
    assert not marker.exists()


def test_the_supported_forms_are_documented_for_the_planner() -> None:
    """The forms the planner is told about are the forms that are enforced."""

    assert SUPPORTED_PROGRAMS == ("uv", "pytest", "ruff")
    for form in SUPPORTED_COMMAND_FORMS:
        argv = tuple(token for token in form.split() if not token.startswith("["))
        authorize_planner_argv(argv, allowed_programs=PROGRAMS)
