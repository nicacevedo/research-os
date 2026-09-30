"""Capability-aware analysis planning: the envelope, the pre-freeze check, the revision.

``docs/SCIENCE_EXECUTION.md`` §1a and §2a are the specification. The second
final v1 qualification (66d5704) had two lineages reach the evidence stage;
both analysis authors froze ``fixed_single_execution`` for a sample one
execution of the only capability could not hold, and the experiment designer
-- who alone was shown the per-execution bound -- refused, correctly, after
the analysis could no longer change. Here the synthetic science repository of
``tests/test_science_campaigns.py`` declares the bounds its program really
has: one execution draws one noise seed (``RESEARCH_OS_SEED_0``) over at most
five ``x``, so at most five records. What these tests prove, by letter:

A. an analysis one execution holds is shown the envelope, checked, and frozen
   bound to it;
B. a single-execution analysis one execution cannot hold is refused before
   freezing with a structured mismatch, the author revises it to a campaign,
   only the revision is frozen, and the design and the campaign proceed;
C. an analysis neither shape can hold is CAPABILITY_LIMITED, nothing is
   frozen, and asking again under the same envelope costs nothing;
D. a claim that one execution (or one campaign) holds more than the committed
   envelope says is refused;
E. campaign units differing in something the capability does not attest are
   refused;
F. a frozen analysis and its envelope binding never change;
G. a retry or a restart never freezes a refused draft, and the revision is
   bounded;
H. an envelope that changed -- its commit, its declaration or its bounds --
   invalidates an unread contract's binding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os import capability as capabilities
from research_os.capability import CapabilityError
from research_os.experiment.config import load_config as load_experiment_config
from research_os.portfolio import empirical, scicontract, shape
from research_os.portfolio.contracts import AnalysisSpec, ContractError
from research_os.portfolio.models import (
    ContractState,
    ExecutionShapeVerdict,
    ExperimentRole,
    ExperimentState,
    PrimaryOutcome,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_science_campaigns import (
    EXPERIMENTS_YAML,
    _git,
    advance,
    analysis,
    campaign_design,
    commit_manifest,
    context_for,
    jobs,
    manifest,
    outcomes,
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

ANALYST = TEMPLATES["analysis_designer"].identity
DESIGNER = TEMPLATES["experiment_designer"].identity
REF = "synthetic.draw@1"


# ------------------------------------------------------ the bounded repo --
def execution_block(*, campaign: bool = True) -> str:
    """What one execution of the synthetic draw really holds.

    ``draw.py`` reads one seed from ``RESEARCH_OS_SEED_0`` and writes one
    record per ``x`` of a plan whose schema allows five: five records, five
    distinct ``x``, one distinct ``seed``. Another seed is a new draw;
    another plan is another grid.
    """

    renew_grid = "\n          across_units:\n            - {varies: plan, fields: [x]}"
    renew_seed = (
        "\n          across_units:\n            - {varies: seeds, fields: [seed]}"
    )
    return f"""\
    execution:
      records:
        - {{observable: points, max: 5}}
      inputs:
        - name: grid
          observable: points
          max: 5
          fields: [x]{renew_grid if campaign else ""}
        - name: draws
          observable: points
          max: 1
          fields: [seed]{renew_seed if campaign else ""}
          description: one noise seed per execution
"""


def bounded_manifest(*, campaign: str | None = "default", **kwargs: Any) -> str:
    text = manifest(campaign=campaign, **kwargs)
    return text.replace(
        "    resources:\n",
        execution_block(campaign=campaign is not None) + "    resources:\n",
        1,
    )


@pytest.fixture
def bounded_repo(science_repo: Path) -> Path:
    commit_manifest(science_repo, bounded_manifest())
    return science_repo


def commands_for(tmp_path: Path, project: str = "proj") -> dict[str, Any]:
    path = tmp_path / "experiments.yaml"
    path.write_text(EXPERIMENTS_YAML.format(project=project), encoding="utf-8")
    return dict(load_experiment_config(path).projects[project].commands)


def loaded_from(text: str, *, commit: str = "a" * 40) -> capabilities.LoadedManifest:
    import hashlib

    raw = text.encode("utf-8")
    return capabilities.LoadedManifest(
        manifest=capabilities.parse_manifest(raw),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        commit=commit,
    )


BOUNDS = capabilities.HumanBounds(
    max_execution_seconds=1800, max_campaign_units=6, max_campaign_seconds=7200
)


def planning_here(
    tmp_path: Path,
    text: str | None = None,
    *,
    bounds: capabilities.HumanBounds = BOUNDS,
    commit: str = "a" * 40,
) -> shape.Planning:
    loaded = loaded_from(text or bounded_manifest(), commit=commit)
    commands = commands_for(tmp_path)
    envelope = capabilities.execution_envelope(loaded, commands, bounds)
    assert envelope is not None
    return shape.Planning(envelope=envelope, loaded=loaded, commands=commands)


# ------------------------------------------------------------ proposals --
def proposal(
    *,
    rule: str = "fixed_single_execution",
    records: int = 5,
    x: int = 5,
    seeds: int = 1,
    execution_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The draw analysis, with the support and the execution shape varied."""

    answer = analysis(stopping_rule=rule, min_records=records)
    answer["support"] = [
        {
            "observable": "points",
            "min_records": records,
            "min_distinct": {"x": x, "seed": seeds},
        }
    ]
    if execution_shape is not None:
        answer["execution_shape"] = execution_shape
    return answer


