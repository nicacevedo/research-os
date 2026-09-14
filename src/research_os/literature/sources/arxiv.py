"""arXiv: preprint metadata and, uniquely among these three, retrievable full text.

Verified against the live API and the published terms of use:

* ``https://export.arxiv.org/api/query`` answers Atom XML, with no credential.
  The plain-HTTP form redirects to HTTPS, so the adapter asks for HTTPS.
* The terms say plainly: **no more than one request every three seconds, and a
  single connection at a time**, counted across every machine under one party's
  control, and they forbid working around it with more machines. That interval
  is enforced by the HTTP client rather than documented and hoped for.
* ``max_results`` is capped at 2000 per request.

The Atom is untrusted XML off the network, and Python's default parser expands
internal entities -- verified here, not assumed -- so a document that declares a
document type at all is refused before it is parsed. arXiv's Atom declares none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

from research_os.errors import SourceUnavailableError
from research_os.literature.http import (
    HostPolicy,
    HttpClient,
    encode_query,
    retry_after_seconds,
)
from research_os.literature.identity import normalize_arxiv_id
from research_os.literature.models import SourceProbe, SourceStatus
from research_os.literature.sources.base import (
    ProviderRecord,
    SourceResult,
    author_records,
    clean_text,
)

NAME = "arxiv"
BASE_URL = "https://export.arxiv.org/api/query"
HOST = "export.arxiv.org"

#: The published terms: one request every three seconds, one connection.
MIN_INTERVAL_SECONDS = 3.0

MAX_RESULTS = 2000

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


@dataclass
class ArxivSource:
    """Adapter for the arXiv Atom query API."""

    name: str = NAME
    base_url: str = BASE_URL
    client: HttpClient = field(default_factory=HttpClient)

    def __post_init__(self) -> None:
        self.client.policies.setdefault(
            HOST,
            HostPolicy(
                host=HOST,
                min_interval_seconds=MIN_INTERVAL_SECONDS,
                note="arXiv terms of use: one request every three seconds",
            ),
        )

    def probe(self) -> SourceProbe:
        return SourceProbe(
            name=self.name,
            base_url=self.base_url,
            status=SourceStatus.UNAVAILABLE if self.client.offline else SourceStatus.OK,
            requires_credential=False,
            credential_env=None,
            credential_present=False,
            contact_configured=bool(self.client.contact_email),
            rate_limit_note=(
                "terms of use: one request every three seconds, one connection, "
                "counted across every machine you control"
            ),
            detail=(
                "offline mode is enabled"
                if self.client.offline
                else "usable without a credential; the only source here that "
                "offers retrievable full text"
            ),
        )

    def search(self, query: str, *, limit: int = 20) -> SourceResult:
        url = f"{self.base_url}?" + encode_query(
            {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": min(max(limit, 1), MAX_RESULTS),
            }
        )
        return self._collect(url)

    def fetch(self, identifier: str) -> SourceResult:
        arxiv_id = normalize_arxiv_id(identifier)
        if arxiv_id is None:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.OK,
                detail=(
                    f"{identifier!r} is not an arXiv id, and arXiv is addressed by one"
                ),
            )
        url = f"{self.base_url}?" + encode_query(
            {"id_list": arxiv_id, "max_results": 1}
        )
        return self._collect(url)

    def _collect(self, url: str) -> SourceResult:
        try:
            response = self.client.get(url, headers={"Accept": "application/atom+xml"})
        except SourceUnavailableError as exc:
            return SourceResult(
                provider=self.name, status=SourceStatus.UNAVAILABLE, detail=str(exc)
            )
        if response.status == 429:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.RATE_LIMITED,
                request_url=url,
                detail="arXiv returned 429; the three-second interval was not enough",
                retry_after_seconds=retry_after_seconds(response),
            )
        if response.status != 200:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"arXiv returned HTTP {response.status}",
            )
        try:
            entries = parse_atom(response.text())
        except ElementTree.ParseError as exc:
            return SourceResult(
                provider=self.name,
                status=SourceStatus.FAILED,
                request_url=url,
                detail=f"arXiv returned a body that is not Atom XML: {exc}",
            )
        records = tuple(
            record
            for entry in entries
            if (record := self.to_record(entry, request_url=url)) is not None
        )
        return SourceResult(
            provider=self.name,
            status=SourceStatus.OK,
            records=records,
            request_url=url,
        )

    def to_record(
        self, payload: dict[str, Any], *, request_url: str = ""
    ) -> ProviderRecord | None:
        arxiv_id = normalize_arxiv_id(payload.get("id"))
        title = clean_text(payload.get("title"))
        if arxiv_id is None or not title:
            return None
        published = str(payload.get("published") or "")
        year = int(published[:4]) if published[:4].isdigit() else None
        identifiers: dict[str, str] = {"arxiv": arxiv_id}
        doi = payload.get("doi")
        if isinstance(doi, str) and doi.strip():
            identifiers["doi"] = doi
        names = [str(item) for item in payload.get("authors") or []]
        return ProviderRecord(
            provider=self.name,
            payload=payload,
            fields={
                "title": title,
                "abstract": clean_text(payload.get("summary")),
                "publication_year": year,
                "venue": clean_text(payload.get("journal_ref")) or "arXiv",
                "work_type": "preprint",
            },
            identifiers=identifiers,
            authors=author_records(names),
            provider_work_id=arxiv_id,
            request_url=request_url,
            pdf_url=payload.get("pdf_url"),
        )


def assert_no_doctype(text: str) -> None:
    """Refuse an XML document that declares a document type.

    This is untrusted XML off the network, and Python's default parser really
    does expand internal general entities -- verified on this machine, where a
    small billion-laughs document expands rather than being refused. Every such
    entity has to be declared in a DTD, so refusing a document type declaration
    removes the whole class without needing to reason about expansion limits.

    Only the prolog is inspected -- everything before the first element start
    tag -- so a paper whose abstract happens to quote the characters
    ``<!DOCTYPE`` is not refused for saying so.

    arXiv's Atom carries no document type declaration, so this costs nothing on
    the real interface and fails loudly on anything that is not it.
    """

    prolog = text
    for index, character in enumerate(text):
        following = text[index + 1] if index + 1 < len(text) else ""
        if character == "<" and (following.isalpha() or following == "_"):
            prolog = text[:index]
            break
    if "<!doctype" in prolog.lower():
        raise ElementTree.ParseError(
            "refusing an XML document that declares a document type; entity "
            "declarations are how a small document becomes an unbounded one"
        )


def parse_atom(text: str) -> list[dict[str, Any]]:
    """Return one dictionary per Atom entry, with nothing interpreted.

    The dictionaries are the archived payload, so they hold what arXiv said
    rather than a tidied reading of it. Translation happens in ``to_record``.
    """

    assert_no_doctype(text)
    root = ElementTree.fromstring(text)
    entries: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM}entry"):
        payload: dict[str, Any] = {
            "id": _text(entry, f"{ATOM}id"),
            "title": _text(entry, f"{ATOM}title"),
            "summary": _text(entry, f"{ATOM}summary"),
            "published": _text(entry, f"{ATOM}published"),
            "updated": _text(entry, f"{ATOM}updated"),
            "doi": _text(entry, f"{ARXIV}doi"),
            "journal_ref": _text(entry, f"{ARXIV}journal_ref"),
            "primary_category": _attribute(entry, f"{ARXIV}primary_category", "term"),
            "authors": [
                name
                for element in entry.findall(f"{ATOM}author")
                if (name := _text(element, f"{ATOM}name"))
            ],
            "categories": [
                term
                for element in entry.findall(f"{ATOM}category")
                if (term := element.get("term"))
            ],
        }
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf" and link.get("href"):
                payload["pdf_url"] = _https(link.get("href") or "")
            elif link.get("rel") == "alternate" and link.get("href"):
                payload["abs_url"] = _https(link.get("href") or "")
        entries.append(payload)
    return entries


def _https(url: str) -> str:
    """Return ``url`` on HTTPS. arXiv still advertises some links as HTTP."""

    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _text(element: ElementTree.Element, path: str) -> str | None:
    found = element.find(path)
    if found is None or found.text is None:
        return None
    return " ".join(found.text.split()) or None


def _attribute(element: ElementTree.Element, path: str, name: str) -> str | None:
    found = element.find(path)
    return found.get(name) if found is not None else None
