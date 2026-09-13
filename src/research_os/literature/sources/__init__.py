"""Source adapters for the literature providers Research OS can actually use.

One adapter per provider, each verified against that provider's live interface
and published terms rather than against a recollection of them. A provider with
no adapter is absent from this registry; a provider with an adapter that cannot
run on this machine reports ``UNAVAILABLE`` rather than raising, because "we
could not ask" is a fact about a literature review that must reach the record.
"""

from research_os.literature.sources.arxiv import ArxivSource
from research_os.literature.sources.base import (
    ProviderRecord,
    SourceAdapter,
    SourceResult,
)
from research_os.literature.sources.crossref import CrossrefSource
from research_os.literature.sources.openalex import OpenAlexSource

__all__ = [
    "ArxivSource",
    "CrossrefSource",
    "OpenAlexSource",
    "ProviderRecord",
    "SourceAdapter",
    "SourceResult",
    "default_sources",
]


def default_sources(client: object | None = None) -> dict[str, SourceAdapter]:
    """Return every adapter, sharing one HTTP client so pacing is global.

    Sharing matters: each provider's minimum interval is per host, and two
    adapters with two clients would each honour arXiv's three seconds
    separately, which between them is one request every 1.5 seconds and a
    breach of the terms.
    """

    from research_os.literature.http import HttpClient

    shared = client if isinstance(client, HttpClient) else HttpClient()
    return {
        OpenAlexSource.name: OpenAlexSource(client=shared),
        CrossrefSource.name: CrossrefSource(client=shared),
        ArxivSource.name: ArxivSource(client=shared),
    }
