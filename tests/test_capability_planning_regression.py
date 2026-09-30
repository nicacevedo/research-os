"""The second final qualification's failure class, against the real ``cg.cells@1`` declaration.

Final qualification run 2 (Research OS 66d5704, CG 5eb3923) had two
independently generated lineages reach the evidence stage. In both, the
analysis author froze ``fixed_single_execution`` for a support requirement
one execution of ``cg.cells@1`` cannot hold -- in the first, at least four
sizes, four dimensions and five seeds of each of two families (ten instances
at least, where a plan holds four); in the second, twelve distinct seeds
(one execution draws four). The experiment designer, shown the per-execution
bound only then, refused both, correctly, and each contract was blocked for
good, although a campaign within the human-set bounds (six units, 10 800 s)
could have held either. Nothing ran and nothing was lost but the lineage.

This does not replay the historical ideas. It tests the class: an evidence
requirement larger than one ``cg.cells@1`` execution, satisfiable by an
allowed campaign, first proposed as ``fixed_single_execution``.

- Against the declaration as it was at CG 5eb3923 -- no execution bounds --
  nothing can be checked, and such an analysis is frozen (the old path, whose
  refusal came from the designer afterwards).
- Against the declaration as CG now commits it, the same analysis is refused
  before freezing with the mismatch stated in numbers, a campaign form of the
  same requirement is valid, it is the one frozen, and the design proceeds to
  a frozen campaign plan.

``tests/fixtures/cg_cells_research_capabilities.yaml`` is the CG manifest
verbatim (sha256 09b336d1...); removing its ``execution`` block gives back
5eb3923's declaration exactly, which the digest test below establishes
against the digest the run recorded (``rcap-v1:84ccf7cc...``).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os import capability as capabilities
from research_os.experiment.config import load_config as load_experiment_config
from research_os.portfolio import empirical, sciencechain, shape
from research_os.portfolio.contracts import AnalysisSpec
from research_os.portfolio.models import (
    ContractState,
    ExecutionShapeVerdict,
    ExperimentRole,
    ExperimentState,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import _context, _idea

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = (FIXTURES / "cg_cells_research_capabilities.yaml").read_text(
    encoding="utf-8"
)
EXPERIMENTS = (FIXTURES / "cg_cells_experiments.yaml").read_text(encoding="utf-8")
SOURCE = "results/ros/cells.json"
ANALYST = TEMPLATES["analysis_designer"].identity
DESIGNER = TEMPLATES["experiment_designer"].identity

#: What the second final qualification recorded for cg.cells@1 at CG 5eb3923.
DIGEST_AT_5EB3923 = (
    "rcap-v1:84ccf7ccfb1c2a556c80fab0e4be0c8c0760d6abb77b68101b7d40f586b27a31"
)

#: The portfolio bounds the second final qualification froze: six units,
#: 10 800 s per campaign, 1 800 s per execution.
QUALIFICATION = capabilities.HumanBounds(
    max_execution_seconds=1800, max_campaign_units=6, max_campaign_seconds=10800
)


def without_execution_bounds(text: str) -> str:
    """The declaration as CG 5eb3923 committed it: the same, less the new block."""

    start = text.index("    # What ONE execution holds, at most")
    end = text.index("    # Several executions of this capability may form one")
    return text[:start] + text[end:]


def commands(tmp_path: Path, project: str = "cg-sparse-regression") -> dict[str, Any]:
    path = tmp_path / "experiments.yaml"
    path.write_text(EXPERIMENTS.replace("{project}", project), encoding="utf-8")
    return dict(load_experiment_config(path).projects[project].commands)


def planning(
    tmp_path: Path,
    text: str = MANIFEST,
    bounds: capabilities.HumanBounds = QUALIFICATION,
) -> shape.Planning:
    raw = text.encode("utf-8")
    loaded = capabilities.LoadedManifest(
        manifest=capabilities.parse_manifest(raw),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        commit="c" * 40,
    )
    declared = commands(tmp_path)
    envelope = capabilities.execution_envelope(loaded, declared, bounds)
    assert envelope is not None
    return shape.Planning(envelope=envelope, loaded=loaded, commands=declared)


# ------------------------------------------------ the requirement shapes --
def _solves(name: str, **include: Any) -> dict[str, Any]:
    return {
        "name": name,
        "source": SOURCE,
        "kind": "records",
        "path": "records",
        "fields": ["n", "p", "seed", "wall_seconds"],
        "include": [
            {"field": "ok", "comparator": "==", "value": True},
            *(
                {"field": key, "comparator": "==", "value": value}
                for key, value in include.items()
            ),
        ],
        "incomplete_records": "exclude",
    }


def _analysis(
    observables: list[dict[str, Any]],
    support: list[dict[str, Any]],
    *,
    rule: str,
    execution_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "analysable": True,
        "estimand": "the correlation of solve time with instance size",
        "population": "the sparse-regression instances the declared families draw",
        "target_claim": "solve time does not grow with instance size",
        "observables": observables,
        "reductions": [
            {
                "name": "flatness",
                "op": "correlation",
                "observable": observables[0]["name"],
                "field": "n",
                "other_field": "wall_seconds",
            }
        ],
        "primary_statistic": "flatness",
        "success": {"comparator": "<", "threshold": 0.25},
        "failure": {"comparator": ">", "threshold": 0.55},
        "support": support,
        "stopping_rule": rule,
    }
    if execution_shape is not None:
        answer["execution_shape"] = execution_shape
    return answer


def two_families(rule: str = "fixed_single_execution", **shape_: Any) -> dict[str, Any]:
    """The first lineage's class: two families, each swept in size and seeded."""

    need = {"min_records": 10, "min_distinct": {"n": 4, "p": 4, "seed": 5}}
    return _analysis(
        [_solves("sparse", family="sparse"), _solves("illcond", family="illcond")],
        [{"observable": "sparse", **need}, {"observable": "illcond", **need}],
        rule=rule,
        execution_shape=shape_ or None,
    )


