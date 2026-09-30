"""Mutation testing for the final v1 sprint: allocation lanes, campaigns, feasibility.

Each mutant removes or bypasses one enforcement point of the three guarantees
the first final qualification's failure asked for, and names the tests that
must notice:

- ``ALLOC-*`` -- lifecycle-aware allocation (``portfolio.allocation``,
  ``portfolio.tick``): the advancement slots, the spend reserve, its
  eligibility, the lanes, and the tick handing the plan the reserve;
- ``CAMP-*`` -- multi-execution campaigns (``portfolio.campaign``,
  ``portfolio.empirical``, ``portfolio.sciencechain``, ``sql/0048``): no
  duplicate or unattested units, the human-set bounds, the whole execution
  authority before any unit runs, no repeated observation when combined,
  the unit-by-unit replication pairing, the gate's re-verification and the
  database's own refusals;
- ``FEAS-*`` -- the feasibility signal (``portfolio.feasibility``): the
  catalogue decides, never the need's words, and only advancement is ordered
  by it.

The runner is the integrity harness's: it applies one mutant, runs the tests
that must notice, restores the file whatever happens, and reports KILLED,
SURVIVED or INVALID. Not collected by pytest and never run by the suite.

    uv run python -m tests.campaign_allocation_mutations           # every mutant
    uv run python -m tests.campaign_allocation_mutations CAMP-3    # some of them
    uv run python -m tests.campaign_allocation_mutations --list

``tests/test_science_campaigns.py::test_every_campaign_and_allocation_mutant_still_applies``
checks that every mutant still applies and names tests that exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

ALLOCATION = "src/research_os/portfolio/allocation.py"
TICK = "src/research_os/portfolio/tick.py"
CAMPAIGN = "src/research_os/portfolio/campaign.py"
EMPIRICAL = "src/research_os/portfolio/empirical.py"
CHAIN = "src/research_os/portfolio/sciencechain.py"
SQL = "src/research_os/runtime/sql/0048_science_campaigns.sql"
FEASIBILITY = "src/research_os/portfolio/feasibility.py"

L = "tests/test_portfolio_allocation_lanes.py"
C = "tests/test_science_campaigns.py"
F = "tests/test_portfolio_feasibility.py"

SLOTS = f"{L}::test_attractive_shallow_work_cannot_take_the_advancement_slots"
SPEND = f"{L}::test_attractive_shallow_work_cannot_spend_the_advancement_reserve"
STRESS = f"{L}::test_a_follow_up_explosion_cannot_consume_the_advancement_authority"
IN_FLIGHT = f"{L}::test_the_reserve_is_held_while_a_deep_stage_runs"
BORROWED = f"{L}::test_when_every_deep_idea_is_blocked_capacity_returns_to_exploration"
LANES = f"{L}::test_every_unit_of_work_has_one_lane_by_its_kind_stage_and_status"
LEDGER = f"{L}::test_the_tick_holds_the_reserve_against_the_real_ledger"

SAME = f"{C}::test_units_that_are_the_same_execution_are_refused"
UNATTESTED = (
    f"{C}::test_units_that_differ_only_in_what_the_capability_does_not_attest"
    "_are_limited"
)
REPEATED = f"{C}::test_an_observation_two_units_both_produced_is_refused_when_combined"
SINGLE_RULE = f"{C}::test_a_campaign_is_not_run_under_a_single_execution_contract"
BOUNDS = (
    f"{C}::test_a_campaign_beyond_the_human_set_bounds_is_budget_limited_before_it_runs"
)
AUTHORITY = f"{C}::test_an_execution_budget_that_cannot_cover_every_unit_starts_none"
PAIRED = f"{C}::test_a_campaign_replication_is_established_unit_by_unit"
REVERIFIED = f"{C}::test_the_gate_re_verifies_every_unit_result_against_what_is_stored"
PARTIAL = f"{C}::test_the_database_refuses_a_campaign_reading_that_omits_a_unit"
UNFROZEN = (
    f"{C}::test_the_database_refuses_a_unit_receipt_of_a_specification_not_frozen"
)
LONGER = f"{C}::test_a_campaign_longer_than_its_capability_allows_is_limited"

FLOOR = f"{L}::test_the_pool_floor_explorer_cannot_take_the_only_advancement_slot"
BINDING = f"{L}::test_the_share_is_of_the_ceiling_that_binds"
SHIFTED = f"{C}::test_a_replication_that_shifts_the_primary_seeds_is_refused"
RESHIFTED = (
    f"{C}::test_a_replication_unit_that_is_another_primary_unit_with_new_resources"
    "_is_refused"
)
TOP_LEVEL = f"{C}::test_records_at_the_top_level_are_combined_there"
ONE_OF_A_CAMPAIGN = f"{C}::test_a_campaign_contract_refuses_a_single_execution"
RESUMED = (
    f"{C}::test_a_unit_whose_result_was_not_stored_before_a_crash_is_stored_on_resume"
)
RETRIED = f"{C}::test_a_retried_unit_never_recovers_an_earlier_attempts_execution"
UNRECORDED = f"{C}::test_a_job_found_after_a_crash_without_its_receipt_is_not_read"

TIE = f"{F}::test_scarce_advancement_goes_to_the_equally_strong_executable_direction"
REPEAT = f"{F}::test_a_limited_direction_does_not_repeatedly_take_deep_execution"
NAMED = f"{F}::test_a_model_cannot_make_a_field_exist_by_naming_it"
MATCH = f"{F}::test_the_signal_is_the_committed_catalogue_against_the_structured_need"
EXPLORE = f"{F}::test_exploration_of_a_limited_idea_is_untouched"
LATER = f"{F}::test_a_later_capability_that_can_hold_the_draws_is_found"
STALE = f"{F}::test_a_contract_blocked_under_another_command_set_is_not_an_answer"

MUTANTS: tuple[Mutant, ...] = (
    # --------------------------------------------------------- allocation --
    Mutant(
        "ALLOC-1",
        "LANES",
        "run1",
        ALLOCATION,
        """        return not (
            lane is Lane.EXPLORATION
            and protected is not None""",
        """        return not (
            False
            and lane is Lane.EXPLORATION
            and protected is not None""",
        (SPEND, STRESS, IN_FLIGHT),
        "exploration may be sold into the advancement reserve",
    ),
    Mutant(
        "ALLOC-2",
        "LANES",
        "run1",
        ALLOCATION,
        """        math.ceil(fraction * remaining)
        if fraction > 0""",
        """        0 * math.ceil(fraction * remaining)
        if fraction > 0""",
        (SLOTS,),
        "no idea slot is reserved for advancement",
    ),
    Mutant(
        "ALLOC-3",
        "LANES",
        "run1",
        ALLOCATION,
        """    if advancement is not None and advancement.in_flight > 0:
        return True
""",
        "",
        (IN_FLIGHT, STRESS),
        "advancement work already running does not hold the reserve",
    ),
    Mutant(
        "ALLOC-4",
        "LANES",
        "run1",
        ALLOCATION,
        """    if which in ADMISSION_STAGES and status in ADMITTED:
        return Lane.ADVANCEMENT
""",
        "",
        (LANES, SLOTS),
        "an admitted idea's sharpening and re-run ladder count as exploration",
    ),
    Mutant(
        "ALLOC-5",
        "LANES",
        "run1",
        TICK,
        """        advancement=advancement,
    )
    report.allocations = allocations""",
        """        advancement=None,
    )
    report.allocations = allocations""",
        (LEDGER,),
        "the tick plans without the reserve the ledger implies",
    ),
    Mutant(
        "ALLOC-6",
        "LANES",
        "run1",
        ALLOCATION,
        """    fraction = Decimal(str(config.bounds.advancement_reserve_fraction))
    if fraction <= 0:
        return False
    if advancement is not None""",
        """    fraction = Decimal(str(config.bounds.advancement_reserve_fraction))
    if fraction <= 0:
        return False
    return True
    if advancement is not None""",
        (BORROWED,),
        "the reserve stays idle when no advancement purchase is possible",
    ),
    # ---------------------------------------------------------- campaigns --
    Mutant(
        "CAMP-1",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """        if not differs:""",
        """        if False:""",
        (SAME,),
        "two identical units are accepted as two observations",
    ),
    Mutant(
        "CAMP-2",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """        elif not set(differs) & varies:""",
        """        elif False:""",
        (UNATTESTED,),
        "units differing only in what the capability does not attest are accepted",
    ),
    Mutant(
        "CAMP-3",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """                            key in seen
                            and seen[key] != unit""",
        """                            False
                            and seen[key] != unit""",
        (REPEATED,),
        "an observation two units both produced is counted twice",
    ),
    Mutant(
        "CAMP-4",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """    if analysis.stopping_rule != CAMPAIGN_STOPPING_RULE:""",
        """    if False:""",
        (SINGLE_RULE,),
        "a campaign runs under a contract that fixed a single execution",
    ),
    Mutant(
        "CAMP-5",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """    if seconds > max_seconds:""",
        """    if False:""",
        (BOUNDS,),
        "a campaign beyond the human-set total time bound is frozen",
    ),
    Mutant(
        "CAMP-6",
        "CAMPAIGN",
        "run1",
        EMPIRICAL,
        """                amount=len(remaining),""",
        """                amount=1,""",
        (AUTHORITY,),
        "a campaign starts with authority for one execution rather than all",
    ),
    Mutant(
        "CAMP-7",
        "CAMPAIGN",
        "run1",
        EMPIRICAL,
        """        context.portfolio.get_receipt(parents[index]) if index in parents else None""",
        """        context.portfolio.get_receipt(parents[0]) if index in parents else None""",
        (PAIRED,),
        "a replication unit is bound to a primary unit other than its own",
    ),
    Mutant(
        "CAMP-8",
        "CAMPAIGN",
        "run1",
        CHAIN,
        """                if chain.is_campaign:
                    problems.extend(
                        campaign_reading_problems(
                            store, artifacts, chain, anchor=receipt, outcome=outcome
                        )
                    )""",
        """                if False:
                    problems.extend(
                        campaign_reading_problems(
                            store, artifacts, chain, anchor=receipt, outcome=outcome
                        )
                    )""",
        (REVERIFIED,),
        "the gate reads a campaign's outcome without re-verifying its units",
    ),
    Mutant(
        "CAMP-9",
        "CAMPAIGN",
        "run1",
        SQL,
        """        if new.unit_count is distinct from frozen or named <> frozen""",
        """        if false""",
        (PARTIAL,),
        "the database accepts a campaign reading that omits a unit",
    ),
    Mutant(
        "CAMP-10",
        "CAMPAIGN",
        "run1",
        SQL,
        """               and u.spec_digest = new.spec_digest) then""",
        """               ) then""",
        (UNFROZEN,),
        "the database accepts a unit receipt of a specification not frozen",
    ),
    Mutant(
        "CAMP-11",
        "CAMPAIGN",
        "run1",
        CAMPAIGN,
        """    if count > support.max_units:""",
        """    if False:""",
        (LONGER,),
        "a campaign longer than its capability allows is accepted",
    ),
    # ------------------------------------- found by the pre-rerun review --
    Mutant(
        "ALLOC-7",
        "LANES",
        "review",
        ALLOCATION,
        """        and remaining > share""",
        """        and remaining""",
        (FLOOR,),
        "the pool-floor explorer takes the only advancement slot",
    ),
    Mutant(
        "ALLOC-8",
        "LANES",
        "review",
        TICK,
        """fraction * min(limits)""",
        """fraction * max(limits)""",
        (BINDING,),
        "the reserve is a share of a ceiling looser than the one that binds",
    ),
    Mutant(
        "CAMP-12",
        "CAMPAIGN",
        "review",
        CAMPAIGN,
        """        position = measured.get(mine.variation_digest)""",
        """        position = (
            mine.index if measured.get(mine.variation_digest) == mine.index else None
        )""",
        (SHIFTED,),
        "a replication unit is compared only with the primary unit of its index",
    ),
    Mutant(
        "CAMP-13",
        "CAMPAIGN",
        "review",
        EMPIRICAL,
        """                position = measured.get(scientific_variation(unit.spec))""",
        """                position = (
                    unit.index
                    if measured.get(scientific_variation(unit.spec)) == unit.index
                    else None
                )""",
        (RESHIFTED,),
        "a replication unit may be another primary unit under new resources",
    ),
    Mutant(
        "CAMP-14",
        "CAMPAIGN",
        "review",
        CAMPAIGN,
        """        return value if document is None else None""",
        """        return {} if document is None else None""",
        (TOP_LEVEL,),
        "records at the top level are dropped from the combination",
    ),
    Mutant(
        "CAMP-15",
        "CAMPAIGN",
        "review",
        EMPIRICAL,
        """    if analysis.stopping_rule == CAMPAIGN_STOPPING_RULE:""",
        """    if False:""",
        (ONE_OF_A_CAMPAIGN,),
        "a campaign contract is run and read as one execution",
    ),
    Mutant(
        "CAMP-16",
        "CAMPAIGN",
        "review",
        EMPIRICAL,
        """        if context.portfolio.unit_result(receipt.receipt_id) is None
    ]""",
        """        if False
    ]""",
        (RESUMED,),
        "a unit whose result was never stored counts as done on resume",
    ),
    Mutant(
        "CAMP-17",
        "CAMPAIGN",
        "review",
        EMPIRICAL,
        '''                "and j.submitted_at >= %s "
                "and not exists (select 1 from execution_receipts r "
                "where r.job_id = j.job_id and (r.experiment_id <> %s "
                "or r.unit_index is distinct from %s "
                "or r.unit_attempt is distinct from %s)) "''',
        '''                "and %s::timestamptz is not null "
                "and %s::text is not null and %s::int is not null "
                "and %s::int is not null "''',
        (RETRIED,),
        "the unit reconciler recovers an earlier attempt's execution",
    ),
    Mutant(
        "CAMP-18",
        "CAMPAIGN",
        "review",
        EMPIRICAL,
        """    if result.get("recovered"):""",
        """    if False:""",
        (UNRECORDED,),
        "a job recovered without its receipt is read as the unit's execution",
    ),
    Mutant(
        "FEAS-5",
        "FEASIBILITY",
        "review",
        FEASIBILITY,
        """    for capability in complete:
        if capability.campaign is not None:""",
        """    for capability in complete[:1]:
        if capability.campaign is not None:""",
        (LATER,),
        "a later campaign-capable capability is never considered",
    ),
    Mutant(
        "FEAS-6",
        "FEASIBILITY",
        "review",
        "src/research_os/portfolio/store.py",
        """                   and (%s::text is null or command_set_digest = %s::text)""",
        """                   and (%s::text is null or %s::text is not null)""",
        (STALE,),
        "a contract blocked under an earlier command set still penalises its idea",
    ),
    # -------------------------------------------------------- feasibility --
    Mutant(
        "FEAS-1",
        "FEASIBILITY",
        "run1",
        ALLOCATION,
        """        score -= weights.capability_limited_penalty""",
        """        score -= 0""",
        (TIE, REPEAT),
        "a capability-limited direction takes scarce advancement from an equal one",
    ),
    Mutant(
        "FEAS-2",
        "FEASIBILITY",
        "run1",
        FEASIBILITY,
        """        missing = [item for item in wanted if item not in _declared_names(capability)]""",
        """        missing = []""",
        (NAMED, MATCH),
        "a field the need names is taken to exist without the catalogue",
    ),
    Mutant(
        "FEAS-3",
        "FEASIBILITY",
        "run1",
        FEASIBILITY,
        """    if data == "existing_records":""",
        """    if False:""",
        (MATCH,),
        "a need for records that already exist is read as executable",
    ),
    Mutant(
        "FEAS-4",
        "FEASIBILITY",
        "run1",
        ALLOCATION,
        """        and candidate.lane is Lane.ADVANCEMENT
    ):
        score -= weights.capability_limited_penalty""",
        """    ):
        score -= weights.capability_limited_penalty""",
        (EXPLORE,),
        "exploration of a capability-limited idea is penalised too",
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
            print(f"{mutant.id:10} {mutant.invariant:11} {mutant.what}")
        return 0
    results = []
    for mutant in chosen:
        outcome = run(mutant)
        results.append(outcome)
        print(
            f"{outcome['id']:10} {outcome['result']:10} {outcome['detail']}", flush=True
        )
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(item["result"] == "KILLED" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
