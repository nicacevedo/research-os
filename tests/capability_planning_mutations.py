"""Mutation testing for capability-aware analysis planning (docs/SCIENCE_EXECUTION.md §2a).

Each mutant removes or bypasses one enforcement point of the guarantee the
second final qualification's failure asked for -- an analysis is planned
against the committed capability envelope and checked against it before it
is frozen -- and names the tests that must notice:

- ``PLAN-1..6`` -- the check (``portfolio.shape``, ``capability``): one
  execution's capacity, whether a campaign is available at all, the human-set
  campaign bounds, a model's claim of capacity, a claimed campaign size,
  unattested unit differences;
- ``PLAN-7..9`` -- a refused proposal is never frozen: by the route, by the
  freezing function, by the database;
- ``PLAN-10..14`` -- the envelope binding: held, recorded, re-verified from
  the bytes, and immutable in the row;
- ``PLAN-15..17`` -- the lower bound: disjoint selections add, what a unit
  difference does not renew does not grow, the records bound is read;
- ``PLAN-18..21`` -- the bounded revision: a CAPABILITY_LIMITED answer is not
  bought again, the revision is bounded and spent, and the author is shown
  the envelope and the refusal;
- ``PLAN-22..24`` -- after freezing: the design realises the frozen shape, and
  an inherited analysis is checked too.

The runner is the integrity harness's: it applies one mutant, runs the tests
that must notice, restores the file whatever happens, and reports KILLED,
SURVIVED or INVALID. Not collected by pytest and never run by the suite.

    uv run python -m tests.capability_planning_mutations           # every mutant
    uv run python -m tests.capability_planning_mutations PLAN-3    # some of them
    uv run python -m tests.capability_planning_mutations --list

``tests/test_capability_planning.py::test_every_capability_planning_mutant_still_applies``
checks that every mutant still applies and names tests that exist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

SHAPE = "src/research_os/portfolio/shape.py"
CAPABILITY = "src/research_os/capability.py"
EMPIRICAL = "src/research_os/portfolio/empirical.py"
SCICONTRACT = "src/research_os/portfolio/scicontract.py"
SQL = "src/research_os/runtime/sql/0049_analysis_shape.sql"

T = "tests/test_capability_planning.py"
R = "tests/test_capability_planning_regression.py"
E = "tests/test_capability_planning_stage_machine_e2e.py"

A_UNIT = f"{T}::test_a_single_execution_that_one_execution_holds_is_valid"
B_UNIT = f"{T}::test_a_single_execution_one_execution_cannot_hold_is_a_mismatch"
C_UNIT = f"{T}::test_a_requirement_no_allowed_shape_can_hold_is_capability_limited"
D_UNIT = f"{T}::test_a_claimed_capacity_beyond_the_committed_envelope_is_refused"
E_UNIT = f"{T}::test_campaign_units_differing_in_what_is_not_attested_are_refused"
RENEWAL = f"{T}::test_what_a_unit_difference_does_not_renew_does_not_grow_with_units"
DISJOINT = (
    f"{T}::test_records_selected_by_different_values_of_a_bounded_field_need"
    "_different_slots"
)
ENVELOPE = (
    f"{T}::test_the_envelope_is_derived_from_committed_sources_and_human_bounds_only"
)
A_ROUTE = f"{T}::test_an_analysis_one_execution_holds_is_frozen_bound_to_the_envelope_it_was_shown"
B_ROUTE = (
    f"{T}::test_a_single_execution_analysis_one_execution_cannot_hold_is_revised"
    "_before_freezing"
)
C_ROUTE = f"{T}::test_an_analysis_nothing_allowed_can_hold_is_never_frozen_and_not_bought_twice"
D_ROUTE = f"{T}::test_a_claim_of_more_capacity_is_refused_before_freezing_on_the_route"
E_ROUTE = f"{T}::test_units_differing_in_the_unattested_are_refused_before_freezing_on_the_route"
F = f"{T}::test_a_frozen_analysis_and_its_envelope_binding_never_change"
G = f"{T}::test_a_retry_or_a_restart_never_freezes_a_refused_draft"
H = f"{T}::test_an_envelope_that_changed_invalidates_an_unread_binding"
REPLICATION = f"{T}::test_a_replication_inherits_the_frozen_shape_and_is_checked_against_the_envelope"
REALISED = f"{T}::test_the_design_realises_the_frozen_execution_shape_and_nothing_else"
TOO_SMALL = (
    f"{T}::test_a_campaign_too_small_for_the_frozen_support_is_refused_before_it_runs"
)

CLASS = f"{R}::test_a_single_execution_requirement_a_campaign_can_hold_is_refused_before_freezing"
SWEEP = f"{R}::test_the_architecture_class_across_every_sample_size"
OFFSETS = f"{R}::test_seed_offsets_alone_cannot_supply_more_sizes_than_one_plan_holds"
BEFORE_AFTER = (
    f"{R}::test_the_old_declaration_froze_it_and_the_committed_one_refuses_it"
)
ON_ROUTE = f"{R}::test_on_the_route_the_single_form_cannot_freeze_and_the_campaign_form_is_designed"
STAGE_MACHINE = (
    f"{E}::test_a_sample_larger_than_one_execution_is_planned_as_a_campaign_before_it"
    "_freezes"
)

MUTANTS: tuple[Mutant, ...] = (
    # --------------------------------------------------------- the check --
    Mutant(
        "PLAN-1",
        "PLANNING",
        "run2",
        SHAPE,
        """        if not single_fits:
            problems.extend(""",
        """        if False:
            problems.extend(""",
        (B_UNIT, CLASS, SWEEP, BEFORE_AFTER, B_ROUTE),
        "the single-execution capacity check is removed",
    ),
    Mutant(
        "PLAN-2",
        "PLANNING",
        "run2",
        SHAPE,
        """    allowed_fits = campaign_supported and not campaign_short(cap.units, allowed_varies)""",
        """    allowed_fits = True""",
        (C_UNIT, SWEEP, C_ROUTE),
        "a campaign is pretended to be always available, and able to hold anything",
    ),
    Mutant(
        "PLAN-3",
        "PLANNING",
        "run2",
        CAPABILITY,
        """            int(bounds.max_campaign_units),
            int(bounds.max_campaign_seconds) // max(1, seconds),""",
        """            int(capability.campaign.max_units),
            int(capability.campaign.max_units),""",
        (C_UNIT, ENVELOPE),
        "campaigns are available beyond the human-set unit and time bounds",
    ),
    Mutant(
        "PLAN-4",
        "PLANNING",
        "run2",
        SHAPE,
        """        committed = _committed_capacity(cap, name, observables)""",
        """        committed = max(claimed, _committed_capacity(cap, name, observables) or 0)""",
        (D_UNIT, D_ROUTE),
        "a model-provided capacity overrides the committed envelope",
    ),
    Mutant(
        "PLAN-5",
        "PLANNING",
        "run2",
        SHAPE,
        """            if shape_units is not None and shape_units > cap.units:""",
        """            if False:""",
        (D_UNIT,),
        "a campaign larger than the envelope allows is accepted when claimed",
    ),
    Mutant(
        "PLAN-6",
        "PLANNING",
        "run2",
        SHAPE,
        """            if unattested:""",
        """            if False:""",
        (E_UNIT, E_ROUTE),
        "campaign units may differ in what the capability does not attest",
    ),
    # --------------------------------------------- a refusal never freezes --
    Mutant(
        "PLAN-7",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    checked = planning.check(spec) if planning is not None else None
    if checked is not None and checked.refused:
        return _refuse_proposal(""",
        """    checked = planning.check(spec) if planning is not None else None
    if False:
        return _refuse_proposal(""",
        (B_ROUTE, C_ROUTE, G),
        "the route freezes the refused draft",
    ),
    Mutant(
        "PLAN-8",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    if checked is not None and checked.refused:
        raise EmpiricalError(""",
        """    if False:
        raise EmpiricalError(""",
        (G,),
        "the freezing function accepts a refused check",
    ),
    Mutant(
        "PLAN-9",
        "PLANNING",
        "run2",
        SQL,
        """           and d.analysis_digest = new.analysis_digest""",
        """           and false and d.analysis_digest = new.analysis_digest""",
        (G,),
        "the database freezes an analysis refused under the same envelope",
    ),
    # --------------------------------------------------- the binding ------
    Mutant(
        "PLAN-10",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    return contract.envelope_digest == planning.envelope.digest""",
        """    return True""",
        (H,),
        "an unread contract checked against another envelope is designed anyway",
    ),
    Mutant(
        "PLAN-11",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """        envelope_digest=(
            checked.envelope_digest
            if checked is not None and checked.envelope_digest
            else None
        ),""",
        """        envelope_digest=None,""",
        (A_ROUTE, H),
        "the envelope an analysis was checked against is not bound to its contract",
    ),
    Mutant(
        "PLAN-12",
        "PLANNING",
        "run2",
        SCICONTRACT,
        """        if check.get("envelope_digest") != contract.envelope_digest:""",
        """        if False:""",
        (F,),
        "verification does not read the binding back out of the frozen bytes",
    ),
    Mutant(
        "PLAN-13",
        "PLANNING",
        "run2",
        SCICONTRACT,
        """        if check.get("verdict") not in FREEZABLE_SHAPE_VERDICTS:""",
        """        if False:""",
        (F,),
        "verification accepts a frozen analysis whose record says it was refused",
    ),
    Mutant(
        "PLAN-14",
        "PLANNING",
        "run2",
        SQL,
        """    if new.envelope_digest is distinct from old.envelope_digest then""",
        """    if false then""",
        (F,),
        "the database lets a contract's envelope binding change",
    ),
    # ----------------------------------------------------- the lower bound --
    Mutant(
        "PLAN-15",
        "PLANNING",
        "run2",
        SHAPE,
        """        if len(groups) < 2:
            continue""",
        """        if True:
            continue""",
        (DISJOINT, CLASS),
        "records of different values of a bounded field are taken to share slots",
    ),
    Mutant(
        "PLAN-16",
        "PLANNING",
        "run2",
        CAPABILITY,
        """        return tuple(
            item.varies for item in bound.across_units if field_name in item.fields
        )""",
        """        return self.unit_varies""",
        (RENEWAL, OFFSETS),
        "what a unit difference does not renew is taken to grow with units",
    ),
    Mutant(
        "PLAN-17",
        "PLANNING",
        "run2",
        SHAPE,
        """        limit = envelope.records_max(capability_name)""",
        """        limit = None""",
        (B_UNIT,),
        "the declared records bound is not read",
    ),
    # ------------------------------------------------ the bounded revision --
    Mutant(
        "PLAN-18",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    if limited:""",
        """    if False:""",
        (C_ROUTE,),
        "a CAPABILITY_LIMITED analysis is bought again under the same envelope",
    ),
    Mutant(
        "PLAN-19",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    elif len(earlier) + 1 < shapes.MAX_REFUSED_DRAFTS:""",
        """    elif True:""",
        (G,),
        "the revision after a refusal is unbounded",
    ),
    Mutant(
        "PLAN-20",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    if len(earlier) >= shapes.MAX_REFUSED_DRAFTS:""",
        """    if False:""",
        (G,),
        "a spent revision is paid for again",
    ),
    Mutant(
        "PLAN-21",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """        blocks["capability_envelope"] = planning.envelope.lines()""",
        """        pass""",
        (A_ROUTE, STAGE_MACHINE),
        "the analysis author is not shown the envelope before it chooses",
    ),
    Mutant(
        "PLAN-22",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """            blocks["refused_analysis"] = _refused_block(context, earlier[-1])""",
        """            pass""",
        (B_ROUTE, ON_ROUTE),
        "the revision is not shown why its proposal was refused",
    ),
    # --------------------------------------------------- after freezing --
    Mutant(
        "PLAN-23",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    if frozen_shape is not None and planning is not None:""",
        """    if False:""",
        (REALISED, TOO_SMALL),
        "the experiment designer may reinterpret the frozen execution shape",
    ),
    Mutant(
        "PLAN-24",
        "PLANNING",
        "run2",
        EMPIRICAL,
        """    if checked is not None and checked.refused:
        return ExperimentStep(
            ok=False,
            detail=(
                f"the primary's frozen analysis cannot be executed as a replication \"""",
        """    if False:
        return ExperimentStep(
            ok=False,
            detail=(
                f"the primary's frozen analysis cannot be executed as a replication \"""",
        (REPLICATION,),
        "an inherited analysis is frozen without being checked against the envelope",
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
