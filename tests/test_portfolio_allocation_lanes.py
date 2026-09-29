"""Lifecycle-aware allocation: exploration and advancement, and the reserve between them.

The first final qualification (538c54f) spent about 96 % of its authority on
exploration -- screening, originating and falsifying follow-up ideas -- while
admitted directions waited, and froze no executable contract. The allocator
now keeps two lanes (``allocation.Lane``) and, while advancement work is
eligible, holds ``bounds.advancement_reserve_fraction`` of the idea slots and
of the human-set monetary authority for it. These tests hold the semantics the
sprint brief states:

1. with only shallow work, exploration runs as before;
2. an unlimited stream of attractive shallow follow-ups cannot starve
   advancement, in slots or in spend -- one plan, and a many-tick stress run;
3. several eligible deep ideas are ranked by utility, not identity;
4. when every deep idea is blocked or refused, capacity returns to exploration;
5. budget exhaustion is unchanged;
6. nothing autonomous can change the configured split.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import allocation
from research_os.portfolio.allocation import (
    AdvancementPosition,
    Candidate,
    Lane,
    SaleAuthority,
)
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    PortfolioIdea,
    PortfolioStatus,
    QualityDimensions,
    QualityTier,
    Stage,
)
from tests.portfolio_helpers import portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

SRC = Path(__file__).resolve().parents[1] / "src" / "research_os"


# ------------------------------------------------------------- building --
def _idea(
    idea_id: str, *, lineage: str, status: IdeaStatus = IdeaStatus.CANDIDATE
) -> PortfolioIdea:
    moment = datetime(2026, 9, 29, tzinfo=UTC)
    return PortfolioIdea(
        idea_id=idea_id,
        project_id="p",
        depth=0,
        lineage_root=lineage,
        origin=IdeaOrigin.BLIND_EXPLORER,
        current_version=1,
        status=status,
        operational_state=OperationalState.IDLE,
        quality_tier=QualityTier.NONE,
        created_at=moment,
        updated_at=moment,
    )


def _candidate(
    idea_id: str,
    *,
    stage: Stage,
    status: IdeaStatus = IdeaStatus.CANDIDATE,
    lineage: str | None = None,
    novelty: float | None = None,
    objections: int = 0,
    spent: str = "0",
    bought: bool = False,
) -> Candidate:
    config = load_config()
    root = lineage or f"L-{idea_id}"
    return Candidate(
        idea=_idea(idea_id, lineage=root, status=status),
        dimensions=QualityDimensions(novelty=novelty),
        diversity=allocation.diversity_key(
            lineage_root=root, adjudication=[], research_question=idea_id
        ),
        stage=stage,
        reason=f"{stage} for {idea_id}",
        expected_cost=config.cost_for(stage),
        idle_seconds=0.0,
        open_objections=objections,
        spent=Decimal(spent),
        lineage_spent=Decimal(spent),
        bought=bought,
    )


def fresh(count: int, *, prefix: str = "F") -> list[Candidate]:
    """Unassessed follow-up children: the stream that scored near 3.05 in run 1."""

    return [
        _candidate(f"{prefix}{index:04d}", stage=Stage.DEDUP) for index in range(count)
    ]


def deep(
    idea_id: str,
    *,
    stage: Stage = Stage.LITERATURE_AUDIT,
    novelty: float = 0.7,
    objections: int = 10,
) -> Candidate:
    """A PROMISING idea whose next stage advances it: 2.1-ish utility in run 1."""

    return _candidate(
        idea_id,
        stage=stage,
        status=IdeaStatus.PROMISING,
        novelty=novelty,
        objections=objections,
    )


def _config(fraction: str = "0.5", **bounds: Any) -> Any:
    return load_config().with_overrides(
        {"advancement_reserve_fraction": fraction, **bounds}
    )


def plan(
    candidates: list[Candidate],
    *,
    fraction: str = "0.5",
    free_slots: int = 3,
    authority: SaleAuthority | None = None,
    advancement: AdvancementPosition | None = None,
    open_requests: int = 0,
    may_explore: bool = False,
    candidate_pool: int = 100,
    **bounds: Any,
) -> tuple[allocation.Allocation, ...]:
    return allocation.plan(
        candidates=candidates,
        config=_config(fraction, max_active_per_lineage=100, **bounds),
        free_slots=free_slots,
        lineage_in_flight={},
        candidate_pool=candidate_pool,
        pending_seeds=0,
        origin_counts={},
        minable_failures=0,
        tick_bucket="t",
        may_explore=may_explore,
        open_requests=[
            (f"REQ-{index}", 0, "FALSIFIER_OBJECTION") for index in range(open_requests)
        ],
        authority=authority,
        advancement=advancement,
    )


def lanes(sold: tuple[allocation.Allocation, ...]) -> list[Lane]:
    return [item.lane for item in sold]


# ------------------------------------------------------------ the lanes --
@pytest.mark.parametrize(
    ("kind", "stage", "status", "lane"),
    [
        (allocation.EXPLORE, None, None, Lane.EXPLORATION),
        (allocation.FOLLOW_UP, None, None, Lane.EXPLORATION),
        (allocation.LITERATURE_REQUEST, None, None, Lane.ADVANCEMENT),
        (allocation.SYNTHESIZE, None, None, Lane.ADVANCEMENT),
        (allocation.ADVANCE_IDEA, Stage.DEDUP, IdeaStatus.CANDIDATE, Lane.EXPLORATION),
        (
            allocation.ADVANCE_IDEA,
            Stage.FALSIFY,
            IdeaStatus.CANDIDATE,
            Lane.EXPLORATION,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.DISCOVER,
            IdeaStatus.CANDIDATE,
            Lane.EXPLORATION,
        ),
        (allocation.ADVANCE_IDEA, Stage.BRANCH, IdeaStatus.PROMISING, Lane.EXPLORATION),
        (
            allocation.ADVANCE_IDEA,
            Stage.DISCOVER,
            IdeaStatus.PROMISING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.FALSIFY,
            IdeaStatus.PROMISING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.ADJUDICATE,
            IdeaStatus.INVESTIGATING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.LITERATURE_AUDIT,
            IdeaStatus.PROMISING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.EVIDENCE,
            IdeaStatus.PROMISING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.REVIEW_BOARD,
            IdeaStatus.INVESTIGATING,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.META_REVIEW,
            IdeaStatus.REVIEW,
            Lane.ADVANCEMENT,
        ),
        (
            allocation.ADVANCE_IDEA,
            Stage.REPLICATE,
            IdeaStatus.VALIDATED,
            Lane.ADVANCEMENT,
        ),
        # A queue row from before lanes: the ladder reads as exploration.
        (allocation.ADVANCE_IDEA, "falsify", None, Lane.EXPLORATION),
    ],
)
def test_every_unit_of_work_has_one_lane_by_its_kind_stage_and_status(
    kind: str, stage: Any, status: IdeaStatus | None, lane: Lane
) -> None:
    assert allocation.lane_of(kind, stage, status) is lane


# ---------------------------------------------------------- 1. shallow only --
def test_with_only_shallow_work_exploration_runs_as_it_did() -> None:
    """No admitted idea: the reserve is not in force, and the plan is unchanged."""

    pool = fresh(12)
    authority = SaleAuthority(cost_usd=Decimal(30))
    position = AdvancementPosition(protected_usd=Decimal(15))
    with_reserve = plan(
        pool,
        free_slots=8,
        authority=authority,
        advancement=position,
        open_requests=1,
        may_explore=True,
        candidate_pool=4,
    )
    without = plan(
        pool,
        fraction="0",
        free_slots=8,
        authority=authority,
        open_requests=1,
        may_explore=True,
        candidate_pool=4,
    )

    assert [(item.kind, item.idea_id) for item in with_reserve] == [
        (item.kind, item.idea_id) for item in without
    ]
    assert set(lanes(with_reserve)) == {Lane.EXPLORATION}
    assert not allocation.advancement_eligible(
        candidates=pool, config=_config(), authority=authority, advancement=position
    )
    # Every idea slot, the follow-up and an explorer are bought.
    assert sum(1 for item in with_reserve if item.kind == allocation.ADVANCE_IDEA) == 7
    assert any(item.kind == allocation.FOLLOW_UP for item in with_reserve)
    assert any(item.kind == allocation.EXPLORE for item in with_reserve)


# --------------------------------------------- 2. no starvation, one plan --
def test_attractive_shallow_work_cannot_take_the_advancement_slots() -> None:
    """Fifty fresh children outrank two admitted ideas; the admitted ideas are bought."""

    pool = [*fresh(50), deep("D1"), deep("D2", stage=Stage.DISCOVER, objections=4)]
    config = _config()
    top_fresh = max(
        allocation.utility(item, config=config, active=[]) for item in pool[:50]
    )
    assert all(
        allocation.utility(item, config=config, active=[]) < top_fresh
        for item in pool[50:]
    ), "not vacuous: breadth outranks both"

    sold = plan(pool, free_slots=3)

    ideas = [item.idea_id for item in sold if item.kind == allocation.ADVANCE_IDEA]
    assert ideas[:2] == ["D1", "D2"] or ideas[:2] == ["D2", "D1"]
    assert all("reserved for advancement" in item.reason for item in sold[:2])
    assert len(ideas) == 3, "the third slot is still breadth"
    assert sold[2].lane is Lane.EXPLORATION


def test_the_pool_floor_explorer_cannot_take_the_only_advancement_slot() -> None:
    """One free slot, a short pool, an admitted idea waiting: the idea gets the slot.

    The pool-floor explorer used to be bought before the share was counted,
    so at capacity -- one slot, on every tick the pool was short -- the
    reserve held nothing.
    """

    sold = plan(
        [*fresh(3), deep("D1")], free_slots=1, may_explore=True, candidate_pool=0
    )
    assert [item.idea_id for item in sold if item.kind == allocation.ADVANCE_IDEA] == [
        "D1"
    ]
    assert all(item.kind != allocation.EXPLORE for item in sold)
    # Nothing to advance: the floor explorer takes the slot, as it always did.
    sold = plan(fresh(3), free_slots=1, may_explore=True, candidate_pool=0)
    assert [item.kind for item in sold] == [allocation.EXPLORE]
    # Two slots: the explorer and the advancement share, one each.
    sold = plan(
        [*fresh(3), deep("D1")], free_slots=2, may_explore=True, candidate_pool=0
    )
    assert sorted(str(item.kind) for item in sold) == sorted(
        [str(allocation.EXPLORE), str(allocation.ADVANCE_IDEA)]
    )
    assert "D1" in {item.idea_id for item in sold}


def test_attractive_shallow_work_cannot_spend_the_advancement_reserve() -> None:
    """Unlimited slots and shallow work: exploration stops at the protected share."""

    pool = [*fresh(200), deep("D1")]
    authority = SaleAuthority(cost_usd=Decimal(10))
    sold = plan(
        pool,
        free_slots=200,
        authority=authority,
        advancement=AdvancementPosition(protected_usd=Decimal(5)),
        open_requests=1,
    )

    explored = sum(
        (item.charged for item in sold if item.lane is Lane.EXPLORATION), Decimal(0)
    )
    advanced = sum(
        (item.charged for item in sold if item.lane is Lane.ADVANCEMENT), Decimal(0)
    )
    assert "D1" in {item.idea_id for item in sold}
    assert advanced == Decimal("1.50")
    # Everything but the advancement reserve less what advancement bought.
    assert explored <= Decimal(10) - Decimal(5)
    assert explored > Decimal(4), "exploration still spends its own share"


def test_the_reserve_is_held_while_a_deep_stage_runs() -> None:
    """Nothing admissible this tick, but advancement work in flight: the reserve holds."""

    pool = fresh(100)
    sold = plan(
        pool,
        free_slots=100,
        authority=SaleAuthority(cost_usd=Decimal(10)),
        advancement=AdvancementPosition(protected_usd=Decimal(6), in_flight=1),
    )
    spent = sum((item.charged for item in sold), Decimal(0))
    assert spent <= Decimal(4)


# -------------------------------------------- 2b. the follow-up explosion --
#: One admitted direction's remaining path to completion: 9.80 USD of stages.
PATH: tuple[Stage, ...] = (
    Stage.DISCOVER,
    Stage.FALSIFY,
    Stage.ADJUDICATE,
    Stage.LITERATURE_AUDIT,
    Stage.EVIDENCE,
    Stage.REVIEW_BOARD,
    Stage.META_REVIEW,
    Stage.REPLICATE,
)


def _explosion(
    *, fraction: str, spend_reserve: bool = True, ticks: int = 400
) -> dict[str, Any]:
    """The scheduler, many ticks, against an endless follow-up explosion.

    Every tick ten fresh, attractive children arrive and a follow-up question
    is open; the pool is below its floor, so an explorer is wanted too. One
    admitted direction has the path above to run, and each of its stages is
    in flight for three ticks after it is bought -- the case in which a
    reserve read only from this tick's candidates would be borrowed away.
    Every sale settles at its full ceiling, the worst case for the reserve.
    The human-set ceiling is 30 USD.
    """

    ceiling = Decimal(30)
    share = Decimal(fraction)
    spent = {Lane.EXPLORATION: Decimal(0), Lane.ADVANCEMENT: Decimal(0)}
    done = 0
    running = 0
    exploration_while_pending: list[Decimal] = []
    for tick in range(ticks):
        left = ceiling - spent[Lane.EXPLORATION] - spent[Lane.ADVANCEMENT]
        if left < Decimal("0.25"):
            break
        pool = fresh(10, prefix=f"T{tick:04d}-")
        if done < len(PATH) and running == 0:
            pool.append(deep("DEEP", stage=PATH[done]))
        position = AdvancementPosition(
            protected_usd=(
                max(Decimal(0), share * ceiling - spent[Lane.ADVANCEMENT])
                if spend_reserve
                else None
            ),
            in_flight=1 if running else 0,
        )
        sold = plan(
            pool,
            fraction=fraction,
            free_slots=3,
            authority=SaleAuthority(cost_usd=left),
            advancement=position,
            open_requests=1,
            may_explore=True,
            candidate_pool=0,
        )
        running = max(0, running - 1)
        for item in sold:
            spent[item.lane] += item.charged
            if item.idea_id == "DEEP":
                done += 1
                running = 3
        if done < len(PATH):
            exploration_while_pending.append(spent[Lane.EXPLORATION])
    return {
        "done": done,
        "spent": spent,
        "exploration_while_pending": exploration_while_pending,
        "ceiling": ceiling,
    }


def test_a_follow_up_explosion_cannot_consume_the_advancement_authority() -> None:
    """The scheduler stress test: exploration stops at its share; advancement completes."""

    run = _explosion(fraction="0.5")

    assert run["done"] == len(PATH), "the admitted direction ran its whole path"
    # While it had work left, exploration never took more than its share --
    # exactly: a sale is refused if it would leave less than the reserve.
    assert max(run["exploration_while_pending"]) <= Decimal(15)
    assert max(run["exploration_while_pending"]) > Decimal(14), (
        "not vacuous: the explosion really did press against the reserve"
    )
    assert sum(run["spent"].values(), Decimal(0)) <= run["ceiling"]


def test_without_the_spend_reserve_the_same_explosion_starves_completion() -> None:
    """Not vacuous: reserved slots alone let breadth drain the authority first."""

    run = _explosion(fraction="0.5", spend_reserve=False)

    assert run["done"] < len(PATH)
    assert run["spent"][Lane.EXPLORATION] > Decimal(20)


def test_at_fraction_zero_the_explosion_is_the_first_final_qualification() -> None:
    """The earlier release's plan: a sharpening outranked by breadth is never bought."""

    run = _explosion(fraction="0")

    assert run["done"] == 0


