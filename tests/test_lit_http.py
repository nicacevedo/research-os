"""What the one networked component is and is not allowed to do.

Four properties, each of which would be a real problem if it silently stopped
holding: politeness is enforced rather than documented, a credential does not
follow a redirect off its host, plaintext HTTP is refused, and a response body is
bounded. None of them is visible in ordinary use, which is exactly why they need
tests.
"""

from __future__ import annotations

import io

import pytest

from research_os.errors import SourceUnavailableError
from research_os.literature.http import (
    MAX_RETRIES,
    HostPolicy,
    HttpClient,
    encode_query,
    read_bounded,
    user_agent,
)
from tests.literature_helpers import ScriptedClient, ScriptedResponse


def client(**kwargs: object) -> ScriptedClient:
    return ScriptedClient(**kwargs)  # type: ignore[arg-type]


# -- politeness ---------------------------------------------------------------


def test_a_second_request_to_one_host_waits_for_that_host_s_interval() -> None:
    """arXiv asks for three seconds, and the client sleeps rather than promising."""

    transport = client(
        responses=[ScriptedResponse(match="example.invalid", body=b"{}")],
    )
    transport.policies["example.invalid"] = HostPolicy(
        host="example.invalid", min_interval_seconds=3.0
    )

    transport.get("https://example.invalid/one")
    transport.get("https://example.invalid/two")

    assert transport.waited, "the second request did not wait"
    assert transport.waited[0] > 2.0


def test_two_hosts_do_not_wait_for_each_other() -> None:
    transport = client(
        responses=[
            ScriptedResponse(match="alpha.invalid", body=b"{}"),
            ScriptedResponse(match="beta.invalid", body=b"{}"),
        ],
    )
    transport.policies["alpha.invalid"] = HostPolicy(
        host="alpha.invalid", min_interval_seconds=3.0
    )

    transport.get("https://alpha.invalid/x")
    transport.get("https://beta.invalid/y")

    assert transport.waited == []


def test_adapters_sharing_one_client_share_one_budget() -> None:
    """Two adapters with two clients would each honour arXiv's three seconds.

    Between them that is a request every 1.5 seconds, which is a breach of the
    published terms, so the source registry hands every adapter the same client.
    """

    from research_os.literature.sources import default_sources

    transport = client()
    adapters = default_sources(transport)

    assert len({id(adapter.client) for adapter in adapters.values()}) == 1
    assert transport.policy_for("export.arxiv.org").min_interval_seconds == 3.0


# -- credentials --------------------------------------------------------------


def test_a_credential_reaches_the_host_it_was_configured_for() -> None:
    transport = client(responses=[ScriptedResponse(match="api.invalid", body=b"{}")])

    transport.get(
        "https://api.invalid/works",
        headers={"Authorization": "Bearer secret-token"},
        credential_host="api.invalid",
    )

    assert transport.headers_for("api.invalid")["Authorization"] == (
        "Bearer secret-token"
    )


def test_a_credential_does_not_follow_a_url_to_another_host() -> None:
    """A provider that points at a CDN must not be handed its own API key."""

    transport = client(responses=[ScriptedResponse(match="cdn.invalid", body=b"{}")])

    transport.get(
        "https://cdn.invalid/file.pdf",
        headers={"Authorization": "Bearer secret-token"},
        credential_host="api.invalid",
    )

    assert "Authorization" not in transport.headers_for("cdn.invalid")


def test_a_key_is_never_placed_in_the_url() -> None:
    """A recorded request_url ends up in the store and in reports."""

    from research_os.literature.sources.openalex import OpenAlexSource

    transport = client(
        responses=[ScriptedResponse(match="api.openalex.org", body=b'{"results":[]}')],
    )
    source = OpenAlexSource(client=transport, api_key="secret-token")

    result = source.search("widgets", limit=1)

    assert "secret-token" not in result.request_url
    assert "secret-token" not in "".join(transport.urls())
    assert transport.headers_for("api.openalex.org")["Authorization"] == (
        "Bearer secret-token"
    )


# -- transport rules ----------------------------------------------------------


def test_plaintext_http_is_refused() -> None:
    """Metadata read or altered in transit is not evidence."""

    transport = client(responses=[ScriptedResponse(match="example", body=b"{}")])

    with pytest.raises(SourceUnavailableError, match="HTTPS only"):
        transport.get("http://example.invalid/x")


def test_offline_mode_reaches_nothing() -> None:
    transport = client(offline=True)

    with pytest.raises(SourceUnavailableError, match="offline"):
        transport.get("https://example.invalid/x")
    assert transport.requests == []


def test_a_body_is_read_up_to_the_cap_and_marked_truncated() -> None:
    """A server that streams forever costs a bounded amount of memory."""

    body, truncated = read_bounded(io.BytesIO(b"x" * 100), 16)

    assert body == b"x" * 16
    assert truncated is True


def test_a_body_exactly_the_size_of_the_cap_is_not_reported_as_cut_short() -> None:
    """A truncated PDF is not a PDF, so the two cases must stay distinguishable."""

    body, truncated = read_bounded(io.BytesIO(b"x" * 16), 16)

    assert body == b"x" * 16
    assert truncated is False


def test_a_short_body_is_returned_whole() -> None:
    body, truncated = read_bounded(io.BytesIO(b"hello"), 1024)

    assert (body, truncated) == (b"hello", False)


def test_a_throttled_request_is_retried_a_bounded_number_of_times() -> None:
    transport = client(
        responses=[
            ScriptedResponse(
                match="example", status=429, headers={"retry-after": "1"}, body=b""
            )
        ],
    )

    response = transport.get("https://example.invalid/x")

    assert response.status == 429
    assert len(transport.requests) == MAX_RETRIES + 1, (
        "a provider saying stop must not be argued with indefinitely"
    )


def test_a_retry_after_is_honoured_but_bounded() -> None:
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                status=503,
                headers={"retry-after": "99999"},
                body=b"",
            )
        ],
    )

    transport.get("https://example.invalid/x")

    assert transport.waited
    assert max(transport.waited) <= 60


def test_an_unreachable_host_is_unavailable_rather_than_a_crash() -> None:
    transport = client(
        responses=[
            ScriptedResponse(
                match="example",
                raises=SourceUnavailableError("cannot reach example.invalid"),
            )
        ],
    )

    with pytest.raises(SourceUnavailableError):
        transport.get("https://example.invalid/x")


# -- identification -----------------------------------------------------------


def test_the_client_identifies_itself_even_without_a_contact_address() -> None:
    assert "research-os/" in user_agent(None)
    assert "mailto:" not in user_agent(None)


def test_a_contact_address_is_included_when_configured() -> None:
    assert "mailto:someone@example.invalid" in user_agent("someone@example.invalid")


def test_a_default_user_agent_is_always_sent() -> None:
    transport = client(responses=[ScriptedResponse(match="example", body=b"{}")])

    transport.get("https://example.invalid/x")

    assert "research-os/" in transport.headers_for("example.invalid")["User-Agent"]


def test_query_encoding_is_deterministic_and_drops_unset_values() -> None:
    """A recorded request URL is only comparable if it is built the same way."""

    first = encode_query({"b": "2", "a": "1", "c": None})
    second = encode_query({"a": "1", "c": None, "b": "2"})

    assert first == second == "a=1&b=2"


def test_a_default_policy_exists_for_a_host_nobody_declared() -> None:
    assert HttpClient().policy_for("unknown.invalid").min_interval_seconds >= 1.0
