"""Local ranked retrieval over the scholarly index.

BM25, from SQLite's FTS5, computed locally. That choice is the point of this
module rather than an implementation detail: ranking is the step a system is
most tempted to spend a frontier model on, and it is the step where a model buys
the least. BM25 is deterministic, free, instantaneous, explainable to a
researcher who asks why a paper ranked third, and reproducible next week. A model
re-rank, when it is worth doing at all, happens *after* this and over a short
list this produced.

No embeddings and no vector index, deliberately. They would add a dependency, a
build step, a similarity threshold nobody can justify, and a second copy of the
corpus that silently goes stale -- in exchange for recall improvements this
subsystem has no evidence it needs. The known-item evaluation in
:mod:`.evaluation` exists so that claim can be checked rather than assumed.

Two things make the ranking honest. Field weights are stated here rather than
buried, so a title match outranking an abstract match is a decision a reader can
see and argue with. And ties break on the work key, so the same index and the
same query produce the same order every time.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from research_os.errors import LiteratureError
from research_os.literature.models import SearchResult
from research_os.literature.store import LiteratureStore

#: How much each indexed field counts toward a work's BM25 score.
#:
#: A title match is the strongest signal that a paper is *about* something; an
#: abstract match is next; an author or venue match is usually incidental to a
#: topical query, so they are present but quiet. FTS5's ``bm25()`` treats a
#: larger weight as more important.
FIELD_WEIGHTS: tuple[float, ...] = (0.0, 10.0, 4.0, 1.0, 0.5)
"""Weights for ``(work_key, title, abstract, authors, venue)``.

