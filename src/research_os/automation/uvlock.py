"""Keeping the controller's own checks out of the project's dependency state.

``uv run`` materialises a project environment before it runs anything, and it
resolves a lock file to do so. Left alone it will happily write ``uv.lock`` into
the tree it is standing in. That tree is an isolated worktree, but it is still
the researcher's project: a lock the controller created while *checking* a
change looks exactly like a lock the worker wrote, so it lands in the diff a
reviewer reads and in the scope the controller enforces, and neither of those
should be describing something the controller did.

The rule here is therefore about the project's dependency state rather than
about a filename:

* **A lock that was already there is never rewritten.** ``UV_FROZEN`` is set for
  the check, which is uv's documented "run without updating the lockfile" mode,
  so uv resolves from the existing file and leaves it alone. The bytes are
  recorded before and re-established afterwards, so even a uv that ignored the
  setting cannot change the project's pinned dependencies through a check.
* **A lock that was not there does not survive the check.** ``UV_FROZEN`` cannot
  be used - uv refuses it outright when there is no lock to freeze - so uv is
  allowed to resolve, and the file it created is removed once the check sequence
  has finished. The removal is recorded, so a run says plainly that it resolved
  dependencies rather than leaving an unexplained artifact behind.

Nothing here weakens command authorisation. The acceptance command is the
argument vector the policy already approved; this only decides the environment
it runs in and what the worktree looks like afterwards.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

#: The file uv writes a project's resolved dependency set to.
UV_LOCK_FILENAME = "uv.lock"

#: uv's documented setting for "run without updating the lockfile".
#:
#: Read from ``uv run --help`` on this machine: ``--frozen`` carries
#: ``[env: UV_FROZEN=]``. Passed as an environment variable rather than as an
#: argument so the command the controller executes stays exactly the argument
#: vector the policy authorised.
UV_FROZEN = "UV_FROZEN"

#: What the controller has to do about the lock once its checks have finished.
LOCK_UNCHANGED = "unchanged"
LOCK_ABSENT = "absent"
LOCK_REMOVED = "removed"
LOCK_RESTORED = "restored"
LOCK_RETAINED = "retained"


@dataclass(frozen=True, slots=True)
class UvLockOutcome:
    """What the guard did about the lock file, and why."""

    action: str
    path: str
    detail: str

    @property
    def notable(self) -> bool:
        """Return whether this outcome is worth telling a reader about.

        An untouched lock and a project that never had one are the ordinary
        cases and say nothing; the other three describe the controller having
        had to put something back the way it found it.
        """

        return self.action not in {LOCK_UNCHANGED, LOCK_ABSENT}


@dataclass(frozen=True, slots=True)
class UvLockGuard:
    """The lock state observed before a check sequence, and how to restore it.

    Construct with :meth:`observe` immediately before the acceptance commands
    run, and call :meth:`settle` immediately after them, whatever happened in
    between.

    Between those two moments the acceptance commands run, and those commands
    execute project code a write-enabled worker just wrote. So what was observed
    is treated as a *record of what was there*, never as a promise about what is
    there now: every read and every write below opens the path with
    ``O_NOFOLLOW``, so a ``uv.lock`` that has since become a symlink out of the
    worktree cannot be written through. That matters because restoring the lock
    happens before the containment gate re-scans the worktree; without
    ``O_NOFOLLOW`` this restore would be a write past the isolation boundary
    that Git evidence could never show.
    """

    path: Path
    present_before: bool
    regular_before: bool
    symlinked_before: bool
    content_before: bytes | None
    digest_before: str | None

    @classmethod
    def observe(cls, worktree: Path) -> UvLockGuard:
        """Record the project's lock state before any check runs."""

        target = worktree / UV_LOCK_FILENAME
        try:
            info = os.lstat(target)
        except OSError:
            return cls(
                path=target,
                present_before=False,
                regular_before=False,
                symlinked_before=False,
                content_before=None,
                digest_before=None,
            )
        symlinked = stat_module.S_ISLNK(info.st_mode)
        if not stat_module.S_ISREG(info.st_mode):
            # A symlink, a directory, a device: present, but not a lock file
            # this module may read, restore, or vouch for.
            return cls(
                path=target,
                present_before=True,
                regular_before=False,
                symlinked_before=symlinked,
                content_before=None,
                digest_before=None,
            )
        content = _read_without_following(target)
        return cls(
            path=target,
            present_before=True,
            regular_before=True,
            symlinked_before=False,
            content_before=content,
            digest_before=(
                hashlib.sha256(content).hexdigest() if content is not None else None
            ),
        )

    @property
    def frozen(self) -> bool:
        """Return whether ``UV_FROZEN`` may be set for this check sequence.

        Only when a real lock file was already there. uv refuses the setting
        outright when there is no lock to freeze - ``Unable to find lockfile at
        uv.lock, but UV_FROZEN=1 was provided`` - so asking for it on a project
        that commits no lock would fail every check rather than protect
        anything. A path that exists but is not a regular file is not a lock
        either, and claiming to have frozen one would be a false assurance.
        """

        return self.regular_before

    def settle(self) -> UvLockOutcome:
        """Put the lock back the way the checks found it and say what was done.

        Never touches anything but ``<worktree>/uv.lock``, and never follows a
        symlink. ``unlink`` removes a link rather than its target, and every
        read and write goes through ``O_NOFOLLOW``, so neither the "remove what
        the check created" path nor the "restore what the project had" path can
        reach a file outside the worktree.
        """

        display = str(self.path)
        if self.present_before and not self.regular_before:
            kind = "a symlink" if self.symlinked_before else "not a regular file"
            return UvLockOutcome(
                action=LOCK_RETAINED,
                path=display,
                detail=(
                    f"uv.lock is {kind}; the controller neither froze nor "
                    "removed it, and did not write through it"
                ),
            )
        if self.present_before:
            return self._restore(display)
        return self._discard(display)

    def _restore(self, display: str) -> UvLockOutcome:
        """Re-establish the bytes the checks found, if they are not there now."""

        if self.content_before is None:
            return UvLockOutcome(
                action=LOCK_RETAINED,
                path=display,
                detail=(
                    "uv.lock could not be read before the checks ran, so the "
                    "controller did not attempt to restore it"
                ),
            )
        current = _read_without_following(self.path)
        if current is not None and hashlib.sha256(current).hexdigest() == (
            self.digest_before
        ):
            return UvLockOutcome(
                action=LOCK_UNCHANGED,
                path=display,
                detail="the project's uv.lock is byte-identical to before",
            )
        error = _write_without_following(self.path, self.content_before)
        if error is not None:
            return UvLockOutcome(
                action=LOCK_RETAINED,
                path=display,
                detail=f"uv.lock changed and could not be restored: {error}",
            )
        return UvLockOutcome(
            action=LOCK_RESTORED,
            path=display,
            detail=(
                "a check changed the project's uv.lock; the controller "
                "restored the bytes it found"
            ),
        )

    def _discard(self, display: str) -> UvLockOutcome:
        """Remove a lock the check sequence itself brought into existence."""

        if not os.path.lexists(self.path):
            return UvLockOutcome(
                action=LOCK_ABSENT,
                path=display,
                detail="the project has no uv.lock and the checks created none",
            )
        try:
            # ``unlink`` removes the entry, never what a link points at, so a
            # link a check left behind is discarded without touching its target.
            self.path.unlink()
        except OSError as exc:
            return UvLockOutcome(
                action=LOCK_RETAINED,
                path=display,
                detail=f"a check created uv.lock and it could not be removed: {exc}",
            )
        return UvLockOutcome(
            action=LOCK_REMOVED,
            path=display,
            detail=(
                "the project commits no uv.lock, so uv resolved dependencies "
                "for this check and the controller removed the file it wrote"
            ),
        )


