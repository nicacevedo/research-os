"""What the literature client does when the network misbehaves.

The first live run is the reason this file exists. Crossref answered, arXiv
timed out, and OpenAlex returned 429 with its daily budget exhausted -- and the
arXiv leg was lost to a *single* timeout that nothing ever retried. A literature
record that says "arXiv was unavailable" when one more attempt would have
reached it is not a reproducible record, it is a guess.

The property these tests exist to keep is the one a review depends on: a
provider that could not be reached is never reported as a provider that found
nothing. Every case below therefore ends in an assertion about which of those
two a caller is told.

Nothing here opens a socket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from research_os.errors import SourceUnavailableError
from research_os.literature.http import (
    MAX_INLINE_WAIT_SECONDS,
    MAX_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    HttpClient,
    retry_after_seconds,
)
from research_os.literature.models import SourceStatus
from research_os.literature.sources.arxiv import ArxivSource
from research_os.literature.sources.crossref import CrossrefSource
from research_os.literature.sources.openalex import OpenAlexSource
from tests.literature_helpers import ScriptedClient, ScriptedResponse, json_response


def client(**kwargs: object) -> ScriptedClient:
    return ScriptedClient(**kwargs)  # type: ignore[arg-type]


def backoffs(waited: list[float]) -> list[float]:
    """The retry delays, separated from the per-host politeness intervals.

    Both go through the same sleep, so a test that read the raw list would be
    asserting about the default one-second host interval as much as about the
    backoff it meant to check.
    """

    return [value for value in waited if value >= 2.0]


# -- the transport itself -----------------------------------------------------


def test_a_request_that_succeeds_is_made_exactly_once() -> None:
    transport = client(responses=[ScriptedResponse(match="example", body=b"{}")])

    response = transport.get("https://example.invalid/x")

    assert response.status == 200
    assert len(transport.requests) == 1
    assert transport.waited == []


def test_a_timeout_is_retried_and_then_reported_as_unreachable() -> None:
    """The arXiv failure from the first live run, as a property."""

    transport = client(
        responses=[ScriptedResponse(match="example", transport_error="timed out")],
    )

    with pytest.raises(SourceUnavailableError) as unavailable:
        transport.get("https://example.invalid/x")

    assert len(transport.requests) == MAX_RETRIES + 1
    assert "timed out" in str(unavailable.value)
    assert f"after {MAX_RETRIES + 1} attempt(s)" in str(unavailable.value)


def test_one_transient_timeout_is_survived_rather_than_reported() -> None:
    """The whole point of the retry: a blip must not become a finding."""

    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                body=b'{"ok": true}',
                transport_failures=1,
                transport_error="timed out",
            )
        ],
    )

    response = transport.get("https://example.invalid/x")

    assert response.status == 200
    assert response.body == b'{"ok": true}'
    assert len(transport.requests) == 2, "one failure, one success"


def test_a_transient_server_error_is_survived() -> None:
    transport = client(
        responses=[
            ScriptedResponse(match="example.invalid/retry", status=500, body=b""),
        ],
    )

    response = transport.get("https://example.invalid/retry")

    assert response.status == 500
    assert len(transport.requests) == MAX_RETRIES + 1


def test_a_permanent_client_error_is_not_retried() -> None:
    """404 is the provider's answer, and arguing with it wastes its budget."""

    transport = client(responses=[ScriptedResponse(match="example", status=404)])

    response = transport.get("https://example.invalid/x")

    assert response.status == 404
    assert len(transport.requests) == 1
    assert transport.waited == []


def test_a_400_is_not_retried_either() -> None:
    transport = client(responses=[ScriptedResponse(match="example", status=400)])

    transport.get("https://example.invalid/x")

    assert len(transport.requests) == 1


def test_retries_are_bounded_even_against_a_host_that_never_recovers() -> None:
    transport = client(
        responses=[ScriptedResponse(match="example", transport_error="reset")],
    )

    with pytest.raises(SourceUnavailableError):
        transport.get("https://example.invalid/x")

    assert len(transport.requests) == MAX_RETRIES + 1, "no retry storm"
    assert len(backoffs(transport.waited)) == MAX_RETRIES


