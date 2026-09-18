"""The coding pipeline, end to end, inside real containment.

**Why this file exists.** The evidence that the acceptance path works under
containment was a transcript: a script somebody ran once, on one machine, that
was not in the repository. An audit pointed out what that costs a release gate
-- an unreproducible transcript cannot be re-run after the next change, cannot
be inspected for the proxy defects that had already been found twice in sibling
suites, and cannot fail. So it is here.

It is the path that broke. `run_acceptance_command` is where code this system
did not write gets executed, and the 112 failures that opened this release were
all downstream of it:

```text
bwrap: execvp pytest: No such file or directory     the executable closure
fatal: not a git repository: .../worktrees/T-001    the linked worktree
bwrap: Can't find source path .../env               the uv project environment
error: Failed to fetch https://pypi.org/simple/...  the cold cache
```

Four separate defects, one call site, and every one of them reported as the
project's fault. Each is asserted here against a real project, a real linked
worktree, a real virtual environment and real containment.

**`REQUIRED`, and `contained` is asserted.** The rest of the suite runs the
controller at its default `preferred`, where a silent degradation to uncontained
would leave every test passing. These run at `required` and check the flag.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.automation.checkprofiles import resolve_check_profiles
from research_os.automation.checks import run_acceptance_command, uv_cache_dir
from research_os.automation.config import DEFAULT_ALLOWED_CHECK_PROGRAMS
from research_os.automation.models import AcceptanceCommand
from research_os.sandbox import SandboxMode, available_backend, unavailable_reason

BACKEND = available_backend()
needs_sandbox = pytest.mark.skipif(
    BACKEND is None,
    reason=f"no containment technology is available here: {unavailable_reason()}",
)
needs_uv = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed on this machine"
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def project() -> Iterator[Path]:
    """A real uv project, committed, with its own virtual environment.

    Built outside ``/tmp``: the sandbox mounts its own tmpfs over ``/tmp``, and
    a project hidden inside for that reason would make every assertion below
    mean something other than what it says.
    """

    if shutil.which("uv") is None:
        pytest.skip("uv is not installed on this machine")
    base = Path("/var/tmp")
    if not base.is_dir() or not os.access(base, os.W_OK):  # pragma: no cover
        pytest.skip("this host has no writable /var/tmp to build a project in")
    root = Path(tempfile.mkdtemp(prefix="research-os-coding-", dir=base))
    checkout = root / "project"
    checkout.mkdir()
    try:
        import pytest as _pytest

        (checkout / "pyproject.toml").write_text(
            "[project]\n"
            'name = "contained-pipeline-fixture"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.12"\n'
            "dependencies = []\n\n"
            "[dependency-groups]\n"
            f'dev = ["pytest=={_pytest.__version__}", "ruff"]\n\n'
            "[tool.uv]\npackage = false\n",
            encoding="utf-8",
        )
        (checkout / "adder.py").write_text(
            '"""Deliberately incomplete."""\n\n\ndef add(left, right):\n'
            "    raise NotImplementedError\n",
            encoding="utf-8",
        )
        (checkout / "test_adder.py").write_text(
            "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        (checkout / ".gitignore").write_text(
            ".venv/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
        )
        _git(["init", "-q", "--initial-branch=main", "."], checkout)
        _git(["config", "user.email", "t@example.invalid"], checkout)
        _git(["config", "user.name", "T"], checkout)
        locked = subprocess.run(
            ["uv", "lock", "-q"], cwd=checkout, check=False, capture_output=True
        )
        if locked.returncode != 0:  # pragma: no cover - needs a registry or a cache
            pytest.skip(
                "uv could not resolve the fixture's dependencies here: "
                + (locked.stderr or b"").decode("utf-8", "replace")[-200:]
            )
        _git(["add", "-A"], checkout)
        _git(["commit", "-qm", "init"], checkout)
        synced = subprocess.run(
            ["uv", "sync", "-q", "--frozen"],
            cwd=checkout,
            check=False,
            capture_output=True,
        )
        if synced.returncode != 0:  # pragma: no cover
            pytest.skip("uv could not build the fixture's environment here")
        yield checkout
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def worktree(project: Path) -> Iterator[tuple[Path, Path]]:
    """A disposable linked worktree with the change a coder would have made."""

    target = project.parent / "worktrees" / "T-001"
    _git(["worktree", "add", "-q", str(target), "-b", "auto/T-001"], project)
    # `.git` in a linked worktree is a pointer file, not a directory. That is
    # the whole reason `git` inside containment needed work.
    assert (target / ".git").is_file()
    (target / "adder.py").write_text(
        '"""Completed by the worker."""\n\n\ndef add(left, right):\n'
        "    return left + right\n",
        encoding="utf-8",
    )
    environment = project.parent / "env" / "T-001"
    try:
        yield target, environment
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(target)],
            cwd=project,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", "auto/T-001"],
            cwd=project,
            check=False,
            capture_output=True,
        )
        shutil.rmtree(environment, ignore_errors=True)


def _run(argv: list[str], *, cwd: Path, environment: Path | None, tmp_path: Path):
    return run_acceptance_command(
        AcceptanceCommand(argv=argv, required=True),
        cwd=cwd,
        timeout_seconds=900,
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
        uv_project_environment=environment,
        uv_frozen=environment is not None,
        sandbox_mode=SandboxMode.REQUIRED,
    )


def _captured(tmp_path: Path) -> str:
    found = ""
    for name in ("out.txt", "err.txt"):
        path = tmp_path / name
        if path.is_file():
            found += path.read_text("utf-8", errors="replace")
    return found


@needs_sandbox
@needs_uv
def test_the_projects_discovered_checks_all_pass_contained(
    project: Path, worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """What production would actually run, at ``required``, all of it.

    The commands are not chosen here: `resolve_check_profiles` discovers them
    from the project's own metadata, exactly as the controller does.
    """

    target, environment = worktree
    tracked = frozenset(
        subprocess.run(
            ["git", "ls-files"],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    profiles, configured = resolve_check_profiles(
        tracked=tracked,
        dependencies=frozenset({"pytest", "ruff"}),
        tool_sections=frozenset(),
        allowed_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
    )
    assert not configured
    assert [list(item.argv) for item in profiles], "discovery proposed no checks"

    for profile in profiles:
        result = _run(
            list(profile.argv), cwd=target, environment=environment, tmp_path=tmp_path
        )
        assert result.contained is True, (profile.argv, result.containment)
        assert result.exit_code == 0, (
            profile.argv,
            result.error,
            _captured(tmp_path)[-1500:],
        )


@needs_sandbox
def test_a_bare_console_script_runs_where_it_used_to_vanish(
    worktree: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact failure this release opened with, through the production path.

    ``pytest`` resolves to ``<venv>/bin/pytest``, whose ``#!`` line names an
    interpreter in the same environment, which is a symbolic link into a
    uv-managed CPython somewhere else entirely. The acceptance-command grammar
    takes only a bare program name, which is why the failure only ever appeared
    as ``execvp pytest: No such file or directory`` for a file that existed.
    """

    target, _environment = worktree
    console = Path(sys.prefix) / "bin" / "pytest"
    if not console.is_file():  # pragma: no cover - depends on how tests were started
        pytest.skip("this environment has no console-script pytest to exercise")
    monkeypatch.setenv("PATH", f"{console.parent}:{os.environ['PATH']}")

    result = _run(["pytest", "-q"], cwd=target, environment=None, tmp_path=tmp_path)
    assert result.contained is True, result.containment
    assert result.exit_code == 0, (result.error, _captured(tmp_path)[-1500:])
    assert "1 passed" in _captured(tmp_path)