``work_key`` is an ``UNINDEXED`` column and contributes nothing; it is listed so
the tuple lines up with the table definition and cannot drift out of order.
"""

#: How much full-text chunk matching counts when it is included.
CHUNK_WEIGHTS: tuple[float, ...] = (0.0, 0.0, 1.0)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200

SNIPPET_CHARS = 240

_TOKEN_RE = re.compile(r"[\w][\w.\-/]*", re.UNICODE)


def query_tokens(query: str) -> list[str]:
    """Return the searchable words of a query, refusing one that has none."""

    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        raise LiteratureError(
            "a search needs at least one word; this query has no searchable token"
        )
    return tokens


def to_match_expression(query: str, *, operator: str = "AND") -> str:
    """Return a query as a safe FTS5 MATCH expression.

    FTS5 has its own query grammar with operators (``NEAR``, ``OR``, ``*``,
    column filters, quoted phrases), and a researcher's query is text rather
    than a program. Rather than passing it through and hoping, every token is
    extracted and re-quoted as a literal, which means a query containing
    ``AND``, a stray quote, or a column name is searched for rather than
    executed as syntax.

    The default is conjunctive: a query names several things, and a result
    containing only one of them is usually not what was asked for. ``search``
    relaxes to ``OR`` only when the conjunctive form matched nothing, and says
    so in the result rather than quietly widening what was asked.
    """

    quoted = ['"' + token.replace('"', '""') + '"' for token in query_tokens(query)]
    return f" {operator} ".join(quoted)


@dataclass(frozen=True, slots=True)
class SearchOptions:
    """What a local search is allowed to look at."""

    limit: int = DEFAULT_LIMIT
    include_fulltext: bool = True
    include_retracted: bool = False
    year_from: int | None = None
    year_to: int | None = None

    def bounded_limit(self) -> int:
        return max(1, min(self.limit, MAX_LIMIT))


def search(
    store: LiteratureStore,
    query: str,
    options: SearchOptions | None = None,
) -> list[SearchResult]:
    """Return locally ranked results for ``query``, best first.

    Metadata matches and full-text matches are scored separately and then
    combined per work, because they answer different questions: the metadata
    index says a paper is *about* this, and the full-text index says the words
    *appear* in it. A paper that matches both should outrank one that matches
    either, and the combination is a sum of weighted BM25 scores rather than
    anything cleverer, so it stays explainable.
    """

    settings = options or SearchOptions()
    scores, matched, snippets = _collect(store, to_match_expression(query), settings)
    if not scores and len(query_tokens(query)) > 1:
        # Nothing contained every word. Rather than answering "no results" to a
        # query that plainly describes something in the index, the search is
        # relaxed once -- and every result says it was, so a reader is never
        # shown a widened answer as though it were the one they asked for.
        scores, matched, snippets = _collect(
            store, to_match_expression(query, operator="OR"), settings
        )
        for key in matched:
            matched[key].add("any-term")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    results: list[SearchResult] = []
    for key, score in ranked:
        work = store.work(key)
        if work is None:
            continue
        if work.is_retracted and not settings.include_retracted:
            continue
        if settings.year_from is not None and (
            work.publication_year is None or work.publication_year < settings.year_from
        ):
            continue
        if settings.year_to is not None and (
            work.publication_year is None or work.publication_year > settings.year_to
        ):
            continue
        results.append(
            SearchResult(
                work=work,
                score=round(score, 6),
                matched="+".join(sorted(matched.get(key, {"metadata"}))),
                snippet=snippets.get(key, "") or _abstract_snippet(work.abstract),
            )
        )
        if len(results) >= settings.bounded_limit():
            break
    return results


def _collect(
    store: LiteratureStore,
    expression: str,
    settings: SearchOptions,
) -> tuple[dict[str, float], dict[str, set[str]], dict[str, str]]:
    """Score one MATCH expression over both indexes."""

    scores: dict[str, float] = {}
    matched: dict[str, set[str]] = {}
    snippets: dict[str, str] = {}
    for key, score in _metadata_matches(store, expression):
        scores[key] = scores.get(key, 0.0) + score
        matched.setdefault(key, set()).add("metadata")
    if settings.include_fulltext:
        for key, score, snippet in _fulltext_matches(store, expression):
            scores[key] = scores.get(key, 0.0) + score
            matched.setdefault(key, set()).add("fulltext")
            snippets.setdefault(key, snippet)
    return scores, matched, snippets


def _metadata_matches(
    store: LiteratureStore, expression: str
) -> list[tuple[str, float]]:
    """Return work keys and positive scores from the metadata index."""

    try:
        rows = store.connection.execute(
            "SELECT work_key, bm25(work_fts, ?, ?, ?, ?, ?) AS score "
            "FROM work_fts WHERE work_fts MATCH ? "
            "ORDER BY score, work_key LIMIT 500",
            (*FIELD_WEIGHTS, expression),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise LiteratureError(f"the search index rejected that query: {exc}") from exc
    # FTS5 returns bm25() as a negative number, most relevant most negative, so
    # the sign is flipped to make "larger is better" true everywhere else.
    return [(row["work_key"], -float(row["score"])) for row in rows]


def _fulltext_matches(
    store: LiteratureStore, expression: str
) -> list[tuple[str, float, str]]:
    """Return the best full-text chunk score per work, with a snippet.

    Best chunk rather than summed chunks: a long paper has more chunks, and
    summing would rank a book above a precise short paper for no reason but
    length.
    """

    try:
        rows = store.connection.execute(
            "SELECT work_key, bm25(chunk_fts, ?, ?, ?) AS score, "
            "snippet(chunk_fts, 2, '', '', ' ... ', 24) AS excerpt "
            "FROM chunk_fts WHERE chunk_fts MATCH ? "
            "ORDER BY score, work_key LIMIT 2000",
            (*CHUNK_WEIGHTS, expression),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise LiteratureError(f"the search index rejected that query: {exc}") from exc
    best: dict[str, tuple[float, str]] = {}
    for row in rows:
        key = row["work_key"]
        score = -float(row["score"])
        if key not in best or score > best[key][0]:
            best[key] = (
                score,
                " ".join((row["excerpt"] or "").split())[:SNIPPET_CHARS],
            )
    return [(key, score, excerpt) for key, (score, excerpt) in best.items()]


def _abstract_snippet(abstract: str | None) -> str:
    if not abstract:
        return ""
    text = " ".join(abstract.split())
    return text[:SNIPPET_CHARS] + ("..." if len(text) > SNIPPET_CHARS else "")
