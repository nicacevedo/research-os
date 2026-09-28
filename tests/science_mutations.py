"""Mutation testing for the v1 science chain (``docs/SCIENCE_EXECUTION.md``).

Each mutant removes one enforcement point the closure added -- mechanical
capability resolution, the frozen-object ordering and immutability, the
plan-pinned execution, result validation, the system-computed outcome and the
gate's reading of the chain -- as a one-snippet edit. The runner is the
integrity harness's: it applies the mutant, runs the tests that must notice,
restores the file whatever happens, and reports KILLED, SURVIVED or INVALID.

Not collected by pytest and never run by the suite.

    uv run python -m tests.science_mutations            # every mutant
    uv run python -m tests.science_mutations SCI-01-1   # some of them
    uv run python -m tests.science_mutations --list

``tests/test_science_invariants.py`` checks that every mutant named in
``docs/science_invariants.yaml`` exists here and still applies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.integrity_mutations import Mutant, run

CAP = "src/research_os/capability.py"
CHAIN = "src/research_os/portfolio/sciencechain.py"
EMP = "src/research_os/portfolio/empirical.py"
GATES = "src/research_os/portfolio/gates.py"
MODELS = "src/research_os/portfolio/models.py"
SQL = "src/research_os/runtime/sql/0047_science_chain.sql"
SCI = "src/research_os/portfolio/scicontract.py"

T = "tests/test_capability.py"
E = "tests/test_science_v1_e2e.py"
G = "tests/test_portfolio_gates.py"

MUTANTS: tuple[Mutant, ...] = (
    # ----------------------------------------- SCI-01 capability honesty --
    Mutant(
        "SCI-01-1",
        "SCI-01",
        "-",
        CAP,
        """        declared_field = observable.field(name)
        if declared_field is None:
            unmet.append(""",
        """        declared_field = observable.field(name)
        if declared_field is None:
            continue
            unmet.append(""",
        (
            f"{T}::test_an_unmet_requirement_is_capability_limited_and_named",
            f"{E}::test_an_observable_no_capability_declares_is_capability_limited",
        ),
        "a field the capability does not declare is accepted",
    ),
    Mutant(
        "SCI-01-2",
        "SCI-01",
        "-",
        CAP,
        "if name in need.numeric and declared_field.type not in NUMERIC_TYPES:",
        "if False:",
        (
            f"{T}::test_an_unmet_requirement_is_capability_limited_and_named",
            f"{E}::test_a_numeric_reading_of_a_string_field_is_capability_limited",
        ),
        "a string field read as a number is accepted",
    ),
    Mutant(
        "SCI-01-3",
        "SCI-01",
        "-",
        CAP,
        """    for token in requirements.perturbations:
        if token not in offered:""",
        """    for token in requirements.perturbations:
        if False:""",
        (
            f"{T}::test_a_chosen_command_perturbation_and_host_limit_are_all_checked",
            f"{E}::test_a_replication_varying_what_the_capability_does_not_attest_gets_no_plan",
        ),
        "a replication may vary what the capability does not attest",
    ),
    Mutant(
        "SCI-01-4",
        "SCI-01",
        "-",
        CAP,
        "and capability.resources.timeout_seconds > requirements.max_seconds",
        "and False",
        (f"{T}::test_a_chosen_command_perturbation_and_host_limit_are_all_checked",),
        "a capability needing longer than the host allows is executable",
    ),
    Mutant(
        "SCI-01-5",
        "SCI-01",
        "-",
        CAP,
        "if result_path is not None and need.source != result_path:",
        "if False:",
        (f"{T}::test_a_result_named_by_a_path_parameter_is_located_by_its_value",),
        "an observable read from a file other than the result artifact binds",
    ),
    Mutant(
        "SCI-01-6",
        "SCI-01",
        "-",
        EMP,
        """        if not resolution.executable:
            return _capability_limited(""",
        """        if False:
            return _capability_limited(""",
        (f"{E}::test_an_observable_no_capability_declares_is_capability_limited",),
        "no mechanical resolution before the design is asked for",
    ),
    Mutant(
        "SCI-01-7",
        "SCI-01",
        "-",
        CAP,
        """    unmet = check_against_command(capability, commands)
    result_path = _result_path(capability, requirements.parameters)""",
        """    unmet = []
    result_path = _result_path(capability, requirements.parameters)""",
        (f"{E}::test_a_contract_no_host_can_execute_is_capability_limited",),
        "a capability the host never declared resolves",
    ),
    # ------------------------------------------ SCI-02 the frozen chain --
    Mutant(
        "SCI-02-1",
        "SCI-02",
        "-",
        SQL,
        "    if parent.frozen_at > new.frozen_at then",
        "    if false then",
        (f"{E}::test_nothing_is_frozen_before_what_it_realises",),
        "an object may be frozen before the object it realises",
    ),
    Mutant(
        "SCI-02-2",
        "SCI-02",
        "-",
        SQL,
        "       or (new.kind = 'PLAN' and parent.kind <> 'DESIGN') then",
        "       then",
        (f"{E}::test_nothing_is_frozen_before_what_it_realises",),
        "a plan may realise a contract directly, skipping the design",
    ),
    Mutant(
        "SCI-02-3",
        "SCI-02",
        "-",
        SQL,
        "    if tg_op = 'UPDATE' and new.plan_digest is distinct from old.plan_digest then",
        "    if false then",
        (f"{E}::test_frozen_objects_and_an_experiments_plan_never_change",),
        "an experiment's plan may be repointed",
    ),
    Mutant(
        "SCI-02-4",
        "SCI-02",
        "-",
        CHAIN,
        """    try:
        payload = provenance.verified_json(artifacts, row.artifact_id)
    except provenance.ProvenanceError as exc:""",
        """    try:
        payload = json.loads(artifacts.get_bytes(row.artifact_id))
    except provenance.ProvenanceError as exc:""",
        (f"{E}::test_a_changed_frozen_design_stops_the_execution",),
        "a frozen object is read without re-hashing its bytes",
    ),
    Mutant(
        "SCI-02-5",
        "SCI-02",
        "-",
        EMP,
        """    chain: sciencechain.VerifiedChain | None = None
    if experiment.plan_digest:
        try:
            chain = _verified_chain(context, experiment)
        except (sciencechain.ChainError, EmpiricalError) as exc:
            return _operational(context, experiment, str(exc), exc.failure_class)""",
        """    chain: sciencechain.VerifiedChain | None = None
    if False:
        pass""",
        (f"{E}::test_a_changed_frozen_design_stops_the_execution",),
        "an execution runs without re-verifying its frozen chain",
    ),
    Mutant(
        "SCI-02-6",
        "SCI-02",
        "-",
        EMP,
        "            base_commit=chain.code_commit if chain is not None else None,",
        "            base_commit=None,",
        (f"{E}::test_the_execution_runs_the_commit_its_plan_froze",),
        "the workspace is cut from HEAD, not from the plan's commit",
    ),
    Mutant(
        "SCI-02-7",
        "SCI-02",
        "-",
        SQL,
        "    if new.plan_digest is distinct from wanted then",
        "    if false then",
        (f"{E}::test_a_receipt_must_name_its_experiments_plan",),
        "a receipt of a plan-bound execution may name no plan",
    ),
    Mutant(
        "SCI-02-8",
        "SCI-02",
        "-",
        SQL,
        "        if found and (rct.experiment_id is distinct from new.experiment_id",
        "        if false and (rct.experiment_id is distinct from new.experiment_id",
        (f"{E}::test_the_database_refuses_an_outcome_that_reads_another_execution",),
        "an outcome may read another execution's receipt",
    ),
    Mutant(
        "SCI-02-9",
        "SCI-02",
        "-",
        CHAIN,
        """        if rebuilt != contract.digest:
            problems.append(""",
        """        if False:
            problems.append(""",
        (f"{E}::test_a_chain_whose_contract_the_analysis_does_not_derive_is_refused",),
        "a contract object the frozen analysis does not derive is accepted",
    ),
    Mutant(
        "SCI-02-10",
        "SCI-02",
        "-",
        SCI,
        """    if not payload.get("population"):
        payload.pop("population", None)""",
        """    if False:
        payload.pop("population", None)""",
        (f"{E}::test_an_analysis_without_a_population_keeps_the_digest_it_had",),
        "adding the population field moves every earlier analysis digest",
    ),
    # --------------------------------------- SCI-03 result validation --
    Mutant(
        "SCI-03-1",
        "SCI-03",
        "-",
        CHAIN,
        "        validate_document(document, schema, where=path)",
        "        pass",
        (f"{E}::test_a_result_off_its_declaration_is_invalid_evidence",),
        "a result off its declared schema is accepted",
    ),
    Mutant(
        "SCI-03-2",
        "SCI-03",
        "-",
        EMP,
        "    if result_check is not None and not result_check.ok:",
        "    if False:",
        (f"{E}::test_a_result_off_its_declaration_is_invalid_evidence",),
        "the decision rule is applied to an invalid result",
    ),
    Mutant(
        "SCI-03-3",
        "SCI-03",
        "-",
        CHAIN,
        """    if not result.ok:
        return PrimaryOutcome.INVALID_EVIDENCE, result.reason""",
        """    if False:
        return PrimaryOutcome.INVALID_EVIDENCE, result.reason""",
        (f"{E}::test_a_result_off_its_declaration_is_invalid_evidence",),
        "an invalid result's outcome is read from the rule",
    ),
    Mutant(
        "SCI-03-4",
        "SCI-03",
        "-",
        CHAIN,
        """    if hashlib.sha256(data).hexdigest() != sha:
        return ResultCheck(""",
        """    if False:
        return ResultCheck(""",
        (f"{E}::test_validation_reads_only_the_bytes_the_runner_hashed",),
        "the validated bytes need not be the bytes the runner hashed",
    ),
    # ----------------------------------------- SCI-04 system outcomes --
    Mutant(
        "SCI-04-1",
        "SCI-04",
        "-",
        CHAIN,
        """    EmpiricalConclusion.INCONCLUSIVE: (
        PrimaryOutcome.INCONCLUSIVE,""",
        """    EmpiricalConclusion.INCONCLUSIVE: (
        PrimaryOutcome.SUPPORTED,""",
        (f"{E}::test_the_primary_outcome_is_computed_from_the_frozen_rule",),
        "the inconclusive region reads as support",
    ),
    Mutant(
        "SCI-04-2",
        "SCI-04",
        "-",
        CHAIN,
        "    FailureClass.BUDGET_EXHAUSTED: PrimaryOutcome.BUDGET_LIMITED,\n",
        "",
        (f"{E}::test_an_execution_a_budget_cannot_cover_is_budget_limited",),
        "a budget-limited execution records no outcome",
    ),
    Mutant(
        "SCI-04-3",
        "SCI-04",
        "-",
        CHAIN,
        "    FailureClass.EXECUTOR_FAILED: PrimaryOutcome.EXECUTION_FAILED,",
        "    FailureClass.EXECUTOR_FAILED: PrimaryOutcome.INVALID_EVIDENCE,",
        (
            f"{E}::test_an_execution_that_crashes_is_execution_failed_and_never_evidence",
        ),
        "a crashed execution is recorded as a reading problem",
    ),
    Mutant(
        "SCI-04-4",
        "SCI-04",
        "-",
        CHAIN,
        '    EmpiricalConclusion.CONTRADICTS: (PrimaryOutcome.REFUTED, "failure_criterion_held"),',
        '    EmpiricalConclusion.CONTRADICTS: (PrimaryOutcome.INCONCLUSIVE, "failure_criterion_held"),',
        (f"{E}::test_the_primary_outcome_is_computed_from_the_frozen_rule",),
        "a refutation is softened to inconclusive",
    ),
    # ---------------------------------------- SCI-05 the gate reads it --
    Mutant(
        "SCI-05-1",
        "SCI-05",
        "-",
        GATES,
        """        if chain is not None and chain.admissible:
            return True, \"\"""",
        """        if True:
            return True, \"\"""",
        (
            f"{G}::test_an_empirical_measurement_without_a_science_chain_does_not_validate",
        ),
        "an executed row validates with no science chain behind it",
    ),
    Mutant(
        "SCI-05-2",
        "SCI-05",
        "-",
        MODELS,
        """            and self.capability_ref is not None
            and self.outcome in DECISIVE_OUTCOMES""",
        """            and self.outcome in DECISIVE_OUTCOMES""",
        (
            f"{G}::test_an_empirical_measurement_without_a_science_chain_does_not_validate",
        ),
        "a chain bound to no capability is admissible",
    ),
    Mutant(
        "SCI-05-3",
        "SCI-05",
        "-",
        MODELS,
        """            and self.capability_ref is not None
            and self.outcome in DECISIVE_OUTCOMES""",
        """            and self.capability_ref is not None
            and self.outcome is not None""",
        (
            f"{G}::test_an_empirical_measurement_without_a_science_chain_does_not_validate",
            f"{E}::test_the_primary_outcome_is_computed_from_the_frozen_rule",
        ),
        "an inconclusive or invalid outcome is admissible",
    ),
    Mutant(
        "SCI-05-4",
        "SCI-05",
        "-",
        GATES,
        """        admissible_chains = {
            chain.evidence_id for chain in science_chains if chain.admissible
        }""",
        """        admissible_chains = {item.evidence_id for item in evidence}""",
        (f"{G}::test_a_replication_needs_its_own_science_chain_for_human_ready",),
        "a replication needs no science chain of its own",
    ),
    Mutant(
        "SCI-05-5",
        "SCI-05",
        "-",
        CHAIN,
        """            if outcome is None:
                problems.append("no system-computed outcome reads this execution")""",
        """            if False:
                problems.append("no system-computed outcome reads this execution")""",
        (f"{E}::test_a_reading_with_no_recorded_outcome_is_not_an_intact_chain",),
        "a chain with no recorded outcome is intact",
    ),
)

#: Mutants no test can kill because another protection makes them
#: unobservable -- kept with the argument rather than hidden.
EQUIVALENT: dict[str, str] = {
    "_verify_input_artifacts removed from submit": (
        "The workspace is cut from the plan's own commit and the plan's input "
        "hashes were read from that commit's Git objects, so the bytes in the "
        "workspace cannot differ from the plan's hashes unless Git's object "
        "store itself changed. The check is defence in depth against a "
        "workspace adopted from elsewhere, which `submit` never does -- it "
        "always releases and recreates the workspace first."
    ),
    "the live-experiment check in sciencechain._reading_chain": (
        "An INTERPRETED experiment can never be superseded (sql/0046 refuses "
        "the state change), and evidence rows exist only for interpreted "
        "experiments, so the experiment named by a readable row is always its "
        "version's live one. The check states the rule for a future state "
        "machine rather than deciding a case today."
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    chosen = [m for m in MUTANTS if not args.ids or m.id in args.ids]
    if args.list:
        for mutant in chosen:
            print(f"{mutant.id:10} {mutant.invariant:7} {mutant.what}")
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
