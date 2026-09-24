"""Mutation-killing regressions for the frozen analysis and the contract around it.

An independent mutation review applied small, plausible defects to the three
places a scientific conclusion is computed or guarded -- the engine that
evaluates a frozen analysis (``portfolio.analysis``), the verifier that
re-hashes a contract (``portfolio.scicontract``) and the route that runs and
reads under one (``portfolio.empirical``) -- and every defect named below
survived the suite as it stood. Each test here fails with its mutation
applied and passes on the tree; the mutation id is in the docstring.

Three groups, by what they hold:

- **the engine** (no database): an interval decides only when all of it
  agrees; a regression reads the coefficient it was asked for; the support a
  contract demands is counted by value, over the records the statistic
  actually rests on; the summary statistics are the textbook ones; and a
  record, a selection or an uncertainty that cannot be evaluated makes the
  conclusion INSUFFICIENT rather than quietly smaller, zero, or certain;
- **the verifier** (the database, no subprocess): each half of a stored
  contract is re-hashed, not trusted, and a contract is used only for the
  hypothesis it names;
- **the route** (a real subprocess): what runs realises the frozen design
  and its preregistration names the contract, interpretation re-verifies
  what it reads under, a repair is bounded in size and in number, a
  replication inherits rather than re-asks, the experiment designer is not
  shown the bar even when the idea states it, and a result already committed
  to the repository is not read as this run's.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest

from research_os.portfolio import analysis as engine
from research_os.portfolio import empirical, scicontract
from research_os.portfolio.config import load_config
from research_os.portfolio.contracts import AnalysisSpec
from research_os.portfolio.models import (
    AdjudicationType,
    EmpiricalConclusion,
    EvidenceStrength,
    ExperimentRole,
    ExperimentState,
    IdeaExperiment,
    IdeaOrigin,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_scientific_contract import (
    _advance,
    _analysis_answer,
    _cells,
    _context,
    _design_answer,
    _frozen,
    _idea,
    _project,
    _router,
    _spec,
    grid_repo,
)

__all__ = [
    "grid_repo",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

SOURCE = "results/grid.json"

#: Ten noisy repeats of one cell: mean 1.1, and a 95% bootstrap interval of
#: roughly [0.8, 1.45] -- wide enough to straddle a threshold near the mean.
NOISY = (1.0, 1.4, 0.2, 2.2, 0.9, 1.1, 1.8, 0.4, 1.3, 0.7)


def _measured(values: Sequence[Any]) -> dict[str, Any]:
    """One record per value at a single grid cell: a repeated measurement."""

    return {
        "cells": [{"size": 1, "difficulty": 1, "iterations": value} for value in values]
    }


def _statistic(op: str = "mean", **overrides: Any) -> AnalysisSpec:
    """One field operation over ``iterations``, no support rule, no interval."""

    payload: dict[str, Any] = {
        "reductions": [
            {"name": "m", "op": op, "observable": "cells", "field": "iterations"}
        ],
        "primary_statistic": "m",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": -1.0},
        "support": [],
    }
    payload.update(overrides)
    return _spec(**payload)


def _analysis(**payload: Any) -> AnalysisSpec:
    """A frozen analysis over an observable of the test's own choosing."""

    spec = AnalysisSpec.model_validate(
        {
            "analysable": True,
            "estimand": "the share of runs that failed to converge",
            **payload,
        }
    )
    spec.check()
    return spec


# =================================================================== engine --
def test_a_refutation_must_hold_across_the_whole_interval() -> None:
    """AN01: the mirror of ``test_a_bootstrap_interval_must_clear_the_threshold_whole``.

    That test holds SUPPORTS to the whole interval; nothing held CONTRADICTS
    to it, so a mutation reading the failure predicate on the point estimate
    alone survived. The mean is below the failure threshold and the interval
    is not: a refutation the data cannot sustain is INCONCLUSIVE.
    """

    straddling = _statistic(
        uncertainty={"resamples": 400, "seed": 7},
        success={"comparator": ">", "threshold": 5.0},
        failure={"comparator": "<", "threshold": 1.2},
    )
    result = engine.evaluate(straddling, {SOURCE: _measured(NOISY)})
    assert result.statistic == pytest.approx(1.1)
    assert straddling.failure is not None and straddling.failure.holds(1.1)
    assert result.interval is not None
    assert result.interval[0] < 1.2 < result.interval[1], result.interval
    assert result.conclusion is EmpiricalConclusion.INCONCLUSIVE, result.summary
    assert any("straddles" in note for note in result.notes)

    # The control: when the whole interval is past the failure threshold,
    # the same data do contradict.
    cleared = _statistic(
        uncertainty={"resamples": 400, "seed": 7},
        success={"comparator": ">", "threshold": 5.0},
        failure={"comparator": "<", "threshold": 3.0},
    )
    assert engine.evaluate(cleared, {SOURCE: _measured(NOISY)}).conclusion is (
        EmpiricalConclusion.CONTRADICTS
    )


@pytest.mark.parametrize(
    ("terms", "coefficient", "expected"),
    [
        (["size", "difficulty", "size:difficulty"], "size", 2.0),
        (["size", "difficulty", "size:difficulty"], "difficulty", 3.0),
        (["size:difficulty", "size", "difficulty"], "size:difficulty", 0.5),
        (["size:difficulty", "size", "difficulty"], "size", 2.0),
    ],
)
def test_a_regression_reads_the_coefficient_it_was_asked_for(
    terms: list[str], coefficient: str, expected: float
) -> None:
    """AN03: every earlier test asked for the *last* term, the interaction.

    So ``_ols`` returning ``solution[-1]`` whatever was asked passed them
    all. Exact data (``10 + 2*size + 3*difficulty + 0.5*size*difficulty``)
    recover every coefficient exactly, wherever its term is listed.
    """

    spec = _spec(
        reductions=[
            {
                "name": "coef",
                "op": "ols_coefficient",
                "observable": "cells",
                "response": "iterations",
                "terms": terms,
                "coefficient": coefficient,
            }
        ],
        primary_statistic="coef",
    )
    result = engine.evaluate(spec, {SOURCE: _cells([1, 2, 4], [1, 3])})
    assert result.statistics["coef"] == pytest.approx(expected)
    assert result.statistic == pytest.approx(expected)