def spec(**kwargs: Any) -> AnalysisSpec:
    return AnalysisSpec.model_validate(proposal(**kwargs))


#: One execution cannot hold this -- ten records, two seeds -- and a campaign
#: of two seed-differing units can.
NEEDS_TWO = {"records": 10, "seeds": 2}
CAMPAIGN_SHAPE = {
    "capability": REF,
    "units": 2,
    "unit_varies": ["seeds"],
    "rationale": "one execution draws one seed; two units draw two",
}
REVISED = proposal(rule="fixed_campaign", execution_shape=CAMPAIGN_SHAPE, **NEEDS_TWO)
FIRST = proposal(**NEEDS_TWO)
SINGLE_DESIGN = {
    key: value for key, value in campaign_design([]).items() if key != "campaign"
}


class RevisingRouter(ScriptedRouter):
    """The analysis author answers ``first``, and ``revised`` once shown a refusal."""

    first: dict[str, Any]
    revised: dict[str, Any] | None

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if request.prompt_version == ANALYST:
            shown = "REFUSED ANALYSIS:" in request.prompt
            self.answers_by_prompt[ANALYST] = (
                self.revised if shown and self.revised is not None else self.first
            )
        return super().complete(request)


def revising_router(
    runtime_db: Database,
    *,
    first: dict[str, Any],
    revised: dict[str, Any] | None = None,
    design: dict[str, Any] | None = None,
) -> RevisingRouter:
    router = RevisingRouter(
        answers={},
        answers_by_prompt={
            ANALYST: first,
            DESIGNER: design or campaign_design([unit(7), unit(8)]),
        },
        store=RuntimeStore(runtime_db),
    )
    router.first = first
    router.revised = revised
    return router


def analyst_requests(router: ScriptedRouter) -> list[Any]:
    return router.requests_for_prompt(ANALYST)


def contract_of(portfolio: PortfolioStore, context: Any) -> Any:
    return portfolio.live_contract(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )


def drafts_of(portfolio: PortfolioStore, context: Any) -> tuple[Any, ...]:
    return portfolio.analysis_drafts(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
    )


def envelope_now(context: Any) -> capabilities.ExecutionEnvelope:
    loaded, error = empirical.capability_manifest(context)
    assert error is None
    planning = shape.planning_for(
        loaded, empirical.declared_commands(context.project_id), context.config.bounds
    )
    assert planning is not None
    return planning.envelope


def stored_analysis(context: Any, contract: Any) -> dict[str, Any]:
    return json.loads(context.artifacts.get_text(contract.analysis_artifact_id))


# ===================================================== the declaration --
def test_execution_bounds_are_part_of_the_declaration_and_checked_when_read() -> None:
    (capability,) = capabilities.parse_manifest(
        bounded_manifest().encode()
    ).capabilities
    assert capability.execution is not None
    assert [item.name for item in capability.execution.inputs] == ["grid", "draws"]
    assert capability.execution.records[0].max == 5

    broken = {
        "an undeclared observable": (
            "observable: points, max: 5",
            "observable: nope, max: 5",
        ),
        "an undeclared field": ("fields: [seed]", "fields: [noise]"),
        "a field bounded twice": ("fields: [seed]", "fields: [x]"),
        "a renewal by an undeclared difference": (
            "{varies: seeds, fields: [seed]}",
            "{varies: solver, fields: [seed]}",
        ),
        "a renewal of another input's field": (
            "{varies: seeds, fields: [seed]}",
            "{varies: seeds, fields: [x]}",
        ),
    }
    for what, (old, new) in broken.items():
        text = bounded_manifest().replace(old, new, 1)
        assert text != bounded_manifest(), what
        with pytest.raises(CapabilityError):
            capabilities.parse_manifest(text.encode())
    # Renewal across units is a campaign's; a capability with none states none.
    with pytest.raises(CapabilityError, match="declares no campaign"):
        capabilities.parse_manifest(
            manifest(campaign=None)
            .replace(
                "    resources:\n", execution_block(campaign=True) + "    resources:\n"
            )
            .encode()
        )
    capabilities.parse_manifest(bounded_manifest(campaign=None).encode())


