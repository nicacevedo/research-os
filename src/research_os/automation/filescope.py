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
import stat
from pathlib import Path, PurePosixPath
from typing import BinaryIO

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
    controller collects evidence, so a symlink that shipped in the base commit
    and one the worker left behind are both caught.

    What this does not catch, stated plainly because an independent reviewer
    had to point it out: a link created, written through, and deleted *during*
    the invocation is gone by the time the second scan runs. Closing that would
    need filesystem mediation this system does not have. The real bound on a
    write worker is its tool set -- file tools only, no command tool, enforced
    in :class:`~research_os.automation.providers.InvocationRequest` -- and the
    trusted-repository boundary documented in the README.
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def open_contained(root: Path, relative: str) -> BinaryIO | None:
    """Open ``root/relative`` for reading, if it is a file written *there*.

    The one containment rule every scientific reader applies to a directory a
    run could write -- a workspace, a run directory, a checkout. It began as
    the portfolio's ``_contained_output`` and lives here, beside the worktree
    link scan, so the runtime's readers and the v1 experiment route apply the
    same rule rather than one each. ``None`` for an absolute or escaping
    path, a path any component of which is a link, or anything that is not a
    regular file.

    **The check is the read.** The path is walked one component at a time
    with ``O_NOFOLLOW`` relative to the directory already opened, and the file
    is opened the same way, so a component replaced by a link between an
    earlier check and this read is refused by the kernel rather than
    followed. The earlier form asked ``is_symlink()`` and then reopened by
    path. ``O_NONBLOCK`` because a program can leave a FIFO where its log
    should be, and a blocking open of one waits for a writer that never comes.

    ``root`` itself is trusted and followed: it is a directory the host chose.
    """

    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or not (_NOFOLLOW and _DIRECTORY)
    ):
        return None
    descriptor = -1
    try:
        descriptor = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
        for part in pure.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        leaf = os.open(
            pure.parts[-1],
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=descriptor,
        )
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            os.close(leaf)
            return None
        return os.fdopen(leaf, "rb")
    except OSError:
        os.close(leaf)
        return None


def contained_file(root: Path, relative: str) -> Path | None:
    """``root/relative`` if :func:`open_contained` would open it, else ``None``.

    For a caller that needs the answer rather than the bytes. A caller that
    goes on to *read* the file should use :func:`open_contained` instead, or
    the path can change under it between the two.
    """

    handle = open_contained(root, relative)
    if handle is None:
        return None
    handle.close()
    return root / relative


def read_contained(root: Path, relative: str, *, max_bytes: int) -> bytes | None:
    """The bytes of a contained file no larger than ``max_bytes``, else ``None``."""

    handle = open_contained(root, relative)
    if handle is None:
        return None
    with handle:
        if os.fstat(handle.fileno()).st_size > max_bytes:
            return None
        data = handle.read(max_bytes + 1)
    return data if len(data) <= max_bytes else None
