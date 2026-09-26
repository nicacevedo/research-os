"""INV-04 and INV-08 -- every review answer has its own record; no failure is masked.

The findings this file closes (final adversarial review of 37e8afe):

- **H4.** Every meta-review was re-asked on an unchanged record: its own
  review joined the META_REVIEW basis, so the basis it was bought against was
  never the basis afterwards. ``record_review`` was idempotent on the binding,
  so the second call's row was dropped by ``on conflict do nothing`` -- and
  its recommendation was applied anyway. VALIDATED stood beside a META review
  that had declined it.
- **M2.** Each board reviewer node overwrote the last one's failure, so a
  board whose methodology reviewer could not be reached was SUCCEEDED and
  moved the idea to REVIEW on two readings.
- **M3.** A re-run reviewer's objections were attached to the first call's
  review, because ``record_review`` returned the existing row.

The repairs: reviews are append-only and keyed by the call (one row per
call, with its attempt, what it supersedes, the owning action and a response
digest); a meta-review's basis excludes its own output; a response is
applied only beside its own row; a board is complete only when every
required role has a live completed review; every live reading of a role must
endorse.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import gates, stages
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ActionStatus,
    Disposition,
    IdeaStatus,
    ReviewerRole,
    ReviewVerdict,
    Severity,
    Stage,
)
from research_os.portfolio.store import PortfolioStateError, PortfolioStore
from research_os.portfolio.track import advance_idea
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import ModelCallStatus
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, record_review, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TerminologyAwareLiterature,
    TwoPathRouter,
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

NOT_YET = {
    "recommendation": "CONTINUE",
    "summary": "not yet: the board's praise is thin and nothing was replicated",
    "unresolved_disagreements": [],
}
PROMOTE = {
    "recommendation": "VALIDATED",
    "summary": "all three reviewers were satisfied",
    "unresolved_disagreements": [],
}


class _Recording(TwoPathRouter):
    """Remembers every call it answered, by role, with its call id."""

    by_role: dict[str, list[str]]
    meta_answers: list[dict[str, Any]]
    down_roles: set[str]

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        role = str(request.role)
        if role in self.down_roles:
            self.down_roles.discard(role)
            raise ProviderCallFailedError(
                "session limit", failure_class=FailureClass.PROVIDER_UNAVAILABLE
            )
        if role == "meta_reviewer" and self.meta_answers:
            self.answers = {**self.answers, "meta_reviewer": self.meta_answers.pop(0)}
        response = super().complete(request)
        self.by_role.setdefault(role, []).append(str(response.call_id))
        return response


def _recording(runtime_db: Database, **overrides: Any) -> _Recording:
    base = _router(runtime_db, **overrides)
    router = _Recording(answers=base.answers, store=RuntimeStore(runtime_db))
    router.by_role = {}
    router.meta_answers = []
    router.down_roles = set()
    return router


def _step(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
    router: Any,
) -> Any:
    return advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=project,
        idea_id=idea_id,
        models=router,
        literature=TerminologyAwareLiterature(),
    )


def _until(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
    router: Any,
    stop: Any,
    *,
    steps: int = 30,
) -> list[Any]:
    results: list[Any] = []
    for _ in range(steps):
        result = _step(runtime_db, pg_dsn, tmp_path, project, idea_id, router)
        results.append(result)
        if stop(result) or result.stage is None or not result.ok:
            break
    return results


def _trace(results: list[Any]) -> str:
    return _why([f"{r.stage} ok={r.ok} {(r.detail or r.reason)[:80]}" for r in results])


def _snapshot(store: PortfolioStore, idea_id: str) -> stages.TrackSnapshot:
    snap = stages.snapshot_for(store, store.require_idea(idea_id))
    assert snap is not None
    return snap


# ------------------------------------------------------------------- H4 -----
def test_a_meta_review_of_an_unchanged_record_is_not_bought_again(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _recording(runtime_db)
    router.meta_answers = [NOT_YET, PROMOTE]
    results = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.META_REVIEW,
    )
    assert results[-1].stage is Stage.META_REVIEW, _trace(results)
    snap = _snapshot(portfolio, idea.idea_id)
    assert Stage.META_REVIEW in snap.basis_stages, (
        "the meta-review's own output moved its basis"
    )
    stage, _reason = stages.select_stage(snap, load_config())
    assert stage is not Stage.META_REVIEW
    # Driving on changes nothing about that: no second synthesis of this record.
    more = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: False,
    )
    assert len(router.by_role.get("meta_reviewer", [])) == 1, _trace(results + more)
    assert portfolio.require_idea(idea.idea_id).status is not IdeaStatus.VALIDATED


def test_every_meta_review_call_has_exactly_one_row_and_the_status_follows_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Neighbour: a genuine re-synthesis after the record changed is its own row."""

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _recording(runtime_db)
    router.meta_answers = [NOT_YET]
    first = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.META_REVIEW,
    )
    # The record changes: a new, supporting literature row joins the version,
    # which stales the board and so the synthesis of it.
    version = portfolio.require_version(idea.idea_id)
    from research_os.portfolio.models import EvidenceKind, EvidenceStrength

    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=version.version,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.CONSISTENT_WITH,
        summary="a further reading",
        literature_key="openalex:W9",
    )
    router.meta_answers = [PROMOTE]
    second = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.META_REVIEW,
    )
    calls = router.by_role.get("meta_reviewer", [])
    assert len(calls) == 2, _trace(first + second)
    metas = [
        item
        for item in portfolio.list_reviews(idea_id=idea.idea_id)
        if item.reviewer_role is ReviewerRole.META
    ]
    assert sorted(item.call_id for item in metas) == sorted(calls)
    assert all(item.action_id and item.response_digest for item in metas)
    assert len({item.reviewed_evidence_digest for item in metas}) == 2
    live = [
        item
        for item in portfolio.live_reviews(idea_id=idea.idea_id)
        if item.reviewer_role is ReviewerRole.META
    ]
    assert [item.recommendation for item in live] == [Disposition.VALIDATED]
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.VALIDATED


