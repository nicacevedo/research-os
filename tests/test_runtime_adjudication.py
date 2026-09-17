"""An unresolved target is not automatically an experiment.

The defect these tests pin. ``planner@5`` stopped the runtime freezing a
seventh specification for a hypothesis that already had six. It did not stop
the *first* one being the wrong kind of work: the live thesis runtime designed
six experiments for HYP-0002, a biconditional about when an infimum is finite,
which no measurement can decide. The runtime knew only that the hypothesis was
unresolved, and its only verb for an unresolved hypothesis was
``design_experiment``.

Two halves are tested here and both have to hold:

.. code-block:: text

    classification    from the project's own statement and falsifier,
                      deterministic, never from a model
    routing           a deterministic refusal, after every authority check,
                      that names the action which *would* answer the question

The four thesis hypotheses appear below because they are the real cases. So do
targets from an unrelated domain, because a classifier that only works on this
project is a lookup table with extra steps.
"""

from __future__ import annotations

import pytest

from research_os.runtime.adjudication import (
    Adjudication,
    AdjudicationKind,
    adjudication_view,
    classify,
    classify_object,
    unresolved_adjudications,
)

# --- the real thesis targets, verbatim from the capsule ---------------------
#
# Copied rather than read from disk: these tests must keep passing after the
# researcher edits their own capsule, and what is being pinned is that *this
# text* classifies this way.
HYP_0002 = (
    (
        "Under the model's scaling convention, the pricing subproblem at a master "
        "dual psi has a finite infimum if and only if ||X' psi||_inf <= lambda_1, "
        "which is exactly the feasibility constraint of the dual of the full LASSO; "
        "and when it is finite the infimum is zero."
    ),
    (
        "Exhibit a psi with ||X' psi||_inf > lambda_1 at which the pricing infimum "
        "is finite, or a psi with ||X' psi||_inf <= lambda_1 at which it is minus "
        "infinity, or a finite infimum that is non-zero."
    ),
)
HYP_0003 = (
    (
        "With unrestricted master weights and signed-unit-vector columns, the "
        "restricted master's reachable set is the span of the selected coordinates, "
        "so the method is the LASSO restricted to a coordinate working set, and "
        "selecting the v largest |(X' psi)_i| is the maximum-violation rule. It is "
        "therefore not a new column-generation scheme but a conic-solver "
        "implementation of a known working-set method."
    ),
    (
        "Exhibit a generated column that is not a signed unit vector under the "
        "default configuration, or a reachable master solution whose support is not "
        "contained in the selected coordinate set, or a published working-set "
        "method whose selection rule this provably differs from."
    ),
)
HYP_0004 = (
    (
        "Restricting the pricing problem to ||beta||_2 <= 1 raises the infimum of a "
        "minimisation, so the resulting value is an upper bound on the Lagrangian "
        "dual function and not a lower bound on the primal optimum. There exist "
        "reachable dual points at which it exceeds the primal optimum."
    ),
    (
        "Prove that the unit-ball pricing value is always at most the primal "
        "optimum, for every dual point the algorithm can reach; or show that the "
        "primal supplies a matching bound on ||beta||_2 that makes the restriction "
        "valid."
    ),
)
HYP_0006 = (
    (
        "The implemented method stops on a dual-stall heuristic rather than on a "
        "pricing optimality certificate, because its boundedness test treats the "
        "KKT equality |a_i| = lambda_1 -- which holds exactly on the support at "
        "optimality -- as unboundedness."
    ),
    (
        "Exhibit a run that terminates through the pricing test rather than through "
        "the dual-stall or no-new-column criteria, under the default tolerance."
    ),
)
HYP_0007 = (
    (
        "In the implemented method the per-iteration cost is dominated by the "
        "restricted master conic solve rather than by forming X' psi, so "
        "approximate, stochastic or GPU matrix-vector machinery would address the "
        "wrong term."
    ),
    (
        "Profile the method and find X' psi accounting for a larger share of per-"
        "iteration wall time than the master solve, in any preregistered instance "
        "regime."
    ),
)


