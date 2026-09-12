"""Known-item evaluation: a regression guard on retrieval, not a benchmark.

The question this answers is narrow and useful: for a query where a human has
already said which works are relevant, does the local ranking still return them,
and still return them near the top? That is exactly the property that degrades
silently when field weights are adjusted, when a tokenizer changes, or when
chunking is retuned -- and it is the property nobody notices has degraded until a
literature review quietly misses the paper it was supposed to find.

Two things it is deliberately not.

It is not a benchmark. There is no held-out corpus, no MAP, no NDCG, and no
leaderboard. A fixture is a handful of queries a researcher wrote down, and the
verdict is "the known items came back" or "these did not".

It is not something to optimise against. A fixture that is tuned until it passes
has stopped measuring anything. It is here to fail when retrieval breaks, and
the correct response to a failure is to look at why, not to reweight until it
goes green.
"""

from __future__ import annotations

from dataclasses import dataclass

from research_os.literature.models import EvaluationOutcome, KnownItem
from research_os.literature.search import SearchOptions, search
from research_os.literature.store import LiteratureStore

#: How deep a known item may rank and still count as retrieved.
#:
#: Ten because that is roughly what a person actually reads before deciding the
#: search failed. A known item at rank 40 is not a success with a caveat.
DEFAULT_CUTOFF = 10


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """The result of running one whole fixture."""

    fixture: str
    cutoff: int
    outcomes: tuple[EvaluationOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.outcomes)

    @property
    def queries(self) -> int:
        return len(self.outcomes)

    @property
    def failing(self) -> tuple[EvaluationOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.ok)

    @property
    def mean_recall(self) -> float:
        if not self.outcomes:
            return 1.0
        return sum(item.recall for item in self.outcomes) / len(self.outcomes)

    def summary(self) -> str:
        state = "PASS" if self.ok else "FAIL"
        return (
            f"{state}  fixture {self.fixture}: {self.queries} queries, "
            f"recall@{self.cutoff} {self.mean_recall:.2f}, "
            f"{len(self.failing)} failing"
        )


def evaluate(
    store: LiteratureStore,
    fixture: str,
    *,
    cutoff: int = DEFAULT_CUTOFF,
) -> EvaluationReport:
    """Run every query in ``fixture`` and report what came back.

    A missing work is a failure of the fixture rather than of retrieval, and it
    is reported as such: an expected key that names nothing in the store means
    the corpus was never loaded, and calling that a ranking regression would
    send a reader looking in the wrong place.
    """

    items = store.known_items(fixture)
    by_query: dict[str, list[KnownItem]] = {}
    for item in items:
        by_query.setdefault(item.query, []).append(item)

    outcomes: list[EvaluationOutcome] = []
    for query in sorted(by_query):
        expected = sorted(
            {
                store.resolve_key(item.work_key) or item.work_key
                for item in by_query[query]
            }
        )
        results = search(store, query, SearchOptions(limit=cutoff))
        retrieved = [item.work.key for item in results]
        ranks = {key: retrieved.index(key) + 1 for key in expected if key in retrieved}
        outcomes.append(
            EvaluationOutcome(
                query=query,
                expected=expected,
                retrieved=retrieved,
                found=sorted(ranks),
                missing=sorted(set(expected) - set(ranks)),
                ranks=ranks,
            )
        )
    return EvaluationReport(fixture=fixture, cutoff=cutoff, outcomes=tuple(outcomes))


def render_report(report: EvaluationReport) -> str:
    """Render one evaluation for a human, naming what was missed."""

    lines = ["", report.summary(), ""]
    for outcome in report.outcomes:
        state = "ok  " if outcome.ok else "MISS"
        lines.append(f"{state}  {outcome.query}")
        for key in outcome.expected:
            rank = outcome.ranks.get(key)
            position = f"rank {rank}" if rank else f"not in the top {report.cutoff}"
            lines.append(f"        {key}  {position}")
    lines.append("")
    return "\n".join(lines) + "\n"
