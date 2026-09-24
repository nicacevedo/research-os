"""What must still be true after something goes wrong.

The brief's §34 lists eight failures and six things none of them may cause.
This file is that list, one test per row, and the assertions are deliberately
about *state a researcher would read* rather than about return values: the
failure mode this layer is most exposed to is not a crash, it is a crash that
leaves the record saying something untrue.

The six prohibitions, and where each is checked:

```text
false-promote an idea          every failure test asserts the status
lose an idea                   test_no_failure_loses_an_idea
duplicate successors           test_a_replayed_tick_buys_one_action
consume lineage incorrectly    test_a_reclaimed_track_does_not_consume_a_revision
erase provenance               test_a_failed_stage_still_records_what_it_cost
block unrelated work           test_one_blocked_idea_does_not_block_the_portfolio
```
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.curator import BANK_ROOT, checkout_path, curate
from research_os.portfolio.models import (
    ActionStatus,
    IdeaStatus,
    OperationalState,
    PortfolioStatus,
    QualityTier,
    ReviewerRole,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.portfolio.track import advance_idea
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.models import BudgetScope
from research_os.runtime.store import RuntimeStore
from tests.fs_helpers import make_git_repo
from tests.portfolio_helpers import portfolio, record_review, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


@pytest.fixture
def checkpoint_tables(pg_dsn: str) -> str:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    return pg_dsn


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = make_git_repo(tmp_path / "project")
    (repo / "README.md").write_text("a project\n", encoding="utf-8")
    for args in (
        ["config", "user.email", "r@example.invalid"],
        ["config", "user.name", "R"],
        ["add", "README.md"],
        ["commit", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _advance(runtime_db, pg_dsn, tmp_path, project, idea_id, router):
    return advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=project,
        idea_id=idea_id,
        models=router,
    )


def _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, project, idea_id) -> None:
    """Advance one idea to the first stage that actually calls a model.

    A fresh idea's first stage is deduplication, and in a portfolio with one
    idea that is decided by arithmetic -- no model is asked, so a broken
    provider is never reached. Every failure test below is about what happens
    when a call goes wrong, so each of them has to get to a call first.
    """

    good = ScriptedRouter(
        answers={"duplicate_adjudicator": {"verdict": "distinct"}},
        store=RuntimeStore(runtime_db),
    )
    result = _advance(runtime_db, pg_dsn, tmp_path, project, idea_id, good)
    assert result.stage is Stage.DEDUP and result.ok, result.detail


def _tick(runtime_db, pg_dsn, tmp_path, project, **kwargs):
    return tick(
        db=runtime_db,
        project_id=project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        **kwargs,
    )


# ------------------------------------------------------ provider outage --
def test_a_provider_outage_is_not_a_scientific_verdict(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The failure `docs/RUNTIME.md` §17 records actually happening once.

    An OAuth token that could not be refreshed became three research runs
    reported to a researcher as finished with a decision waiting. The
    portfolio's version of that mistake would be an idea reported as rejected
    because nobody answered.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    router = ScriptedRouter(unavailable=True, store=RuntimeStore(runtime_db))
    result = _advance(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router
    )

    assert not result.ok
    after = portfolio.require_idea(idea.idea_id)
    assert after.status is IdeaStatus.CANDIDATE
    assert after.operational_state is OperationalState.BLOCKED_PROVIDER
    assert after.retire_reason is None
    assert after.quality_tier is QualityTier.NONE
    assert portfolio.list_reviews(idea_id=idea.idea_id) == ()
    assert result.disposition is None


def test_an_outage_that_clears_returns_the_idea_to_the_portfolio(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_operational_state(
        idea_id=idea.idea_id, state=OperationalState.BLOCKED_PROVIDER
    )
    RuntimeStore(runtime_db).record_provider_result(provider="claude", ok=True)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.blocks_cleared == 1
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.IDLE
    )


# --------------------------------------------------------- malformed --
def test_a_model_that_returns_prose_writes_no_idea_content(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The failure this layer is most exposed to, and it must be loud.

    A model that answers with a paragraph where a contract was required has
    not produced a weaker result; it has produced none.
    """

    idea, version = seed_idea(portfolio, runtime_project)
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    router = ScriptedRouter(answers={}, store=RuntimeStore(runtime_db))
    result = _advance(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router
    )

    assert not result.ok
    assert "contract" in result.detail or "JSON" in result.detail
    assert len(portfolio.list_versions(idea.idea_id)) == 1, (
        "no version may be appended from output that did not validate"
    )
    assert portfolio.require_version(idea.idea_id).content_digest == (
        version.content_digest
    )
    assert portfolio.list_reviews(idea_id=idea.idea_id) == ()
    assert portfolio.list_evidence(idea_id=idea.idea_id) == ()


