"""Provider pacing that outlives the process that learned it.

:class:`~research_os.literature.http.HttpClient` already paces one process well:
each host has a minimum interval from that provider's published terms, and the
client sleeps to honour it. What it cannot do is remember. Two Research OS runs
started an hour apart each begin believing every provider is fresh, and a
provider that answered the first with ``429 Retry-After: 3600`` answers the
second the same way, for the same reason, at the same cost.

So the operational half of what a run learns is persisted, in the literature
store that is already there. Four properties.

**Cache first.** A query this store already answered recently is answered from
the store. Provider quota is not spent to refresh data with no freshness need.

**Reservation is atomic.** Deciding a slot is free and taking it happen inside
one ``BEGIN IMMEDIATE`` transaction, so two concurrent runs cannot both look,
both see "available", and both issue a request. The network call happens after
the transaction commits: a SQLite write lock is never held across I/O.

**Only real information is recorded.** ``Retry-After`` in either of its two
forms, an explicit quota reset, a 429, a timeout, a permanent error. Nothing is
estimated. A quota nobody reported stays unknown, and unknown is a value.

**Nothing sleeps irrationally.** A provider asking for an hour gets an hour --
recorded, not waited out. The run returns ``RATE_LIMITED`` with the reset time
and moves on to the providers that will answer.

This is operational state. Deleting it costs a run some politeness and no
science at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from research_os.errors import LiteratureStoreError
from research_os.literature.models import SourceStatus

#: The minimum interval Research OS keeps between requests to each source.
#:
#: Taken from the same published terms the HTTP host policies use, and kept here
#: as well because this table is about a *source* rather than a host: two
#: adapters could share a host, and a run in another process has no HttpClient
#: of ours to consult.
MIN_INTERVAL_SECONDS: dict[str, float] = {
    "arxiv": 3.0,
    "crossref": 0.15,
    "openalex": 0.2,
}

#: The interval used for a source with no published figure of its own.
DEFAULT_MIN_INTERVAL_SECONDS = 1.0

#: How long a successful search stays fresh enough to answer from the store.
#:
#: A day. Long enough that a run repeated the same afternoon costs no provider
#: quota, short enough that a literature review started tomorrow sees what was
#: published today. A caller that needs something fresher says so; a caller that
#: needs something older is asking for a different thing and should say that too.
DEFAULT_CACHE_TTL_SECONDS = 86_400


def utc_stamp(moment: datetime) -> str:
    """Return the ISO-8601 UTC string this subsystem stores times as.

    Second granularity, the same shape every other timestamp in Research OS
    uses, so a researcher reading ``lit sources`` beside a run record is reading
    one format rather than two.
    """

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _whole_seconds(interval: float) -> int:
    """Round a sub-second interval up to the resolution this state is stored at.

    OpenAlex's published interval is 0.2 seconds and Crossref's is 0.15, and a
    timestamp stored to the second cannot express either. Rounding *up* is the
    only safe direction: a cross-process bound of one second where the published
    figure is a fifth of one asks a provider less often than it permits, which
    is the error this subsystem is allowed to make. Within a single process the
    HTTP client still honours the exact figure.
    """

    return max(1, -(-int(interval * 1000) // 1000))


def parse_stamp(value: str | None) -> datetime | None:
    """Parse one stored timestamp, returning ``None`` for anything unusable.

    Total on purpose. A corrupt operational row must degrade to "we do not know
    when this source is next allowed", which fails towards asking politely, and
    never to an exception out of a retrieval that had nothing wrong with it.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """What is persistently known about one provider's willingness to answer."""

    source: str
    next_allowed_at: str | None = None
    rate_limited_until: str | None = None
    quota_remaining: int | None = None
    quota_reset_at: str | None = None
    last_status: str = ""
    last_detail: str = ""
    last_success_at: str | None = None
    last_failure_at: str | None = None
    updated_at: str | None = None

    def availability(self, now: datetime) -> str:
        """Return the one word a researcher wants: what can this source do now?"""

        limited = parse_stamp(self.rate_limited_until)
        if limited is not None and limited > now:
            return "RATE_LIMITED"
        if self.last_status == SourceStatus.UNAVAILABLE.value:
            return "UNAVAILABLE"
        if self.last_status == SourceStatus.FAILED.value:
            return "FAILED"
        return "AVAILABLE"