# ------------------------------------------ 3. ranked by utility, not identity --
def test_several_deep_ideas_are_ranked_by_utility_not_by_identity() -> None:
    weak = deep("A-weak", novelty=0.2, objections=10)
    strong = deep("Z-strong", novelty=0.9, objections=1)
    middle = deep("M-middle", novelty=0.5, objections=5)
    config = _config()
    ranked = sorted(
        [weak, strong, middle],
        key=lambda item: -allocation.utility(item, config=config, active=[]),
    )
    assert [item.idea.idea_id for item in ranked] == ["Z-strong", "M-middle", "A-weak"]

    sold = plan([*fresh(20), weak, middle, strong], free_slots=2)
    reserved = [
        item.idea_id for item in sold if "reserved for advancement" in item.reason
    ]
    assert reserved == ["Z-strong"]

    # Rename them: the choice follows the utility, not the id.
    renamed = [
        dataclasses.replace(item, idea=item.idea.model_copy(update={"idea_id": name}))
        for item, name in (
            (weak, "Z-was-weak"),
            (middle, "B-was-middle"),
            (strong, "A-was-strong"),
        )
    ]
    sold = plan([*fresh(20), *renamed], free_slots=2)
    reserved = [
        item.idea_id for item in sold if "reserved for advancement" in item.reason
    ]
    assert reserved == ["A-was-strong"]


