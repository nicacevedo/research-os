"""Pacing that outlives the process, and the race it has to survive.

The HTTP client already paces one process well. What it cannot do is remember,
so two runs started an hour apart each begin believing every provider is fresh,
and a provider that answered the first with ``429 Retry-After: 3600`` answers the
second identically, for the same reason, at the same cost.

Every test here uses a fake clock and a scripted transport. Nothing sleeps,
nothing reaches the network, and "an hour later" is a value rather than a wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from research_os.errors import LiteratureStoreError
from research_os.literature.models import SourceStatus
from research_os.literature.pacing import (
    MIN_INTERVAL_SECONDS,
    SourceHealth,
    SourcePacer,
    parse_stamp,
    utc_stamp,
)
from research_os.literature.report import render_sources
from research_os.literature.service import LiteratureService
from research_os.literature.sources.arxiv import ArxivSource
from research_os.literature.sources.crossref import CrossrefSource
from research_os.literature.sources.openalex import OpenAlexSource
from research_os.literature.store import LiteratureStore
from tests.literature_helpers import (
    ScriptedClient,
    ScriptedResponse,
    arxiv_feed,
    crossref_work,
    fake_config,
    json_response,
    openalex_work,
)

START = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


class Clock:
    """A clock a test moves on purpose."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def openalex_page(count: int = 1) -> ScriptedResponse:
    return json_response(
        "api.openalex.org",
        {"results": [openalex_work()] * count, "meta": {"count": count}},
    )


def crossref_page() -> ScriptedResponse:
    return json_response("api.crossref.org", {"message": {"items": [crossref_work()]}})


def arxiv_page() -> ScriptedResponse:
    return ScriptedResponse(
        match="export.arxiv.org",
        body=arxiv_feed(),
        headers={"content-type": "application/atom+xml"},
    )


def service(
    store: LiteratureStore,
    responses: list[ScriptedResponse],
    *,
    clock: Clock,
    enabled: tuple[str, ...] = ("openalex",),
    cache_ttl_seconds: int = 0,
) -> tuple[LiteratureService, ScriptedClient]:
    client = ScriptedClient(contact_email="tests@example.invalid", responses=responses)
    built = LiteratureService(
        store=store,
        config=fake_config(enabled=enabled, cache_ttl_seconds=cache_ttl_seconds),
        sources={
            "openalex": OpenAlexSource(client=client),
            "crossref": CrossrefSource(client=client),
            "arxiv": ArxivSource(client=client),
        },
        client=client,
        files_directory=Path("/nonexistent"),
        clock=clock,
    )
    return built, client


# -- the state itself ----------------------------------------------------


def test_an_unseen_source_is_fresh() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    health = pacer.health("arxiv")
    assert health == SourceHealth(source="arxiv")
    assert health.availability(START) == "AVAILABLE"


def test_a_first_reservation_is_granted() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    reservation = pacer.reserve("openalex", now=START)
    assert reservation.granted is True
    assert reservation.next_allowed_at == utc_stamp(START + timedelta(seconds=1))


def test_a_sub_second_interval_rounds_up_rather_than_down() -> None:
    """The persisted bound is coarser than the published one, never finer.

    OpenAlex permits a request every 0.2 seconds and this state is stored to the
    second. Rounding up asks less often than permitted; rounding down would ask
    more often than permitted, which is the error that gets a client blocked.
    """

    assert MIN_INTERVAL_SECONDS["openalex"] < 1.0
    pacer = LiteratureStore.open_memory().pacer()
    pacer.reserve("openalex", now=START)
    assert (
        pacer.reserve("openalex", now=START + timedelta(seconds=0.5)).granted is False
    )
    assert pacer.reserve("openalex", now=START + timedelta(seconds=1.5)).granted is True


def test_a_second_reservation_obeys_the_minimum_interval() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    pacer.reserve("arxiv", now=START)
    second = pacer.reserve("arxiv", now=START)
    assert second.granted is False
    assert second.status is SourceStatus.RATE_LIMITED
    assert "minimum interval" in second.reason


def test_the_interval_expires() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    pacer.reserve("arxiv", now=START)
    later = START + timedelta(seconds=MIN_INTERVAL_SECONDS["arxiv"] + 0.1)
    assert pacer.reserve("arxiv", now=later).granted is True


def test_a_429_persists_when_the_source_may_be_asked_again() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    until = pacer.record_rate_limited(
        "openalex", now=START, retry_after_seconds=3600, detail="429"
    )
    assert until == utc_stamp(START + timedelta(hours=1))
    refused = pacer.reserve("openalex", now=START + timedelta(minutes=30))
    assert refused.granted is False
    assert "rate-limited us" in refused.reason
    assert refused.next_allowed_at == until