def test_no_digest_frozen_before_the_envelope_existed_moves() -> None:
    """The declaration, analysis and capability digests, pinned at 66d5704.

    Computed at the base commit, where neither ``execution`` nor
    ``execution_shape`` existed; absent, both are omitted from the digest.
    ``rcap-v1:84ccf7cc...`` is the digest the second final qualification
    recorded for ``cg.cells@1`` at CG 5eb3923 (its evidence, ``04-``), which
    ``tests/test_capability_planning_regression.py`` re-derives.
    """

    def digest(text: str) -> str:
        (capability,) = capabilities.parse_manifest(text.encode()).capabilities
        return capabilities.capability_digest(capability)

    assert digest(manifest()) == (
        "rcap-v1:26c3d23f4137d107774c3ba4b6c6af3a45f2f94a88a0467bcb3f499a9d4a0ab5"
    )
    assert digest(manifest(campaign=None)) == (
        "rcap-v1:7c7f2e58c189f25e7e4f3e87171d4c88c0fffb30b32efcc7c98c8035496d4258"
    )
    assert scicontract.analysis_digest(
        AnalysisSpec.model_validate(analysis(stopping_rule="fixed_campaign"))
    ) == (
        "panalysis-v1:904c0a8fff582df5c3947f700dc421ce6c7d7e7e099000f4babad5d138553ee7"
    )
    assert scicontract.analysis_digest(
        AnalysisSpec.model_validate(analysis(stopping_rule="fixed_single_execution"))
    ) == (
        "panalysis-v1:8de755c0d619eb299fb3dbcf1d3ebe40869ea47a2d52a53f266a632d137e852a"
    )
    # Stated, both are content, and move their digests.
    assert digest(bounded_manifest()) != digest(manifest())
    assert scicontract.analysis_digest(
        AnalysisSpec.model_validate(REVISED)
    ) != scicontract.analysis_digest(
        AnalysisSpec.model_validate(proposal(rule="fixed_campaign", **NEEDS_TWO))
    )


# ======================================================== the envelope --
def test_the_envelope_is_derived_from_committed_sources_and_human_bounds_only(
    tmp_path: Path,
) -> None:
    loaded = loaded_from(bounded_manifest())
    commands = commands_for(tmp_path)
    envelope = capabilities.execution_envelope(loaded, commands, BOUNDS)
    assert envelope is not None
    (item,) = envelope.capabilities
    assert item.ref == REF and item.usable
    # The unit ceiling is the command's (120 s) under the person's 1800 s,
    # and 7200 s of campaign time fits 60 of them: the capability's 6 bind.
    assert item.seconds == 120 and item.units == 6
    assert item.records_max("points") == 5
    assert [bound.max for bound in item.inputs_of("points")] == [5, 1]
    record = envelope.record()
    assert record["schema"] == capabilities.ENVELOPE_SCHEMA
    assert record["commit"] == "a" * 40
    assert record["bounds"] == BOUNDS.record()
    # Same inputs, same envelope; any input changed, another one.
    again = capabilities.execution_envelope(
        loaded_from(bounded_manifest()), commands, BOUNDS
    )
    assert again is not None and again.digest == envelope.digest
    others = [
        capabilities.execution_envelope(
            loaded_from(bounded_manifest(), commit="b" * 40), commands, BOUNDS
        ),
        capabilities.execution_envelope(
            loaded_from(bounded_manifest().replace("max: 5}", "max: 4}", 1)),
            commands,
            BOUNDS,
        ),
        capabilities.execution_envelope(
            loaded, commands, capabilities.HumanBounds(1800, 5, 7200)
        ),
        capabilities.execution_envelope(
            loaded, commands, capabilities.HumanBounds(1800, 6, 360)
        ),
        capabilities.execution_envelope(
            loaded,
            {"draw": commands["draw"].model_copy(update={"timeout_seconds": 60})},
            BOUNDS,
        ),
    ]
    digests = {item.digest for item in others if item is not None}
    assert len(digests) == len(others) and envelope.digest not in digests
    # The human-set campaign time decides how many units there are.
    tight = capabilities.execution_envelope(
        loaded, commands, capabilities.HumanBounds(1800, 6, 360)
    )
    assert tight is not None and tight.capabilities[0].units == 3
    # No manifest, no envelope.
    assert capabilities.execution_envelope(None, commands, BOUNDS) is None


def test_the_envelope_states_its_bounds_in_numbers_before_anything_is_chosen(
    tmp_path: Path,
) -> None:
    planning = planning_here(tmp_path)
    text = "\n".join(planning.envelope.lines())
    assert planning.envelope.digest in text
    assert "ONE EXECUTION may run up to 120s and holds at most:" in text
    assert "5 records of observable points" in text
    assert "1 draws of observable points, so at most 1 distinct values of seed" in text
    assert "A CAMPAIGN (stopping_rule fixed_campaign) may have 2 to 6 units" in text
    assert "a unit differing in seeds renews draws: seed" in text
    assert "a unit differing in plan renews grid: x" in text
    assert "nothing you write can enlarge it" in text
    # And the typed catalogue every role reads says so too.
    catalogue = "\n".join(
        capabilities.catalogue_lines(planning.loaded, planning.commands)
    )
    assert "one execution holds at most: 5 records of points" in catalogue
    # A capability that declares no campaign says it cannot have one.
    plain = planning_here(tmp_path, bounded_manifest(campaign=None))
    assert "A CAMPAIGN is not available" in "\n".join(plain.envelope.lines())


# ================================================== the check, by rule --
def test_a_single_execution_that_one_execution_holds_is_valid(tmp_path: Path) -> None:
    """A: nothing in the support exceeds one execution."""

    checked = planning_here(tmp_path).check(spec())
    assert checked.verdict is ExecutionShapeVerdict.VALID_SINGLE_EXECUTION
    assert checked.valid and not checked.refused and checked.single_fits
    assert checked.capability == REF and checked.problems == ()
    assert checked.record()["envelope_digest"] == checked.envelope_digest


