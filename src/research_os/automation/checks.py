"""Deterministic acceptance-command execution.

The controller runs these itself. A worker reporting that the tests pass is a
claim; an exit code the controller observed is evidence, and only the second one
gates a run. Commands are argument vectors executed without a shell, inside the
task's own worktree, under an enforced timeout.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from research_os.automation.models import AcceptanceCommand, CommandResult, utc_now

MAX_CAPTURE_CHARS = 200_000


def assert_programs_allowed(
    commands: list[AcceptanceCommand],
    allowed: tuple[str, ...],
) -> None:
    """Raise ``ValueError`` naming the first command outside the allowlist.

    The controller executes these commands, so the set of programs a plan may
    name is policy, not a suggestion from the planner.
    """

    for command in commands:
        program = command.argv[0]
        if program not in allowed:
            raise ValueError(
                f"acceptance command program {program!r} is not allowed; "
                f"permitted programs: {', '.join(sorted(allowed))}"
            )


def run_acceptance_command(
    command: AcceptanceCommand,
    *,
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> CommandResult:
    """Run one acceptance command and record exactly what happened."""

    started = utc_now()
    monotonic = time.monotonic()
    argv = list(command.argv)

    if shutil.which(argv[0]) is None:
        return CommandResult(
            argv=argv,
            cwd=str(cwd),
            required=command.required,
            exit_code=None,
            timed_out=False,
            timeout_seconds=timeout_seconds,
            started_at=started,
            ended_at=utc_now(),
            duration_ms=0,
            error=f"{argv[0]} is not on PATH",
        )

    timed_out = False
    error: str | None = None
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        error = f"command timed out after {timeout_seconds}s"
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
    except OSError as exc:
        error = f"command could not be started: {exc}"

    duration_ms = int((time.monotonic() - monotonic) * 1000)
    stored_stdout = _store(stdout_path, stdout)
    stored_stderr = _store(stderr_path, stderr)

    return CommandResult(
        argv=argv,
        cwd=str(cwd),
        required=command.required,
        exit_code=exit_code,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        started_at=started,
        ended_at=utc_now(),
        duration_ms=duration_ms,
        stdout_path=stored_stdout,
        stderr_path=stored_stderr,
        error=error,
    )


def tail(text: str, limit: int = 4000) -> str:
    """Return the last ``limit`` characters, marked when anything was dropped."""

    if len(text) <= limit:
        return text
    return "[...truncated...]\n" + text[-limit:]


def _store(path: Path | None, text: str) -> str | None:
    if path is None:
        return None
    payload = text
    if len(payload) > MAX_CAPTURE_CHARS:
        payload = payload[:MAX_CAPTURE_CHARS] + "\n[truncated by the controller]\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return str(path)


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""