@dataclass(frozen=True, slots=True)
class Reservation:
    """The answer to "may I call this provider right now?".

    ``granted`` is the whole decision. When it is False the caller must not make
    the request: ``reason`` says why in a sentence a researcher can read, and
    ``next_allowed_at`` says when to come back, when that is known.
    """

    source: str
    granted: bool
    reason: str = ""
    next_allowed_at: str | None = None
    status: SourceStatus = SourceStatus.OK


class SourcePacer:
    """Persistent per-source pacing over one literature store connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # -- reading ---------------------------------------------------------

    def health(self, source: str) -> SourceHealth:
        """Return what is known about ``source``. An unseen source is fresh."""

        row = self.connection.execute(
            "SELECT * FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return SourceHealth(source=source)
        return _hydrate(row)

    def all_health(self) -> list[SourceHealth]:
        rows = self.connection.execute(
            "SELECT * FROM source_health ORDER BY source"
        ).fetchall()
        return [_hydrate(row) for row in rows]

    # -- reserving -------------------------------------------------------

    def reserve(
        self,
        source: str,
        *,
        now: datetime,
        min_interval_seconds: float | None = None,
    ) -> Reservation:
        """Atomically decide whether a request to ``source`` may be made, and take it.

        The read and the write are one ``BEGIN IMMEDIATE`` transaction, which is
        the whole point: two runs that both merely *read* "available" would both
        proceed, and the pacing a provider published would be violated by
        exactly the client that was trying to honour it.

        The network call happens after this returns. A SQLite write lock is
        never held across I/O -- that would convert one slow provider into a
        stalled literature subsystem for every other process on the machine.
        """

        interval = (
            min_interval_seconds
            if min_interval_seconds is not None
            else MIN_INTERVAL_SECONDS.get(source, DEFAULT_MIN_INTERVAL_SECONDS)
        )
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM source_health WHERE source = ?", (source,)
            ).fetchone()
            health = _hydrate(row) if row is not None else SourceHealth(source=source)

            limited = parse_stamp(health.rate_limited_until)
            if limited is not None and limited > now:
                return Reservation(
                    source=source,
                    granted=False,
                    reason=(
                        f"{source} rate-limited us and asked us to wait until "
                        f"{utc_stamp(limited)}"
                    ),
                    next_allowed_at=utc_stamp(limited),
                    status=SourceStatus.RATE_LIMITED,
                )

            allowed = parse_stamp(health.next_allowed_at)
            if allowed is not None and allowed > now:
                return Reservation(
                    source=source,
                    granted=False,
                    reason=(
                        f"{source} was asked too recently; its minimum interval "
                        f"allows the next request at {utc_stamp(allowed)}"
                    ),
                    next_allowed_at=utc_stamp(allowed),
                    status=SourceStatus.RATE_LIMITED,
                )

            reserved = utc_stamp(now + timedelta(seconds=_whole_seconds(interval)))
            _write(
                connection,
                source,
                next_allowed_at=reserved,
                updated_at=utc_stamp(now),
            )
            return Reservation(source=source, granted=True, next_allowed_at=reserved)

    # -- recording -------------------------------------------------------

    def record_success(
        self,
        source: str,
        *,
        now: datetime,
        quota_remaining: int | None = None,
        quota_reset_at: str | None = None,
        detail: str = "",
    ) -> None:
        """Record that ``source`` answered. Clears any rate limit it had."""

        with self._immediate() as connection:
            _write(
                connection,
                source,
                rate_limited_until=None,
                last_status=SourceStatus.OK.value,
                last_detail=detail,
                last_success_at=utc_stamp(now),
                quota_remaining=quota_remaining,
                quota_reset_at=quota_reset_at,
                updated_at=utc_stamp(now),
            )

    def record_rate_limited(
        self,
        source: str,
        *,
        now: datetime,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
        detail: str = "",
    ) -> str | None:
        """Record a 429 and return when this source may be asked again.

        Only what the provider actually said. With no ``Retry-After`` and no
        reset, the source is marked limited for its own minimum interval and
        nothing longer: inventing a cooldown a provider did not ask for would be
        this client deciding, on no evidence, that a literature review should
        stop.
        """

        if reset_at is not None:
            until = reset_at
        elif retry_after_seconds is not None:
            until = now + timedelta(seconds=max(retry_after_seconds, 0.0))
        else:
            until = now + timedelta(
                seconds=_whole_seconds(
                    MIN_INTERVAL_SECONDS.get(source, DEFAULT_MIN_INTERVAL_SECONDS)
                )
            )
        stamp = utc_stamp(until)
        with self._immediate() as connection:
            _write(
                connection,
                source,
                rate_limited_until=stamp,
                next_allowed_at=stamp,
                quota_reset_at=stamp if reset_at is not None else None,
                last_status=SourceStatus.RATE_LIMITED.value,
                last_detail=detail,
                last_failure_at=utc_stamp(now),
                updated_at=utc_stamp(now),
            )
        return stamp

    def record_failure(
        self,
        source: str,
        *,
        now: datetime,
        status: SourceStatus,
        detail: str = "",
    ) -> None:
        """Record a non-throttling failure: a timeout, a 5xx, a permanent error.

        Deliberately does *not* set ``rate_limited_until``. A provider that
        failed has not asked us to wait, and treating a transient outage as a
        quota exhaustion would keep a working source switched off for a run that
        could have used it.
        """

        with self._immediate() as connection:
            _write(
                connection,
                source,
                last_status=status.value,
                last_detail=detail,
                last_failure_at=utc_stamp(now),
                updated_at=utc_stamp(now),
            )

    # -- internals -------------------------------------------------------

    def _immediate(self):
        """Return a context manager holding an exclusive write transaction.

        ``BEGIN IMMEDIATE`` rather than the default deferred transaction, which
        Python's sqlite3 starts only when the first write statement runs -- so a
        read followed by a write would be exactly the unprotected sequence this
        module exists to eliminate.
        """

        return _ImmediateTransaction(self.connection)


class _ImmediateTransaction:
    """One explicit ``BEGIN IMMEDIATE`` on a connection sqlite3 usually manages.

    The connection's autocommit behaviour is switched off for the duration and
    restored afterwards, including on the error path, so nothing else that uses
    this connection sees a changed transaction mode.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._previous: object = None

    def __enter__(self) -> sqlite3.Connection:
        self._previous = self.connection.isolation_level
        self.connection.isolation_level = None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.connection.isolation_level = self._previous  # type: ignore[assignment]
            raise LiteratureStoreError(
                f"cannot take the literature pacing lock: {exc}"
            ) from exc
        return self.connection

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        try:
            if kind is None:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute("ROLLBACK")
        except sqlite3.Error as exc:  # pragma: no cover - only on a broken handle
            if kind is None:
                raise LiteratureStoreError(
                    f"literature pacing write failed: {exc}"
                ) from exc
        finally:
            self.connection.isolation_level = self._previous  # type: ignore[assignment]


