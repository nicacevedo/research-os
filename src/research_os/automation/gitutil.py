"""Deterministic Git inspection and worktree operations.

None of this is model work. Branch names, commit shas, dirty state, diffs, and
worktree creation are facts the controller establishes itself; asking a model
for any of them would replace a certainty with a guess.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from research_os.errors import GitError

GIT_TIMEOUT_SECONDS = 120


#: Configuration a repository must not be able to set for a command we run.
#:
#: Git is a code-execution primitive in a directory somebody else can write.
#: ``core.fsmonitor``, ``diff.external``, ``core.pager`` and a hooks path are
#: programs git will run, and every one of them can be set by a `.git/config`
#: inside a worktree a write-enabled worker has been given.
#:
#: An adversarial review executed the whole chain: a contained acceptance
#: command rewrote `<worktree>/.git` -- which is a *file* holding
#: ``gitdir: ...``, inside the bind the sandbox correctly makes writable -- to
#: point at a gitdir it built itself, whose config set ``core.fsmonitor``. The
#: controller then ran ``git add --intent-to-add`` and ``git diff HEAD`` in that
#: worktree, on the host, with no containment, and git executed the program.
#: Output: "HOST CODE EXECUTION as <user>, keys visible: 2".
#:
#: The redirect is invisible to the scope check, because ``git diff --name-only``
#: cannot report a change inside `.git`, and invisible to the symlink check,
#: because a gitdir pointer is not a symlink.
#:
#: So every git command this module runs is run with these emptied. `-c` beats
#: the repository's own config, and the two `GIT_CONFIG_*` variables remove the
#: user's and the system's.
_NEUTRALISED_CONFIG: tuple[str, ...] = (
    "core.fsmonitor=",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "core.sshCommand=",
    "protocol.ext.allow=never",
    "uploadpack.packObjectsHook=",
)

#: Subcommands that take ``--no-ext-diff``, and get it.
#:
#: ``diff.external`` is the one hostile setting ``-c`` cannot neutralise:
#: ``-c diff.external=`` makes git try to *run* the empty string
#: (``error: cannot run : No such file or directory``) rather than disabling the
#: feature. Measured, not assumed. The subcommand flag is the correct
#: incantation and was verified to defeat a repository-set ``diff.external``.
_NO_EXTERNAL_DIFF: frozenset[str] = frozenset({"diff", "log", "show"})


def git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one Git command with no shell, no inherited stdin, and no hooks.

    "No hooks" is doing real work and is not a tidiness measure: several of the
    settings a repository can put in its own config name programs git will then
    execute, and this module runs git inside worktrees that a write-enabled
    worker has just been editing. See :data:`_NEUTRALISED_CONFIG`.
    """

    argv, environment = _invocation(args)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=environment,
            start_new_session=True,
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


def git_bytes(
    args: list[str],
    *,
    cwd: Path,
    stdin: bytes = b"",
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> bytes | None:
    """Run one Git command as :func:`git` does, on bytes; ``None`` if it failed.

    For content, where decoding would change what is compared: an object's
    bytes, or bytes to hash. Same neutralised configuration, same environment.
    """

    argv, environment = _invocation(args)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            input=stdin,
            timeout=timeout,
            env=environment,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    return completed.stdout if completed.returncode == 0 else None


def _invocation(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """The argv and environment every command in this module runs with."""

    environment = {
        **os.environ,
        # The user's and the system's config, removed. A worker cannot write
        # either, but a command run with them is a command whose behaviour
        # depends on the researcher's machine rather than on the repository.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        # `git` runs the ssh binary for a remote operation. Nothing here does
        # one, and an empty command fails loudly rather than reaching the
        # researcher's agent.
        "GIT_SSH_COMMAND": "",
    }
    neutralised: list[str] = []
    for setting in _NEUTRALISED_CONFIG:
        neutralised.extend(["-c", setting])
    invocation = list(args)
    if invocation and invocation[0] in _NO_EXTERNAL_DIFF:
        invocation.insert(1, "--no-ext-diff")
    return ["git", *neutralised, *invocation], environment


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