def test_three_and_three_point_zero_are_one_level_not_two() -> None:
    """AN05: distinct values are counted by value, not by spelling.

    A design that writes one instance size as ``3`` in one record and
    ``3.0`` in the next has one level of it; counting representations would
    let the co-design defence be satisfied by formatting. JSON and CSV both
    produce the int/float pair, so both are checked.
    """

    support = [{"observable": "cells", "min_distinct": {"size": 2}}]
    spec = _statistic(support=support)
    document = engine.parse_document(
        '{"cells": ['
        '{"size": 3, "difficulty": 1, "iterations": 20},'
        '{"size": 3.0, "difficulty": 2, "iterations": 22},'
        '{"size": 3, "difficulty": 3, "iterations": 24},'
        '{"size": 3.0, "difficulty": 4, "iterations": 26}]}',
        name="grid.json",
    )
    assert {type(row["size"]) for row in document["cells"]} == {int, float}
    result = engine.evaluate(spec, {SOURCE: document})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "'size' takes 1 distinct value(s)" in result.summary

    table = engine.parse_document(
        "size,difficulty,iterations\n3,1,20\n3.0,2,22\n3,3,24\n3.0,4,26\n",
        name="grid.csv",
    )
    assert {type(row["size"]) for row in table} == {int, float}
    csv_spec = _statistic(
        support=support,
        observables=[
            {
                "name": "cells",
                "source": "results/grid.csv",
                "kind": "records",
                "fields": ["size", "difficulty", "iterations"],
            }
        ],
    )
    from_csv = engine.evaluate(csv_spec, {"results/grid.csv": table})
    assert from_csv.conclusion is EmpiricalConclusion.INSUFFICIENT, from_csv.summary

    # The control: two sizes that really differ meet the requirement.
    document["cells"][1]["size"] = 4
    assert engine.evaluate(spec, {SOURCE: document}).conclusion is (
        EmpiricalConclusion.SUPPORTS
    )