def test_a_single_execution_one_execution_cannot_hold_is_a_mismatch(
    tmp_path: Path,
) -> None:
    """B, the class of the qualification failure: a campaign could, one run cannot."""

    planning = planning_here(tmp_path)
    checked = planning.check(spec(**NEEDS_TWO))
    assert checked.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert checked.refused and not checked.single_fits and checked.campaign_fits
    assert checked.min_units == 2 and checked.max_units == 6
    problems = " ".join(checked.problems)
    assert (
        "records of points: needs 10" in problems
        and "one execution holds 5" in problems
    )
    assert "distinct seed of points: needs 2" in problems
    assert "fixed_campaign with 2 to 6 units" in checked.suggestion
    assert "does not convert it" in checked.suggestion
    needed = {
        (item["bound"], item["field"]): (
            item["needed"],
            item["one_execution"],
            item["campaign"],
        )
        for item in checked.record()["demands"]
    }
    assert needed[("records", None)] == (10, 5, 30)
    assert needed[("draws", "seed")] == (2, 1, 6)
    # The campaign form of the same requirement is valid -- the requirement
    # did not change, only how it is executed.
    revised = planning.check(AnalysisSpec.model_validate(REVISED))
    assert revised.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN
    assert revised.shape_units == 2 and revised.shape_varies == ("seeds",)
    # Left to the design, the smallest campaign is still named.
    open_ended = planning.check(spec(rule="fixed_campaign", **NEEDS_TWO))
    assert open_ended.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN
    assert open_ended.min_units == 2 and open_ended.shape_units is None


def test_a_requirement_no_allowed_shape_can_hold_is_capability_limited(
    tmp_path: Path,
) -> None:
    """C: seven independent seeds, and the most any campaign here draws is six."""

    planning = planning_here(tmp_path)
    for rule in ("fixed_single_execution", "fixed_campaign"):
        checked = planning.check(spec(rule=rule, records=10, seeds=7))
        assert checked.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED, rule
        assert not checked.single_fits and not checked.campaign_fits
        assert checked.min_units is None
        assert "not converted, weakened or frozen" in checked.suggestion
    # The human-set bounds are part of what is allowed: room for one unit is
    # no campaign, and the six seeds a campaign could draw become one.
    tight = planning_here(tmp_path, bounds=capabilities.HumanBounds(1800, 6, 200))
    checked = tight.check(spec(**NEEDS_TWO))
    assert checked.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED
    assert not checked.campaign_supported and checked.max_units == 1
    # And a capability that declares no campaign has none to offer.
    plain = planning_here(tmp_path, bounded_manifest(campaign=None))
    assert plain.check(spec(**NEEDS_TWO)).verdict is (
        ExecutionShapeVerdict.CAPABILITY_LIMITED
    )
    # A campaign rule it cannot have, over a sample one execution holds, is
    # a mismatch the other way round.
    other_way = plain.check(spec(rule="fixed_campaign"))
    assert other_way.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert "declares no campaign support" in " ".join(other_way.problems)
    assert "one execution of synthetic.draw@1" in other_way.suggestion


def test_a_claimed_capacity_beyond_the_committed_envelope_is_refused(
    tmp_path: Path,
) -> None:
    """D: the model may read the envelope; nothing it writes enlarges it."""

    planning = planning_here(tmp_path)
    before = planning.envelope.digest
    # Its requirement fits; its claim about the laboratory does not, and the
    # claim is refused rather than believed.
    for claim in ({"seed": 3}, {"draws": 3}, {"records": 50}, {"x": 9}):
        checked = planning.check(
            spec(
                execution_shape={"capability": REF, "units": 1, "per_execution": claim}
            )
        )
        assert checked.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH, claim
        assert "the committed envelope says" in " ".join(checked.problems), claim
    # A true claim is fine.
    assert planning.check(
        spec(execution_shape={"units": 1, "per_execution": {"seed": 1, "records": 5}})
    ).valid
    # Nor may it claim a larger campaign than the person allows.
    oversized = planning.check(
        AnalysisSpec.model_validate({**REVISED, "execution_shape": {"units": 12}})
    )
    assert oversized.refused
    assert "fixes 12 units and at most 6 are allowed" in " ".join(oversized.problems)
    # Or plan on a capability its observables do not bind to.
    elsewhere = planning.check(
        AnalysisSpec.model_validate(
            {
                **REVISED,
                "execution_shape": {**CAMPAIGN_SHAPE, "capability": "other.lab@1"},
            }
        )
    )
    assert elsewhere.refused
    # And checking changed nothing about the envelope.
    assert planning.envelope.digest == before


def test_campaign_units_differing_in_what_is_not_attested_are_refused(
    tmp_path: Path,
) -> None:
    """E: the envelope's units may differ in seeds or plan, and nothing else."""

    planning = planning_here(tmp_path)
    for token in ("solver", "implementation", "threads"):
        checked = planning.check(
            AnalysisSpec.model_validate(
                {
                    **REVISED,
                    "execution_shape": {**CAMPAIGN_SHAPE, "unit_varies": [token]},
                }
            )
        )
        assert checked.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH, token
        assert "does not attest campaign units may differ in" in " ".join(
            checked.problems
        )


