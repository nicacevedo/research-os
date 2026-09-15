"""OpenAlex: broad metadata, citation counts, and open-access links.

Everything about this adapter's interface was verified against the live API and
the current documentation rather than recalled:

* ``https://api.openalex.org/works`` answers without any credential.
* A key is optional and multiplies the daily budget. It is passed as
  ``Authorization: Bearer <key>`` rather than in the query string, so it does
  not end up in a recorded ``request_url`` or a shell history.
* The ``mailto`` polite pool was **deprecated in February 2026**. It is
  therefore not sent as a query parameter; the contact address goes in the
  ``User-Agent``, which every provider still asks for.
* Budget is reported in ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``
  headers and a request costs ``X-RateLimit-Credits-Used``. Those are recorded
  so a run can say why retrieval stopped instead of guessing.
* The hard ceiling is 100 requests per second; exceeding the daily budget
  returns ``429``.

Abstracts arrive as an inverted index -- a map from word to the positions it
occupies -- rather than as text, so reconstructing one is arithmetic done here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from research_os.errors import SourceUnavailableError
from research_os.literature.http import (
    HostPolicy,
    HttpClient,
    encode_query,
    retry_after_seconds,
    retry_after_when_throttled,
)
from research_os.literature.identity import normalize_doi
from research_os.literature.models import SourceProbe, SourceStatus
from research_os.literature.sources.base import (
    ProviderRecord,
    SourceResult,
    author_records,
    clean_text,
    first_int,
)

NAME = "openalex"
BASE_URL = "https://api.openalex.org"
HOST = "api.openalex.org"

#: Where an optional API key is read from. Never a literal in configuration.
API_KEY_ENV = "RESEARCH_OS_OPENALEX_API_KEY"

#: Verified from the live API: 100 requests per second is the hard ceiling.
#: One request every 0.2 seconds stays far inside it while several adapters run.
MIN_INTERVAL_SECONDS = 0.2

#: The fields actually used. Requesting only these keeps payloads small enough
#: to archive verbatim, which is what makes a merged field auditable.
SELECT_FIELDS = (
    "id,doi,title,display_name,publication_year,type,cited_by_count,"
    "abstract_inverted_index,primary_location,open_access,authorships,"
    "is_retracted,referenced_works,ids"
)

MAX_PER_PAGE = 200


@dataclass
class OpenAlexSource:
    """Adapter for the OpenAlex works API."""

    name: str = NAME
    base_url: str = BASE_URL
    client: HttpClient = field(default_factory=HttpClient)
    api_key: str | None = None

    def __post_init__(self) -> None:
        self.client.policies.setdefault(
            HOST,
            HostPolicy(
                host=HOST,
                min_interval_seconds=MIN_INTERVAL_SECONDS,
                note="OpenAlex allows up to 100 requests/second against a daily budget",
            ),
        )
        if self.api_key is None:
            self.api_key = (os.environ.get(API_KEY_ENV) or "").strip() or None

    # -- contract --------------------------------------------------------

    def probe(self) -> SourceProbe:
        """Report readiness without spending a request.

        Deliberately local. A probe that called the API would consume part of
        the very budget it is reporting on, and ``researchctl doctor`` should be
        free to run.
        """

        return SourceProbe(
            name=self.name,
            base_url=self.base_url,
            status=SourceStatus.UNAVAILABLE if self.client.offline else SourceStatus.OK,
            requires_credential=False,
            credential_env=API_KEY_ENV,
            credential_present=self.api_key is not None,
            contact_configured=bool(self.client.contact_email),
            rate_limit_note=(
                "no key required; a free key in "
                f"{API_KEY_ENV} multiplies the daily budget. Hard ceiling 100 "
                "requests/second. The mailto polite pool was deprecated in "
                "February 2026, so the contact address is sent in the User-Agent."
            ),
            detail=(
                "offline mode is enabled"
                if self.client.offline
                else "usable without a credential"
            ),
        )

    def search(self, query: str, *, limit: int = 20) -> SourceResult:
        url = f"{self.base_url}/works?" + encode_query(
            {
                "search": query,
                "per-page": min(max(limit, 1), MAX_PER_PAGE),
                "select": SELECT_FIELDS,
            }
        )
        return self._collect(url)

    def fetch(self, identifier: str) -> SourceResult:
        """Fetch one work by DOI or OpenAlex id.

        OpenAlex addresses a work by either, so the adapter does not make the
        caller know which kind of string it is holding.
        """

        doi = normalize_doi(identifier)
        token = f"doi:{doi}" if doi else identifier.strip()
        url = f"{self.base_url}/works/{token}?" + encode_query(
            {"select": SELECT_FIELDS}
        )
        return self._collect(url, single=True)

    # -- internals -------------------------------------------------------

    def _collect(self, url: str, *, single: bool = False) -> SourceResult:
        try:
            response = self.client.get(
                url, headers=self._headers(), credential_host=HOST
            )
        except SourceUnavailableError as exc:
            return SourceResult(
                provider=self.name, status=SourceStatus.UNAVAILABLE, detail=str(exc)
            )
        note = _rate_limit_note(response.headers)
        if response.status == 429:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.RATE_LIMITED,
                request_url=url,
                detail="OpenAlex returned 429; the daily budget is exhausted",
                retry_after_seconds=retry_after_seconds(response),
                rate_limit_note=note,
            )
        if response.status == 404:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.OK,
                request_url=url,
                detail="OpenAlex has no record for that identifier",
                rate_limit_note=note,
            )
        if response.status != 200:
            return SourceResult(
                retry_after_seconds=retry_after_when_throttled(response),
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"OpenAlex returned HTTP {response.status}",
                rate_limit_note=note,
            )
        try:
            payload = json.loads(response.text())
        except ValueError as exc:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"OpenAlex returned a body that is not JSON: {exc}",
                rate_limit_note=note,
            )
        entries = [payload] if single else (payload.get("results") or [])
        records = tuple(
            record
            for item in entries
            if isinstance(item, dict)
            and (record := self.to_record(item, request_url=url)) is not None
        )
        return SourceResult(
            provider=self.name,
            status=SourceStatus.OK,
            records=records,
            request_url=url,
            rate_limit_note=note,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def to_record(
        self, payload: dict[str, Any], *, request_url: str = ""
    ) -> ProviderRecord | None:
        """Translate one OpenAlex work object into the neutral record shape."""

        openalex_id = payload.get("id")
        title = clean_text(payload.get("title") or payload.get("display_name"))
        if not openalex_id and not title:
            return None
        ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else {}
        identifiers: dict[str, str] = {}
        if isinstance(openalex_id, str):
            identifiers["openalex"] = openalex_id
        for scheme, key in (("doi", "doi"), ("pmid", "pmid")):
            value = payload.get(key) or ids.get(key)
            if isinstance(value, str):
                identifiers[scheme] = value

        authorships = payload.get("authorships")
        names: list[str] = []
        orcids: list[str | None] = []
        if isinstance(authorships, list):
            for item in authorships:
                if not isinstance(item, dict):
                    continue
                author = item.get("author")
                if not isinstance(author, dict):
                    continue
                name = clean_text(author.get("display_name"))
                if name:
                    names.append(name)
                    orcid = author.get("orcid")
                    orcids.append(orcid if isinstance(orcid, str) else None)

        location = payload.get("primary_location")
        venue = None
        if isinstance(location, dict):
            source = location.get("source")
            if isinstance(source, dict):
                venue = clean_text(source.get("display_name"))
        access = payload.get("open_access")
        open_url = None
        if isinstance(access, dict):
            open_url = clean_text(access.get("oa_url"))

        referenced = payload.get("referenced_works")
        references = (
            tuple(item for item in referenced if isinstance(item, str))
            if isinstance(referenced, list)
            else ()
        )

        return ProviderRecord(
            provider=self.name,
            payload=payload,
            fields={
                "title": title,
                "abstract": _abstract_from_inverted_index(
                    payload.get("abstract_inverted_index")
                ),
                "publication_year": first_int(payload.get("publication_year")),
                "venue": venue,
                "work_type": clean_text(payload.get("type")),
                "cited_by_count": first_int(payload.get("cited_by_count")),
                "open_access_url": open_url,
                "is_retracted": bool(payload.get("is_retracted")),
            },
            identifiers=identifiers,
            authors=author_records(names, orcids),
            provider_work_id=openalex_id if isinstance(openalex_id, str) else None,
            request_url=request_url,
            pdf_url=open_url,
            referenced_ids=references,
        )


def _abstract_from_inverted_index(value: Any) -> str | None:
    """Rebuild an abstract from OpenAlex's word-to-positions map.

    OpenAlex does not ship abstract text; it ships the inverted index, which is
    the same information with the word order taken out and put in the values.
    Reassembling it is deterministic: every position appears once, so the
    result is the original word sequence. A position the index does not cover is
    left as a gap rather than filled in, because inventing a word would be
    fabricating part of a source.
    """

    if not isinstance(value, dict) or not value:
        return None
    positions: dict[int, str] = {}
    for word, places in value.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int) and not isinstance(place, bool):
                positions[place] = str(word)
    if not positions:
        return None
    ordered = [positions.get(index, "") for index in range(max(positions) + 1)]
    return " ".join(item for item in ordered if item) or None


def _rate_limit_note(headers: dict[str, str]) -> str:
    """Return what the response said about the remaining budget.

    Recorded rather than acted on. The client already paces requests; this is so
    a run that stops early can say "the daily budget was 1000 and 4 remained"
    instead of "retrieval failed".
    """

    limit = headers.get("x-ratelimit-limit")
    remaining = headers.get("x-ratelimit-remaining")
    used = headers.get("x-ratelimit-credits-used")
    reset = headers.get("x-ratelimit-reset")
    if limit is None and remaining is None:
        return ""
    parts = [f"budget {remaining or '?'}/{limit or '?'}"]
    if used:
        parts.append(f"cost {used}")
    if reset:
        parts.append(f"resets in {reset}s")
    return "; ".join(parts)