def test_backoff_between_transport_retries_grows_and_is_capped() -> None:
    transport = client(
        responses=[ScriptedResponse(match="example", transport_error="reset")],
    )

    with pytest.raises(SourceUnavailableError):
        transport.get("https://example.invalid/x")

    delays = backoffs(transport.waited)
    assert delays == sorted(delays), "backoff must not shrink"
    assert delays[0] < delays[-1], "and must actually grow"
    assert max(delays) <= MAX_RETRY_AFTER_SECONDS


# -- Retry-After --------------------------------------------------------------


def test_a_numeric_retry_after_is_honoured() -> None:
    transport = client(
        responses=[
            ScriptedResponse(
                match="example", status=429, headers={"retry-after": "7"}, body=b""
            )
        ],
    )

    transport.get("https://example.invalid/x")

    # Politeness waits are recorded here too, so the assertion is that the
    # header's value is what the retries actually used.
    assert transport.waited.count(7.0) == MAX_RETRIES


def test_a_date_form_retry_after_is_honoured_rather_than_ignored() -> None:
    """RFC 9110 allows both forms, so reading only one is reading it wrong.

    A date-form header used to fall through to the default backoff, which means
    a client that believed it was being polite was retrying far sooner than it
    had been asked to. That is how an address gets blocked.
    """

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=429,
                headers={"retry-after": when},
                body=b"",
            )
        ],
    )

    transport.get("https://example.invalid/x")

    assert transport.waited
    assert 20.0 <= max(transport.waited) <= MAX_RETRY_AFTER_SECONDS


def test_a_date_form_retry_after_in_the_past_means_retry_now() -> None:
    when = format_datetime(datetime.now(UTC) - timedelta(hours=1))
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=503,
                headers={"retry-after": when},
                body=b"",
            )
        ],
    )

    transport.get("https://example.invalid/x")

    assert all(value >= 0 for value in transport.waited)


def test_a_malformed_retry_after_falls_back_instead_of_failing() -> None:
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=429,
                headers={"retry-after": "whenever you feel like it"},
                body=b"",
            )
        ],
    )

    response = transport.get("https://example.invalid/x")

    assert response.status == 429
    assert transport.waited and max(transport.waited) <= MAX_RETRY_AFTER_SECONDS


def test_a_retry_after_far_in_the_future_is_not_waited_out() -> None:
    """A provider asking for a week is recorded, not slept through.

    v1.0.0 clamped the wait to a minute and retried anyway, twice, which is two
    minutes a bounded run does not have to spend arguing with a provider that
    has already said when it will answer. Now the response comes straight back
    with its header intact; :mod:`research_os.literature.pacing` persists what
    was asked for, and the rest of the retrieval asks the providers that will
    answer.
    """

    when = format_datetime(datetime.now(UTC) + timedelta(days=7))
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=429,
                headers={"retry-after": when},
                body=b"",
            )
        ],
    )

    response = transport.get("https://example.invalid/x")

    assert response.status == 429
    assert transport.waited == [], "a week-long Retry-After must not be slept on"
    assert len(transport.requests) == 1, "nor retried against"
    asked = retry_after_seconds(response)
    assert asked is not None and asked > MAX_INLINE_WAIT_SECONDS


def test_a_short_retry_after_is_still_honoured_inline() -> None:
    """The other half: a provider asking for seconds still gets its seconds."""

    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=429,
                headers={"retry-after": "5"},
                body=b"",
            )
        ],
    )

    transport.get("https://example.invalid/x")

    assert transport.waited
    assert max(transport.waited) == 5.0
    assert len(transport.requests) == MAX_RETRIES + 1


# -- a settled refusal is never retried ---------------------------------------


def test_plaintext_is_refused_without_a_single_attempt() -> None:
    transport = client(responses=[ScriptedResponse(match="example")])

    with pytest.raises(SourceUnavailableError, match="HTTPS only"):
        transport.get("http://example.invalid/x")

    assert transport.requests == [], "a settled refusal must not be retried"


def test_offline_mode_is_refused_without_a_single_attempt() -> None:
    transport = client(responses=[ScriptedResponse(match="example")], offline=True)

    with pytest.raises(SourceUnavailableError, match="offline"):
        transport.get("https://example.invalid/x")

    assert transport.requests == []


