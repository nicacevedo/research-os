"""The independent review's attack, on the route, and every way around it that remains.

``docs/SCIENCE_EXECUTION.md`` §3a is the specification. The review
(``CAPABILITY_PLANNING_CLOSURE_FAIL``, against 7f39fd1) reproduced a campaign
executing with scientific variation its frozen analysis forbade:

- the frozen analysis permits units to differ in the seed only;
- the campaign design changes the seed *and* the slope inside the composed
  plan between its units;
- the designer-side check filtered each pair's differences to what the
  capability attests (``seeds``) before comparing them with the frozen shape,
  so the plan change was never seen;
- the compiler accepted the pair because one of its differences was attested;
- the campaign ran, was read ``SUPPORTED`` and passed science-chain
  admissibility.

On the base candidate the first test here fails for exactly that reason
(the campaign runs). Now it is refused before anything runs, three times
over, each independently: by the designer-side check, by the compiler, and
by the chain verification every run, every reading and readiness goes
through. The later tests take the earlier layers away -- restoring the base
candidate's own rules for them -- and show the remaining one still refuses:
on a retry, on a recovered execution and for a replication campaign.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import campaign as campaigns
from research_os.portfolio import empirical, sciencechain
from research_os.portfolio import shape as shapes
from research_os.portfolio.models import (
    ContractState,
    ExperimentRole,
    ExperimentState,
    PrimaryOutcome,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from tests.portfolio_helpers import portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_science_campaigns import (
    advance,
    analysis,
    campaign_design,
    chains,
    commit_manifest,
    context_for,
    jobs,
    manifest,
    outcomes,
    router,
    science_repo,
    unit,
)

__all__ = [
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "science_repo",
]

#: Read outcomes: what a trusted scientific reading of the campaign would be.
READ = {PrimaryOutcome.SUPPORTED, PrimaryOutcome.REFUTED, PrimaryOutcome.INCONCLUSIVE}

#: The attack: two units, a new seed each, and unit 1's plan at another slope.
ATTACK = [unit(7), unit(8, slope=3.0)]


def seed_only_capability(repo: Path) -> None:
    """The review's declaration: campaign units may differ in the seed only."""

    commit_manifest(repo, manifest(unit_varies="[seeds]"))


def attacked(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    the_analysis: dict[str, Any] | None = None,
    units: list[dict[str, Any]] | None = None,
) -> Any:
    return context_for(
        portfolio,
        runtime_db,
        runtime_project,
        repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design(units or ATTACK),
            the_analysis=the_analysis,
        ),
    )


def nothing_was_read(context: Any, runtime_db: Database) -> None:
    """Nothing ran, and nothing a gate could take for a scientific reading exists."""

    assert jobs(runtime_db) == []
    assert not [item for item in outcomes(context) if item.state in READ]
    assert not [item for item in outcomes(context) if item.receipt_id]
    assert not [item for item in chains(context) if item.admissible]


