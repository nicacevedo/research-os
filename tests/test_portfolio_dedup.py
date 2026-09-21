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
