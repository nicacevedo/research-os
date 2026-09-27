"""INV-05, second round: a distinct executed retrieval path, under an operational rule.

The independent review of ``8e92e8c`` (``REORDERED_QUERY``) satisfied the
"second terminology path" with ``lasso support overlap`` followed by
``overlap support lasso``: the same three words, two completed retrievals, one
newly assessed work. The gate compared a digest over case and whitespace only,
so word order made a "different terminology".

The fix is not a sorted digest. The claim itself was too strong: Research OS
cannot establish that two searches are *semantically* independent. What it can
establish, and what INV-05 now says, is a **distinct executed retrieval path**:
a separate, successful, recorded execution whose normalised content terms
(``digests.retrieval_terms``: NFKC, casefold, split on anything that is not a
letter or digit, stopwords and one-letter tokens dropped, a trailing plural
``s`` folded, duplicates removed, order discarded) include at least one term
that no first-path search used -- where two terms count as the same when one
folded term is a prefix of the other (both at least three characters) -- and
that retrieved and had assessed at least one work no first-path search
retrieved. The lexical rule is deliberately conservative: it errs towards
calling two searches the same, and it is not a proof of independent meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio import gates
from research_os.portfolio.models import (
    AdjudicationType,
    EvidenceKind,
    EvidenceStrength,
    IdeaEvidence,
    LiteratureRetrieval,
    RetrievalPurpose,
    RetrievalStatus,
)

T0 = datetime(2026, 9, 27, tzinfo=UTC)
FIRST = "lasso support overlap"
RULE = gates.REPLICATION_RULES[AdjudicationType.NOVELTY_OR_LITERATURE]


def _search(
    retrieval_id: str,
    purpose: RetrievalPurpose,
    query: str,
    keys: tuple[str, ...],
    action: str,
    *,
    status: RetrievalStatus = RetrievalStatus.COMPLETED,
) -> LiteratureRetrieval:
    completed = status is RetrievalStatus.COMPLETED
    return LiteratureRetrieval(
        retrieval_id=retrieval_id,
        project_id="p",
        idea_id="i",
        idea_version=1,
        action_id=action,
        purpose=purpose,
        query=query,
        query_digest=pdigests.retrieval_query_digest(query),
        backend="one-index",
        result_limit=12,
        status=status,
        result_keys=keys if completed else (),
        result_digest=pdigests.retrieval_result_digest(keys) if completed else None,
        started_at=T0,
        completed_at=T0 if completed else None,
    )


def _assessed(retrieval_id: str, key: str) -> IdeaEvidence:
    return IdeaEvidence(
        evidence_id=f"e-{retrieval_id}-{key}",
        idea_id="i",
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="assessed",
        literature_key=key,
        source_call_id=f"call-{retrieval_id}",
        retrieval_id=retrieval_id,
        created_at=T0 + timedelta(minutes=1),
    )


def _second_path_met(second_query: str, *, new_work_assessed: bool = True) -> bool:
    first = _search("r1", RetrievalPurpose.AUDIT, FIRST, ("W1", "W2"), "a1")
    second = _search(
        "r2", RetrievalPurpose.SECOND_PATH, second_query, ("W1", "W2", "W3"), "a2"
    )
    evidence = (_assessed("r2", "W3"),) if new_work_assessed else ()
    return gates._replication_met(RULE, evidence, set(), (first, second))


# ------------------------------------------------ the reviewer's case --------
def test_rev_reordered_identical_terms_are_not_a_distinct_retrieval_path() -> None:
    """REORDERED_QUERY: same word multiset, separate executions, a new work."""

    assert not _second_path_met("overlap support lasso")


# ----------------------------------------- superficial transformations -------
@pytest.mark.parametrize(
    "variant",
    [
        "Lasso, SUPPORT-overlap!",  # punctuation and case
        "lasso lasso support overlap overlap",  # duplicated tokens
        "  lasso\tsupport \n overlap  ",  # whitespace
        "lasso support overlap",  # the same normalised query
        "the lasso and its support overlap",  # stopword padding
        "lassos supports overlaps",  # plural folding
        "lasso's support overlap",  # possessive
        "lasso support",  # a subset of the first path's words
        "Ｌａｓｓｏ ｓｕｐｐｏｒｔ ｏｖｅｒｌａｐ",  # full-width forms, NFKC
        "supporting overlapping lassoes",  # inflections sharing a prefix
    ],
)
def test_a_superficial_variant_of_the_first_path_is_not_a_second_path(
    variant: str,
) -> None:
    assert not _second_path_met(variant)


def test_a_recombination_of_first_path_words_is_not_a_second_path() -> None:
    """Every word was already used by some first-path search: nothing new."""

    first = _search("r1", RetrievalPurpose.AUDIT, FIRST, ("W1",), "a1")
    screen = _search(
        "r0", RetrievalPurpose.NOVELTY_SCREEN, "sparse recovery", ("W2",), "a0"
    )
    second = _search(
        "r2", RetrievalPurpose.SECOND_PATH, "sparse lasso recovery", ("W3",), "a2"
    )
    assert not gates._replication_met(
        RULE, (_assessed("r2", "W3"),), set(), (screen, first, second)
    )


def test_a_failed_first_path_search_still_counts_as_words_already_used() -> None:
    """Conservative: a first-path attempt that failed still used its words."""

    first = _search("r1", RetrievalPurpose.AUDIT, FIRST, ("W1",), "a1")
    failed = _search(
        "r0",
        RetrievalPurpose.AUDIT,
        "compressed sensing",
        (),
        "a0",
        status=RetrievalStatus.FAILED,
    )
    second = _search(
        "r2", RetrievalPurpose.SECOND_PATH, "compressed sensing", ("W3",), "a2"
    )
    assert not gates._replication_met(
        RULE, (_assessed("r2", "W3"),), set(), (first, failed, second)
    )


# ------------------------------------------------------ what still passes ----
def test_a_materially_different_term_set_with_new_assessed_work_passes() -> None:
    assert _second_path_met("compressed sensing basis pursuit")


def test_one_new_content_term_is_enough_under_the_operational_rule() -> None:
    """The rule is lexical, stated, and not a claim of independent meaning."""

    assert _second_path_met("lasso support recovery guarantees")


def test_new_terms_with_no_newly_assessed_work_are_not_a_path() -> None:
    assert not _second_path_met(
        "compressed sensing basis pursuit", new_work_assessed=False
    )


def test_new_terms_from_the_same_execution_are_not_a_path() -> None:
    """A distinct execution: a different action, not a relabelled first search."""

    first = _search("r1", RetrievalPurpose.AUDIT, FIRST, ("W1",), "a1")
    second = _search(
        "r2", RetrievalPurpose.SECOND_PATH, "compressed sensing", ("W3",), "a1"
    )
    assert not gates._replication_met(
        RULE, (_assessed("r2", "W3"),), set(), (first, second)
    )


def test_an_unsuccessful_second_search_is_not_a_path() -> None:
    first = _search("r1", RetrievalPurpose.AUDIT, FIRST, ("W1",), "a1")
    second = _search(
        "r2",
        RetrievalPurpose.SECOND_PATH,
        "compressed sensing",
        (),
        "a2",
        status=RetrievalStatus.FAILED,
    )
    assert not gates._replication_met(
        RULE, (_assessed("r2", "W3"),), set(), (first, second)
    )


# ------------------------------------------------ the normalisation itself ---
def test_the_term_fingerprint_is_deterministic_and_order_free() -> None:
    assert pdigests.retrieval_terms("Overlap support, LASSO!") == (
        "lasso",
        "overlap",
        "support",
    )
    assert pdigests.retrieval_terms("the lassos' supports") == ("lasso", "support")
    assert pdigests.retrieval_terms("a of the and") == ()
    assert pdigests.retrieval_terms_digest("b a") == pdigests.retrieval_terms_digest(
        "A  B"
    )


def test_a_query_of_only_stopwords_is_no_path_at_all() -> None:
    assert not _second_path_met("the of and to")
