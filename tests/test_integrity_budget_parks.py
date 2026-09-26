"""M4 -- an idea parked only on a spend ceiling comes back when a person raises it.

The finding (final adversarial review of 37e8afe, M4): ``tick._never_allocatable``
parked an idea at its idea or lineage spend ceiling with ``revisit_if = "a
person raises bounds.idea_spend_ceiling_usd"``, and nothing read that
sentence. A person who did exactly what the record said found the idea still
retired -- listed with the digest's negative results and shown to the
failure-mining explorer as a rejected idea.

The block is structural now (``ideas.park_reason``, ``park_stage``,
``resume_status``), and the tick re-checks it against the ceilings a person
set. The idea resumes only when a raised ceiling removes the *sole* block;
one also blocked for another reason, or parked for any reason that is not a
ceiling, stays parked. The revival reads a ceiling and never raises one
(INV-10).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ActionStatus,
    IdeaStatus,
    ParkReason,
    QualityDimensions,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_tick import _tick

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def _spent_to_the_ceiling(
    portfolio: PortfolioStore,
    project: str,
    *,
    lineage: bool = False,
    screened: bool = False,
) -> str:
    """A PROMISING idea whose recorded spend leaves no room for its next stage.

    ``screened`` runs the dedup pass and the novelty screen first, so the
    stage it waits for is one the novelty floor governs.
    """

    config = load_config()
    idea, _ = seed_idea(portfolio, project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    if screened:
        for stage in (Stage.DEDUP, Stage.NOVELTY_SCREEN):
            done = portfolio.open_action(
                idea_id=idea.idea_id,
                idea_version=1,
                stage=stage,
                basis_digest=str(stage),
            )
            portfolio.complete_action(
                action_id=done.action_id, status=ActionStatus.SUCCEEDED
            )
    ceiling = (
        config.bounds.lineage_spend_ceiling_usd
        if lineage
        else config.bounds.idea_spend_ceiling_usd
    )
    action = portfolio.open_action(
        idea_id=idea.idea_id, idea_version=1, stage=Stage.FALSIFY, basis_digest="s"
    )
    portfolio.complete_action(
        action_id=action.action_id,
        status=ActionStatus.SUCCEEDED,
        cost_usd=ceiling - Decimal("0.01"),
    )
    return idea.idea_id


def _parked(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    *,
    lineage: bool = False,
    screened: bool = False,
) -> str:
    idea_id = _spent_to_the_ceiling(
        portfolio, project, lineage=lineage, screened=screened
    )
    if lineage:
        # Room at the idea ceiling, so the lineage is what binds.
        portfolio.upsert_state(
            project_id=project, bounds={"idea_spend_ceiling_usd": "500"}
        )
    _tick(runtime_db, pg_dsn, tmp_path, project)
    parked = portfolio.require_idea(idea_id)
    assert parked.status is IdeaStatus.PARKED
    assert parked.park_reason is (
        ParkReason.LINEAGE_SPEND_CEILING if lineage else ParkReason.IDEA_SPEND_CEILING
    )
    assert parked.park_stage and parked.resume_status is IdeaStatus.PROMISING
    return idea_id


def test_raising_the_idea_ceiling_resumes_the_idea_as_it_was(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea_id = _parked(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"idea_spend_ceiling_usd": "100"}
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.budget_parks_revived == 1
    after = portfolio.require_idea(idea_id)
    assert after.status is IdeaStatus.PROMISING
    assert after.park_reason is None and after.retire_reason is None
    assert after.revisit_if is None


def test_raising_the_lineage_ceiling_resumes_a_lineage_parked_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea_id = _parked(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, lineage=True
    )
    portfolio.upsert_state(
        project_id=runtime_project,
        bounds={"idea_spend_ceiling_usd": "500", "lineage_spend_ceiling_usd": "500"},
    )
    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert portfolio.require_idea(idea_id).status is IdeaStatus.PROMISING


def test_a_ceiling_raised_too_little_revives_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    config = load_config()
    idea_id = _parked(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    raised = config.bounds.idea_spend_ceiling_usd + Decimal("0.01")
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"idea_spend_ceiling_usd": str(raised)}
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.budget_parks_revived == 0
    assert portfolio.require_idea(idea_id).status is IdeaStatus.PARKED


def test_an_idea_also_blocked_by_the_novelty_floor_stays_parked(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The ceiling was not the *sole* block, so raising it lifts nothing."""

    idea_id = _parked(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, screened=True
    )
    parked = portfolio.require_idea(idea_id)
    assert parked.park_stage not in {str(Stage.DEDUP), str(Stage.NOVELTY_SCREEN)}, (
        "the floor does not govern the screen itself; this needs a later stage"
    )
    portfolio.set_dimensions(
        idea_id=idea_id, version=1, dimensions=QualityDimensions(novelty=0.01)
    )
    portfolio.upsert_state(
        project_id=runtime_project, bounds={"idea_spend_ceiling_usd": "100"}
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.budget_parks_revived == 0
    assert portfolio.require_idea(idea_id).status is IdeaStatus.PARKED


def test_a_park_for_any_other_reason_is_not_revived_by_a_ceiling(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="the synthesis of the reviews parked it: not yet",
        revisit_if="a reviewer's objection is answered",
    )
    portfolio.upsert_state(
        project_id=runtime_project,
        bounds={"idea_spend_ceiling_usd": "500", "lineage_spend_ceiling_usd": "500"},
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.budget_parks_revived == 0
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.PARKED


def test_a_park_reason_does_not_outlive_its_park(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="spent",
        revisit_if="a person raises bounds.idea_spend_ceiling_usd",
        park_reason=ParkReason.IDEA_SPEND_CEILING,
        park_stage=str(Stage.FALSIFY),
        resume_status=IdeaStatus.CANDIDATE,
    )
    moved = portfolio.set_status(
        idea_id=idea.idea_id, status=IdeaStatus.REJECTED, retire_reason="killed"
    )
    assert moved is not None
    assert (moved.park_reason, moved.park_stage, moved.resume_status) == (
        None,
        None,
        None,
    )
