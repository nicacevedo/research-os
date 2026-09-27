"""The independent review's MEDIUM and LOW findings against 8e92e8c.

**MEDIUM -- LEGACY_BUDGET_PARK.** An idea parked before park reasons were
structural carried none after upgrade, so raising the ceiling its revisit text
named revived nothing -- which fails closed scientifically and misleads about
recovery. ``sql/0045`` marks every such park ``legacy_unknown``: the legacy
text cannot establish a budget park (it is model-writable prose, and no stage
or resume status was recorded), so it is never revived by a ceiling or by the
lineage-room rule, and every place a person reads it says a person decides.
The neighbour found while repairing it: the lineage-room reviver parsed the
same prose field for a status to resume at, so a model that wrote "room in
its lineage to branch; resumes as PROMISING" could have had an idea revived
at a status it named. It reads the structural reason and resume status now.

**LOW -- SIMULTANEOUS_OWNERS.** Two schedulers that both found no active
action both inserted one; the partial unique index let one commit and the
loser saw a raw database error. It is the documented lost race now.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from research_os.portfolio import frontier
from research_os.portfolio.config import load_config
from research_os.portfolio.curator import render_idea
from research_os.portfolio.models import IdeaStatus, ParkReason, Stage
from research_os.portfolio.store import ActiveTrackExistsError, PortfolioStore
from research_os.portfolio.tick import _revive_budget_parks
from research_os.runtime.db import Database
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project"]


def _legacy_park(portfolio: PortfolioStore, runtime_db: Database, project: str) -> str:
    """A park exactly as a 0036 deployment left it, after `sql/0045`."""

    idea, _ = seed_idea(portfolio, project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="spent its idea ceiling",
        revisit_if="a person raises bounds.idea_spend_ceiling_usd",
    )
    with runtime_db.tx() as conn:
        conn.execute(
            "update ideas set park_reason = 'legacy_unknown' where idea_id = %s",
            (idea.idea_id,),
        )
    return idea.idea_id


def test_a_legacy_park_is_revived_by_no_ceiling_and_says_a_person_decides(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The intended recovery behaviour, end to end.

    Raising every ceiling revives nothing; the idea's page keeps its history
    (why it stopped, what its text said would revisit it) and states, in
    trusted text, that no ceiling or lineage change revives it. The idea can
    still be moved by a status change a person makes, which clears the park.
    """

    idea_id = _legacy_park(portfolio, runtime_db, runtime_project)
    raised = load_config().with_overrides(
        {"idea_spend_ceiling_usd": 1000.0, "lineage_spend_ceiling_usd": 1000.0}
    )
    assert _revive_budget_parks(portfolio, runtime_project, raised) == 0
    assert frontier.revive_for_lineage_room(portfolio, runtime_project, raised) == 0
    parked = portfolio.require_idea(idea_id)
    assert parked.status is IdeaStatus.PARKED
    assert parked.park_reason is ParkReason.LEGACY_UNKNOWN
    page = render_idea(portfolio, parked)
    assert "a person decides" in page
    # The legacy sentence is kept (rendered inert, as all model-writable text).
    assert "revisit if" in page and "idea" in page.split("revisit if", 1)[1][:200]

    moved = portfolio.set_status(
        idea_id=idea_id, status=IdeaStatus.CANDIDATE, clear_retirement=True
    )
    assert moved is not None and moved.park_reason is None


def test_model_prose_naming_a_lineage_room_status_revives_nothing(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The neighbour: a revisit sentence is not a structural reason."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="could not be made precise: a model said so",
        # A model's `minimum_decisive_action`, written into revisit_if by the
        # discover stage, shaped like the lineage-room sentence.
        revisit_if=f"{frontier.LINEAGE_ROOM}PROMISING",
    )
    assert portfolio.require_idea(idea.idea_id).park_reason is None
    assert (
        frontier.revive_for_lineage_room(portfolio, runtime_project, load_config()) == 0
    )
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.PARKED


def test_a_structural_lineage_room_park_still_resumes_at_its_recorded_status(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The control: the reviver still does its job from the structural fields."""

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.PARKED,
        retire_reason="waiting for its lineage to have room",
        revisit_if="anything at all -- the prose is not read",
        park_reason=ParkReason.LINEAGE_ROOM,
        resume_status=IdeaStatus.PROMISING,
    )
    assert (
        frontier.revive_for_lineage_room(portfolio, runtime_project, load_config()) == 1
    )
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.PROMISING


def test_the_scheduler_that_loses_the_insert_race_is_told_it_lost(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """SIMULTANEOUS_OWNERS, made deterministic.

    The winner's action is inserted and left uncommitted, so the loser's
    ``for update`` finds no active row, inserts, and waits on the unique index
    until the winner commits. The loser's result is ActiveTrackExistsError --
    the same lost race as the check it slipped past -- not a raw database
    error.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    outcome: dict[str, Any] = {}

    def lose() -> None:
        try:
            portfolio.open_action(
                idea_id=idea.idea_id,
                idea_version=1,
                stage=Stage.NOVELTY_SCREEN,
                basis_digest="basis-loser",
                run_id="RRUN-loser",
                attempt=1,
                lease_owner="loser",
            )
            outcome["result"] = "won"
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["result"] = exc

    loser = threading.Thread(target=lose, daemon=True)
    with runtime_db.tx() as conn:
        conn.execute(
            "insert into idea_actions (action_id, idea_id, idea_version, stage, "
            "basis_digest, status, run_id, attempt, lease_owner) values "
            "('IACT-19700101T000000Z-winner00', %s, 1, 'novelty_screen', "
            "'basis-winner', 'ACTIVE', 'RRUN-winner', 1, 'winner')",
            (idea.idea_id,),
        )
        loser.start()
        loser.join(timeout=1.5)
        assert loser.is_alive(), "the loser should be waiting on the index"
    loser.join(timeout=10)
    assert not loser.is_alive()
    result = outcome["result"]
    assert isinstance(result, ActiveTrackExistsError), repr(result)
    assert "IACT-19700101T000000Z-winner00" in str(result)


def test_a_failure_that_is_not_the_race_is_not_disguised_as_one(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    from research_os.runtime.db import RuntimeDatabaseError

    with pytest.raises(RuntimeDatabaseError):
        portfolio.open_action(
            idea_id="PIDEA-19700101T000000Z-nonexist",
            idea_version=1,
            stage=Stage.NOVELTY_SCREEN,
            basis_digest="b",
        )
