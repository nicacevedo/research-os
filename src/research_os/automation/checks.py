"""Deterministic acceptance-command execution.

The controller runs these itself. A worker reporting that the tests pass is a
claim; an exit code the controller observed is evidence, and only the second one
gates a run. Commands are argument vectors executed without a shell, inside the
task's own worktree, under an enforced timeout.

Whether a planner-originated command may run at all is decided in
``command_policy``, before anything here is called.

One command needs its execution environment placed for it. ``uv run``
materialises the project's environment before it runs anything, and by default
that is ``<project>/.venv`` - inside the worktree, holding interpreter symlinks
that point at the uv-managed Python outside it. Those are real outbound
symlinks, so the containment gate that runs before a repair worker is invoked
refuses them, and a work order whose checks use ``uv run`` becomes unrepairable
through no fault of the worker. The controller therefore tells uv where to put
the environment: somewhere the controller owns, outside every worktree.

The same command also resolves the project's dependency lock. ``uvlock`` decides
what that is allowed to do to the project; this module only carries the decision
into the environment uv is given.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from research_os.automation.models import AcceptanceCommand, CommandResult, utc_now
from research_os.automation.uvlock import UV_FROZEN

MAX_CAPTURE_CHARS = 200_000

#: The program whose project environment the controller relocates.
UV_PROGRAM = "uv"

#: The uv setting that names where a project environment is materialised.
UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"


def check_environment(
    argv: Sequence[str],
    *,
    uv_project_environment: Path | None,
    uv_frozen: bool = False,
) -> dict[str, str] | None:
    """Return the environment for one acceptance command, or ``None`` to inherit.

    Only a ``uv`` invocation is touched, and only to say where its project
    environment goes and whether it may rewrite the project's lock. Everything
    else - a bare ``pytest``, a bare ``ruff`` - runs in exactly the environment
    it ran in before, because nothing about it creates a directory inside the
    worktree or resolves a dependency set.

    The controller's decisions always win. An inherited
    ``UV_PROJECT_ENVIRONMENT`` or ``UV_FROZEN`` is a setting from the
    researcher's shell about the researcher's own work; neither may decide what
    an isolated work order does, so both are overwritten rather than respected.
    ``UV_FROZEN`` is explicitly *removed* when the controller did not ask for
    it, because a project with no lock file cannot run under it at all.
    """

    if uv_project_environment is None:
        return None
    if not argv or argv[0] != UV_PROGRAM:
        return None
    environment = dict(os.environ)
    environment[UV_PROJECT_ENVIRONMENT] = str(uv_project_environment)
    if uv_frozen:
        environment[UV_FROZEN] = "1"
    else:
        environment.pop(UV_FROZEN, None)
    return environment


def run_acceptance_command(
    command: AcceptanceCommand,
    *,
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    uv_project_environment: Path | None = None,
    uv_frozen: bool = False,
) -> CommandResult:
    """Run one acceptance command and record exactly what happened.

    ``uv_project_environment`` is where a ``uv`` command must materialise the
    project environment, and ``uv_frozen`` says whether it may resolve a new
    dependency lock. Both are ignored by every other program.
    """

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
            env=check_environment(
                argv,
                uv_project_environment=uv_project_environment,
                uv_frozen=uv_frozen,
            ),
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