# ---------------------------------------- 4. blocked deep work frees capacity --
def test_when_every_deep_idea_is_blocked_capacity_returns_to_exploration() -> None:
    """A deep idea over its ceiling and one below the novelty floor: no reserve."""

    over = dataclasses.replace(deep("OVER"), spent=Decimal("11.00"))
    known = deep("KNOWN", novelty=0.05)
    pool = [*fresh(100), over, known]
    authority = SaleAuthority(cost_usd=Decimal(10))
    position = AdvancementPosition(protected_usd=Decimal(5))

    assert not allocation.advancement_eligible(
        candidates=pool,
        config=_config(idea_spend_ceiling_usd="12"),
        authority=authority,
        advancement=position,
    )
    sold = plan(
        pool,
        free_slots=100,
        authority=authority,
        advancement=position,
        idea_spend_ceiling_usd="12",
    )
    assert {"OVER", "KNOWN"}.isdisjoint({item.idea_id for item in sold})
    explored = sum((item.charged for item in sold), Decimal(0))
    assert explored > Decimal(9), "the reserve is borrowed, not left idle"


def test_a_capability_limited_deep_idea_is_still_advanced_when_nothing_else_waits() -> (
    None
):
    """Refused work leaves the candidate set; limited work only loses ties."""

    from research_os.portfolio.models import Feasibility

    limited = dataclasses.replace(
        deep("LIMITED"), feasibility=Feasibility.CAPABILITY_LIMITED
    )
    sold = plan([*fresh(20), limited], free_slots=2)
    assert "LIMITED" in {item.idea_id for item in sold}


