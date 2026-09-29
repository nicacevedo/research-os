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

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from research_os.portfolio import digests as pdigests
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.models import (
    Feasibility,
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
#: One open frontier request turned into the new ideas it raises. Same
#: string as `research_os.portfolio.frontier.FOLLOW_UP`.
FOLLOW_UP = "portfolio_follow_up"
#: One open literature request answered from retrieved sources. Same string
#: as `research_os.portfolio.litintel.LITERATURE_REQUEST`.
LITERATURE_REQUEST = "portfolio_literature"
#: One synthesis of the reviewed evidence, written and refereed. Same string
#: as `research_os.portfolio.synthesis.SYNTHESIZE`.
SYNTHESIZE = "portfolio_synthesize"


#: The work kinds that make model calls, and so need budget authority.
MODEL_KINDS: tuple[str, ...] = (
    ADVANCE_IDEA,
    EXPLORE,
    FOLLOW_UP,
    LITERATURE_REQUEST,
    SYNTHESIZE,
)


#: The stages that deepen an admitted track rather than widen the portfolio.
#:
#: What one slot per tick is reserved for. The cheap ladder -- dedup, the
#: novelty screen, the falsifier, discovery, adjudication -- is how an idea
#: earns admission; these are what it is admitted *to*. BRANCH is absent on
#: purpose: it opens children, which is breadth, and the reservation exists
#: because breadth crowds depth out.
DEPTH_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.LITERATURE_AUDIT,
        Stage.EVIDENCE,
        Stage.REVIEW_BOARD,
        Stage.META_REVIEW,
        Stage.REPLICATE,
    }
)


class Lane(StrEnum):
    """Which of the portfolio's two purposes a unit of work serves.

    **Exploration** widens the portfolio: an explorer, a follow-up question
    turned into new ideas, a branch, and the cheap ladder -- dedup, the
    novelty screen, the falsifier, discovery, adjudication -- on an idea that
    has not yet earned admission. **Advancement** carries an admitted idea
    towards completion: the same ladder re-run on a revision of an idea
    already PROMISING or beyond, the literature audit, the evidence stage
    (contract, design, capability binding, plan, execution), the review
    board, the meta-review and the replication; and the two pieces of
    project work that serve ideas already in flight, a literature question
    and a synthesis of reviewed evidence.

    The first final qualification spent about 96 % of its authority on
    exploration while three admitted directions waited, so the split is
    explicit and the reserve below protects advancement from being starved
    by an unbounded stream of attractive shallow work.
    """

    EXPLORATION = "exploration"
    ADVANCEMENT = "advancement"


#: The stages an idea runs to *earn* admission. On a candidate they are
#: exploration; on an idea already PROMISING or beyond -- a revision re-running
#: the ladder, a sharpening, an adjudication -- they are how it advances.
ADMISSION_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.DISCOVER,
        Stage.ADJUDICATE,
    }
)

#: The statuses of an idea that has earned admission.
ADMITTED: frozenset[IdeaStatus] = frozenset(
    {
        IdeaStatus.PROMISING,
        IdeaStatus.INVESTIGATING,
        IdeaStatus.REVIEW,
        IdeaStatus.VALIDATED,
    }
)


def lane_of(
    kind: str, stage: Stage | str | None = None, status: IdeaStatus | None = None
) -> Lane:
    """The lane one unit of work belongs to. Ordinary code over its kind and stage.

    ``status`` is the idea's status when the work is bought. Unknown -- a
    queue row from before lanes were recorded -- reads the cheap ladder as
    exploration, which is the side that only makes the reserve stricter.
    """

    if kind in {LITERATURE_REQUEST, SYNTHESIZE}:
        return Lane.ADVANCEMENT
    if kind != ADVANCE_IDEA or not stage:
        return Lane.EXPLORATION
    try:
        which = Stage(str(stage))
    except ValueError:
        return Lane.EXPLORATION
    if which in DEPTH_STAGES:
        return Lane.ADVANCEMENT
    if which in ADMISSION_STAGES and status in ADMITTED:
        return Lane.ADVANCEMENT
    return Lane.EXPLORATION


