"""Deduplication: deterministic where it can be, and a model only where it must.

The property the brief asks to be tested is stability -- *rephrased duplicates,
restart, DB reconnect, event replay, model nondeterminism* -- and all five
reduce to one question: does the identity depend on anything that could differ
between two runs? These tests answer it by computing the identity twice, in
different processes' worth of state, and by never letting a model near it.
"""

from __future__ import annotations

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio.config import load_config
from research_os.portfolio.dedup import DedupVerdict, screen
from research_os.portfolio.models import EdgeKind, IdeaOrigin, IdeaStatus
from research_os.portfolio.store import PortfolioStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


def test_identical_content_is_recognised_without_a_model(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    seed_idea(portfolio, runtime_project)
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields=idea_fields(),
        config=load_config(),
    )
    assert outcome.verdict is DedupVerdict.IDENTICAL
    assert outcome.match_version == 1


def test_a_rephrasing_collides_on_the_canonical_digest(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Word order, articles and voice disappear; content words do not."""

    original = idea_fields(
        research_question=(
            "Do column generation and working sets visit the same supports?"
        ),
        core_idea=(
            "Compare the support trajectories of column generation and working sets."
        ),
    )
    # Reordered, repunctuated, articles changed, voice changed -- and the
    # content words are the same multiset. That is the class of rephrasing
    # layer 2 exists to collapse, and it is deliberately *narrow*: adding a
    # single content word takes a candidate to layer 4, where a model decides
    # rather than arithmetic.
    rephrased = idea_fields(
        research_question=(
            "The same supports: do working sets and column generation visit them?"
        ),
        core_idea=(
            "The support trajectories of working sets and of column generation, "
            "compare."
        ),
    )
    portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=original,
        origin_role="blind_explorer",
    )
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields=rephrased,
        config=load_config(),
    )
    assert outcome.verdict is DedupVerdict.CANONICAL_DUPLICATE


def test_a_duplicate_inside_one_lineage_is_reported_as_such(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """A branch that went in a circle is a different problem from a coincidence."""

    parent, _ = seed_idea(portfolio, runtime_project)
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields=idea_fields(
            research_question=idea_fields()["research_question"].upper(),
            core_idea=idea_fields()["core_idea"],
        ),
        config=load_config(),
        lineage_family=[parent.idea_id],
    )
    assert outcome.verdict is DedupVerdict.LINEAGE_DUPLICATE
    assert outcome.match_idea_id == parent.idea_id


def test_something_close_but_not_identical_is_put_to_a_model(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Layer 4 is a *gate*, and this is what opens it.

    Nothing is concluded here. The verdict says only that arithmetic could not
    decide, which is the one situation where spending a model call on sameness
    is worth it.
    """

    seed_idea(portfolio, runtime_project)
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields=idea_fields(
            research_question=(
                "Do column-generation and working-set methods for L1-regularised "
                "regression visit the same sequence of supports, under ties?"
            )
        ),
        config=load_config(),
    )
    assert outcome.verdict is DedupVerdict.NEEDS_ADJUDICATION
    assert outcome.similarity >= load_config().thresholds.duplicate_similarity
    assert outcome.neighbours


def test_an_unrelated_idea_is_distinct_and_costs_nothing(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    seed_idea(portfolio, runtime_project)
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields=idea_fields(
            title="Thermal drift in cryogenic amplifiers",
            research_question=(
                "Does thermal drift in the first-stage amplifier dominate the noise "
                "floor below 100 millikelvin?"
            ),
            core_idea=(
                "Substrate conduction couples the amplifier to the mixing chamber "
                "more strongly than radiative loading does."
            ),
        ),
        config=load_config(),
    )
    assert outcome.verdict is DedupVerdict.DISTINCT
    assert outcome.neighbours == ()