def many_seeds(
    seeds: int = 12, rule: str = "fixed_single_execution", **shape_: Any
) -> dict[str, Any]:
    """The second lineage's class: many independent draws of one regime."""

    return _analysis(
        [_solves("solves")],
        [
            {
                "observable": "solves",
                "min_records": seeds,
                "min_distinct": {"seed": seeds},
            }
        ],
        rule=rule,
        execution_shape=shape_ or None,
    )


def verdict(checker: shape.Planning, answer: dict[str, Any]) -> shape.ShapeCheck:
    return checker.check(AnalysisSpec.model_validate(answer))


# ============================================================= declaration --
def test_the_fixture_is_cg_5eb3923_plus_its_execution_bounds_and_nothing_else() -> None:
    (old,) = capabilities.parse_manifest(
        without_execution_bounds(MANIFEST).encode()
    ).capabilities
    assert capabilities.capability_digest(old) == DIGEST_AT_5EB3923
    (new,) = capabilities.parse_manifest(MANIFEST.encode()).capabilities
    assert new.execution is not None
    assert capabilities.capability_digest(new) != DIGEST_AT_5EB3923
    # Nothing but the statement of bounds changed: not the per-execution
    # limit, the campaign limit, the time, the observables or the attestation.
    assert new.model_copy(update={"execution": None}) == old
    assert new.campaign is not None and new.campaign.max_units == 6
    assert new.resources.timeout_seconds == 1500


def test_the_committed_cg_declaration_states_what_one_execution_holds(
    tmp_path: Path,
) -> None:
    checker = planning(tmp_path)
    (item,) = checker.envelope.capabilities
    assert item.ref == "cg.cells@1" and item.usable
    assert item.seconds == 1800 and item.units == 6
    assert item.records_max("solves") == 240
    instances = item.input_named("instances")
    assert instances is not None and instances.max == 4
    assert set(item.renewed_by(instances, "seed")) == {"seeds", "plan"}
    assert item.renewed_by(instances, "n") == ("plan",)
    repetitions = item.input_named("repetitions")
    assert repetitions is not None and item.renewed_by(repetitions, "repetition") == ()
    text = "\n".join(checker.envelope.lines())
    assert "4 instances of observable solves" in text
    assert "may have 2 to 6 units here" in text
    assert (
        "distinct seed: at most 4 in one execution, at most 24 in a campaign of 6"
        in text
    )
    # The same bounds under a person's tighter campaign time give fewer units.
    tighter = planning(
        tmp_path, bounds=capabilities.HumanBounds(1800, 6, 7200)
    ).envelope.capabilities[0]
    assert tighter.units == 4


# =========================================================== the class --
@pytest.mark.parametrize(
    ("name", "answer", "min_units"),
    [
        ("two families swept in size and seeded", two_families(), 3),
        ("twelve independent seeds of one regime", many_seeds(12), 3),
        ("five seeds of one regime", many_seeds(5), 2),
    ],
)
def test_a_single_execution_requirement_a_campaign_can_hold_is_refused_before_freezing(
    tmp_path: Path, name: str, answer: dict[str, Any], min_units: int
) -> None:
    checker = planning(tmp_path)
    checked = verdict(checker, answer)
    assert checked.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH, name
    assert not checked.single_fits and checked.campaign_fits
    assert checked.min_units == min_units, name
    assert "one execution holds 4" in " ".join(checked.problems)
    # The same requirement, executed as a campaign of the smallest size the
    # envelope names, is valid -- and one unit fewer is not.
    campaign = {
        **answer,
        "stopping_rule": "fixed_campaign",
        "execution_shape": {"capability": "cg.cells@1", "units": min_units},
    }
    assert verdict(checker, campaign).verdict is ExecutionShapeVerdict.VALID_CAMPAIGN
    smaller = {**campaign, "execution_shape": {"units": min_units - 1}}
    if min_units - 1 >= 2:
        assert verdict(checker, smaller).verdict is (
            ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
        )


def test_the_architecture_class_across_every_sample_size(tmp_path: Path) -> None:
    """One execution holds four draws, an allowed campaign twenty-four, nothing more."""

    checker = planning(tmp_path)
    for seeds in range(1, 31):
        single = verdict(checker, many_seeds(seeds))
        campaign = verdict(checker, many_seeds(seeds, rule="fixed_campaign"))
        if seeds <= 4:
            assert single.verdict is ExecutionShapeVerdict.VALID_SINGLE_EXECUTION
            assert campaign.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN
        elif seeds <= 24:
            assert single.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
            assert campaign.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN
            assert campaign.min_units == -(-seeds // 4)
        else:
            assert single.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED
            assert campaign.verdict is ExecutionShapeVerdict.CAPABILITY_LIMITED


def test_seed_offsets_alone_cannot_supply_more_sizes_than_one_plan_holds(
    tmp_path: Path,
) -> None:
    """Another offset redraws the same four sizes; eight sizes need other plans."""

    checker = planning(tmp_path)
    seeds_only = verdict(
        checker, two_families("fixed_campaign", units=6, unit_varies=["seeds"])
    )
    assert seeds_only.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
    assert "distinct n of solves: needs 8" in " ".join(seeds_only.problems)
    plans = verdict(
        checker, two_families("fixed_campaign", units=3, unit_varies=["plan", "seeds"])
    )
    assert plans.verdict is ExecutionShapeVerdict.VALID_CAMPAIGN


def test_the_old_declaration_froze_it_and_the_committed_one_refuses_it(
    tmp_path: Path,
) -> None:
    """Before and after, the one variable being the declared bounds."""

    old = verdict(
        planning(tmp_path, without_execution_bounds(MANIFEST)), two_families()
    )
    assert old.verdict is ExecutionShapeVerdict.VALID_SINGLE_EXECUTION
    new = verdict(planning(tmp_path), two_families())
    assert new.verdict is ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH


# ====================================================== through the route --
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def cg_repo(tmp_path: Path, runtime_xdg: Path, runtime_project: str) -> Path:
    """A repository committing the real cg.cells declaration. Nothing here runs it."""

    repo = tmp_path / "cg"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / ".research").mkdir()
    (repo / ".research" / "project.yaml").write_text("id: cg\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "ros_cells.py").write_text(
        "raise SystemExit('not run by this test')\n", encoding="utf-8"
    )
    (repo / "research-capabilities.yaml").write_text(MANIFEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "cg.cells@1, with its execution bounds")
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS.replace("{project}", runtime_project), encoding="utf-8"
    )
    return repo


def _plan(family: str, sizes: list[tuple[int, int]], seed: int) -> dict[str, Any]:
    return {
        "instances": [
            {"family": family, "n": n, "p": p, "k": 3, "seed": seed + index}
            for index, (n, p) in enumerate(sizes)
        ],
        "lambda_ratios": [0.1],
        "arms": [{"solver": "conic_thesis", "tolerances": [1e-6]}],
        "repetitions": 2,
        "timeout_seconds": 30,
    }


