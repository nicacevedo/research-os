"""One writer per run, enforced by the kernel.

A run record is a small JSON file replaced whole. That makes a single writer
safe and two writers silently wrong: the loser's decision disappears under the
winner's next write.

Seen for real. A researcher cancelled a research run in one terminal while it
was still executing in another; ``cancel`` reported CANCELLED, the executing
process finished its next task, wrote the record again, and the run ended
READY_FOR_HUMAN. Nothing errored, nothing was logged, and the decision was gone.

So every process that intends to *change* a run takes an exclusive lock on it
first. Reading needs no lock: reads are atomic because writes are.

**Why ``flock`` and not a lock file we manage ourselves.** The obvious design --
create a file exclusively, and if one already exists decide whether its owner is
dead and take it over -- is where this module was first written, and it was
wrong. An independent reviewer demonstrated two processes holding one lock:
both read the stale file, both concluded the owner was dead, both removed it,
and the second removal deleted the *first one's fresh lock*. A later attempt to
fix it with ``os.link`` kept the same unlink and therefore the same race.

``fcntl.flock`` has no such window. The kernel owns the lock, grants it to
exactly one file description, and releases it when the holder exits for any
reason -- including ``SIGKILL``, which no amount of cleanup code survives. There
is no staleness to detect, so there is no takeover to race.

The trade is that flock is advisory and is unreliable over NFS. Both are fine
here: nothing outside Research OS writes these files, and runtime state lives
under the local state home by design.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from research_os.automation.models import utc_now
from research_os.automation.store import locks_root
from research_os.errors import RunLockedError

#: Locks this process already holds, by lock path, with the descriptor and the
#: nesting depth.
#:
#: Depth-counted rather than a flag, so nested acquisitions release in the right
#: order and an inner block cannot drop the outer block's lock. ``research start
#: --run`` plans and then executes, and both take the lock; a non-reentrant lock
#: would deadlock the most ordinary command there is.
_HELD: dict[str, list[int]] = {}


def lock_path(run_id: str) -> Path:
    return locks_root() / f"run-{run_id}.lock"


@contextmanager
def run_lock(run_id: str, *, action: str) -> Iterator[Path]:
    """Hold the exclusive lock for ``run_id`` while changing it.

    ``action`` is recorded in the lock file and quoted back to whoever is
    refused, so "who has this run" is answered with what they are doing rather
    than only a process id.
    """

    target = lock_path(run_id)
    key = str(target)
    held = _HELD.get(key)
    if held is not None:
        held[1] += 1
        try:
            yield target
        finally:
            held[1] -= 1
            if held[1] <= 0:
                _HELD.pop(key, None)
                _release(held[0], target)
        return

    handle = _acquire(target, run_id=run_id, action=action)
    _HELD[key] = [handle, 1]
    try:
        yield target
    finally:
        entry = _HELD.pop(key, None)
        if entry is not None:
            _release(entry[0], target)


def _acquire(target: Path, *, run_id: str, action: str) -> int:
    """Take the kernel lock for ``target``, or explain who already holds it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RunLockedError(f"cannot open the lock for {run_id}: {exc}") from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Read the holder's note for the message, not for the decision: the
        # kernel already made the decision, and a note that is missing or
        # half-written changes nothing about who holds the lock.
        owner = _read(target)
        os.close(handle)
        raise RunLockedError(
            f"{run_id} is being changed by another process "
            f"(pid {owner.get('pid', 'unknown')}, "
            f"{owner.get('action', 'unknown action')}, started "
            f"{owner.get('created_at', 'at an unknown time')}). Wait for it to "
            "finish, or stop that process, before changing this run."
        ) from None

    payload = json.dumps(
        {
            "run_id": run_id,
            "action": action,
            "pid": os.getpid(),
            "created_at": utc_now(),
        },
        sort_keys=True,
    )
    try:
        os.ftruncate(handle, 0)
        os.lseek(handle, 0, os.SEEK_SET)
        os.write(handle, (payload + "\n").encode("utf-8"))
    except OSError:
        # The note is a courtesy for the next caller's error message. Failing to
        # write it does not make the lock any less held.
        pass
    return handle


def _release(handle: int, target: Path) -> None:
    """Drop the lock and remove its file, in that order and without racing.

    The file is unlinked while the lock is still held, so no other process can
    be holding *this* file when it goes away. A process that opened the same
    path a moment earlier is waiting on a file that no longer has a name, and
    its own ``flock`` will succeed on a file nobody else can reach -- so it
    proceeds, which is correct: the run really is free.
    """

    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


def _read(target: Path) -> dict[str, Any]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