# --- 1. the four regression cases §4.4 names --------------------------------
@pytest.mark.parametrize(
    ("name", "target", "expected"),
    [
        ("HYP-0002 pricing biconditional", HYP_0002, AdjudicationKind.MATHEMATICAL),
        ("HYP-0003 mechanism plus priority", HYP_0003, AdjudicationKind.MIXED),
        ("HYP-0004 bound direction", HYP_0004, AdjudicationKind.MATHEMATICAL),
        ("HYP-0006 termination behaviour", HYP_0006, AdjudicationKind.DIAGNOSTIC),
        ("HYP-0007 profiling", HYP_0007, AdjudicationKind.EMPIRICAL),
    ],
)
def test_the_thesis_targets_classify_by_how_they_could_be_settled(
    name: str, target: tuple[str, str], expected: AdjudicationKind
) -> None:
    statement, falsification = target
    verdict = classify(statement=statement, falsification=falsification)
    assert verdict.kind is expected, f"{name}: {verdict.reason()}"


def test_a_mathematical_target_is_not_decidable_by_measurement() -> None:
    """The property the routing guard actually consults."""

    assert not classify(*HYP_0002).settles_by_measurement
    assert not classify(*HYP_0004).settles_by_measurement


def test_an_empirical_target_still_admits_an_experiment() -> None:
    """§4.4's fifth case: the fix must not make everything unmeasurable."""

    assert classify(*HYP_0007).settles_by_measurement


def test_novelty_routes_away_from_measurement() -> None:
    verdict = classify(
        statement="This selection rule is a new contribution.",
        falsification=(
            "Find a published pre-2015 source in the literature stating the "
            "same known rule, which would show it is not novel."
        ),
    )
    assert verdict.kind is AdjudicationKind.NOVELTY_OR_LITERATURE
    assert not verdict.settles_by_measurement


# --- 2. it is not a lookup table for this one project -----------------------
@pytest.mark.parametrize(
    ("statement", "falsification", "expected"),
    [
        (
            "Cortisol declines monotonically across the first waking hour.",
            (
                "Measure salivary cortisol in 40 participants over five "
                "repetitions and observe a non-monotone median trajectory."
            ),
            AdjudicationKind.EMPIRICAL,
        ),
        (
            (
                "The estimator is unbiased for every sampling distribution with "
                "finite variance."
            ),
            (
                "Prove that the expectation differs from the parameter for some "
                "distribution, or exhibit a counterexample."
            ),
            AdjudicationKind.MATHEMATICAL,
        ),
        (
            "This annealing schedule has not appeared before.",
            (
                "Locate a published paper in the prior art literature using the "
                "same known schedule."
            ),
            AdjudicationKind.NOVELTY_OR_LITERATURE,
        ),
    ],
)
def test_targets_from_an_unrelated_domain_classify_by_the_same_rule(
    statement: str, falsification: str, expected: AdjudicationKind
) -> None:
    assert classify(statement=statement, falsification=falsification).kind is expected


def test_the_falsifier_outweighs_the_statement() -> None:
    """The asymmetry the whole method rests on.

    A statement full of mathematical vocabulary whose falsifier asks for a
    measurement is an empirical target: the falsifier is where the project
    wrote down what would settle it.
    """

    verdict = classify(
        statement=(
            "The infimum of the objective is attained, and the bound is exactly "
            "equivalent to the dual condition."
        ),
        falsification=(
            "Measure the wall time over five repetitions per seed and find a "
            "median above the preregistered threshold in any instance regime."
        ),
    )
    assert verdict.kind is AdjudicationKind.EMPIRICAL


# --- 3. conservative where it does not know ---------------------------------
def test_text_with_no_signal_is_undetermined_and_blocks_nothing() -> None:
    verdict = classify(statement="The thing is the case.", falsification="It is not.")
    assert verdict.kind is AdjudicationKind.UNDETERMINED
    # The property that makes an unclassifiable target behave exactly as it did
    # before this module existed.
    assert verdict.settles_by_measurement


def test_an_empty_target_is_undetermined_rather_than_an_error() -> None:
    assert classify().kind is AdjudicationKind.UNDETERMINED
    assert classify(statement="", falsification="").matched == ()


def test_two_comparable_signals_give_mixed_rather_than_a_coin_flip() -> None:
    verdict = classify(
        statement="A derivation would settle whether this is already published.",
        falsification="Prove it, or find the published prior art.",
    )
    assert verdict.kind is AdjudicationKind.MIXED


