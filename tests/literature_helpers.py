"""Deterministic doubles for the literature subsystem.

The literature tests must never require the network, so every one of them drives
the real adapters through a scripted transport. What is faked is exactly one
thing -- the bytes a server would have returned -- and everything above it is the
production code: the real URL construction, the real rate-limit policy, the real
JSON and Atom parsing, the real identity rules, the real SQLite schema, and the
real BM25 ranking.

The one deliberate exception is sleeping. :class:`ScriptedClient` records how long
politeness would have made it wait and returns immediately, so a test can assert
that arXiv's three-second interval was honoured without taking three seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_os.errors import SourceUnavailableError
from research_os.literature.config import LiteratureConfig
from research_os.literature.http import HttpClient, HttpResponse
from research_os.literature.models import AuthorRecord
from research_os.literature.store import LiteratureStore


@dataclass
class ScriptedResponse:
    """One canned HTTP response, matched by a substring of the request URL."""

    match: str
    status: int = 200
    body: bytes = b"{}"
    headers: dict[str, str] = field(default_factory=dict)
    raises: Exception | None = None
    transport_error: str = ""
    """A request that never completed: a timeout, a reset, a DNS failure.

    Distinct from ``raises`` because the client treats the two differently and
    has to: a transport failure is retried, a settled refusal is not. Scripting
    them through one field would make the retry tests unable to tell which
    behaviour they were actually observing.
    """

    transport_failures: int | None = None
    """How many leading attempts fail before this response is served.

    Counted down across calls, which is what makes "recovers after one transient
    error" expressible as a scripted fact rather than as two separate clients.
    ``None`` with a ``transport_error`` set means every attempt fails, which is
    the host that never recovers.
    """


@dataclass
class ScriptedClient(HttpClient):
    """An :class:`HttpClient` that never opens a socket.

    Subclasses the real client rather than reimplementing it, so URL validation,
    credential scoping, retry behaviour, and rate-limit accounting are the real
    ones under test.
    """

    responses: list[ScriptedResponse] = field(default_factory=list)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    waited: list[float] = field(default_factory=list)

    def _sleep(self, seconds: float) -> None:  # type: ignore[override]
        """Record politeness instead of performing it."""

        if seconds > 0:
            self.waited.append(seconds)

    def _perform(self, url: str, headers: dict[str, str]) -> HttpResponse:
        from research_os.literature.http import _TransportFailure

        self.requests.append((url, dict(headers)))
        for response in self.responses:
            if response.match in url:
                if response.raises is not None:
                    raise response.raises
                host = url.split("/")[2] if "//" in url else url
                if response.transport_failures is None:
                    if response.transport_error:
                        raise _TransportFailure(response.transport_error, host=host)
                elif response.transport_failures > 0:
                    response.transport_failures -= 1
                    raise _TransportFailure(
                        response.transport_error or "connection reset", host=host
                    )
                return HttpResponse(
                    url=url,
                    status=response.status,
                    headers={
                        key.lower(): value for key, value in response.headers.items()
                    },
                    body=response.body,
                )
        raise SourceUnavailableError(f"no scripted response matches {url}")

    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]

    def headers_for(self, fragment: str) -> dict[str, str]:
        for url, headers in self.requests:
            if fragment in url:
                return headers
        raise AssertionError(f"no request matched {fragment!r}")


def json_response(match: str, payload: Any, **headers: str) -> ScriptedResponse:
    return ScriptedResponse(
        match=match,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )


def openalex_work(
    *,
    openalex_id: str = "https://openalex.org/W2000000001",
    doi: str | None = "https://doi.org/10.1000/alpha",
    title: str = "A study of widget dynamics",
    year: int = 2021,
    authors: tuple[str, ...] = ("Jane Roe", "Kim Smith"),
    abstract_words: tuple[str, ...] = ("Widgets", "behave", "predictably"),
    retracted: bool = False,
    cited_by: int = 12,
    referenced: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a payload shaped exactly like a real OpenAlex work object."""

    return {
        "id": openalex_id,
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": "article",
        "cited_by_count": cited_by,
        "is_retracted": retracted,
        "abstract_inverted_index": {
            word: [index] for index, word in enumerate(abstract_words)
        },
        "primary_location": {"source": {"display_name": "Journal of Widgets"}},
        "open_access": {"oa_url": "https://example.invalid/alpha.pdf"},
        "authorships": [
            {"author": {"display_name": name, "orcid": None}} for name in authors
        ],
        "referenced_works": list(referenced),
        "ids": {"doi": doi} if doi else {},
    }