@needs_sandbox
def test_git_works_inside_a_linked_worktree_and_cannot_move_a_canonical_ref(
    project: Path, worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """Both halves of the second defect: it has to work, and only to read."""

    target, _environment = worktree
    before = subprocess.run(
        ["git", "show-ref"], cwd=project, check=True, capture_output=True, text=True
    ).stdout

    result = _run(
        ["git", "diff", "--check", "HEAD"],
        cwd=target,
        environment=None,
        tmp_path=tmp_path,
    )
    assert result.contained is True
    assert result.exit_code == 0, (result.error, _captured(tmp_path))

    attempt = _run(
        ["git", "update-ref", "refs/heads/main", "0" * 40],
        cwd=target,
        environment=None,
        tmp_path=tmp_path,
    )
    assert attempt.exit_code != 0, _captured(tmp_path)
    after = subprocess.run(
        ["git", "show-ref"], cwd=project, check=True, capture_output=True, text=True
    ).stdout
    assert after == before


@needs_sandbox
@needs_uv
def test_the_contained_run_installs_offline_and_leaves_the_host_cache_alone(
    worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """The overlay, measured where it matters: no network, nothing kept.

    A contained command is denied the network, so the only source of wheels is
    the researcher's own cache exposed as a throwaway overlay. If that is broken
    this check fails with a DNS error; if the overlay leaks, the host cache
    gains files.
    """

    cache = uv_cache_dir()
    if cache is None:  # pragma: no cover - depends on the machine
        pytest.skip("this host has no uv cache to expose")
    target, environment = worktree

    def inventory() -> set[str]:
        """Two levels deep, because uv writes into existing directories.

        An audit pointed out that a set of *top-level* names would not change
        when uv unpacked a new wheel into `archive-v0/<hash>/` -- so the check
        would have passed with the cache bound read-write, which is the exact
        regression this release calls its worst-ever defect.
        """

        found: set[str] = set()
        for entry in cache.iterdir():
            found.add(entry.name)
            if entry.is_dir():
                try:
                    found.update(
                        f"{entry.name}/{child.name}" for child in entry.iterdir()
                    )
                except OSError:  # pragma: no cover
                    continue
        return found

    before = inventory()

    result = _run(
        ["uv", "run", "pytest", "-q"],
        cwd=target,
        environment=environment,
        tmp_path=tmp_path,
    )
    assert result.contained is True, result.containment
    assert result.exit_code == 0, (result.error, _captured(tmp_path)[-1500:])
    assert "network denied" in (result.containment or "")
    assert inventory() == before


@needs_sandbox
@needs_uv
def test_the_projects_environment_is_never_writable_through_the_worktree(
    project: Path, worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """A writable ``site-packages`` is host code execution on the next run."""

    target, _environment = worktree
    victim = project / ".venv" / "lib"
    if not victim.is_dir():  # pragma: no cover
        pytest.skip("the fixture environment has no lib directory")
    planted = victim / "sitecustomize.py"

    result = _run(
        [
            "python",
            "-c",
            (
                "import pathlib\n"
                f"p = pathlib.Path({str(planted)!r})\n"
                "try:\n"
                "    p.write_text('import os\\n')\n"
                "    print('WROTE')\n"
                "except OSError as exc:\n"
                "    print('DENIED', type(exc).__name__)\n"
            ),
        ],
        cwd=target,
        environment=None,
        tmp_path=tmp_path,
    )
    assert result.contained is True
    seen = _captured(tmp_path)
    # The control. `contained` is set before the command runs, so without this
    # a probe that failed to start at all would satisfy every assertion below
    # while attempting nothing.
    assert "DENIED" in seen, seen
    assert "WROTE" not in seen, seen
    assert not planted.exists()


@needs_sandbox
def test_the_worktrees_own_git_pointer_cannot_be_rewritten_from_inside(
    worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """`.git` is a *file* here, and rewriting it aims the next run elsewhere.

    `LocalExecutor` has protected `.git` and `.research` since an adversarial
    review planted a hook through them. This path did not, and an independent
    review showed what that allowed: a contained command rewriting the pointer
    to name any other repository on the host.
    """

    target, _environment = worktree
    pointer = target / ".git"
    before = pointer.read_text("utf-8")

    result = _run(
        [
            "python",
            "-c",
            (
                "import pathlib\n"
                f"p = pathlib.Path({str(pointer)!r})\n"
                "try:\n"
                "    p.write_text('gitdir: /elsewhere/.git\\n')\n"
                "    print('WROTE')\n"
                "except OSError as exc:\n"
                "    print('DENIED', type(exc).__name__)\n"
            ),
        ],
        cwd=target,
        environment=None,
        tmp_path=tmp_path,
    )
    assert result.contained is True
    seen = _captured(tmp_path)
    assert "DENIED" in seen, seen
    assert "WROTE" not in seen, seen
    assert pointer.read_text("utf-8") == before