def test_a_rate_limit_expires_and_availability_returns() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    pacer.record_rate_limited("openalex", now=START, retry_after_seconds=60)
    after = START + timedelta(seconds=61)
    assert pacer.reserve("openalex", now=after).granted is True
    assert pacer.health("openalex").availability(after) == "AVAILABLE"


def test_a_429_with_no_retry_after_holds_only_its_own_interval() -> None:
    """Nothing is estimated. A provider that named no delay gets no invented one."""

    pacer = LiteratureStore.open_memory().pacer()
    until = pacer.record_rate_limited("crossref", now=START, detail="429")
    assert until == utc_stamp(START + timedelta(seconds=1))
    assert MIN_INTERVAL_SECONDS["crossref"] < 1.0


def test_a_success_clears_a_rate_limit() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    pacer.record_rate_limited("openalex", now=START, retry_after_seconds=10)
    pacer.record_success("openalex", now=START + timedelta(seconds=11), detail="ok")
    health = pacer.health("openalex")
    assert health.rate_limited_until is None
    assert health.last_status == "OK"
    assert health.last_success_at is not None


def test_a_failure_is_not_a_rate_limit() -> None:
    """A provider that timed out has not asked us to wait.

    Treating a transient outage as quota exhaustion would keep a working source
    switched off for a run that could have used it.
    """

    pacer = LiteratureStore.open_memory().pacer()
    pacer.record_failure(
        "arxiv", now=START, status=SourceStatus.UNAVAILABLE, detail="timeout"
    )
    health = pacer.health("arxiv")
    assert health.rate_limited_until is None
    assert health.availability(START) == "UNAVAILABLE"
    assert pacer.reserve("arxiv", now=START).granted is True


def test_a_failure_does_not_erase_an_earlier_success() -> None:
    pacer = LiteratureStore.open_memory().pacer()
    pacer.record_success("arxiv", now=START, detail="ok")
    pacer.record_failure(
        "arxiv",
        now=START + timedelta(minutes=5),
        status=SourceStatus.FAILED,
        detail="500",
    )
    health = pacer.health("arxiv")
    assert health.last_success_at == utc_stamp(START)
    assert health.last_failure_at == utc_stamp(START + timedelta(minutes=5))


def test_a_corrupt_timestamp_degrades_to_asking_politely() -> None:
    store = LiteratureStore.open_memory()
    store.connection.execute(
        "INSERT INTO source_health(source, next_allowed_at) VALUES('arxiv', 'soon')"
    )
    assert store.pacer().reserve("arxiv", now=START).granted is True
    assert parse_stamp("soon") is None


# -- it survives the process ---------------------------------------------


def test_state_survives_a_reopened_store(tmp_path: Path) -> None:
    """The whole point of persisting it."""

    path = tmp_path / "literature.sqlite3"
    with LiteratureStore.open(path) as first:
        first.pacer().record_rate_limited(
            "openalex", now=START, retry_after_seconds=3600, detail="429 daily budget"
        )
    with LiteratureStore.open(path) as second:
        refused = second.pacer().reserve("openalex", now=START + timedelta(minutes=5))
        assert refused.granted is False
        assert second.pacer().health("openalex").last_detail == "429 daily budget"


# -- concurrency ---------------------------------------------------------


def test_two_processes_do_not_both_reserve_the_same_slot(tmp_path: Path) -> None:
    """The race the transaction exists for.

    Two stores, two connections, one database -- which is what two Research OS
    runs on one machine actually are. The second must see what the first
    committed, not the state it read a moment before.
    """

    path = tmp_path / "literature.sqlite3"
    with LiteratureStore.open(path) as first, LiteratureStore.open(path) as second:
        assert first.pacer().reserve("arxiv", now=START).granted is True
        refused = second.pacer().reserve("arxiv", now=START)
        assert refused.granted is False
        assert "minimum interval" in refused.reason


def test_a_reservation_takes_its_lock_before_it_reads(tmp_path: Path) -> None:
    """``BEGIN IMMEDIATE``, not a deferred transaction that upgrades later.

    This is the assertion that distinguishes the two, and the first version of
    it did not: accepting any database error let a deferred transaction pass,
    because *that* also fails -- just later, from the write, after it has already
    read a row another run was in the middle of replacing. Mutating the BEGIN
    proved the test blind.

    So the discriminator is *which* failure. A run that takes the lock first
    fails at the BEGIN, as this module's own orderly error. A run that reads
    first fails at the UPDATE, as a raw ``sqlite3.OperationalError`` escaping a
    retrieval, having already made its decision on stale data.
    """

    path = tmp_path / "literature.sqlite3"
    with LiteratureStore.open(path) as first, LiteratureStore.open(path) as second:
        second.connection.execute("PRAGMA busy_timeout = 0")
        first.connection.isolation_level = None
        first.connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(
                LiteratureStoreError, match="cannot take the literature pacing lock"
            ):
                second.pacer().reserve("arxiv", now=START)
        finally:
            first.connection.execute("ROLLBACK")
            first.connection.isolation_level = ""