# ------------------------------------------------------ 5. budget exhaustion --
def test_a_remainder_below_every_call_buys_nothing_in_either_lane() -> None:
    pool = [*fresh(10), deep("D1")]
    sold = plan(
        pool,
        free_slots=5,
        authority=SaleAuthority(cost_usd=Decimal("0.20")),
        advancement=AdvancementPosition(protected_usd=Decimal("0.10")),
        open_requests=1,
    )
    assert sold == ()


def test_advancement_may_spend_the_whole_remainder() -> None:
    """The reserve binds exploration only: a deep stage may take what is left."""

    sold = plan(
        [*fresh(10), deep("D1")],
        free_slots=3,
        authority=SaleAuthority(cost_usd=Decimal("1.60")),
        advancement=AdvancementPosition(protected_usd=Decimal(15)),
    )
    assert [item.idea_id for item in sold] == ["D1"]


def test_the_tick_still_pauses_a_spent_portfolio_with_advancement_waiting(
    portfolio: Any, runtime_db: Any, pg_dsn: str, tmp_path: Path, runtime_project: str
) -> None:
    from research_os.portfolio.tick import tick
    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope
    from tests.portfolio_helpers import seed_idea
    from tests.runtime_graph_helpers import make_config

    idea, _ = seed_idea(portfolio, runtime_project, title="admitted")
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("0.20"),
        explicit=True,
    )
    report = tick(
        db=runtime_db,
        project_id=runtime_project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
    )
    assert report.status is PortfolioStatus.PAUSED_BUDGET_EXHAUSTED
    assert report.allocations == ()