def _read_without_following(target: Path) -> bytes | None:
    """Return the bytes of ``target``, or ``None`` if it is not a plain file now.

    ``O_NOFOLLOW`` makes "is this a symlink?" and "read it" one operation, so
    there is no window in which the answer can change between them.
    """

    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _write_without_following(target: Path, content: bytes) -> str | None:
    """Write ``content`` to ``target``, refusing to write to another file's inode.

    Returns ``None`` on success, or the reason it did not happen.

    ``O_NOFOLLOW`` refuses a symlink in the kernel rather than in a check this
    code performs, so nothing that changes between the decision and the open can
    defeat it. That leaves one other way a path inside the worktree can name
    content outside it: a hard link, which is a perfectly ordinary regular file
    as far as ``O_NOFOLLOW`` is concerned. So the link count is read from the
    descriptor that is about to be written - the same open file, not the path
    again - and a lock with more than one name is declined rather than
    truncated. A researcher who genuinely hard-linked their lock file loses a
    restore they did not need; anything else loses an escape.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o644)
    except OSError as exc:
        return str(exc)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            links = os.fstat(handle.fileno()).st_nlink
            if links > 1:
                return (
                    f"uv.lock has {links} hard links, so writing it would change "
                    "a file that is also named somewhere else"
                )
            handle.truncate(0)
            handle.write(content)
    except OSError as exc:
        return str(exc)
    return None
