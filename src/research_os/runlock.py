"""One writer per run, enforced across processes.

A run record is a small JSON file replaced whole. That makes a single writer
safe and two writers silently wrong: the loser's decision simply disappears
under the winner's next write.

Seen for real. A researcher cancelled a research run in one terminal while it
was still executing in another; ``cancel`` reported CANCELLED, the executing
process finished its next task, wrote the record again, and the run ended
READY_FOR_HUMAN. Nothing errored, nothing was logged, and the researcher's
decision was gone. A stop that does not stop anything is worse than no stop
command at all.

So every process that intends to *change* a run takes an exclusive lock on it
first, and a process that cannot take it is told who holds it rather than
allowed to interleave. Reading a run needs no lock: reads are atomic because
writes are.

Three properties this deliberately has.

**It is advisory, and that is enough.** Nothing outside Research OS writes these
files. The lock exists to stop two ``researchctl`` processes from disagreeing,
not to defend the directory against arbitrary programs.

**It is reentrant within one process.** ``research start --run`` plans and then
executes, and both take the lock. A non-reentrant lock would deadlock the most
ordinary command there is.

**A dead owner does not block the living.** A process killed mid-run leaves its
lock file behind. The next caller checks whether that pid still exists and, if
it does not, takes the lock over and says so, because the alternative is a run
that no command can ever touch again.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from research_os.automation.models import utc_now
from research_os.automation.store import locks_root
from research_os.errors import RunLockedError

#: Locks this process already holds, by lock path.
#:
#: Depth-counted rather than a flag, so nested acquisitions release in the right
#: order and an inner block cannot drop the outer block's lock.
_HELD: dict[str, int] = {}


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
    if key in _HELD:
        _HELD[key] += 1
        try:
            yield target
        finally:
            _HELD[key] -= 1
            if _HELD[key] <= 0:
                del _HELD[key]
        return

    _acquire(target, run_id=run_id, action=action)
    _HELD[key] = 1
    try:
        yield target
    finally:
        del _HELD[key]
        target.unlink(missing_ok=True)


def _acquire(target: Path, *, run_id: str, action: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
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
        handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        owner = _read(target)
        if _alive(owner.get("pid")):
            raise RunLockedError(
                f"{run_id} is being changed by another process "
                f"(pid {owner.get('pid', 'unknown')}, "
                f"{owner.get('action', 'unknown action')}, started "
                f"{owner.get('created_at', 'at an unknown time')}). Wait for it "
                "to finish, or stop that process, before changing this run."
            ) from None
        handle = _take_over(target, run_id=run_id)
    except OSError as exc:
        raise RunLockedError(f"cannot lock {run_id}: {exc}") from exc
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")


def _take_over(target: Path, *, run_id: str) -> int:
    """Claim a lock whose owner is gone, without racing another claimant.

    An independent reviewer found the obvious version wrong: unlink the stale
    file, then create it exclusively. Two processes both unlink, both create,
    and the second unlink removes the first's *fresh* lock -- so both believe
    they hold it, which is precisely the silent double-writer this module
    exists to prevent.

    Creating a uniquely-named file and hard-linking it into place fixes it.
    ``os.link`` fails if the destination exists, so exactly one claimant wins
    however many are trying, and the loser re-reads and is refused normally.
    """

    unique = target.with_name(f"{target.name}.{os.getpid()}.{time.time_ns()}")
    try:
        handle = os.open(unique, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise RunLockedError(
            f"cannot take over the stale lock on {run_id}: {exc}"
        ) from exc
    try:
        # Remove the corpse, then claim the name atomically. Another claimant
        # that got there first makes os.link fail, and nobody's live lock is
        # ever unlinked because only a dead owner's name is removed here.
        target.unlink(missing_ok=True)
        os.link(unique, target)
    except OSError as exc:
        os.close(handle)
        raise RunLockedError(
            f"another process took over the stale lock on {run_id} first: {exc}"
        ) from exc
    finally:
        unique.unlink(missing_ok=True)
    return handle


def _read(target: Path) -> dict[str, Any]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alive(pid: Any) -> bool:
    """Return whether ``pid`` is a process this user could signal.

    A pid that is not an integer, or that no longer exists, is treated as dead.
    A pid owned by another user answers ``PermissionError``, which means it very
    much does exist, so that counts as alive.
    """

    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True