# --------------------------------------------- the tick, against the ledger --
def test_the_tick_holds_the_reserve_against_the_real_ledger(
    portfolio: Any, runtime_db: Any, pg_dsn: str, tmp_path: Path, runtime_project: str
) -> None:
    """End to end through the tick: the protected share comes from the ledger.

    A 4 USD ceiling, fraction 0.5: 2 USD are held while the admitted idea
    has advancement work (its re-run ladder), and exploration is sold only
    the other half, however many children wait.
    """

    from research_os.portfolio.tick import tick
    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope
    from tests.portfolio_helpers import seed_idea
    from tests.runtime_graph_helpers import make_config

    admitted, _ = seed_idea(portfolio, runtime_project, title="admitted direction")
    portfolio.set_status(idea_id=admitted.idea_id, status=IdeaStatus.PROMISING)
    for index in range(30):
        seed_idea(portfolio, runtime_project, title=f"child {index}")
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("4.00"),
        explicit=True,
    )
    config = load_config().with_overrides(
        {
            "max_active_tracks": 30,
            "candidate_pool_floor": 0,
            "max_active_per_lineage": 30,
        }
    )
    report = tick(
        db=runtime_db,
        project_id=runtime_project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=config,
    )
    explored = sum(
        (item.charged for item in report.allocations if item.lane is Lane.EXPLORATION),
        Decimal(0),
    )
    assert report.advancement_eligible
    assert Decimal(report.advancement_protected_usd) == Decimal(2)
    assert admitted.idea_id in {item.idea_id for item in report.allocations}
    assert explored <= Decimal("2.00")
    # And the lane is recorded on each queue item it bought.
    from research_os.runtime.queue import WorkQueue

    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select payload->>'idea_id' as idea, payload->>'lane' as lane "
            "from work_items where kind = 'portfolio_advance_idea'"
        ).fetchall()
    recorded = {row["idea"]: row["lane"] for row in rows}
    assert recorded[admitted.idea_id] == "advancement"
    assert set(recorded.values()) <= {"advancement", "exploration"}
    assert WorkQueue  # the queue the tick enqueued into