def test_what_a_unit_difference_does_not_renew_does_not_grow_with_units(
    tmp_path: Path,
) -> None:
    """Six seed-differing units draw six seeds and the same five x -- never six x."""

    planning = planning_here(tmp_path)
    needs_six_x = {"rule": "fixed_campaign", "records": 10, "x": 6, "seeds": 2}
    seeds_only = planning.check(
        spec(**needs_six_x, execution_shape={"units": 6, "unit_varies": ["seeds"]})
    )
    assert seeds_only.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert "distinct x of points: needs 6" in " ".join(seeds_only.problems)
    # Units that also differ in plan draw new grids, and hold it.
    both = planning.check(
        spec(
            **needs_six_x,
            execution_shape={"units": 2, "unit_varies": ["seeds", "plan"]},
        )
    )
    assert both.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN


def test_records_selected_by_different_values_of_a_bounded_field_need_different_slots(
    tmp_path: Path,
) -> None:
    """Two analyses of two seeds cannot share the one seed an execution draws."""

    answer = proposal(records=3, x=3, seeds=1)
    answer["observables"] = [
        {
            **answer["observables"][0],
            "name": "seven",
            "include": [{"field": "seed", "comparator": "==", "value": 7}],
        },
        {
            **answer["observables"][0],
            "name": "eight",
            "include": [{"field": "seed", "comparator": "==", "value": 8}],
        },
    ]
    answer["reductions"] = [{**answer["reductions"][0], "observable": "seven"}]
    answer["support"] = [
        {"observable": "seven", "min_records": 3, "min_distinct": {"x": 3}},
        {"observable": "eight", "min_records": 3, "min_distinct": {"x": 3}},
    ]
    checked = planning_here(tmp_path).check(AnalysisSpec.model_validate(answer))
    assert checked.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    problems = " ".join(checked.problems)
    assert "draws (jointly) of points: needs 2" in problems
    assert "records of points: needs 6" in problems
    assert checked.min_units == 2


def test_a_campaign_cannot_read_what_its_capability_cannot_combine(
    tmp_path: Path,
) -> None:
    text = bounded_manifest().replace("        - {observable: count, rule: sum}\n", "")
    planning = planning_here(tmp_path, text)
    answer = dict(REVISED)
    answer["observables"] = [
        *REVISED["observables"],
        {
            "name": "counted",
            "source": "results/draw.json",
            "kind": "scalar",
            "path": "summary.count",
        },
    ]
    checked = planning.check(AnalysisSpec.model_validate(answer))
    assert checked.refused
    assert "cannot combine count" in " ".join(checked.problems)


def test_nothing_to_check_is_unresolved_and_left_to_capability_resolution(
    tmp_path: Path,
) -> None:
    answer = proposal()
    answer["observables"] = [
        {**answer["observables"][0], "fields": ["x", "y", "noise"]}
    ]
    checked = planning_here(tmp_path).check(AnalysisSpec.model_validate(answer))
    assert checked.verdict is ExecutionShapeVerdict.UNRESOLVED
    assert not checked.refused and not checked.valid
    unanalysable = AnalysisSpec(
        analysable=False, unanalysable_reason="nothing observes it"
    )
    assert planning_here(tmp_path).check(unanalysable).verdict is (
        ExecutionShapeVerdict.UNRESOLVED
    )


def test_an_execution_shape_that_contradicts_its_own_stopping_rule_is_malformed() -> (
    None
):
    for bad in (
        proposal(execution_shape={"units": 2}),
        proposal(execution_shape={"unit_varies": ["seeds"]}),
        proposal(rule="fixed_campaign", execution_shape={"units": 1}),
    ):
        with pytest.raises(ContractError):
            AnalysisSpec.model_validate(bad).check()
    with pytest.raises(ContractError):
        AnalysisSpec.model_validate(
            {
                "analysable": False,
                "unanalysable_reason": "no observable",
                "execution_shape": {"units": 1},
            }
        ).check()