def test_a_reservation_in_flight_blocks_another_from_reading_the_slot(
    tmp_path: Path,
) -> None:
    """The interleaving itself: A is mid-reservation when B arrives.

    ``_immediate`` is entered directly rather than through ``reserve`` because
    the point is to stop A between its read and its write, which is precisely
    the window a deferred transaction would leave open for B to read through.
    """

    path = tmp_path / "literature.sqlite3"
    with LiteratureStore.open(path) as first, LiteratureStore.open(path) as second:
        second.connection.execute("PRAGMA busy_timeout = 0")
        held = first.pacer()._immediate()
        connection = held.__enter__()
        try:
            connection.execute(
                "SELECT * FROM source_health WHERE source = 'arxiv'"
            ).fetchone()
            with pytest.raises(
                LiteratureStoreError, match="cannot take the literature pacing lock"
            ):
                second.pacer().reserve("arxiv", now=START)
        finally:
            held.__exit__(None, None, None)


def test_the_transaction_mode_is_restored_after_use() -> None:
    store = LiteratureStore.open_memory()
    before = store.connection.isolation_level
    store.pacer().reserve("arxiv", now=START)
    assert store.connection.isolation_level == before


def test_a_failed_reservation_leaves_no_partial_write() -> None:
    store = LiteratureStore.open_memory()
    pacer = SourcePacer(store.connection)
    with pytest.raises(ZeroDivisionError), pacer._immediate() as connection:
        connection.execute("INSERT INTO source_health(source) VALUES('openalex')")
        raise ZeroDivisionError
    assert pacer.health("openalex") == SourceHealth(source="openalex")


# -- the service uses it -------------------------------------------------


def test_a_retrieval_reserves_before_it_asks() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(store, [openalex_page()], clock=clock)
    report = built.retrieve("widgets")
    assert report.reached == ("openalex",)
    assert len(client.requests) == 1
    assert store.pacer().health("openalex").last_status == "OK"


def test_a_second_retrieval_inside_the_interval_asks_nobody() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(store, [openalex_page()], clock=clock)
    built.retrieve("widgets")
    report = built.retrieve("widgets")
    assert len(client.requests) == 1, "the interval was not respected"
    assert report.rate_limited
    assert report.network_calls == 0
    assert report.complete is False


def test_a_rate_limit_is_never_reported_as_zero_literature() -> None:
    """ "We did not ask" and "there is nothing" are different facts."""

    clock = Clock()
    store = LiteratureStore.open_memory()
    built, _ = service(store, [openalex_page()], clock=clock)
    built.retrieve("widgets")
    report = built.retrieve("widgets")
    outcome = report.outcomes[0]
    assert outcome.status is SourceStatus.RATE_LIMITED
    assert outcome.ok is False
    assert report.skipped
    assert outcome.next_allowed_at is not None


def test_a_timeout_and_a_zero_result_are_different_rows() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    empty, _ = service(
        store,
        [json_response("api.openalex.org", {"results": [], "meta": {"count": 0}})],
        clock=clock,
    )
    assert empty.retrieve("nothing matches this").outcomes[0].status is SourceStatus.OK

    clock.advance(60)
    broken, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                transport_error="timed out",
                transport_failures=None,
            )
        ],
        clock=clock,
    )
    outcome = broken.retrieve("anything").outcomes[0]
    assert outcome.status is SourceStatus.UNAVAILABLE
    assert store.pacer().health("openalex").availability(clock.now) == "UNAVAILABLE"


def test_a_429_from_a_provider_is_persisted_by_the_service() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                status=429,
                headers={"retry-after": "3600"},
                body=b"{}",
            )
        ],
        clock=clock,
    )
    report = built.retrieve("widgets")
    assert report.outcomes[0].status is SourceStatus.RATE_LIMITED
    health = store.pacer().health("openalex")
    assert health.rate_limited_until == utc_stamp(START + timedelta(hours=1))
    assert health.availability(clock.now) == "RATE_LIMITED"


def test_a_date_form_retry_after_is_persisted() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    when = format_datetime(datetime.now(UTC) + timedelta(hours=2))
    built, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                status=429,
                headers={"retry-after": when},
                body=b"{}",
            )
        ],
        clock=clock,
    )
    built.retrieve("widgets")
    limited = parse_stamp(store.pacer().health("openalex").rate_limited_until)
    assert limited is not None
    assert limited > clock.now + timedelta(hours=1)