def crossref_work(
    *,
    doi: str = "10.1000/alpha",
    title: str = "A study of widget dynamics",
    year: int = 2021,
    authors: tuple[tuple[str, str], ...] = (("Jane", "Roe"), ("Kim", "Smith")),
    container: str = "Journal of Widgets",
    updated_by: tuple[dict[str, str], ...] = (),
    update_to: tuple[dict[str, str], ...] = (),
    abstract: str | None = None,
) -> dict[str, Any]:
    """Return a payload shaped exactly like a real Crossref work object."""

    payload: dict[str, Any] = {
        "DOI": doi,
        "title": [title],
        "container-title": [container],
        "type": "journal-article",
        "issued": {"date-parts": [[year, 3, 1]]},
        "author": [{"given": given, "family": family} for given, family in authors],
        "is-referenced-by-count": 7,
    }
    if abstract is not None:
        payload["abstract"] = abstract
    if updated_by:
        payload["updated-by"] = list(updated_by)
    if update_to:
        payload["update-to"] = list(update_to)
    return payload


def arxiv_feed(
    *,
    arxiv_id: str = "2101.00001v2",
    title: str = "A study of widget dynamics",
    summary: str = "Widgets behave predictably.",
    published: str = "2021-01-04T10:00:00Z",
    authors: tuple[str, ...] = ("Jane Roe", "Kim Smith"),
    doi: str | None = None,
    journal_ref: str | None = None,
) -> bytes:
    """Return an Atom feed shaped exactly like a real arXiv query response."""

    author_elements = "".join(
        f"<author><name>{name}</name></author>" for name in authors
    )
    doi_element = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    journal = (
        f"<arxiv:journal_ref>{journal_ref}</arxiv:journal_ref>" if journal_ref else ""
    )
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}</id>
    <title>{title}</title>
    <summary>{summary}</summary>
    <published>{published}</published>
    <updated>{published}</updated>
    {doi_element}{journal}
    {author_elements}
    <category term="cond-mat.mes-hall"/>
    <link href="http://arxiv.org/pdf/{arxiv_id}" title="pdf"/>
    <link href="http://arxiv.org/abs/{arxiv_id}" rel="alternate"/>
  </entry>
</feed>
""".encode()


def fake_config(
    *,
    contact_email: str | None = "tests@example.invalid",
    enabled: tuple[str, ...] = ("openalex", "crossref", "arxiv"),
    offline: bool = False,
    fetch_fulltext: bool = True,
) -> LiteratureConfig:
    return LiteratureConfig(
        contact_email=contact_email,
        enabled_sources=enabled,
        offline=offline,
        default_search_limit=5,
        fetch_fulltext=fetch_fulltext,
        source=None,
    )


def seeded_store(entries: list[dict[str, Any]] | None = None) -> LiteratureStore:
    """Return an in-memory store holding a small deterministic corpus."""

    store = LiteratureStore.open_memory()
    for entry in entries if entries is not None else default_corpus():
        store.ingest(
            provider=entry.get("provider", "test"),
            payload=entry.get("payload", {}),
            fields=entry["fields"],
            identifiers=entry["identifiers"],
            authors=[
                AuthorRecord(position=index, name=name)
                for index, name in enumerate(entry.get("authors", ()))
            ],
        )
    return store


def default_corpus() -> list[dict[str, Any]]:
    """A handful of works that differ enough to make ranking observable."""

    return [
        {
            "identifiers": {"doi": "10.1000/widget-dynamics"},
            "fields": {
                "title": "Widget dynamics under load",
                "abstract": "We measure how widgets deform when loaded.",
                "publication_year": 2021,
                "venue": "Journal of Widgets",
            },
            "authors": ("Jane Roe",),
        },
        {
            "identifiers": {"doi": "10.1000/gadget-thermal"},
            "fields": {
                "title": "Thermal response of gadgets",
                "abstract": "Gadget temperature rises with applied current.",
                "publication_year": 2019,
                "venue": "Gadget Review",
            },
            "authors": ("Kim Smith",),
        },
        {
            "identifiers": {"arxiv": "2201.00002"},
            "fields": {
                "title": "A survey of widget measurement",
                "abstract": "Widgets, gadgets, and how each is measured.",
                "publication_year": 2022,
                "venue": "arXiv",
            },
            "authors": ("Lee Park",),
        },
        {
            "identifiers": {"doi": "10.1000/retracted-widget"},
            "fields": {
                "title": "Widget dynamics revisited",
                "abstract": "A reanalysis of widget deformation.",
                "publication_year": 2020,
                "venue": "Journal of Widgets",
                "is_retracted": True,
                "retraction_note": "retraction by 10.1000/notice",
            },
            "authors": ("Sam Doe",),
        },
    ]


MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 62>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (widget deformation measured here) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)
"""A tiny but genuinely valid PDF with a real text layer.

Hand-built rather than generated so the tests do not depend on a PDF library,
and genuinely valid so ``pdftotext`` really parses it: a fixture that only looked
like a PDF would test the magic-byte check and nothing else.
"""


def write_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MINIMAL_PDF)
    return path
