"""Running one experiment on this machine.

The same execution discipline the acceptance checks already use: an argument
vector, no shell, an enforced timeout, standard input closed, output captured to
files rather than held in memory. What is different is only what is recorded
afterwards -- an experiment produces artifacts a result depends on, so the
executor's job does not end when the process does.

Two measurement honesty rules.

Wall clock is measured with a monotonic clock, because an experiment that
straddles a clock adjustment should not report a negative duration. CPU time and
peak memory come from ``resource.getrusage`` for the child process, which is a
real measurement on Linux; where the platform does not supply one the field stays
``None`` and the report says "unknown" rather than inventing a number that would
later be quoted.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from research_os.automation.models import utc_now
from research_os.experiment.models import (
    ExecutionState,
    ExecutorKind,
    ResourceUsage,
)
from research_os.experiment.spec import ResolvedCommand

#: How much of one stream is kept. Beyond this the file is truncated and says so.
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What an executor observed. Not yet a run record; the store makes that."""

    state: ExecutionState
    exit_code: int | None
    started_at: str
    ended_at: str
    usage: ResourceUsage
    stdout_path: str | None = None
    stderr_path: str | None = None
    failure_reason: str | None = None


def run_locally(
    command: ResolvedCommand,
    *,
    worktree: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int | None = None,
    environment: dict[str, str] | None = None,
) -> ExecutionOutcome:
    """Run one resolved command in ``worktree`` and record what happened."""

    timeout = timeout_seconds or command.timeout_seconds
    cwd = worktree
    if command.working_directory:
        cwd = worktree / command.working_directory
    if not cwd.is_dir():
        return _refused(f"the working directory {cwd} does not exist in this checkout")
    if shutil.which(command.argv[0]) is None:
        return _refused(f"{command.argv[0]} is not on PATH")

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    monotonic = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)

    state = ExecutionState.COMPLETED
    exit_code: int | None = None
    failure: str | None = None
    try:
        with (
            stdout_path.open("wb") as out,
            stderr_path.open("wb") as err,
        ):
            completed = subprocess.run(
                list(command.argv),
                cwd=str(cwd),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
                env=dict(environment) if environment is not None else None,
            )
        exit_code = completed.returncode
        if exit_code != 0:
            state = ExecutionState.FAILED
            failure = f"the command exited {exit_code}"
    except subprocess.TimeoutExpired:
        state = ExecutionState.TIMED_OUT
        failure = f"the experiment exceeded its {timeout}s timeout and was stopped"
    except OSError as exc:
        state = ExecutionState.FAILED
        failure = f"the command could not be started: {exc}"

    elapsed = time.monotonic() - monotonic
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    _truncate(stdout_path)
    _truncate(stderr_path)
    return ExecutionOutcome(
        state=state,
        exit_code=exit_code,
        started_at=started,
        ended_at=utc_now(),
        usage=_usage(before, after, elapsed),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        failure_reason=failure,
    )


def _usage(
    before: resource.struct_rusage,
    after: resource.struct_rusage,
    elapsed: float,
) -> ResourceUsage:
    """Return what this platform actually measured about the child process.

    ``RUSAGE_CHILDREN`` is cumulative over every child this process has reaped,
    so the difference is what this run cost -- provided no other child ran
    concurrently, which the controller does not do. ``ru_maxrss`` is the
    high-water mark across all children rather than a difference, so it is
    reported as the maximum observed rather than as this run's exact peak.
    """

    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    max_rss = after.ru_maxrss if after.ru_maxrss > 0 else None
    return ResourceUsage(
        wall_clock_seconds=round(elapsed, 3),
        cpu_seconds=round(cpu, 3) if cpu >= 0 else None,
        max_rss_kb=max_rss,
        cpu_count=os.cpu_count(),
        observed_by="resource.getrusage(RUSAGE_CHILDREN) on this machine",
    )


def _truncate(path: Path) -> None:
    """Keep a captured stream bounded, and say so in the file when it was cut."""

    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= MAX_CAPTURE_BYTES:
        return
    try:
        with path.open("rb+") as handle:
            handle.seek(MAX_CAPTURE_BYTES)
            handle.truncate()
            handle.write(b"\n[truncated by the controller]\n")
    except OSError:
        return


def _refused(reason: str) -> ExecutionOutcome:
    moment = utc_now()
    return ExecutionOutcome(
        state=ExecutionState.FAILED,
        exit_code=None,
        started_at=moment,
        ended_at=moment,
        usage=ResourceUsage(observed_by="nothing ran"),
        failure_reason=reason,
    )


LOCAL_KIND = ExecutorKind.LOCAL
