"""Portfolio configuration: the bounds, the weights, and the stage ceilings.

Its own file under the config home, `portfolio.yaml`, the way
`research_os.automation.config`, `research_os.experiment.config` and
`research_os.literature.config` each have one. Not a section of
`runtime.yaml`: `ConfigDocument` forbids extra keys, so adding one there would
mean the runtime's configuration schema knowing about the layer above it.

**Everything here is configuration, not scientific policy.** The brief is
explicit that "6-10 idea tracks per project" is a starting number rather than a
rule, and the same is true of every bound below. What is *not* configurable is
anything a quality gate reads as a requirement -- three reviews are three
reviews, a numerical witness is not a proof, and no YAML key turns either off.
The one number here a gate consults is ``novelty_min_sources``, and it has a
floor of 1 in the field definition so that configuration can make the
literature requirement stricter and cannot remove it.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_os.errors import ResearchOSError
from research_os.paths import config_home
from research_os.portfolio.models import Stage

CONFIG_FILENAME = "portfolio.yaml"


class PortfolioConfigError(ResearchOSError):
    """Raised when portfolio configuration is missing or unusable."""


def config_path() -> Path:
    return config_home() / CONFIG_FILENAME


class Bounds(BaseModel):
    """The anti-explosion controls. Every one of them a finite stop condition.

    Invariant 14 says no workflow may recurse or retry indefinitely, and a
    portfolio has six ways to do it that a single objective does not: breadth,
    lineage depth, branching factor, revision, spend, and generating forever
    without generating anything. One bound each. The sixth was found by
    running the tick twice against a real project and watching the queue
    grow.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: How many idea tracks may be in flight in one project at once. The
    #: brief's suggested starting range is 6-10; eight is the middle of it.
    max_active_tracks: int = Field(default=8, ge=1, le=200)
    #: How many ideas of one lineage family may be alive at once, so one early
    #: direction cannot become the whole portfolio.
    max_active_per_lineage: int = Field(default=3, ge=1, le=100)
    #: How many children one BRANCH disposition may open.
    max_children_per_branch: int = Field(default=3, ge=1, le=20)
    #: How deep a lineage may go without any descendant acquiring new evidence.
    #: Deepening forever on reasoning alone is the failure this bounds.
    max_depth_without_evidence: int = Field(default=3, ge=1, le=50)
    #: How many times one idea may be rewritten. "Revise until a stochastic
    #: reviewer stops objecting" is a loop, and this is its stop condition.
    max_revisions_per_idea: int = Field(default=4, ge=1, le=50)
    #: How many reviews one idea may accumulate across all versions, so a
    #: pathological revise/review cycle has a ceiling in reviewer spend too.
    max_reviews_per_idea: int = Field(default=24, ge=3, le=500)
    #: Total model spend one idea may attract, summed over its actions and
    #: checked by the allocator before a stage is enqueued.
    idea_spend_ceiling_usd: Decimal = Field(default=Decimal("8.00"), ge=0)
    #: The same, for a whole lineage family.
    lineage_spend_ceiling_usd: Decimal = Field(default=Decimal("40.00"), ge=0)
    #: How many CANDIDATE ideas the pool must hold before the allocator stops
    #: spending on exploration and starts spending on investigation.
    candidate_pool_floor: int = Field(default=6, ge=0, le=200)
    #: And the ceiling, so a run of explorers cannot fill the table.
    candidate_pool_ceiling: int = Field(default=40, ge=1, le=1_000)
    #: How many explorer runs may succeed without producing a single new idea
    #: before the portfolio stops exploring. The sixth loop, and the one the
    #: other five do not cover: every explorer generating a near-duplicate
    #: that the screen rejects leaves the pool below its floor forever, so the
    #: tick buys another explorer every cadence until the budget is gone.
    #: The budget does stop it, eventually, and "you have spent your ceiling"
    #: is the wrong diagnosis for "this project's idea space is exhausted".
    max_barren_explorations: int = Field(default=6, ge=1, le=100)