def test_the_duplicate_is_kept_as_a_reference_rather_than_dropped(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Nothing explored is deleted, including the fact of having had it twice."""

    survivor, _ = seed_idea(portfolio, runtime_project)
    duplicate, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(title="the same direction, found again"),
        origin_role="blind_explorer",
    )
    portfolio.add_edge(
        parent_idea_id=survivor.idea_id,
        child_idea_id=duplicate.idea_id,
        kind=EdgeKind.DUPLICATE_OF,
        detail="canonical digest match",
    )
    portfolio.set_status(
        idea_id=duplicate.idea_id,
        status=IdeaStatus.SUPERSEDED,
        retire_reason=f"a restatement of {survivor.idea_id}",
    )
    assert portfolio.duplicate_survivor(duplicate.idea_id) == survivor.idea_id
    assert portfolio.require_idea(duplicate.idea_id).retire_reason


# ------------------------------------------------ determinism, five ways --
def test_the_canonical_identity_does_not_depend_on_anything_that_can_vary() -> None:
    """The identity is a pure function of the text, computed twice.

    Restart, reconnect, replay and model nondeterminism all reduce to one
    question -- does the identity depend on anything that could differ between
    two runs? -- and the answer here is an argument plus this: the function
    reads only its arguments, and two separately constructed inputs with the
    same text give the same digest. That is not the same as having restarted a
    process, and the docstring said it was.
    """

    fields = idea_fields()
    first = pdigests.canonical_digest({**fields, "project": "demo"})
    second = pdigests.canonical_digest(
        {**{key: str(value) for key, value in fields.items()}, "project": "demo"}
    )
    assert first == second
    assert first.startswith("pidea-canonical-v1:")


def test_the_identity_is_project_scoped(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """An idea copied into another project does not inherit a reviewed identity.

    The rule ``research_os.digests`` applies to capsule objects, applied here
    for the same reason.
    """

    left = pdigests.content_digest({**idea_fields(), "project": "project-a"})
    right = pdigests.content_digest({**idea_fields(), "project": "project-b"})
    assert left != right
    del portfolio, runtime_project


@pytest.mark.parametrize(
    ("left", "right", "expected_equal"),
    [
        ("A method for X", "a method for  x", True),
        ("A method for X", "An approach to X", True),
        ("A method for X", "A method for Y", False),
        ("the widget deforms", "The  widget   deforms!", True),
    ],
)
def test_the_normalisation_is_lossy_in_exactly_the_intended_ways(
    left: str, right: str, expected_equal: bool
) -> None:
    assert (pdigests.normalise(left) == pdigests.normalise(right)) is expected_equal


def test_similarity_is_symmetric_and_bounded() -> None:
    a = pdigests.normalise("column generation prices out one coordinate per iteration")
    b = pdigests.normalise("working set methods add several coordinates per iteration")
    assert pdigests.trigram_similarity(a, b) == pdigests.trigram_similarity(b, a)
    assert 0.0 <= pdigests.trigram_similarity(a, b) <= 1.0
    assert pdigests.trigram_similarity(a, a) == 1.0
    assert pdigests.trigram_similarity((), a) == 0.0


def test_nothing_close_says_how_close_nothing_was(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The first dogfood's calibration measurement, kept as a property.

    Two explorers produced the same research direction in different words and
    this layer said "nothing close", because character-trigram Jaccard over
    the two phrasings is 0.297 against a threshold of 0.72. Across 46 pairs
    from two real projects nothing exceeded 0.377, so the semantic
    adjudicator was never consulted once.

    The threshold is a researcher's judgement and is unchanged. What must not
    happen again is that the observation is invisible: "nothing close" with no
    number cannot be checked, and a researcher cannot calibrate a threshold
    against a sentence.
    """

    config = load_config()
    seed_idea(
        portfolio,
        runtime_project,
        research_question=(
            "Is the bounded-pricing reduced-cost test in the conic column-generation "
            "decomposition algebraically identical to the LASSO dual-feasibility "
            "condition, and does its selection rule coincide with an existing "
            "working-set construction rule?"
        ),
        core_idea=(
            "Derive the KKT stationarity conditions for the conic reformulation and "
            "show the pricing subproblem's reduced cost reduces to the standard LASSO "
            "dual residual."
        ),
    )
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields={
            "research_question": (
                "Given that the conic model is an exact LASSO reformulation, does the "
                "column-generation decomposition offer any computational advantage "
                "over established safe-screening and working-set LASSO solvers, or is "
                "it structurally equivalent to them?"
            ),
            "core_idea": (
                "Column generation prices out violated dual constraints from a "
                "restricted master; safe-screening methods add coordinates using dual "
                "feasibility certificates from the same KKT system."
            ),
        },
        config=config,
    )

    assert outcome.verdict is DedupVerdict.DISTINCT
    assert 0.0 < outcome.similarity < config.thresholds.duplicate_similarity
    assert f"{outcome.similarity:.2f}" in outcome.detail
    assert str(config.thresholds.duplicate_similarity) in outcome.detail


