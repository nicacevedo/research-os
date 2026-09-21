"""What the portfolio spends the next unit of work on. Ordinary Python.

No model is consulted here, and an earlier draft that allowed one to break ties
inside a band the policy had already chosen is gone. ``researchd`` runs the
tick, and ``DESIGN_INVARIANTS.md``'s R5 record says of the daemon: *it calls no
model*. A tie-breaker would have made that false in the one process where it is
load bearing, and the existing guard could not have caught it -- the AST test
that checks for a model call parses ``daemon.py`` only.

What that buys, beyond the invariant: the same portfolio state produces the
same plan, which is what makes fairness, diversity and bound enforcement
testable at all.

The scalar utility is an **operational number**. It is stored on
``idea_actions`` so allocation decisions are auditable, it never reaches the
scientific record, and the human-facing top-ideas list is a Pareto front with a
diversity constraint rather than an ordering by it. See docs/adr/0004.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from research_os.portfolio import digests as pdigests
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.models import (
    IdeaOrigin,
    IdeaStatus,
    PortfolioIdea,
    QualityDimensions,
    Stage,
)

#: Work kinds the allocator emits. Strings rather than an enum because they are
#: queue payload discriminators, which is the same reason ``WorkKind`` in the
#: daemon is plain constants.
ADVANCE_IDEA = "portfolio_advance_idea"
EXPLORE = "portfolio_explore"
CURATE = "portfolio_curate"
DIGEST = "portfolio_digest"


@dataclass(frozen=True, slots=True)
class DiversityKey:
    """The coordinates an idea occupies in the portfolio's diversity space.

    Every component is deterministic. An earlier draft also grouped on a
    free-text ``mechanism_family`` label a model returned, which would have
    made "the same state produces the same plan" false the moment two runs
    phrased one mechanism differently -- and would have made the diversity
    bound depend on vocabulary rather than on content.

    ``subproblem`` is the normalised token set of the research question, run
    through the same normaliser the canonical digest uses, so "spectral
    regularisation" and "spectral penalty" are one coordinate.
    """

    lineage_root: str
    adjudication: tuple[str, ...]
    subproblem: frozenset[str]

    def overlap(self, other: DiversityKey) -> float:
        """How much of the portfolio's diversity space these two share, 0 to 1."""

        score = 0.0
        if self.lineage_root == other.lineage_root:
            score += 0.5
        if set(self.adjudication) & set(other.adjudication):
            score += 0.2
        if self.subproblem and other.subproblem:
            shared = len(self.subproblem & other.subproblem)
            union = len(self.subproblem | other.subproblem)
            score += 0.3 * (shared / union if union else 0.0)
        return min(1.0, score)


@dataclass(frozen=True, slots=True)
class Allocation:
    """One unit of work the portfolio has decided to buy."""

    kind: str
    reason: str
    utility: Decimal = Decimal(0)
    idea_id: str | None = None
    stage: Stage | None = None
    explorer: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """What stops two ticks buying the same thing twice.

        For an idea advance it is the idea and the stage: the *action* row is
        the real idempotency (a partial unique index on the scientific basis),
        and this only has to stop two work items existing at once. For an
        explorer it is the explorer and the tick's own bucket, because two
        blind explorations in one tick is a duplicate and two in successive
        ticks is the system working.
        """

        if self.kind == ADVANCE_IDEA:
            return f"{self.kind}:{self.idea_id}:{self.stage}"
        return f"{self.kind}:{self.explorer or ''}:{self.payload.get('bucket', '')}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One allocatable idea, with everything the utility function reads."""

    idea: PortfolioIdea
    dimensions: QualityDimensions
    diversity: DiversityKey
    stage: Stage
    reason: str
    expected_cost: Decimal
    idle_seconds: float
    open_objections: int
    spent: Decimal
    lineage_spent: Decimal


def diversity_key(
    *, lineage_root: str, adjudication: Sequence[str], research_question: str
) -> DiversityKey:
    return DiversityKey(
        lineage_root=lineage_root,
        adjudication=tuple(sorted(str(item) for item in adjudication)),
        subproblem=frozenset(pdigests.normalise(research_question)),
    )