class Thresholds(BaseModel):
    """Where a number decides something, and what the number is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Trigram Jaccard above which a candidate is put to the semantic duplicate
    #: adjudicator. Below it, no model is asked and the candidate proceeds.
    #:
    #: 0.72 is a starting value, not a measurement. It is high enough that two
    #: genuinely different directions in one subfield -- which share most of
    #: their vocabulary -- do not collide, and low enough that a rephrasing
    #: does. The dogfood is what would calibrate it.
    duplicate_similarity: float = Field(default=0.72, ge=0.0, le=1.0)
    #: Distinct retrieved literature keys a deep novelty audit must produce
    #: before VALIDATED. A floor of 1 so configuration can only make the
    #: literature requirement stricter.
    novelty_min_sources: int = Field(default=3, ge=1, le=100)
    #: How long a review stays live. A parked idea unparked six months later
    #: has reviews nobody revisited and literature that has moved.
    review_max_age_seconds: int = Field(default=30 * 86_400, ge=3_600)
    #: Below this assessed novelty, an idea is not worth investigating. Applied
    #: by the allocator, not by a gate: it decides what to spend on next, and
    #: never what an idea *is*.
    novelty_floor: float = Field(default=0.25, ge=0.0, le=1.0)


class Weights(BaseModel):
    """The scheduling utility's coefficients.

    An operational number's ingredients. None of this reaches the scientific
    record, and the human-facing top-ideas list is a Pareto front rather than
    an ordering by this. See docs/adr/0004.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    novelty: float = 1.0
    potential_impact: float = 1.2
    plausibility: float = 0.8
    falsifiability: float = 0.6
    tractability: float = 0.8
    evidence_strength: float = 0.5
    #: Subtracted, scaled by how much of the active set shares this idea's
    #: diversity coordinates. The one term that is about the portfolio rather
    #: than about the idea.
    diversity_penalty: float = 1.5
    #: Subtracted, scaled by the stage's expected cost in dollars.
    cost: float = 0.4
    #: Added for an idea nothing has touched recently, so a portfolio does not
    #: converge on one lineage by neglect.
    staleness: float = 0.3
    #: Added for an idea whose only open question is cheap to close.
    decisiveness: float = 0.7


#: What one invocation of each stage may cost. A per-request ceiling, passed
#: to the router as ``max_cost_usd``, so a stage that goes wrong is bounded
#: before the idea ceiling has to catch it.
#:
#: The shape of the numbers is the governing principle as arithmetic: explore
#: broadly and kill cheaply are cents, deepen selectively is dollars, replicate
#: consequentially is the most expensive thing here.
DEFAULT_STAGE_COST_USD: dict[Stage, Decimal] = {
    Stage.DEDUP: Decimal("0.10"),
    Stage.NOVELTY_SCREEN: Decimal("0.25"),
    Stage.FALSIFY: Decimal("0.40"),
    Stage.DISCOVER: Decimal("0.50"),
    Stage.ADJUDICATE: Decimal("0.00"),
    Stage.EVIDENCE: Decimal("2.50"),
    Stage.LITERATURE_AUDIT: Decimal("1.50"),
    Stage.REVIEW_BOARD: Decimal("1.80"),
    Stage.META_REVIEW: Decimal("0.60"),
    Stage.REPLICATE: Decimal("2.50"),
    Stage.BRANCH: Decimal("0.50"),
}

#: The minimum quality tier an idea must hold before a stage may run on it.
#:
#: This is where "expensive evidence stages require stronger quality gates"
#: lives. ``ADJUDICATE`` is absent because it costs nothing and consults no
#: model -- it reads the falsifier.
STAGE_MINIMUM_STATUS: dict[Stage, str] = {
    Stage.EVIDENCE: "PROMISING",
    Stage.LITERATURE_AUDIT: "PROMISING",
    Stage.REVIEW_BOARD: "INVESTIGATING",
    Stage.META_REVIEW: "REVIEW",
    Stage.REPLICATE: "VALIDATED",
    Stage.BRANCH: "PROMISING",
}


#: What one explorer call may cost.
#:
#: Its own number rather than a stage's, because exploration is not a stage of
#: an idea -- it is what produces one. An earlier version charged it against
#: the deduplication ceiling, which is ten cents, and would have made every
#: explorer call on a real provider fail for being too expensive to make.
DEFAULT_EXPLORER_COST_USD = Decimal("0.60")