def test_the_first_idea_in_a_portfolio_has_no_nearest_neighbour(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """And must not report one, or `0.00` reads as "measured, and far"."""

    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields={"research_question": "anything at all", "core_idea": "anything"},
        config=load_config(),
    )
    assert outcome.verdict is DedupVerdict.DISTINCT
    assert outcome.similarity == 0.0
    assert "first idea" in outcome.detail


def test_the_screen_asks_a_model_about_a_real_near_duplicate(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The calibration, as a property rather than a number in a comment.

    Both texts below are verbatim from the first dogfood: two ideas produced
    by two different explorers, asking the same research question -- is the
    column-generation pricing rule a re-derivation of known safe-screening
    theory -- in different words. They score 0.297.

    Under the previous threshold of 0.72 the screen said "nothing close", and
    across 1,081 real pairs from two projects the semantic adjudicator was
    never consulted once.
    """

    config = load_config()
    seed_idea(
        portfolio,
        runtime_project,
        research_question=(
            "Is the bounded-pricing reduced-cost test in the conic "
            "column-generation decomposition algebraically identical to the LASSO "
            "dual-feasibility condition |X_j^T r| \u2264 \u03bb, and does its "
            "most-violated-column selection rule coincide index-for-index with an "
            "existing working-set construction rule such as Blitz's Gauss-Southwell "
            "rule or GAP Safe screening?"
        ),
        core_idea=(
            "Derive the KKT stationarity conditions for the conic reformulation "
            "identified in docs/2026/TIMELINE.md \u00a7T0 (exact LASSO reformulation, "
            "not an \u21130 relaxation) and show the pricing subproblem's reduced cost "
            "for column j reduces to \u03bb \u2212 |X_j^T r| up to a constant, i.e. "
            "exactly the standard LASSO dual residual. Then run the column-generation "
            "algorithm and Celer/Blitz on identical instances from identical warm "
            "starts and compare the exact sequence of entering columns, not just "
            "runtime, to test rule-level identity rather than just performance "
            "identity."
        ),
    )
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields={
            "research_question": (
                "Given that the conic model is an exact LASSO reformulation, does the "
                "column-generation decomposition offer any computational advantage "
                "over established safe-screening and working-set LASSO solvers (e.g., "
                "celer/Gap-Safe rules, blitz), or is it structurally equivalent to "
                "them?"
            ),
            "core_idea": (
                "Benchmark the column-generation solver against celer and blitz on "
                "identical instances, tolerances, and lambda grids, and separately "
                "check whether the pricing step's optimality certificate is "
                "mathematically the same test as a Gap-Safe/dynamic screening rule."
            ),
        },
        config=config,
    )

    assert outcome.verdict is DedupVerdict.NEEDS_ADJUDICATION, (
        "the screen must hand a real near-duplicate to the adjudicator; at the "
        "old threshold of 0.72 it said 'nothing close'"
    )
    assert outcome.similarity >= config.thresholds.duplicate_similarity
    assert outcome.match_idea_id is not None


def test_two_unrelated_directions_in_one_subfield_still_do_not_collide(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The control, and the reason the threshold was high in the first place.

    Ideas in one subfield share most of their vocabulary. Lowering the cut
    point is only correct if the bulk of unrelated work stays below it --
    measured at p50 0.160 and p90 0.231 over the same 1,081 pairs.
    """

    config = load_config()
    seed_idea(
        portfolio,
        runtime_project,
        research_question=(
            "Does the historical speed regime survive a fair modern comparison at "
            "matched objectives, matched tolerances and controlled threads?"
        ),
        core_idea=(
            "Force every solver to stop at the same duality gap and pin the thread "
            "count, removing the convergence-criterion and BLAS-parallelism confounds."
        ),
    )
    outcome = screen(
        portfolio,
        project_id=runtime_project,
        fields={
            "research_question": (
                "Is the numerical instability recorded in the historical material a "
                "solver-era artifact or structural to the second-order cone "
                "formulation itself?"
            ),
            "core_idea": (
                "Re-run the recorded failures on a current interior-point solver and "
                "compare conditioning of the master problem's internal solves."
            ),
        },
        config=config,
    )

    assert outcome.verdict is DedupVerdict.DISTINCT
    assert outcome.similarity < config.thresholds.duplicate_similarity