def utility(
    candidate: Candidate,
    *,
    config: PortfolioConfig,
    active: Sequence[DiversityKey],
) -> Decimal:
    """The scheduling utility. An operational number, not a quality score.

    Every term is a reason to spend the next unit of work here rather than
    somewhere else. None of them is a claim about whether the idea is right --
    the closest, ``plausibility``, is a model's assessment recorded as a
    dimension, and the gates never read it.
    """

    weights = config.weights
    dimensions = candidate.dimensions
    score = 0.0
    for name, weight in (
        ("novelty", weights.novelty),
        ("potential_impact", weights.potential_impact),
        ("plausibility", weights.plausibility),
        ("falsifiability", weights.falsifiability),
        ("tractability", weights.tractability),
        ("evidence_strength", weights.evidence_strength),
    ):
        value = getattr(dimensions, name)
        # An *unassessed* dimension is worth the midpoint, not zero. The
        # difference matters: a candidate nobody has assessed should be worth
        # looking at, and scoring it zero would make the portfolio prefer
        # anything already measured -- which is the state every new idea is in.
        score += weight * (0.5 if value is None else value)

    # Cheap work first, so the portfolio buys three falsifications before one
    # replication when their scientific value is close.
    score -= weights.cost * float(candidate.expected_cost)

    # An idea nothing has touched drifts up, so a portfolio does not converge
    # on one lineage by neglect. Capped at a day so an idea parked for a month
    # does not outrank everything alive.
    score += weights.staleness * min(1.0, candidate.idle_seconds / 86_400.0)

    # A cheap stage that could close the idea's last open question is worth
    # more than the same stage on an idea with six of them.
    score += weights.decisiveness / (1.0 + candidate.open_objections)

    # And the one term that is about the portfolio rather than the idea.
    if active:
        crowding = max(candidate.diversity.overlap(other) for other in active)
        score -= weights.diversity_penalty * crowding

    return Decimal(str(round(score, 6)))


def choose_explorer(
    *,
    pending_seeds: int,
    origin_counts: Mapping[IdeaOrigin, int],
    minable_failures: int,
) -> tuple[str, str]:
    """Which explorer to run next, and why. Deterministic and rotating.

    Seeds first, because a researcher's seed sitting unconsumed while the
    system explores on its own is the portfolio ignoring the one input it was
    given. Then failure mining, if there is anything to mine and it is
    under-represented. Then blind, whose whole value is that it has not seen
    the bank.

    Rotation rather than a fixed ratio: the counts decide, so a portfolio that
    has drifted towards one origin corrects itself without anybody tuning a
    weight.
    """

    if pending_seeds:
        return "seeded_explorer", f"{pending_seeds} researcher seed(s) unconsumed"
    blind = origin_counts.get(IdeaOrigin.BLIND_EXPLORER, 0)
    mined = origin_counts.get(IdeaOrigin.FAILURE_MINING_EXPLORER, 0)
    if minable_failures and mined * 2 <= blind:
        return "failure_mining_explorer", (
            f"{minable_failures} failure(s) to mine and only {mined} idea(s) came "
            f"from mining"
        )
    return "blind_explorer", "keep generating directions that have not seen the bank"