#: The columns a partial update leaves alone unless it names them.
_FIELDS = (
    "next_allowed_at",
    "rate_limited_until",
    "quota_remaining",
    "quota_reset_at",
    "last_status",
    "last_detail",
    "last_success_at",
    "last_failure_at",
    "updated_at",
)

#: Columns whose ``None`` means "clear this", rather than "leave it alone".
#:
#: Only the two that describe a *current* restriction. Everything else is a
#: historical fact -- when this source last succeeded, what it last said -- and
#: a later partial update must not erase history it was not talking about.
_CLEARABLE = frozenset({"rate_limited_until", "quota_reset_at"})


def _write(connection: sqlite3.Connection, source: str, **fields: object) -> None:
    """Insert or update one source's row, touching only the fields named."""

    connection.execute(
        "INSERT INTO source_health(source) VALUES(?) ON CONFLICT(source) DO NOTHING",
        (source,),
    )
    assignments = []
    values: list[object] = []
    for name in _FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if value is None and name not in _CLEARABLE:
            continue
        assignments.append(f"{name} = ?")
        values.append(value)
    if not assignments:
        return
    values.append(source)
    connection.execute(
        f"UPDATE source_health SET {', '.join(assignments)} WHERE source = ?",
        values,
    )


def _hydrate(row: sqlite3.Row) -> SourceHealth:
    return SourceHealth(
        source=row["source"],
        next_allowed_at=row["next_allowed_at"],
        rate_limited_until=row["rate_limited_until"],
        quota_remaining=row["quota_remaining"],
        quota_reset_at=row["quota_reset_at"],
        last_status=row["last_status"] or "",
        last_detail=row["last_detail"] or "",
        last_success_at=row["last_success_at"],
        last_failure_at=row["last_failure_at"],
        updated_at=row["updated_at"],
    )
