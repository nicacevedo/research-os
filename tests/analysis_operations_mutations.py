"""Mutation testing for the second analysis language (docs/SCIENCE_EXECUTION.md §4a).

Each mutant removes or bends one enforcement point of the language Cycle 002
of the live column-generation run asked for, and names the tests that must
notice:

- ``AOV2-1..4`` -- the arithmetic: a zero denominator and the logarithm of
  a non-positive number are undefined, never numbers; the expression bounds
  hold; a malformed expression never parses;
- ``AOV2-5..8`` -- the crossing: interpolated between adjacent points, one
  ``y`` per ``x``, the frozen ``single`` convention, and a touch is no
  crossing;
- ``AOV2-9..14`` -- the permutation null: strata respected, the frozen
  seed used, undefined draws counted as extreme, a mostly undefined null
  refused, the work bound held, and no bootstrap of a p-value;
- ``AOV2-15..17`` -- the correlations: average ranks for ties, both fields
  residualised, ranks used when asked;
- ``AOV2-18..21`` -- tables: an undefined derived value obeys the frozen
  policy, group keys are typed, what a table reads is a capability
  requirement, and a second-language analysis is checked again before it is
  read;
- ``AOV2-22..24`` -- the effective population: a support rule no data can
  meet is refused, a table's support reaches the envelope, and a bootstrap
  derives its tables again;
- ``AOV2-25..27`` -- history: a first-language analysis dumps what it always
  did, a second-language one is hashed under its own version, and the
  analysis author's prompt was retired so the blocked contracts are asked
  again.

The runner is the integrity harness's: it applies one mutant, runs the tests
that must notice, restores the file whatever happens, and reports KILLED,
SURVIVED or INVALID. Not collected by pytest and never run by the suite.

    uv run python -m tests.analysis_operations_mutations           # every mutant
    uv run python -m tests.analysis_operations_mutations AOV2-3    # some of them
    uv run python -m tests.analysis_operations_mutations --list

``tests/test_analysis_operations_v2.py::test_every_analysis_operations_mutant_still_applies``
checks that every mutant still applies and names tests that exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

EXPRESSIONS = "src/research_os/portfolio/expressions.py"
ANALYSIS = "src/research_os/portfolio/analysis.py"
CONTRACTS = "src/research_os/portfolio/contracts.py"
SCICONTRACT = "src/research_os/portfolio/scicontract.py"
SCIENCECHAIN = "src/research_os/portfolio/sciencechain.py"
SHAPE = "src/research_os/portfolio/shape.py"
PROMPTS = "src/research_os/portfolio/prompts.py"

T = "tests/test_analysis_operations_v2.py"
C = "tests/test_analysis_operations_v2_cycle002.py"
R = "tests/test_analysis_operations_v2_route.py"

UNDEFINED = f"{T}::test_an_undefined_expression_is_none_never_a_number"
BOUNDED = f"{T}::test_an_expression_is_bounded_in_size_nodes_depth_and_names"
MALFORMED = f"{T}::test_a_malformed_expression_fails_closed_before_anything_is_frozen"
INTERPOLATED = (
    f"{T}::test_a_crossing_is_interpolated_linearly_between_the_adjacent_points"
)
WHICH = f"{T}::test_which_crossing_is_the_one_frozen_in_advance"
CROSS_UNDEFINED = f"{T}::test_an_undefined_crossing_is_insufficient_never_an_endpoint"
TOUCH = (
    f"{T}::test_a_point_on_the_level_is_where_it_crosses_and_a_touch_is_not_a_crossing"
)
STRATA = f"{T}::test_a_permutation_never_moves_a_value_out_of_its_stratum"
SEEDED = f"{T}::test_a_permutation_null_is_a_deterministic_function_of_its_frozen_seed"
EXTREME = f"{T}::test_a_permutation_where_the_statistic_is_undefined_counts_as_extreme"
MOSTLY = f"{T}::test_a_null_mostly_undefined_is_not_a_distribution_and_gives_no_p_value"
WORK = (
    f"{T}::test_a_permutation_past_its_work_bound_is_insufficient_without_being_drawn"
)
INCOHERENT_P = f"{T}::test_an_incoherent_permutation_is_refused_before_it_is_frozen"
SPEARMAN = f"{T}::test_a_rank_correlation_is_spearman_with_average_ranks_for_ties"
PARTIAL = f"{T}::test_a_partial_correlation_is_the_textbook_first_order_partial"
DERIVED = f"{T}::test_an_undefined_derived_value_is_insufficient_or_excluded_as_fixed_in_advance"
TYPED = f"{T}::test_group_keys_are_typed_so_a_boolean_and_a_number_are_two_groups"
HONEST = (
    f"{T}::test_what_a_table_reads_is_a_capability_requirement_the_capability_must_meet"
)
NO_REQUIREMENT = (
    f"{T}::test_a_derived_field_is_computed_from_declared_fields_and_is_no_requirement"
)
BYPASS = f"{T}::test_a_second_language_analysis_built_without_its_checks_is_not_read"
IMPOSSIBLE = f"{T}::test_a_support_rule_no_data_can_meet_is_refused_before_it_is_frozen"
PROVED = f"{T}::test_only_a_proved_impossibility_is_refused"
EFFECTIVE = (
    f"{T}::test_a_tables_support_is_the_statistics_effective_population_before_freezing"
)
EQUIVALENCE = f"{T}::test_equivalence_is_the_whole_interval_below_the_margin"
HISTORY = f"{T}::test_every_analysis_frozen_in_the_first_language_rebuilds_to_its_digest_and_bytes"
NO_NEW_KEYS = f"{T}::test_a_first_language_analysis_dumps_no_key_the_second_added"
OWN_VERSION = f"{T}::test_a_second_language_analysis_is_hashed_under_its_own_version"
UNIT = f"{T}::test_a_unit_identity_no_capability_declares_cannot_be_grouped_by"
LIMITED_35 = f"{C}::test_35ef9d24_is_capability_limited_by_the_envelope_before_anything_is_frozen"
LIMITED_D1 = f"{C}::test_d1b13797_needs_five_times_the_draws_a_campaign_holds"
UNREAD_D1 = f"{C}::test_d1b13797_still_cannot_read_a_result_nothing_measured"
FROZEN_READ = (
    f"{R}::test_a_second_language_analysis_is_frozen_run_as_a_campaign_and_read"
)
REOPENED = (
    f"{R}::test_a_contract_cycle_002_left_blocked_is_analysed_again_in_the_new_language"
)

MUTANTS: tuple[Mutant, ...] = (
    # ------------------------------------------------------- arithmetic --
    Mutant(
        "AOV2-1",
        "SCI-04",
        "c002",
        EXPRESSIONS,
        """        if right == 0.0:
            return None
        return _finite(left / right)""",
        """        if right == 0.0:
            return _finite(left)
        return _finite(left / right)""",
        (UNDEFINED,),
        "a zero denominator gives the numerator instead of an undefined value",
    ),
    Mutant(
        "AOV2-2",
        "SCI-04",
        "c002",
        EXPRESSIONS,
        """            return math.log(numbers[0]) if numbers[0] > 0.0 else None""",
        """            return math.log(abs(numbers[0]) or 1.0)""",
        (UNDEFINED,),
        "the logarithm of a non-positive number is a number",
    ),
    Mutant(
        "AOV2-3",
        "SCI-04",
        "c002",
        EXPRESSIONS,
        """        if depth > MAX_DEPTH:
            raise self.fail(f"it nests deeper than {MAX_DEPTH} levels")
        return value""",
        """        return value""",
        (BOUNDED,),
        "the nesting bound on a node is dropped",
    ),
    Mutant(
        "AOV2-4",
        "SCI-04",
        "c002",
        EXPRESSIONS,
        """    if parser.peek() is not None:
        raise parser.fail(""",
        """    if False:
        raise parser.fail(""",
        (MALFORMED,),
        "trailing text after a complete expression is ignored",
    ),
    # --------------------------------------------------------- crossing --
    Mutant(
        "AOV2-5",
        "SCI-04",
        "c002",
        ANALYSIS,
        """            location = x0 + (rule.level - y0) * (x1 - x0) / (y1 - y0)""",
        """            location = x0""",
        (INTERPOLATED,),
        "the crossing is the left endpoint rather than the interpolated point",
    ),
    Mutant(
        "AOV2-6",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        if before == after:
            raise _Undefined(""",
        """        if False:
            raise _Undefined(""",
        (CROSS_UNDEFINED,),
        "several records at one abscissa are read as a curve",
    ),
    Mutant(
        "AOV2-7",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    if rule.pick == "single" and len(found) != 1:""",
        """    if False:""",
        (WHICH,),
        "the frozen single-crossing convention picks the first of several",
    ),
    Mutant(
        "AOV2-8",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        if (offsets[low] < 0.0) == (offsets[high] < 0.0):
            continue""",
        """        if False:
            continue""",
        (TOUCH,),
        "touching the level without changing sign is read as a crossing",
    ),
    # ------------------------------------------------------ permutation --
    Mutant(
        "AOV2-9",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    strata = _partition(rows, permutation.by)""",
        """    strata = _partition(rows, ())""",
        (STRATA,),
        "the frozen strata are ignored and values shuffled across all records",
    ),
    Mutant(
        "AOV2-10",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    generator = random.Random(permutation.seed)""",
        """    generator = random.Random()""",
        (SEEDED,),
        "the permutation null is drawn from an unrecorded seed",
    ),
    Mutant(
        "AOV2-11",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    upper = (1 + at_least + undefined) / (draws + 1)""",
        """    upper = (1 + at_least) / (draws + 1)""",
        (EXTREME,),
        "draws where the statistic is undefined are dropped, shrinking the p-value",
    ),
    Mutant(
        "AOV2-12",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    if undefined > (1.0 - MIN_FINITE_RESAMPLES) * draws:""",
        """    if False:""",
        (MOSTLY,),
        "a null that is mostly undefined still yields a p-value",
    ),
    Mutant(
        "AOV2-13",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    if work > MAX_RESAMPLING_WORK:
        raise _Undefined(""",
        """    if False:
        raise _Undefined(""",
        (WORK,),
        "the permutation null's work bound is dropped",
    ),
    Mutant(
        "AOV2-14",
        "SCI-04",
        "c002",
        CONTRACTS,
        """            if permuted:
                raise ContractError(""",
        """            if False:
                raise ContractError(""",
        (INCOHERENT_P,),
        "a bootstrap of a permutation p-value is frozen",
    ),
    # ----------------------------------------------------- correlations --
    Mutant(
        "AOV2-15",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        shared = (start + end) / 2.0 + 1.0""",
        """        shared = start + 1.0""",
        (SPEARMAN,),
        "tied values take the lowest rank rather than the average",
    ),
    Mutant(
        "AOV2-16",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        left = _residuals(values, columns, reduction)""",
        """        left = (
            list(values)
            if name == reduction.other_field
            else _residuals(values, columns, reduction)
        )""",
        (PARTIAL,),
        "only one field is residualised: a semi-partial read as a partial correlation",
    ),
    Mutant(
        "AOV2-17",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        return _partial(rows, reduction, ranked=op == "partial_rank_correlation")""",
        """        return _partial(rows, reduction, ranked=False)""",
        (PARTIAL,),
        "the partial rank correlation is computed on values, not ranks",
    ),
    # ----------------------------------------------------------- tables --
    Mutant(
        "AOV2-18",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        if counts["incomplete"] and table.incomplete_records == "insufficient":
            return _insufficient(""",
        """        if False:
            return _insufficient(""",
        (DERIVED,),
        "an undefined derived value is dropped although the contract said insufficient",
    ),
    Mutant(
        "AOV2-19",
        "SCI-04",
        "c002",
        ANALYSIS,
        """    if isinstance(value, bool):
        return (0, int(value))""",
        """    if False:
        return (0, int(value))""",
        (TYPED,),
        "a boolean key and a number key are merged into one group",
    ),
    Mutant(
        "AOV2-20",
        "SCI-01",
        "c002",
        SCIENCECHAIN,
        """        fields.extend(fields_v2)
        numeric.extend(numeric_v2)""",
        """        fields.extend(())
        numeric.extend(())""",
        (HONEST, NO_REQUIREMENT, UNIT, UNREAD_D1),
        "what a table reads is not a capability requirement: a table invents an observable",
    ),
    Mutant(
        "AOV2-21",
        "SCI-01",
        "c002",
        ANALYSIS,
        """    if spec.language() == 2:
        # Checked when it was frozen, and checked again here""",
        """    if False:
        # Checked when it was frozen, and checked again here""",
        (BYPASS,),
        "a second-language analysis built without its checks is read",
    ),
    # ----------------------------------------- the effective population --
    Mutant(
        "AOV2-22",
        "SCI-04",
        "c001",
        CONTRACTS,
        """        self._check_support_can_be_met(observables=observables, defined=defined)
""",
        """
""",
        (IMPOSSIBLE, PROVED),
        "a support rule no data can meet is frozen, as cycle 001's was",
    ),
    Mutant(
        "AOV2-23",
        "SCI-04",
        "c002",
        SHAPE,
        """    for implied in analysis.implied_support():""",
        """    for implied in ():""",
        (EFFECTIVE, LIMITED_35, LIMITED_D1, FROZEN_READ),
        "a table's support never reaches the pre-freeze envelope check",
    ),
    Mutant(
        "AOV2-24",
        "SCI-04",
        "c002",
        ANALYSIS,
        """        if spec.tables:
            sample = _rebuilt(spec, sample, spec.tables)""",
        """        if False:
            sample = _rebuilt(spec, sample, spec.tables)""",
        (EQUIVALENCE,),
        "a bootstrap resamples records but reads the tables of the observed ones",
    ),
    # ---------------------------------------------------------- history --
    Mutant(
        "AOV2-25",
        "SCI-02",
        "c002",
        CONTRACTS,
        """            if key in dumped and not dumped[key]:
                del dumped[key]""",
        """            if False:
                del dumped[key]""",
        (HISTORY, NO_NEW_KEYS),
        "a first-language reduction dumps the second language's empty keys",
    ),
    Mutant(
        "AOV2-26",
        "SCI-02",
        "c002",
        SCICONTRACT,
        """        ANALYSIS_DIGEST_VERSION_2 if spec.language() == 2 else ANALYSIS_DIGEST_VERSION""",
        """        ANALYSIS_DIGEST_VERSION""",
        (OWN_VERSION, FROZEN_READ),
        "a second-language analysis is hashed as a first-language one",
    ),
    Mutant(
        "AOV2-27",
        "SCI-02",
        "c002",
        PROMPTS,
        """    version=5,""",
        """    version=4,""",
        (REOPENED, FROZEN_READ),
        "the analysis author's prompt is not retired, so a blocked @4 contract is never asked again",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    chosen = [m for m in MUTANTS if not args.ids or m.id in args.ids]
    if args.list:
        for mutant in chosen:
            print(f"{mutant.id:8} {mutant.what}")
        return 0
    results = []
    for mutant in chosen:
        outcome = run(mutant)
        results.append(outcome)
        print(
            f"{outcome['id']:8} {outcome['result']:10} {outcome['detail']}", flush=True
        )
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(item["result"] == "KILLED" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