def test_too_few_records_is_insufficient_whatever_they_show() -> None:
    """AN06: ``min_records`` is enforced, and the summary says by how much.

    Every earlier support test failed on ``min_distinct`` first, so deleting
    the record-count check changed no outcome.
    """

    spec = _statistic(support=[{"observable": "cells", "min_records": 12}])
    result = engine.evaluate(spec, {SOURCE: _measured(NOISY)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "'cells' has 10 analysed record(s) and the contract requires 12" in (
        result.summary
    )
    assert result.support[0]["met"] is False

    enough = _statistic(support=[{"observable": "cells", "min_records": 10}])
    assert engine.evaluate(enough, {SOURCE: _measured(NOISY)}).conclusion is (
        EmpiricalConclusion.SUPPORTS
    )


def _type7(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def _reference_bootstrap(
    values: Sequence[float], *, level: float, resamples: int, seed: int
) -> tuple[float, float]:
    """A percentile bootstrap of the mean, written out independently.

    The documented procedure: ``random.Random(seed)``, each resample draws
    ``n`` records with replacement by ``randrange(n)``, the statistic is
    recomputed, and the interval is the Hyndman-Fan type-7 quantile at
    ``(1 - level) / 2`` and its complement.
    """

    generator = random.Random(seed)
    means = []
    for _ in range(resamples):
        sample = [values[generator.randrange(len(values))] for _ in values]
        means.append(math.fsum(sample) / len(sample))
    tail = (1.0 - level) / 2.0
    return _type7(means, tail), _type7(means, 1.0 - tail)


def test_the_bootstrap_interval_is_the_documented_percentile_interval() -> None:
    """AN08: a 95% interval puts 2.5% in each tail, not 5%.

    The interval is a deterministic function of the data and the contract --
    that is what writing the seed down is for -- so it is pinned against an
    independent implementation of the documented procedure. A change to the
    procedure changes every interval ever recorded, and must arrive with a
    new ``ENGINE_VERSION`` and a new expectation here, not silently.
    """

    values = [((index * 7) % 13) / 4 for index in range(40)]
    expected = {
        level: _reference_bootstrap(values, level=level, resamples=1000, seed=11)
        for level in (0.9, 0.95)
    }
    assert expected[0.9] != pytest.approx(expected[0.95]), "the check must discern"
    for level in (0.9, 0.95):
        spec = _statistic(
            uncertainty={"resamples": 1000, "seed": 11, "level": level},
            success={"comparator": ">", "threshold": 0.5},
        )
        result = engine.evaluate(spec, {SOURCE: _measured(values)})
        assert result.interval == pytest.approx(expected[level], rel=1e-12), level


def test_the_bootstrap_interval_has_the_width_its_level_implies() -> None:
    """AN08, stated without the random number generator.

    For the mean of 200 roughly uniform values the percentile bootstrap is
    close to normal, so its width at level ``L`` is close to
    ``2 z_{(1+L)/2} sigma / sqrt(n)``. An interval computed with the tail
    not halved is 16% narrower at 0.95 -- far outside the tolerance --
    whatever resampling scheme produced it.
    """

    values = [((index * 37) % 101) / 10 for index in range(200)]
    mean = math.fsum(values) / len(values)
    sigma = math.sqrt(math.fsum((item - mean) ** 2 for item in values) / len(values))
    widths = {}
    for level in (0.5, 0.9, 0.95):
        spec = _statistic(
            uncertainty={"resamples": 3000, "seed": 20260915, "level": level},
            success={"comparator": ">", "threshold": 0.5},
        )
        result = engine.evaluate(spec, {SOURCE: _measured(values)})
        assert result.interval is not None
        width = result.interval[1] - result.interval[0]
        normal = 2 * NormalDist().inv_cdf((1 + level) / 2) * sigma / math.sqrt(200)
        assert 0.93 * normal <= width <= 1.07 * normal, (level, width, normal)
        widths[level] = width
    assert widths[0.5] < widths[0.9] < widths[0.95]


@pytest.mark.parametrize(
    ("constant", "value", "why"),
    [
        ("MAX_BOOTSTRAP_DRAWS", 10, "record draws"),
        ("MIN_FINITE_RESAMPLES", 1.01, "gave a defined statistic"),
        ("MIN_BOOTSTRAP_RECORDS", 1_000, "is not an interval"),
    ],
)
def test_an_uncertainty_that_cannot_be_computed_is_insufficient_not_ignored(
    monkeypatch: pytest.MonkeyPatch, constant: str, value: float, why: str
) -> None:
    """AN15: a preregistered interval that cannot be had is not waived.

    The contract said the conclusion rests on an interval. Falling back to
    the point estimate when the interval fails would read SUPPORTS from the
    one number the contract said was not enough. Each of the engine's three
    refusals is forced in turn, on data whose interval would otherwise
    support.
    """

    spec = _statistic(
        uncertainty={"resamples": 200, "seed": 3},
        success={"comparator": ">", "threshold": 0.5},
        failure={"comparator": "<", "threshold": 0.0},
    )
    assert engine.evaluate(spec, {SOURCE: _measured(NOISY)}).conclusion is (
        EmpiricalConclusion.SUPPORTS
    ), "the control: with the interval computable, these data support"

    monkeypatch.setattr(engine, constant, value)
    result = engine.evaluate(spec, {SOURCE: _measured(NOISY)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert result.interval is None
    assert "the preregistered uncertainty could not be computed" in result.summary
    assert why in result.summary


def test_a_bootstrap_over_too_few_records_is_insufficient() -> None:
    """The one-record interval an independent review measured, generalised.

    Nine tightly clustered records resample to an interval that clears the
    threshold comfortably, and it is an artefact of the resampling: below
    ``MIN_BOOTSTRAP_RECORDS`` the conclusion is INSUFFICIENT.
    """

    clustered = (1.0, 1.1, 0.9, 1.2, 1.0, 1.3, 0.95, 1.05, 1.15)
    assert len(clustered) < engine.MIN_BOOTSTRAP_RECORDS
    spec = _statistic(uncertainty={"resamples": 200, "seed": 5})
    result = engine.evaluate(spec, {SOURCE: _measured(clustered)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "a bootstrap over 9 record(s) is not an interval" in result.summary


def test_a_bootstrap_with_no_spread_over_few_records_is_not_certainty() -> None:
    """Twelve identical values resample to one value, every time.

    That is not a zero-width interval; it is a sample too small to show its
    own variability, and below ``MIN_DEGENERATE_RECORDS`` it is INSUFFICIENT.
    """

    spec = _statistic(uncertainty={"resamples": 200, "seed": 5})
    result = engine.evaluate(spec, {SOURCE: _measured([2.0] * 12)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "claims a certainty 12 records cannot give" in result.summary


def test_a_fraction_of_five_out_of_five_does_not_clear_ninety_percent() -> None:
    """The Wilson score interval, where a percentile bootstrap collapses.

    Five successes of five is p = 1, and its 95% Wilson interval is
    [0.566, 1.0]: it straddles a success threshold of 0.9, so the conclusion
    is INCONCLUSIVE. The lower bound is computed here from the closed form.
    """

    spec = _analysis(
        observables=[
            {
                "name": "runs",
                "source": "results/runs.json",
                "kind": "records",
                "fields": ["converged"],
            }
        ],
        reductions=[
            {
                "name": "share",
                "op": "fraction",
                "observable": "runs",
                "where": [{"field": "converged", "comparator": "==", "value": True}],
            }
        ],
        primary_statistic="share",
        uncertainty={"method": "wilson_score"},
        success={"comparator": ">", "threshold": 0.9},
        failure={"comparator": "<", "threshold": 0.5},
    )
    runs = {"results/runs.json": [{"converged": True} for _ in range(5)]}
    result = engine.evaluate(spec, runs)
    z = NormalDist().inv_cdf(0.975)
    lower = (1 + z * z / 10 - z * math.sqrt(z * z / 100)) / (1 + z * z / 5)
    assert result.statistic == 1.0
    assert result.interval is not None
    assert result.interval[0] == pytest.approx(lower, rel=1e-9)
    assert result.interval[0] == pytest.approx(0.5655, abs=1e-3)
    assert result.interval[1] == pytest.approx(1.0)
    assert result.conclusion is EmpiricalConclusion.INCONCLUSIVE, result.summary
    assert "Wilson score interval" in result.summary


def test_std_is_the_sample_standard_deviation() -> None:
    """AN16: the ``n - 1`` estimator, which the engine documents and computes.

    Golden value: 2, 4, 4, 4, 5, 5, 7, 9 has population deviation 2 and
    sample deviation sqrt(32 / 7).
    """

    result = engine.evaluate(
        _statistic("std"), {SOURCE: _measured([2, 4, 4, 4, 5, 5, 7, 9])}
    )
    assert result.statistic == pytest.approx(math.sqrt(32 / 7))
    assert result.statistic != pytest.approx(2.0)


def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two() -> None:
    """AN17: ``sorted(v)[n // 2]`` is the upper median, not the median.

    Golden values over an even count, given unsorted: the median of
    10, 1, 3, 2 is 2.5, and its type-7 first quartile is 1.75.
    """

    measured = {SOURCE: _measured([10, 1, 3, 2])}
    assert engine.evaluate(_statistic("median"), measured).statistic == (
        pytest.approx(2.5)
    )
    quartile = _statistic(
        reductions=[
            {
                "name": "m",
                "op": "quantile",
                "observable": "cells",
                "field": "iterations",
                "q": 0.25,
            }
        ]
    )
    assert engine.evaluate(quartile, measured).statistic == pytest.approx(1.75)


@pytest.mark.parametrize(
    ("condition", "undecidable"),
    [
        ({"field": "quality", "comparator": ">=", "value": 0.5}, "diverged"),
        ({"field": "quality", "comparator": "==", "value": "good"}, 1),
    ],
    ids=["inequality-on-a-string", "string-equality-on-a-number"],
)
def test_an_inclusion_rule_that_cannot_be_decided_makes_the_record_incomplete(
    condition: dict[str, Any], undecidable: Any
) -> None:
    """AN18: an undecidable inclusion rule is not a failed one.

    A record whose quality is "diverged" has not failed ``quality >= 0.5``;
    nobody knows whether it passes. Counting it as excluded would drop it
    silently under the default ``incomplete_records: insufficient``, which
    exists precisely so that dropping awkward rows is decided in advance.
    """

    passing = 0.9 if condition["comparator"] == ">=" else "good"
    records = [
        {"size": 1, "difficulty": 1, "iterations": value, "quality": passing}
        for value in NOISY
    ]
    records.append(
        {"size": 1, "difficulty": 1, "iterations": 1.0, "quality": undecidable}
    )
    spec = _statistic(
        observables=[
            {
                "name": "cells",
                "source": SOURCE,
                "kind": "records",
                "path": "cells",
                "fields": ["size", "difficulty", "iterations", "quality"],
                "include": [condition],
            }
        ]
    )
    result = engine.evaluate(spec, {SOURCE: {"cells": records}})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "hold a non-number where one is required" in result.summary
    assert result.records["cells"]["incomplete"] == 1
    assert result.records["cells"]["excluded_by_rule"] == 0

    # The control: a record that cleanly fails the rule is excluded, counted.
    records[-1]["quality"] = 0.1 if condition["comparator"] == ">=" else "bad"
    clean = engine.evaluate(spec, {SOURCE: {"cells": records}})
    assert clean.conclusion is EmpiricalConclusion.SUPPORTS, clean.summary
    assert clean.records["cells"]["excluded_by_rule"] == 1


def _failure_share(source: str, *, where: dict[str, Any]) -> AnalysisSpec:
    """The share of runs that failed, which the idea says is small."""

    return _analysis(
        observables=[
            {
                "name": "runs",
                "source": source,
                "kind": "records",
                "fields": ["seed"],
            }
        ],
        reductions=[
            {"name": "failed", "op": "fraction", "observable": "runs", "where": [where]}
        ],
        primary_statistic="failed",
        success={"comparator": "<", "threshold": 0.2},
        failure={"comparator": ">", "threshold": 0.5},
    )


@pytest.mark.parametrize("unreadable", ["diverged", "nan"])
def test_a_selection_that_cannot_be_decided_is_insufficient_not_a_smaller_count(
    unreadable: str,
) -> None:
    """A ``where`` over a value that is not a number decides nothing.

    Three runs of ten wrote "diverged" (or "nan", which a CSV reader keeps as
    text) where the gap should be. Dropping them from the numerator of "the
    share with gap > 0.01" while keeping them in the denominator read 0 of
    10 failed -- SUPPORTS for a claim that failures are rare. They make the
    analysis INSUFFICIENT instead.
    """

    table = "seed,gap\n" + "".join(
        f"{seed},{0.001 if seed < 7 else unreadable}\n" for seed in range(10)
    )
    rows = engine.parse_document(table, name="runs.csv")
    assert [row["gap"] for row in rows][-3:] == [unreadable] * 3
    spec = _failure_share(
        "results/runs.csv", where={"field": "gap", "comparator": ">", "value": 0.01}
    )
    result = engine.evaluate(spec, {"results/runs.csv": rows})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert result.records["runs"]["incomplete"] == 3


def test_a_selection_on_an_absent_field_is_insufficient() -> None:
    """The same, for a run that did not write the field at all."""

    runs = [{"seed": seed, "gap": 0.001} for seed in range(7)]
    runs += [{"seed": seed} for seed in range(7, 10)]
    spec = _failure_share(
        "results/runs.json", where={"field": "gap", "comparator": ">", "value": 0.01}
    )
    result = engine.evaluate(spec, {"results/runs.json": runs})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert result.records["runs"]["incomplete"] == 3


@pytest.mark.parametrize(
    ("name", "document"),
    [
        (
            "runs.csv",
            "seed,converged\n"
            + "".join(
                f"{seed},{'True' if seed < 3 else 'False'}\n" for seed in range(10)
            ),
        ),
        (
            "runs.json",
            json.dumps(
                [{"seed": seed, "converged": int(seed < 3)} for seed in range(10)]
            ),
        ),
    ],
    ids=["csv-text-booleans", "json-integer-booleans"],
)
def test_equality_is_typed_and_a_mismatch_is_undecidable_not_unequal(
    name: str, document: str
) -> None:
    """``converged == false`` against "False" or 0 is not a clean non-match.

    The finding: a CSV's "False" read as unequal to ``false``, so seven failed
    runs of ten counted as none and a rule on the failure share read
    SUPPORTS. A value of another type makes the comparison undefined, the
    record incomplete, and the analysis INSUFFICIENT.
    """

    source = f"results/{name}"
    spec = _failure_share(
        source, where={"field": "converged", "comparator": "==", "value": False}
    )
    result = engine.evaluate(spec, {source: engine.parse_document(document, name=name)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert result.records["runs"]["incomplete"] == 10

    # The control: real booleans compare, and seven of ten failed.
    typed = [{"seed": seed, "converged": seed < 3} for seed in range(10)]
    read = engine.evaluate(spec, {source: typed})
    assert read.statistic == pytest.approx(0.7)
    assert read.conclusion is EmpiricalConclusion.CONTRADICTS


def _violations(source: str, *, path: str = "", include: Sequence[Any] = ()) -> Any:
    return _analysis(
        observables=[
            {
                "name": "checks",
                "source": source,
                "kind": "records",
                "path": path,
                "fields": ["error"],
                "include": list(include),
            }
        ],
        reductions=[
            {
                "name": "violations",
                "op": "count",
                "observable": "checks",
                "where": [{"field": "error", "comparator": ">", "value": 0.25}],
            }
        ],
        primary_statistic="violations",
        success={"comparator": "<", "threshold": 1.0},
        failure={"comparator": ">=", "threshold": 1.0},
    )


@pytest.mark.parametrize(
    ("spec", "documents"),
    [
        (
            _violations("results/checks.json", path="checks"),
            {"results/checks.json": {"checks": []}},
        ),
        (
            _violations("results/checks.csv"),
            {"results/checks.csv": engine.parse_document("error\n", name="c.csv")},
        ),
        (
            _violations(
                "results/checks.json",
                include=[{"field": "valid", "comparator": "==", "value": True}],
            ),
            {"results/checks.json": [{"error": 0.9, "valid": False} for _ in range(4)]},
        ),
    ],
    ids=["empty-list", "header-only-csv", "every-record-excluded"],
)
def test_a_count_over_nothing_is_insufficient_not_zero_violations(
    spec: AnalysisSpec, documents: dict[str, Any]
) -> None:
    """Zero violations over no records is nothing measured.

    The finding: a run that wrote ``[]``, a header-only CSV, or records every
    one of which the inclusion rule excluded, read zero violations and
    SUPPORTS. No support rule is declared here, so nothing but the count's
    own refusal stands between those runs and a conclusion.
    """

    result = engine.evaluate(spec, documents)
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert "has no analysed records" in result.summary


def _regimes() -> dict[str, Any]:
    easy = _cells([1, 2, 4], [1, 3])["cells"]
    hard = _cells([1, 2], [1, 3, 5])["cells"]
    return {
        "cells": [{**row, "regime": "easy"} for row in easy]
        + [{**row, "regime": "hard"} for row in hard]
    }


@pytest.mark.parametrize(
    ("support", "unmet"),
    [
        (
            {"observable": "cells", "min_records": 4, "min_distinct": {"size": 3}},
            "computes on records where 'size' takes 2 distinct value(s)",
        ),
        (
            {"observable": "cells", "min_records": 8},
            "interaction computes on 6 record(s)",
        ),
    ],
    ids=["levels", "records"],
)
def test_support_is_checked_over_the_records_the_statistic_rests_on(
    support: dict[str, Any], unmet: str
) -> None:
    """A support rule met by the observable and not by the selection.

    The regression is computed where ``regime == "hard"``: six records, two
    sizes. The whole observable has twelve records and three sizes, so a
    rule checked only over the observable passes -- which is the co-design
    the rule exists to stop, moved one filter inward.
    """

    spec = _spec(
        observables=[
            {
                "name": "cells",
                "source": SOURCE,
                "kind": "records",
                "path": "cells",
                "fields": ["size", "difficulty", "iterations", "regime"],
            }
        ],
        reductions=[
            {
                "name": "interaction",
                "op": "ols_coefficient",
                "observable": "cells",
                "response": "iterations",
                "terms": ["size", "difficulty", "size:difficulty"],
                "coefficient": "size:difficulty",
                "where": [{"field": "regime", "comparator": "==", "value": "hard"}],
            }
        ],
        support=[support],
    )
    result = engine.evaluate(spec, {SOURCE: _regimes()})
    assert result.support[0]["met"] is True, "the whole observable satisfies it"
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT, result.summary
    assert unmet in result.summary

    # The control: without the rule the subset does identify the coefficient.
    free = engine.evaluate(
        spec.model_copy(update={"support": ()}), {SOURCE: _regimes()}
    )
    assert free.conclusion is EmpiricalConclusion.SUPPORTS, free.summary
    assert free.statistic == pytest.approx(0.5)


# ============================================================ requirements --
def test_requirements_are_blind_to_the_predicates_and_name_the_support() -> None:
    """SC01 and SC03: what the experiment designer is shown of an analysis.

    Two analyses identical but for both predicates -- every threshold and
    every comparator different -- must render byte-identical requirements,
    or the designer can read the bar off the block. And the block must say
    what the data must exhibit, or a design cannot know it will be judged
    INSUFFICIENT for collapsing a level.
    """

    one = _spec(
        success={"comparator": ">", "threshold": 0.4375},
        failure={"comparator": "<", "threshold": 0.0625},
    )
    other = _spec(
        success={"comparator": "<=", "threshold": 0.8125},
        failure={"comparator": ">=", "threshold": 0.9375},
    )
    shown = scicontract.requirements_block(one)
    assert shown == scicontract.requirements_block(other)
    rendered = "\n".join(shown)
    for threshold in ("0.4375", "0.0625", "0.8125", "0.9375"):
        assert threshold not in rendered
    assert "at least 4 record(s)" in rendered
    assert "at least 2 distinct value(s) of size" in rendered
    assert "at least 2 distinct value(s) of difficulty" in rendered


def test_withholding_redacts_exactly_the_thresholds() -> None:
    """The idea's text loses the bar and nothing else.

    Equal values in any spelling and either sign are redacted; a number that
    merely shares digits is not; 0 and 1 are exempt, as they are when the
    analysis's own text is checked.
    """

    spec = _spec(
        success={"comparator": ">", "threshold": 0.4375},
        failure={"comparator": "<", "threshold": 0.0625},
    )
    lines = scicontract.withhold_thresholds(
        [
            "the interaction exceeds 0.4375, or 0.43750, and is never -0.4375",
            "below 0.0625 it is refuted; 10.4375 and 0.43751 are other numbers",
            "a share between 0 and 1",
        ],
        spec,
    )
    withheld = scicontract.WITHHELD
    assert lines == [
        f"the interaction exceeds {withheld}, or {withheld}, and is never {withheld}",
        f"below {withheld} it is refuted; 10.4375 and 0.43751 are other numbers",
        "a share between 0 and 1",
    ]


# ================================================================ verifier --
def _stored(context: Any, document: dict[str, Any]) -> str:
    return context.artifacts.put_text(
        json.dumps(document),
        media_type="application/json",
        role="forged",
        producer="test",
    ).artifact_id


def test_verify_rehashes_the_stored_design_rather_than_trusting_the_row(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """VF01: a design swapped under an unchanged contract digest is refused.

    The forged document keeps the contract digest it records, so the only
    thing that can notice is hashing the design that is actually stored.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    scicontract.verify(context.artifacts, contract)
    document = json.loads(context.artifacts.get_text(contract.contract_artifact_id))
    document["design"]["argv"] = [*document["design"]["argv"], "--quietly-different"]
    forged = contract.model_copy(
        update={"contract_artifact_id": _stored(context, document)}
    )
    with pytest.raises(scicontract.ContractIntegrityError, match="stored design"):
        scicontract.verify(context.artifacts, forged)


def test_verify_refuses_a_version_the_contract_does_not_test(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """VF02: a contract is about one hypothesis, by id, version and content."""

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    version = portfolio.require_version(contract.idea_id, contract.idea_version)
    scicontract.verify(context.artifacts, contract, version=version)
    for moved in (
        {"content_digest": "pidea-content-v1:" + "0" * 64},
        {"version": version.version + 1},
        {"idea_id": "PIDEA-20260101T000000Z-00000000"},
    ):
        with pytest.raises(scicontract.ContractIntegrityError, match="cannot be used"):
            scicontract.verify(
                context.artifacts, contract, version=version.model_copy(update=moved)
            )


def test_verify_refuses_a_contract_document_recording_another_digest(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """VF03: the document's own ``contract_digest`` is compared too.

    Everything the digest covers is intact; only the digest the document
    records differs. A reviewer reads that document, so it may not disagree
    with the row it is filed under.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    document = json.loads(context.artifacts.get_text(contract.contract_artifact_id))
    document["contract_digest"] = "pcontract-v1:" + "0" * 64
    forged = contract.model_copy(
        update={"contract_artifact_id": _stored(context, document)}
    )
    with pytest.raises(scicontract.ContractIntegrityError, match="hashes to"):
        scicontract.verify(context.artifacts, forged)


def test_verify_refuses_an_analysis_filed_under_another_hypothesis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """VF04: the analysis document's ``hypothesis_digest`` is checked.

    The analysis itself is untouched and still hashes to the row, so only
    the hypothesis it was frozen for can betray it.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    document = json.loads(context.artifacts.get_text(contract.analysis_artifact_id))
    document["hypothesis_digest"] = "pidea-content-v1:" + "0" * 64
    forged = contract.model_copy(
        update={"analysis_artifact_id": _stored(context, document)}
    )
    with pytest.raises(
        scicontract.ContractIntegrityError, match="different hypothesis"
    ):
        scicontract.verify(context.artifacts, forged)


# =================================================================== route --
def _jobs(runtime_db: Database) -> int:
    return len(RuntimeStore(runtime_db).list_external_jobs())


def _forged_execution(
    context: Any,
    contract: Any,
    project: str,
    *,
    spec_changes: dict[str, Any],
    record_changes: dict[str, Any],
) -> IdeaExperiment:
    """Replace the designed execution with one whose digests are self-consistent.

    The pattern of ``test_an_execution_that_moved_under_its_contract_does_not_run``:
    the forged preregistration hashes to its row and the contract verifies,
    so what refuses it can only be the comparison under test.
    """

    from research_os.runtime.executors import spec_digest

    original = context.portfolio.get_experiment(
        idea_id=contract.idea_id, idea_version=1
    )
    spec, _rule = empirical._preregistered(context, original)
    forged_id = "PEXP-20260923T000000Z-f0f0f0f0"
    moved = replace(spec, cwd=str(empirical.workspace_for(forged_id)), **spec_changes)
    record = json.loads(
        context.artifacts.get_text(original.preregistration_artifact_id)
    )
    record.update(
        {
            "experiment_id": forged_id,
            "spec": empirical._spec_record(moved),
            "spec_digest": spec_digest(moved),
            **record_changes,
        }
    )
    context.portfolio.update_experiment(
        original.experiment_id, state=ExperimentState.SUPERSEDED, detail="make room"
    )
    return context.portfolio.create_experiment(
        idea_id=original.idea_id,
        idea_version=1,
        project_id=project,
        role=ExperimentRole.PRIMARY,
        command=original.command,
        spec_digest=spec_digest(moved),
        variation_digest=empirical.variation_digest(moved),
        workspace_path=moved.cwd,
        decision_rule=original.decision_rule,
        no_rule_reason=original.no_rule_reason,
        preregistration_artifact_id=_stored(context, record),
        experiment_id=forged_id,
        contract_id=contract.contract_id,
    )


@pytest.mark.parametrize(
    "forged",
    [
        {"contract_digest": "pcontract-v1:" + "f" * 64},
        {"contract_id": "PCON-20260923T000000Z-f0f0f0f0"},
    ],
    ids=["another-digest", "another-contract"],
)
def test_a_preregistration_that_names_another_contract_does_not_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
    forged: dict[str, Any],
) -> None:
    """EX04: the preregistration must name the contract it executes, exactly.

    The specification realises the frozen design and the contract verifies;
    only the contract the preregistration names is wrong. That binding is
    what makes "this execution was run under this contract" a hash rather
    than a column.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    experiment = _forged_execution(
        context, contract, runtime_project, spec_changes={}, record_changes=forged
    )
    before = _jobs(runtime_db)
    step = empirical.submit(context, experiment)
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "names contract" in step.detail
    assert _jobs(runtime_db) == before, "nothing may run under a contract it misnames"


def test_a_seed_changed_through_the_environment_does_not_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The seed reaches the program as ``RESEARCH_OS_SEED_i``, so it is frozen there.

    The finding: a design froze ``seeds`` and not ``env``, and a job with a
    different seed ran under an unchanged, verifying contract. The forged
    specification here differs from the frozen design in ``env`` alone.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    verified = scicontract.verify(context.artifacts, contract)
    assert verified.design is not None
    assert verified.design["env"] == {
        "RESEARCH_OS_SEED_0": str(empirical.DEFAULT_SEED)
    }, "the design freezes the environment the program reads its seed from"
    experiment = _forged_execution(
        context,
        contract,
        runtime_project,
        spec_changes={"env": {"RESEARCH_OS_SEED_0": "7"}},
        record_changes={},
    )
    before = _jobs(runtime_db)
    step = empirical.submit(context, experiment)
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "differs from its frozen design in env" in step.detail
    assert _jobs(runtime_db) == before


def test_a_forged_execution_that_changes_nothing_scientific_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The control for the two tests above: the forging itself is not refused.

    The same construction, moving only the workspace, realises the design
    under the right contract and runs -- so each refusal above is about the
    one thing it changed.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    experiment = _forged_execution(
        context, contract, runtime_project, spec_changes={}, record_changes={}
    )
    step = empirical.submit(context, experiment)
    assert step.ok, step.detail
    assert step.experiment is not None
    assert step.experiment.state is ExperimentState.COMPLETED


def test_interpretation_re_verifies_the_contract_it_reads_under(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """EX02: the rule is re-hashed when the result is read, not only when it runs.

    The measurement finishes under the frozen contract; then both stored
    analyses are swapped, with the trigger bypassed, for one whose thresholds
    would read the same data as CONTRADICTS. Interpretation must refuse with
    MISSING_SCIENTIFIC_AUTHORITY and write no evidence at all.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    experiment = portfolio.get_experiment(idea_id=contract.idea_id, idea_version=1)
    assert experiment is not None
    ran = empirical.submit(context, experiment)
    assert ran.ok, ran.detail
    assert ran.experiment is not None
    assert ran.experiment.state is ExperimentState.COMPLETED

    moved = _analysis_answer(
        success={"comparator": ">", "threshold": 0.9},
        failure={"comparator": "<", "threshold": 0.6},
    )
    analysis_doc = json.loads(context.artifacts.get_text(contract.analysis_artifact_id))
    contract_doc = json.loads(context.artifacts.get_text(contract.contract_artifact_id))
    analysis_doc["analysis"] = moved
    contract_doc["analysis"] = moved
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update scientific_contracts set analysis_artifact_id = %s, "
            "contract_artifact_id = %s where contract_id = %s",
            (
                _stored(context, analysis_doc),
                _stored(context, contract_doc),
                contract.contract_id,
            ),
        )

    step = empirical.interpret(
        context, portfolio.require_experiment(experiment.experiment_id)
    )
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert step.conclusion is not EmpiricalConclusion.CONTRADICTS
    assert not portfolio.list_evidence(idea_id=contract.idea_id, idea_version=1)
    read = portfolio.require_experiment(experiment.experiment_id)
    assert read.analysis_artifact_id is None and read.evidence_id is None


def _bounded(seconds: int) -> Any:
    config = load_config()
    return config.model_copy(
        update={
            "bounds": config.bounds.model_copy(
                update={"max_experiment_seconds": seconds}
            )
        }
    )


def _timed_out(portfolio: PortfolioStore, experiment: IdeaExperiment) -> Any:
    """What `interpret` records for a run that hit its time limit."""

    return portfolio.update_experiment(
        experiment.experiment_id,
        state=ExperimentState.OPERATIONALLY_FAILED,
        failure_class=str(FailureClass.EXECUTOR_FAILED),
        detail="the experiment did not run correctly (TIMED_OUT, exit None: "
        "timed out after 1s)",
        count_attempt=True,
    )


def test_a_repair_cannot_raise_the_time_limit_past_either_ceiling(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """EX07: a repaired time limit is bounded by the declaration and the portfolio.

    The command declares 120 seconds and the portfolio allows 1800 by
    default: a million is refused, 121 is refused by the declaration, and
    under a portfolio ceiling of 30 so is 60. No refusal records anything,
    and a limit inside both is accepted.
    """

    context, contract = _frozen(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo, run=False
    )
    designed = portfolio.get_experiment(idea_id=contract.idea_id, idea_version=1)
    assert designed is not None
    failed = _timed_out(portfolio, designed)

    def refused(timeout: int) -> None:
        with pytest.raises(empirical.EmpiricalError, match="outside what") as caught:
            empirical.repair_implementation(context, failed, timeout_seconds=timeout)
        assert caught.value.failure_class is FailureClass.POLICY_REFUSED

    for timeout in (10**6, 121, 0):
        refused(timeout)
    context.config = _bounded(30)
    refused(60)
    executions = [
        item
        for item in portfolio.list_experiments(idea_id=contract.idea_id)
        if item.contract_id == contract.contract_id
    ]
    assert [item.experiment_id for item in executions] == [failed.experiment_id]

    repaired = empirical.repair_implementation(context, failed, timeout_seconds=20)
    assert repaired.contract_id == contract.contract_id
    assert repaired.experiment_id != failed.experiment_id


def test_implementation_repairs_stop_at_their_bound(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """EX08: ``MAX_IMPLEMENTATION_REPAIRS`` is where a repair loop stops.

    Each round, the execution times out and a person raises the ceiling by a
    second, so every precondition for a repair holds every time. The bound
    lives in ``_repair_if_warranted``, the one caller that repairs on its
    own, and after that many repairs it declines.
    """

    idea = _idea(portfolio, runtime_project)
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3]),
        ),
        grid_repo,
    )
    context.config = _bounded(1)
    step = empirical.design(context, portfolio.require_version(idea))
    assert step.ok, step.detail
    assert step.experiment is not None
    current = step.experiment
    repairs = []
    for ceiling in range(2, empirical.MAX_IMPLEMENTATION_REPAIRS + 4):
        failed = _timed_out(portfolio, current)
        context.config = _bounded(ceiling)
        repaired = empirical._repair_if_warranted(context, failed)
        if repaired is None:
            break
        repairs.append(repaired)
        current = repaired
    assert len(repairs) == empirical.MAX_IMPLEMENTATION_REPAIRS
    executions = [
        item
        for item in portfolio.list_experiments(idea_id=idea)
        if item.contract_id == current.contract_id
    ]
    assert len(executions) == empirical.MAX_IMPLEMENTATION_REPAIRS + 1


def test_a_replication_inherits_the_primary_analysis_and_asks_no_one_for_another(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """EX09: "the replication measures what the primary measured" is a digest.

    After the primary is read the analysis designer's script changes, so a
    replication that asked it would freeze a different analysis. It must not
    ask: same analysis digest, one analysis-designer call in total, and the
    provenance says where the analysis came from.
    """

    idea = _idea(portfolio, runtime_project)
    analyst = TEMPLATES["analysis_designer"].identity
    router = ScriptedRouter(
        answers_by_prompt={
            analyst: _analysis_answer(),
            TEMPLATES["experiment_designer"].identity: _design_answer(
                [1, 2, 4], [1, 3]
            ),
            TEMPLATES["replication_designer"].identity: {
                **_design_answer([1, 2, 3], [1, 3]),
                "variation_kind": "grid",
                "variation_detail": "a different middle instance size",
            },
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea, router, grid_repo
    )
    first = _advance(context)
    assert first.ok, first.detail
    router.answers_by_prompt[analyst] = _analysis_answer(
        success={"comparator": ">", "threshold": 0.3},
        failure={"comparator": "<", "threshold": 0.05},
    )

    second = _advance(context, role=ExperimentRole.REPLICATION)

    assert second.ok, second.detail
    assert first.experiment is not None and second.experiment is not None
    primary = portfolio.require_contract(first.experiment.contract_id)
    replica = portfolio.require_contract(second.experiment.contract_id)
    assert replica.contract_id != primary.contract_id
    assert replica.role is ExperimentRole.REPLICATION
    assert replica.analysis_digest == primary.analysis_digest
    assert replica.analysis_prompt == f"inherited:{primary.contract_id}"
    assert len(router.requests_for_prompt(analyst)) == 1
    assert second.conclusion is EmpiricalConclusion.SUPPORTS, second.detail


def test_the_experiment_designer_does_not_read_the_bar_in_the_idea_text(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """``withhold_thresholds``, on the production path.

    The idea's falsifier states the bar -- which is where the analysis
    designer read it from, and where the experiment designer would read it
    too. The analysis designer is shown it; the experiment designer is shown
    the same sentence with the numbers withheld and the direction intact.
    """

    idea, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            adjudication_types=[AdjudicationType.EMPIRICAL],
            falsifier=(
                "The size-by-difficulty interaction exceeds 0.4375 if the idea "
                "holds; an interaction below 0.0625 refutes it."
            ),
        ),
        origin_role="blind_explorer",
    )
    router = _router(
        runtime_db,
        analysis=_analysis_answer(
            success={"comparator": ">", "threshold": 0.4375},
            failure={"comparator": "<", "threshold": 0.0625},
        ),
        design=_design_answer([1, 2, 4], [1, 3]),
    )
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea.idea_id,
        router,
        grid_repo,
    )
    step = empirical.design(context, portfolio.require_version(idea.idea_id))
    assert step.ok, step.detail

    (analyst,) = router.requests_for_prompt(TEMPLATES["analysis_designer"].identity)
    assert "exceeds 0.4375" in analyst.prompt and "below 0.0625" in analyst.prompt
    (designer,) = router.requests_for_prompt(TEMPLATES["experiment_designer"].identity)
    assert "0.4375" not in designer.prompt and "0.0625" not in designer.prompt
    assert f"exceeds {scicontract.WITHHELD}" in designer.prompt
    assert f"below {scicontract.WITHHELD}" in designer.prompt


NOOP_SCRIPT = """\
print("I claim success and write nothing")
"""


def test_a_result_already_in_the_checkout_is_not_read_as_this_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
    runtime_xdg: Path,
) -> None:
    """SC05: the stale-checkout guard on the contract route.

    A supporting ``results/grid.json`` is committed to the repository before
    the question is asked, and the declared command exits 0 without writing
    anything. The disposable worktree therefore holds a perfect result that
    this run did not produce -- and it is unavailable to the analysis, which
    makes the conclusion INSUFFICIENT rather than a reading of Git history.
    """

    repo = _project(tmp_path, runtime_xdg, runtime_project, script=NOOP_SCRIPT)
    (repo / "results").mkdir()
    (repo / SOURCE).write_text(json.dumps(_cells([1, 2, 4], [1, 3])), "utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "an old result"], cwd=repo, check=True)

    idea = _idea(portfolio, runtime_project)
    context = _context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3]),
        ),
        repo,
    )
    step = _advance(context)
    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT, step.detail
    assert "byte-identical to the copy committed at" in step.detail
    (row,) = portfolio.list_evidence(idea_id=idea, idea_version=1)
    assert row.strength is EvidenceStrength.INCONCLUSIVE
