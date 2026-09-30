"""Mutation testing for campaign unit variation (docs/SCIENCE_EXECUTION.md §3a).

Each mutant weakens one enforcement point of one rule -- every pair of a
campaign's units differs, and in nothing its frozen analysis does not allow,
``actual <= allowed`` and ``actual != {}`` -- and names the tests that must
notice:

- ``VAR-1..3`` -- the three the independent review's finding asks for by
  name: the subset rule turned into the defective "intersects" rule, one
  forbidden difference ignored, a composed document's nested content not
  inspected;
- ``VAR-4..6`` and ``VAR-13`` -- each of the three layers weakened alone:
  the designer-side check filtering what it compares (the base candidate's
  defect) or intersecting rather than containing, the compiler not applying
  the rule, the chain verification not re-deriving it;
- ``VAR-7..8`` -- the rule bypassed for a replication campaign, at the
  designer-side check and at the chain verification;
- ``VAR-9..12`` -- what "actual" and "allowed" are: the specification is
  trusted over the recorded parameters, a difference no declared input
  accounts for is never allowed, allowed is not required, and the
  capability's result location is not science.

The runner is the integrity harness's: it applies one mutant, runs the tests
that must notice, restores the file whatever happens, and reports KILLED,
SURVIVED or INVALID. Not collected by pytest and never run by the suite.

    uv run python -m tests.campaign_variation_mutations           # every mutant
    uv run python -m tests.campaign_variation_mutations VAR-3     # some of them
    uv run python -m tests.campaign_variation_mutations --list

``tests/test_campaign_variation.py::test_every_campaign_variation_mutant_still_applies``
checks that every mutant still applies and names tests that exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

CAMPAIGN = "src/research_os/portfolio/campaign.py"
SHAPE = "src/research_os/portfolio/shape.py"
EMPIRICAL = "src/research_os/portfolio/empirical.py"
CHAIN = "src/research_os/portfolio/sciencechain.py"

U = "tests/test_campaign_variation.py"
X = "tests/test_campaign_variation_reproduction.py"

B_UNIT = f"{U}::test_b_units_differing_only_in_a_forbidden_slope_are_refused"
C_UNIT = f"{U}::test_c_an_allowed_seed_never_masks_a_forbidden_slope"
D_UNIT = f"{U}::test_d_allowed_is_not_required"
F_UNIT = f"{U}::test_f_units_that_do_not_differ_are_refused"
G_UNIT = f"{U}::test_g_operational_metadata_is_not_a_scientific_difference"
H_UNIT = f"{U}::test_h_a_forbidden_value_nested_in_the_composed_plan_is_refused"
I_UNIT = f"{U}::test_i_one_forbidden_unit_refuses_the_whole_campaign"
CLAIMED = f"{U}::test_what_the_units_say_about_themselves_is_not_what_they_differ_in"
UNATTRIBUTED = f"{U}::test_a_difference_no_declared_input_accounts_for_is_never_allowed"
AGREE = (
    f"{U}::test_the_configuration_is_the_same_from_the_units_and_from_the_frozen_plan"
)

REPRODUCTION = f"{X}::test_the_reviewers_attack_is_refused_before_anything_runs"
COMPILER_ALONE = (
    f"{X}::test_without_the_designer_side_check_the_compiler_refuses_it_alone"
)
CHAIN_ALONE = (
    f"{X}::test_a_campaign_frozen_under_the_base_rules_never_runs_and_is_never_read"
)
RECOVERED = f"{X}::test_a_recovered_campaign_is_held_to_the_rule"
REPLICATION = f"{X}::test_a_replication_campaign_with_a_forbidden_variation_is_refused"
REPLICATION_CHAIN = (
    f"{X}::test_a_replication_campaign_frozen_under_the_base_rules_never_runs"
)

MUTANTS: tuple[Mutant, ...] = (
    # ------------------------------------------------- the review's three --
    Mutant(
        "VAR-1",
        "VARIATION",
        "review",
        CAMPAIGN,
        """        elif forbidden:""",
        """        elif not set(differs) & permitted:""",
        (C_UNIT, H_UNIT, I_UNIT, COMPILER_ALONE, CHAIN_ALONE),
        "a pair is accepted when any of its differences is allowed (intersection, "
        "not subset)",
    ),
    Mutant(
        "VAR-2",
        "VARIATION",
        "review",
        CAMPAIGN,
        """        forbidden = [item for item in differs if item not in permitted]""",
        """        forbidden = [item for item in differs if item not in permitted][1:]""",
        (B_UNIT, C_UNIT, COMPILER_ALONE, CHAIN_ALONE),
        "one forbidden difference of a pair is ignored",
    ),
    Mutant(
        "VAR-3",
        "VARIATION",
        "review",
        CAMPAIGN,
        """            found.setdefault(name, {}).setdefault("composed", []).append(str(sha))""",
        """            found.setdefault(name, {}).setdefault("composed", [])""",
        (H_UNIT, AGREE, REPRODUCTION),
        "a composed document is compared by name, never by its (nested) content",
    ),
    # ------------------------------------------------ each layer, alone --
    Mutant(
        "VAR-4",
        "VARIATION",
        "review",
        SHAPE,
        """        differing = set(pair.tokens)""",
        """        differing = set(pair.tokens) & attested""",
        (REPRODUCTION, REPLICATION),
        "the designer-side check compares only what the capability attests (the "
        "base candidate's defect)",
    ),
    Mutant(
        "VAR-13",
        "VARIATION",
        "review",
        SHAPE,
        """        if differing - allowed:""",
        """        if not differing & allowed:""",
        (REPRODUCTION, REPLICATION),
        "the designer-side check accepts a pair when any of its differences is "
        "allowed (intersection, not subset)",
    ),
    Mutant(
        "VAR-5",
        "VARIATION",
        "review",
        CAMPAIGN,
        """    unmet: list[Unmet] = unit_variation(""",
        """    unmet: list[Unmet] = [] and unit_variation(""",
        (C_UNIT, F_UNIT, COMPILER_ALONE),
        "the compiler does not apply the variation rule",
    ),
    Mutant(
        "VAR-6",
        "VARIATION",
        "review",
        CHAIN,
        """            _campaign_variation_problems(plan, contract=contract, capability=capability)""",
        """            []""",
        (CHAIN_ALONE, RECOVERED, REPLICATION_CHAIN),
        "the chain verification does not re-derive the variation before a run",
    ),
    # --------------------------------------------------- replication --
    Mutant(
        "VAR-7",
        "VARIATION",
        "review",
        EMPIRICAL,
        """    if frozen_shape is not None and planning is not None:""",
        """    if frozen_shape is not None and planning is not None and not replication:""",
        (REPLICATION,),
        "a replication campaign's design is not held to the frozen shape",
    ),
    Mutant(
        "VAR-8",
        "VARIATION",
        "review",
        CHAIN,
        """            for item in plan.payload.get("units") or ()""",
        """            for item in plan.payload.get("units") or ()
            if plan.payload.get("role") != "REPLICATION\"""",
        (REPLICATION_CHAIN,),
        "a replication campaign's frozen units are not re-verified before a run",
    ),
    # --------------------------------------------- what the sets are --
    Mutant(
        "VAR-9",
        "VARIATION",
        "review",
        CAMPAIGN,
        """                put(placeholder.group(1), f"argv[{position}]", str(token))""",
        """                continue""",
        (CLAIMED,),
        "the recorded parameters are trusted over the frozen argument vector",
    ),
    Mutant(
        "VAR-10",
        "VARIATION",
        "review",
        CAMPAIGN,
        """            put(UNATTRIBUTED + "env", key, str(value))""",
        """            pass""",
        (UNATTRIBUTED,),
        "an environment difference no declared input accounts for is ignored",
    ),
    Mutant(
        "VAR-11",
        "VARIATION",
        "review",
        CAMPAIGN,
        """        forbidden = [item for item in differs if item not in permitted]""",
        """        forbidden = [item for item in differs if item not in permitted] + sorted(
            permitted - set(differs)
        )""",
        (D_UNIT,),
        "every allowed difference is treated as required",
    ),
    Mutant(
        "VAR-12",
        "VARIATION",
        "review",
        CAMPAIGN,
        """        {capability.result.artifact_parameter} - {""}""",
        """        set() - {""}""",
        (G_UNIT,),
        "where the result is written is compared as if it were science",
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
