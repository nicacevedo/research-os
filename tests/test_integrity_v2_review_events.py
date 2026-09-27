"""INV-04, second round: a review and all of its objections are one durable event.

The independent review of ``8e92e8c`` reproduced three failures of one design,
in which an objection was keyed on its normalised text per version and written
in its own transaction after the review:

- ``FATAL_DEDUPED_AS_MINOR`` -- a later reviewer's FATAL objection worded like
  an earlier reviewer's MINOR one was "the same objection": the MINOR row and
  the earlier review's attribution stood, and the fatal gate saw nothing;
- ``LOST_REVIEW_OBJECTION`` -- a worker that died after the review committed
  and before its objection did left a durable ``PASS_WITH_OBJECTIONS`` review
  of FATAL severity with zero objections, and its role counted as completed;
- ``REPEATED_OBJECTION`` (M3, still open) -- a re-run's objection with the same
  text was attached to the first call's review.

What these tests hold (``docs/ARCHITECTURE_INVARIANTS.md``, INV-04): the review
and its objection set commit together or not at all (``sql/0044`` checks it at
commit, whatever the code does); nothing is deduplicated across reviews; what a
review and its objections said is immutable; a legacy review whose objection
set cannot be shown complete is never live; and the gates themselves refuse a
live review whose own objections are not all standing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio import gates, runner, stages
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ObjectionTarget,
    QualityTier,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.store import PortfolioStateError, PortfolioStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.interfaces import ModelResponse
from research_os.runtime.models import ModelCallStatus
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project"]

SAME = "the identification argument assumes what it proves"
CLAIM = ObjectionTarget.CLAIM


def _call(db: Database, role: str = "reviewer") -> str:
    return (
        RuntimeStore(db)
        .record_model_call(provider="codex", role=role, status=ModelCallStatus.OK)
        .call_id
    )


def _context(
    portfolio: PortfolioStore, db: Database, idea_id: str, project: str
) -> Any:
    return SimpleNamespace(
        portfolio=portfolio,
        runtime=RuntimeStore(db),
        idea_id=idea_id,
        project_id=project,
        action_id=None,
    )


def _respond(
    portfolio: PortfolioStore,
    db: Database,
    idea_id: str,
    project: str,
    *,
    role: ReviewerRole,
    objections: list[tuple[Severity, str]],
    call_id: str | None = None,
    verdict: ReviewVerdict = ReviewVerdict.PASS_WITH_OBJECTIONS,
) -> str:
    """One reviewer response, recorded the way the runner records it."""

    call = call_id or _call(db, f"{role}_reviewer")
    snapshot = stages.snapshot_for(portfolio, portfolio.require_idea(idea_id))
    assert snapshot is not None
    worst = max(
        (severity for severity, _ in objections),
        key=lambda item: {
            Severity.MINOR: 1,
            Severity.MAJOR: 2,
            Severity.CRITICAL: 3,
            Severity.FATAL: 4,
        }[item],
        default=Severity.NONE,
    )
    response = ModelResponse(
        provider="codex",
        model="gpt-test",
        structured={
            "verdict": str(verdict),
            "objections": [
                {"severity": str(severity), "summary": text}
                for severity, text in objections
            ],
        },
        call_id=call,
    )
    return runner._record_review(
        _context(portfolio, db, idea_id, project),
        snapshot,
        role=role,
        template=runner.PORTFOLIO_TEMPLATES["methodology_reviewer"],
        response=response,
        verdict=verdict,
        severity=worst,
        summary="a reading",
        objections=[(severity, CLAIM, text) for severity, text in objections],
    )


def _promising(portfolio: PortfolioStore, idea_id: str) -> list[str]:
    snapshot = stages.snapshot_for(portfolio, portfolio.require_idea(idea_id))
    assert snapshot is not None
    return gates._promising_unmet(
        snapshot.version,
        portfolio.open_objections(idea_id=idea_id),
        frozenset({Stage.NOVELTY_SCREEN}),
        portfolio.live_reviews(idea_id=idea_id),
    )


# ------------------------------------------------ the reviewer's three cases --
def test_rev_a_later_fatal_objection_is_not_folded_into_an_earlier_minor_one(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """FATAL_DEDUPED_AS_MINOR: same words, two reviews, two objections."""

    idea, _ = seed_idea(portfolio, runtime_project)
    first = _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.METHODOLOGY,
        objections=[(Severity.MINOR, SAME)],
    )
    second = _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.SKEPTIC,
        objections=[(Severity.FATAL, SAME)],
    )

    (minor,) = portfolio.review_objections(first)
    (fatal,) = portfolio.review_objections(second)
    assert minor.objection_id != fatal.objection_id
    assert (minor.severity, minor.raised_in_review) == (Severity.MINOR, first)
    assert (fatal.severity, fatal.raised_in_review) == (Severity.FATAL, second)
    assert minor.objection_key == fatal.objection_key, "the stickiness key remains"
    unmet = _promising(portfolio, idea.idea_id)
    assert any("unanswered fatal objection" in item for item in unmet), unmet


def test_rev_a_crash_before_the_objections_leaves_no_review_at_all(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOST_REVIEW_OBJECTION: the worker dies between the review and its objection.

    Simulated at the same point the reviewer chose -- after the review row is
    written, before the objection is -- which is now inside one transaction:
    the review goes with it, and the role is still to be done.
    """

    idea, _ = seed_idea(portfolio, runtime_project)

    def die(_summary: str) -> str:
        raise OSError("simulated worker death after the review insert")

    with monkeypatch.context() as patched:
        patched.setattr(pdigests, "objection_key", die)
        with pytest.raises(RuntimeDatabaseError):
            _respond(
                portfolio,
                runtime_db,
                idea.idea_id,
                runtime_project,
                role=ReviewerRole.METHODOLOGY,
                objections=[(Severity.FATAL, "fatal flaw")],
            )

    assert portfolio.list_reviews(idea_id=idea.idea_id) == ()
    assert portfolio.open_objections(idea_id=idea.idea_id) == ()
    snapshot = stages.snapshot_for(portfolio, portfolio.require_idea(idea.idea_id))
    assert snapshot is not None
    assert stages.board_state(snapshot)[ReviewerRole.METHODOLOGY] == "MISSING"


