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
    SandboxPreparationError,
    SandboxSpec,
    contain,
    linked_worktree_paths,
    process_limit_preexec,
)

MAX_CAPTURE_CHARS = 200_000

#: The program whose project environment the controller relocates.
UV_PROGRAM = "uv"

#: The uv setting that names where a project environment is materialised.
UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"

#: The uv setting that names where its package cache lives.
UV_CACHE_DIR = "UV_CACHE_DIR"

#: The uv setting that says there is no network, so use the cache or fail.
UV_OFFLINE = "UV_OFFLINE"


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
    network: bool = True,
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
    if not network:
        # The sandbox denies the network, so say so rather than letting uv
        # discover it. Told, uv resolves from its cache; not told, it attempts
        # the index, retries three times and fails the check with
        #
        #   error: Request failed after 3 retries ... dns error
        #
        # which is a network diagnosis of a containment decision, attributed to
        # the project. It is also what the sandbox already promised: "a command
        # that silently fetches something is a command whose result is not
        # reproducible" -- and a command that *cannot* fetch should not spend
        # fifteen seconds trying.
        overrides[UV_OFFLINE] = "1"
    cache = uv_cache_dir()
    if cache is not None:
        # Point uv at the cache this command is given as a throwaway overlay --
        # see `uv_discarded_paths`. Without this uv uses the sandbox's own
        # tmpfs HOME, which is empty, and an empty cache with no network cannot
        # install anything.
        overrides[UV_CACHE_DIR] = str(cache)
    return overrides


def uv_readonly_paths(argv: Sequence[str]) -> tuple[Path, ...]:
    """The directories a contained ``uv`` command needs to **read**.

    One: uv's data directory, which holds the interpreters it manages. A project
    whose ``.python-version`` names one of those needs it present, and needs
    only to read it.

    **This used to return the package cache too, and put both in
    ``writable``.** A final adversarial review executed what that granted, and
    it was the worst defect in this release: ``--bind`` (read-write) on
    ``~/.local/share/uv``, whose managed interpreter is what nine virtual
    environments on this machine execute, with its ``lib/python3.12`` directory
    writable by this user. Contained model-written code drops a
    ``sitecustomize.py`` there and the next ``uv run`` *anywhere on the host* --
    the daemon's own, ``researchctl`` itself, the researcher's shell -- executes
    it outside the sandbox with the full environment. Strictly worse than the
    ``.git``-hook escape an earlier review found, because
    ``canonical_fingerprint`` sees none of it, nothing has to be checked out for
    it to fire, and it reaches every project on the machine.

    The cache is not in this list, because a read-only cache does not work:
    measured, uv exits with ``Failed to initialize cache ... Permission
    denied`` before running anything. It is handled by `uv_discarded_paths`
    instead, which layers a throwaway tmpfs over it.

    **An earlier version of this paragraph said the cache was simply dropped**,
    and that uv would use the sandbox's tmpfs ``HOME`` -- "verified to work from
    empty with ``UV_OFFLINE=1``", at a cost of "a cold cache per contained run.
    That is the correct price." The verification did not cover a project with a
    dependency. A cold cache and no network cannot install one, so the price
    was not a slower run, it was every ``uv run`` check failing with a DNS
    error attributed to the project. Left here rather than deleted: the
    measurement was real and the conclusion drawn from it was too broad, which
    is a failure worth being able to recognise again.
    """

    if not argv or argv[0] != UV_PROGRAM:
        return ()
    if shutil.which(UV_PROGRAM) is None:
        return ()
    xdg_data = os.environ.get("XDG_DATA_HOME")
    data_dir = (Path(xdg_data) if xdg_data else Path.home() / ".local" / "share") / "uv"
    return (data_dir,) if data_dir.exists() else ()


def uv_cache_dir() -> Path | None:
    """Where uv keeps downloaded packages on this host, if it exists.

    ``XDG_CACHE_HOME`` when set, ``~/.cache/uv`` otherwise, which is uv's own
    rule. ``None`` when there is nothing there to expose.
    """

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    found = (Path(xdg_cache) if xdg_cache else Path.home() / ".cache") / "uv"
    return found if found.is_dir() else None


