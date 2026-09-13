"""The shared scholarly store: migrations, deduplication, merging, provenance.

Three properties are load-bearing and are tested here in that order.

**The schema is versioned.** A store written by an older build upgrades; one
written by a newer build is refused rather than silently misread.

**Two providers describing one paper produce one row.** Including the hard case:
two rows that already exist separately and are only later proved to be the same
work, whose merge must move every identifier, payload, file, and chunk rather
than discarding whichever side loses.

**Nothing a provider said is ever destroyed.** The merged row holds one value per
field, and the provenance table holds every value that was ever offered, marked
with whether it is the one in force.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research_os.errors import LiteratureStoreError
from research_os.literature.models import (
    AuthorRecord,
    CitationEdge,
    ExtractionStatus,
    FileRecord,
    KnownItem,
    SearchHit,
    SearchRecord,
    SourceStatus,
    TextChunk,
    TextDocument,
)
from research_os.literature.store import SCHEMA_VERSION, LiteratureStore


def author(name: str, position: int = 0) -> AuthorRecord:
    return AuthorRecord(position=position, name=name)


# -- migrations ---------------------------------------------------------------


def test_a_fresh_store_is_at_the_current_schema_version(tmp_path: Path) -> None:
    store = LiteratureStore.open(tmp_path / "lit.sqlite3")

    assert store.schema_version() == SCHEMA_VERSION
    assert (tmp_path / "lit.sqlite3").is_file()


def test_opening_an_existing_store_applies_nothing_twice(tmp_path: Path) -> None:
    target = tmp_path / "lit.sqlite3"
    first = LiteratureStore.open(target)
    first.ingest(
        provider="test",
        payload={},
        fields={"title": "A paper"},
        identifiers={"doi": "10.1/a"},
        authors=[author("Roe")],
    )
    first.close()

    second = LiteratureStore.open(target)

    assert second.schema_version() == SCHEMA_VERSION
    assert second.count_works() == 1


def test_a_store_from_a_newer_build_is_refused_rather_than_downgraded(
    tmp_path: Path,
) -> None:
    """Proceeding would misread rows a newer Research OS wrote."""

    target = tmp_path / "lit.sqlite3"
    store = LiteratureStore.open(target)
    store.connection.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION + 5),),
    )
    store.connection.commit()
    store.close()

    with pytest.raises(LiteratureStoreError, match="Upgrade Research OS"):
        LiteratureStore.open(target)


def test_every_declared_table_exists_after_migration() -> None:
    store = LiteratureStore.open_memory()

    names = {
        row["name"]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }

    assert {
        "works",
        "work_aliases",
        "work_authors",
        "work_identifiers",
        "field_provenance",
        "source_records",
        "searches",
        "search_results",
        "files",
        "documents",
        "chunks",
        "citations",
        "known_items",
        "work_fts",
        "chunk_fts",
    } <= names


def test_foreign_keys_are_enforced() -> None:
    """Without this, a merge could leave rows pointing at a deleted work."""

    store = LiteratureStore.open_memory()

    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO work_authors(work_key, position, name) VALUES ('nope', 0, 'x')"
        )


# -- identity and deduplication ----------------------------------------------


def test_one_provider_twice_is_one_work_and_one_payload() -> None:
    """Re-ingesting an identical record is a cache hit, not a second row."""

    store = LiteratureStore.open_memory()
    for _ in range(3):
        key = store.ingest(
            provider="openalex",
            payload={"id": "W1"},
            fields={"title": "A paper", "publication_year": 2020},
            identifiers={"openalex": "W1"},
            authors=[author("Roe")],
        )

    assert store.count_works() == 1
    assert len(store.source_records(key)) == 1


def test_three_providers_describing_one_paper_produce_one_work() -> None:
    store = LiteratureStore.open_memory()
    common = {"title": "Widget dynamics", "publication_year": 2021}

    store.ingest(
        provider="arxiv",
        payload={"a": 1},
        fields={**common, "abstract": "from arxiv"},
        identifiers={"arxiv": "2101.00001v2"},
        authors=[author("Jane Roe")],
    )
    store.ingest(
        provider="crossref",
        payload={"c": 1},
        fields={**common, "venue": "Journal of Widgets"},
        identifiers={"doi": "https://doi.org/10.1000/ALPHA"},
        authors=[author("Roe, Jane")],
    )
    key = store.ingest(
        provider="openalex",
        payload={"o": 1},
        fields={**common, "cited_by_count": 42},
        identifiers={"openalex": "W9", "doi": "10.1000/alpha"},
        authors=[author("Jane Roe")],
    )

    work = store.work(key)
    assert store.count_works() == 1
    assert work is not None
    assert work.doi == "10.1000/alpha"
    assert work.arxiv_id == "2101.00001"
    assert work.openalex_id == "W9"
    assert work.abstract == "from arxiv"
    assert work.venue == "Journal of Widgets"
    assert work.cited_by_count == 42
    assert {item.provider for item in store.source_records(key)} == {
        "arxiv",
        "crossref",
        "openalex",
    }


def test_two_papers_with_the_same_title_in_different_years_stay_apart() -> None:
    """The fallback is strict, and this is the case that proves it."""

    store = LiteratureStore.open_memory()
    store.ingest(
        provider="test",
        payload={},
        fields={"title": "Scaling laws", "publication_year": 2019},
        identifiers={"doi": "10.1/a"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="test",
        payload={},
        fields={"title": "Scaling laws", "publication_year": 2021},
        identifiers={"doi": "10.1/b"},
        authors=[author("Roe")],
    )

    assert store.count_works() == 2


def test_a_record_with_no_usable_identity_is_refused() -> None:
    store = LiteratureStore.open_memory()

    with pytest.raises(LiteratureStoreError, match="no usable identity"):
        store.ingest(
            provider="test",
            payload={},
            fields={"title": "Untitled fragment"},
            identifiers={},
            authors=[],
        )


# -- the late merge -----------------------------------------------------------


def test_a_later_record_merges_two_rows_that_were_always_one_work() -> None:
    """The preprint and the published paper, joined by a record carrying both ids."""

    store = LiteratureStore.open_memory()
    preprint = store.ingest(
        provider="arxiv",
        payload={"a": 1},
        fields={"title": "Preprint title", "publication_year": 2020},
        identifiers={"arxiv": "2001.01234"},
        authors=[author("Jane Roe")],
    )
    published = store.ingest(
        provider="crossref",
        payload={"c": 1},
        fields={
            "title": "Published title",
            "publication_year": 2021,
            "venue": "Nature",
        },
        identifiers={"doi": "10.1000/xyz"},
        authors=[author("Jane Roe")],
    )
    assert store.count_works() == 2

    store.record_file(
        FileRecord(
            file_sha256="a" * 64,
            work_key=preprint,
            provider="arxiv",
            source_url="https://example.invalid/p.pdf",
            media_type="application/pdf",
            byte_size=10,
            retrieved_at="2026-01-01T00:00:00Z",
            stored_path="/tmp/p.pdf",
        )
    )
    store.record_document(
        TextDocument(
            work_key=preprint,
            file_sha256="a" * 64,
            extracted_at="2026-01-01T00:00:00Z",
            char_count=11,
            extractor="pdftotext",
        ),
        [
            TextChunk(
                document_id=0,
                work_key=preprint,
                ordinal=0,
                char_start=0,
                char_end=11,
                text="hello world",
            )
        ],
    )

    merged = store.ingest(
        provider="openalex",
        payload={"o": 1},
        fields={"title": "Published title", "publication_year": 2021},
        identifiers={"openalex": "W9", "doi": "10.1000/xyz", "arxiv": "2001.01234"},
        authors=[author("Jane Roe")],
    )

    assert store.count_works() == 1
    assert merged == published, "the DOI-keyed row should win the merge"
    work = store.work(merged)
    assert work is not None
    assert work.doi == "10.1000/xyz"
    assert work.arxiv_id == "2001.01234"
    assert work.venue == "Nature"
    assert [item.work_key for item in store.files(merged)] == [merged]
    assert [item.work_key for item in store.documents(merged)] == [merged]
    assert {item.provider for item in store.source_records(merged)} == {
        "arxiv",
        "crossref",
        "openalex",
    }


def test_a_key_someone_wrote_down_before_a_merge_still_resolves() -> None:
    store = LiteratureStore.open_memory()
    preprint = store.ingest(
        provider="arxiv",
        payload={},
        fields={"title": "Preprint", "publication_year": 2020},
        identifiers={"arxiv": "2001.01234"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={"title": "Published", "publication_year": 2021},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    winner = store.ingest(
        provider="openalex",
        payload={},
        fields={"title": "Published", "publication_year": 2021},
        identifiers={"doi": "10.1/x", "arxiv": "2001.01234"},
        authors=[author("Roe")],
    )

    assert store.resolve_key(preprint) == winner
    resolved = store.work(preprint)
    assert resolved is not None and resolved.key == winner


def test_the_full_text_index_follows_a_merged_work() -> None:
    """Otherwise a merge would silently make a paper's full text unfindable."""

    store = LiteratureStore.open_memory()
    preprint = store.ingest(
        provider="arxiv",
        payload={},
        fields={"title": "Preprint", "publication_year": 2020},
        identifiers={"arxiv": "2001.01234"},
        authors=[author("Roe")],
    )
    store.record_file(
        FileRecord(
            file_sha256="b" * 64,
            work_key=preprint,
            provider="arxiv",
            source_url="u",
            media_type="application/pdf",
            byte_size=1,
            retrieved_at="2026-01-01T00:00:00Z",
            stored_path="p",
        )
    )
    store.record_document(
        TextDocument(
            work_key=preprint,
            file_sha256="b" * 64,
            extracted_at="2026-01-01T00:00:00Z",
            char_count=20,
            extractor="pdftotext",
        ),
        [
            TextChunk(
                document_id=0,
                work_key=preprint,
                ordinal=0,
                char_start=0,
                char_end=20,
                text="distinctive phrase here",
            )
        ],
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={"title": "Published", "publication_year": 2021},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    winner = store.ingest(
        provider="openalex",
        payload={},
        fields={"title": "Published", "publication_year": 2021},
        identifiers={"doi": "10.1/x", "arxiv": "2001.01234"},
        authors=[author("Roe")],
    )

    rows = store.connection.execute(
        "SELECT work_key FROM chunk_fts WHERE chunk_fts MATCH 'distinctive'"
    ).fetchall()
    assert [row["work_key"] for row in rows] == [winner]


# -- provenance ---------------------------------------------------------------


def test_every_provider_that_offered_a_field_is_recorded() -> None:
    store = LiteratureStore.open_memory()
    key = store.ingest(
        provider="arxiv",
        payload={},
        fields={"title": "The arXiv title", "publication_year": 2021},
        identifiers={"arxiv": "2101.00001"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={"title": "The published title", "publication_year": 2021},
        identifiers={"arxiv": "2101.00001", "doi": "10.1/x"},
        authors=[author("Roe")],
    )

    work = store.work(key)
    assert work is not None
    titles = {
        item.provider: item.superseded
        for item in work.provenance
        if item.field == "title"
    }
    assert titles == {"arxiv": False, "crossref": True}, (
        "the value in force and the value that lost must both be visible"
    )
    assert work.title == "The arXiv title", "a later provider does not overwrite"


def test_filling_an_empty_field_is_not_a_disagreement() -> None:
    store = LiteratureStore.open_memory()
    key = store.ingest(
        provider="arxiv",
        payload={},
        fields={"title": "A paper", "publication_year": 2021},
        identifiers={"arxiv": "2101.00001"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={"title": "A paper", "publication_year": 2021, "venue": "Nature"},
        identifiers={"arxiv": "2101.00001"},
        authors=[author("Roe")],
    )

    work = store.work(key)
    assert work is not None
    venue = next(item for item in work.provenance if item.field == "venue")
    assert venue.superseded is False
    assert work.venue == "Nature"


def test_a_retraction_from_any_provider_sticks() -> None:
    """An older record that did not know must not un-retract a paper."""

    store = LiteratureStore.open_memory()
    key = store.ingest(
        provider="openalex",
        payload={},
        fields={"title": "A paper", "publication_year": 2020},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={
            "title": "A paper",
            "is_retracted": True,
            "retraction_note": "retraction by 10.1/notice",
        },
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    store.ingest(
        provider="openalex",
        payload={"stale": True},
        fields={"title": "A paper", "publication_year": 2020},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )

    work = store.work(key)
    assert work is not None
    assert work.is_retracted is True
    assert work.retraction_note == "retraction by 10.1/notice"


# -- searches, files, citations, fixtures ------------------------------------


def test_a_provider_search_is_archived_even_when_it_returned_nothing() -> None:
    """ "We looked and found nothing" and "we never looked" must stay distinct."""

    store = LiteratureStore.open_memory()
    search_id = store.record_search(
        SearchRecord(
            query="widgets",
            provider="openalex",
            requested_at="2026-01-01T00:00:00Z",
            status=SourceStatus.RATE_LIMITED,
            detail="daily budget exhausted",
        ),
        [],
    )

    archived = store.searches()
    assert len(archived) == 1
    assert archived[0].status is SourceStatus.RATE_LIMITED
    assert archived[0].detail == "daily budget exhausted"
    assert store.search_hits(search_id) == []


def test_provider_ranking_is_kept_alongside_the_search() -> None:
    store = LiteratureStore.open_memory()
    key = store.ingest(
        provider="test",
        payload={},
        fields={"title": "A paper"},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    search_id = store.record_search(
        SearchRecord(
            query="widgets", provider="openalex", requested_at="2026-01-01T00:00:00Z"
        ),
        [],
    )
    store.record_search_hits(
        search_id, [SearchHit(search_id=search_id, rank=1, work_key=key)]
    )

    hits = store.search_hits(search_id)
    assert [item.work_key for item in hits] == [key]


def test_extraction_status_round_trips() -> None:
    store = LiteratureStore.open_memory()
    key = store.ingest(
        provider="test",
        payload={},
        fields={"title": "A paper"},
        identifiers={"doi": "10.1/x"},
        authors=[author("Roe")],
    )
    store.record_file(
        FileRecord(
            file_sha256="c" * 64,
            work_key=key,
            provider="arxiv",
            source_url="u",
            media_type="application/pdf",
            byte_size=5,
            retrieved_at="2026-01-01T00:00:00Z",
            stored_path="p",
        )
    )
    store.set_extraction("c" * 64, ExtractionStatus.UNSUPPORTED, "no text layer")

    stored = store.file("c" * 64)
    assert stored is not None
    assert stored.extraction_status is ExtractionStatus.UNSUPPORTED
    assert stored.extraction_detail == "no text layer"


def test_citations_are_directional_and_deduplicated() -> None:
    store = LiteratureStore.open_memory()
    for doi in ("10.1/a", "10.1/b"):
        store.ingest(
            provider="test",
            payload={},
            fields={"title": doi},
            identifiers={"doi": doi},
            authors=[author("Roe")],
        )
    edge = CitationEdge(
        citing_key="doi:10.1/a", cited_key="doi:10.1/b", provider="openalex"
    )
    store.record_citations([edge, edge])

    assert store.references("doi:10.1/a") == ["doi:10.1/b"]
    assert store.cited_by("doi:10.1/b") == ["doi:10.1/a"]
    assert store.cited_by("doi:10.1/a") == []


def test_known_item_fixtures_are_recorded_and_listed() -> None:
    store = LiteratureStore.open_memory()
    store.record_known_items(
        [
            KnownItem(fixture="widgets", query="widget load", work_key="doi:10.1/a"),
            KnownItem(fixture="widgets", query="widget load", work_key="doi:10.1/b"),
        ]
    )

    assert store.known_fixtures() == ["widgets"]
    assert [item.work_key for item in store.known_items("widgets")] == [
        "doi:10.1/a",
        "doi:10.1/b",
    ]
