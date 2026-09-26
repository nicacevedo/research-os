"""INV-05 -- search claims, confidence and independence come from executed searches.

The findings this file closes (final adversarial review of 37e8afe):

- **H1.** HUMAN_READY's "second terminology path" compared the keys two scouts
  *cited*. Over an index that returns the same works whatever it is asked, a
  second scout handed a byte-identical prompt that cited a work the first left
  out passed as an independent path, and a retried first audit counted as the
  second path.
- **M1.** ``literature_confidence`` and "over N query(ies)" were computed from
  the scout's own ``queries`` list: six invented queries over one executed
  search gave a confidence of 1.0.

Every search is now a ``literature_retrievals`` row the system writes before
the search and completes after it, owned by the action that ran it; every
literature row an audit writes names its retrieval. The gate's criterion is in
``docs/ARCHITECTURE_INVARIANTS.md`` (INV-05) and is exercised here case by
case: the same search referenced twice, a retried first audit, an identical
cached result reused, a scout that claims queries no search ran, and the
genuine separate path that must still pass.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio import gates
from research_os.portfolio.models import (
    EvidenceKind,
    EvidenceStrength,
    IdeaEvidence,
    IdeaStatus,
    LiteratureRetrieval,
    RetrievalPurpose,
    RetrievalStatus,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TerminologyAwareLiterature,
    TwoPathRouter,
    _drive,
    _matrix,
    _router,
    _why,
    checkpoint_tables,
)

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

T0 = datetime(2026, 9, 26, tzinfo=UTC)
FIRST_KEYS = ("openalex:W1", "openalex:W2", "openalex:W3")
NEW_KEYS = ("openalex:W4", "openalex:W5")


def _search(
    retrieval_id: str,
    *,
    purpose: RetrievalPurpose,
    query: str,
    keys: tuple[str, ...],
    action: str | None,
    minute: int = 0,
    status: RetrievalStatus = RetrievalStatus.COMPLETED,
) -> LiteratureRetrieval:
    completed = status is RetrievalStatus.COMPLETED
    return LiteratureRetrieval(
        retrieval_id=retrieval_id,
        project_id="demo-project",
        idea_id="PIDEA-x",
        idea_version=1,
        action_id=action,
        purpose=purpose,
        query=query,
        query_digest=pdigests.retrieval_query_digest(query),
        backend="test-index",
        result_limit=12,
        status=status,
        result_keys=keys if completed else (),
        result_digest=pdigests.retrieval_result_digest(keys) if completed else None,
        started_at=T0 + timedelta(minutes=minute),
        completed_at=T0 + timedelta(minutes=minute) if completed else None,
    )


def _cites(
    retrieval: str | None, *keys: str, call: str = "MCALL-scout", minute: int = 0
) -> tuple[IdeaEvidence, ...]:
    return tuple(
        IdeaEvidence(
            evidence_id=f"IEVD-{call}-{key}",
            idea_id="PIDEA-x",
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.SUPPORTS,
            summary=f"{key} assessed",
            literature_key=key,
            source_call_id=call,
            retrieval_id=retrieval,
            created_at=T0 + timedelta(minutes=minute),
        )
        for key in keys
    )


AUDIT = _search(
    "PRET-audit",
    purpose=RetrievalPurpose.AUDIT,
    query="Has this comparison already been published?",
    keys=FIRST_KEYS,
    action="IACT-audit",
)
AUDIT_ROWS = _cites("PRET-audit", *FIRST_KEYS[:2], call="MCALL-audit")


def _second(**overrides: Any) -> LiteratureRetrieval:
    fields: dict[str, Any] = {
        "purpose": RetrievalPurpose.SECOND_PATH,
        "query": "the same result under other terminology",
        "keys": NEW_KEYS,
        "action": "IACT-replicate",
        "minute": 9,
    }
    fields.update(overrides)
    return _search("PRET-second", **fields)


def _met(
    retrievals: tuple[LiteratureRetrieval, ...], evidence: tuple[IdeaEvidence, ...]
) -> bool:
    return gates._second_terminology_path(evidence, retrievals, 1)


# ----------------------------------------------------------- the control ----
def test_a_genuinely_separate_retrieval_path_passes() -> None:
    """Different execution, different words, different results, new works assessed."""

    rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W4", call="MCALL-second"))
    assert _met((AUDIT, _second()), rows)


# ------------------------------------------------------ the adversaries ----
def test_the_same_search_referenced_twice_is_one_path() -> None:
    """Two scouts, two calls, one search: citing different works from it is not looking again."""

    other = _cites("PRET-audit", "openalex:W3", call="MCALL-other-scout", minute=5)
    assert not _met((AUDIT,), (*AUDIT_ROWS, *other))
    # Even beside a real second-path search, rows bound to the *first* search
    # do not become the second path's assessment.
    assert not _met((AUDIT, _second()), (*AUDIT_ROWS, *other))


def test_a_retried_first_audit_is_not_a_second_path() -> None:
    """The retry is the first search again, under a new action -- purpose AUDIT."""

    retry = _search(
        "PRET-retry",
        purpose=RetrievalPurpose.AUDIT,
        query=AUDIT.query,
        keys=(*FIRST_KEYS, "openalex:W9"),
        action="IACT-retry",
        minute=20,
    )
    rows = (*AUDIT_ROWS, *_cites("PRET-retry", "openalex:W9", call="MCALL-retry"))
    assert not _met((AUDIT, retry), rows)


def test_an_identical_cached_result_reused_is_not_a_second_path() -> None:
    """New words, the same retrieved set: nothing independent was found."""

    cached = _second(keys=tuple(reversed(FIRST_KEYS)))
    rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W3", call="MCALL-second"))
    assert not _met((AUDIT, cached), rows)


def test_the_same_words_asked_again_are_not_a_different_terminology() -> None:
    same_words = _second(query="  has this COMPARISON already been published?  ")
    rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W4", call="MCALL-second"))
    assert not _met((AUDIT, same_words), rows)


def test_one_execution_cannot_be_both_paths() -> None:
    owned_by_the_audit = _second(action="IACT-audit")
    rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W4", call="MCALL-second"))
    assert not _met((AUDIT, owned_by_the_audit), rows)
    unowned = _second(action=None)
    assert not _met((AUDIT, unowned), rows)


def test_new_works_retrieved_but_never_assessed_are_not_a_path() -> None:
    assert not _met((AUDIT, _second()), AUDIT_ROWS)


def test_a_new_work_cited_outside_the_second_search_is_not_its_assessment() -> None:
    """The second search retrieved W4, but the row citing W4 names another search."""

    elsewhere = _cites("PRET-audit", "openalex:W4", call="MCALL-other", minute=12)
    unbound = _cites(None, "openalex:W4", call="MCALL-prose", minute=13)
    assert not _met((AUDIT, _second()), (*AUDIT_ROWS, *elsewhere))
    assert not _met((AUDIT, _second()), (*AUDIT_ROWS, *unbound))


def test_a_claimed_search_with_no_completed_execution_is_not_a_path() -> None:
    """The scout says it searched; nothing completed. Rows naming no search count for nothing."""

    for status in (RetrievalStatus.STARTED, RetrievalStatus.FAILED):
        pending = _second(status=status)
        rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W4", call="MCALL-second"))
        assert not _met((AUDIT, pending), rows), status
    # And rows that name no retrieval at all -- every row a model's prose could
    # produce -- are no path whatever keys they cite.
    unbound = _cites(None, *NEW_KEYS, call="MCALL-second")
    assert not _met((AUDIT,), (*AUDIT_ROWS, *unbound))


def test_a_work_the_first_path_retrieved_but_did_not_cite_is_not_new() -> None:
    """Retrieved, not cited, is what "found" means: W3 was found by the first search."""

    refound = _second(keys=("openalex:W3", "openalex:W7"))
    rows = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W3", call="MCALL-second"))
    assert not _met((AUDIT, refound), rows)
    rows_new = (*AUDIT_ROWS, *_cites("PRET-second", "openalex:W7", call="MCALL-second"))
    assert _met((AUDIT, refound), rows_new)


# ------------------------------------------------ recorded and owned, e2e ----
def test_every_search_a_stage_runs_is_a_recorded_owned_retrieval(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    literature = TerminologyAwareLiterature()
    trace = _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        _router(runtime_db),
        literature,
    )
    version = portfolio.require_version(idea.idea_id)
    retrievals = portfolio.list_retrievals(idea_id=idea.idea_id)
    assert len(retrievals) == len(literature.queries), _why(trace)
    assert [item.query for item in retrievals] == literature.queries
    actions = {
        item.action_id: item for item in portfolio.list_actions(idea_id=idea.idea_id)
    }
    for item in retrievals:
        assert item.status is RetrievalStatus.COMPLETED
        assert item.backend == "TerminologyAwareLiterature"
        assert item.action_id in actions, "a search no action owns"
        assert item.run_id == actions[item.action_id].run_id
        assert item.result_digest == pdigests.retrieval_result_digest(item.result_keys)
    purposes = {item.purpose for item in retrievals}
    assert {RetrievalPurpose.AUDIT, RetrievalPurpose.SECOND_PATH} <= purposes
    rows = [
        row
        for row in portfolio.list_evidence(
            idea_id=idea.idea_id, idea_version=version.version
        )
        if row.kind is EvidenceKind.LITERATURE
    ]
    by_id = {item.retrieval_id: item for item in retrievals}
    for row in rows:
        assert row.retrieval_id in by_id, "a literature row that names no search"
        assert row.literature_key in by_id[row.retrieval_id].result_keys
    # And it earns HUMAN_READY through the genuine second path.
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.HUMAN_READY, _why(
        trace
    )


class _FailingIndex(TerminologyAwareLiterature):
    def search(self, query: str, *, limit: int = 12) -> Any:
        if "terminolog" in query.lower():
            raise OSError("the index is unreadable")
        return super().search(query, limit=limit)


def test_a_search_that_raises_is_recorded_failed_and_counts_for_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    # The stage runs inside its execution lock's connection context, which
    # classifies whatever escapes it -- the index's OSError arrives as a
    # database-layer error, exactly as it did when only the graph was inside
    # the lock. What matters here is the record, below.
    with pytest.raises((OSError, RuntimeDatabaseError), match="unreadable"):
        _drive(
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea.idea_id,
            _router(runtime_db),
            _FailingIndex(),
        )
    failed = [
        item
        for item in portfolio.list_retrievals(idea_id=idea.idea_id)
        if item.status is RetrievalStatus.FAILED
    ]
    assert len(failed) == 1 and "unreadable" in (failed[0].error or "")
    assert failed[0].purpose is RetrievalPurpose.SECOND_PATH
    assert portfolio.require_idea(idea.idea_id).status is not IdeaStatus.HUMAN_READY


# ------------------------------------------------------------------ M1 -----
INVENTED = tuple(f"invented query {n}" for n in range(6))


class _ClaimsSixQueries(TwoPathRouter):
    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            keys = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt)))
            self.answers = {
                **self.answers,
                "literature_scout": {**_matrix(keys), "queries": list(INVENTED)},
            }
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


def test_confidence_and_the_search_count_are_the_executed_searches(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _router(runtime_db)
    router = _ClaimsSixQueries(answers=base.answers, store=base.store)
    index = TerminologyAwareLiterature()
    trace = _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        index,
        steps=12,
    )
    assert not set(INVENTED) & set(index.queries)
    version = portfolio.require_version(idea.idea_id)
    executed = {
        item.query_digest
        for item in portfolio.list_retrievals(
            idea_id=idea.idea_id, idea_version=version.version
        )
        if item.status is RetrievalStatus.COMPLETED
        and item.purpose is not RetrievalPurpose.NOVELTY_SCREEN
    }
    confidence = version.dimensions.literature_confidence
    assert confidence is not None, _why(trace)
    assert confidence == pytest.approx(min(1.0, len(executed) / 6.0)), _why(trace)
    assert confidence < 1.0
    audits = [
        action
        for action in portfolio.list_actions(idea_id=idea.idea_id)
        if action.stage is Stage.LITERATURE_AUDIT
    ]
    assert audits and all("executed search" in (a.detail or "") for a in audits)
    assert not any("6 query" in (a.detail or "") for a in audits)
