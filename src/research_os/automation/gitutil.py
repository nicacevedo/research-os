"""Deterministic Git inspection and worktree operations.

None of this is model work. Branch names, commit shas, dirty state, diffs, and
worktree creation are facts the controller establishes itself; asking a model
for any of them would replace a certainty with a guess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from research_os.errors import GitError

GIT_TIMEOUT_SECONDS = 120


def git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one Git command with no shell and no inherited standard input."""

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and completed.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {cwd} "
            f"({completed.returncode}): {completed.stderr.strip()}"
        )
    return completed


def repository_root(path: Path) -> Path:
    """Return the Git repository root containing ``path``."""

    completed = git(["rev-parse", "--show-toplevel"], cwd=path)
    return Path(completed.stdout.strip()).resolve()


def head_commit(path: Path) -> str:
    """Return the full 40-character HEAD commit sha."""

    return git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def has_commits(path: Path) -> bool:
    """Return whether the repository has any commit yet.

    A freshly initialised repository has an unborn HEAD, so ``rev-parse HEAD``
    fails there. Callers that need a base commit check this first and say so,
    rather than surfacing Git's "ambiguous argument 'HEAD'".
    """

    completed = git(["rev-parse", "--verify", "HEAD"], cwd=path, check=False)
    return completed.returncode == 0


def current_branch(path: Path) -> str | None:
    """Return the checked-out branch name, or ``None`` on a detached HEAD."""

    name = git(["branch", "--show-current"], cwd=path).stdout.strip()
    return name or None


def porcelain_status(path: Path) -> tuple[str, ...]:
    """Return ``git status --porcelain`` lines, empty when the tree is clean."""

    completed = git(["status", "--porcelain"], cwd=path)
    return tuple(line for line in completed.stdout.splitlines() if line.strip())


def is_dirty(path: Path) -> bool:
    return bool(porcelain_status(path))


def commit_exists(path: Path, commit: str) -> bool:
    completed = git(
        ["cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=path,
        check=False,
    )
    return completed.returncode == 0


def branch_exists(path: Path, branch: str) -> bool:
    completed = git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=path,
        check=False,
    )
    return completed.returncode == 0


def add_worktree(*, repository: Path, target: Path, branch: str, commit: str) -> None:
    """Create a new worktree at ``target`` on a new ``branch`` based on ``commit``."""

    git(
        ["worktree", "add", "-b", branch, str(target), commit],
        cwd=repository,
    )


def remove_worktree(*, repository: Path, target: Path, force: bool = True) -> None:
    """Remove a worktree, leaving its branch and any commits intact."""

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    git(args, cwd=repository)


def stage_intent_to_add(path: Path) -> None:
    """Record untracked files in the index so ``git diff`` shows them.

    Without this a newly created file is invisible to ``git diff`` and a worker
    could satisfy a task with changes the reviewer never sees. Only ever run in
    an automation worktree, and it stages intent rather than content, so nothing
    is committed.
    """

    git(["add", "--intent-to-add", "--", "."], cwd=path)


def working_diff(path: Path) -> str:
    """Return the full unified diff of the working tree against HEAD."""

    return git(["diff", "HEAD"], cwd=path).stdout


def working_diff_stat(path: Path) -> str:
    return git(["diff", "--stat", "HEAD"], cwd=path).stdout


def changed_paths(path: Path) -> tuple[str, ...]:
    """Return every path that differs from HEAD, tracked or newly added."""

    completed = git(["diff", "--name-only", "HEAD"], cwd=path)
    tracked = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    untracked = git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=path,
    ).stdout.splitlines()
    combined = {*tracked, *(line.strip() for line in untracked if line.strip())}
    return tuple(sorted(combined))
