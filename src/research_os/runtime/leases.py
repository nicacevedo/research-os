"""Keeping a claimed work item alive while a worker works on it.

A lease is short -- two minutes by default -- and a bounded research cycle can
take much longer than that. Something must renew it while the work runs, and
that something cannot be the work itself: a cycle blocked on a model call for
ninety seconds is not in a position to make a database round trip.

So a background thread does it. Three properties make that safe:

**It is a daemon thread.** If the main thread dies, the renewer dies with the
process, the lease expires, and the control plane recovers the item. A renewer
that outlived its worker would hold a lease for work nobody is doing -- the one
failure mode worse than losing the lease.

**Losing the lease is reported, not retried.** When
:meth:`~research_os.runtime.queue.WorkQueue.renew` refuses -- because the
deadline passed and someone else may now own the item -- the keeper records it
and stops. The worker checks :attr:`LeaseKeeper.lost` at each step it can
usefully abandon, and the idempotency ledger makes whatever it already did
reusable by whoever took over.

**It renews well before the deadline.** Default is every 30 seconds on a
120-second lease, so three consecutive failures are needed before expiry. A
renewer that woke up at the deadline would lose leases to ordinary jitter.
"""

from __future__ import annotations

import logging
import threading
from types import TracebackType
from typing import Self

from research_os.runtime.queue import LeaseLostError, WorkQueue

LOG = logging.getLogger("research_os.runtime.leases")


class LeaseKeeper:
    """Renews one work item's lease in the background for the life of a block."""

    __slots__ = (
        "_error",
        "_interval",
        "_lease_seconds",
        "_lost",
        "_owner",
        "_queue",
        "_stop",
        "_thread",
        "_work_id",
    )

    def __init__(
        self,
        queue: WorkQueue,
        *,
        work_id: str,
        owner: str,
        lease_seconds: int,
        renew_every: float,
    ) -> None:
        self._queue = queue
        self._work_id = work_id
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._interval = max(1.0, float(renew_every))
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._error: str = ""
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        """True once this worker no longer holds the lease."""
        return self._lost.is_set()

    @property
    def error(self) -> str:
        return self._error

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._queue.renew(
                    self._work_id, owner=self._owner, lease_seconds=self._lease_seconds
                )
            except LeaseLostError as exc:
                self._error = str(exc)
                self._lost.set()
                LOG.warning("lease on %s lost: %s", self._work_id, exc)
                return
            except Exception as exc:  # noqa: BLE001 - a renewer must not kill the worker
                # A transient database failure is not a lost lease. Keep trying:
                # the deadline is three renewals away, and giving up here would
                # abandon work over a hiccup.
                self._error = str(exc)
                LOG.warning("could not renew lease on %s: %s", self._work_id, exc)

    def __enter__(self) -> Self:
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self._work_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