def test_the_real_client_translates_a_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the real ``_perform``, which the scripted client replaces.

    Without this the retry tests above would all be asserting against a
    translation the test helper performs, and a regression in the client's own
    ``except`` clauses would go unnoticed.
    """

    waited: list[float] = []

    def never_answers(*_args: object, **_kwargs: object):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", never_answers)
    monkeypatch.setattr(
        HttpClient, "_sleep", staticmethod(lambda seconds: waited.append(seconds))
    )

    with pytest.raises(SourceUnavailableError) as unavailable:
        HttpClient().get("https://example.invalid/x")

    assert "example.invalid" in str(unavailable.value)
    assert len(backoffs(waited)) == MAX_RETRIES, "one backoff per retry, no more"


# -- what the adapters make of it ---------------------------------------------


def test_an_unreachable_openalex_is_unavailable_never_zero_results() -> None:
    transport = client(
        responses=[ScriptedResponse(match="openalex", transport_error="timed out")],
    )
    result = OpenAlexSource(client=transport).search("widgets")

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.records == ()
    assert result.status is not SourceStatus.OK, "a failure is not an empty result"


def test_an_unreachable_arxiv_is_unavailable_never_zero_results() -> None:
    transport = client(
        responses=[ScriptedResponse(match="arxiv", transport_error="timed out")],
    )
    result = ArxivSource(client=transport).search("widgets")

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.records == ()
    assert "timed out" in result.detail


def test_an_unreachable_crossref_is_unavailable_never_zero_results() -> None:
    transport = client(
        responses=[ScriptedResponse(match="crossref", transport_error="timed out")],
    )
    result = CrossrefSource(client=transport).search("widgets")

    assert result.status is SourceStatus.UNAVAILABLE
    assert result.records == ()


def test_a_rate_limited_openalex_is_distinguished_from_an_empty_result() -> None:
    """The 429 the first live run actually received."""

    transport = client(
        responses=[
            ScriptedResponse(
                match="openalex",
                status=429,
                headers={"retry-after": "1", "x-ratelimit-remaining": "0"},
                body=b"",
            )
        ],
    )
    result = OpenAlexSource(client=transport).search("widgets")

    assert result.status is SourceStatus.RATE_LIMITED
    assert result.records == ()
    assert result.status is not SourceStatus.OK


def test_a_genuinely_empty_openalex_result_is_ok_and_says_so() -> None:
    """The other side of the distinction, which is what gives it meaning."""

    transport = client(
        responses=[json_response("openalex", {"results": [], "meta": {"count": 0}})],
    )
    result = OpenAlexSource(client=transport).search("widgets that do not exist")

    assert result.status is SourceStatus.OK
    assert result.records == ()


def test_a_malformed_openalex_body_is_a_failure_not_an_empty_result() -> None:
    transport = client(
        responses=[ScriptedResponse(match="openalex", body=b"{not json")],
    )
    result = OpenAlexSource(client=transport).search("widgets")

    assert result.status is not SourceStatus.OK
    assert result.records == ()


def test_an_adapter_recovers_from_one_transient_failure() -> None:
    """Crossref is the provider that worked; generalising retries must not break it."""

    transport = client(
        responses=[
            ScriptedResponse(
                match="crossref",
                body=b'{"message": {"items": []}}',
                headers={"content-type": "application/json"},
                transport_failures=1,
                transport_error="connection reset",
            )
        ],
    )
    result = CrossrefSource(client=transport).search("widgets")

    assert result.status is SourceStatus.OK
    assert len(transport.requests) == 2


def test_a_non_ascii_digit_retry_after_does_not_crash_the_request() -> None:
    """``str.isdigit()`` is True for characters ``float`` refuses.

    ``Retry-After: ²`` — superscript two — passed the digit check and then
    raised ValueError out of a request that had already reached the provider,
    escaping as something no caller catches. Found by an independent review.
    """

    transport = client(
        responses=[
            ScriptedResponse(
                match="example", status=429, headers={"retry-after": "²"}, body=b""
            )
        ],
    )

    response = transport.get("https://example.invalid/x")

    assert response.status == 429, "the provider's answer still came back"
    assert backoffs(transport.waited), "it fell back to exponential backoff"
    assert max(transport.waited) <= MAX_RETRY_AFTER_SECONDS