def test_the_share_is_of_the_ceiling_that_binds() -> None:
    """A system ceiling tighter than the project's: the share is of the system's.

    Of the looser one, half of 30 USD would be held against a 4 USD system
    authority, and exploration could buy nothing while anything was eligible.
    """

    from types import SimpleNamespace

    from research_os.portfolio.tick import _advancement_position
    from research_os.runtime.budgets import Dimension
    from research_os.runtime.models import BudgetScope

    class _Store:
        def lane_work(self, **_: Any) -> list[Any]:
            return []

        def lane_spend(self, **_: Any) -> list[Any]:
            return []

    ledgers = {
        (BudgetScope.PROJECT, Dimension.MODEL_COST_USD): SimpleNamespace(
            limit_value="30"
        ),
        (BudgetScope.SYSTEM, Dimension.MODEL_COST_USD): SimpleNamespace(
            limit_value="4"
        ),
    }
    position = _advancement_position(_Store(), "P", _config(), ledgers)
    assert position.protected_usd == Decimal(2)
    del ledgers[(BudgetScope.SYSTEM, Dimension.MODEL_COST_USD)]
    position = _advancement_position(_Store(), "P", _config(), ledgers)
    assert position.protected_usd == Decimal(15)


# ------------------------------------------------- 6. a person's number only --
def _calls_with_keyword(tree: ast.AST, function: str, keyword: str) -> list[int]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else ""
        )
        if name == function and any(item.arg == keyword for item in node.keywords):
            found.append(node.lineno)
    return found


def test_nothing_autonomous_can_raise_lower_or_remove_the_configured_split() -> None:
    """The fraction is read from a person's file or a person's stored override only.

    No module anywhere in the package writes a project's bound overrides
    (``upsert_state(bounds=...)``), and nothing but the configuration model
    names the field as a keyword or assigns it -- the allocator and the tick
    only read it.
    """

    writers: list[str] = []
    namers: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()
        if _calls_with_keyword(tree, "upsert_state", "bounds"):
            writers.append(rel)
        if rel == "portfolio/config.py":
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "advancement_reserve_fraction"
            ):
                namers.append(f"{rel}:{node.value.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == ("advancement_reserve_fraction")
                and isinstance(node.ctx, ast.Store | ast.Del)
            ):
                namers.append(f"{rel}:{node.lineno}")
            if (
                isinstance(node, ast.Constant)
                and node.value == "advancement_reserve_fraction"
            ):
                namers.append(f"{rel}:{node.lineno}")
    assert writers == []
    assert namers == []


def test_a_tick_leaves_the_bounds_a_person_set_exactly_as_they_were(
    portfolio: Any, runtime_db: Any, pg_dsn: str, tmp_path: Path, runtime_project: str
) -> None:
    from research_os.portfolio.tick import tick
    from tests.portfolio_helpers import seed_idea
    from tests.runtime_graph_helpers import make_config

    idea, _ = seed_idea(portfolio, runtime_project, title="admitted")
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    before = portfolio.upsert_state(project_id=runtime_project).bounds
    for _ in range(2):
        tick(
            db=runtime_db,
            project_id=runtime_project,
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
        )
    assert portfolio.get_state(runtime_project).bounds == before


def test_the_fraction_is_bounded_so_advancement_can_never_take_everything() -> None:
    from pydantic import ValidationError

    from research_os.portfolio.config import Bounds

    assert Bounds().advancement_reserve_fraction == Decimal("0.5")
    with pytest.raises(ValidationError):
        Bounds(advancement_reserve_fraction=Decimal("0.95"))
    with pytest.raises(ValidationError):
        Bounds(advancement_reserve_fraction=Decimal("-0.1"))