def test_a_recommendation_is_not_applied_beside_another_calls_row(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen store's behaviour, reinstated as a fault: the answer must not land."""

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _recording(runtime_db, meta_reviewer=PROMOTE)
    results = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: (
            _snapshot(portfolio, idea.idea_id).missing_review_roles == ()
            and r.stage is Stage.REVIEW_BOARD
        ),
    )
    assert results[-1].stage is Stage.REVIEW_BOARD, _trace(results)
    board = portfolio.live_reviews(idea_id=idea.idea_id)
    impostor = board[0]
    real = PortfolioStore.record_review

    def returns_someone_elses_row(self: PortfolioStore, **kwargs: Any) -> Any:
        if kwargs.get("reviewer_role") is ReviewerRole.META:
            return impostor, False
        return real(self, **kwargs)

    monkeypatch.setattr(PortfolioStore, "record_review", returns_someone_elses_row)
    before = portfolio.require_idea(idea.idea_id).status
    with pytest.raises(PortfolioStateError, match="no record of its own"):
        _step(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
    assert portfolio.require_idea(idea.idea_id).status is before
    (meta_action,) = [
        a
        for a in portfolio.list_actions(idea_id=idea.idea_id)
        if a.stage is Stage.META_REVIEW
    ]
    assert meta_action.status is ActionStatus.FAILED


# ------------------------------------------------------------------- M2 -----
@pytest.mark.parametrize(
    "down",
    ["methodology_reviewer", "novelty_reviewer", "skeptic_reviewer"],
)
def test_a_board_missing_any_reviewer_is_not_complete_and_says_which(
    down: str,
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _recording(runtime_db)
    router.down_roles = {down}
    results = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.REVIEW_BOARD,
    )
    board = results[-1]
    assert board.stage is Stage.REVIEW_BOARD, _trace(results)
    assert not board.ok
    assert board.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert down in board.detail and "incomplete" in board.detail
    assert portfolio.require_idea(idea.idea_id).status is not IdeaStatus.REVIEW
    state = stages.board_state(_snapshot(portfolio, idea.idea_id))
    missing = {role for role, value in state.items() if value == "MISSING"}
    assert {str(role) for role in missing} == {down}
    (action,) = [
        a
        for a in portfolio.list_actions(idea_id=idea.idea_id)
        if a.stage is Stage.REVIEW_BOARD
    ]
    assert action.status is ActionStatus.FAILED

    # The retry asks only the missing role, and then the board is complete.
    asked_before = {role: len(calls) for role, calls in router.by_role.items()}
    retry = _step(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
    assert retry.stage is Stage.REVIEW_BOARD and retry.ok, retry.detail
    asked = {
        role: len(calls) - asked_before.get(role, 0)
        for role, calls in router.by_role.items()
        if len(calls) - asked_before.get(role, 0)
    }
    assert asked == {down: 1}
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.REVIEW


def test_a_reviewer_answering_with_a_superseded_prompt_does_not_complete_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review that is not live is not board evidence, whatever else succeeded."""

    from research_os.portfolio import prompts as pprompts

    monkeypatch.setitem(
        pprompts.CURRENT_REVIEW_PROMPTS, "skeptic_reviewer", "skeptic_reviewer@99"
    )
    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    router = _recording(runtime_db)
    results = _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.REVIEW_BOARD,
    )
    board = results[-1]
    assert board.stage is Stage.REVIEW_BOARD and not board.ok, _trace(results)
    assert "skeptic_reviewer" in board.detail
    assert portfolio.require_idea(idea.idea_id).status is not IdeaStatus.REVIEW


def test_one_endorsement_does_not_mask_another_reading_of_the_same_role(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    store = RuntimeStore(runtime_db)

    def call() -> str:
        return store.record_model_call(
            provider="codex", role="methodology_reviewer", status=ModelCallStatus.OK
        ).call_id

    revise = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.METHODOLOGY,
        verdict=ReviewVerdict.REVISE,
        severity=Severity.MAJOR,
        call_id=call(),
    )
    endorse = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.METHODOLOGY,
        verdict=ReviewVerdict.PASS,
        call_id=call(),
    )
    assert endorse.supersedes_review_id == revise.review_id
    unmet = gates._validated_unmet(
        portfolio.require_version(idea.idea_id),
        portfolio.live_reviews(idea_id=idea.idea_id),
        (),
        (),
        frozenset(),
        load_config(),
    )
    assert any("methodology_reviewer did not endorse" in item for item in unmet), unmet


# ------------------------------------------------------------------- M3 -----
class _ObjectsOnce(TwoPathRouter):
    """A methodologist that raises a different objection each time it is asked."""

    asked: int = 0

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "methodology_reviewer":
            type(self).asked += 1
            self.answers = {
                **self.answers,
                "methodology_reviewer": {
                    "verdict": "PASS_WITH_OBJECTIONS",
                    "summary": f"reading {type(self).asked}",
                    "objections": [
                        {
                            "severity": "MINOR",
                            "target": "CLAIM",
                            "summary": f"objection from reading {type(self).asked}",
                        }
                    ],
                },
            }
        return super().complete(request)


def test_a_rerun_reviewers_objections_attach_to_its_own_review(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The methodologist's review ages out; it is asked again on the same binding."""

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _router(runtime_db)
    _ObjectsOnce.asked = 0
    router = _ObjectsOnce(answers=base.answers, store=base.store)
    _until(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        lambda r: r.stage is Stage.REVIEW_BOARD,
    )
    (first,) = [
        r
        for r in portfolio.list_reviews(idea_id=idea.idea_id)
        if r.reviewer_role is ReviewerRole.METHODOLOGY
    ]
    age = load_config().thresholds.review_max_age_seconds
    with runtime_db.tx() as conn:
        conn.execute(
            # Seconds, not days: a day-valued interval is calendar arithmetic
            # in the session's time zone and loses an hour across a DST change.
            "update idea_reviews set created_at = created_at - "
            "make_interval(secs => %s) where review_id = %s",
            (float(age + 3_600), first.review_id),
        )
    assert (
        stages.board_state(_snapshot(portfolio, idea.idea_id))[ReviewerRole.METHODOLOGY]
        == "MISSING"
    )
    rerun = _step(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
    assert rerun.stage is Stage.REVIEW_BOARD and rerun.ok, rerun.detail
    reviews = [
        r
        for r in portfolio.list_reviews(idea_id=idea.idea_id)
        if r.reviewer_role is ReviewerRole.METHODOLOGY
    ]
    assert len(reviews) == 2, "the re-run was folded into the first call's row"
    second = next(r for r in reviews if r.review_id != first.review_id)
    assert second.call_id != first.call_id
    assert (second.attempt, second.supersedes_review_id) == (2, first.review_id)
    by_summary = {o.summary: o for o in portfolio.open_objections(idea_id=idea.idea_id)}
    assert by_summary["objection from reading 1"].raised_in_review == first.review_id
    assert by_summary["objection from reading 2"].raised_in_review == second.review_id


def test_a_replay_of_one_call_is_one_review_and_one_set_of_objections(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The replay a resumed node performs stays free: same call, same row."""

    idea, _ = seed_idea(portfolio, runtime_project)
    call = (
        RuntimeStore(runtime_db)
        .record_model_call(
            provider="codex", role="skeptic_reviewer", status=ModelCallStatus.OK
        )
        .call_id
    )
    one = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        call_id=call,
    )
    again = record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=ReviewerRole.SKEPTIC,
        call_id=call,
    )
    assert one.review_id == again.review_id
    assert re.match(r"^IREV-", one.review_id)
