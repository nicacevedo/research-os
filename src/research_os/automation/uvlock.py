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
    """

    path: Path
    present_before: bool
    content_before: bytes | None
    digest_before: str | None
    symlinked: bool

    @classmethod
    def observe(cls, worktree: Path) -> UvLockGuard:
        """Record the project's lock state before any check runs."""

        target = worktree / UV_LOCK_FILENAME
        symlinked = target.is_symlink()
        if symlinked or not target.is_file():
            return cls(
                path=target,
                present_before=target.exists(),
                content_before=None,
                digest_before=None,
                symlinked=symlinked,
            )
        try:
            content = target.read_bytes()
        except OSError:
            return cls(
                path=target,
                present_before=True,
                content_before=None,
                digest_before=None,
                symlinked=False,
            )
        return cls(
            path=target,
            present_before=True,
            content_before=content,
            digest_before=hashlib.sha256(content).hexdigest(),
            symlinked=False,
        )

    @property
    def frozen(self) -> bool:
        """Return whether ``UV_FROZEN`` may be set for this check sequence.

        Only when a real lock file was already there. uv refuses the setting
        outright when there is no lock to freeze - ``Unable to find lockfile at
        uv.lock, but UV_FROZEN=1 was provided`` - so asking for it on a project
        that commits no lock would fail every check rather than protect
        anything.
        """

        return self.present_before and not self.symlinked

    def settle(self) -> UvLockOutcome:
        """Put the lock back the way the checks found it and say what was done.

        Never touches anything but ``<worktree>/uv.lock``, and never follows a
        symlink: a lock that is a link out of the worktree is reported and left
        alone, because writing through it would be the very escape the
        containment gate exists to refuse.
        """

        display = str(self.path)
        if self.symlinked:
            return UvLockOutcome(
                action=LOCK_RETAINED,
                path=display,
                detail=(
                    "uv.lock is a symlink; the controller neither froze nor "
                    "removed it, and did not write through it"
                ),
            )
        exists_now = self.path.exists()
        if self.present_before:
            if self.content_before is None:
                return UvLockOutcome(
                    action=LOCK_RETAINED,
                    path=display,
                    detail=(
                        "uv.lock could not be read before the checks ran, so "
                        "the controller did not attempt to restore it"
                    ),
                )
            if exists_now and self._digest_now() == self.digest_before:
                return UvLockOutcome(
                    action=LOCK_UNCHANGED,
                    path=display,
                    detail="the project's uv.lock is byte-identical to before",
                )
            try:
                self.path.write_bytes(self.content_before)
            except OSError as exc:
                return UvLockOutcome(
                    action=LOCK_RETAINED,
                    path=display,
                    detail=f"uv.lock changed and could not be restored: {exc}",
                )
            return UvLockOutcome(
                action=LOCK_RESTORED,
                path=display,
                detail=(
                    "a check changed the project's uv.lock; the controller "
                    "restored the bytes it found"
                ),
            )
        if not exists_now:
            return UvLockOutcome(
                action=LOCK_ABSENT,
                path=display,
                detail="the project has no uv.lock and the checks created none",
            )
        try:
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

    def _digest_now(self) -> str | None:
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return None