class Cadence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: How often the deterministic portfolio tick runs, per project. It makes
    #: no model call, so this is cheap; it is not free, because it queries.
    tick_seconds: int = Field(default=120, ge=60, le=86_400)
    #: How often a digest is produced.
    digest_seconds: int = Field(default=86_400, ge=3_600, le=30 * 86_400)
    #: How long an ACTIVE action may sit with no live work item before the tick
    #: treats it as stranded. The portfolio's analogue of
    #: ``run_reconcile_grace_seconds``, and chosen the same way: it must exceed
    #: one lease plus the largest retry delay.
    stale_action_grace_seconds: int = Field(default=600, ge=60, le=86_400)


class ConfigDocument(BaseModel):
    """The on-disk shape of ``portfolio.yaml``. Every key optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    bounds: Bounds | None = None
    thresholds: Thresholds | None = None
    weights: Weights | None = None
    cadence: Cadence | None = None
    stage_cost_usd: dict[str, Decimal] | None = None
    explorer_cost_usd: Decimal | None = None


class PortfolioConfig(BaseModel):
    """Resolved portfolio configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bounds: Bounds = Bounds()
    thresholds: Thresholds = Thresholds()
    weights: Weights = Weights()
    cadence: Cadence = Cadence()
    stage_cost_usd: dict[Stage, Decimal] = Field(
        default_factory=lambda: dict(DEFAULT_STAGE_COST_USD)
    )
    explorer_cost_usd: Decimal = DEFAULT_EXPLORER_COST_USD
    source: Path | None = None

    def cost_for(self, stage: Stage) -> Decimal:
        return self.stage_cost_usd.get(stage, DEFAULT_STAGE_COST_USD[stage])

    def with_overrides(self, bounds: Mapping[str, Any] | None) -> PortfolioConfig:
        """Apply one project's stored bound overrides.

        ``portfolio_state.bounds`` holds them, so a project that needs a
        different breadth says so in its own row rather than in the file every
        project shares. Unknown keys are refused rather than ignored: a
        misspelled bound that silently does nothing is worse than an error.
        """

        if not bounds:
            return self
        try:
            merged = Bounds.model_validate({**self.bounds.model_dump(), **dict(bounds)})
        except ValidationError as exc:
            raise PortfolioConfigError(
                f"project bound overrides are not valid: {exc}"
            ) from None
        return self.model_copy(update={"bounds": merged})


def load_config(path: Path | None = None) -> PortfolioConfig:
    """Load portfolio configuration, falling back to documented defaults.

    A missing default file is normal and yields defaults. A file that was
    *asked for* by path and is missing is an error, because the caller said
    where it was. Same contract as ``research_os.runtime.config.load_config``.
    """

    target = path or config_path()
    document = ConfigDocument()
    if target.exists():
        try:
            raw: Any = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise PortfolioConfigError(f"{target}: not valid YAML: {exc}") from None
        if not isinstance(raw, dict):
            raise PortfolioConfigError(f"{target}: expected a mapping at the top level")
        try:
            document = ConfigDocument.model_validate(raw)
        except ValidationError as exc:
            raise PortfolioConfigError(f"{target}: {exc}") from None
    elif path is not None:
        raise PortfolioConfigError(f"{target}: no such portfolio configuration file")

    costs = dict(DEFAULT_STAGE_COST_USD)
    for name, value in (document.stage_cost_usd or {}).items():
        try:
            costs[Stage(name)] = Decimal(value)
        except ValueError:
            raise PortfolioConfigError(
                f"{target}: {name!r} is not a stage. The stages are: "
                + ", ".join(sorted(item.value for item in Stage))
            ) from None
    return PortfolioConfig(
        bounds=document.bounds or Bounds(),
        thresholds=document.thresholds or Thresholds(),
        weights=document.weights or Weights(),
        cadence=document.cadence or Cadence(),
        stage_cost_usd=costs,
        explorer_cost_usd=(
            document.explorer_cost_usd
            if document.explorer_cost_usd is not None
            else DEFAULT_EXPLORER_COST_USD
        ),
        source=target if target.exists() else None,
    )
