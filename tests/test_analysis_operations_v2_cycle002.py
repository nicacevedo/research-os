"""The four analyses Cycle 002 could not write, against the real ``cg.cells@1``.

Cycle 002 of the live column-generation run stopped ``PAUSED_NO_FRONTIER``
with 33.27 USD of authority left: its four INVESTIGATING ideas were each
blocked by a frozen analysis that said, in prose, that the first analysis
language could not express it. The stored documents are in
``tests/fixtures/analysis_v2/cycle002/`` verbatim, each named by its
artifact address. For each idea this module holds, against the committed
``cg.cells@1`` declaration (``tests/fixtures/cg_cells_research_capabilities.yaml``,
sha256 09b336d1...) and the run's human bounds (6 units, 10 800 s):

- ``7d78decb`` -- **expressible.** Per-family transition points by two
  pre-specified schemes (linear interpolation along lambda_ratio and along
  log lambda_ratio), their per-family shift, its spread across families, and
  an equivalence decision of that spread against a margin through the
  existing interval rule. Capability-limited in part: ``support_recall``
  exists only for the planted-truth families, so two of the six it names
  cannot contribute. Malformed in part: the permutation null it proposed --
  scheme labels shuffled within family -- flips each family's shift and
  tests the scheme's main effect, not the interaction (the falsifier's own
  objection, Anderson & ter Braak 2003), and a TOST is not calibrated by a
  null of no effect. Neither is reproduced.
- ``35ef9d24`` -- **expressible, and honestly CAPABILITY_LIMITED.** Per-seed
  bracket interpolation of log(cg_hist / celer median wall time) along k/p,
  the cross-seed spread per family and its within-seed permutation null are
  all expressible; the sample it needs -- 8 seeds x 4 k/p instances x 3
  families = 96 instances -- is four times what a campaign can hold (6 units
  x 4 instances), and the pre-freeze check now says so before anything is
  frozen. Its ``single`` crossing convention makes most permutations
  undefined, which the falsifier objected to and the engine now reports.
- ``d1b13797`` -- **not expressible, and should not be.** Its comparison
  target, "the parent ablation's toggle-induced shift", was never measured:
  the parent (``c09ac546``) is PARKED and has no contract. No capability
  observes a toggle, and a record carries no execution unit; ~30 executions
  is five times the campaign bound.
- ``e3906d0e`` -- **expressible, except its selection rule.** Per-record
  derived fields, the partial Spearman correlation after removing k/p, log
  iterations and lambda_ratio, the screens, the confound diagnostic and a
  permutation p-value of the largest screened correlation are expressible;
  its subset -- not-ok cells OR lambda_ratio at or above each family's own
  75th percentile -- is a disjunction with a data-dependent threshold, which
  the closed condition language does not have. ``condition`` is the constant
  1.0 outside ``illcond``, so the full-covariate model it asks for is
  unidentified in three of its four families.
"""

from __future__ import annotations

import copy
import json
import math
import random
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from research_os.portfolio import analysis as engine
from research_os.portfolio import scicontract
from research_os.portfolio.contracts import ANALYSIS_OPERATIONS, AnalysisSpec
from research_os.portfolio.models import EmpiricalConclusion, ExecutionShapeVerdict
from tests.test_analysis_operations_v2 import FIXTURES, cg_planning, resolve_on_cg

SOURCE = "results/ros/cells.json"
CYCLE002 = FIXTURES / "analysis_v2" / "cycle002"
FIRST_LANGUAGE = (
    "value, count, fraction, mean, median, std, min, max, sum, quantile, "
    "difference, ratio, correlation, ols_coefficient"
)


def stored(idea: str) -> dict[str, Any]:
    (path,) = CYCLE002.glob(f"{idea}-*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def solves(
    fields: list[str], *, include: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "name": "solves",
        "source": SOURCE,
        "kind": "records",
        "path": "records",
        "fields": fields,
        "include": include or [],
        "incomplete_records": "exclude",
    }


def checked(payload: dict[str, Any]) -> AnalysisSpec:
    analysis = AnalysisSpec.model_validate(payload)
    analysis.check()
    return analysis