def plan(
    *,
    candidates: Sequence[Candidate],
    config: PortfolioConfig,
    free_slots: int,
    lineage_active: Mapping[str, int],
    candidate_pool: int,
    pending_seeds: int,
    origin_counts: Mapping[IdeaOrigin, int],
    minable_failures: int,
    tick_bucket: str,
    explorers_in_flight: int = 0,
) -> tuple[Allocation, ...]:
    """The ordered, bounded list of work this tick buys.

    Order of business, and it is the governing principle again: keep the pool
    from emptying, then spend what is left on the highest-utility work that
    every bound permits.
    """

    allocations: list[Allocation] = []
    remaining = max(0, free_slots)

    if (
        remaining
        and candidate_pool < config.bounds.candidate_pool_floor
        and explorers_in_flight == 0
    ):
        explorer, why = choose_explorer(
            pending_seeds=pending_seeds,
            origin_counts=origin_counts,
            minable_failures=minable_failures,
        )
        allocations.append(
            Allocation(
                kind=EXPLORE,
                reason=(
                    f"the candidate pool is {candidate_pool}, below the floor of "
                    f"{config.bounds.candidate_pool_floor}: {why}"
                ),
                explorer=explorer,
                payload={"bucket": tick_bucket},
            )
        )
        remaining -= 1

    # Recomputed as the plan grows, so the second thing allocated is judged
    # against a portfolio that already contains the first. Without this a tick
    # with four free slots would allocate four ideas from one lineage, each
    # scored against the same unchanged active set.
    active: list[DiversityKey] = []
    taken_lineage = dict(lineage_active)
    pool = list(candidates)
    while remaining > 0 and pool:
        scored = [(utility(item, config=config, active=active), item) for item in pool]
        # Ties broken by idea id, so the plan is a function of state and not of
        # dictionary order.
        scored.sort(key=lambda pair: (-pair[0], pair[1].idea.idea_id))
        chosen: Candidate | None = None
        for score, item in scored:
            if taken_lineage.get(item.idea.lineage_root, 0) >= (
                config.bounds.max_active_per_lineage
            ):
                continue
            # The novelty floor. Only for an idea whose novelty has actually
            # been *assessed*: an unassessed one is not below the floor, it is
            # unmeasured, and the cheap screen that measures it is the next
            # thing this allocation would buy.
            #
            # It was documented as "applied by the allocator" and read by
            # nothing -- an independent test audit found the identifier
            # appeared only in its own config line.
            novelty = item.dimensions.novelty
            if (
                novelty is not None
                and novelty < config.thresholds.novelty_floor
                and _screened(item)
            ):
                continue
            if item.spent >= config.bounds.idea_spend_ceiling_usd:
                continue
            if item.lineage_spent >= config.bounds.lineage_spend_ceiling_usd:
                continue
            chosen = item
            allocations.append(
                Allocation(
                    kind=ADVANCE_IDEA,
                    reason=item.reason,
                    utility=score,
                    idea_id=item.idea.idea_id,
                    stage=item.stage,
                )
            )
            break
        if chosen is None:
            break
        pool.remove(chosen)
        active.append(chosen.diversity)
        taken_lineage[chosen.idea.lineage_root] = (
            taken_lineage.get(chosen.idea.lineage_root, 0) + 1
        )
        remaining -= 1

    # Whatever is left over goes to exploration, up to the pool ceiling. A
    # portfolio with capacity and nothing to deepen should be generating, not
    # idling -- but only one explorer at a time, for the reason
    # `PortfolioStore.explorations_in_flight` gives. This branch is where it
    # mattered: it fires whenever there is a free slot and the pool is under
    # its *ceiling*, which on a quiet portfolio is every tick, so it bought an
    # explorer every cadence whether or not the last one had even started.
    if (
        remaining
        and candidate_pool < config.bounds.candidate_pool_ceiling
        and explorers_in_flight == 0
    ):
        explorer, why = choose_explorer(
            pending_seeds=pending_seeds,
            origin_counts=origin_counts,
            minable_failures=minable_failures,
        )
        if not any(item.kind == EXPLORE for item in allocations):
            allocations.append(
                Allocation(
                    kind=EXPLORE,
                    reason=f"capacity to spare: {why}",
                    explorer=explorer,
                    payload={"bucket": tick_bucket},
                )
            )
    return tuple(allocations)


def _screened(candidate: Candidate) -> bool:
    """Whether this idea's novelty is a measurement rather than a default.

    The floor must not remove an idea nobody has looked at. A candidate whose
    next stage is still the cheap screen has not been looked at.
    """

    return candidate.stage not in {Stage.DEDUP, Stage.NOVELTY_SCREEN}


def idle_seconds(idea: PortfolioIdea, *, now: datetime | None = None) -> float:
    moment = now or datetime.now(UTC)
    return max(0.0, (moment - idea.updated_at).total_seconds())


def allocatable(idea: PortfolioIdea) -> bool:
    """Whether the allocator may spend on this idea at all.

    ``HUMAN_READY`` is excluded here as well as by ``occupies_capacity``, and
    the two are different statements: the first is "do not spend on it", the
    second is "it does not hold a slot". Together they are why one idea waiting
    for the researcher neither costs money nor stops anything else.
    """

    return (
        idea.status
        not in {
            IdeaStatus.REJECTED,
            IdeaStatus.SUPERSEDED,
            IdeaStatus.PARKED,
            IdeaStatus.HUMAN_READY,
        }
        and idea.operational_state.name == "IDLE"
    )
