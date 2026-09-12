"""The deterministic retrieval pipeline: ask providers, record everything, index.

This is the layer the CLI and the controller both call, and it is ordinary
Python from end to end. No model is involved in any of it, because none of it is
a judgement: which provider to ask, how to normalise an identifier, whether two
records are the same paper, which provider supplied which field, whether a PDF
has a text layer, and how BM25 ranks the result are all decided by code, once,
the same way every time.

The pipeline is also honest about partial failure. A retrieval across three
providers where one is rate limited is not a failed retrieval; it is a retrieval
that reached two providers, and the record says which and why. Every provider
call is archived as a :class:`SearchRecord` whether it returned anything or not,
so a literature review can distinguish "nothing matched" from "we never asked".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.models import utc_now
from research_os.errors import LiteratureError, SourceUnavailableError
from research_os.literature.config import LiteratureConfig, default_config
from research_os.literature.extract import build_document, extract
from research_os.literature.files import (
    normalize_media_type,
    object_path,
    read_file,
    store_file,
)
from research_os.literature.http import HttpClient
from research_os.literature.models import (
    CitationEdge,
    ExtractionStatus,
    FileRecord,
    SearchHit,
    SearchRecord,
    SourceProbe,
    SourceStatus,
)
from research_os.literature.sources import default_sources
from research_os.literature.sources.base import ProviderRecord, SourceAdapter
from research_os.literature.store import LiteratureStore, files_root


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """What one provider contributed to one retrieval."""

    provider: str
    status: SourceStatus
    records: int
    ingested: tuple[str, ...]
    request_url: str
    detail: str
    rate_limit_note: str
    search_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is SourceStatus.OK


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """The complete result of one retrieval across every enabled provider."""

    query: str
    outcomes: tuple[ProviderOutcome, ...]
    work_keys: tuple[str, ...]

    @property
    def reached(self) -> tuple[str, ...]:
        return tuple(item.provider for item in self.outcomes if item.ok)

    @property
    def skipped(self) -> tuple[ProviderOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.ok)

    @property
    def complete(self) -> bool:
        """Whether every enabled provider actually answered.

        Reported rather than asserted. An incomplete retrieval is a normal,
        usable outcome; what is not acceptable is a report that does not say it
        was incomplete.
        """

        return not self.skipped


@dataclass
class LiteratureService:
    """Retrieval, ingestion, and indexing over one store and one set of sources."""

    store: LiteratureStore
    config: LiteratureConfig = field(default_factory=default_config)
    sources: dict[str, SourceAdapter] = field(default_factory=dict)
    client: HttpClient | None = None
    files_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = HttpClient(
                contact_email=self.config.contact_email,
                offline=self.config.offline,
            )
        if not self.sources:
            self.sources = default_sources(self.client)
        if self.files_directory is None:
            self.files_directory = files_root()

    # -- probing ---------------------------------------------------------

    def probe(self) -> dict[str, SourceProbe]:
        """Report every known source, including the ones that are switched off."""

        found: dict[str, SourceProbe] = {}
        for name, adapter in sorted(self.sources.items()):
            probe = adapter.probe()
            if not self.config.enabled(name):
                probe = probe.model_copy(
                    update={
                        "status": SourceStatus.UNAVAILABLE,
                        "detail": "disabled in literature.yaml",
                    }
                )
            found[name] = probe
        return found

    def enabled_sources(self) -> list[tuple[str, SourceAdapter]]:
        return [
            (name, adapter)
            for name, adapter in sorted(self.sources.items())
            if self.config.enabled(name)
        ]

    # -- retrieval -------------------------------------------------------

    def retrieve(self, query: str, *, limit: int | None = None) -> RetrievalReport:
        """Search every enabled provider for ``query`` and ingest what comes back."""

        bounded = self.config.bounded_limit(limit)
        outcomes: list[ProviderOutcome] = []
        keys: list[str] = []
        for name, adapter in self.enabled_sources():
            outcome = self._call(
                name,
                query=query,
                parameters={"limit": bounded, "operation": "search"},
                run=lambda adapter=adapter: adapter.search(query, limit=bounded),
            )
            outcomes.append(outcome)
            keys.extend(outcome.ingested)
        return RetrievalReport(
            query=query,
            outcomes=tuple(outcomes),
            work_keys=tuple(dict.fromkeys(keys)),
        )

    def fetch(self, identifier: str) -> RetrievalReport:
        """Fetch one work by identifier from every provider that can address it.

        All of them are asked rather than only the one whose scheme matches,
        because the point of fetching a known work is to merge what each
        provider knows: Crossref has the retraction status, OpenAlex has the
        citation count, arXiv has the PDF.
        """

        outcomes: list[ProviderOutcome] = []
        keys: list[str] = []
        for name, adapter in self.enabled_sources():
            outcome = self._call(
                name,
                query=identifier,
                parameters={"operation": "fetch"},
                run=lambda adapter=adapter: adapter.fetch(identifier),
            )
            outcomes.append(outcome)
            keys.extend(outcome.ingested)
        return RetrievalReport(
            query=identifier,
            outcomes=tuple(outcomes),
            work_keys=tuple(dict.fromkeys(keys)),
        )

    def _call(
        self,
        name: str,
        *,
        query: str,
        parameters: dict[str, object],
        run,
    ) -> ProviderOutcome:
        """Run one provider call, archive it, and ingest whatever it returned.

        Every path through here records a :class:`SearchRecord`. A provider that
        raised, a provider that was unavailable, and a provider that returned
        nothing are three different rows, and a literature review that cannot
        tell them apart is not reproducible.
        """

        requested_at = utc_now()
        try:
            result = run()
        except SourceUnavailableError as exc:
            return self._archive(
                name,
                query=query,
                requested_at=requested_at,
                parameters=parameters,
                status=SourceStatus.UNAVAILABLE,
                detail=str(exc),
                request_url="",
                ingested=(),
                rate_limit_note="",
            )
        except LiteratureError as exc:
            return self._archive(
                name,
                query=query,
                requested_at=requested_at,
                parameters=parameters,
                status=SourceStatus.FAILED,
                detail=str(exc),
                request_url="",
                ingested=(),
                rate_limit_note="",
            )

        ingested: list[str] = []
        if result.ok:
            for record in result.records:
                try:
                    ingested.append(self.ingest_record(record))
                except LiteratureError as exc:
                    # One unusable record does not discard the rest of a page of
                    # results; it is reported in the detail and skipped.
                    result = result.__class__(
                        provider=result.provider,
                        status=result.status,
                        records=result.records,
                        request_url=result.request_url,
                        detail=(result.detail + "; " if result.detail else "")
                        + f"skipped an unusable record: {exc}",
                        rate_limit_note=result.rate_limit_note,
                    )
        return self._archive(
            name,
            query=query,
            requested_at=requested_at,
            parameters=parameters,
            status=result.status,
            detail=result.detail,
            request_url=result.request_url,
            ingested=tuple(ingested),
            rate_limit_note=result.rate_limit_note,
            record_count=len(result.records),
        )

    def _archive(
        self,
        name: str,
        *,
        query: str,
        requested_at: str,
        parameters: dict[str, object],
        status: SourceStatus,
        detail: str,
        request_url: str,
        ingested: tuple[str, ...],
        rate_limit_note: str,
        record_count: int | None = None,
    ) -> ProviderOutcome:
        record = SearchRecord(
            query=query,
            provider=name,
            requested_at=requested_at,
            parameters=json.dumps(
                {**parameters, "request_url": request_url}, sort_keys=True
            ),
            status=status,
            result_count=record_count if record_count is not None else len(ingested),
            detail=detail,
        )
        search_id = self.store.record_search(
            record,
            [],
        )
        hits = [
            SearchHit(search_id=search_id, rank=index, work_key=key)
            for index, key in enumerate(ingested, start=1)
        ]
        if hits:
            self.store.record_search_hits(search_id, hits)
        return ProviderOutcome(
            provider=name,
            status=status,
            records=record.result_count,
            ingested=ingested,
            request_url=request_url,
            detail=detail,
            rate_limit_note=rate_limit_note,
            search_id=search_id,
        )

    def ingest_record(self, record: ProviderRecord) -> str:
        """Ingest one provider record and its citation edges."""

        key = self.store.ingest(
            provider=record.provider,
            payload=record.payload,
            fields=record.fields,
            identifiers=record.identifiers,
            authors=record.authors,
            request_url=record.request_url,
            provider_work_id=record.provider_work_id,
        )
        if record.referenced_ids:
            self._record_known_references(key, record)
        return key

    def _record_known_references(self, key: str, record: ProviderRecord) -> None:
        """Record citation edges, but only to works the store already knows.

        A reference list is hundreds of identifiers for papers nobody asked
        about. Creating a row for each would turn one fetch into a crawl, so an
        edge is kept only when both ends are already present; the rest stay in
        the archived payload, where a later fetch can pick them up.
        """

        from research_os.literature.identity import normalize_openalex_id

        edges = []
        for reference in record.referenced_ids:
            openalex = normalize_openalex_id(reference)
            if openalex is None:
                continue
            cited = self.store.find_by_identifier("openalex", openalex)
            if cited:
                edges.append(
                    CitationEdge(
                        citing_key=key, cited_key=cited, provider=record.provider
                    )
                )
        if edges:
            self.store.record_citations(edges)

    # -- full text -------------------------------------------------------

    def download_fulltext(self, work_key: str) -> FileRecord | None:
        """Fetch, store, and index the full text of one work, when it is available.

        Only what a provider advertised as openly available is fetched. Research
        OS does not go looking for a copy of a paywalled paper, and a URL that
        returns something other than the declared type is refused rather than
        stored as if it were the paper.
        """

        work = self.store.work(work_key)
        if work is None:
            raise LiteratureError(f"no work {work_key} in the literature store")
        url = work.open_access_url or self._arxiv_pdf_url(work.arxiv_id)
        if not url:
            return None
        if self.client is None:  # pragma: no cover - set in __post_init__
            raise LiteratureError("no HTTP client is configured")
        response = self.client.get(url, headers={"Accept": "application/pdf"})
        if response.status != 200:
            raise LiteratureError(
                f"fetching full text for {work.key} returned HTTP {response.status}"
            )
        if response.truncated:
            raise LiteratureError(
                f"the full text for {work.key} exceeded the response size limit"
            )
        media_type = normalize_media_type(response.header("content-type")) or (
            "application/pdf"
        )
        assert self.files_directory is not None
        stored = store_file(
            response.body, root=self.files_directory, media_type=media_type
        )
        record = FileRecord(
            file_sha256=stored.sha256,
            work_key=work.key,
            provider="arxiv" if work.arxiv_id else "open-access",
            source_url=url,
            media_type=stored.media_type,
            byte_size=stored.byte_size,
            retrieved_at=utc_now(),
            stored_path=str(stored.path),
        )
        self.store.record_file(record)
        return self.index_file(record)

    def index_file(self, record: FileRecord) -> FileRecord:
        """Extract and index the text of one stored file, recording the outcome."""

        assert self.files_directory is not None
        payload = read_file(self.files_directory, record.file_sha256)
        path = object_path(self.files_directory, record.file_sha256)
        _ = payload  # read_file verifies the digest; extraction reads the path
        result = extract(path, media_type=record.media_type)
        self.store.set_extraction(record.file_sha256, result.status, result.detail)
        if not result.ok:
            return record.model_copy(
                update={
                    "extraction_status": result.status,
                    "extraction_detail": result.detail,
                }
            )
        document, chunks = build_document(
            result, work_key=record.work_key, file_sha256=record.file_sha256
        )
        self.store.record_document(document, chunks)
        return record.model_copy(
            update={
                "extraction_status": ExtractionStatus.EXTRACTED,
                "extraction_detail": (
                    f"{len(chunks)} chunks from {result.extractor}"
                    + (" (truncated)" if result.truncated else "")
                ),
            }
        )

    @staticmethod
    def _arxiv_pdf_url(arxiv_id: str | None) -> str | None:
        """Return the canonical arXiv PDF URL for an id, if there is one.

        Constructed rather than taken from the payload so it cannot be redirected
        by a provider field: arXiv's PDF location is a documented, stable pattern.
        """

        return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None