# ===================================================== what was observed --
@pytest.mark.parametrize("idea", ["7d78decb", "35ef9d24", "d1b13797", "e3906d0e"])
def test_each_blocked_analysis_was_frozen_unanalysable_in_the_first_language(
    idea: str,
) -> None:
    document = stored(idea)
    analysis = AnalysisSpec.model_validate(document["analysis"])
    assert not analysis.analysable and analysis.language() == 1
    assert scicontract.analysis_digest(analysis) == document["analysis_digest"]
    assert document["provenance"]["analysis_prompt"] == "analysis_designer@4"
    assert document["execution_shape_check"]["verdict"] == "UNRESOLVED"


def test_the_vocabulary_the_refusals_name_is_the_first_language_and_no_longer_all_of_it() -> (
    None
):
    reasons = {
        idea: stored(idea)["analysis"]["unanalysable_reason"]
        for idea in ("7d78decb", "35ef9d24")
    }
    first = [item.strip() for item in FIRST_LANGUAGE.split(",")]
    for reason in reasons.values():
        # Each refusal lists the first language's fourteen operations.
        assert all(
            f"{name}," in reason or f"{name})" in reason or f"or {name}" in reason
            for name in first
        ), reason
    assert list(ANALYSIS_OPERATIONS[: len(first)]) == first
    assert {
        "crossing",
        "permutation_p",
        "rank_correlation",
        "partial_rank_correlation",
        "expression",
    } <= set(ANALYSIS_OPERATIONS)
    for missing in ("logistic", "tost", "join"):
        assert not any(missing in item for item in ANALYSIS_OPERATIONS)


# ================================================================ 7d78decb --
def transition_points() -> dict[str, Any]:
    return {
        "analysable": True,
        "estimand": "the spread across families of the shift between two transition-point schemes",
        "population": "cg_hist solves of the planted-truth families at one tolerance",
        "target_claim": "the shift between the two schemes is the same in every family, within the margin",
        "observables": [
            solves(
                ["family", "seed", "lambda_ratio", "support_recall"],
                include=[{"field": "solver", "comparator": "==", "value": "cg_hist"}],
            )
        ],
        "tables": [
            {
                "name": "curve",
                "source": "solves",
                "by": ["family", "lambda_ratio"],
                "aggregates": [
                    {"name": "recall", "op": "mean", "field": "support_recall"}
                ],
                "compute": [{"field": "log_lambda", "expression": "log(lambda_ratio)"}],
            },
            {
                "name": "transition",
                "source": "curve",
                "by": ["family"],
                "aggregates": [
                    {
                        "name": "t_linear",
                        "op": "crossing",
                        "field": "lambda_ratio",
                        "other_field": "recall",
                        "crossing": {
                            "level": 0.5,
                            "pick": "first",
                            "direction": "falling",
                        },
                    },
                    {
                        "name": "t_log",
                        "op": "crossing",
                        "field": "log_lambda",
                        "other_field": "recall",
                        "crossing": {
                            "level": 0.5,
                            "pick": "first",
                            "direction": "falling",
                        },
                    },
                ],
                "compute": [{"field": "shift", "expression": "t_linear - exp(t_log)"}],
            },
        ],
        "reductions": [
            {
                "name": "shift_max",
                "op": "max",
                "observable": "transition",
                "field": "shift",
            },
            {
                "name": "shift_min",
                "op": "min",
                "observable": "transition",
                "field": "shift",
            },
            {
                "name": "interaction",
                "op": "difference",
                "of": ["shift_max", "shift_min"],
            },
        ],
        "primary_statistic": "interaction",
        "uncertainty": {
            "method": "bootstrap_percentile",
            "level": 0.9,
            "resamples": 300,
            "seed": 7,
        },
        "success": {"comparator": "<", "threshold": 0.005},
        "failure": {"comparator": ">=", "threshold": 0.005},
        "support": [
            {
                "observable": "solves",
                "min_records": 40,
                "min_distinct": {"family": 4, "lambda_ratio": 5, "seed": 3},
            },
            {"observable": "transition", "min_records": 4},
        ],
        "stopping_rule": "fixed_campaign",
        "execution_shape": {
            "capability": "cg.cells@1",
            "units": 6,
            "unit_varies": ["seeds"],
        },
    }


def recall_curves(
    centres: dict[str, float], *, unplanted: bool = False
) -> dict[str, Any]:
    rng = random.Random(2)
    records = []
    for family, centre in centres.items():
        for seed in range(6):
            for lam in (0.1, 0.25, 0.4, 0.55, 0.7):
                record = {
                    "family": family,
                    "seed": 1000 + seed,
                    "lambda_ratio": lam,
                    "solver": "cg_hist",
                }
                if not unplanted or family not in {"correlated", "historical"}:
                    recall = 1 / (1 + math.exp((lam - centre) * 12)) + rng.gauss(
                        0, 0.01
                    )
                    record["support_recall"] = min(1.0, max(0.0, recall))
                records.append(record)
    return {"records": records}


def test_7d78decb_is_expressible_and_capability_resolves(tmp_path: Path) -> None:
    analysis = checked(transition_points())
    assert analysis.language() == 2
    assert resolve_on_cg(analysis, tmp_path).executable
    assert (
        cg_planning(tmp_path).check(analysis).verdict
        is ExecutionShapeVerdict.VALID_CAMPAIGN
    )


def test_7d78decb_decides_equivalence_and_its_absence_from_the_measured_curves() -> (
    None
):
    analysis = checked(transition_points())
    additive = engine.evaluate(
        analysis,
        {
            SOURCE: recall_curves(
                {"sparse": 0.4, "toeplitz": 0.4, "block": 0.4, "illcond": 0.4}
            )
        },
    )
    assert additive.conclusion is EmpiricalConclusion.SUPPORTS, additive.summary
    assert additive.interval is not None and additive.interval[1] < 0.005
    # Families whose curves cross where the two schemes disagree by different
    # amounts: the shift between the schemes depends on the family.
    interacting = engine.evaluate(
        analysis,
        {
            SOURCE: recall_curves(
                {"sparse": 0.2, "toeplitz": 0.33, "block": 0.48, "illcond": 0.62}
            )
        },
    )
    assert interacting.conclusion is EmpiricalConclusion.CONTRADICTS, (
        interacting.summary
    )
    assert interacting.records["transition"]["included"] == 4


def test_7d78decb_cannot_measure_the_families_without_a_planted_truth() -> None:
    """``support_recall`` exists only for sparse, illcond, toeplitz and block."""

    six = {
        "sparse": 0.4,
        "toeplitz": 0.4,
        "block": 0.4,
        "illcond": 0.4,
        "correlated": 0.4,
        "historical": 0.4,
    }
    payload = transition_points()
    payload["support"][0]["min_distinct"]["family"] = 6
    analysis = checked(payload)
    result = engine.evaluate(analysis, {SOURCE: recall_curves(six, unplanted=True)})
    assert result.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert result.records["solves"]["incomplete"] == 60
    assert "'family' takes 4 distinct value(s)" in result.summary


# ================================================================ 35ef9d24 --
def crossover(pick: str = "first") -> dict[str, Any]:
    def per_family(name: str) -> list[dict[str, Any]]:
        where = [{"field": "family", "comparator": "==", "value": name}]
        return [
            {
                "name": f"q3_{name}",
                "op": "quantile",
                "observable": "seeds",
                "field": "xover",
                "q": 0.75,
                "where": where,
            },
            {
                "name": f"q1_{name}",
                "op": "quantile",
                "observable": "seeds",
                "field": "xover",
                "q": 0.25,
                "where": where,
            },
            {
                "name": f"iqr_{name}",
                "op": "difference",
                "of": [f"q3_{name}", f"q1_{name}"],
            },
            {
                "name": f"p_{name}",
                "op": "permutation_p",
                "of": [f"iqr_{name}"],
                "observable": "inst",
                "field": "log_ratio",
                "permutation": {
                    "by": ["family", "seed"],
                    "tail": "lower",
                    "resamples": 1000,
                    "seed": 11,
                },
            },
        ]

    return {
        "analysable": True,
        "estimand": "per family, the cross-seed spread of the interpolated k/p crossover, against a within-seed shuffle",
        "population": "cg_hist against celer at the parent's fixed baseline",
        "target_claim": "the crossover is tighter across seeds than a shuffled pairing allows, in every tested family",
        "observables": [solves(["family", "seed", "k", "p", "solver", "wall_seconds"])],
        "tables": [
            {
                "name": "inst",
                "source": "solves",
                "by": ["family", "seed", "k", "p"],
                "aggregates": [
                    {
                        "name": "cg",
                        "op": "median",
                        "field": "wall_seconds",
                        "where": [
                            {"field": "solver", "comparator": "==", "value": "cg_hist"}
                        ],
                    },
                    {
                        "name": "base",
                        "op": "median",
                        "field": "wall_seconds",
                        "where": [
                            {"field": "solver", "comparator": "==", "value": "celer"}
                        ],
                    },
                ],
                "compute": [
                    {"field": "kp", "expression": "k / p"},
                    {"field": "log_ratio", "expression": "log(cg / base)"},
                ],
            },
            {
                "name": "seeds",
                "source": "inst",
                "by": ["family", "seed"],
                "aggregates": [
                    {
                        "name": "xover",
                        "op": "crossing",
                        "field": "kp",
                        "other_field": "log_ratio",
                        "crossing": {"level": 0.0, "pick": pick},
                    }
                ],
            },
        ],
        "reductions": [
            *per_family("sparse"),
            *per_family("toeplitz"),
            *per_family("block"),
            {
                "name": "worst",
                "op": "expression",
                "expression": "max(p_sparse, p_toeplitz, p_block)",
            },
        ],
        "primary_statistic": "worst",
        "success": {"comparator": "<", "threshold": 0.05},
        "failure": {"comparator": ">=", "threshold": 0.05},
        "support": [
            {"observable": "seeds", "min_records": 8},
            {"observable": "inst", "min_records": 96},
        ],
        "stopping_rule": "fixed_campaign",
    }


def crossover_records(anchored: bool) -> dict[str, Any]:
    rng = random.Random(5)
    records = []
    for family in ("sparse", "toeplitz", "block"):
        for seed in range(8):
            for k in (5, 10, 20, 40):
                for solver in ("cg_hist", "celer"):
                    for repetition in range(3):
                        kp = k / 200
                        anchor = 0.08 if anchored else rng.uniform(0.03, 0.18)
                        wall = (
                            math.exp(4 * (kp - anchor)) if solver == "cg_hist" else 1.0
                        )
                        records.append(
                            {
                                "family": family,
                                "seed": 100 * seed,
                                "k": k,
                                "p": 200,
                                "solver": solver,
                                "repetition": repetition,
                                "wall_seconds": wall * math.exp(rng.gauss(0, 0.05)),
                            }
                        )
    return {"records": records}


def test_35ef9d24_is_expressible_and_capability_resolves(tmp_path: Path) -> None:
    analysis = checked(crossover())
    assert resolve_on_cg(analysis, tmp_path).executable


def test_35ef9d24_is_capability_limited_by_the_envelope_before_anything_is_frozen(
    tmp_path: Path,
) -> None:
    """96 instances where a campaign holds 6 x 4: refused, and not converted."""

    check = cg_planning(tmp_path).check(checked(crossover()))
    assert check.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED
    assert any(
        "instances (jointly) of solves: needs 96" in item for item in check.problems
    ), check.problems


def test_35ef9d24_reads_its_null_as_preregistered_and_its_falsifiers_objection_is_visible() -> (
    None
):
    analysis = checked(crossover("first"))
    anchored = engine.evaluate(analysis, {SOURCE: crossover_records(True)})
    assert anchored.statistic is not None
    for family in ("sparse", "toeplitz", "block"):
        assert anchored.statistics[f"p_{family}"] is not None
    assert anchored.records["seeds"]["included"] == 24
    # The `single` convention: most shuffles of four values give two or three
    # sign changes, the crossing is undefined, and so is the null.
    single = engine.evaluate(
        checked(crossover("single")), {SOURCE: crossover_records(True)}
    )
    assert single.conclusion is EmpiricalConclusion.INSUFFICIENT
    assert "not a null distribution" in " ".join(single.notes)


# ================================================================ d1b13797 --
def noise_floor(field: str) -> dict[str, Any]:
    return {
        "analysable": True,
        "estimand": "the seed-only spread of a solver-pair ranking statistic",
        "observables": [solves(["seed", "solver", "objective", field])],
        "tables": [
            {
                "name": "per_draw",
                "source": "solves",
                "by": ["seed"],
                "aggregates": [{"name": "gap", "op": "mean", "field": "objective"}],
            }
        ],
        "reductions": [
            {
                "name": "floor",
                "op": "quantile",
                "observable": "per_draw",
                "field": "gap",
                "q": 0.95,
            },
            {"name": "parent", "op": "max", "observable": "solves", "field": field},
            {"name": "excess", "op": "difference", "of": ["parent", "floor"]},
        ],
        "primary_statistic": "excess",
        "success": {"comparator": ">", "threshold": 0.0},
        "failure": {"comparator": "<=", "threshold": 0.0},
        "support": [{"observable": "per_draw", "min_records": 30}],
        "stopping_rule": "fixed_campaign",
    }


@pytest.mark.parametrize("field", ["toggle_shift", "parent_shift", "campaign_unit"])
def test_d1b13797_still_cannot_read_a_result_nothing_measured(
    field: str, tmp_path: Path
) -> None:
    """The parent ablation never ran; no table can make its result an observable."""

    resolution = resolve_on_cg(checked(noise_floor(field)), tmp_path)
    assert not resolution.executable
    assert any(field in item.detail for item in resolution.unmet), resolution.unmet


def test_d1b13797_needs_five_times_the_draws_a_campaign_holds(tmp_path: Path) -> None:
    payload = noise_floor("objective")
    analysis = checked(payload)
    assert resolve_on_cg(analysis, tmp_path).executable
    check = cg_planning(tmp_path).check(analysis)
    assert check.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED
    assert any(
        "distinct seed of solves: needs 30" in item for item in check.problems
    ), check.problems


# ================================================================ e3906d0e --
def active_set(terms: list[str] | None = None) -> dict[str, Any]:
    return {
        "analysable": True,
        "estimand": "the largest screened partial Spearman correlation of per-iteration cost with excess/p",
        "population": "not-ok cg_hist cells of the planted-truth families",
        "target_claim": "per-round cost carries active-set overshoot beyond k/p, iteration count and lambda",
        "observables": [
            solves(
                [
                    "family",
                    "wall_seconds",
                    "iterations",
                    "columns_generated",
                    "k",
                    "p",
                    "lambda_ratio",
                    "condition",
                    "ok",
                ],
                include=[{"field": "solver", "comparator": "==", "value": "cg_hist"}],
            )
        ],
        "tables": [
            {
                "name": "cells",
                "source": "solves",
                "compute": [
                    {"field": "per_iter", "expression": "wall_seconds / iterations"},
                    {"field": "kp", "expression": "k / p"},
                    {"field": "excess_p", "expression": "(columns_generated - k) / p"},
                    {
                        "field": "abs_excess_p",
                        "expression": "abs(columns_generated - k) / p",
                    },
                    {"field": "log_iter", "expression": "log(iterations)"},
                ],
                "include": [{"field": "ok", "comparator": "==", "value": False}],
            },
            {
                "name": "families",
                "source": "cells",
                "by": ["family"],
                "aggregates": [
                    {
                        "name": "rho",
                        "op": "partial_rank_correlation",
                        "field": "per_iter",
                        "other_field": "excess_p",
                        "terms": terms or ["kp", "log_iter", "lambda_ratio"],
                    },
                    {
                        "name": "diag",
                        "op": "rank_correlation",
                        "field": "excess_p",
                        "other_field": "log_iter",
                    },
                    {
                        "name": "q1",
                        "op": "quantile",
                        "field": "abs_excess_p",
                        "q": 0.25,
                    },
                    {
                        "name": "q3",
                        "op": "quantile",
                        "field": "abs_excess_p",
                        "q": 0.75,
                    },
                    {"name": "median_p", "op": "median", "field": "p"},
                ],
                "compute": [
                    {"field": "margin", "expression": "(q3 - q1) - 4 / median_p"},
                    {"field": "abs_diag", "expression": "abs(diag)"},
                ],
                "incomplete_records": "exclude",
            },
        ],
        "reductions": [
            {
                "name": "strongest",
                "op": "max",
                "observable": "families",
                "field": "rho",
                "where": [
                    {"field": "margin", "comparator": ">", "value": 0},
                    {"field": "abs_diag", "comparator": "<=", "value": 0.8},
                ],
            },
            {
                "name": "p_value",
                "op": "permutation_p",
                "of": ["strongest"],
                "observable": "cells",
                "field": "per_iter",
                "permutation": {
                    "by": ["family"],
                    "tail": "upper",
                    "resamples": 1000,
                    "seed": 3,
                },
            },
        ],
        "primary_statistic": "p_value",
        "success": {"comparator": "<", "threshold": 0.05},
        "failure": {"comparator": ">=", "threshold": 0.05},
        "support": [
            {"observable": "cells", "min_records": 40},
            {"observable": "families", "min_records": 1},
        ],
        "stopping_rule": "fixed_campaign",
    }


def cg_hist_cells(
    effect: float, *, condition_everywhere: bool = False
) -> dict[str, Any]:
    rng = random.Random(8)
    records = []
    for family in ("sparse", "illcond", "toeplitz", "block"):
        for _ in range(40):
            k, p = rng.choice((5, 10, 20)), rng.choice((100, 200))
            iterations = rng.randint(5, 60)
            excess = rng.randint(0, 30)
            per_round = 0.01 + effect * excess + 0.02 * k / p
            records.append(
                {
                    "family": family,
                    "solver": "cg_hist",
                    "ok": False,
                    "wall_seconds": iterations
                    * per_round
                    * math.exp(rng.gauss(0, 0.1)),
                    "iterations": iterations,
                    "columns_generated": k + excess,
                    "k": k,
                    "p": p,
                    "lambda_ratio": rng.choice((0.3, 0.5, 0.7)),
                    "condition": rng.choice((10.0, 100.0))
                    if family == "illcond" or condition_everywhere
                    else 1.0,
                }
            )
    return {"records": records}


def test_e3906d0e_is_expressible_and_capability_resolves(tmp_path: Path) -> None:
    analysis = checked(active_set())
    assert resolve_on_cg(analysis, tmp_path).executable
    assert (
        cg_planning(tmp_path).check(analysis).verdict
        is ExecutionShapeVerdict.VALID_CAMPAIGN
    )


def test_e3906d0e_finds_a_planted_effect_and_not_an_absent_one() -> None:
    analysis = checked(active_set())
    planted = engine.evaluate(analysis, {SOURCE: cg_hist_cells(0.001)})
    assert planted.conclusion is EmpiricalConclusion.SUPPORTS, planted.summary
    absent = engine.evaluate(analysis, {SOURCE: cg_hist_cells(0.0)})
    assert absent.conclusion is EmpiricalConclusion.CONTRADICTS, absent.summary


def test_e3906d0e_full_covariate_model_is_unidentified_where_condition_is_constant() -> (
    None
):
    """``condition`` is 1.0 for every instance outside ``illcond``."""

    analysis = checked(active_set(["kp", "log_iter", "lambda_ratio", "condition"]))
    result = engine.evaluate(analysis, {SOURCE: cg_hist_cells(0.001)})
    assert result.records["families"]["incomplete"] == 3
    assert result.records["families"]["included"] == 1


def test_e3906d0e_selection_rule_is_not_in_the_condition_language() -> None:
    """A disjunction with a per-family data-dependent threshold has no form here."""

    analysis = AnalysisSpec.model_validate(copy.deepcopy(active_set()))
    table = analysis.tables[0].model_dump(mode="json")
    assert set(table["include"][0]) == {"field", "comparator", "value"}
    with pytest.raises(ValidationError):
        AnalysisSpec.model_validate(
            {
                **active_set(),
                "tables": [
                    {
                        **active_set()["tables"][0],
                        "include": [
                            {
                                "any_of": [
                                    {"field": "ok", "comparator": "==", "value": False}
                                ]
                            }
                        ],
                    }
                ],
            }
        )