@dataclass(frozen=True, slots=True)
class AdvancementPosition:
    """What the tick knows about the advancement lane, for one plan.

    ``protected_usd`` is the share of the human-set authority still held for
    advancement: ``fraction x ceiling`` less what advancement work has
    already committed (recorded spend, what its calls in flight hold, and one
    call ceiling per advancement item bought and not started). ``None`` when
    no finite monetary ceiling applies, which is what an absent budget row
    means everywhere in the ledger -- there is then no scarce authority to
    protect, and only the slot reservation applies.

    ``in_flight`` counts advancement work queued or running. While any is,
    advancement is eligible even if no advancement candidate can be bought
    this tick, so exploration cannot spend the reserve while a deep stage
    runs and leave the idea nothing to continue with.
    """

    protected_usd: Decimal | None = None
    in_flight: int = 0


def call_ceiling(
    kind: str, stage: Stage | str | None, config: PortfolioConfig
) -> Decimal:
    """The ``max_cost_usd`` one call of this work declares -- what it must reserve.

    The same number each handler passes to the router, read from the same
    configuration, so what the allocator sells and what the ledger is later
    asked to cover are one figure. Zero for work that makes no model call.
    """

    if kind == ADVANCE_IDEA:
        if not stage:
            return Decimal(0)
        return config.cost_for(Stage(stage))
    if kind == EXPLORE:
        return config.explorer_cost_usd
    if kind == FOLLOW_UP:
        return config.cost_for(Stage.BRANCH)
    if kind == LITERATURE_REQUEST:
        return config.cost_for(Stage.LITERATURE_AUDIT)
    if kind == SYNTHESIZE:
        return config.cost_for(Stage.REVIEW_BOARD)
    return Decimal(0)


def cheapest_call(config: PortfolioConfig) -> Decimal:
    """The smallest positive ceiling any portfolio model call declares.

    A budget with less than this left cannot authorise another call of any
    kind, which is what ``PAUSED_BUDGET_EXHAUSTED`` means.
    """

    ceilings = [value for value in config.stage_cost_usd.values() if value > 0]
    ceilings.append(config.explorer_cost_usd)
    positive = [value for value in ceilings if value > 0]
    return min(positive) if positive else Decimal(0)


