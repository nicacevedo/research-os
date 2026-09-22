"""One idea, from a candidate to something a researcher is handed.

The audit that prompted this file put it plainly: every other test asserts a
*negation*. Replacing the body of `_apply_disposition` with `return` left the
whole suite green, and the `CANDIDATE -> PROMISING` transition was never
exercised at all -- so "this machine can promote an idea" was unproven as unit,
integration and production evidence simultaneously.

It drives a literature-adjudicated idea, whose evidence is retrieved sources
and whose replication is a second search with different words. When this file
was written that was the only route this build could complete. It no longer
is: §19 wired the empirical route, and
`tests/test_portfolio_empirical.py::test_a_real_empirical_idea_traverses_experiment_evidence_and_review`
is a positive control that drives it to HUMAN_READY with a real subprocess.
The mathematical route still needs an executed check and remains unwired --
`run_evidence` refuses it, by §18.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    AdjudicationType,
    Disposition,
    EvidenceKind,
    IdeaStatus,
    QualityTier,
    ReviewerRole,
    Severity,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.track import advance_idea
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

LITERATURE_FALSIFIER = (
    "Find a prior publication that already reports this; search the literature "
    "for the same result under other terminology."
)


@pytest.fixture
def checkpoint_tables(pg_dsn: str) -> str:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    return pg_dsn


# ------------------------------------------------------------- doubles --
@dataclass(frozen=True, slots=True)
class _Work:
    key: str
    title: str
    abstract: str


@dataclass(frozen=True, slots=True)
class _Entry:
    work: _Work
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class _Packet:
    query: str
    entries: tuple[_Entry, ...]

    @property
    def work_keys(self) -> tuple[str, ...]:
        return tuple(item.work.key for item in self.entries)


class TerminologyAwareLiterature:
    """A corpus that answers different words with different papers.

    Which is the whole point of a second terminology path: a literature source
    that returned the same three works whatever it was asked could not
    distinguish "we looked again" from "we looked again the same way", and a
    replication rule tested against one would be testing nothing.
    """

    FIRST = ("openalex:W1", "openalex:W2", "openalex:W3")
    SECOND = ("openalex:W4", "openalex:W5", "openalex:W6")

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        self.queries.append(query)
        # Keyed on the words, not on a call counter. The cheap screen searches
        # too, so a counter made "which pass is this" depend on how many other
        # stages had run -- and the point of the double is that *different
        # words* find different papers.
        # The replication path searches on the core idea and the claimed
        # difference; the audit searches on the research question. Keyed on a
        # word only the second one carries.
        keys = self.SECOND if "terminolog" in query.lower() else self.FIRST
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in keys[:limit]
            ),
        )


def _matrix(keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        "rows": [
            {
                "proposed_component": f"component {index}",
                "closest_known_result": f"the nearest result to {key}",
                "relation": "different",
                "precise_difference": "trajectories rather than optima",
                "source_key": key,
                "confidence": 0.8,
            }
            for index, key in enumerate(keys)
        ],
        "queries": ["first terminology", "second terminology"],
        "summary": "no prior result compares trajectories",
    }


class TwoPathRouter(ScriptedRouter):
    """A literature scout that cites what it was actually shown.

    Reads the keys out of its own prompt rather than being told which pass it
    is on. That is both more realistic -- a scout cites the packet it was
    handed -- and the only way to satisfy the fail-closed citation check,
    which invalidates a whole report that names a work nobody retrieved.
    """

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            keys = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt)))
            self.answers = {**self.answers, "literature_scout": _matrix(keys)}
        return super().complete(request)


def _review(who: str) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "summary": f"{who} found nothing to object to",
        "objections": [],
    }


def _router(runtime_db: Database, **overrides: Any) -> TwoPathRouter:
    answers: dict[str, Any] = {
        "duplicate_adjudicator": {"verdict": "distinct", "rationale": "different"},
        "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
        "falsifier": {
            "summary": "no cheap kill found",
            "objections": [],
            "attempted": ["a subsuming theorem", "a simpler explanation"],
        },
        "scientific_discovery": {
            "can_be_made_precise": True,
            "minimum_decisive_action": "search for the closest prior result",
            "refined": {
                **{
                    key: value
                    for key, value in idea_fields().items()
                    if key != "adjudication_types"
                },
                "research_question": ("Has this comparison already been published?"),
                "core_idea": ("Searching under other terminology may already find it."),
                "claimed_difference": "other terminology was not searched",
                "falsifier": LITERATURE_FALSIFIER,
            },
        },
        "methodology_reviewer": _review("the methodologist"),
        "novelty_reviewer": _review("the novelty reviewer"),
        "skeptic_reviewer": _review("the skeptic"),
        "meta_reviewer": {
            "recommendation": "HUMAN_READY",
            "summary": "all three reviewers were satisfied",
            "unresolved_disagreements": [],
        },
        "brancher": {"children": [], "relations": []},
    }
    answers.update(overrides)
    return TwoPathRouter(answers=answers, store=RuntimeStore(runtime_db))


def _drive(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
    router: ScriptedRouter,
    literature: TerminologyAwareLiterature,
    *,
    steps: int = 30,
) -> list[str]:
    trace: list[str] = []
    for _ in range(steps):
        result = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=project,
            idea_id=idea_id,
            models=router,
            literature=literature,
        )
        trace.append(
            f"{result.stage} ok={result.ok} {(result.detail or result.reason)[:70]}"
        )
        if result.stage is None:
            break
        if not result.ok:
            break
    return trace


def _why(trace: list[str]) -> str:
    return "\n  " + "\n  ".join(trace)


# ------------------------------------------------------- the positive --
def test_an_idea_is_driven_from_candidate_to_human_ready(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The whole machine, in the direction nothing else tested.

    Every status transition below is performed by production code: the gate
    grants PROMISING from rows, the audit moves it to INVESTIGATING, the board
    moves it to REVIEW, and the meta-review's recommendation is lowered or
    allowed by `gates.permit` -- which is the only writer of VALIDATED and
    HUMAN_READY in the system.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(runtime_db)
    literature = TerminologyAwareLiterature()
    trace = _drive(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router, literature
    )

    final = portfolio.require_idea(idea.idea_id)
    assert final.status is IdeaStatus.HUMAN_READY, _why(trace)
    assert final.quality_tier is QualityTier.HUMAN_READY, _why(trace)

    # And it got there for the right reasons, not by a shortcut.
    version = portfolio.require_version(idea.idea_id)
    assert AdjudicationType.NOVELTY_OR_LITERATURE in version.adjudication_types
    live = portfolio.live_reviews(idea_id=idea.idea_id)
    assert {item.reviewer_role for item in live} >= {
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    }
    keys = {
        item.literature_key
        for item in portfolio.list_evidence(idea_id=idea.idea_id)
        if item.kind is EvidenceKind.LITERATURE
    }
    assert keys >= set(TerminologyAwareLiterature.FIRST)
    assert keys & set(TerminologyAwareLiterature.SECOND), (
        "the second terminology path must have found something the first did not"
    )
    assert portfolio.open_objections(idea_id=idea.idea_id) == ()


def test_every_status_on_the_way_is_written_by_production_code(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """No `set_status` from the test. The sequence is what the machine did."""

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(runtime_db)
    literature = TerminologyAwareLiterature()

    seen: list[IdeaStatus] = [portfolio.require_idea(idea.idea_id).status]
    for _ in range(30):
        result = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=literature,
        )
        status = portfolio.require_idea(idea.idea_id).status
        if status is not seen[-1]:
            seen.append(status)
        if result.stage is None or not result.ok:
            break

    assert seen == [
        IdeaStatus.CANDIDATE,
        IdeaStatus.PROMISING,
        IdeaStatus.INVESTIGATING,
        IdeaStatus.REVIEW,
        IdeaStatus.VALIDATED,
        IdeaStatus.HUMAN_READY,
    ], seen


def test_the_gate_holds_the_line_when_a_requirement_is_missing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The control for the control.

    The same drive, with the skeptic refusing to endorse. The meta-reviewer
    still recommends HUMAN_READY every time; the idea must not get there, and
    must not get to VALIDATED either.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(
        runtime_db,
        skeptic_reviewer={
            "verdict": "REVISE",
            "summary": "the comparison is not like for like",
            "objections": [{"severity": "CRITICAL", "summary": "the baselines differ"}],
        },
    )
    literature = TerminologyAwareLiterature()
    trace = _drive(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router, literature
    )

    final = portfolio.require_idea(idea.idea_id)
    assert final.status not in {IdeaStatus.VALIDATED, IdeaStatus.HUMAN_READY}, _why(
        trace
    )
    assert final.quality_tier in {QualityTier.NONE, QualityTier.PROMISING}
    standing = portfolio.open_objections(idea_id=idea.idea_id)
    assert any(item.severity is Severity.CRITICAL for item in standing)


def test_a_fatal_objection_still_ends_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(
        runtime_db,
        falsifier={
            "summary": "already known",
            "objections": [
                {"severity": "FATAL", "summary": "a 2013 theorem states this"}
            ],
            "attempted": ["a literature check"],
        },
    )
    _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        TerminologyAwareLiterature(),
    )
    final = portfolio.require_idea(idea.idea_id)
    assert final.status is IdeaStatus.REJECTED
    assert "2013 theorem" in (final.retire_reason or "")


# --------------------------------------------- objections, end to end --
def test_an_objection_is_closed_by_a_revision_and_a_second_opinion(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The production path for closing an objection, which had no test.

    A skeptic raises a critical objection; the track revises in answer to it;
    the board re-reviews the new version; and the objection closes -- resolved
    by a review of a *different role*, which is the rule
    `resolve_objection` enforces and which nothing had exercised end to end.
    A `_try_resolve_objections` that always returned zero passed the suite.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(
        runtime_db,
        skeptic_reviewer={
            "verdict": "PASS_WITH_OBJECTIONS",
            "summary": "one thing to fix",
            "objections": [{"severity": "CRITICAL", "summary": "the baselines differ"}],
        },
    )
    literature = TerminologyAwareLiterature()
    _drive(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router, literature
    )

    raised = portfolio.open_objections(idea_id=idea.idea_id)
    assert any("baselines differ" in item.summary for item in raised), (
        "the skeptic's objection must be standing before anything answers it"
    )

    # The skeptic is satisfied by the revision; everything else is unchanged.
    router.answers = {
        **router.answers,
        "skeptic_reviewer": _review("the skeptic, on reflection"),
    }
    trace = _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        TerminologyAwareLiterature(),
    )

    closed = [
        item
        for item in portfolio.list_reviews(idea_id=idea.idea_id)
        if item.reviewer_role is ReviewerRole.SKEPTIC
    ]
    assert len(closed) >= 2, _why(trace)
    standing = portfolio.open_objections(
        idea_id=idea.idea_id, minimum=Severity.CRITICAL
    )
    assert standing == (), (
        f"the objection was answered and re-reviewed and is still standing: {trace}"
    )


def test_an_objection_nobody_answered_is_still_standing_after_a_reword(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The control for the test above.

    If revising were enough on its own, the previous test would pass for the
    wrong reason. Here the skeptic keeps objecting, and the objection keeps
    standing however many times the idea is rewritten.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(
        runtime_db,
        skeptic_reviewer={
            "verdict": "PASS_WITH_OBJECTIONS",
            "summary": "still wrong",
            "objections": [{"severity": "CRITICAL", "summary": "the baselines differ"}],
        },
    )
    for _ in range(3):
        _drive(
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea.idea_id,
            router,
            TerminologyAwareLiterature(),
            steps=12,
        )
    assert portfolio.open_objections(idea_id=idea.idea_id, minimum=Severity.CRITICAL)
    assert portfolio.require_idea(idea.idea_id).status is not IdeaStatus.VALIDATED


def test_the_meta_reviewers_recommendation_is_recorded_beside_what_was_allowed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """A divergence between what was recommended and what happened is evidence.

    A researcher asking "why is this only VALIDATED when the synthesis said
    otherwise" should find the answer on the row rather than in a log.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(runtime_db)
    _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        TerminologyAwareLiterature(),
    )
    metas = [
        item
        for item in portfolio.list_reviews(idea_id=idea.idea_id)
        if item.reviewer_role is ReviewerRole.META
    ]
    assert metas
    assert all(item.recommendation is Disposition.HUMAN_READY for item in metas)

    actions = [
        item
        for item in portfolio.list_actions(idea_id=idea.idea_id)
        if item.stage is Stage.META_REVIEW
    ]
    assert actions
    assert actions[0].disposition is not None
    assert actions[0].detail and "gate permits" in actions[0].detail


