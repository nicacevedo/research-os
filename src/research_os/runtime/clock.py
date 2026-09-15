"""Time, injected rather than imported.

Two rules, learned from the durability tests rather than from taste.

**Deadlines belong to the database.** A lease that expires at "now plus sixty
seconds" computed in Python expires according to *that worker's* clock. Two
workers with a few seconds of skew will disagree about whether a lease is dead,
and the disagreement shows up as two workers holding one work item -- exactly
the failure leases exist to prevent. Every lease and schedule deadline in this
runtime is therefore computed by PostgreSQL, in SQL, from ``now()``. This module
is not used for any of that.

**Observations belong to the caller.** Timestamps written for a human to read,
poll loop pacing, and "has this been waiting too long" checks are ordinary
program logic, and tests need them to be deterministic. Those go through a
:class:`Clock`.

So: if a value decides who owns a row, it comes from the database. If it decides
what to print or when to wake up, it comes from here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """A source of wall-clock time and of sleeping."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


class FrozenClock:
    """A clock that only moves when a test moves it.

    ``sleep`` advances the clock instead of blocking, so a test can drive a poll
    loop through an hour of simulated waiting in no time at all and still assert
    on the exact instants involved.
    """

    __slots__ = ("_now", "slept")

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def isoformat(moment: datetime) -> str:
    """Render ``moment`` the way the rest of Research OS renders timestamps."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
