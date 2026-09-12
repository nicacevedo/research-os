"""Symlink containment for the isolated worktree.

Worktree isolation is a Git fact, not a filesystem one. A worktree can contain a
symlink whose target is outside it, and a writer told to change that in-scope
path writes straight through the link: the file outside is mutated, the link
itself is unchanged, so ``git diff`` reports nothing and scope enforcement --
which reads Git -- sees a clean, in-scope run.

The MVP rule is therefore conservative and structural rather than clever: if any
symlink anywhere in the worktree resolves outside the real worktree root, the
work order is refused. That covers every allowed path, every parent component of
every allowed path, and every symlink beneath an allowed directory, without
having to reason about which of them a worker will actually touch.

The scan never follows a symlinked directory, so a link pointing at ``/`` costs
one ``lstat`` rather than a walk of the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path

from research_os.errors import SymlinkScopeError


def outbound_symlinks(worktree: Path) -> tuple[str, ...]:
    """Return every symlink in ``worktree`` that resolves outside it.

    Paths are returned relative to the worktree and sorted, so the same tree
    always produces the same evidence. A broken symlink is judged by where it
    points, not by whether that target exists yet: a dangling link out of the
    worktree becomes a live escape as soon as something creates the target.
    """

    try:
        root = worktree.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SymlinkScopeError(f"cannot inspect worktree {worktree}: {exc}") from exc

    offenders: list[str] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        here = Path(directory)
        for name in (*subdirectories, *files):
            candidate = here / name
            if not candidate.is_symlink():
                continue
            if not _is_contained(candidate, root):
                offenders.append(str(candidate.relative_to(root)))
    return tuple(sorted(offenders))


def assert_contained_symlinks(worktree: Path) -> None:
    """Raise ``SymlinkScopeError`` if any symlink escapes ``worktree``.

    Called before a write-enabled worker is invoked and again when the
    controller collects evidence, so neither a symlink that shipped in the base
    commit nor one the worker created can carry a write out of the worktree.
    """

    escaping = outbound_symlinks(worktree)
    if escaping:
        raise SymlinkScopeError(
            f"{len(escaping)} symlink(s) in {worktree} resolve outside it, so a "
            "write to an in-scope path could land outside the isolated "
            f"worktree: {', '.join(escaping)}"
        )


def _is_contained(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` resolves to ``root`` or something beneath it.

    A link the operating system cannot resolve at all -- a loop, a path too
    long, a permission failure -- counts as not contained. A symlink the
    controller cannot reason about is exactly the one it must not let a writer
    follow.
    """

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents
