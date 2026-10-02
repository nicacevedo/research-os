"""The second analysis language: its semantics, its refusals and its bounds.

``docs/SCIENCE_EXECUTION.md`` §4a is the specification. Cycle 002 of the live
column-generation run stopped ``PAUSED_NO_FRONTIER`` with four INVESTIGATING
ideas whose frozen analyses could not be written in the first language --
grouping by seed, per-record derived fields, the crossing of a curve, a
permutation null, rank and partial correlations. These tests hold, with no
database and no subprocess:

- **the arithmetic** -- the closed expression grammar computes what it says,
  is undefined rather than a number where arithmetic is, and refuses what is
  not in it, before anything is frozen;
- **the operations** -- grouping (by seed, by what a campaign unit renews),
  per-record derived values, the crossing, the permutation null, the rank
  and partial correlations, each against a value computed by hand or a
  textbook identity, and equivalence through the existing interval rule;
- **honesty** -- a table derives from declared fields and cannot make an
  undeclared one exist; an unsupported operation, a malformed expression and
  an incoherent table fail closed, and so does a caller that bypasses the
  validator;
- **the bounds** -- every list, every expression and every resampling is
  bounded, and a resampling past its bound is INSUFFICIENT, not thinned;
- **determinism and history** -- a reading is a function of the data and the
  contract, and every analysis frozen in the first language rebuilds to its
  digest and is read exactly as the base engine read it.

The Cycle 002 reproductions are in
``tests/test_analysis_operations_v2_cycle002.py``; the route (freezing,
campaigns, retries, the retired prompt) in
``tests/test_analysis_operations_v2_route.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from research_os import capability as capabilities
from research_os.experiment.config import load_config as load_experiment_config
from research_os.portfolio import analysis as engine
from research_os.portfolio import (
    campaign,
    expressions,
    scicontract,
    sciencechain,
    shape,
)
from research_os.portfolio.contracts import (
    AnalysisSpec,
    ComputedField,
    ContractError,
    Reduction,
    Table,
)
from research_os.portfolio.models import EmpiricalConclusion, ExecutionShapeVerdict

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = "results/grid.json"
CG_SOURCE = "results/ros/cells.json"
INSUFFICIENT = EmpiricalConclusion.INSUFFICIENT
SUPPORTS = EmpiricalConclusion.SUPPORTS
CONTRADICTS = EmpiricalConclusion.CONTRADICTS
INCONCLUSIVE = EmpiricalConclusion.INCONCLUSIVE


# ================================================================ helpers --
def points(fields: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "name": "points",
        "source": SOURCE,
        "kind": "records",
        "path": "records",
        "fields": fields,
        **extra,
    }


def spec(**payload: Any) -> AnalysisSpec:
    """A checked analysis: the defaults decide on a primary statistic above 0.5."""

    body: dict[str, Any] = {
        "analysable": True,
        "estimand": "a quantity of the synthetic records",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": -0.5},
    }
    body.update(payload)
    built = AnalysisSpec.model_validate(body)
    built.check()
    return built


def read(
    analysis: AnalysisSpec, records: list[dict[str, Any]]
) -> engine.AnalysisResult:
    return engine.evaluate(analysis, {SOURCE: {"records": records}})


def value_of(expression: str, **names: Any) -> float | None:
    return expressions.evaluate(expressions.parse(expression), names.get)


def crossing_spec(
    *, level: float = 0.5, pick: str = "first", direction: str = "any"
) -> AnalysisSpec:
    return spec(
        observables=[points(["x", "y"])],
        reductions=[
            {
                "name": "where_it_crosses",
                "op": "crossing",
                "observable": "points",
                "field": "x",
                "other_field": "y",
                "crossing": {"level": level, "pick": pick, "direction": direction},
            }
        ],
        primary_statistic="where_it_crosses",
        success={"comparator": ">", "threshold": -100.0},
        failure={"comparator": "<", "threshold": -200.0},
    )


def curve(*pairs: tuple[float, float]) -> list[dict[str, Any]]:
    return [{"x": x, "y": y} for x, y in pairs]


def cg_planning(tmp_path: Path) -> shape.Planning:
    raw = (FIXTURES / "cg_cells_research_capabilities.yaml").read_bytes()
    loaded = capabilities.LoadedManifest(
        manifest=capabilities.parse_manifest(raw),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        commit="c" * 40,
    )
    path = tmp_path / "experiments.yaml"
    path.write_text(
        (FIXTURES / "cg_cells_experiments.yaml")
        .read_text(encoding="utf-8")
        .replace("{project}", "cg"),
        encoding="utf-8",
    )
    commands = dict(load_experiment_config(path).projects["cg"].commands)
    bounds = capabilities.HumanBounds(
        max_execution_seconds=1800, max_campaign_units=6, max_campaign_seconds=10800
    )
    envelope = capabilities.execution_envelope(loaded, commands, bounds)
    assert envelope is not None
    return shape.Planning(envelope=envelope, loaded=loaded, commands=commands)


def resolve_on_cg(analysis: AnalysisSpec, tmp_path: Path) -> capabilities.Resolution:
    planning = cg_planning(tmp_path)
    return capabilities.resolve(
        capabilities.Requirements(
            observables=sciencechain.requirements_from_analysis(analysis),
            max_seconds=1800,
        ),
        loaded=planning.loaded,
        commands=planning.commands,
    )


# ============================================================ arithmetic --
@pytest.mark.parametrize(
    ("text", "names", "expected"),
    [
        ("wall_seconds / iterations", {"wall_seconds": 6.0, "iterations": 3}, 2.0),
        (
            "(columns_generated - k) / p",
            {"columns_generated": 30, "k": 10, "p": 200},
            0.1,
        ),
        ("log(x)", {"x": math.e}, 1.0),
        ("exp(log(x))", {"x": 5.0}, 5.0),
        ("abs(-3 - x)", {"x": 1}, 4.0),
        ("max(a, b, c)", {"a": 1, "b": 7, "c": 3}, 7.0),
        ("min(a, b)", {"a": -1, "b": 2}, -1.0),
        ("1 + 2 * 3", {}, 7.0),
        ("2 - 3 - 4", {}, -5.0),
        ("8 / 4 / 2", {}, 1.0),
        ("-(1 + 2)", {}, -3.0),
        ("- - x", {"x": 2}, 2.0),
        ("1.5e1 + .5", {}, 15.5),
        ("detail.columns * 2", {"detail.columns": 4}, 8.0),
    ],
)
def test_an_expression_computes_exactly_the_arithmetic_it_says(
    text: str, names: dict[str, Any], expected: float
) -> None:
    assert value_of(text, **names) == pytest.approx(expected, rel=1e-15, abs=1e-15)


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("x / y", {"x": 1, "y": 0}),
        ("x / y", {"x": 0, "y": 0.0}),
        ("log(x)", {"x": 0}),
        ("log(x)", {"x": -1}),
        ("exp(x)", {"x": 1000}),
        ("x * y", {"x": 1e200, "y": 1e200}),
        ("x + 1", {}),
        ("x + 1", {"x": True}),
        ("x + 1", {"x": "3"}),
        ("x + 1", {"x": None}),
        ("max(x, y)", {"x": 1}),
    ],
)
def test_an_undefined_expression_is_none_never_a_number(
    text: str, names: dict[str, Any]
) -> None:
    assert value_of(text, **names) is None


MALFORMED = [
    "",
    "   ",
    "x +",
    "(x",
    "x)",
    "x ** 2",
    "x ^ 2",
    "x % 2",
    "x if y else z",
    "__import__('os').system('true')",
    "lambda: 1",
    "x; y",
    "log(x, y)",
    "max(x)",
    "sqrt(x)",
    "log",
    "x y",
    "1e999",
    "x == y",
    "x < y",
    "'s'",
    "x[0]",
    "a.b()",
    "f(x)",
    "{}",
    "x,",
    "∑x",
    "1 +* 2",
]


@pytest.mark.parametrize("text", MALFORMED)
def test_a_malformed_expression_fails_closed_before_anything_is_frozen(
    text: str,
) -> None:
    with pytest.raises(expressions.ExpressionError):
        expressions.parse(text)
    if text.strip():
        with pytest.raises(ValidationError):
            Reduction.model_validate(
                {"name": "q", "op": "expression", "expression": text}
            )
        with pytest.raises(ValidationError):
            ComputedField.model_validate({"field": "q", "expression": text})


def test_an_expression_is_bounded_in_size_nodes_depth_and_names() -> None:
    with pytest.raises(expressions.ExpressionError, match="at most 256 characters"):
        expressions.parse("x + " * 64 + "x")
    with pytest.raises(expressions.ExpressionError, match="more than 64 nodes"):
        expressions.parse("+".join(["x"] * 40))
    with pytest.raises(expressions.ExpressionError, match="deeper than 16"):
        expressions.parse("(" * 20 + "x" + ")" * 20)
    with pytest.raises(expressions.ExpressionError, match="deeper than 16"):
        expressions.parse("-" * 20 + "x")
    with pytest.raises(expressions.ExpressionError, match="more than 8 variables"):
        expressions.parse("a + b + c + d + e + f + g + h + i")
    assert expressions.parse("a + b + c + d + e + f + g + h").variables == tuple(
        "abcdefgh"
    )


def test_an_expression_reads_names_only_from_the_mapping_it_is_given() -> None:
    """No attribute, builtin or global is reachable: a name is a lookup, and nothing else."""

    asked: list[str] = []

    def lookup(name: str) -> float | None:
        asked.append(name)
        return None

    for text in ("__class__", "__builtins__ + 1", "os.system", "abs(__import__)"):
        assert expressions.evaluate(expressions.parse(text), lookup) is None
    assert asked == ["__class__", "__builtins__", "os.system", "__import__"]


# ============================================================== grouping --
def by_seed_spec(**overrides: Any) -> AnalysisSpec:
    payload: dict[str, Any] = {
        "observables": [points(["seed", "x", "y"])],
        "tables": [
            {
                "name": "per_seed",
                "source": "points",
                "by": ["seed"],
                "aggregates": [
                    {"name": "level", "op": "mean", "field": "y"},
                    {
                        "name": "slope",
                        "op": "ols_coefficient",
                        "response": "y",
                        "terms": ["x"],
                        "coefficient": "x",
                    },
                    {"name": "n", "op": "count"},
                ],
            }
        ],
        "reductions": [
            {"name": "spread", "op": "std", "observable": "per_seed", "field": "level"},
            {
                "name": "typical_slope",
                "op": "median",
                "observable": "per_seed",
                "field": "slope",
            },
        ],
        "primary_statistic": "typical_slope",
        "support": [{"observable": "per_seed", "min_records": 3}],
    }
    payload.update(overrides)
    return spec(**payload)


def seeded_lines(seeds: list[Any], slopes: dict[Any, float]) -> list[dict[str, Any]]:
    return [
        {"seed": seed, "x": x, "y": slopes[seed] * x + (1 if seed == seeds[0] else 0)}
        for seed in seeds
        for x in (0.0, 1.0, 2.0, 3.0)
    ]


def test_grouping_by_seed_makes_one_record_per_seed_and_aggregates_within_it() -> None:
    slopes = {3: 2.0, 1: 1.0, 2: 3.0}
    result = read(by_seed_spec(), seeded_lines([3, 1, 2], slopes))
    assert result.conclusion is SUPPORTS, result.summary
    assert result.records["per_seed"] == {
        "total": 3,
        "included": 3,
        "incomplete": 0,
        "excluded_by_rule": 0,
    }
    assert result.statistics["typical_slope"] == pytest.approx(2.0)
    # The level of each seed's line is its mean y: 3.0+1, 1.5, 4.5.
    levels = [2 * 1.5 + 1, 1.0 * 1.5, 3.0 * 1.5]
    centre = sum(levels) / 3
    expected = math.sqrt(sum((item - centre) ** 2 for item in levels) / 2)
    assert result.statistics["spread"] == pytest.approx(expected)


def test_the_groups_do_not_depend_on_the_order_the_records_arrive_in() -> None:
    records = seeded_lines([3, 1, 2], {3: 2.0, 1: 1.0, 2: 3.0})
    first = read(by_seed_spec(), records).record()
    shuffled = list(records)
    random.Random(4).shuffle(shuffled)
    assert read(by_seed_spec(), shuffled).record() == first


def test_group_keys_are_typed_so_a_boolean_and_a_number_are_two_groups() -> None:
    analysis = spec(
        observables=[points(["g", "y"])],
        tables=[
            {
                "name": "groups",
                "source": "points",
                "by": ["g"],
                "aggregates": [{"name": "n", "op": "count"}],
            }
        ],
        reductions=[{"name": "k", "op": "count", "observable": "groups"}],
        primary_statistic="k",
    )
    records = [
        {"g": True, "y": 0},
        {"g": 1, "y": 0},
        {"g": 3, "y": 0},
        {"g": 3.0, "y": 0},
        {"g": "3", "y": 0},
    ]
    assert read(analysis, records).statistic == 4.0


def test_grouping_by_campaign_unit_is_grouping_by_what_each_unit_renews() -> None:
    """A campaign's records are concatenated; a unit is identified by what it renewed.

    Records carry no unit field, and Research OS adds none (the combined
    result is hashed as the units wrote it). Units that differ in the seed
    offset renew the ``seed`` field, which the capability declares, so
    grouping the combined records by ``seed`` is a per-unit statistic --
    the same number as reading each unit alone.
    """

    def unit_document(seed: int, slope: float) -> dict[str, Any]:
        return {
            "records": [
                {"x": x, "y": slope * x + 0.5, "seed": seed} for x in (0, 1, 2, 3, 4)
            ]
        }

    rule = [
        {
            "capability_observable": "points",
            "path": "records",
            "kind": "records",
            "rule": "concatenate",
            "identity": ["x", "seed"],
        }
    ]
    units = [
        (0, unit_document(7, 1.0)),
        (1, unit_document(8, 2.0)),
        (2, unit_document(9, 4.0)),
    ]
    combined = campaign.combine(rule, units)
    assert combined.ok, combined.detail
    result = engine.evaluate(by_seed_spec(), {SOURCE: combined.document})
    assert result.conclusion is SUPPORTS
    per_unit = [
        engine.evaluate(by_seed_spec(support=[]), {SOURCE: document}).statistics[
            "typical_slope"
        ]
        for _index, document in units
    ]
    assert per_unit == pytest.approx([1.0, 2.0, 4.0])
    assert result.statistics["typical_slope"] == pytest.approx(sorted(per_unit)[1])


def test_a_unit_identity_no_capability_declares_cannot_be_grouped_by(
    tmp_path: Path,
) -> None:
    """``cg.cells@1`` records carry no unit; grouping by one is CAPABILITY_LIMITED, exactly."""

    analysis = spec(
        observables=[
            {
                "name": "solves",
                "source": CG_SOURCE,
                "kind": "records",
                "path": "records",
                "fields": ["objective"],
            }
        ],
        tables=[
            {
                "name": "per_unit",
                "source": "solves",
                "by": ["campaign_unit"],
                "aggregates": [{"name": "best", "op": "min", "field": "objective"}],
            }
        ],
        reductions=[
            {"name": "spread", "op": "std", "observable": "per_unit", "field": "best"}
        ],
        primary_statistic="spread",
    )
    resolution = resolve_on_cg(analysis, tmp_path)
    assert not resolution.executable
    assert any("campaign_unit" in item.detail for item in resolution.unmet), (
        resolution.unmet
    )


# ======================================================== derived values --
def per_iteration_spec(policy: str = "insufficient") -> AnalysisSpec:
    return spec(
        observables=[points(["wall_seconds", "iterations"])],
        tables=[
            {
                "name": "cells",
                "source": "points",
                "compute": [
                    {"field": "per_iter", "expression": "wall_seconds / iterations"},
                    {"field": "log_iter", "expression": "log(iterations)"},
                ],
                "incomplete_records": policy,
            }
        ],
        reductions=[
            {"name": "cost", "op": "mean", "observable": "cells", "field": "per_iter"},
            {"name": "scale", "op": "max", "observable": "cells", "field": "log_iter"},
        ],
        primary_statistic="cost",
    )


def test_a_per_record_derived_value_is_computed_from_that_record() -> None:
    records = [
        {"wall_seconds": 2.0, "iterations": 4},
        {"wall_seconds": 3.0, "iterations": 3},
        {"wall_seconds": 9.0, "iterations": 6},
    ]
    result = read(per_iteration_spec(), records)
    assert result.conclusion is SUPPORTS, result.summary
    assert result.statistics["cost"] == pytest.approx((0.5 + 1.0 + 1.5) / 3)
    assert result.statistics["scale"] == pytest.approx(math.log(6))


def test_an_undefined_derived_value_is_insufficient_or_excluded_as_fixed_in_advance() -> (
    None
):
    records = [
        {"wall_seconds": 2.0, "iterations": 4},
        {"wall_seconds": 1.0, "iterations": 0},  # a zero denominator and log(0)
        {"wall_seconds": 9.0, "iterations": 6},
    ]
    strict = read(per_iteration_spec("insufficient"), records)
    assert strict.conclusion is INSUFFICIENT
    assert "1 of 3 records of table 'cells'" in strict.summary
    lenient = read(per_iteration_spec("exclude"), records)
    assert lenient.conclusion is SUPPORTS
    assert lenient.records["cells"]["incomplete"] == 1
    assert lenient.statistics["cost"] == pytest.approx((0.5 + 1.5) / 2)


def test_a_derived_field_is_computed_from_declared_fields_and_is_no_requirement() -> (
    None
):
    (need,) = sciencechain.requirements_from_analysis(per_iteration_spec())
    assert set(need.fields) == {"wall_seconds", "iterations"}
    assert set(need.numeric) == {"wall_seconds", "iterations"}
    assert "per_iter" not in need.fields and "log_iter" not in need.fields


# ============================================================== crossing --
def test_a_crossing_is_interpolated_linearly_between_the_adjacent_points() -> None:
    result = read(crossing_spec(), curve((0, 1.0), (1, 0.6), (2, 0.2), (3, 0.0)))
    assert result.statistic == pytest.approx(1.25)
    unsorted = read(crossing_spec(), curve((3, 0.0), (1, 0.6), (0, 1.0), (2, 0.2)))
    assert unsorted.statistic == result.statistic


@pytest.mark.parametrize(
    ("pick", "direction", "expected"),
    [
        ("first", "any", 0.5),
        ("last", "any", 1.5),
        ("first", "falling", 1.5),
        ("last", "rising", 0.5),
        ("single", "rising", 0.5),
        ("single", "falling", 1.5),
        ("single", "any", None),
    ],
)
def test_which_crossing_is_the_one_frozen_in_advance(
    pick: str, direction: str, expected: float | None
) -> None:
    result = read(
        crossing_spec(pick=pick, direction=direction),
        curve((0, 0.0), (1, 1.0), (2, 0.0)),
    )
    if expected is None:
        assert result.conclusion is INSUFFICIENT
        assert "crosses 0.5 2 times" in result.summary
    else:
        assert result.statistic == pytest.approx(expected)


def test_a_point_on_the_level_is_where_it_crosses_and_a_touch_is_not_a_crossing() -> (
    None
):
    on_level = read(crossing_spec(), curve((0, 0.0), (1, 0.5), (2, 1.0)))
    assert on_level.statistic == 1.0
    plateau = read(crossing_spec(), curve((0, 0.0), (1, 0.5), (2, 0.5), (3, 1.0)))
    assert plateau.statistic == 1.5
    touch = read(crossing_spec(), curve((0, 1.0), (1, 0.5), (2, 1.0)))
    assert touch.conclusion is INSUFFICIENT
    assert "does not cross" in touch.summary


@pytest.mark.parametrize(
    ("records", "why"),
    [
        (curve((0, 1.0), (0, 0.0), (1, 0.2)), "aggregate them first"),
        (curve((0, 1.0)), "at least two points"),
        (curve((0, 1.0), (1, 0.9), (2, 0.8)), "does not cross"),
    ],
)
def test_an_undefined_crossing_is_insufficient_never_an_endpoint(
    records: list[dict[str, Any]], why: str
) -> None:
    result = read(crossing_spec(), records)
    assert result.conclusion is INSUFFICIENT
    assert why in result.summary


def test_a_crossing_per_group_is_a_table_aggregate() -> None:
    analysis = spec(
        observables=[points(["seed", "x", "y"])],
        tables=[
            {
                "name": "per_seed",
                "source": "points",
                "by": ["seed"],
                "aggregates": [
                    {
                        "name": "xover",
                        "op": "crossing",
                        "field": "x",
                        "other_field": "y",
                        "crossing": {"level": 0.0, "pick": "single"},
                    }
                ],
            }
        ],
        reductions=[
            {
                "name": "where",
                "op": "median",
                "observable": "per_seed",
                "field": "xover",
            }
        ],
        primary_statistic="where",
    )
    records = [
        {"seed": seed, "x": x, "y": x - shift}
        for seed, shift in ((1, 1.0), (2, 1.5), (3, 2.0))
        for x in (0.0, 1.0, 2.0, 3.0)
    ]
    assert read(analysis, records).statistic == pytest.approx(1.5)


# =========================================================== permutation --
def association_spec(
    *,
    tail: str = "upper",
    by: list[str] | None = None,
    seed: int = 11,
    resamples: int = 2000,
) -> AnalysisSpec:
    return spec(
        observables=[points(["g", "x", "y"])],
        reductions=[
            {
                "name": "rho",
                "op": "rank_correlation",
                "observable": "points",
                "field": "x",
                "other_field": "y",
            },
            {
                "name": "p_value",
                "op": "permutation_p",
                "of": ["rho"],
                "observable": "points",
                "field": "y",
                "permutation": {
                    "by": by or [],
                    "tail": tail,
                    "resamples": resamples,
                    "seed": seed,
                },
            },
        ],
        primary_statistic="p_value",
        success={"comparator": "<", "threshold": 0.05},
        failure={"comparator": ">=", "threshold": 0.05},
    )


def noisy(n: int = 30, *, slope: float = 0.0, seed: int = 3) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [
        {
            "g": "a" if index % 2 else "b",
            "x": float(index),
            "y": slope * index + rng.gauss(0, 1),
        }
        for index in range(n)
    ]


def test_a_permutation_null_is_a_deterministic_function_of_its_frozen_seed() -> None:
    records = noisy(slope=0.05)
    first = read(association_spec(seed=11), records)
    again = read(association_spec(seed=11), records)
    other = read(association_spec(seed=12), records)
    assert first.record() == again.record() and first.summary == again.summary
    assert first.statistic != other.statistic
    assert 0.0 < first.statistic <= 1.0 and 0.0 < other.statistic <= 1.0


def test_a_perfect_association_has_the_smallest_p_value_the_draws_allow() -> None:
    records = [{"g": "a", "x": float(i), "y": math.exp(i / 10)} for i in range(30)]
    result = read(association_spec(resamples=2000), records)
    assert result.statistics["rho"] == pytest.approx(1.0)
    assert result.statistic == pytest.approx(1 / 2001)
    assert result.conclusion is SUPPORTS


def test_a_permutation_never_moves_a_value_out_of_its_stratum() -> None:
    """A statistic of one stratum's values is invariant under shuffling within strata."""

    analysis = spec(
        observables=[points(["g", "y"])],
        reductions=[
            {
                "name": "level",
                "op": "mean",
                "observable": "points",
                "field": "y",
                "where": [{"field": "g", "comparator": "==", "value": "a"}],
            },
            {
                "name": "p_value",
                "op": "permutation_p",
                "of": ["level"],
                "observable": "points",
                "field": "y",
                "permutation": {
                    "by": ["g"],
                    "tail": "upper",
                    "resamples": 500,
                    "seed": 1,
                },
            },
        ],
        primary_statistic="p_value",
    )
    records = [{"g": "a", "y": 10.0 + i} for i in range(5)] + [
        {"g": "b", "y": float(i)} for i in range(5)
    ]
    assert read(analysis, records).statistic == 1.0, "every draw ties the observation"
    mixed = analysis.model_copy(
        update={
            "reductions": (
                analysis.reductions[0],
                analysis.reductions[1].model_copy(
                    update={
                        "permutation": analysis.reductions[1].permutation.model_copy(
                            update={"by": ()}
                        )
                    }
                ),
            )
        }
    )
    assert read(mixed, records).statistic < 0.05, "without strata the values mix"


def test_the_tails_are_what_they_say() -> None:
    records = noisy(slope=0.04)
    lower = read(association_spec(tail="lower"), records).statistic
    upper = read(association_spec(tail="upper"), records).statistic
    both = read(association_spec(tail="two_sided"), records).statistic
    assert lower is not None and upper is not None and both is not None
    # Every draw is at most or at least the observation, a tie both: so the
    # two tails cover every draw once, and the ties twice.
    assert 1.0 + 1 / 2001 - 1e-12 <= lower + upper <= 1.1
    assert both == pytest.approx(min(1.0, 2 * min(lower, upper)))


def undefined_sometimes_spec(resamples: int) -> AnalysisSpec:
    return spec(
        observables=[points(["g", "y"])],
        reductions=[
            {
                "name": "ya",
                "op": "sum",
                "observable": "points",
                "field": "y",
                "where": [{"field": "g", "comparator": "==", "value": "a"}],
            },
            {"name": "inverse", "op": "expression", "expression": "1 / ya"},
            {
                "name": "p_value",
                "op": "permutation_p",
                "of": ["inverse"],
                "observable": "points",
                "field": "y",
                "permutation": {"tail": "upper", "resamples": resamples, "seed": 5},
            },
        ],
        primary_statistic="p_value",
    )


def test_a_permutation_where_the_statistic_is_undefined_counts_as_extreme() -> None:
    """One record of group ``a``; y holds a zero and 1..19. Observed ya = 1.

    Under the null ya is uniform on {0, ..., 19}: ``1 / ya`` is undefined in
    a twentieth of draws (counted as extreme) and ties the observation in
    another twentieth -- so p is about 0.1, not the 0.05 that dropping the
    undefined draws would report.
    """

    records = [{"g": "a", "y": 1.0}, {"g": "b", "y": 0.0}] + [
        {"g": "b", "y": float(value)} for value in range(2, 20)
    ]
    result = read(undefined_sometimes_spec(5000), records)
    assert result.statistics["inverse"] == 1.0
    assert result.statistic == pytest.approx(0.1, abs=0.015)


def test_a_null_mostly_undefined_is_not_a_distribution_and_gives_no_p_value() -> None:
    records = [{"g": "a", "y": 1.0}] + [{"g": "b", "y": 0.0}] * 3
    result = read(undefined_sometimes_spec(400), records)
    assert result.conclusion is INSUFFICIENT
    assert "permutations, so they are not a null distribution" in result.summary


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"uncertainty": {"method": "bootstrap_percentile"}}, "nested resampling"),
        ({"observable": "elsewhere"}, "does not read 'elsewhere'"),
        ({"strata": ["y"]}, "changes nothing"),
        ({"of": ["rho", "rho"]}, "exactly one earlier quantity"),
        ({"nested": True}, "a permutation of a permutation test"),
        ({"no_permutation": True}, "needs its null fixed"),
        ({"where": True}, "nothing else"),
    ],
)
def test_an_incoherent_permutation_is_refused_before_it_is_frozen(
    change: dict[str, Any], why: str
) -> None:
    permutation: dict[str, Any] = {
        "name": "p_value",
        "op": "permutation_p",
        "of": change.get("of", ["rho"]),
        "observable": change.get("observable", "points"),
        "field": "y",
        "permutation": {
            "by": change.get("strata", []),
            "tail": "upper",
            "resamples": 200,
        },
    }
    if change.get("no_permutation"):
        del permutation["permutation"]
    if change.get("where"):
        permutation["where"] = [{"field": "x", "comparator": ">", "value": 0}]
    reductions: list[dict[str, Any]] = [
        {
            "name": "rho",
            "op": "rank_correlation",
            "observable": "points",
            "field": "x",
            "other_field": "y",
        },
        permutation,
    ]
    if change.get("nested"):
        reductions.append(
            {
                **permutation,
                "name": "p_of_p",
                "of": ["p_value"],
                "permutation": {"tail": "upper", "resamples": 200},
            }
        )
    payload: dict[str, Any] = {
        "analysable": True,
        "estimand": "an association",
        "observables": [
            points(["x", "y"]),
            {
                "name": "elsewhere",
                "source": "results/other.json",
                "kind": "records",
                "path": "records",
            },
        ],
        "reductions": reductions,
        "primary_statistic": "p_of_p" if change.get("nested") else "p_value",
        "success": {"comparator": "<", "threshold": 0.05},
        "failure": {"comparator": ">=", "threshold": 0.05},
    }
    if "uncertainty" in change:
        payload["uncertainty"] = change["uncertainty"]
    with pytest.raises(ContractError, match=why):
        AnalysisSpec.model_validate(payload).check()


# ========================================================= correlations --
def correlation_spec(op: str, terms: list[str] | None = None) -> AnalysisSpec:
    reduction: dict[str, Any] = {
        "name": "r",
        "op": op,
        "observable": "points",
        "field": "y",
        "other_field": "x",
    }
    if terms:
        reduction["terms"] = terms
    return spec(
        observables=[points(["x", "y", "z", "w"])],
        reductions=[reduction],
        primary_statistic="r",
        success={"comparator": ">", "threshold": 2.0},
        failure={"comparator": "<", "threshold": -2.0},
    )


def test_a_rank_correlation_is_spearman_with_average_ranks_for_ties() -> None:
    records = [
        {"x": x, "y": y, "z": 0, "w": 0}
        for x, y in zip([1, 2, 3, 4, 5], [5, 6, 7, 8, 7], strict=True)
    ]
    # x ranks 1..5; y ranks 1, 2, 3.5, 5, 3.5: sxy = 8, sxx = 10, syy = 9.5.
    assert read(
        correlation_spec("rank_correlation"), records
    ).statistic == pytest.approx(8 / math.sqrt(95))
    monotone = [{"x": i, "y": math.exp(i), "z": 0, "w": 0} for i in range(6)]
    assert read(correlation_spec("rank_correlation"), monotone).statistic == 1.0
    flat = [{"x": i, "y": 2.0, "z": 0, "w": 0} for i in range(6)]
    assert read(correlation_spec("rank_correlation"), flat).conclusion is INSUFFICIENT


def _pearson(xs: list[float], ys: list[float]) -> float:
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    )


def _ranked(values: list[float]) -> list[float]:
    order = sorted(values)
    return [(2 * order.index(v) + order.count(v) + 1) / 2 for v in values]


def correlated(n: int = 40) -> list[dict[str, Any]]:
    rng = random.Random(9)
    out = []
    for _ in range(n):
        z = rng.gauss(0, 1)
        x = 0.8 * z + rng.gauss(0, 1)
        y = 0.6 * z + 0.3 * x + rng.gauss(0, 1)
        out.append({"x": x, "y": y, "z": z, "w": rng.gauss(0, 1)})
    return out


def test_a_partial_correlation_is_the_textbook_first_order_partial() -> None:
    records = correlated()
    xs, ys, zs = ([item[name] for item in records] for name in "xyz")
    rxy, rxz, ryz = _pearson(ys, xs), _pearson(xs, zs), _pearson(ys, zs)
    expected = (rxy - rxz * ryz) / math.sqrt((1 - rxz**2) * (1 - ryz**2))
    result = read(correlation_spec("partial_correlation", ["z"]), records)
    assert result.statistic == pytest.approx(expected, abs=1e-10)

    rx, ry, rz = _ranked(xs), _ranked(ys), _ranked(zs)
    sxy, sxz, syz = _pearson(ry, rx), _pearson(rx, rz), _pearson(ry, rz)
    spearman = (sxy - sxz * syz) / math.sqrt((1 - sxz**2) * (1 - syz**2))
    ranked = read(correlation_spec("partial_rank_correlation", ["z"]), records)
    assert ranked.statistic == pytest.approx(spearman, abs=1e-10)


@pytest.mark.parametrize(
    ("records", "terms", "why"),
    [
        (
            [{"x": i, "y": 2 * i + 1, "z": i, "w": 0} for i in range(10)],
            ["z"],
            "is determined by the terms",
        ),
        (
            [{"x": i, "y": i % 3, "z": i, "w": 2 * i} for i in range(10)],
            ["z", "w"],
            "collinear or constant",
        ),
        (
            [{"x": i, "y": i % 3, "z": i, "w": 1.0} for i in range(10)],
            ["w"],
            "collinear or constant",
        ),
        (
            [{"x": i, "y": i % 2, "z": i * i, "w": 0} for i in range(3)],
            ["z"],
            "needs at least 4 records",
        ),
    ],
)
def test_an_unidentified_partial_correlation_is_insufficient(
    records: list[dict[str, Any]], terms: list[str], why: str
) -> None:
    result = read(correlation_spec("partial_correlation", terms), records)
    assert result.conclusion is INSUFFICIENT
    assert why in result.summary


# =========================================================== equivalence --
def magnitude_spec(margin: float) -> AnalysisSpec:
    """A non-negative magnitude with an interval and a margin: an equivalence test."""

    return spec(
        observables=[points(["g", "y"])],
        tables=[
            {
                "name": "groups",
                "source": "points",
                "by": ["g"],
                "aggregates": [{"name": "level", "op": "mean", "field": "y"}],
            }
        ],
        reductions=[
            {"name": "top", "op": "max", "observable": "groups", "field": "level"},
            {"name": "bottom", "op": "min", "observable": "groups", "field": "level"},
            {"name": "magnitude", "op": "difference", "of": ["top", "bottom"]},
        ],
        primary_statistic="magnitude",
        uncertainty={
            "method": "bootstrap_percentile",
            "level": 0.9,
            "resamples": 400,
            "seed": 2,
        },
        success={"comparator": "<", "threshold": margin},
        failure={"comparator": ">=", "threshold": margin},
    )


def test_equivalence_is_the_whole_interval_below_the_margin() -> None:
    """TOST, as the existing interval rule already reads a magnitude against a margin.

    A non-negative magnitude with a (1 - 2 alpha) interval: equivalent when
    the whole interval is below the margin, not equivalent when the whole of
    it is at or above, and INCONCLUSIVE when it straddles -- which a margin at
    the point estimate always does.
    """

    rng = random.Random(1)
    records = [
        {"g": group, "y": shift + rng.gauss(0, 0.2)}
        for group, shift in (("a", 0.0), ("b", 0.3), ("c", 0.1))
        for _ in range(40)
    ]
    wide = read(magnitude_spec(5.0), records)
    assert wide.conclusion is SUPPORTS, wide.summary
    assert wide.interval is not None and wide.statistic is not None
    low, high = wide.interval
    assert 0.0 <= low < wide.statistic < high
    assert read(magnitude_spec(low / 2), records).conclusion is CONTRADICTS
    assert read(magnitude_spec(wide.statistic), records).conclusion is INCONCLUSIVE


