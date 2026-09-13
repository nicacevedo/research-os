"""Isolated Git worktrees for write-enabled workers.

One writing worker, one dedicated worktree. The rule is enforced here, not
asked for in a prompt: the controller calls ``assert_isolated`` immediately
before every write invocation, and an exclusive on-disk lock makes a second
concurrent worker in the same checkout a hard failure rather than a race.

Worktrees are created under the Research OS state home, never inside the
researcher's repository, and are removed only by an explicit cleanup command --
``auto cleanup``, ``paper cleanup``, ``experiment cleanup``, or
``storage --reclaim`` -- so a failed run leaves its evidence in place.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from research_os.automation.gitutil import (
    add_worktree,
    branch_exists,
    remove_worktree,
    repository_root,
)
from research_os.automation.models import WorktreeRecord, utc_now
from research_os.automation.store import locks_root, worktrees_root
from research_os.errors import WorktreeError, WorktreeIsolationError


def branch_name(run_id: str, task_id: str) -> str:
    """Return the deterministic branch name for one task of one run."""

    return f"automation/{run_id.lower()}/{task_id.lower()}"


def worktree_path(run_id: str, task_id: str) -> Path:
    """Return the deterministic worktree path for one task of one run."""

    return worktrees_root() / run_id / task_id


def lock_path(target: Path) -> Path:
    """Return the lock file that guards one worktree path."""

    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
    return locks_root() / f"{digest}.lock"


def create_worktree(
    *,
    run_id: str,
    task_id: str,
    repository: Path,
    base_commit: str,
) -> WorktreeRecord:
    """Create and lock an isolated worktree for one write-enabled task."""

    target = worktree_path(run_id, task_id)
    branch = branch_name(run_id, task_id)
    canonical = repository_root(repository)

    if target.exists():
        raise WorktreeError(
            f"worktree path already exists: {target}; run "
            "'researchctl auto cleanup' for the owning run first"
        )
    if branch_exists(canonical, branch):
        raise WorktreeError(
            f"branch {branch} already exists in {canonical}; refusing to reuse it"
        )
    _assert_outside_repository(target, canonical)

    lock = _acquire_lock(target, run_id=run_id, task_id=task_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        add_worktree(
            repository=canonical,
            target=target,
            branch=branch,
            commit=base_commit,
        )
    except Exception:
        lock.unlink(missing_ok=True)
        raise

    record = WorktreeRecord(
        task_id=task_id,
        path=str(target),
        branch=branch,
        base_commit=base_commit,
        lock_path=str(lock),
        created_at=utc_now(),
    )
    assert_isolated(record, canonical_repository=canonical)
    return record


def assert_isolated(record: WorktreeRecord, *, canonical_repository: Path) -> None:
    """Raise unless ``record`` really is a separate, locked, live worktree.

    Checked immediately before every write invocation. A write worker that
    reached the researcher's own checkout would be able to modify canonical
    scientific files, so this is a guard, not a formality.
    """

    target = Path(record.path)
    canonical = canonical_repository.resolve()
    if not target.is_dir():
        raise WorktreeIsolationError(f"worktree does not exist: {target}")
    resolved = target.resolve()
    if resolved == canonical:
        raise WorktreeIsolationError(
            f"refusing to run a write-enabled worker in the canonical worktree "
            f"{canonical}"
        )
    _assert_outside_repository(target, canonical)
    try:
        toplevel = repository_root(resolved)
    except Exception as exc:  # pragma: no cover - surfaced as isolation failure
        raise WorktreeIsolationError(
            f"{resolved} is not a usable Git worktree: {exc}"
        ) from exc
    if toplevel != resolved:
        raise WorktreeIsolationError(
            f"{resolved} is not the root of its own worktree (root is {toplevel})"
        )
    lock = Path(record.lock_path)
    if not lock.is_file():
        raise WorktreeIsolationError(
            f"worktree lock {lock} is missing; refusing to run a write worker"
        )
    owner = _read_lock(lock)
    if owner.get("path") != str(target):
        raise WorktreeIsolationError(
            f"worktree lock {lock} belongs to {owner.get('path')}, not {target}"
        )


def release_worktree(record: WorktreeRecord, *, repository: Path) -> WorktreeRecord:
    """Remove the worktree directory and its lock, keeping the branch.

    The branch is deliberately kept: it holds whatever the worker produced, and
    discarding a failed attempt's evidence is not cleanup, it is deletion.
    """

    target = Path(record.path)
    canonical = repository_root(repository)
    if target.exists():
        _assert_outside_repository(target, canonical)
        remove_worktree(repository=canonical, target=target)
    Path(record.lock_path).unlink(missing_ok=True)
    return record.model_copy(update={"removed_at": utc_now()})


def _acquire_lock(target: Path, *, run_id: str, task_id: str) -> Path:
    """Take the exclusive lock for ``target`` or explain who already holds it."""

    lock = lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "run_id": run_id,
            "task_id": task_id,
            "path": str(target),
            "pid": os.getpid(),
            "created_at": utc_now(),
        },
        sort_keys=True,
    )
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        owner = _read_lock(lock)
        raise WorktreeError(
            f"worktree {target} is already owned by run "
            f"{owner.get('run_id', 'unknown')} task {owner.get('task_id', 'unknown')}; "
            "refusing a second concurrent write worker"
        ) from exc
    except OSError as exc:
        raise WorktreeError(f"cannot create worktree lock {lock}: {exc}") from exc
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")
    return lock


def _read_lock(lock: Path) -> dict[str, object]:
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _assert_outside_repository(target: Path, canonical: Path) -> None:
    """Reject a worktree inside the repository it is supposed to be isolated from."""

    resolved = target.resolve() if target.exists() else target
    if resolved == canonical or canonical in resolved.parents:
        raise WorktreeIsolationError(
            f"automation worktree {resolved} must live outside the project "
            f"repository {canonical}"
        )