def test_a_long_rate_limit_does_not_stop_the_other_providers() -> None:
    """One provider asking for an hour must not end a literature review."""

    clock = Clock()
    store = LiteratureStore.open_memory()
    built, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                status=429,
                headers={"retry-after": "3600"},
                body=b"{}",
            ),
            crossref_page(),
            arxiv_page(),
        ],
        clock=clock,
        enabled=("openalex", "crossref", "arxiv"),
    )
    report = built.retrieve("widgets")
    assert "openalex" not in report.reached
    assert set(report.reached) == {"crossref", "arxiv"}
    assert report.work_keys


def test_crossref_behaviour_is_otherwise_unchanged() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(
        store, [crossref_page()], clock=clock, enabled=("crossref",)
    )
    report = built.retrieve("widget dynamics")
    assert report.reached == ("crossref",)
    assert report.complete is True
    assert len(report.work_keys) == 1
    assert "api.crossref.org" in client.urls()[0]


# -- cache first ---------------------------------------------------------


def test_a_cached_query_costs_no_provider_request() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(
        store, [openalex_page()], clock=clock, cache_ttl_seconds=3600
    )
    first = built.retrieve("widgets")
    clock.advance(600)
    second = built.retrieve("widgets")

    assert len(client.requests) == 1, "the cache did not prevent a second request"
    assert second.network_calls == 0
    assert second.cached
    assert second.outcomes[0].from_cache is True
    assert second.work_keys == first.work_keys
    assert second.reached == ("openalex",)


def test_a_stale_cache_entry_is_not_used() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(store, [openalex_page()], clock=clock, cache_ttl_seconds=60)
    built.retrieve("widgets")
    clock.advance(3600)
    built.retrieve("widgets")
    assert len(client.requests) == 2


def test_a_different_query_is_not_a_cache_hit() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(
        store, [openalex_page()], clock=clock, cache_ttl_seconds=3600
    )
    built.retrieve("widgets")
    clock.advance(600)
    built.retrieve("gadgets")
    assert len(client.requests) == 2


def test_a_failed_search_is_never_served_as_a_cache_hit() -> None:
    """One outage must not become a permanent empty answer."""

    clock = Clock()
    store = LiteratureStore.open_memory()
    failing, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                transport_error="timed out",
                transport_failures=None,
            )
        ],
        clock=clock,
        cache_ttl_seconds=3600,
    )
    failing.retrieve("widgets")
    clock.advance(600)
    working, client = service(
        store, [openalex_page()], clock=clock, cache_ttl_seconds=3600
    )
    report = working.retrieve("widgets")
    assert len(client.requests) == 1
    assert report.reached == ("openalex",)


def test_the_cache_is_off_by_default_in_these_tests() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(store, [openalex_page()], clock=clock)
    built.retrieve("widgets")
    clock.advance(600)
    built.retrieve("widgets")
    assert len(client.requests) == 2


def test_a_cache_hit_is_served_even_while_the_source_is_rate_limited() -> None:
    """Cache first means first: before the slot, not after it."""

    clock = Clock()
    store = LiteratureStore.open_memory()
    built, client = service(
        store, [openalex_page()], clock=clock, cache_ttl_seconds=3600
    )
    built.retrieve("widgets")
    store.pacer().record_rate_limited(
        "openalex", now=clock.now, retry_after_seconds=7200
    )
    clock.advance(60)
    report = built.retrieve("widgets")
    assert report.outcomes[0].from_cache is True
    assert report.reached == ("openalex",)
    assert len(client.requests) == 1


# -- what a researcher sees ----------------------------------------------


def test_the_source_report_shows_persisted_state() -> None:
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, _ = service(
        store,
        [
            ScriptedResponse(
                match="api.openalex.org",
                status=429,
                headers={"retry-after": "3600"},
                body=b"{}",
            )
        ],
        clock=clock,
    )
    built.retrieve("widgets")
    probes = built.probe()
    health = {item.source: item for item in store.pacer().all_health()}
    rendered = render_sources(probes, health, now=clock.now)
    assert "RATE_LIMITED" in rendered
    assert utc_stamp(START + timedelta(hours=1)) in rendered
    assert "429" in rendered


def test_the_source_report_never_prints_a_credential(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_OS_OPENALEX_API_KEY", "super-secret-value")
    clock = Clock()
    store = LiteratureStore.open_memory()
    built, _ = service(store, [openalex_page()], clock=clock)
    rendered = render_sources(
        built.probe(),
        {item.source: item for item in store.pacer().all_health()},
        now=clock.now,
    )
    assert "super-secret-value" not in rendered