# ================================================ support that cannot hold --
def c61e2198() -> dict[str, Any]:
    (path,) = (FIXTURES / "analysis_v2" / "cycle002").glob("c61e2198-*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_support_rule_no_data_can_meet_is_refused_before_it_is_frozen() -> None:
    """Cycle 001's structurally INCONCLUSIVE analysis, frozen then, refused now.

    ``min_distinct solver: 3`` beside ``median_cg`` selected to ``solver ==
    'cg_hist'``: the evaluator checks support over that selection too, where
    solver takes one value. Six contained executions were spent on it.
    """

    stored = AnalysisSpec.model_validate(c61e2198()["analysis"])
    with pytest.raises(
        ContractError,
        match="median_cg computes on the records of 'solves' where solver == 'cg_hist'",
    ):
        stored.check()


@pytest.mark.parametrize(
    ("include", "where", "distinct", "refused"),
    [
        ([], [{"field": "g", "comparator": "==", "value": "a"}], {"g": 2}, True),
        ([{"field": "g", "comparator": "==", "value": "a"}], [], {"g": 2}, True),
        ([], [{"field": "g", "comparator": "==", "value": "a"}], {"g": 1}, False),
        ([], [{"field": "g", "comparator": "!=", "value": "a"}], {"g": 2}, False),
        ([], [{"field": "x", "comparator": ">", "value": 1}], {"x": 3}, False),
    ],
)
def test_only_a_proved_impossibility_is_refused(
    include: list[dict[str, Any]],
    where: list[dict[str, Any]],
    distinct: dict[str, int],
    refused: bool,
) -> None:
    payload = {
        "analysable": True,
        "estimand": "a mean",
        "observables": [points(["g", "x", "y"], include=include)],
        "reductions": [
            {
                "name": "m",
                "op": "mean",
                "observable": "points",
                "field": "y",
                "where": where,
            }
        ],
        "primary_statistic": "m",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": -0.5},
        "support": [{"observable": "points", "min_distinct": distinct}],
    }
    analysis = AnalysisSpec.model_validate(payload)
    if refused:
        with pytest.raises(ContractError, match="no data can meet this rule"):
            analysis.check()
    else:
        analysis.check()


def test_a_count_and_a_fraction_are_exempt_because_their_selection_is_what_they_count() -> (
    None
):
    spec(
        observables=[points(["g", "y"])],
        reductions=[
            {
                "name": "share",
                "op": "fraction",
                "observable": "points",
                "where": [{"field": "g", "comparator": "==", "value": "a"}],
            }
        ],
        primary_statistic="share",
        support=[{"observable": "points", "min_distinct": {"g": 2}}],
    )


def test_diversity_across_selections_is_stated_on_a_table_grouped_by_the_field() -> (
    None
):
    """What cycle 001's analysis meant, written so that it can be met."""

    analysis = spec(
        observables=[points(["solver", "wall_seconds"])],
        tables=[{"name": "solvers", "source": "points", "by": ["solver"]}],
        reductions=[
            {
                "name": "median_cg",
                "op": "median",
                "observable": "points",
                "field": "wall_seconds",
                "where": [{"field": "solver", "comparator": "==", "value": "cg_hist"}],
            },
            {
                "name": "median_base",
                "op": "median",
                "observable": "points",
                "field": "wall_seconds",
                "where": [{"field": "solver", "comparator": "!=", "value": "cg_hist"}],
            },
            {"name": "ratio_cg", "op": "ratio", "of": ["median_cg", "median_base"]},
        ],
        primary_statistic="ratio_cg",
        success={"comparator": "<", "threshold": 0.8},
        failure={"comparator": ">=", "threshold": 1.0},
        support=[{"observable": "solvers", "min_records": 3}],
    )
    three = [
        {"solver": name, "wall_seconds": wall}
        for name, wall in (("cg_hist", 1.0), ("celer", 2.0), ("lars", 3.0))
    ] * 4
    assert read(analysis, three).conclusion is SUPPORTS
    two = [item for item in three if item["solver"] != "lars"]
    short = read(analysis, two)
    assert short.conclusion is INSUFFICIENT
    assert "'solvers' has 2 analysed record(s)" in short.summary


# ==================================== a table's support reaches the envelope --
def draw_planning(tmp_path: Path) -> shape.Planning:
    from tests.test_capability_planning import planning_here

    return planning_here(tmp_path)


def per_seed_draw(
    seeds: int, *, rule: str = "fixed_campaign", shape_units: int | None = None
) -> AnalysisSpec:
    payload: dict[str, Any] = {
        "analysable": True,
        "estimand": "the spread of the per-seed slope",
        "observables": [
            {
                "name": "points",
                "source": "results/draw.json",
                "kind": "records",
                "path": "records",
                "fields": ["x", "y", "seed"],
            }
        ],
        "tables": [
            {
                "name": "per_seed",
                "source": "points",
                "by": ["seed"],
                "aggregates": [
                    {
                        "name": "slope",
                        "op": "ols_coefficient",
                        "response": "y",
                        "terms": ["x"],
                        "coefficient": "x",
                    }
                ],
            }
        ],
        "reductions": [
            {"name": "spread", "op": "std", "observable": "per_seed", "field": "slope"}
        ],
        "primary_statistic": "spread",
        "success": {"comparator": "<", "threshold": 0.05},
        "failure": {"comparator": ">=", "threshold": 0.05},
        "support": [{"observable": "per_seed", "min_records": seeds}],
        "stopping_rule": rule,
    }
    if shape_units is not None:
        payload["execution_shape"] = {"units": shape_units, "unit_varies": ["seeds"]}
    analysis = AnalysisSpec.model_validate(payload)
    analysis.check()
    return analysis


def test_a_tables_support_is_the_statistics_effective_population_before_freezing(
    tmp_path: Path,
) -> None:
    """One execution draws one seed; a table of three seeds needs three units.

    Without the implied requirement the check saw no support rule on the
    observable and called every shape valid -- the structurally INCONCLUSIVE
    execution the new language would otherwise have made easy to freeze.
    """

    planning = draw_planning(tmp_path)
    single = planning.check(per_seed_draw(3, rule="fixed_single_execution"))
    assert single.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert any(
        "distinct seed of points: needs 3" in item for item in single.problems
    ), single.problems
    assert (
        planning.check(per_seed_draw(3, shape_units=3)).verdict
        is ExecutionShapeVerdict.VALID_CAMPAIGN
    )
    assert planning.check(per_seed_draw(3, shape_units=2)).refused
    assert (
        planning.check(per_seed_draw(7)).verdict
        is ExecutionShapeVerdict.CAPABILITY_LIMITED
    )


def test_a_requirement_on_what_a_table_computes_implies_nothing_and_stays_allowed(
    tmp_path: Path,
) -> None:
    analysis = spec(
        observables=[
            {
                "name": "points",
                "source": "results/draw.json",
                "kind": "records",
                "path": "records",
                "fields": ["x", "y", "seed"],
            }
        ],
        tables=[
            {
                "name": "scaled",
                "source": "points",
                "compute": [{"field": "z", "expression": "y * 1000"}],
            }
        ],
        reductions=[{"name": "m", "op": "mean", "observable": "scaled", "field": "z"}],
        primary_statistic="m",
        support=[{"observable": "scaled", "min_distinct": {"z": 50}}],
    )
    assert analysis.implied_support()[0].min_distinct == {}
    assert (
        draw_planning(tmp_path).check(analysis).verdict
        is ExecutionShapeVerdict.VALID_SINGLE_EXECUTION
    )


# ================================================================ refusals --
@pytest.mark.parametrize(
    "op", ["logistic_coefficient", "tost", "eval", "spearman", "join"]
)
def test_an_unsupported_operation_fails_closed(op: str) -> None:
    with pytest.raises(ValidationError):
        Reduction.model_validate(
            {"name": "q", "op": op, "observable": "points", "field": "y"}
        )


@pytest.mark.parametrize(
    ("tables", "reductions", "why"),
    [
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "v", "op": "permutation_p", "of": ["m"]}],
                }
            ],
            None,
            "not an operation over a group's records",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [
                        {"name": "v", "op": "expression", "expression": "1"}
                    ],
                }
            ],
            None,
            "not an operation over a group's records",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "v", "op": "value"}],
                }
            ],
            None,
            "not an operation over a group's records",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [
                        {
                            "name": "v",
                            "op": "mean",
                            "field": "y",
                            "observable": "points",
                        }
                    ],
                }
            ],
            None,
            "names no observable",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "aggregates": [{"name": "v", "op": "mean", "field": "y"}],
                }
            ],
            None,
            "aggregates summarise groups",
        ),
        ([{"name": "t", "source": "points"}], None, "its source again"),
        (
            [
                {
                    "name": "t",
                    "source": "nowhere",
                    "compute": [{"field": "z", "expression": "y"}],
                }
            ],
            None,
            "not a records observable or an earlier table",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "count",
                    "compute": [{"field": "z", "expression": "y"}],
                }
            ],
            None,
            "is a scalar",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "compute": [
                        {"field": "a", "expression": "b"},
                        {"field": "b", "expression": "y"},
                    ],
                }
            ],
            None,
            "computed after it",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "compute": [{"field": "y", "expression": "y * 2"}],
                }
            ],
            None,
            "a derived field takes a name of its own",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "v", "op": "mean", "field": "y"}],
                    "compute": [{"field": "z", "expression": "x"}],
                }
            ],
            None,
            "hold only its keys",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "g", "op": "count"}],
                }
            ],
            None,
            "must all be distinct",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "v", "op": "count"}],
                }
            ],
            [{"name": "m", "op": "mean", "observable": "t", "field": "y"}],
            "does not hold",
        ),
        (
            [
                {
                    "name": "t",
                    "source": "points",
                    "by": ["g"],
                    "aggregates": [{"name": "v", "op": "count"}],
                    "include": [{"field": "y", "comparator": ">", "value": 0}],
                }
            ],
            None,
            "does not hold",
        ),
        (
            [
                {"name": "t", "source": "u", "by": ["g"]},
                {"name": "u", "source": "points", "by": ["g"]},
            ],
            None,
            "not a records observable or an earlier table",
        ),
    ],
)
def test_an_incoherent_table_fails_closed_before_it_is_frozen(
    tables: list[dict[str, Any]], reductions: list[dict[str, Any]] | None, why: str
) -> None:
    payload = {
        "analysable": True,
        "estimand": "a table",
        "observables": [
            points(["g", "x", "y"]),
            {"name": "count", "source": SOURCE, "kind": "scalar", "path": "n"},
        ],
        "tables": tables,
        "reductions": reductions
        or [{"name": "m", "op": "mean", "observable": "points", "field": "y"}],
        "primary_statistic": "m",
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": -0.5},
    }
    with pytest.raises(ContractError, match=why):
        AnalysisSpec.model_validate(payload).check()


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"crossing": {"level": 0.5}}, "only a crossing takes 'crossing'"),
        (
            {"permutation": {"tail": "upper"}},
            "only a permutation_p takes 'permutation'",
        ),
        ({"expression": "y + 1"}, "only an expression takes 'expression'"),
    ],
)
def test_the_new_parameters_belong_to_their_own_operation_only(
    extra: dict[str, Any], why: str
) -> None:
    with pytest.raises(ContractError, match=why):
        AnalysisSpec.model_validate(
            {
                "analysable": True,
                "estimand": "a mean",
                "observables": [points(["y"])],
                "reductions": [
                    {
                        "name": "m",
                        "op": "mean",
                        "observable": "points",
                        "field": "y",
                        **extra,
                    }
                ],
                "primary_statistic": "m",
                "success": {"comparator": ">", "threshold": 0.5},
                "failure": {"comparator": "<", "threshold": -0.5},
            }
        ).check()