@dataclass(frozen=True, slots=True)
class SaleAuthority:
    """What the project's and the system's ledgers can still authorise.

    Available capacity -- limit, less recorded spend, less what calls in
    flight hold -- less one call ceiling for every item already bought and
    not yet started, whose first call has reserved nothing yet. ``None`` in a
    dimension means no ceiling applies there, which is what an absent budget
    row means everywhere in the ledger.

    This is how a tick avoids *selling* work the ledger will refuse. It is not
    the boundary itself: the boundary is the ledger's reservation, taken per
    call in the same statement as the check, and it holds whatever this
    estimate says.
    """

    cost_usd: Decimal | None = None
    calls: int | None = None

    def covers(self, ceiling: Decimal, *, sold_cost: Decimal, sold_calls: int) -> bool:
        if ceiling <= 0:
            # A stage that makes no model call needs no model authority.
            return True
        if self.cost_usd is not None and self.cost_usd - sold_cost < ceiling:
            return False
        return self.calls is None or self.calls - sold_calls >= 1


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
    #: The idea version this work is for. Part of the dedup key, because a
    #: revision legitimately re-runs the cheap ladder against new content.
    idea_version: int | None = None
    #: How many times this ``(idea, stage, version)`` triple has already
    #: failed terminally. Part of the dedup key, and nothing else reads it.
    generation: int = 0
    #: The authority this sale took from the tick: the call ceiling of new
    #: work, and zero for work that makes no model call or for re-announcing
    #: an item the queue already holds. The invariant is that a queued item
    #: is charged once -- as queued exposure in the sale authority, by the
    #: tick that bought it -- and never again by a later plan; recorded so
    #: that it is asserted rather than inferred.
    charged: Decimal = Decimal(0)
    #: The lane this work serves, recorded on the queue item so what each lane
    #: has committed can be read back (`PortfolioStore.lane_spend`).
    lane: Lane = Lane.EXPLORATION

    @property
    def dedup_key(self) -> str:
        """What stops two ticks buying the same thing twice.

        For an idea advance it is the idea, the stage, and how many times that
        pair has already failed: the *action* row is the real idempotency (a
        partial unique index on the scientific basis), and this only has to
        stop two work items existing at once. For an explorer it is the
        explorer and the tick's own bucket, because two blind explorations in
        one tick is a duplicate and two in successive ticks is the system
        working.

        **The version is load-bearing and was missing, and that cost a whole
        soak.** A revision appends a new ``idea_versions`` row and the cheap
        ladder must run again against the new content -- ``dedup``,
        ``novelty_screen`` and ``falsify`` are all version-scoped in
        ``succeeded_stages_for_version``, so the stage machine correctly asks
        for them again. Without the version in the key they could not be
        bought: the key was spent by the *previous* version's run, which had
        SUCCEEDED, so ``on conflict do nothing`` refused it forever.

        Every idea that reached ``PROMISING`` and was then sharpened was
        therefore wedged permanently at the bottom of its own re-run ladder.
        The first soak's report blamed throughput for never reaching the
        literature audit or the review board; the throughput was real and
        this was the actual cause. It surfaced the moment ``work_refused``
        existed to show a tick allocating eight items and buying none.

        **The generation is load-bearing and was missing** -- first as a
        failure count, which is what this paragraph describes, and then as
        only that: an item that *succeeded* without running a stage spent the
        key the same way. It now counts every finished item for the stage
        (``PortfolioStore.advance_generations``). The sentence
        above says this key only has to stop two items existing *at once*, and
        without the count it did much more than that: ``work_items.dedup_key``
        is a permanent unique index and ``enqueue`` is ``on conflict do
        nothing``, so the first terminal failure of a pair spent its key for
        good. The allocator kept choosing that stage and the queue kept
        silently refusing it -- an hour of the first dogfood's ticks each
        deciding the same eight things and enqueueing none of them. Worse, it
        was unrecoverable: fixing the defect that caused the failures did not
        help, because the keys were still spent.

        A count and not a timestamp, because two ticks racing must compute the
        same key. ``Bounds.max_stage_failures`` is what stops it being an
        unbounded retry.
        """

        if self.kind == ADVANCE_IDEA:
            return (
                f"{self.kind}:{self.idea_id}:{self.stage}:"
                f"v{self.idea_version}:{self.generation}"
            )
        if self.kind == SYNTHESIZE:
            return (
                f"{self.kind}:{self.payload.get('basis', '')}:"
                f"{self.payload.get('generation', 0)}"
            )
        if self.kind in {FOLLOW_UP, LITERATURE_REQUEST}:
            # The request and its generation -- how many of its work items
            # already finished, read from the queue -- for the reason the
            # failure count is in an idea advance's key. It was the request's
            # `attempts` column, which only the handled failure paths bumped:
            # a provider outage, a spent budget or a crash spent the key, and
            # the oldest-first queue then refused every later request too.
            return (
                f"{self.kind}:{self.payload.get('request_id', '')}:"
                f"{self.payload.get('generation', 0)}"
            )
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
    #: What this idea has committed: recorded spend, plus what its calls in
    #: flight hold, plus one call ceiling per item bought for it and not yet
    #: started. Compared with its ceiling *including* the next stage's call.
    spent: Decimal
    #: The same, for its whole lineage.
    lineage_spent: Decimal
    #: How many times this idea's next stage has already failed terminally,
    #: on this version. Carried into the allocation so the dedup key can name
    #: the generation; the ceiling that stops it growing forever is applied in
    #: ``tick._candidates``, where the idea can also be marked blocked.
    generation: int = 0
    #: The queue already holds an unstarted item for exactly this stage.
    #:
    #: Re-announced rather than dropped -- the queue refuses the duplicate,
    #: and a pass whose every decision is refused is the signal that items
    #: are not being worked -- but not charged again: its call ceiling is
    #: already in ``spent``, ``lineage_spent`` and the sale authority.
    bought: bool = False
    #: Whether the declared capabilities can answer this idea's empirical
    #: question (`research_os.portfolio.feasibility`). Planning metadata, never
    #: evidence: it orders advancement work between otherwise equal ideas and
    #: changes nothing about what an idea is.
    feasibility: Feasibility = Feasibility.UNKNOWN

    @property
    def lane(self) -> Lane:
        return lane_of(ADVANCE_IDEA, self.stage, self.idea.status)


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

    # Completion resources are scarce, and an idea the declared capabilities
    # cannot answer would spend them on a refusal. Subtracted only in the
    # advancement lane, so a capability-limited idea still explores, is kept
    # in the bank, and is bought for advancement when nothing executable of
    # comparable value is waiting. Nothing about the idea itself changes.
    if (
        candidate.feasibility is Feasibility.CAPABILITY_LIMITED
        and candidate.lane is Lane.ADVANCEMENT
    ):
        score -= weights.capability_limited_penalty

    return Decimal(str(round(score, 6)))