def test_reviews_are_bound_to_the_version_that_was_reviewed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """After the drive, no review of a superseded version counts."""

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        _router(runtime_db),
        TerminologyAwareLiterature(),
    )
    current = portfolio.require_idea(idea.idea_id).current_version
    for review in portfolio.live_reviews(idea_id=idea.idea_id):
        assert review.idea_version == current
        assert review.reviewed_content_digest == (
            portfolio.require_version(idea.idea_id).content_digest
        )


def test_a_prompt_version_bump_stales_the_board_and_the_track_recovers(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The livelock the audit predicted, driven through the production path.

    A superseded prompt makes a review stale. `select_stage` then demands the
    board again -- and if `_basis_for` still counted the stale review, the
    basis would be the one the board already ran against and `open_action`
    would refuse it as a duplicate, forever. Both must read liveness the same
    way.
    """

    from research_os.portfolio import prompts as pprompts

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _router(runtime_db)
    _drive(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        TerminologyAwareLiterature(),
    )
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.HUMAN_READY

    monkeypatch.setitem(
        pprompts.CURRENT_REVIEW_PROMPTS, "skeptic_reviewer", "skeptic_reviewer@2"
    )
    assert not any(
        item.reviewer_role is ReviewerRole.SKEPTIC
        for item in portfolio.live_reviews(idea_id=idea.idea_id)
    )

    result = advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=runtime_project,
        idea_id=idea.idea_id,
        models=router,
        literature=TerminologyAwareLiterature(),
    )
    # HUMAN_READY is terminal for allocation, so the track does no further
    # work -- but it must say so rather than refusing itself as a duplicate.
    assert result.ok
    assert "another pass" not in result.detail