def test_rev_a_rerun_objection_with_the_same_words_is_attributed_to_the_rerun(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """REPEATED_OBJECTION (M3): each call's objection belongs to its own review."""

    idea, _ = seed_idea(portfolio, runtime_project)
    reviews = [
        _respond(
            portfolio,
            runtime_db,
            idea.idea_id,
            runtime_project,
            role=ReviewerRole.METHODOLOGY,
            objections=[(Severity.CRITICAL, SAME)],
            verdict=ReviewVerdict.REVISE,
        )
        for _ in range(2)
    ]
    assert reviews[0] != reviews[1]
    attributions = [
        portfolio.review_objections(review)[0].raised_in_review for review in reviews
    ]
    assert attributions == reviews


# ------------------------------------------------- neighbouring attacks ------
def test_fatal_first_then_minor_keeps_both_and_the_fatal_blocks(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    for role, severity in (
        (ReviewerRole.SKEPTIC, Severity.FATAL),
        (ReviewerRole.METHODOLOGY, Severity.MINOR),
    ):
        _respond(
            portfolio,
            runtime_db,
            idea.idea_id,
            runtime_project,
            role=role,
            objections=[(severity, SAME)],
        )
    severities = sorted(
        str(item.severity) for item in portfolio.open_objections(idea_id=idea.idea_id)
    )
    assert severities == ["FATAL", "MINOR"]
    assert any("fatal" in item for item in _promising(portfolio, idea.idea_id))


def test_every_objection_of_one_review_is_recorded_in_order(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    review_id = _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.NOVELTY,
        objections=[
            (Severity.MINOR, "a"),
            (Severity.CRITICAL, "b"),
            (Severity.MINOR, "a"),  # the reviewer repeated itself: two rows
        ],
        verdict=ReviewVerdict.REVISE,
    )
    rows = portfolio.review_objections(review_id)
    assert [(item.ordinal, item.summary) for item in rows] == [
        (0, "a"),
        (1, "b"),
        (2, "a"),
    ]
    (review,) = [
        item
        for item in portfolio.list_reviews(idea_id=idea.idea_id)
        if item.review_id == review_id
    ]
    assert review.objection_count == 3 and review.severity is Severity.CRITICAL


def test_a_replay_of_the_same_call_writes_nothing_twice(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    call = _call(runtime_db)
    first = _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.SKEPTIC,
        objections=[(Severity.MAJOR, "x"), (Severity.MINOR, "y")],
        call_id=call,
    )
    replay = _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.SKEPTIC,
        objections=[(Severity.MAJOR, "x"), (Severity.MINOR, "y")],
        call_id=call,
    )
    assert replay == first
    assert len(portfolio.list_reviews(idea_id=idea.idea_id)) == 1
    assert len(portfolio.open_objections(idea_id=idea.idea_id)) == 2


def test_a_replay_that_disagrees_with_its_record_is_refused(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """A call's record is its record: a different objection set is not applied."""

    idea, _ = seed_idea(portfolio, runtime_project)
    call = _call(runtime_db)
    _respond(
        portfolio,
        runtime_db,
        idea.idea_id,
        runtime_project,
        role=ReviewerRole.SKEPTIC,
        objections=[(Severity.MAJOR, "x")],
        call_id=call,
    )
    with pytest.raises(PortfolioStateError, match="not its own"):
        _respond(
            portfolio,
            runtime_db,
            idea.idea_id,
            runtime_project,
            role=ReviewerRole.SKEPTIC,
            objections=[(Severity.MAJOR, "x"), (Severity.FATAL, "z")],
            call_id=call,
        )


def test_a_review_from_a_superseded_prompt_is_not_live_and_its_objection_stands(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Reviewer version change: the reading stops counting; the objection does not."""

    idea, _ = seed_idea(portfolio, runtime_project)
    record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.METHODOLOGY,
        verdict=ReviewVerdict.PASS_WITH_OBJECTIONS,
        severity=Severity.FATAL,
        prompt_version="methodology_reviewer@0-superseded",
        objections=[(Severity.FATAL, CLAIM, SAME)],
    )
    assert portfolio.live_reviews(idea_id=idea.idea_id) == ()
    assert [
        str(item.severity) for item in portfolio.open_objections(idea_id=idea.idea_id)
    ] == ["FATAL"]
    assert any("fatal" in item for item in _promising(portfolio, idea.idea_id))


def test_a_stale_review_stops_counting_and_its_fatal_objection_still_blocks(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=ReviewVerdict.REJECT,
        severity=Severity.FATAL,
        objections=[(Severity.FATAL, CLAIM, SAME)],
    )
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(mechanism="reworded, not answered"),
        origin_role="scientific_discovery",
    )
    assert portfolio.live_reviews(idea_id=idea.idea_id) == ()
    assert any("fatal" in item for item in _promising(portfolio, idea.idea_id))


def test_a_meta_review_retry_keeps_each_calls_disagreements_as_its_own(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    ids = [
        record_review(
            portfolio,
            idea_id=idea.idea_id,
            version=1,
            role=ReviewerRole.META,
            verdict=ReviewVerdict.PASS_WITH_OBJECTIONS,
            severity=Severity.MINOR,
            call_id=_call(runtime_db, "meta_reviewer"),
            objections=[(Severity.MINOR, CLAIM, "unresolved disagreement: scope")],
        ).review_id
        for _ in range(2)
    ]
    owners = sorted(
        item.raised_in_review
        for item in portfolio.open_objections(idea_id=idea.idea_id)
    )
    assert owners == sorted(ids)


def test_a_severity_that_is_not_the_worst_objection_is_refused_before_anything_is_written(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    for severity, objections in (
        (Severity.FATAL, []),
        (Severity.MINOR, [(Severity.FATAL, CLAIM, "hidden")]),
        (Severity.NONE, [(Severity.MINOR, CLAIM, "x")]),
    ):
        with pytest.raises(PortfolioStateError, match="worst of its objections"):
            record_review(
                portfolio,
                idea_id=idea.idea_id,
                version=1,
                role=ReviewerRole.SKEPTIC,
                verdict=ReviewVerdict.PASS_WITH_OBJECTIONS,
                severity=severity,
                objections=objections,
            )
    assert portfolio.list_reviews(idea_id=idea.idea_id) == ()


# --------------------------------------------- the database, whatever runs ---
def _raw_review(
    conn: Any, idea_id: str, review_id: str, count: int, severity: str
) -> None:
    conn.execute(
        "insert into idea_reviews (review_id, idea_id, idea_version, reviewer_role, "
        "verdict, severity, summary, reviewed_content_digest, "
        "reviewed_evidence_digest, packet_digest, prompt_version, provider, "
        "provider_family, independence_vs_origin, context_class, objection_count) "
        "select %s, %s, 1, 'skeptic_reviewer', 'PASS_WITH_OBJECTIONS', %s, 's', "
        "v.content_digest, 'e', 'p', 'skeptic_reviewer@1', 'codex', 'openai', "
        "'different_family', 'FROZEN_PACKET', %s from idea_versions v "
        "where v.idea_id = %s and v.version = 1",
        (review_id, idea_id, severity, count, idea_id),
    )


def _raw_objection(
    conn: Any, idea_id: str, review_id: str, ordinal: int, severity: str
) -> None:
    conn.execute(
        "insert into idea_objections (objection_id, idea_id, raised_in_review, "
        "raised_at_version, objection_key, severity, target, summary, ordinal) "
        "values (%s, %s, %s, 1, 'k', %s, 'CLAIM', 'o', %s)",
        (f"IOBJ-{review_id}-{ordinal}", idea_id, review_id, severity, ordinal),
    )


def test_a_review_committed_without_its_objections_is_refused_at_commit(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Two transactions, the old shape: the first one cannot commit."""

    idea, _ = seed_idea(portfolio, runtime_project)
    with (
        pytest.raises(RuntimeDatabaseError, match="one event"),
        runtime_db.tx() as conn,
    ):
        _raw_review(conn, idea.idea_id, "IREV-raw-1", 1, "FATAL")
    with (
        pytest.raises(RuntimeDatabaseError, match="one event"),
        runtime_db.tx() as conn,
    ):
        _raw_review(conn, idea.idea_id, "IREV-raw-2", 2, "FATAL")
        _raw_objection(conn, idea.idea_id, "IREV-raw-2", 0, "FATAL")
    with (
        pytest.raises(RuntimeDatabaseError, match="one event"),
        runtime_db.tx() as conn,
    ):
        # Right count, wrong severity: the FATAL the review claims is not there.
        _raw_review(conn, idea.idea_id, "IREV-raw-3", 1, "FATAL")
        _raw_objection(conn, idea.idea_id, "IREV-raw-3", 0, "MINOR")
    assert portfolio.list_reviews(idea_id=idea.idea_id) == ()
    assert portfolio.open_objections(idea_id=idea.idea_id) == ()

    with runtime_db.tx() as conn:
        _raw_review(conn, idea.idea_id, "IREV-raw-4", 1, "FATAL")
        _raw_objection(conn, idea.idea_id, "IREV-raw-4", 0, "FATAL")
    assert len(portfolio.review_objections("IREV-raw-4")) == 1


def test_what_a_review_and_its_objections_said_cannot_be_rewritten(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    review = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        verdict=ReviewVerdict.REJECT,
        severity=Severity.FATAL,
        objections=[(Severity.FATAL, CLAIM, SAME)],
    )
    (objection,) = portfolio.review_objections(review.review_id)
    for statement, params in (
        (
            "update idea_objections set severity = 'MINOR' where objection_id = %s",
            (objection.objection_id,),
        ),
        (
            ("update idea_objections set summary = 'softened' where objection_id = %s"),
            (objection.objection_id,),
        ),
        (
            "delete from idea_objections where objection_id = %s",
            (objection.objection_id,),
        ),
        (
            "update idea_reviews set verdict = 'PASS' where review_id = %s",
            (review.review_id,),
        ),
    ):
        with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
            conn.execute(statement, params)
    (still,) = portfolio.review_objections(review.review_id)
    assert still.severity is Severity.FATAL and still.summary == SAME


def test_a_legacy_review_whose_objections_may_be_incomplete_is_never_live(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Pre-`sql/0044` rows: kept, standing if objections, never counted."""

    idea, _ = seed_idea(portfolio, runtime_project)
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        _raw_review(conn, idea.idea_id, "IREV-legacy", 0, "FATAL")
        conn.execute(
            "update idea_reviews set objection_count = null where review_id = 'IREV-legacy'"
        )
        conn.execute(
            "update idea_reviews set reviewed_evidence_digest = %s "
            "where review_id = 'IREV-legacy'",
            (portfolio.evidence_digest(idea_id=idea.idea_id, idea_version=1),),
        )
    assert [
        item.review_id for item in portfolio.list_reviews(idea_id=idea.idea_id)
    ] == ["IREV-legacy"]
    assert (
        portfolio.live_reviews(idea_id=idea.idea_id, current_prompt_versions=None) == ()
    )
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        _raw_objection(conn, idea.idea_id, "IREV-legacy", 0, "FATAL")


def test_the_gate_itself_refuses_a_live_review_missing_its_objection(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Defence in depth: the triggers bypassed, the gate still reads the rows.

    ``session_replication_role = replica`` switches the database's checks off,
    which is what a restore, a hand edit or a future bug could do. The gate is
    handed a live FATAL review with no objection and does not count it: the
    idea is not even PROMISING.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        # No objection at all behind a FATAL review ...
        _raw_review(conn, idea.idea_id, "IREV-hollow", 1, "FATAL")
        # ... and a review whose one standing objection has its severity, but
        # which raised two: the worst is on record, a second one is not.
        _raw_review(conn, idea.idea_id, "IREV-short", 2, "FATAL")
        _raw_objection(conn, idea.idea_id, "IREV-short", 0, "FATAL")
        conn.execute(
            "update idea_reviews set reviewed_evidence_digest = %s "
            "where review_id in ('IREV-hollow', 'IREV-short')",
            (portfolio.evidence_digest(idea_id=idea.idea_id, idea_version=1),),
        )
    live = portfolio.live_reviews(idea_id=idea.idea_id, current_prompt_versions=None)
    assert sorted(item.review_id for item in live) == ["IREV-hollow", "IREV-short"]
    snapshot = stages.snapshot_for(portfolio, portfolio.require_idea(idea.idea_id))
    assert snapshot is not None
    unmet = gates._promising_unmet(
        snapshot.version,
        portfolio.open_objections(idea_id=idea.idea_id),
        frozenset({Stage.NOVELTY_SCREEN}),
        live,
    )
    assert any("IREV-hollow" in item and "not counted" in item for item in unmet)
    assert any("IREV-short" in item and "not counted" in item for item in unmet)
    result = gates.evaluate(
        version=snapshot.version,
        live_reviews=live,
        objections=portfolio.open_objections(idea_id=idea.idea_id),
        evidence=(),
        succeeded_stages=frozenset({Stage.NOVELTY_SCREEN}),
        config=load_config(),
        requested=QualityTier.PROMISING,
    )
    assert not result.passed
