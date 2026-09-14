"""Crossref: DOI metadata, and the authoritative signal that a paper was retracted.

Verified against the live API rather than recalled:

* ``https://api.crossref.org`` needs no sign-up and no credential.
* Supplying a contact address moves a client into the polite pool. The live
  response confirms it: ``x-api-pool: polite-single``, with
  ``x-rate-limit-limit: 10`` per ``x-rate-limit-interval: 1s`` and
  ``x-concurrency-limit: 3``.
* ``/works/{doi}`` returns one work; ``/works?query.bibliographic=`` searches.
* Post-publication updates are carried in ``update-to`` and ``updated-by``. A
  ``retraction`` there is the difference between a paper and a paper nobody
  should be citing, which is why this adapter exists even though OpenAlex also
  has metadata.

The polite pool matters here in a way it does not elsewhere: without a contact
address the anonymous pool is explicitly not guaranteed any rate at all, so the
probe says plainly when one is not configured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from research_os.errors import SourceUnavailableError
from research_os.literature.http import (
    HostPolicy,
    HttpClient,
    encode_query,
    retry_after_seconds,
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

NAME = "crossref"
BASE_URL = "https://api.crossref.org"
HOST = "api.crossref.org"

#: The live polite-pool headers say 10 requests per second. One every 0.15
#: seconds stays inside that with room for the other adapters in one process.
MIN_INTERVAL_SECONDS = 0.15

#: The update types Crossref uses that mean "do not rely on this paper".
#:
#: Concern and removal are included alongside retraction: all three tell a
#: reader that citing the work as established is a mistake, and collapsing them
#: into "retracted" for the index while keeping the exact word in the note is
#: more useful than three nearly-identical flags.
WITHDRAWING_UPDATES: frozenset[str] = frozenset(
    {"retraction", "withdrawal", "removal", "expression_of_concern"}
)

MAX_ROWS = 100


@dataclass
class CrossrefSource:
    """Adapter for the Crossref REST API."""

    name: str = NAME
    base_url: str = BASE_URL
    client: HttpClient = field(default_factory=HttpClient)

    def __post_init__(self) -> None:
        self.client.policies.setdefault(
            HOST,
            HostPolicy(
                host=HOST,
                min_interval_seconds=MIN_INTERVAL_SECONDS,
                note="Crossref polite pool: 10 requests/second, 3 concurrent",
            ),
        )

    def probe(self) -> SourceProbe:
        polite = bool(self.client.contact_email)
        return SourceProbe(
            name=self.name,
            base_url=self.base_url,
            status=SourceStatus.UNAVAILABLE if self.client.offline else SourceStatus.OK,
            requires_credential=False,
            credential_env=None,
            credential_present=False,
            contact_configured=polite,
            rate_limit_note=(
                "polite pool: 10 requests/second, 3 concurrent"
                if polite
                else "anonymous pool: no guaranteed rate"
            ),
            detail=(
                "offline mode is enabled"
                if self.client.offline
                else (
                    "usable; a contact address is configured, so requests use the "
                    "polite pool"
                    if polite
                    else "usable, but no contact address is configured, so requests "
                    "fall in the anonymous pool, which Crossref guarantees no rate for"
                )
            ),
        )

    def search(self, query: str, *, limit: int = 20) -> SourceResult:
        url = f"{self.base_url}/works?" + encode_query(
            {
                "query.bibliographic": query,
                "rows": min(max(limit, 1), MAX_ROWS),
                "select": (
                    "DOI,title,author,issued,container-title,type,abstract,"
                    "update-to,updated-by,is-referenced-by-count,URL"
                ),
                **self._polite(),
            }
        )
        return self._collect(url)

    def fetch(self, identifier: str) -> SourceResult:
        doi = normalize_doi(identifier)
        if doi is None:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.OK,
                detail=f"{identifier!r} is not a DOI, and Crossref is addressed by DOI",
            )
        url = f"{self.base_url}/works/{doi}"
        query = encode_query(self._polite())
        if query:
            url = f"{url}?{query}"
        return self._collect(url, single=True)

    def _polite(self) -> dict[str, str | None]:
        """Return the polite-pool parameter, when a contact address is known."""

        return {"mailto": self.client.contact_email}

    def _collect(self, url: str, *, single: bool = False) -> SourceResult:
        try:
            response = self.client.get(url, headers={"Accept": "application/json"})
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
                detail="Crossref returned 429",
                retry_after_seconds=retry_after_seconds(response),
                rate_limit_note=note,
            )
        if response.status == 404:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.OK,
                request_url=url,
                detail="Crossref has no record for that DOI",
                rate_limit_note=note,
            )
        if response.status != 200:
            return SourceResult(
                retry_after_seconds=retry_after_seconds(response),
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"Crossref returned HTTP {response.status}",
                rate_limit_note=note,
            )
        try:
            body = json.loads(response.text())
        except ValueError as exc:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"Crossref returned a body that is not JSON: {exc}",
                rate_limit_note=note,
            )
        message = body.get("message")
        if not isinstance(message, dict):
            return SourceResult(
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail="Crossref response had no message object",
                rate_limit_note=note,
            )
        entries = [message] if single else (message.get("items") or [])
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

    def to_record(
        self, payload: dict[str, Any], *, request_url: str = ""
    ) -> ProviderRecord | None:
        """Translate one Crossref work into the neutral record shape."""

        doi = payload.get("DOI")
        title = _first_string(payload.get("title"))
        if not doi and not title:
            return None
        names: list[str] = []
        orcids: list[str | None] = []
        author = payload.get("author")
        if isinstance(author, list):
            for item in author:
                if not isinstance(item, dict):
                    continue
                given = clean_text(item.get("given")) or ""
                family = clean_text(item.get("family")) or ""
                display = clean_text(item.get("name")) or " ".join(
                    part for part in (given, family) if part
                )
                if display:
                    names.append(display)
                    orcid = item.get("ORCID")
                    orcids.append(orcid if isinstance(orcid, str) else None)

        retracted, note = _withdrawal(payload)
        return ProviderRecord(
            provider=self.name,
            payload=payload,
            fields={
                "title": clean_text(title),
                "abstract": _strip_jats(payload.get("abstract")),
                "publication_year": _issued_year(payload.get("issued")),
                "venue": clean_text(_first_string(payload.get("container-title"))),
                "work_type": clean_text(payload.get("type")),
                "cited_by_count": first_int(payload.get("is-referenced-by-count")),
                "is_retracted": retracted,
                "retraction_note": note,
            },
            identifiers={"doi": str(doi)} if isinstance(doi, str) else {},
            authors=author_records(names, orcids),
            provider_work_id=str(doi) if isinstance(doi, str) else None,
            request_url=request_url,
        )


def _withdrawal(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return whether Crossref says this work was withdrawn, and in what words.

    ``updated-by`` is the one that matters: it names the notices that point *at*
    this work, which is how a retraction is expressed. ``update-to`` points the
    other way -- this work is itself the notice -- and is reported in the note
    rather than treated as this work being retracted, because a retraction
    notice is not a retracted paper.
    """

    notes: list[str] = []
    retracted = False
    for item in payload.get("updated-by") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        target = str(item.get("DOI") or "").strip()
        if kind in WITHDRAWING_UPDATES:
            retracted = True
            notes.append(f"{kind} by {target}" if target else kind)
    for item in payload.get("update-to") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        target = str(item.get("DOI") or "").strip()
        if kind in WITHDRAWING_UPDATES:
            notes.append(
                f"this record is itself a {kind} of {target}"
                if target
                else f"this record is itself a {kind}"
            )
    return retracted, "; ".join(notes) or None


def _issued_year(value: Any) -> int | None:
    """Return the publication year from Crossref's date-parts structure."""

    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return None
    first = parts[0]
    if not isinstance(first, list) or not first:
        return None
    return first_int(first[0])


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item
    return None


def _strip_jats(value: Any) -> str | None:
    """Return a Crossref abstract as plain text.

    Crossref abstracts are JATS XML fragments. Tags are removed rather than
    parsed: what is wanted is the sentences, and an XML parser here would be a
    parser pointed at untrusted input for no gain. Entity references that a
    reader would otherwise see as ``&amp;`` are decoded, and nothing else is
    interpreted.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    import html
    import re

    without_tags = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html.unescape(without_tags))


def _rate_limit_note(headers: dict[str, str]) -> str:
    limit = headers.get("x-rate-limit-limit")
    interval = headers.get("x-rate-limit-interval")
    pool = headers.get("x-api-pool")
    parts = []
    if pool:
        parts.append(f"pool {pool}")
    if limit and interval:
        parts.append(f"{limit} per {interval}")
    return "; ".join(parts)