# ======================================================= through the route --
def test_an_analysis_one_execution_holds_is_frozen_bound_to_the_envelope_it_was_shown(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """A: shown the envelope, proposes one execution, checked, frozen, measured."""

    router = revising_router(runtime_db, first=proposal(), design=SINGLE_DESIGN)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    step = advance(context)
    assert step.ok, step.detail
    envelope = envelope_now(context)
    (asked,) = analyst_requests(router)
    # The envelope reached the author before it chose anything.
    assert "CAPABILITY ENVELOPE:" in asked.prompt
    assert envelope.digest in asked.prompt
    assert "1 draws of observable points" in asked.prompt
    assert "REFUSED ANALYSIS:" not in asked.prompt

    contract = portfolio.require_contract(
        portfolio.list_experiments(idea_id=context.idea_id)[0].contract_id or ""
    )
    assert contract.envelope_digest == envelope.digest
    check = stored_analysis(context, contract)["execution_shape_check"]
    assert check["verdict"] == "VALID_SINGLE_EXECUTION"
    assert check["envelope_digest"] == envelope.digest
    assert check["commit"] == _git(bounded_repo, "rev-parse", "HEAD")
    assert drafts_of(portfolio, context) == ()
    (reading,) = [item for item in outcomes(context) if item.receipt_id]
    assert reading.state is PrimaryOutcome.SUPPORTED


def test_a_single_execution_analysis_one_execution_cannot_hold_is_revised_before_freezing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """B: refused before freezing, shown why, revised to a campaign, only that frozen."""

    router = revising_router(runtime_db, first=FIRST, revised=REVISED)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )

    refused = advance(context)
    assert not refused.ok
    assert refused.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "EXECUTION_SHAPE_MISMATCH" in refused.detail
    assert "nothing was frozen" in refused.detail
    assert refused.model_calls == 1
    assert contract_of(portfolio, context) is None
    assert not portfolio.list_experiments(idea_id=context.idea_id)
    (draft,) = drafts_of(portfolio, context)
    assert draft.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert draft.analysis_digest == scicontract.analysis_digest(
        AnalysisSpec.model_validate(FIRST)
    )
    assert draft.check_record["campaign"]["min_units"] == 2
    assert jobs(runtime_db) == []

    measured = advance(context)
    assert measured.ok, measured.detail
    first_ask, second_ask = analyst_requests(router)
    assert "REFUSED ANALYSIS:" not in first_ask.prompt
    assert "REFUSED ANALYSIS:" in second_ask.prompt
    assert "EXECUTION_SHAPE_MISMATCH" in second_ask.prompt
    assert "the smallest that holds it has 2 units" in second_ask.prompt
    assert '"stopping_rule": "fixed_single_execution"' in second_ask.prompt

    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.state is ExperimentState.INTERPRETED
    contract = portfolio.require_contract(experiment.contract_id or "")
    # Only the revision is frozen, and it says what it revises.
    assert contract.analysis_digest == scicontract.analysis_digest(
        AnalysisSpec.model_validate(REVISED)
    )
    assert contract.analysis_digest != draft.analysis_digest
    stored = stored_analysis(context, contract)
    assert stored["analysis"]["stopping_rule"] == "fixed_campaign"
    assert stored["execution_shape_check"]["verdict"] == "VALID_CAMPAIGN"
    (revises,) = stored["revises"]
    assert revises["draft_id"] == draft.draft_id
    assert revises["changed"] == ["execution_shape", "stopping_rule"]
    # The design proceeded, as a campaign of the frozen shape, and ran.
    (reading,) = [item for item in outcomes(context) if item.receipt_id]
    assert reading.state is PrimaryOutcome.SUPPORTED and reading.unit_count == 2
    assert len(portfolio.unit_receipts(experiment.experiment_id)) == 2


