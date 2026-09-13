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

The lock file itself is never removed, and that is not an oversight. A flock is
held on an inode; unlinking the name while holding it lets the next arrival
create a new inode at the same path and lock that. The second review measured
exactly that happening, so the file stays -- a few hundred bytes per run id.

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
#:
#: Process-global, and therefore *not* thread-safe: a second thread asking for a
#: lock this process holds would be granted it. Every entry point here is a CLI
#: command running on one thread, so that cannot happen today; it is recorded
#: because the reentrancy that makes ``start --run`` work is the same mechanism
#: that would make a threaded caller wrong, and a future one should see this
#: before adding threads rather than after.
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
    """Drop the lock. Never remove its file.

    The file stays, and that is the whole correctness argument. A ``flock`` is
    held on an *inode*, not on a name; unlinking the name while holding the lock
    leaves the holder locking an inode nobody can reach, and the next arrival
    creates a fresh inode at the same path and locks that instead. Two holders,
    one run.

    An earlier version of this function unlinked first and argued that the
    interleaving was harmless. An independent reviewer measured it: ten
    processes contending produced violations in roughly one acquisition in
    fifty, and a control run differing *only* in the removal of that unlink
    produced none. Reproduced here at eight percent.

    So the lock file is permanent, one small file per run under the locks
    directory. That is the cost of the guarantee, and it is a few hundred bytes.
    """

    del target  # deliberately unused: see above
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


def is_held(run_id: str) -> bool:
    """Whether some live process currently holds the lock for ``run_id``.

    Asks the kernel rather than reading the note in the file. A ``flock`` is
    released when its holder dies for any reason, so "could not acquire" means
    there is a living owner and "could acquire" means there is not. That is the
    whole reason this module uses ``flock``, and it is the only staleness
    question this codebase is allowed to answer -- the one the kernel answers.

    Acquired and released immediately, and the answer is a fact about that
    instant, not a reservation. It is therefore only ever safe to use in one
    direction: to decide to *leave something alone*. A caller must never read a
    False as permission to take anything, because between the answer and the
    action another process may have arrived. Recovery uses it exactly that way
    -- to refuse to reclaim a run that is being prepared right now -- and being
    wrong in the conservative direction costs one more reclaim pass.
    """

    target = lock_path(run_id)
    if str(target) in _HELD:
        return True
    if not target.exists():
        return False
    try:
        handle = os.open(target, os.O_RDWR)
    except OSError:
        # Unreadable is not provably free, and the safe answer to "may I delete
        # what this guards" is no.
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
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