def test_an_expression_quantity_reads_only_earlier_quantities() -> None:
    base: dict[str, Any] = {
        "analysable": True,
        "estimand": "a combination",
        "observables": [points(["y"])],
        "success": {"comparator": ">", "threshold": 0.5},
        "failure": {"comparator": "<", "threshold": -0.5},
    }
    with pytest.raises(ContractError, match="not reductions computed earlier"):
        AnalysisSpec.model_validate(
            {
                **base,
                "reductions": [
                    {"name": "e", "op": "expression", "expression": "m * 2"},
                    {"name": "m", "op": "mean", "observable": "points", "field": "y"},
                ],
                "primary_statistic": "e",
            }
        ).check()
    with pytest.raises(ContractError, match="takes nothing else"):
        AnalysisSpec.model_validate(
            {
                **base,
                "reductions": [
                    {
                        "name": "e",
                        "op": "expression",
                        "expression": "1",
                        "observable": "points",
                    }
                ],
                "primary_statistic": "e",
            }
        ).check()


def test_every_new_list_and_count_is_bounded() -> None:
    table = {
        "name": "t",
        "source": "points",
        "compute": [{"field": "z", "expression": "y"}],
    }
    with pytest.raises(ValidationError, match="at most 8 tables"):
        AnalysisSpec.model_validate(
            {
                "analysable": False,
                "tables": [dict(table, name=f"t{i}") for i in range(9)],
            }
        )
    with pytest.raises(ValidationError, match="at most 4 distinct fields"):
        Table.model_validate({"name": "t", "source": "points", "by": list("abcde")})
    with pytest.raises(ValidationError, match="at most 8 aggregates"):
        Table.model_validate(
            {
                "name": "t",
                "source": "p",
                "by": ["g"],
                "aggregates": [{"name": f"a{i}", "op": "count"} for i in range(9)],
            }
        )
    with pytest.raises(ValidationError, match="at most 8 computed"):
        Table.model_validate(
            {
                "name": "t",
                "source": "p",
                "compute": [{"field": f"c{i}", "expression": "1"} for i in range(9)],
            }
        )
    for resamples in (99, 20_001):
        with pytest.raises(ValidationError):
            Reduction.model_validate(
                {
                    "name": "p",
                    "op": "permutation_p",
                    "permutation": {"tail": "upper", "resamples": resamples},
                }
            )
    with pytest.raises(ValidationError):
        Reduction.model_validate(
            {"name": "c", "op": "crossing", "crossing": {"level": float("nan")}}
        )


def test_an_unanalysable_answer_carries_no_table() -> None:
    with pytest.raises(ContractError, match="carries a reason and nothing else"):
        AnalysisSpec.model_validate(
            {
                "analysable": False,
                "unanalysable_reason": "nothing measures it",
                "tables": [
                    {
                        "name": "t",
                        "source": "points",
                        "compute": [{"field": "z", "expression": "y"}],
                    }
                ],
            }
        ).check()


def test_a_threshold_in_a_tables_words_is_refused_like_any_other() -> None:
    with pytest.raises(ContractError, match="restates the threshold 0.25"):
        spec(
            observables=[points(["y"])],
            tables=[
                {
                    "name": "t",
                    "source": "points",
                    "compute": [{"field": "z", "expression": "y"}],
                    "description": "kept when z clears 0.25",
                }
            ],
            reductions=[{"name": "m", "op": "mean", "observable": "t", "field": "z"}],
            primary_statistic="m",
            success={"comparator": ">", "threshold": 0.25},
            failure={"comparator": "<", "threshold": 0.1},
        )


# ================================================================== bounds --
def test_a_permutation_past_its_work_bound_is_insufficient_without_being_drawn() -> (
    None
):
    analysis = association_spec(resamples=20_000)
    records = noisy(n=600)
    started = time.perf_counter()
    result = read(analysis, records)
    assert time.perf_counter() - started < 5.0
    assert result.conclusion is INSUFFICIENT
    assert f"at most {engine.MAX_RESAMPLING_WORK}" in result.summary


def test_a_bootstrap_over_tables_counts_the_tables_it_derives_again() -> None:
    analysis = by_seed_spec(
        uncertainty={"method": "bootstrap_percentile", "resamples": 5000, "seed": 1},
        success={"comparator": ">", "threshold": 0.5},
        failure={"comparator": "<", "threshold": 0.1},
    )
    records = [{"seed": i % 50, "x": float(i % 4), "y": float(i)} for i in range(2100)]
    result = read(analysis, records)
    assert result.conclusion is INSUFFICIENT
    assert "record draws" in result.summary and "at most" in result.summary


# ============================================================ determinism --
def test_a_reading_in_the_second_language_is_a_function_of_data_and_contract() -> None:
    records = noisy(slope=0.05)
    permuted = [read(association_spec(), records).record() for _ in range(2)]
    assert permuted[0] == permuted[1]
    bootstrapped = [
        read(
            magnitude_spec(5.0), [{"g": i % 3, "y": float(i % 7)} for i in range(60)]
        ).record()
        for _ in range(2)
    ]
    assert bootstrapped[0] == bootstrapped[1]


# ================================================================ history --
def cycle002() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / "analysis_v2" / "cycle002").glob("*.json"))
    ]


