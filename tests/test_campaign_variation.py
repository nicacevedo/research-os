"""Campaign units vary only as the frozen analysis allows: the whole difference, never part.

``docs/SCIENCE_EXECUTION.md`` §3a is the specification. An independent review
reproduced a campaign whose frozen analysis let its units differ only in the
seed, and whose design changed the seed *and* the slope inside the composed
plan: the designer-side check filtered the slope out before comparing, the
compiler accepted the pair because one of its differences (the seed) was
allowed, and the campaign ran and was read. The rule these tests hold, for
every pair of units a campaign compiles::

    actual differences  <=  allowed differences      (never merely intersecting)
    actual differences  !=  {}

with ``actual`` derived by code from the units' frozen specifications
(``campaign.configuration``) and ``allowed`` from the frozen analysis and the
capability declaration (``campaign.allowed_variation``). The route-level
reproduction, the retries, the recovery and the replication are in
``tests/test_campaign_variation_reproduction.py``.

These are the compiler's own tests, called directly -- the public API a caller
could reach without the designer-side check in front of it (L). By letter:

A. allowed {seeds}, actual {seeds}                     -> compiled
B. allowed {seeds}, actual {slope}                     -> refused
C. allowed {seeds}, actual {seeds, slope}              -> refused (the review's)
D. allowed {seeds, slope}, actual {seeds}              -> compiled (allowed is not required)
E. allowed {seeds, slope}, actual {seeds, slope}       -> compiled
F. identical units                                     -> refused
G. allowed {seeds}, seeds + operational metadata       -> compiled
H. allowed {seeds}, seeds + a slope nested in the plan -> refused
I. three units, one of them forbidden                  -> the whole campaign refused
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import pytest

from research_os import capability as capabilities
from research_os.experiment.config import load_config as load_experiment_config
from research_os.portfolio import campaign as campaigns
from research_os.portfolio import empirical
from research_os.portfolio.contracts import AnalysisSpec, DesignSpecification
from research_os.runtime.executors import spec_digest
from tests.test_science_campaigns import analysis as draw_analysis

SOURCE = "results/sweep.json"

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  proj:
    default_executor: local
    commands:
      sweep:
        name: sweep
        description: A seeded linear response over a composed grid, at a slope.
        argv: ["python3", "sweep.py", "--plan", "{plan}", "--slope", "{slope}", "--out", "{out}"]
        parameters:
          - name: plan
            type: generated
            required: true
            max_bytes: 4096
            input_schema:
              type: object
              additionalProperties: false
              required: ["xs"]
              properties:
                slope: {type: number, minimum: -10, maximum: 10}
                xs:
                  type: array
                  minItems: 1
                  maxItems: 5
                  items: {type: number, minimum: -100, maximum: 100}
          - name: slope
            type: number
            required: true
            minimum: -10
            maximum: 10
          - name: out
            type: path
            required: true
        outputs: []
        timeout_seconds: 120
        checks: []
"""


def manifest(unit_varies: str = "[seeds, plan, slope]") -> str:
    return f"""\
schema: research-os-capabilities-v1
capabilities:
  - id: synthetic.sweep
    version: 1
    title: A seeded linear response at a declared slope, one record per x
    command: sweep
    parameters:
      - {{name: plan, description: the composed grid}}
      - {{name: slope, description: the slope of the response}}
      - {{name: out, description: where the result is written}}
    result:
      artifact_parameter: out
      format: json
      schema:
        type: object
        required: [records]
        properties:
          records:
            type: array
            items:
              type: object
              required: [x, y, seed]
              properties:
                x: {{type: number}}
                y: {{type: number}}
                seed: {{type: integer}}
    observables:
      - name: points
        kind: records
        path: records
        fields:
          - {{name: x, type: number}}
          - {{name: y, type: number}}
          - {{name: seed, type: integer}}
    determinism: seeded
    replication:
      perturbations:
        - kind: seeds
          description: RESEARCH_OS_SEED_0 seeds the noise every y is drawn with
        - kind: parameter
          name: plan
          description: the plan chooses every x
        - kind: parameter
          name: slope
          description: the slope multiplies every x
    campaign:
      max_units: 6
      unit_varies: {unit_varies}
      aggregation:
        - {{observable: points, rule: concatenate, identity: [x, seed]}}
    resources:
      timeout_seconds: 60
"""


