"""The shared scholarly store: SQLite, versioned, and explicitly not science.

Where this lives matters more than what is in it. Capsule objects are
Git-tracked files inside one project, because they are that project's scientific
truth and a human must be able to read and review them. A literature index is
the opposite kind of thing: large, shared between projects, rebuildable from the
providers, and containing nobody's conclusions. So it lives under the Research
OS data home in one database, and deleting that database loses a cache and costs
no science.

SQLite rather than JSON because the access pattern is genuinely relational and
genuinely large -- tens of thousands of works, their identifiers, their source
records, their extracted text -- and because FTS5 gives deterministic BM25
ranking in the standard library, with no service to run and no vector index to
keep in sync.

Both full-text tables stem with Porter. Without it, a researcher searching for
"widget deformation under load" would not match a paper whose abstract says
"we measure how widgets deform when loaded" -- which is the same paper, said the
way a person writes an abstract. Stemming is the cheapest recall this subsystem
can buy: deterministic, built into SQLite, and no second copy of the corpus.

Two rules shape the schema.

**Provenance is never destroyed.** ``source_records`` keeps the payload each
provider actually returned, and ``field_provenance`` records which provider
supplied which field and when. A merge supersedes a provenance row; it never
deletes one. A reader who doubts a merged title can see every title that was
ever offered and who offered it.

**Identity is decided in one place.** :mod:`.identity` says when two records are
the same work; this module only applies that decision. When a later record
proves two rows were always one work, they are merged rather than left as
duplicates, and the losing key becomes an alias so an earlier reference still
resolves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Self

from research_os.automation.models import utc_now
from research_os.errors import LiteratureStoreError
from research_os.literature.identity import (
    IDENTIFIER_PRECEDENCE,
    TITLE_SCHEME,
    normalize_arxiv_id,
    normalize_author,
    normalize_doi,
    normalize_openalex_id,
    normalize_pmid,
    normalize_title,
    title_fallback_key,
    work_key,
)
from research_os.literature.models import (
    AuthorRecord,
    CitationEdge,
    ExtractionStatus,
    FieldProvenance,
    FileRecord,
    KnownItem,
    SearchHit,
    SearchRecord,
    SourceRecord,
    TextChunk,
    TextDocument,
    WorkIdentifier,
    WorkRecord,
)
from research_os.paths import data_home

DATABASE_DIRNAME = "literature"
DATABASE_FILENAME = "literature.sqlite3"
FILES_DIRNAME = "files"

#: The schema version this build writes and expects.
SCHEMA_VERSION = 1

#: The fields a merge tracks provenance for.
#:
#: Content fields only. The key, the timestamps, and the counts are things the
#: store decided; these are things a provider asserted.
PROVENANCED_FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "publication_year",
    "venue",
    "work_type",
    "doi",
    "arxiv_id",
    "openalex_id",
    "pmid",
    "open_access_url",
    "cited_by_count",
    "is_retracted",
    "authors",
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE works (
            work_key         TEXT PRIMARY KEY,
            title            TEXT NOT NULL DEFAULT '',
            normalized_title TEXT NOT NULL DEFAULT '',
            publication_year INTEGER,
            venue            TEXT,
            work_type        TEXT,
            abstract         TEXT,
            doi              TEXT,
            arxiv_id         TEXT,
            openalex_id      TEXT,
            pmid             TEXT,
            open_access_url  TEXT,
            cited_by_count   INTEGER,
            is_retracted     INTEGER NOT NULL DEFAULT 0,
            retraction_note  TEXT,
            first_seen       TEXT NOT NULL,
            last_updated     TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX works_doi ON works(doi) WHERE doi IS NOT NULL",
        "CREATE UNIQUE INDEX works_arxiv ON works(arxiv_id) WHERE arxiv_id IS NOT NULL",
        """
        CREATE UNIQUE INDEX works_openalex ON works(openalex_id)
        WHERE openalex_id IS NOT NULL
        """,
        "CREATE INDEX works_normalized_title ON works(normalized_title)",
        """
        CREATE TABLE work_aliases (
            alias_key TEXT PRIMARY KEY,
            work_key  TEXT NOT NULL REFERENCES works(work_key) ON DELETE CASCADE,
            merged_at TEXT NOT NULL,
            reason    TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE work_authors (
            work_key        TEXT NOT NULL REFERENCES works(work_key) ON DELETE CASCADE,
            position        INTEGER NOT NULL,
            name            TEXT NOT NULL,
            normalized_name TEXT NOT NULL DEFAULT '',
            orcid           TEXT,
            PRIMARY KEY (work_key, position)
        )
        """,
        """
        CREATE TABLE work_identifiers (
            work_key TEXT NOT NULL REFERENCES works(work_key) ON DELETE CASCADE,
            scheme   TEXT NOT NULL,
            value    TEXT NOT NULL,
            provider TEXT NOT NULL,
            PRIMARY KEY (work_key, scheme, value, provider)
        )
        """,
        """
        CREATE INDEX work_identifiers_lookup
            ON work_identifiers(scheme, value)
        """,
        """
        CREATE TABLE field_provenance (
            work_key     TEXT NOT NULL REFERENCES works(work_key) ON DELETE CASCADE,
            field        TEXT NOT NULL,
            provider     TEXT NOT NULL,
            value_digest TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            superseded   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (work_key, field, provider, value_digest)
        )
        """,
        """
        CREATE TABLE source_records (
            record_id       INTEGER PRIMARY KEY,
            work_key        TEXT NOT NULL
                            REFERENCES works(work_key) ON DELETE CASCADE,
            provider        TEXT NOT NULL,
            provider_work_id TEXT,
            request_url     TEXT NOT NULL DEFAULT '',
            retrieved_at    TEXT NOT NULL,
            payload_sha256  TEXT NOT NULL,
            payload         TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX source_records_unique
            ON source_records(work_key, provider, payload_sha256)
        """,
        """
        CREATE TABLE searches (
            search_id    INTEGER PRIMARY KEY,
            query        TEXT NOT NULL,
            provider     TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            parameters   TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            detail       TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE search_results (
            search_id      INTEGER NOT NULL
                           REFERENCES searches(search_id) ON DELETE CASCADE,
            rank           INTEGER NOT NULL,
            work_key       TEXT NOT NULL,
            provider_score REAL,
            PRIMARY KEY (search_id, rank)
        )
        """,
        """
        CREATE TABLE files (
            file_sha256       TEXT PRIMARY KEY,
            work_key          TEXT NOT NULL
                              REFERENCES works(work_key) ON DELETE CASCADE,
            provider          TEXT NOT NULL,
            source_url        TEXT NOT NULL DEFAULT '',
            media_type        TEXT NOT NULL DEFAULT '',
            byte_size         INTEGER NOT NULL DEFAULT 0,
            retrieved_at      TEXT NOT NULL,
            stored_path       TEXT NOT NULL,
            extraction_status TEXT NOT NULL DEFAULT 'pending',
            extraction_detail TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX files_work ON files(work_key)",
        """
        CREATE TABLE documents (
            document_id       INTEGER PRIMARY KEY,
            work_key          TEXT NOT NULL
                              REFERENCES works(work_key) ON DELETE CASCADE,
            file_sha256       TEXT NOT NULL
                              REFERENCES files(file_sha256) ON DELETE CASCADE,
            kind              TEXT NOT NULL DEFAULT 'fulltext',
            extracted_at      TEXT NOT NULL,
            char_count        INTEGER NOT NULL DEFAULT 0,
            extractor         TEXT NOT NULL DEFAULT '',
            extractor_version TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE UNIQUE INDEX documents_file ON documents(file_sha256, kind)",
        """
        CREATE TABLE chunks (
            chunk_id    INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL
                        REFERENCES documents(document_id) ON DELETE CASCADE,
            work_key    TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            char_start  INTEGER NOT NULL,
            char_end    INTEGER NOT NULL,
            text        TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX chunks_ordinal ON chunks(document_id, ordinal)",
        """
        CREATE TABLE citations (
            citing_key TEXT NOT NULL,
            cited_key  TEXT NOT NULL,
            provider   TEXT NOT NULL,
            PRIMARY KEY (citing_key, cited_key, provider)
        )
        """,
        """
        CREATE TABLE known_items (
            fixture  TEXT NOT NULL,
            query    TEXT NOT NULL,
            work_key TEXT NOT NULL,
            note     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (fixture, query, work_key)
        )
        """,
        """
        CREATE VIRTUAL TABLE work_fts USING fts5(
            work_key UNINDEXED,
            title,
            abstract,
            authors,
            venue,
            tokenize = 'porter unicode61 remove_diacritics 2'
        )
        """,
        """
        CREATE VIRTUAL TABLE chunk_fts USING fts5(
            chunk_id UNINDEXED,
            work_key UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        )
        """,
    )
}


def database_root() -> Path:
    """Return the directory holding the shared scholarly store."""

    return data_home() / DATABASE_DIRNAME


def database_path() -> Path:
    return database_root() / DATABASE_FILENAME


def files_root() -> Path:
    """Return the content-addressed file store beside the database."""

    return database_root() / FILES_DIRNAME


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LiteratureStore:
    """One connection to the shared scholarly store.

    Opened with foreign keys on and WAL journaling, because several processes --
    a CLI search, a background fetch, an automation run -- may reasonably read
    it at once. Every write goes through a transaction on this connection; there
    is no ORM and no lazy loading.
    """

    def __init__(self, connection: sqlite3.Connection, *, path: Path | None) -> None:
        self.connection = connection
        self.path = path
        self.connection.row_factory = sqlite3.Row

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def open(cls, path: Path | None = None) -> LiteratureStore:
        """Open (creating if needed) the shared store and apply migrations."""

        target = path if path is not None else database_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(target))
        except (OSError, sqlite3.Error) as exc:
            raise LiteratureStoreError(
                f"cannot open the literature store at {target}: {exc}"
            ) from exc
        store = cls(connection, path=target)
        store._configure()
        store.migrate()
        return store

    @classmethod
    def open_memory(cls) -> LiteratureStore:
        """Open an in-memory store. Used by tests and by dry inspection."""

        store = cls(sqlite3.connect(":memory:"), path=None)
        store._configure()
        store.migrate()
        return store

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.path is not None:
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work, committing it or leaving the store untouched."""

        try:
            with self.connection:
                yield self.connection
        except sqlite3.Error as exc:
            raise LiteratureStoreError(f"literature store write failed: {exc}") from exc

    # -- migrations ------------------------------------------------------

    def schema_version(self) -> int:
        """Return the applied schema version, or 0 for an empty database."""

        row = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if row is None:
            return 0
        found = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(found["value"]) if found else 0

    def migrate(self) -> int:
        """Bring the database up to :data:`SCHEMA_VERSION` and return it.

        Migrations are numbered and applied in order inside one transaction
        each, so an interrupted upgrade leaves the previous version intact. A
        database newer than this build is refused rather than downgraded: a
        newer Research OS may have written rows this one cannot read correctly,
        and silently proceeding would corrupt them.
        """

        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise LiteratureStoreError(
                f"the literature store at {self.path or ':memory:'} is at schema "
                f"version {current}, but this build of Research OS understands "
                f"{SCHEMA_VERSION}. Upgrade Research OS rather than downgrading "
                "the store."
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            statements = _MIGRATIONS.get(version)
            if statements is None:
                raise LiteratureStoreError(
                    f"no migration defined for version {version}"
                )
            with self.transaction() as connection:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
        return SCHEMA_VERSION

    # -- identity --------------------------------------------------------

    def resolve_key(self, key: str) -> str | None:
        """Return the live key for ``key``, following one merge alias.

        A merge rewrites rows onto the winner and records the loser as an alias,
        so a work id a researcher wrote down last week still addresses the same
        paper today.
        """

        row = self.connection.execute(
            "SELECT work_key FROM works WHERE work_key = ?", (key,)
        ).fetchone()
        if row is not None:
            return key
        alias = self.connection.execute(
            "SELECT work_key FROM work_aliases WHERE alias_key = ?", (key,)
        ).fetchone()
        return alias["work_key"] if alias else None

    def find_by_identifier(self, scheme: str, value: str) -> str | None:
        """Return the work carrying one registered identifier, if any."""

        row = self.connection.execute(
            "SELECT work_key FROM work_identifiers WHERE scheme = ? AND value = ? "
            "ORDER BY work_key LIMIT 1",
            (scheme, value),
        ).fetchone()
        if row is not None:
            return self.resolve_key(row["work_key"])
        column = {
            "doi": "doi",
            "arxiv": "arxiv_id",
            "openalex": "openalex_id",
            "pmid": "pmid",
        }.get(scheme)
        if column is None:
            return None
        direct = self.connection.execute(
            f"SELECT work_key FROM works WHERE {column} = ?", (value,)
        ).fetchone()
        return direct["work_key"] if direct else None

    # -- ingestion -------------------------------------------------------

    def ingest(
        self,
        *,
        provider: str,
        payload: dict[str, object],
        fields: dict[str, object],
        identifiers: dict[str, str],
        authors: list[AuthorRecord],
        request_url: str = "",
        provider_work_id: str | None = None,
        retrieved_at: str | None = None,
    ) -> str:
        """Record one provider's view of one work and return its live key.

        This is the only way content enters the store. It resolves identity,
        merges any rows the new identifiers prove were always one work, writes
        the fields that were previously unknown, records provenance for every
        field the provider supplied, and archives the raw payload.

        Existing content is not overwritten by a later provider. A field that is
        already set stays set and the new value is recorded as a competing
        provenance row, so a merge never silently replaces a title a human may
        already have read. The one exception is a field that was empty, which is
        not a disagreement.
        """

        moment = retrieved_at or utc_now()
        normalized = _normalized_identifiers(identifiers)
        # An identifier is both an identity and a field a reader asks for, so
        # the normalised form is the one that gets stored. An adapter therefore
        # supplies each identifier once, in whatever shape the provider used,
        # rather than remembering to normalise it into two places.
        fields = {**_identifier_fields(normalized), **fields}
        candidates = self._matching_keys(normalized, fields, authors)
        key = self._elect_key(normalized, fields, authors, candidates)
        if key is None:
            raise LiteratureStoreError(
                f"{provider} returned a record with no usable identity: it has no "
                "DOI, arXiv id, or OpenAlex id, and not enough of a title, first "
                "author, and year to identify it"
            )

        with self.transaction() as connection:
            for loser in sorted(candidates - {key}):
                self._merge_into(connection, winner=key, loser=loser, reason=provider)
            self._upsert_work(
                connection,
                key=key,
                fields=fields,
                authors=authors,
                provider=provider,
                moment=moment,
            )
            for scheme, value in sorted(normalized.items()):
                connection.execute(
                    "INSERT OR IGNORE INTO work_identifiers"
                    "(work_key, scheme, value, provider) VALUES (?,?,?,?)",
                    (key, scheme, value, provider),
                )
            document = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT OR IGNORE INTO source_records"
                "(work_key, provider, provider_work_id, request_url, retrieved_at,"
                " payload_sha256, payload) VALUES (?,?,?,?,?,?,?)",
                (
                    key,
                    provider,
                    provider_work_id,
                    request_url,
                    moment,
                    digest_text(document),
                    document,
                ),
            )
            self._reindex_work(connection, key)
        return key

    def _matching_keys(
        self,
        identifiers: dict[str, str],
        fields: dict[str, object],
        authors: list[AuthorRecord],
    ) -> set[str]:
        """Return every existing work this record could be."""

        found: set[str] = set()
        for scheme, value in identifiers.items():
            match = self.find_by_identifier(scheme, value)
            if match is not None:
                found.add(match)
        fallback = title_fallback_key(
            title=str(fields.get("title") or ""),
            first_author=authors[0].name if authors else None,
            year=_as_year(fields.get("publication_year")),
        )
        if fallback is not None:
            match = self.resolve_key(work_key(TITLE_SCHEME, fallback))
            if match is not None:
                found.add(match)
            # A work stored under a DOI can still be the one this title names,
            # but only when nothing stronger disagrees: same normalised title,
            # same year, same first-author surname.
            found.update(self._title_matches(fields, authors))
        return found

    def _title_matches(
        self, fields: dict[str, object], authors: list[AuthorRecord]
    ) -> set[str]:
        normalized = normalize_title(str(fields.get("title") or ""))
        year = _as_year(fields.get("publication_year"))
        surname = normalize_author(authors[0].name if authors else None)
        if not normalized or year is None or not surname:
            return set()
        rows = self.connection.execute(
            "SELECT work_key FROM works WHERE normalized_title = ? "
            "AND publication_year = ?",
            (normalized, year),
        ).fetchall()
        matches: set[str] = set()
        for row in rows:
            first = self.connection.execute(
                "SELECT normalized_name FROM work_authors WHERE work_key = ? "
                "AND position = 0",
                (row["work_key"],),
            ).fetchone()
            if first is not None and first["normalized_name"] == surname:
                matches.add(row["work_key"])
        return matches

    @staticmethod
    def _elect_key(
        identifiers: dict[str, str],
        fields: dict[str, object],
        authors: list[AuthorRecord],
        candidates: set[str],
    ) -> str | None:
        """Return the key this record belongs under.

        An existing row wins over a fresh key, because re-keying a work every
        time a stronger identifier arrives would break references a human
        already wrote down. Among several existing rows the strongest-scheme key
        wins, which is what the merge below then consolidates onto.
        """

        if candidates:
            return min(candidates, key=_key_rank)
        for scheme in IDENTIFIER_PRECEDENCE:
            if scheme in identifiers:
                return work_key(scheme, identifiers[scheme])
        fallback = title_fallback_key(
            title=str(fields.get("title") or ""),
            first_author=authors[0].name if authors else None,
            year=_as_year(fields.get("publication_year")),
        )
        return work_key(TITLE_SCHEME, fallback) if fallback else None

    def _merge_into(
        self,
        connection: sqlite3.Connection,
        *,
        winner: str,
        loser: str,
        reason: str,
    ) -> None:
        """Fold ``loser`` into ``winner``, keeping everything both knew.

        Called when a new record proves two rows were always the same work --
        typically a Crossref record supplying the DOI for something arXiv had
        only as a preprint title. Identifiers, source records, files, documents,
        chunks, provenance, and citations all move; fields the winner lacks are
        filled from the loser; the loser becomes an alias so an id a researcher
        wrote down still resolves.
        """

        if winner == loser:
            return
        target = connection.execute(
            "SELECT * FROM works WHERE work_key = ?", (winner,)
        ).fetchone()
        source = connection.execute(
            "SELECT * FROM works WHERE work_key = ?", (loser,)
        ).fetchone()
        if target is None or source is None:
            return

        # Order matters twice over. Every child row moves to the winner before
        # the loser row is deleted, because the foreign keys cascade and a
        # delete-first merge would destroy exactly the identifiers, payloads,
        # and files the merge exists to keep. And the winner's own columns are
        # filled only after that delete, because ``doi``, ``arxiv_id``, and
        # ``openalex_id`` are unique: copying the loser's DOI onto the winner
        # while the loser still holds it is a constraint violation, not a merge.
        keep_authors = not connection.execute(
            "SELECT 1 FROM work_authors WHERE work_key = ? LIMIT 1", (winner,)
        ).fetchone()
        if keep_authors:
            connection.execute(
                "UPDATE work_authors SET work_key = ? WHERE work_key = ?",
                (winner, loser),
            )
        for table in (
            "work_identifiers",
            "field_provenance",
            "source_records",
            "files",
            "documents",
            "chunks",
            "search_results",
            "known_items",
        ):
            connection.execute(
                f"UPDATE OR IGNORE {table} SET work_key = ? WHERE work_key = ?",
                (winner, loser),
            )
        for column in ("citing_key", "cited_key"):
            connection.execute(
                f"UPDATE OR IGNORE citations SET {column} = ? WHERE {column} = ?",
                (winner, loser),
            )
        connection.execute(
            "UPDATE work_aliases SET work_key = ? WHERE work_key = ?", (winner, loser)
        )
        connection.execute("DELETE FROM works WHERE work_key = ?", (loser,))

        filled: dict[str, object] = {
            column: source[column]
            for column in (
                "title",
                "normalized_title",
                "publication_year",
                "venue",
                "work_type",
                "abstract",
                "doi",
                "arxiv_id",
                "openalex_id",
                "pmid",
                "open_access_url",
                "cited_by_count",
                "retraction_note",
            )
            if _is_empty(target[column]) and not _is_empty(source[column])
        }
        if source["is_retracted"]:
            filled["is_retracted"] = 1
        if filled:
            assignments = ", ".join(f"{name} = ?" for name in filled)
            connection.execute(
                f"UPDATE works SET {assignments}, last_updated = ? WHERE work_key = ?",
                (*filled.values(), utc_now(), winner),
            )
        connection.execute(
            "INSERT OR REPLACE INTO work_aliases"
            "(alias_key, work_key, merged_at, reason) VALUES (?,?,?,?)",
            (loser, winner, utc_now(), f"merged after a record from {reason}"),
        )
        connection.execute("DELETE FROM work_fts WHERE work_key = ?", (loser,))
        connection.execute(
            "UPDATE chunk_fts SET work_key = ? WHERE work_key = ?", (winner, loser)
        )

    def _upsert_work(
        self,
        connection: sqlite3.Connection,
        *,
        key: str,
        fields: dict[str, object],
        authors: list[AuthorRecord],
        provider: str,
        moment: str,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM works WHERE work_key = ?", (key,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO works(work_key, first_seen, last_updated) VALUES (?,?,?)",
                (key, moment, moment),
            )
            existing = connection.execute(
                "SELECT * FROM works WHERE work_key = ?", (key,)
            ).fetchone()

        updates: dict[str, object] = {}
        for name in (
            "title",
            "abstract",
            "publication_year",
            "venue",
            "work_type",
            "doi",
            "arxiv_id",
            "openalex_id",
            "pmid",
            "open_access_url",
            "cited_by_count",
            "retraction_note",
        ):
            value = fields.get(name)
            if _is_empty(value):
                continue
            # A value this provider supplied is superseded exactly when the
            # store already held a different one. Filling an empty field is not
            # a disagreement, and agreeing with what is there is not either.
            stored = not _is_empty(existing[name])
            if not stored:
                updates[name] = value
            self._record_provenance(
                connection,
                key=key,
                field=name,
                provider=provider,
                value=value,
                moment=moment,
                superseded=stored and not _same(existing[name], value),
            )
        if fields.get("is_retracted"):
            # A retraction is the one signal a later provider may set on its
            # own: "this was withdrawn" is never overridden by an older record
            # that did not know yet.
            updates["is_retracted"] = 1
            self._record_provenance(
                connection,
                key=key,
                field="is_retracted",
                provider=provider,
                value=True,
                moment=moment,
                superseded=False,
            )
        if "title" in updates:
            updates["normalized_title"] = normalize_title(str(updates["title"]))
        if authors:
            names = [item.name for item in authors]
            stored_names = [
                item["name"]
                for item in connection.execute(
                    "SELECT name FROM work_authors WHERE work_key = ? ORDER BY position",
                    (key,),
                )
            ]
            if not stored_names:
                for author in authors:
                    connection.execute(
                        "INSERT OR REPLACE INTO work_authors"
                        "(work_key, position, name, normalized_name, orcid)"
                        " VALUES (?,?,?,?,?)",
                        (
                            key,
                            author.position,
                            author.name,
                            author.normalized_name or normalize_author(author.name),
                            author.orcid,
                        ),
                    )
            self._record_provenance(
                connection,
                key=key,
                field="authors",
                provider=provider,
                value=names,
                moment=moment,
                superseded=bool(stored_names) and stored_names != names,
            )
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(
                f"UPDATE works SET {assignments}, last_updated = ? WHERE work_key = ?",
                (*updates.values(), moment, key),
            )
        else:
            connection.execute(
                "UPDATE works SET last_updated = ? WHERE work_key = ?", (moment, key)
            )

    @staticmethod
    def _record_provenance(
        connection: sqlite3.Connection,
        *,
        key: str,
        field: str,
        provider: str,
        value: object,
        moment: str,
        superseded: bool,
    ) -> None:
        """Record that ``provider`` asserted this value for this field.

        The digest rather than the value: provenance answers "who said what, and
        is it still the value in force", and the value itself is already in the
        archived source record. A row that disagrees with what is stored is
        marked superseded rather than dropped, which is how a reader finds the
        title that lost.
        """

        connection.execute(
            "INSERT OR REPLACE INTO field_provenance"
            "(work_key, field, provider, value_digest, retrieved_at, superseded)"
            " VALUES (?,?,?,?,?,?)",
            (
                key,
                field,
                provider,
                digest_text(json.dumps(value, ensure_ascii=False, sort_keys=True)),
                moment,
                1 if superseded else 0,
            ),
        )

    # -- reading ---------------------------------------------------------

    def work(self, key: str) -> WorkRecord | None:
        """Return one work with its authors, identifiers, and provenance."""

        live = self.resolve_key(key)
        if live is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM works WHERE work_key = ?", (live,)
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def works(self, keys: Iterable[str]) -> list[WorkRecord]:
        found = [self.work(key) for key in keys]
        return [item for item in found if item is not None]

    def all_work_keys(self) -> list[str]:
        return [
            row["work_key"]
            for row in self.connection.execute(
                "SELECT work_key FROM works ORDER BY work_key"
            )
        ]

    def count_works(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
        )

    def _hydrate(self, row: sqlite3.Row) -> WorkRecord:
        key = row["work_key"]
        authors = [
            AuthorRecord(
                position=item["position"],
                name=item["name"],
                normalized_name=item["normalized_name"],
                orcid=item["orcid"],
            )
            for item in self.connection.execute(
                "SELECT * FROM work_authors WHERE work_key = ? ORDER BY position",
                (key,),
            )
        ]
        identifiers = [
            WorkIdentifier(
                scheme=item["scheme"], value=item["value"], provider=item["provider"]
            )
            for item in self.connection.execute(
                "SELECT * FROM work_identifiers WHERE work_key = ? "
                "ORDER BY scheme, value, provider",
                (key,),
            )
        ]
        provenance = [
            FieldProvenance(
                field=item["field"],
                provider=item["provider"],
                value_digest=item["value_digest"],
                retrieved_at=item["retrieved_at"],
                superseded=bool(item["superseded"]),
            )
            for item in self.connection.execute(
                "SELECT * FROM field_provenance WHERE work_key = ? "
                "ORDER BY field, provider, value_digest",
                (key,),
            )
        ]
        return WorkRecord(
            key=key,
            title=row["title"] or "",
            normalized_title=row["normalized_title"] or "",
            publication_year=row["publication_year"],
            venue=row["venue"],
            work_type=row["work_type"],
            abstract=row["abstract"],
            doi=row["doi"],
            arxiv_id=row["arxiv_id"],
            openalex_id=row["openalex_id"],
            pmid=row["pmid"],
            open_access_url=row["open_access_url"],
            cited_by_count=row["cited_by_count"],
            is_retracted=bool(row["is_retracted"]),
            retraction_note=row["retraction_note"],
            first_seen=row["first_seen"],
            last_updated=row["last_updated"],
            authors=authors,
            identifiers=identifiers,
            provenance=provenance,
        )

    def source_records(self, key: str) -> list[SourceRecord]:
        live = self.resolve_key(key) or key
        return [
            SourceRecord(
                record_id=row["record_id"],
                work_key=row["work_key"],
                provider=row["provider"],
                provider_work_id=row["provider_work_id"],
                request_url=row["request_url"],
                retrieved_at=row["retrieved_at"],
                payload_sha256=row["payload_sha256"],
                payload=row["payload"],
            )
            for row in self.connection.execute(
                "SELECT * FROM source_records WHERE work_key = ? "
                "ORDER BY provider, retrieved_at, record_id",
                (live,),
            )
        ]

    # -- searches --------------------------------------------------------

    def record_search(self, record: SearchRecord, hits: list[SearchHit]) -> int:
        """Archive one provider search and the ranking it returned."""

        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO searches"
                "(query, provider, requested_at, parameters, status, result_count,"
                " detail) VALUES (?,?,?,?,?,?,?)",
                (
                    record.query,
                    record.provider,
                    record.requested_at,
                    record.parameters,
                    str(record.status),
                    record.result_count,
                    record.detail,
                ),
            )
            search_id = int(cursor.lastrowid or 0)
            for hit in hits:
                connection.execute(
                    "INSERT OR REPLACE INTO search_results"
                    "(search_id, rank, work_key, provider_score) VALUES (?,?,?,?)",
                    (search_id, hit.rank, hit.work_key, hit.provider_score),
                )
        return search_id

    def record_search_hits(self, search_id: int, hits: list[SearchHit]) -> None:
        """Attach a provider's ranking to a search that is already recorded.

        Separate from :meth:`record_search` because the archive is written
        before the records are ingested: a call that returned results the store
        then refused still has to appear as a call that was made.
        """

        with self.transaction() as connection:
            for hit in hits:
                connection.execute(
                    "INSERT OR REPLACE INTO search_results"
                    "(search_id, rank, work_key, provider_score) VALUES (?,?,?,?)",
                    (search_id, hit.rank, hit.work_key, hit.provider_score),
                )

    def search_hits(self, search_id: int) -> list[SearchHit]:
        return [
            SearchHit(
                search_id=row["search_id"],
                rank=row["rank"],
                work_key=row["work_key"],
                provider_score=row["provider_score"],
            )
            for row in self.connection.execute(
                "SELECT * FROM search_results WHERE search_id = ? ORDER BY rank",
                (search_id,),
            )
        ]

    def searches(self, limit: int = 50) -> list[SearchRecord]:
        return [
            SearchRecord(
                search_id=row["search_id"],
                query=row["query"],
                provider=row["provider"],
                requested_at=row["requested_at"],
                parameters=row["parameters"],
                status=row["status"],
                result_count=row["result_count"],
                detail=row["detail"],
            )
            for row in self.connection.execute(
                "SELECT * FROM searches ORDER BY search_id DESC LIMIT ?", (limit,)
            )
        ]

    # -- files, documents, chunks ----------------------------------------

    def record_file(self, record: FileRecord) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO files"
                "(file_sha256, work_key, provider, source_url, media_type, byte_size,"
                " retrieved_at, stored_path, extraction_status, extraction_detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record.file_sha256,
                    record.work_key,
                    record.provider,
                    record.source_url,
                    record.media_type,
                    record.byte_size,
                    record.retrieved_at,
                    record.stored_path,
                    str(record.extraction_status),
                    record.extraction_detail,
                ),
            )

    def files(self, key: str) -> list[FileRecord]:
        live = self.resolve_key(key) or key
        return [
            FileRecord(
                file_sha256=row["file_sha256"],
                work_key=row["work_key"],
                provider=row["provider"],
                source_url=row["source_url"],
                media_type=row["media_type"],
                byte_size=row["byte_size"],
                retrieved_at=row["retrieved_at"],
                stored_path=row["stored_path"],
                extraction_status=ExtractionStatus(row["extraction_status"]),
                extraction_detail=row["extraction_detail"],
            )
            for row in self.connection.execute(
                "SELECT * FROM files WHERE work_key = ? ORDER BY file_sha256", (live,)
            )
        ]

    def file(self, file_sha256: str) -> FileRecord | None:
        row = self.connection.execute(
            "SELECT * FROM files WHERE file_sha256 = ?", (file_sha256,)
        ).fetchone()
        if row is None:
            return None
        return FileRecord(
            file_sha256=row["file_sha256"],
            work_key=row["work_key"],
            provider=row["provider"],
            source_url=row["source_url"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            retrieved_at=row["retrieved_at"],
            stored_path=row["stored_path"],
            extraction_status=ExtractionStatus(row["extraction_status"]),
            extraction_detail=row["extraction_detail"],
        )

    def set_extraction(
        self, file_sha256: str, status: ExtractionStatus, detail: str = ""
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE files SET extraction_status = ?, extraction_detail = ? "
                "WHERE file_sha256 = ?",
                (str(status), detail, file_sha256),
            )

    def record_document(self, document: TextDocument, chunks: list[TextChunk]) -> int:
        """Store one extracted document and index its chunks for search."""

        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM documents WHERE file_sha256 = ? AND kind = ?",
                (document.file_sha256, document.kind),
            )
            cursor = connection.execute(
                "INSERT INTO documents"
                "(work_key, file_sha256, kind, extracted_at, char_count, extractor,"
                " extractor_version) VALUES (?,?,?,?,?,?,?)",
                (
                    document.work_key,
                    document.file_sha256,
                    document.kind,
                    document.extracted_at,
                    document.char_count,
                    document.extractor,
                    document.extractor_version,
                ),
            )
            document_id = int(cursor.lastrowid or 0)
            for chunk in chunks:
                inserted = connection.execute(
                    "INSERT INTO chunks"
                    "(document_id, work_key, ordinal, char_start, char_end, text)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        document_id,
                        chunk.work_key,
                        chunk.ordinal,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.text,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunk_fts(chunk_id, work_key, text) VALUES (?,?,?)",
                    (int(inserted.lastrowid or 0), chunk.work_key, chunk.text),
                )
        return document_id

    def documents(self, key: str) -> list[TextDocument]:
        live = self.resolve_key(key) or key
        return [
            TextDocument(
                document_id=row["document_id"],
                work_key=row["work_key"],
                file_sha256=row["file_sha256"],
                kind=row["kind"],
                extracted_at=row["extracted_at"],
                char_count=row["char_count"],
                extractor=row["extractor"],
                extractor_version=row["extractor_version"],
            )
            for row in self.connection.execute(
                "SELECT * FROM documents WHERE work_key = ? ORDER BY document_id",
                (live,),
            )
        ]

    def chunks(self, document_id: int) -> list[TextChunk]:
        return [
            TextChunk(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                work_key=row["work_key"],
                ordinal=row["ordinal"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                text=row["text"],
            )
            for row in self.connection.execute(
                "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal",
                (document_id,),
            )
        ]

    # -- citations and known items ---------------------------------------

    def record_citations(self, edges: Iterable[CitationEdge]) -> int:
        written = 0
        with self.transaction() as connection:
            for edge in edges:
                connection.execute(
                    "INSERT OR IGNORE INTO citations"
                    "(citing_key, cited_key, provider) VALUES (?,?,?)",
                    (edge.citing_key, edge.cited_key, edge.provider),
                )
                written += 1
        return written

    def cited_by(self, key: str) -> list[str]:
        live = self.resolve_key(key) or key
        return [
            row["citing_key"]
            for row in self.connection.execute(
                "SELECT DISTINCT citing_key FROM citations WHERE cited_key = ? "
                "ORDER BY citing_key",
                (live,),
            )
        ]

    def references(self, key: str) -> list[str]:
        live = self.resolve_key(key) or key
        return [
            row["cited_key"]
            for row in self.connection.execute(
                "SELECT DISTINCT cited_key FROM citations WHERE citing_key = ? "
                "ORDER BY cited_key",
                (live,),
            )
        ]

    def record_known_items(self, items: Iterable[KnownItem]) -> int:
        written = 0
        with self.transaction() as connection:
            for item in items:
                connection.execute(
                    "INSERT OR REPLACE INTO known_items"
                    "(fixture, query, work_key, note) VALUES (?,?,?,?)",
                    (item.fixture, item.query, item.work_key, item.note),
                )
                written += 1
        return written

    def known_items(self, fixture: str) -> list[KnownItem]:
        return [
            KnownItem(
                fixture=row["fixture"],
                query=row["query"],
                work_key=row["work_key"],
                note=row["note"],
            )
            for row in self.connection.execute(
                "SELECT * FROM known_items WHERE fixture = ? ORDER BY query, work_key",
                (fixture,),
            )
        ]

    def known_fixtures(self) -> list[str]:
        return [
            row["fixture"]
            for row in self.connection.execute(
                "SELECT DISTINCT fixture FROM known_items ORDER BY fixture"
            )
        ]

    # -- full-text index -------------------------------------------------

    def reindex(self) -> int:
        """Rebuild the work index from the stored rows and return the count."""

        with self.transaction() as connection:
            connection.execute("DELETE FROM work_fts")
            keys = [
                row["work_key"]
                for row in connection.execute(
                    "SELECT work_key FROM works ORDER BY work_key"
                )
            ]
            for key in keys:
                self._reindex_work(connection, key)
        return len(keys)

    @staticmethod
    def _reindex_work(connection: sqlite3.Connection, key: str) -> None:
        row = connection.execute(
            "SELECT title, abstract, venue FROM works WHERE work_key = ?", (key,)
        ).fetchone()
        if row is None:
            return
        authors = " ".join(
            item["name"]
            for item in connection.execute(
                "SELECT name FROM work_authors WHERE work_key = ? ORDER BY position",
                (key,),
            )
        )
        connection.execute("DELETE FROM work_fts WHERE work_key = ?", (key,))
        connection.execute(
            "INSERT INTO work_fts(work_key, title, abstract, authors, venue)"
            " VALUES (?,?,?,?,?)",
            (
                key,
                row["title"] or "",
                row["abstract"] or "",
                authors,
                row["venue"] or "",
            ),
        )


def _normalized_identifiers(raw: dict[str, str]) -> dict[str, str]:
    """Return only the identifiers that normalise to a valid registered form."""

    normalizers = {
        "doi": normalize_doi,
        "arxiv": normalize_arxiv_id,
        "openalex": normalize_openalex_id,
        "pmid": normalize_pmid,
    }
    found: dict[str, str] = {}
    for scheme, value in raw.items():
        normalizer = normalizers.get(scheme)
        if normalizer is None:
            continue
        cleaned = normalizer(value)
        if cleaned:
            found[scheme] = cleaned
    return found


def _identifier_fields(identifiers: dict[str, str]) -> dict[str, str]:
    """Return the stored columns the normalised identifiers imply."""

    columns = {
        "doi": "doi",
        "arxiv": "arxiv_id",
        "openalex": "openalex_id",
        "pmid": "pmid",
    }
    return {
        columns[scheme]: value
        for scheme, value in identifiers.items()
        if scheme in columns
    }


def _key_rank(key: str) -> tuple[int, str]:
    scheme = key.split(":", 1)[0]
    order = (
        IDENTIFIER_PRECEDENCE.index(scheme)
        if scheme in IDENTIFIER_PRECEDENCE
        else len(IDENTIFIER_PRECEDENCE)
    )
    return (order, key)


def _is_empty(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same(stored: object, incoming: object) -> bool:
    if _is_empty(stored):
        return False
    if isinstance(stored, str) and isinstance(incoming, str):
        return stored.strip() == incoming.strip()
    return stored == incoming


def _as_year(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


@contextmanager
def open_store(path: Path | None = None) -> Iterator[LiteratureStore]:
    """Open the shared store for the duration of one operation."""

    store = LiteratureStore.open(path)
    with closing(store.connection):
        yield store