def choose_explorer(
    *,
    pending_seeds: int,
    origin_counts: Mapping[IdeaOrigin, int],
    minable_failures: int,
    frontier_claims: int = 0,
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
    read = origin_counts.get(IdeaOrigin.LITERATURE_EXPLORER, 0)
    if frontier_claims and read * 2 <= blind:
        return "literature_explorer", (
            f"{frontier_claims} verified gap(s) or disagreement(s) in the literature "
            f"and only {read} idea(s) came from reading it"
        )
    return "blind_explorer", "keep generating directions that have not seen the bank"


def plan(
    *,
    candidates: Sequence[Candidate],
    config: PortfolioConfig,
    free_slots: int,
    lineage_in_flight: Mapping[str, int],
    candidate_pool: int,
    pending_seeds: int,
    origin_counts: Mapping[IdeaOrigin, int],
    minable_failures: int,
    tick_bucket: str,
    explorers_in_flight: int = 0,
    may_explore: bool = True,
    open_requests: Sequence[tuple[str, int, str]] = (),
    follow_ups_in_flight: int = 0,
    literature_requests: Sequence[tuple[str, int, str]] = (),
    literature_in_flight: int = 0,
    frontier_claims: int = 0,
    synthesis_basis: tuple[str, int] | None = None,
    syntheses_in_flight: int = 0,
    authority: SaleAuthority | None = None,
    advancement: AdvancementPosition | None = None,
) -> tuple[Allocation, ...]:
    """The ordered, bounded list of work this tick buys.

    Order of business, and it is the governing principle again: keep the pool
    from emptying, then spend what is left on the highest-utility work that
    every bound permits.

    **Every sale is charged against what it may cost, as it is made.** Each
    item takes its call ceiling out of ``authority`` and, for an idea, out of
    its own and its lineage's remaining ceiling, so the second thing a tick
    buys is judged against a budget that already paid for the first. A tick
    used to test every candidate against the same pre-tick sums, and a
    lineage one cent under its ceiling was sold a stage per free slot.

    **Exploration and advancement are two lanes** (:class:`Lane`), and while
    advancement work is eligible ``bounds.advancement_reserve_fraction`` of
    the portfolio is held for it in both currencies it can be starved of:

    - *slots* -- that fraction of the idea slots (rounded up) goes first to
      the best admissible advancement candidate, by the ordinary utility; a
      reserved slot no advancement candidate can take returns to the
      ordinary order;
    - *spend* -- exploration may not be sold into the protected share of the
      human-set authority (``advancement.protected_usd``). Advancement may
      use all of it, and what advancement buys uses the reserve up.

    Advancement is *eligible* when an advancement candidate could be bought
    this tick on its own bounds, or advancement work is already queued or
    running. When none is, exploration uses the whole authority as before:
    the reserve is borrowed, never idle, and it is back the moment an idea
    is admitted. With the fraction at zero the plan is exactly the earlier
    release's: one slot reserved for a depth stage and no spend reserve.
    """

    allocations: list[Allocation] = []
    remaining = max(0, free_slots)
    authority = authority or SaleAuthority()
    fraction = Decimal(str(config.bounds.advancement_reserve_fraction))
    sold_cost = Decimal(0)
    sold_calls = 0

    eligible = advancement_eligible(
        candidates=candidates,
        config=config,
        authority=authority,
        advancement=advancement,
    )
    #: What exploration may not be sold into, as the plan grows. `None` is no
    #: spend reserve: advancement is not eligible, the fraction is zero, or no
    #: finite ceiling applies.
    protected: Decimal | None = (
        advancement.protected_usd
        if eligible
        and advancement is not None
        and advancement.protected_usd is not None
        and authority.cost_usd is not None
        else None
    )

    def affordable(
        kind: str, stage: Stage | None = None, lane: Lane = Lane.EXPLORATION
    ) -> bool:
        ceiling = call_ceiling(kind, stage, config)
        if not authority.covers(ceiling, sold_cost=sold_cost, sold_calls=sold_calls):
            return False
        # The spend reserve: exploration is never sold into what is held for
        # advancement. Advancement itself may use everything.
        return not (
            lane is Lane.EXPLORATION
            and protected is not None
            and ceiling > 0
            and authority.cost_usd is not None
            and authority.cost_usd - sold_cost - ceiling < protected
        )

    def charge(
        kind: str, stage: Stage | None = None, lane: Lane = Lane.EXPLORATION
    ) -> Decimal:
        nonlocal sold_cost, sold_calls, protected
        ceiling = call_ceiling(kind, stage, config)
        if ceiling > 0:
            sold_cost += ceiling
            sold_calls += 1
            if lane is Lane.ADVANCEMENT and protected is not None:
                protected = max(Decimal(0), protected - ceiling)
        return ceiling

    # Project-level work first, and outside the idea slots. A follow-up, a
    # synthesis and a literature question each hold no idea's capacity --
    # they run one at a time per project, which is their bound -- and gating
    # them on a free idea slot did two wrong things at once: a portfolio
    # whose every track was running an experiment starved the literature
    # answers its blocked ideas were waiting on, and one with two free slots
    # spent both on project work and advanced no idea at all.
    #
    # A recorded question is the cheapest unit of genuinely new science the
    # portfolio can buy -- one call, about one event that already happened.
    # `open_requests` is ``(request_id, generation, basis)``, oldest first,
    # already filtered to the requests that may run now. A follow-up widens
    # the portfolio, so it is exploration and the reserve bounds it: that,
    # and not a count of ideas, is what stops recursion crowding completion
    # out while there is something admitted to complete.
    if open_requests and follow_ups_in_flight == 0 and affordable(FOLLOW_UP):
        request_id, generation, basis = open_requests[0]
        allocations.append(
            Allocation(
                kind=FOLLOW_UP,
                reason=f"an open {basis} request owes the frontier new ideas",
                payload={"request_id": request_id, "generation": generation},
                charged=charge(FOLLOW_UP),
                lane=Lane.EXPLORATION,
            )
        )
    # A synthesis, when the reviewed evidence changed. Its referee's findings
    # are what return the writing to the frontier, so it is bought as readily
    # as a question is -- one at a time, once per basis and generation.
    if (
        synthesis_basis
        and syntheses_in_flight == 0
        and affordable(SYNTHESIZE, lane=Lane.ADVANCEMENT)
    ):
        basis_digest, generation = synthesis_basis
        allocations.append(
            Allocation(
                kind=SYNTHESIZE,
                reason="the reviewed evidence changed since the last synthesis",
                payload={"basis": basis_digest, "generation": generation},
                charged=charge(SYNTHESIZE, lane=Lane.ADVANCEMENT),
                lane=Lane.ADVANCEMENT,
            )
        )
    # And a question put to the literature, the same way and for the same
    # reason: it is the cheapest thing that can unblock an idea waiting on it.
    if (
        literature_requests
        and literature_in_flight == 0
        and affordable(LITERATURE_REQUEST, lane=Lane.ADVANCEMENT)
    ):
        request_id, generation, basis = literature_requests[0]
        allocations.append(
            Allocation(
                kind=LITERATURE_REQUEST,
                reason=f"an open {basis} question for the literature",
                payload={"request_id": request_id, "generation": generation},
                charged=charge(LITERATURE_REQUEST, lane=Lane.ADVANCEMENT),
                lane=Lane.ADVANCEMENT,
            )
        )

    # The advancement share of the idea slots, counted before anything takes
    # one: ``ceil(fraction x free slots)``, while an advancement candidate is
    # waiting. The pool-floor explorer below may not take it -- with one free
    # slot it would, on every tick the pool was short, and the reserve would
    # hold nothing. A share no advancement candidate can take this tick goes
    # back to the ordinary order, and the explorer after it.
    share = (
        math.ceil(fraction * remaining)
        if fraction > 0
        and remaining
        and any(item.lane is Lane.ADVANCEMENT for item in candidates)
        else 0
    )
    if (
        may_explore
        and remaining > share
        and candidate_pool < config.bounds.candidate_pool_floor
        and explorers_in_flight == 0
        and affordable(EXPLORE)
    ):
        explorer, why = choose_explorer(
            pending_seeds=pending_seeds,
            origin_counts=origin_counts,
            minable_failures=minable_failures,
            frontier_claims=frontier_claims,
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
                charged=charge(EXPLORE),
                lane=Lane.EXPLORATION,
            )
        )
        remaining -= 1

    # Recomputed as the plan grows, so the second thing allocated is judged
    # against a portfolio that already contains the first. Without this a tick
    # with four free slots would allocate four ideas from one lineage, each
    # scored against the same unchanged active set.
    active: list[DiversityKey] = []
    # Tracks *running* per lineage, plus what this plan takes. Not the ideas a
    # lineage has alive: that is the bound on creating members, and reading it
    # here froze every full lineage -- see
    # `PortfolioStore.lineage_in_flight_counts`.
    taken_lineage = dict(lineage_in_flight)
    #: What this plan has already charged to each lineage's ceiling.
    sold_lineage: dict[str, Decimal] = {}
    pool = list(candidates)
    # **Slots reserved for completion.** The utility is a breadth-first
    # number by construction -- it subtracts a stage's cost and divides its
    # decisiveness by the idea's open objections, and an idea that has
    # survived the falsifier twice carries ten of them -- so a steady supply
    # of fresh children, every one of which scores near 3.0, outranked a
    # PROMISING, empirically adjudicated idea waiting on its literature audit
    # (2.12) on every tick of the first clean qualification, and no track
    # ever reached a contract. The earlier release reserved one slot for a
    # depth stage; the first *final* qualification then showed one slot was
    # not enough -- an admitted idea still had to win its sharpening, its
    # re-run falsifier and its adjudication against breadth, and 96 % of the
    # spend went to breadth. So with a reserve fraction the first
    # ``ceil(fraction x slots)`` go to the best admissible *advancement*
    # candidate; at fraction zero, one slot to the best depth stage, as
    # before. A reserved slot no candidate can take returns to the ordinary
    # order: breadth is still how the portfolio finds what to deepen.
    #
    # At fraction zero an item already queued does not take the depth slot:
    # its need is served, and the slot is for a track that is not. With a
    # reserve the advancement *share* of the slots includes advancement work
    # already queued -- a queued item is re-announced and occupies a slot
    # like any other -- so a second pass over the same state reserves the
    # same items in the same order, and queued breadth never takes a share
    # it does not have.
    if fraction > 0:

        def reservable(item: Candidate) -> bool:
            return item.lane is Lane.ADVANCEMENT

        reserved_left = min(share, remaining)
        tag = "a slot reserved for advancement"
    else:

        def reservable(item: Candidate) -> bool:
            return item.stage in DEPTH_STAGES and not item.bought

        reserved_left = 1 if any(reservable(item) for item in pool) else 0
        tag = "the slot reserved for depth"
    while remaining > 0 and pool:
        scored = [(utility(item, config=config, active=active), item) for item in pool]
        # Ties broken by idea id, so the plan is a function of state and not of
        # dictionary order.
        scored.sort(key=lambda pair: (-pair[0], pair[1].idea.idea_id))
        reserved = reserved_left > 0
        if reserved:
            reserved_left -= 1
            scored = [pair for pair in scored if reservable(pair[1])]
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
            if _below_floor(item, config):
                continue
            # The next stage's call ceiling must fit under the idea's and
            # the lineage's ceilings *after* what is committed and what this
            # plan already sold -- not merely "not yet at the ceiling", which
            # sold a 2.50 stage to a lineage with a cent left.
            charged = Decimal(0)
            lane = item.lane
            if not item.bought:
                exposure = call_ceiling(ADVANCE_IDEA, item.stage, config)
                if item.spent + exposure > config.bounds.idea_spend_ceiling_usd:
                    continue
                root = item.idea.lineage_root
                if (
                    item.lineage_spent + sold_lineage.get(root, Decimal(0)) + exposure
                    > config.bounds.lineage_spend_ceiling_usd
                ):
                    continue
                if not affordable(ADVANCE_IDEA, item.stage, lane):
                    continue
                charged = charge(ADVANCE_IDEA, item.stage, lane)
                sold_lineage[root] = sold_lineage.get(root, Decimal(0)) + exposure
            chosen = item
            allocations.append(
                Allocation(
                    kind=ADVANCE_IDEA,
                    reason=(f"{item.reason} [{tag}]" if reserved else item.reason),
                    utility=score,
                    idea_id=item.idea.idea_id,
                    stage=item.stage,
                    idea_version=item.idea.current_version,
                    generation=item.generation,
                    charged=charged,
                    lane=lane,
                )
            )
            break
        if chosen is None and reserved:
            # No reservable track is admissible this tick; the slots are
            # ordinary.
            reserved_left = 0
            continue
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
        may_explore
        and remaining
        and candidate_pool < config.bounds.candidate_pool_ceiling
        and explorers_in_flight == 0
    ):
        explorer, why = choose_explorer(
            pending_seeds=pending_seeds,
            origin_counts=origin_counts,
            minable_failures=minable_failures,
            frontier_claims=frontier_claims,
        )
        if not any(item.kind == EXPLORE for item in allocations) and affordable(
            EXPLORE
        ):
            allocations.append(
                Allocation(
                    kind=EXPLORE,
                    reason=f"capacity to spare: {why}",
                    explorer=explorer,
                    payload={"bucket": tick_bucket},
                    charged=charge(EXPLORE),
                    lane=Lane.EXPLORATION,
                )
            )
    return tuple(allocations)


