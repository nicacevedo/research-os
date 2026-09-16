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
from research_os.sandbox import (
    SandboxMode,
    SandboxSpec,
    contain,
    process_limit_preexec,
)

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


def check_overrides(
    argv: Sequence[str],
    *,
    uv_project_environment: Path | None,
    uv_frozen: bool = False,
) -> dict[str, str]:
    """Return only the variables the *controller decided*, not the environment.

    The difference from :func:`check_environment` matters exactly once, and it
    is the difference between a sandbox and a decoration.
    ``check_environment`` returns ``dict(os.environ)`` plus the controller's
    settings, because the uncontained path needs a complete environment to hand
    to ``subprocess.run``. Handing that same dictionary to the sandbox would put
    the researcher's entire environment -- their provider keys, their SSH agent
    socket, their tokens -- *inside* the sandbox, which is the one thing it
    exists to prevent.

    So the contained path uses this: the deltas alone, added to the sandbox's
    own allowlist.
    """

    if uv_project_environment is None or not argv or argv[0] != UV_PROGRAM:
        return {}
    overrides = {UV_PROJECT_ENVIRONMENT: str(uv_project_environment)}
    if uv_frozen:
        overrides[UV_FROZEN] = "1"
    return overrides


def uv_support_paths(argv: Sequence[str]) -> tuple[Path, ...]:
    """The directories ``uv`` needs to write, for a contained ``uv`` command.

    ``uv run`` does not only read a project environment. It reads and writes its
    package cache, and on a project whose ``.python-version`` names an
    interpreter it manages, it reads that interpreter out of its own data
    directory. Both live under the researcher's home, which the sandbox replaces
    with an empty tmpfs -- so a contained ``uv run --frozen pytest`` with a
    complete project environment still failed, because the first thing uv does
    is open its cache.

    The honest reading of what this grants: uv's cache and managed-interpreter
    directories become writable inside the sandbox. They are already writable by
    this user outside it, and nothing secret lives there. What the sandbox is
    keeping out of reach is the researcher's environment, their credentials,
    their ``.git`` and ``.research``, and the network; a package cache is not on
    that list, and pretending otherwise would mean containment that cannot run
    the checks it exists to contain.

    Only for ``uv``. Every other program gets nothing, because every other
    program's needs are the caller's to declare.
    """

    if not argv or argv[0] != UV_PROGRAM:
        return ()
    located = shutil.which(UV_PROGRAM)
    if located is None:
        return ()
    # uv's own documented locations, resolved the way uv resolves them: the
    # explicit variable first, then the XDG directory, then the default.
    explicit = os.environ.get("UV_CACHE_DIR")
    if explicit:
        cache_dir = Path(explicit)
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
        cache_dir = base / "uv"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    data_dir = (Path(xdg_data) if xdg_data else Path.home() / ".local" / "share") / "uv"
    return tuple(path for path in (cache_dir, data_dir) if path.exists())


def run_acceptance_command(
    command: AcceptanceCommand,
    *,
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    uv_project_environment: Path | None = None,
    uv_frozen: bool = False,
    sandbox_mode: SandboxMode = SandboxMode.OFF,
    sandbox_readable: Sequence[Path] = (),
    sandbox_network: bool = False,
) -> CommandResult:
    """Run one acceptance command and record exactly what happened.

    ``uv_project_environment`` is where a ``uv`` command must materialise the
    project environment, and ``uv_frozen`` says whether it may resolve a new
    dependency lock. Both are ignored by every other program.

    ``sandbox_mode`` decides whether this runs inside OS-level containment. This
    is the choke point for it, and that is the whole reason it exists here
    rather than in the controller: **this function is where code the system did
    not write gets executed.** The command runs after a write-enabled worker has
    edited files in scope, so ``pytest`` imports Python a model wrote one step
    earlier. Everything above this call decides *whether* to run it; this is
    where it happens, so this is where containment belongs.

    ``sandbox_readable`` are paths the command needs to *read* and the sandbox
    would otherwise deny -- a shared dependency cache, an input artifact. The
    worktree and the uv project environment are writable and are derived here,
    because getting either wrong turns every check into a failure that looks
    like the project's.

    Under ``required`` a host that cannot contain raises
    :class:`~research_os.sandbox.SandboxError`, which the caller reports rather
    than running the command anyway.
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

    inner_environment = check_environment(
        argv,
        uv_project_environment=uv_project_environment,
        uv_frozen=uv_frozen,
    )
    writable = [cwd]
    if uv_project_environment is not None:
        # uv materialises the project environment here, so it has to be
        # writable. It is outside every worktree on purpose -- see this module's
        # docstring -- which means the sandbox would deny it by default and
        # every `uv run` check would fail for a reason that has nothing to do
        # with the project.
        writable.append(uv_project_environment)
    writable.extend(uv_support_paths(argv))
    spec = SandboxSpec(
        workdir=cwd,
        writable=tuple(writable),
        readable=tuple(sandbox_readable),
        network=sandbox_network,
        wall_seconds=timeout_seconds,
        # The controller's *decisions*, not the researcher's environment.
        # `check_environment` returns `dict(os.environ)` plus those decisions
        # because the uncontained path needs a complete environment to hand to
        # `subprocess.run`; handing the same dictionary to the sandbox would put
        # the researcher's provider keys and SSH agent socket inside it, which
        # is the one thing it exists to prevent. See `check_overrides`.
        environment=check_overrides(
            argv,
            uv_project_environment=uv_project_environment,
            uv_frozen=uv_frozen,
        ),
    )
    prepared = contain(argv, spec=spec, mode=sandbox_mode)

    try:
        completed = subprocess.run(
            list(prepared.argv),
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            # Replace rather than raise. One byte of invalid UTF-8 from a check
            # -- `printf '\377\376'` -- used to raise `UnicodeDecodeError` out
            # of this function. That is a `ValueError`, caught by neither the
            # controller's `except (AutomationError, SandboxError)` nor the
            # runtime action's handlers, so the run was never marked terminal
            # and the worktree, the branch and the in-flight run were left
            # behind. Found by an adversarial review, which executed it.
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=prepared.environment if prepared.contained else inner_environment,
            # A session of its own, so the command has no controlling terminal.
            # The contained path gets this from `--new-session`; the uncontained
            # path -- the live one wherever containment is unavailable -- had
            # nothing, and an adversarial review opened `/dev/tty` from an
            # acceptance command and repainted the researcher's terminal while
            # stdout captured something innocuous. `terminal_safe` never sees
            # those bytes, because they never pass through this process.
            start_new_session=True,
            # Only when contained; `RLIMIT_NPROC` is uid-scoped, so applying it
            # to an uncontained child caps the researcher's whole session.
            preexec_fn=process_limit_preexec(spec, contained=prepared.contained),
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
        # The command as the *project* declared it, not the sandbox wrapper.
        # A researcher reading a run wants to see `uv run pytest -q`; the
        # containment is reported in its own field rather than smuggled into
        # the one that says what was run.
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
        contained=prepared.contained,
        containment=f"{prepared.technology}: {prepared.detail}",
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