def uv_discarded_paths(argv: Sequence[str]) -> tuple[Path, ...]:
    """uv's package cache, to be exposed with every write thrown away.

    **This is what makes a contained ``uv run`` possible at all.** A contained
    command has no network, so uv can only install from a cache; and it will
    not use a read-only one -- measured, it exits with ``Failed to initialize
    cache ... Permission denied`` before running anything. The previous release
    concluded from that measurement that the cache should not be exposed, and
    recorded that uv would use the sandbox's tmpfs ``HOME`` instead, "verified
    to work from empty with ``UV_OFFLINE=1``".

    That verification did not cover a project with a dependency, and this
    release measured what happens when it does:

    ```text
    error: Unable to ... Failed to fetch `https://pypi.org/simple/pytest/`
           ... failed to lookup address information
    ```

    Every ``uv run`` acceptance check failed that way, attributed to the
    project. A lock file means dependency resolution does not need the network;
    it does not put the wheels on disk.

    So the cache goes in :attr:`SandboxSpec.discarded`: bubblewrap layers a
    tmpfs over it, uv can initialise and populate what it sees, and the whole
    upper layer is destroyed with the sandbox. Verified: five packages
    installed with the network denied, and nothing new in the host cache
    afterwards. It is not the read-write bind an earlier review found to be a
    host code-execution escape -- no write survives the run for anything to
    execute.

    What it does expose is *read* access to the researcher's package cache:
    wheels, source distributions and their unpacked contents. That is package
    data rather than secrets, and it is strictly less than the managed
    interpreters `uv_readonly_paths` already exposes.
    """

    if not argv or argv[0] != UV_PROGRAM:
        return ()
    if shutil.which(UV_PROGRAM) is None:
        return ()
    cache = uv_cache_dir()
    return (cache,) if cache is not None else ()


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
        # The controller owns this directory and uv creates it on first use --
        # which under containment is too late, because a bind mount needs a
        # source that already exists. Created here rather than in the sandbox:
        # a sandbox that makes host directories in order to contain something
        # is a sandbox with a write of its own.
        uv_project_environment.mkdir(parents=True, exist_ok=True)
        # uv materialises the project environment here, so it has to be
        # writable. It is outside every worktree on purpose -- see this module's
        # docstring -- which means the sandbox would deny it by default and
        # every `uv run` check would fail for a reason that has nothing to do
        # with the project.
        writable.append(uv_project_environment)
    spec = SandboxSpec(
        workdir=cwd,
        writable=tuple(writable),
        # uv's managed interpreters and the repository a linked worktree
        # belongs to, both read-only. Never writable: see `uv_readonly_paths`
        # and `linked_worktree_paths`. Every acceptance command in this
        # pipeline runs inside a `git worktree add` checkout, so a project
        # whose checks include `git diff --check` needs the second one or the
        # check fails with "not a git repository" against a repository that is
        # fine.
        readable=(
            *sandbox_readable,
            *uv_readonly_paths(argv),
            *linked_worktree_paths(cwd),
        ),
        # uv's package cache, readable and writable-in-appearance, with every
        # write discarded when the sandbox exits. See `uv_discarded_paths`.
        discarded=uv_discarded_paths(argv),
        # The two things inside the worktree an acceptance command must not be
        # able to write, whatever else it may. `LocalExecutor` has protected
        # these since an adversarial review planted a `post-checkout` hook from
        # inside "containment"; this path did not, and the asymmetry was the
        # enabler for a second review's finding: `.git` in a linked worktree is
        # a *pointer file*, so a contained command could rewrite it to name any
        # other repository on the host and have it exposed on the next run.
        protected=(cwd / ".git", cwd / ".research"),
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
            network=sandbox_network,
        ),
    )
    try:
        prepared = contain(argv, spec=spec, mode=sandbox_mode)
    except SandboxPreparationError as exc:
        # Containment is available; *this command* could not be set up inside
        # it -- a `#!` line naming an interpreter that does not exist, or one
        # naming a path policy will not expose. Reported against the command,
        # with the reason, rather than raised out of the function: a host that
        # cannot contain at all is a different fact and keeps raising.
        #
        # The distinction is the whole point of the message. Without it the
        # command runs and the kernel says `No such file or directory` naming
        # the script, which exists, and the missing interpreter is never named.
        return CommandResult(
            argv=argv,
            cwd=str(cwd),
            required=command.required,
            exit_code=None,
            timed_out=False,
            timeout_seconds=timeout_seconds,
            started_at=started,
            ended_at=utc_now(),
            duration_ms=int((time.monotonic() - monotonic) * 1000),
            error=f"the sandbox could not be prepared for this command: {exc}",
        )

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