def advancement_eligible(
    *,
    candidates: Sequence[Candidate],
    config: PortfolioConfig,
    authority: SaleAuthority | None,
    advancement: AdvancementPosition | None,
) -> bool:
    """Whether the advancement reserve is in force for this state.

    True when the reserve fraction is positive and either advancement work
    is already queued or running, or some advancement candidate could be
    bought on its own bounds. *Its own bounds*: lineage room is not asked --
    it is taken by running tracks that end, and an idea waiting for it is
    waiting, not blocked -- and everything that does not free itself is: the
    novelty floor, the idea's and its lineage's ceilings, and whether the
    whole authority could pay for the next call at all. Read by the plan and
    by the tick's report, so the two cannot disagree.
    """

    fraction = Decimal(str(config.bounds.advancement_reserve_fraction))
    if fraction <= 0:
        return False
    if advancement is not None and advancement.in_flight > 0:
        return True
    whole = authority or SaleAuthority()
    for item in candidates:
        if item.lane is not Lane.ADVANCEMENT:
            continue
        if item.bought:
            return True
        if _below_floor(item, config):
            continue
        exposure = call_ceiling(ADVANCE_IDEA, item.stage, config)
        if item.spent + exposure > config.bounds.idea_spend_ceiling_usd:
            continue
        if item.lineage_spent + exposure > config.bounds.lineage_spend_ceiling_usd:
            continue
        if whole.covers(exposure, sold_cost=Decimal(0), sold_calls=0):
            return True
    return False


def _below_floor(candidate: Candidate, config: PortfolioConfig) -> bool:
    """Whether an idea's *assessed* novelty is below the floor the plan skips."""

    novelty = candidate.dimensions.novelty
    return (
        novelty is not None
        and novelty < config.thresholds.novelty_floor
        and _screened(candidate)
    )


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