def test_an_analysis_nothing_allowed_can_hold_is_never_frozen_and_not_bought_twice(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """C: CAPABILITY_LIMITED before freezing; asked again only under another envelope."""

    limited = proposal(rule="fixed_campaign", records=10, seeds=7)
    router = revising_router(runtime_db, first=limited, revised=REVISED)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    step = advance(context)
    assert not step.ok and step.failure_class is FailureClass.CAPABILITY_DENIED
    assert "CAPABILITY_LIMITED" in step.detail
    assert contract_of(portfolio, context) is None
    (draft,) = drafts_of(portfolio, context)
    assert draft.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED

    again = advance(context)
    assert not again.ok and again.failure_class is FailureClass.CAPABILITY_DENIED
    assert "still CAPABILITY_LIMITED" in again.detail
    assert again.model_calls == 0 and again.cost_usd == "0"
    assert len(analyst_requests(router)) == 1, "the same refusal is not bought twice"

    # A person's changed bounds are another envelope, and another question.
    context.config = context.config.with_overrides({"max_campaign_units": 5})
    asked = advance(context)
    assert not asked.ok and len(analyst_requests(router)) == 2
    assert contract_of(portfolio, context) is None
    assert jobs(runtime_db) == []


def test_a_claim_of_more_capacity_is_refused_before_freezing_on_the_route(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """D, on the route: a fitting requirement with a false capacity is not frozen."""

    claiming = proposal(execution_shape={"units": 1, "per_execution": {"seed": 12}})
    router = revising_router(runtime_db, first=claiming, design=SINGLE_DESIGN)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    step = advance(context)
    assert not step.ok
    assert "claims one execution holds 12 seed; the committed envelope says 1" in (
        step.detail
    )
    assert contract_of(portfolio, context) is None


def test_units_differing_in_the_unattested_are_refused_before_freezing_on_the_route(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """E, on the route."""

    unattested = {
        **REVISED,
        "execution_shape": {**CAMPAIGN_SHAPE, "unit_varies": ["solver"]},
    }
    router = revising_router(runtime_db, first=unattested)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    step = advance(context)
    assert not step.ok and "does not attest" in step.detail
    assert contract_of(portfolio, context) is None


def test_a_frozen_analysis_and_its_envelope_binding_never_change(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """F: the analysis half and the binding are frozen, in the row and in the bytes."""

    router = revising_router(runtime_db, first=FIRST, revised=REVISED)
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    advance(context)
    assert advance(context).ok
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    contract = portfolio.require_contract(experiment.contract_id or "")
    (draft,) = drafts_of(portfolio, context)
    for statement in (
        (
            "update scientific_contracts set envelope_digest = 'renv-v1:other' "
            "where contract_id = %(id)s"
        ),
        "update scientific_contracts set envelope_digest = null where contract_id = %(id)s",
        (
            "update scientific_contracts set analysis_digest = 'panalysis-v1:other' "
            "where contract_id = %(id)s"
        ),
    ):
        with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
            conn.execute(statement, {"id": contract.contract_id})
    for statement in (
        "update analysis_drafts set verdict = 'CAPABILITY_LIMITED' where draft_id = %(id)s",
        "delete from analysis_drafts where draft_id = %(id)s",
    ):
        with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
            conn.execute(statement, {"id": draft.draft_id})
    assert portfolio.require_contract(contract.contract_id) == contract

    # Verification reads the binding back out of the bytes: a row naming
    # another envelope, or bytes recording a refused verdict, is refused.
    with pytest.raises(scicontract.ContractIntegrityError, match="checked against"):
        scicontract.verify(
            context.artifacts,
            contract.model_copy(update={"envelope_digest": "renv-v1:" + "0" * 64}),
        )
    document = stored_analysis(context, contract)
    document["execution_shape_check"]["verdict"] = "EXECUTION_SHAPE_MISMATCH"
    forged = context.artifacts.put_text(
        json.dumps(document),
        media_type="application/json",
        role="forged",
        producer="test",
    )
    with pytest.raises(scicontract.ContractIntegrityError, match="not one an analysis"):
        scicontract.verify(
            context.artifacts,
            contract.model_copy(
                update={
                    "analysis_artifact_id": forged.artifact_id,
                    "state": ContractState.ANALYSIS_FROZEN,
                }
            ),
        )


def test_a_retry_or_a_restart_never_freezes_a_refused_draft(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """G: an author that will not revise is bounded, and its drafts stay drafts."""

    router = revising_router(runtime_db, first=FIRST)  # repeats itself
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    first = advance(context)
    assert first.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    second = advance(context)
    assert second.failure_class is FailureClass.POLICY_REFUSED
    assert "refused proposal 2 of 2 under this envelope" in second.detail
    assert second.model_calls == 1
    third = advance(context)
    assert third.failure_class is FailureClass.POLICY_REFUSED
    assert third.model_calls == 0 and len(analyst_requests(router)) == 2
    assert contract_of(portfolio, context) is None
    drafts = drafts_of(portfolio, context)
    assert [item.verdict for item in drafts] == [
        ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    ] * 2
    assert len({item.analysis_digest for item in drafts}) == 1

    # Whatever calls the store: the database refuses to freeze the draft
    # under the envelope that refused it, or under none.
    draft = drafts[0]
    version = portfolio.require_version(context.idea_id)

    def freeze(envelope_digest: str | None) -> Any:
        return portfolio.create_contract(
            project_id=runtime_project,
            idea_id=context.idea_id,
            idea_version=1,
            role=ExperimentRole.PRIMARY,
            hypothesis_digest=version.content_digest,
            analysable=True,
            analysis_digest=draft.analysis_digest,
            analysis_artifact_id=draft.artifact_id,
            envelope_digest=envelope_digest,
        )

    for envelope_digest in (draft.envelope_digest, None):
        with pytest.raises(RuntimeDatabaseError, match="refused before freezing"):
            freeze(envelope_digest)
    # Nor will the route's own freezing function take a refused check.
    checked = shape.planning_for(
        empirical.capability_manifest(context)[0],
        empirical.declared_commands(runtime_project),
        context.config.bounds,
    )
    assert checked is not None
    refusal = checked.check(AnalysisSpec.model_validate(FIRST))
    assert refusal.refused
    with pytest.raises(empirical.EmpiricalError, match="never frozen"):
        empirical._freeze_analysis(
            context,
            version,
            role=ExperimentRole.PRIMARY,
            spec=AnalysisSpec.model_validate(FIRST),
            provenance={},
            analysis_prompt=ANALYST,
            analysis_call_id=None,
            checked=refusal,
        )
    assert contract_of(portfolio, context) is None
    # And once a contract is live there is nothing left to draft.
    router.first = REVISED
    context.config = context.config.with_overrides({"max_campaign_units": 5})
    assert advance(context).ok
    with pytest.raises(RuntimeDatabaseError, match="already frozen"):
        portfolio.record_analysis_draft(
            project_id=runtime_project,
            idea_id=context.idea_id,
            idea_version=1,
            role=ExperimentRole.PRIMARY,
            verdict=ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH,
            analysis_digest=draft.analysis_digest,
            artifact_id=draft.artifact_id,
            envelope_digest=draft.envelope_digest,
            code_commit=draft.code_commit,
            check_record=draft.check_record,
        )


@pytest.mark.parametrize("change", ["commit", "declaration", "bounds"])
def test_an_envelope_that_changed_invalidates_an_unread_binding(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
    change: str,
) -> None:
    """H: checked against one envelope, never designed under another."""

    router = revising_router(
        runtime_db,
        first=REVISED,
        design={"testable": True, "command": "draw"},  # malformed: no plan
    )
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    step = advance(context)
    assert not step.ok and step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    held = contract_of(portfolio, context)
    assert held is not None and held.state is ContractState.ANALYSIS_FROZEN
    assert held.envelope_digest == envelope_now(context).digest

    if change == "commit":
        (bounded_repo / "NOTES.md").write_text(
            "an unrelated commit\n", encoding="utf-8"
        )
        _git(bounded_repo, "add", "NOTES.md")
        _git(bounded_repo, "commit", "-qm", "notes")
    elif change == "declaration":
        commit_manifest(
            bounded_repo,
            bounded_manifest().replace(
                "one noise seed per execution", "one noise seed for each execution"
            ),
        )
    else:
        context.config = context.config.with_overrides({"max_campaign_units": 4})
    current = envelope_now(context)
    assert current.digest != held.envelope_digest

    router.answers_by_prompt[DESIGNER] = campaign_design([unit(7), unit(8)])
    measured = advance(context)
    assert measured.ok, measured.detail
    retired = portfolio.require_contract(held.contract_id)
    assert retired.state is ContractState.SUPERSEDED
    assert held.envelope_digest in (retired.detail or "")
    assert current.digest in (retired.detail or "")
    (experiment,) = [
        item
        for item in portfolio.list_experiments(idea_id=context.idea_id)
        if item.state is ExperimentState.INTERPRETED
    ]
    successor = portfolio.require_contract(experiment.contract_id or "")
    assert successor.contract_id != held.contract_id
    assert successor.envelope_digest == current.digest
    assert len(analyst_requests(router)) == 2, "checked again, against the new envelope"


def test_a_replication_inherits_the_frozen_shape_and_is_checked_against_the_envelope(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """An inherited analysis is not revisable: an envelope that cannot hold it refuses it."""

    router = revising_router(runtime_db, first=REVISED)
    router.answers_by_prompt[TEMPLATES["replication_designer"].identity] = (
        campaign_design([unit(17), unit(18)], variation="seeds")
    )
    context = context_for(
        portfolio, runtime_db, runtime_project, bounded_repo, tmp_path, router
    )
    assert advance(context).ok
    primary = contract_of(portfolio, context)
    assert primary is not None

    original = context.config
    context.config = original.with_overrides({"max_campaign_seconds": 200})
    refused = advance(context, ExperimentRole.REPLICATION)
    assert not refused.ok and refused.failure_class is FailureClass.CAPABILITY_DENIED
    assert "the primary's frozen analysis cannot be executed as a replication" in (
        refused.detail
    )
    assert (
        portfolio.live_contract(
            idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
        )
        is None
    )

    context.config = original
    replicated = advance(context, ExperimentRole.REPLICATION)
    assert replicated.ok, replicated.detail
    inherited = portfolio.live_contract(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
    )
    assert inherited is not None
    assert inherited.analysis_digest == primary.analysis_digest
    assert inherited.envelope_digest == envelope_now(context).digest
    check = stored_analysis(context, inherited)["execution_shape_check"]
    assert check["verdict"] == "VALID_CAMPAIGN"


# ======================================================== the design side --
def _held_campaign(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    frozen: dict[str, Any],
    design: dict[str, Any],
) -> tuple[Any, ScriptedRouter]:
    router = revising_router(runtime_db, first=frozen, design=design)
    context = context_for(
        portfolio, runtime_db, runtime_project, repo, tmp_path, router
    )
    return context, router


def test_the_design_realises_the_frozen_execution_shape_and_nothing_else(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """The designer does not reinterpret the frozen shape: units, differences, size."""

    context, router = _held_campaign(
        portfolio,
        runtime_db,
        runtime_project,
        bounded_repo,
        tmp_path,
        frozen=REVISED,
        design=campaign_design([unit(7), unit(8), unit(9)]),
    )
    step = advance(context)
    assert not step.ok and step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "fixes a campaign of 2 units and the design has 3" in step.detail
    (designer,) = router.requests_for_prompt(DESIGNER)
    assert (
        "a campaign of synthetic.draw@1, of exactly 2 units, whose units differ only in seeds"
        in (designer.prompt)
    )
    contract = contract_of(portfolio, context)
    assert contract is not None and contract.state is ContractState.ANALYSIS_FROZEN

    router.answers_by_prompt[DESIGNER] = campaign_design(
        [unit(7, xs=[0, 1, 2, 3, 4]), unit(8, xs=[5, 6, 7, 8, 9])]
    )
    other = advance(context)
    assert not other.ok
    assert "differ in plan; the frozen analysis lets them differ only in seeds" in (
        other.detail
    )

    router.answers_by_prompt[DESIGNER] = campaign_design([unit(7), unit(8)])
    assert advance(context).ok


def test_a_campaign_too_small_for_the_frozen_support_is_refused_before_it_runs(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    bounded_repo: Path,
    tmp_path: Path,
) -> None:
    """Left to the design, the size is still the envelope's: fifteen records need three."""

    frozen = proposal(rule="fixed_campaign", records=15, seeds=3)
    context, router = _held_campaign(
        portfolio,
        runtime_db,
        runtime_project,
        bounded_repo,
        tmp_path,
        frozen=frozen,
        design=campaign_design([unit(7), unit(8)]),
    )
    step = advance(context)
    assert not step.ok
    assert "this campaign cannot hold the frozen support" in step.detail
    (designer,) = router.requests_for_prompt(DESIGNER)
    assert "the support needs a campaign of at least 3 units" in designer.prompt
    router.answers_by_prompt[DESIGNER] = campaign_design([unit(7), unit(8), unit(9)])
    measured = advance(context)
    assert measured.ok, measured.detail
    assert jobs(runtime_db) and all(
        row["status"] == "COMPLETED" for row in jobs(runtime_db)
    )


# ============================================================ the harness --
def test_every_capability_planning_mutant_still_applies() -> None:
    """``tests/capability_planning_mutations.py`` cannot rot silently."""

    import ast

    from tests.capability_planning_mutations import MUTANTS

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