# --- 4. deterministic, and auditable ----------------------------------------
def test_the_same_text_classifies_the_same_way_every_time() -> None:
    """No clock, no model, no randomness: a hundred calls, one answer."""

    verdicts = {classify(*HYP_0003).kind for _ in range(100)}
    assert verdicts == {AdjudicationKind.MIXED}


def test_a_verdict_reports_the_words_it_matched() -> None:
    """A classification a person cannot check is one they have to accept."""

    verdict = classify(*HYP_0004)
    assert "prove" in verdict.matched
    assert "for every" in verdict.matched
    # The words, not the patterns that found them.
    assert not any("\\b" in word or "(?:" in word for word in verdict.matched)
    assert "prove" in verdict.reason()


def test_classification_is_case_insensitive() -> None:
    lower = classify(statement="prove that x", falsification="derive y")
    upper = classify(statement="PROVE THAT X", falsification="DERIVE Y")
    assert lower.kind is upper.kind is AdjudicationKind.MATHEMATICAL


# --- 5. the words deliberately left out -------------------------------------
def test_exhibit_alone_does_not_decide_anything() -> None:
    """It opens a mathematical falsifier and an empirical one alike.

    HYP-0002 asks to "exhibit a psi"; HYP-0001 asks to "exhibit one
    preregistered instance regime". A classifier keying on the verb would put
    them in the same box, and they are not in the same box.
    """

    mathematical = classify(*HYP_0002)
    empirical = classify(
        statement="Modern solvers dominate this method everywhere.",
        falsification=(
            "Exhibit one preregistered instance regime, with threads controlled "
            "and at least five repetitions, in which the median wall time is "
            "lower than every modern baseline's."
        ),
    )
    assert mathematical.kind is AdjudicationKind.MATHEMATICAL
    assert empirical.kind is AdjudicationKind.EMPIRICAL


# --- 6. reading a capsule object --------------------------------------------
class _FakeHypothesis:
    statement = HYP_0002[0]
    falsification = HYP_0002[1]


class _TitleOnly:
    title = "Prove the bound holds for every input"
    statement = ""
    falsification = ""


def test_an_object_is_classified_from_the_text_it_carries() -> None:
    assert classify_object(_FakeHypothesis()).kind is AdjudicationKind.MATHEMATICAL


def test_an_object_with_only_a_title_still_classifies() -> None:
    assert classify_object(_TitleOnly()).kind is AdjudicationKind.MATHEMATICAL


class _ExplodingKernel:
    def by_id(self) -> dict[str, object]:
        raise RuntimeError("the capsule is unreadable")


class _OneObjectKernel:
    """Counts validations, because doing one per target was a real defect."""

    def __init__(self) -> None:
        self.calls = 0

    def by_id(self) -> dict[str, object]:
        self.calls += 1
        return {"HYP-0002": _FakeHypothesis()}


def test_an_unreadable_capsule_is_undetermined_rather_than_a_cycle_failure() -> None:
    """Planning context. Losing one entry of it must never fail a cycle."""

    verdicts = unresolved_adjudications(_ExplodingKernel(), ["HYP-0002"])
    assert verdicts == (("HYP-0002", Adjudication(kind=AdjudicationKind.UNDETERMINED)),)


def test_an_id_the_capsule_does_not_hold_is_undetermined() -> None:
    verdicts = unresolved_adjudications(_OneObjectKernel(), ["HYP-9999"])
    assert verdicts[0][1].kind is AdjudicationKind.UNDETERMINED


def test_the_whole_frontier_costs_one_capsule_validation() -> None:
    """The first version called ``kernel.object()`` per identifier.

    Each of those is a full parse-and-validate of every file in the capsule, so
    a frontier of twenty objects became twenty validations on every planning
    call, and the suite's wall time roughly tripled.
    """

    kernel = _OneObjectKernel()
    unresolved_adjudications(kernel, [f"HYP-{n:04d}" for n in range(20)])
    assert kernel.calls == 1


# --- 7. what the planner is handed ------------------------------------------
def test_the_planner_view_says_noncanonical_and_names_the_instrument() -> None:
    view = adjudication_view("HYP-0002", classify(*HYP_0002))
    assert view["noncanonical"] is True
    assert view["settled_by"] == str(AdjudicationKind.MATHEMATICAL)
    assert view["experiment_can_decide_it"] is False
    assert "infimum" in str(view["basis"])