def test_every_analysis_frozen_in_the_first_language_rebuilds_to_its_digest_and_bytes() -> (
    None
):
    """The five analyses the live state froze (cycle 001's read one, cycle 002's four)."""

    found = cycle002()
    assert len(found) == 5
    for name, document in found:
        assert (
            hashlib.sha256(
                (FIXTURES / "analysis_v2" / "cycle002" / name).read_bytes()
            ).hexdigest()
            in name
        ), "the fixture is the stored artifact, by its address"
        analysis = AnalysisSpec.model_validate(document["analysis"])
        assert analysis.language() == 1
        assert scicontract.analysis_digest(analysis) == document["analysis_digest"]
        assert scicontract.analysis_payload(analysis) == document["analysis"]
        assert document["schema"] == scicontract.ANALYSIS_SCHEMA


def test_a_first_language_analysis_dumps_no_key_the_second_added() -> None:
    analysis = AnalysisSpec.model_validate(c61e2198()["analysis"])
    dumped = analysis.model_dump(mode="json")
    assert "tables" not in dumped
    for reduction in dumped["reductions"]:
        assert not {"crossing", "permutation", "expression"} & set(reduction)


def test_the_first_language_is_read_exactly_as_the_base_engine_read_it() -> None:
    """Fourteen readings by ``portfolio.analysis@1`` at the base SHA, reproduced.

    ``tests/fixtures/analysis_v2/v1_readings_by_base_engine.json`` was written
    by the base commit's engine (1b4ff5a): cycle 001's frozen analysis and
    analyses using every first-language operation, the bootstrap and the
    Wilson interval, incompleteness and subset support, each on two data
    sets. Every number, interval, note and sentence is reproduced; only the
    engine's name moved.
    """

    fixture = json.loads(
        (FIXTURES / "analysis_v2" / "v1_readings_by_base_engine.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["engine"] == "portfolio.analysis@1"
    assert len(fixture["readings"]) == 14
    for item in fixture["readings"]:
        analysis = AnalysisSpec.model_validate(item["analysis"])
        assert analysis.language() == 1
        result = engine.evaluate(
            analysis, {CG_SOURCE: fixture["datasets"][item["dataset"]]}
        )
        reading = {**result.record(), "summary": result.summary}
        expected = dict(item["reading"])
        assert expected.pop("engine") == "portfolio.analysis@1"
        assert reading.pop("engine") == engine.ENGINE_VERSION == "portfolio.analysis@2"
        assert json.dumps(reading, sort_keys=True) == json.dumps(
            expected, sort_keys=True
        ), item["name"]


def test_a_second_language_analysis_is_hashed_under_its_own_version() -> None:
    analysis = by_seed_spec()
    assert analysis.language() == 2
    digest = scicontract.analysis_digest(analysis)
    assert digest.startswith("panalysis-v2:")
    payload = scicontract.analysis_payload(analysis)
    rebuilt = AnalysisSpec.model_validate(json.loads(json.dumps(payload)))
    assert scicontract.analysis_digest(rebuilt) == digest
    assert rebuilt == analysis


# ====================================== the internal API cannot bypass it --
def test_a_second_language_analysis_built_without_its_checks_is_not_read() -> None:
    """``model_construct`` skips every validator; the engine checks again and refuses."""

    loose = AnalysisSpec.model_construct(
        **{
            **by_seed_spec().__dict__,
            "tables": (
                Table.model_construct(
                    name="per_seed",
                    source="missing",
                    by=("seed",),
                    aggregates=(),
                    compute=(),
                    include=(),
                    incomplete_records="insufficient",
                    description="",
                ),
            ),
        }
    )
    result = read(loose, seeded_lines([1, 2, 3], {1: 1.0, 2: 1.0, 3: 1.0}))
    assert result.conclusion is INSUFFICIENT
    assert "not one this engine can read" in result.summary

    unparsed = AnalysisSpec.model_construct(
        **{
            **spec(
                observables=[points(["y"])],
                reductions=[
                    {"name": "m", "op": "mean", "observable": "points", "field": "y"},
                    {"name": "e", "op": "expression", "expression": "m + 1"},
                ],
                primary_statistic="e",
            ).__dict__,
            "reductions": (
                Reduction.model_validate(
                    {"name": "m", "op": "mean", "observable": "points", "field": "y"}
                ),
                Reduction.model_construct(
                    name="e",
                    op="expression",
                    expression="__import__('os')",
                    of=(),
                    observable="",
                    field="",
                    other_field="",
                    where=(),
                    q=None,
                    response="",
                    terms=(),
                    coefficient="",
                    crossing=None,
                    permutation=None,
                ),
            ),
        }
    )
    refused = read(unparsed, [{"y": 1.0}, {"y": 2.0}])
    assert refused.conclusion is INSUFFICIENT


def test_a_stored_analysis_whose_expression_is_not_in_the_language_does_not_load() -> (
    None
):
    payload = scicontract.analysis_payload(per_iteration_spec())
    payload["tables"][0]["compute"][0]["expression"] = "wall_seconds ** iterations"
    with pytest.raises(ValidationError):
        AnalysisSpec.model_validate(payload)


def test_what_a_table_reads_is_a_capability_requirement_the_capability_must_meet(
    tmp_path: Path,
) -> None:
    """A table cannot make ``cg.cells@1`` observe what it does not return."""

    def through_a_table(field: str) -> AnalysisSpec:
        return spec(
            observables=[
                {
                    "name": "solves",
                    "source": CG_SOURCE,
                    "kind": "records",
                    "path": "records",
                    "fields": ["family"],
                }
            ],
            tables=[
                {
                    "name": "families",
                    "source": "solves",
                    "by": ["family"],
                    "aggregates": [{"name": "level", "op": "mean", "field": field}],
                }
            ],
            reductions=[
                {
                    "name": "spread",
                    "op": "std",
                    "observable": "families",
                    "field": "level",
                }
            ],
            primary_statistic="spread",
        )

    assert resolve_on_cg(through_a_table("support_recall"), tmp_path).executable
    for missing in ("duality_gap_trace", "beta", "warm_start"):
        resolution = resolve_on_cg(through_a_table(missing), tmp_path)
        assert not resolution.executable
        assert any(missing in item.detail for item in resolution.unmet)
    # ...nor read a string as a number because a table computes with it.
    computed = spec(
        observables=[
            {
                "name": "solves",
                "source": CG_SOURCE,
                "kind": "records",
                "path": "records",
            }
        ],
        tables=[
            {
                "name": "t",
                "source": "solves",
                "compute": [{"field": "z", "expression": "solver * 2"}],
            }
        ],
        reductions=[{"name": "m", "op": "mean", "observable": "t", "field": "z"}],
        primary_statistic="m",
    )
    resolution = resolve_on_cg(computed, tmp_path)
    assert not resolution.executable
    assert any("solver" in item.detail for item in resolution.unmet)


def test_every_analysis_operations_mutant_still_applies() -> None:
    """``tests/analysis_operations_mutations.py`` cannot rot silently."""

    import ast

    from tests.analysis_operations_mutations import MUTANTS

    root = Path(__file__).resolve().parents[1]
    ids = [item.id for item in MUTANTS]
    assert len(ids) == len(set(ids)) and len(ids) >= 12
    for mutant in MUTANTS:
        source = (root / mutant.file).read_text(encoding="utf-8")
        assert source.count(mutant.find) == 1, mutant.id
        for test in mutant.tests:
            path, _, name = test.partition("::")
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
            }
            assert name.split("[")[0] in names, (mutant.id, test)