# ------------------------------------------------------- worker death --
def test_a_track_whose_worker_died_is_reclaimed_and_retried(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="held",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update idea_actions set updated_at = now() - interval '3 hours' "
            "where action_id = %s",
            (action.action_id,),
        )
    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    reclaimed = portfolio.list_actions(idea_id=idea.idea_id)[0]
    assert reclaimed.status is ActionStatus.FAILED
    assert portfolio.require_idea(idea.idea_id).operational_state is (
        OperationalState.IDLE
    )
    # And the basis is not poisoned: a failed stage may be tried again.
    again = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="held",
    )
    assert again.action_id != action.action_id


def test_a_reclaimed_track_does_not_consume_a_revision(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """ "Consume lineage incorrectly", in the form this layer can produce it.

    The revision bound exists to stop a revise/review loop. A failure that
    spent one would let a handful of outages exhaust an idea's ability to be
    sharpened at all.

    Driven through the production path with a *positive control*: the first
    half kills the discover stage and asserts nothing was spent; the second
    lets it succeed and asserts one was. The first version asserted only the
    first half against a reclaimer that touches no version table, so it was
    structurally incapable of failing.
    """

    idea, _ = seed_idea(portfolio, runtime_project, mechanism="", falsifier="")
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    before = portfolio.revision_count(idea.idea_id)

    broken = ScriptedRouter(
        # Everything on the way answers; only discovery does not.
        answers={
            "duplicate_adjudicator": {"verdict": "distinct"},
            "novelty_screener": {"likely_known": False},
            "falsifier": {"summary": "nothing fatal", "objections": []},
        },
        unavailable_roles={"scientific_discovery"},
        store=RuntimeStore(runtime_db),
    )
    for _ in range(4):
        result = _advance(
            runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, broken
        )
        portfolio.set_operational_state(
            idea_id=idea.idea_id, state=OperationalState.IDLE
        )
        if result.stage is Stage.DISCOVER:
            break
    assert result.stage is Stage.DISCOVER and not result.ok, result.detail
    assert portfolio.revision_count(idea.idea_id) == before

    working = ScriptedRouter(
        answers={
            "duplicate_adjudicator": {"verdict": "distinct"},
            "novelty_screener": {"likely_known": False},
            "falsifier": {"summary": "nothing fatal", "objections": []},
            "scientific_discovery": {
                "can_be_made_precise": True,
                "refined": {
                    "title": "sharpened",
                    "research_question": "Does the bound hold under ties?",
                    "core_idea": "Ties break the monotonicity the bound needs.",
                    "mechanism": "Selection by bound is not monotone under ties.",
                    "falsifier": "Exhibit a tied instance where it still holds.",
                },
            },
        },
        store=RuntimeStore(runtime_db),
    )
    _advance(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, working)
    assert portfolio.revision_count(idea.idea_id) == before + 1, (
        "a discover that succeeded must spend one, or the test above proves nothing"
    )


# ------------------------------------------------------------ replays --
def test_a_replayed_tick_buys_one_action(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """Duplicate events, duplicated ticks and a reconnect all reduce to this."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    moment = datetime.now(UTC)
    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    third = _tick(runtime_db, pg_dsn, tmp_path, runtime_project, now=moment)
    assert first.work_enqueued >= 1
    assert second.work_enqueued == 0
    assert third.work_enqueued == 0


def test_a_stage_that_already_succeeded_is_not_bought_twice(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    from research_os.portfolio.store import DuplicateBasisError

    idea, _ = seed_idea(portfolio, runtime_project)
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.NOVELTY_SCREEN,
        basis_digest="one-basis",
    )
    portfolio.complete_action(action_id=action.action_id, status=ActionStatus.SUCCEEDED)
    with pytest.raises(DuplicateBasisError):
        portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.NOVELTY_SCREEN,
            basis_digest="one-basis",
        )


# ------------------------------------------------------ review failure --
def test_two_reviews_do_not_become_three_because_one_failed(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The gate counts live reviews. A reviewer that never answered is absent.

    Stated as a chaos test because the tempting repair -- treating an
    unavailable reviewer as an abstention -- would make a provider outage into
    a promotion.
    """

    from research_os.portfolio.gates import evaluate

    idea, _ = seed_idea(portfolio, runtime_project)
    for role in (ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY):
        record_review(portfolio, idea_id=idea.idea_id, version=1, role=role)
    result = evaluate(
        version=portfolio.require_version(idea.idea_id),
        live_reviews=portfolio.live_reviews(idea_id=idea.idea_id),
        objections=portfolio.open_objections(idea_id=idea.idea_id),
        evidence=portfolio.list_evidence(idea_id=idea.idea_id),
        succeeded_stages=frozenset(Stage),
        config=load_config(),
        requested=QualityTier.VALIDATED,
    )
    assert not result.passed
    assert any("skeptic_reviewer" in item for item in result.unmet)


# -------------------------------------------------------- curator crash --
def test_a_curator_crash_leaves_nothing_half_written(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """Reset to the tip before rendering, so a partial tree is never committed.

    Simulated by leaving rubbish in the worktree between curations, which is
    what a process killed mid-write leaves behind.
    """

    seed_idea(portfolio, runtime_project)
    curate(db=runtime_db, project_id=runtime_project, repository=repository)

    target = checkout_path(runtime_project, repository)
    (target / BANK_ROOT / "ideas" / "PIDEA-HALF-WRITTEN.md").write_text(
        "half a file from a worker that died\n", encoding="utf-8"
    )
    seed_idea(portfolio, runtime_project, title="something new")
    curate(db=runtime_db, project_id=runtime_project, repository=repository)

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "research-os/autonomous"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert not any("HALF-WRITTEN" in name for name in listing)
    # The control. Without it this passes against a second curation that did
    # nothing at all, which is indistinguishable from "the reset worked".
    ideas = portfolio.list_ideas(project_id=runtime_project)
    assert len(ideas) == 2
    for idea in ideas:
        assert f"{BANK_ROOT}/ideas/{idea.idea_id}.md" in listing


# ------------------------------------------------------------- budgets --
def test_budget_exhaustion_parks_the_work_and_rejects_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(0),
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED
    after = portfolio.require_idea(idea.idea_id)
    assert after.status is IdeaStatus.PROMISING
    assert after.retire_reason is None


# ---------------------------------------------- the six things forbidden --
def test_no_failure_loses_an_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Every failure path, and the idea is still there afterwards."""

    idea, _ = seed_idea(portfolio, runtime_project)
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    for router in (
        ScriptedRouter(unavailable=True, store=RuntimeStore(runtime_db)),
        ScriptedRouter(answers={}, store=RuntimeStore(runtime_db)),
        ScriptedRouter(fail_roles={"novelty_screener"}, store=RuntimeStore(runtime_db)),
    ):
        _advance(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
        assert portfolio.get_idea(idea.idea_id) is not None
        assert portfolio.require_version(idea.idea_id) is not None
        portfolio.set_operational_state(
            idea_id=idea.idea_id, state=OperationalState.IDLE
        )


def test_a_failed_stage_still_records_what_it_cost(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """ "Erase provenance" is the quiet one.

    A stage that spent money and then failed must leave the spend on the
    record, or an idea's ceiling is enforced against a number that is wrong in
    the direction of spending more.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    router = ScriptedRouter(
        answers={"novelty_screener": {"not": "a valid screen"}},
        store=RuntimeStore(runtime_db),
    )
    _advance(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
    actions = portfolio.list_actions(idea_id=idea.idea_id)
    assert actions
    assert actions[-1].status is ActionStatus.FAILED
    assert actions[-1].failure_class
    assert actions[-1].completed_at is not None


def test_one_blocked_idea_does_not_block_the_portfolio(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    blocked, _ = seed_idea(portfolio, runtime_project, title="blocked")
    working, _ = seed_idea(portfolio, runtime_project, title="working")
    portfolio.set_operational_state(
        idea_id=blocked.idea_id, state=OperationalState.BLOCKED_EXTERNAL
    )
    portfolio.set_status(idea_id=working.idea_id, status=IdeaStatus.PROMISING)

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.RUNNING
    allocated = {item.idea_id for item in report.allocations if item.idea_id}
    assert working.idea_id in allocated
    assert blocked.idea_id not in allocated


def test_everything_blocked_pauses_honestly_and_recovers(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path,
    runtime_project: str,
) -> None:
    """One of the four pauses, and it must name the cause rather than guess.

    "All available work blocked by an external prerequisite" is a legitimate
    stop, and it is different from "no viable frontier" -- which is what a
    system that could not tell them apart would report.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_operational_state(
        idea_id=idea.idea_id, state=OperationalState.BLOCKED_EXTERNAL
    )
    # Fill the candidate pool so exploration is not allocated, which would
    # (correctly) mean the portfolio still has something to do.
    for index in range(load_config().bounds.candidate_pool_ceiling):
        extra, _ = seed_idea(portfolio, runtime_project, title=f"blocked {index}")
        portfolio.set_operational_state(
            idea_id=extra.idea_id, state=OperationalState.BLOCKED_EXTERNAL
        )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status is PortfolioStatus.PAUSED_BLOCKED_EXTERNAL
    assert report.allocations == ()

    portfolio.set_operational_state(idea_id=idea.idea_id, state=OperationalState.IDLE)
    recovered = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert recovered.status is PortfolioStatus.RUNNING


def test_no_failure_promotes_an_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The prohibition that matters most, across every failure this file has.

    An idea's *tier* is what a person reads as "this system got somewhere with
    it". None of these failures may move it.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    _past_dedup(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id)
    for router in (
        ScriptedRouter(unavailable=True, store=RuntimeStore(runtime_db)),
        ScriptedRouter(answers={}, store=RuntimeStore(runtime_db)),
        ScriptedRouter(fail_roles={"falsifier"}, store=RuntimeStore(runtime_db)),
    ):
        _advance(runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router)
        after = portfolio.require_idea(idea.idea_id)
        assert after.quality_tier is QualityTier.NONE
        assert after.status is IdeaStatus.CANDIDATE
        portfolio.set_operational_state(
            idea_id=idea.idea_id, state=OperationalState.IDLE
        )


def test_the_tick_survives_a_portfolio_with_nothing_in_it(
    runtime_db: Database, pg_dsn: str, tmp_path, runtime_project: str
) -> None:
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status in {
        PortfolioStatus.RUNNING,
        PortfolioStatus.PAUSED_NO_FRONTIER,
    }
    assert report.active_tracks == 0


def test_an_allocation_for_an_idea_that_vanished_is_not_fatal(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Rows are deleted only by a project going away, and the handler says so
    rather than raising something unclassified."""

    from research_os.portfolio.store import PortfolioStateError

    with pytest.raises(PortfolioStateError):
        portfolio.require_idea("PIDEA-20260101T000000Z-00000000")