def _campaign_design() -> dict[str, Any]:
    sweeps = [
        ("sparse", [(100, 30), (250, 30), (500, 30), (1000, 30)]),
        ("sparse", [(500, 10), (500, 50), (500, 100), (500, 300)]),
        ("illcond", [(100, 30), (250, 30), (500, 30), (1000, 30)]),
        ("illcond", [(500, 10), (500, 50), (500, 100), (500, 300)]),
    ]
    units = [
        {
            "label": f"{family} sweep {index}",
            "seeds": [index + 1],
            "command_parameters": {"plan": _plan(family, sizes, 10 * index)},
        }
        for index, (family, sizes) in enumerate(sweeps)
    ]
    return {
        "testable": True,
        "command": "cg-cells",
        "command_parameters": {"plan": units[0]["command_parameters"]["plan"]},
        "seeds": [1],
        "variables": [
            {"name": "n", "role": "manipulated", "levels": [100, 250, 500, 1000]},
            {"name": "p", "role": "manipulated", "levels": [10, 30, 50, 100, 300]},
        ],
        "sampling": "an n sweep and a p sweep per family, one plan each",
        "dataset_identity": "cg2026 synthetic families",
        "falsification_criterion": "solve time grows with instance size",
        "repetitions": 2,
        "campaign": {
            "units": units,
            "rationale": "four instances per execution; four plans hold both sweeps",
        },
    }


class Revising(ScriptedRouter):
    first: dict[str, Any]
    revised: dict[str, Any]

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if request.prompt_version == ANALYST:
            self.answers_by_prompt[ANALYST] = (
                self.revised if "REFUSED ANALYSIS:" in request.prompt else self.first
            )
        return super().complete(request)


def test_on_the_route_the_single_form_cannot_freeze_and_the_campaign_form_is_designed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    cg_repo: Path,
    tmp_path: Path,
) -> None:
    revised = two_families(
        "fixed_campaign",
        capability="cg.cells@1",
        units=4,
        unit_varies=["plan", "seeds"],
        rationale="four instances per execution; an n and a p sweep per family",
    )
    router = Revising(
        answers={},
        answers_by_prompt={ANALYST: two_families(), DESIGNER: _campaign_design()},
        store=RuntimeStore(runtime_db),
    )
    router.first, router.revised = two_families(), revised
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=_idea(portfolio, runtime_project),
        router=router,
        repo=cg_repo,
    )
    # The run's own bounds: six units, 10 800 s.
    context.config = context.config.with_overrides({"max_campaign_seconds": 10800})
    version = portfolio.require_version(context.idea_id)

    refused = empirical.design(context, version)
    assert not refused.ok
    assert refused.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "EXECUTION_SHAPE_MISMATCH" in refused.detail
    assert "distinct seed of solves: needs 10" in refused.detail
    assert "one execution holds 4" in refused.detail
    assert "fixed_campaign with 3 to 6 units of cg.cells@1" in refused.detail
    assert (
        portfolio.live_contract(
            idea_id=context.idea_id, idea_version=1, role=ExperimentRole.PRIMARY
        )
        is None
    )
    (first_ask,) = router.requests_for_prompt(ANALYST)
    assert "4 instances of observable solves" in first_ask.prompt

    designed = empirical.design(context, version)
    assert designed.ok, designed.detail
    experiment = designed.experiment
    assert experiment is not None and experiment.state is ExperimentState.PROPOSED
    contract = portfolio.require_contract(experiment.contract_id or "")
    assert contract.state is ContractState.FROZEN
    stored = json.loads(context.artifacts.get_text(contract.analysis_artifact_id))
    assert stored["analysis"]["stopping_rule"] == "fixed_campaign"
    assert stored["execution_shape_check"]["verdict"] == "VALID_CAMPAIGN"
    assert stored["revises"][0]["verdict"] == "EXECUTION_SHAPE_MISMATCH"
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert chain.is_campaign and len(chain.units) == 4
    assert chain.contract.payload["execution_shape"]["units"] == 4
    assert len(portfolio.campaign_units(experiment.plan_digest)) == 4
    # Designed and frozen; nothing ran.
    with runtime_db.tx() as conn:
        assert (
            conn.execute("select count(*) as n from external_jobs").fetchone()["n"] == 0
        )