def base_candidate_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two pre-run checks exactly as 7f39fd1 applied them, and nothing else.

    The designer-side check saw only what the capability attests of each
    pair's differences; the compiler accepted a pair any of whose differences
    was attested. What is left is the chain verification.
    """

    real_realisation = shapes.realisation_problems
    real_rule = campaigns.unit_variation

    def filtered(shape_check: Any, *, unit_differences: Any, **kwargs: Any) -> Any:
        cap = kwargs["envelope"].capability(shape_check.capability)
        attested = set(cap.unit_varies) if cap is not None else set()
        return real_realisation(
            shape_check,
            unit_differences=[
                dataclasses.replace(
                    pair, tokens=tuple(item for item in pair.tokens if item in attested)
                )
                for pair in unit_differences
            ],
            **kwargs,
        )

    def intersecting(pairs: Any, *, allowed: Any, attested: Any, ref: str) -> Any:
        return real_rule(
            [pair for pair in pairs if not set(pair.tokens) & set(attested)],
            allowed=allowed,
            attested=attested,
            ref=ref,
        )

    monkeypatch.setattr(shapes, "realisation_problems", filtered)
    monkeypatch.setattr(campaigns, "unit_variation", intersecting)


# ====================================================== the reproduction --
def test_the_reviewers_attack_is_refused_before_anything_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Seed only is allowed; the units change the seed and the slope. Refused.

    On the base candidate this campaign ran both units, was read SUPPORTED
    and was admissible.
    """

    seed_only_capability(science_repo)
    context = attacked(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "the design does not realise the frozen execution shape" in step.detail
    assert "units 0 and 1 differ in plan (at plan.slope)" in step.detail
    assert "the frozen analysis lets them differ only in seeds" in step.detail
    nothing_was_read(context, runtime_db)
    assert portfolio.list_experiments(idea_id=context.idea_id) == ()
    assert outcomes(context) == []
    contract = portfolio.live_contract(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert contract is not None and contract.state is ContractState.ANALYSIS_FROZEN


def test_a_stated_seed_only_shape_refuses_a_plan_change_beside_a_seed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The capability attests the plan; the frozen analysis allowed only the seed."""

    frozen = analysis()
    frozen["execution_shape"] = {
        "capability": "synthetic.draw@1",
        "units": 2,
        "unit_varies": ["seeds"],
    }
    context = attacked(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        the_analysis=frozen,
    )
    step = advance(context)
    assert not step.ok
    assert "units 0 and 1 differ in plan (at plan.slope)" in step.detail
    nothing_was_read(context, runtime_db)
    assert portfolio.list_experiments(idea_id=context.idea_id) == ()


def test_a_retry_is_held_to_the_same_rule_and_the_analysis_does_not_move(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The queue's retry asks again; the same design is refused again, and a
    conforming one runs under the very analysis that was frozen first."""

    seed_only_capability(science_repo)
    context = attacked(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    assert not advance(context).ok
    contract = portfolio.live_contract(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert contract is not None
    again = advance(context)
    assert not again.ok and "differ in plan (at plan.slope)" in again.detail
    nothing_was_read(context, runtime_db)

    context.models.answers_by_prompt[TEMPLATES["experiment_designer"].identity] = (
        campaign_design([unit(7), unit(8)])
    )
    measured = advance(context)
    assert measured.ok, measured.detail
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.state is ExperimentState.INTERPRETED
    assert experiment.contract_id == contract.contract_id
    held = portfolio.require_contract(contract.contract_id)
    assert held.analysis_digest == contract.analysis_digest
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert [item["varies_from_first_unit"] for item in chain.units] == [[], ["seeds"]]
    (record,) = chains(context)
    assert record.admissible, record.problems


# ====================================== each layer alone still refuses it --
def test_without_the_designer_side_check_the_compiler_refuses_it_alone(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the base candidate's designer-side check, the compiler is enough."""

    seed_only_capability(science_repo)
    real_rule = campaigns.unit_variation
    base_candidate_rules(monkeypatch)
    monkeypatch.setattr(campaigns, "unit_variation", real_rule)
    context = attacked(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "the design does not realise" not in step.detail
    assert (
        "units 0 and 1 differ in plan (at plan.slope), and the frozen analysis lets "
        "campaign units differ only in seeds"
    ) in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert outcome.reason == "campaign_not_compiled"
    nothing_was_read(context, runtime_db)
    assert portfolio.list_experiments(idea_id=context.idea_id) == ()


def _frozen_by_the_base_rules(
    context: Any, monkeypatch: pytest.MonkeyPatch, *, role: ExperimentRole
) -> Any:
    """The attack's campaign, frozen as the base candidate would have frozen it."""

    version = context.portfolio.require_version(context.idea_id)
    with monkeypatch.context() as patched:
        base_candidate_rules(patched)
        step = empirical.design(
            context,
            version,
            role=role,
            previous=empirical.previous_for(context, role),
        )
    assert step.ok, step.detail
    assert step.experiment is not None
    return step.experiment


def test_a_campaign_frozen_under_the_base_rules_never_runs_and_is_never_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chain verification alone: before any unit runs, on every attempt."""

    seed_only_capability(science_repo)
    context = attacked(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    experiment = _frozen_by_the_base_rules(
        context, monkeypatch, role=ExperimentRole.PRIMARY
    )
    # What the base candidate froze: a plan whose units differ in the plan.
    with pytest.raises(sciencechain.ChainError) as caught:
        sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert (
        "units 0 and 1 differ in plan, and the frozen analysis lets campaign units "
        "differ only in seeds"
    ) in str(caught.value)

    for _attempt in range(2):
        step = advance(context)
        assert not step.ok
        assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
        assert "units 0 and 1 differ in plan" in step.detail
        nothing_was_read(context, runtime_db)
    failed = portfolio.require_experiment(experiment.experiment_id)
    assert failed.state is ExperimentState.OPERATIONALLY_FAILED
    assert failed.evidence_id is None
    read = empirical.interpret_campaign(context, failed)
    assert not read.ok
    assert read.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    nothing_was_read(context, runtime_db)


def test_a_recovered_campaign_is_held_to_the_rule(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between freezing the contract and recording its execution: the
    recovery records the execution the contract names, and it does not run."""

    seed_only_capability(science_repo)
    context = attacked(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    version = portfolio.require_version(context.idea_id)

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the process stopped here")

    with monkeypatch.context() as patched:
        base_candidate_rules(patched)
        patched.setattr(PortfolioStore, "create_experiment", crash)
        with pytest.raises(RuntimeError, match="the process stopped here"):
            empirical.design(context, version)
    contract = portfolio.live_contract(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )
    assert contract is not None and contract.state is ContractState.FROZEN
    assert portfolio.list_experiments(idea_id=context.idea_id) == ()

    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "units 0 and 1 differ in plan" in step.detail
    (recovered,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert recovered.contract_id == contract.contract_id
    assert recovered.state is ExperimentState.OPERATIONALLY_FAILED
    nothing_was_read(context, runtime_db)


# ========================================================== replication --
def _primary_measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    replication: dict[str, Any],
) -> Any:
    seed_only_capability(repo)
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        repo,
        tmp_path,
        router(
            runtime_db,
            primary=campaign_design([unit(7), unit(8)]),
            replication=replication,
        ),
    )
    step = advance(context)
    assert step.ok, step.detail
    assert len(jobs(runtime_db)) == 2
    return context


def _replication_nothing_was_read(context: Any, runtime_db: Database) -> None:
    assert len(jobs(runtime_db)) == 2  # the primary's two units, and only those
    assert not [
        item
        for item in outcomes(context, ExperimentRole.REPLICATION)
        if item.state in READ or item.receipt_id
    ]
    records = {item.role: item for item in chains(context)}
    assert records[ExperimentRole.PRIMARY].admissible
    assert ExperimentRole.REPLICATION not in records or not (
        records[ExperimentRole.REPLICATION].admissible
    )


def test_a_replication_campaign_with_a_forbidden_variation_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Its units are governed by the same frozen analysis as the primary's."""

    context = _primary_measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(17), unit(18, slope=3.0)], variation="seeds"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "the design does not realise the frozen execution shape" in step.detail
    assert "units 0 and 1 differ in plan (at plan.slope)" in step.detail
    assert (
        portfolio.get_experiment(
            idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
        )
        is None
    )
    _replication_nothing_was_read(context, runtime_db)


def test_a_replication_campaign_frozen_under_the_base_rules_never_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _primary_measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(17), unit(18, slope=3.0)], variation="seeds"),
    )
    replication = _frozen_by_the_base_rules(
        context, monkeypatch, role=ExperimentRole.REPLICATION
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "units 0 and 1 differ in plan" in step.detail
    assert (
        portfolio.require_experiment(replication.experiment_id).state
        is ExperimentState.OPERATIONALLY_FAILED
    )
    _replication_nothing_was_read(context, runtime_db)


def test_the_stronger_replication_rule_is_kept(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Varying only what is allowed does not license re-measuring the primary."""

    context = _primary_measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=campaign_design([unit(8), unit(9)], variation="seeds"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert "unit 0 is the primary's unit 1 run again" in step.detail
    _replication_nothing_was_read(context, runtime_db)