def capability(unit_varies: str = "[seeds, plan, slope]") -> capabilities.Capability:
    (declared,) = capabilities.parse_manifest(
        manifest(unit_varies).encode("utf-8")
    ).capabilities
    return declared


@pytest.fixture
def commands(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "experiments.yaml"
    path.write_text(EXPERIMENTS_YAML, encoding="utf-8")
    return dict(load_experiment_config(path).projects["proj"].commands)


def frozen(unit_varies: list[str] | None) -> AnalysisSpec:
    """The draw analysis over the sweep's result; ``unit_varies`` its stated shape."""

    answer = draw_analysis()
    answer["observables"][0]["source"] = SOURCE
    if unit_varies is not None:
        answer["execution_shape"] = {
            "capability": "synthetic.sweep@1",
            "unit_varies": unit_varies,
        }
    return AnalysisSpec.model_validate(answer)


def unit(seed: int, **parameters: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"label": f"seed {seed}", "seeds": [seed]}
    if parameters:
        entry["command_parameters"] = parameters
    return entry


def design(units: list[dict[str, Any]]) -> DesignSpecification:
    return DesignSpecification.model_validate(
        {
            "testable": True,
            "command": "sweep",
            "command_parameters": {
                "plan": {"xs": [0, 1, 2, 3, 4]},
                "slope": 1.0,
                "out": SOURCE,
            },
            "seeds": [7],
            "falsification_criterion": "no positive slope",
            "campaign": {"units": units},
        }
    )


def compiled_units(
    commands: dict[str, Any],
    units: list[dict[str, Any]],
    *,
    workspace: Path = Path("/tmp/ros-campaign-variation"),
    analysis: AnalysisSpec | None = None,
) -> list[campaigns.UnitInput]:
    """Each unit built exactly as ``empirical._design_campaign`` builds it."""

    reading = analysis or frozen(["seeds"])
    built: list[campaigns.UnitInput] = []
    for index, unit_design in enumerate(campaigns.unit_designs(design(units))):
        spec, _rule, inputs = empirical.build_spec(
            unit_design,
            commands=commands,
            workspace=workspace,
            max_seconds=1800,
            required_outputs=reading.sources(),
        )
        built.append(
            campaigns.UnitInput(
                index=index,
                label=units[index].get("label", ""),
                design=unit_design,
                spec=spec,
                frozen_inputs=tuple(inputs),
                spec_digest=spec_digest(spec),
                variation_digest=empirical.variation_digest(spec),
                parameters=dict(empirical.spec_parameters(spec, unit_design)),
            )
        )
    return built


def compile_(
    commands: dict[str, Any],
    units: list[campaigns.UnitInput],
    analysis: AnalysisSpec,
    *,
    declared: capabilities.Capability | None = None,
) -> campaigns.Compiled | campaigns.Refused:
    return campaigns.compile_campaign(
        units,
        analysis=analysis,
        capability=declared or capability(),
        observables=(("points", "points"),),
        max_units=6,
        max_seconds=7200,
        command_argv=commands["sweep"].argv,
    )


def refused(result: campaigns.Compiled | campaigns.Refused) -> str:
    assert isinstance(result, campaigns.Refused), "the campaign compiled"
    assert result.retry and result.state == "CAPABILITY_LIMITED"
    return result.summary()


# ================================================================ A - I --
def test_a_units_differing_only_in_an_allowed_seed_compile(
    commands: dict[str, Any],
) -> None:
    result = compile_(
        commands, compiled_units(commands, [unit(7), unit(8)]), frozen(["seeds"])
    )
    assert isinstance(result, campaigns.Compiled)
    assert result.assignments == (("seeds",),)


def test_b_units_differing_only_in_a_forbidden_slope_are_refused(
    commands: dict[str, Any],
) -> None:
    units = compiled_units(commands, [unit(7), unit(7, slope=2.0)])
    summary = refused(compile_(commands, units, frozen(["seeds"])))
    assert "units 0 and 1 differ in slope" in summary
    assert "lets campaign units differ only in seeds" in summary


def test_c_an_allowed_seed_never_masks_a_forbidden_slope(
    commands: dict[str, Any],
) -> None:
    """The independent review's reproduction, at the compiler: seed AND slope."""

    units = compiled_units(commands, [unit(7), unit(8, slope=2.0)])
    summary = refused(compile_(commands, units, frozen(["seeds"])))
    assert "units 0 and 1 differ in slope" in summary
    assert "does not excuse it" in summary
    # Allowed, the same campaign compiles: it is the rule, not the design.
    assert isinstance(
        compile_(commands, units, frozen(["seeds", "slope"])), campaigns.Compiled
    )


def test_d_allowed_is_not_required(commands: dict[str, Any]) -> None:
    """A v1 analysis marks no difference required: varying fewer is realising it."""

    units = compiled_units(commands, [unit(7), unit(8)])
    result = compile_(commands, units, frozen(["seeds", "slope"]))
    assert isinstance(result, campaigns.Compiled)
    assert result.assignments == (("seeds",),)


def test_e_units_differing_in_everything_allowed_compile(
    commands: dict[str, Any],
) -> None:
    units = compiled_units(commands, [unit(7), unit(8, slope=2.0)])
    result = compile_(commands, units, frozen(["seeds", "slope"]))
    assert isinstance(result, campaigns.Compiled)
    assert result.assignments == (("seeds", "slope"),)


def test_f_units_that_do_not_differ_are_refused(commands: dict[str, Any]) -> None:
    units = compiled_units(commands, [unit(7), unit(7)])
    assert "units 0 and 1 are the same execution" in refused(
        compile_(commands, units, frozen(["seeds"]))
    )
    # Differing only in where they ran and how they are labelled is not
    # differing: distinct units are required, and these are one execution.
    moved = compiled_units(
        commands, [unit(7), unit(8)], workspace=Path("/tmp/ros-variation-elsewhere")
    )[0]
    same = [units[0], dataclasses.replace(moved, index=1, label="elsewhere")]
    assert "units 0 and 1 are the same execution" in refused(
        compile_(commands, same, frozen(["seeds"]))
    )


def test_g_operational_metadata_is_not_a_scientific_difference(
    commands: dict[str, Any],
) -> None:
    """Where a unit ran, its label, name, time limit and resources are not science."""

    first, second = compiled_units(commands, [unit(7), unit(8)])
    elsewhere = compiled_units(
        commands, [unit(7), unit(8)], workspace=Path("/tmp/ros-variation-elsewhere")
    )[1]
    second = dataclasses.replace(
        second,
        label="another label entirely",
        spec=dataclasses.replace(
            elsewhere.spec,
            name="another-name",
            timeout_seconds=17,
            resources={"memory": "8G", "cpus": "2"},
        ),
    )
    assert second.spec.cwd != first.spec.cwd
    result = compile_(commands, [first, second], frozen(["seeds"]))
    assert isinstance(result, campaigns.Compiled), getattr(result, "summary", str)()
    assert result.assignments == (("seeds",),)
    # And the capability's result-location parameter says where the result
    # goes, not what it measures (`result.artifact_parameter`).
    template = commands["sweep"].argv
    moved = [
        "results/elsewhere.json" if token == SOURCE else token
        for token in second.spec.argv
    ]
    assert (
        campaigns.differences(
            campaigns.unit_configuration(
                second, command_argv=template, capability=capability()
            ),
            campaigns.unit_configuration(
                dataclasses.replace(
                    second,
                    spec=dataclasses.replace(second.spec, argv=tuple(moved)),
                    parameters={**second.parameters, "out": "results/elsewhere.json"},
                ),
                command_argv=template,
                capability=capability(),
            ),
        )
        == ()
    )


def test_h_a_forbidden_value_nested_in_the_composed_plan_is_refused(
    commands: dict[str, Any],
) -> None:
    units = compiled_units(
        commands,
        [unit(7), unit(8, plan={"xs": [0, 1, 2, 3, 4], "slope": 3.0})],
    )
    summary = refused(compile_(commands, units, frozen(["seeds"])))
    assert "units 0 and 1 differ in plan (at plan.slope)" in summary
    # Every forbidden difference is reported, not the first: plan and slope.
    both = compiled_units(
        commands,
        [unit(7), unit(8, slope=2.0, plan={"xs": [0, 1, 2, 3, 4], "slope": 3.0})],
    )
    summary = refused(compile_(commands, both, frozen(["seeds"])))
    assert "differ in plan (at plan.slope), slope" in summary


def test_i_one_forbidden_unit_refuses_the_whole_campaign(
    commands: dict[str, Any],
) -> None:
    units = compiled_units(commands, [unit(7), unit(8), unit(9, slope=2.0)])
    result = compile_(commands, units, frozen(["seeds"]))
    summary = refused(result)
    assert isinstance(result, campaigns.Refused)
    assert [item.requirement for item in result.unmet] == ["campaign units"] * 2
    assert "units 0 and 2 differ in slope" in summary
    assert "units 1 and 2 differ in slope" in summary
    assert "units 0 and 1" not in summary


# ====================================================== what "actual" is --
def test_what_the_units_say_about_themselves_is_not_what_they_differ_in(
    commands: dict[str, Any],
) -> None:
    """The specification decides, not the recorded parameters beside it.

    A unit whose recorded parameters are the first unit's, and whose frozen
    argument vector carries another slope, differs in the slope.
    """

    first, second = compiled_units(commands, [unit(7), unit(8, slope=2.0)])
    claimed = dataclasses.replace(second, parameters=dict(first.parameters))
    assert "units 0 and 1 differ in slope" in refused(
        compile_(commands, [first, claimed], frozen(["seeds"]))
    )


@pytest.mark.parametrize(
    ("change", "token"),
    [
        ({"env": {"RESEARCH_OS_SEED_0": "8", "EXTRA": "1"}}, "spec.env"),
        ({"environment": {"kind": "conda", "name": "other"}}, "spec.environment"),
        ({"outputs": ("results/sweep.json", "results/more.json")}, "spec.outputs"),
    ],
)
def test_a_difference_no_declared_input_accounts_for_is_never_allowed(
    commands: dict[str, Any], change: dict[str, Any], token: str
) -> None:
    first, second = compiled_units(commands, [unit(7), unit(8)])
    forged = dataclasses.replace(
        second, spec=dataclasses.replace(second.spec, **change)
    )
    assert f"units 0 and 1 differ in {token}" in refused(
        compile_(commands, [first, forged], frozen(["seeds"]))
    )


def test_a_literal_argument_that_moved_is_never_allowed(
    commands: dict[str, Any],
) -> None:
    first, second = compiled_units(commands, [unit(7), unit(8)])
    argv = list(second.spec.argv)
    argv[1] = "other.py"
    forged = dataclasses.replace(
        second, spec=dataclasses.replace(second.spec, argv=tuple(argv))
    )
    # The seed is allowed and the moved argument is not; only it is named.
    assert "units 0 and 1 differ in spec.argv, and" in refused(
        compile_(commands, [first, forged], frozen(["seeds"]))
    )


def test_the_seed_delivery_is_the_seed(commands: dict[str, Any]) -> None:
    """``RESEARCH_OS_SEED_<n>`` is how the seed arrives; it is ``seeds``, nothing else."""

    first, second = compiled_units(commands, [unit(7), unit(8)])
    tokens = campaigns.differences(
        campaigns.unit_configuration(
            first, command_argv=commands["sweep"].argv, capability=capability()
        ),
        campaigns.unit_configuration(
            second, command_argv=commands["sweep"].argv, capability=capability()
        ),
    )
    assert tokens == ("seeds",)
    assert first.spec.env != second.spec.env


# ========================================================= what "allowed" is --
def test_allowed_is_the_frozen_shape_and_never_more_than_the_capability_attests() -> (
    None
):
    declared = capability("[seeds, slope]")
    assert campaigns.allowed_variation((), declared) == ("seeds", "slope")
    assert campaigns.allowed_variation(("seeds",), declared) == ("seeds",)
    # A stated difference the capability does not attest is not allowed: the
    # pre-freeze check refuses such an analysis, and this does not rely on it.
    assert campaigns.allowed_variation(("seeds", "plan"), declared) == ("seeds",)
    assert campaigns.stated_variation(frozen(None)) == ()
    assert campaigns.stated_variation(frozen(["seeds"])) == ("seeds",)


def test_an_unstated_shape_allows_what_the_capability_attests(
    commands: dict[str, Any],
) -> None:
    units = compiled_units(commands, [unit(7), unit(8, slope=2.0)])
    assert isinstance(compile_(commands, units, frozen(None)), campaigns.Compiled)
    # The review's own declaration: a capability attesting only seeds, and an
    # analysis that states nothing more.
    summary = refused(
        compile_(commands, units, frozen(None), declared=capability("[seeds]"))
    )
    assert "units 0 and 1 differ in slope" in summary
    assert "which synthetic.sweep@1 does not attest" not in summary


def test_a_difference_the_capability_does_not_attest_alone_is_padding(
    commands: dict[str, Any],
) -> None:
    """Unchanged: units differing only in the unattested are duplicate observations."""

    units = compiled_units(commands, [unit(7), unit(7, slope=2.0)])
    summary = refused(
        compile_(commands, units, frozen(None), declared=capability("[seeds]"))
    )
    assert "differ only in slope" in summary
    assert "duplicate observations" in summary


def test_the_configuration_is_the_same_from_the_units_and_from_the_frozen_plan(
    commands: dict[str, Any],
) -> None:
    """What the compiler derives and what the chain re-derives agree, token for token."""

    from research_os.portfolio import sciencechain

    units = compiled_units(
        commands, [unit(7), unit(8, plan={"xs": [0, 1, 2, 3, 4], "slope": 3.0})]
    )
    template = commands["sweep"].argv
    records = [
        sciencechain.unit_record(
            index=item.index,
            label=item.label,
            spec=item.spec,
            spec_digest=item.spec_digest,
            variation_digest=item.variation_digest,
            parameters=item.parameters,
            result_artifact=SOURCE,
            input_artifacts=(),
            varies=(),
        )
        for item in units
    ]
    from_units = [
        campaigns.unit_configuration(
            item, command_argv=template, capability=capability()
        )
        for item in units
    ]
    from_plan = [
        campaigns.record_configuration(
            item, command_argv=template, capability=capability()
        )
        for item in records
    ]
    assert [item.values for item in from_units] == [item.values for item in from_plan]
    assert campaigns.differences(*from_plan) == ("seeds", "plan")
    digest = hashlib.sha256(units[1].frozen_inputs[0].canonical).hexdigest()
    assert digest == units[1].frozen_inputs[0].sha256


# ============================================================ the harness --
def test_every_campaign_variation_mutant_still_applies() -> None:
    """``tests/campaign_variation_mutations.py`` cannot rot silently."""

    import ast

    from tests.campaign_variation_mutations import MUTANTS

    root = Path(__file__).resolve().parents[1]
    ids = [item.id for item in MUTANTS]
    assert len(ids) == len(set(ids)) and len(ids) >= 8
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
            assert name.split("[", 1)[0] in names, (mutant.id, test)
