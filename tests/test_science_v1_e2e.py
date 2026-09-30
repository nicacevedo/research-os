"""The v1 science chain, end to end, on a synthetic science repository.

contract -> design -> capability resolution -> plan -> trusted execution ->
validated result -> system-computed outcome -> replication -> assessment ->
gate. ``docs/SCIENCE_EXECUTION.md`` is the specification.

The science repository declares one typed capability in its committed
``research-capabilities.yaml``; the host declares the command that runs it in
``experiments.yaml``. The program is a seeded linear response whose composed
plan chooses the slope, so a test can choose which side of the frozen
thresholds the system-computed estimate lands on -- and a ``mode`` that makes
it crash, write nothing, write malformed JSON or write JSON off its declared
schema. Everything else is the real route: the real resolver, the real
freezing, the real executor and receipt, the real analysis engine, the real
database triggers.

Each case the closure brief names is a test here: an executable contract; a
missing observable; malformed output; the wrong result schema; a result from
the wrong execution; a changed frozen design; a missing trusted receipt; a
replication with an unsupported perturbation; a replication contradiction;
deterministic SUPPORTED, REFUTED and INCONCLUSIVE; BUDGET_LIMITED;
CAPABILITY_LIMITED.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import empirical, sciencechain
from research_os.portfolio.models import (
    AdjudicationType,
    ContractState,
    EmpiricalConclusion,
    EvidenceKind,
    ExperimentRole,
    ExperimentState,
    PrimaryOutcome,
    ScienceObjectKind,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import BudgetScope
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import _context, _idea

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


# ------------------------------------------------------ the science repo --
RESPOND_SCRIPT = """\
import json, os, pathlib, random, sys
plan = json.loads(pathlib.Path(sys.argv[sys.argv.index("--plan") + 1]).read_text())
seed = int(os.environ.get("RESEARCH_OS_SEED_0", "0"))
rng = random.Random(seed)
out = pathlib.Path("results/response.json")
out.parent.mkdir(parents=True, exist_ok=True)
mode = plan.get("mode", "normal")
if mode == "crash":
    print("the node fell over", file=sys.stderr)
    raise SystemExit(3)
if mode == "silent":
    raise SystemExit(0)
if mode == "malformed":
    out.write_text("{this is not json")
    raise SystemExit(0)
records = [
    {"x": x, "y": plan["slope"] * x + rng.uniform(-0.01, 0.01), "group": "a",
     "wall_seconds": 0.001}
    for x in plan["xs"]
]
if mode == "offschema":
    records = [{**item, "x": str(item["x"])} for item in records]
out.write_text(json.dumps({"records": records, "summary": {"count": len(records)}}))
"""

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  {project}:
    default_executor: local
    commands:
      respond:
        name: respond
        description: Draw a seeded linear response over a composed grid of x.
        argv: ["python3", "respond.py", "--plan", "{{plan}}"]
        parameters:
          - name: plan
            type: generated
            required: true
            max_bytes: 4096
            input_schema:
              type: object
              additionalProperties: false
              required: ["slope", "xs"]
              properties:
                mode:
                  type: string
                  enum: ["normal", "crash", "silent", "malformed", "offschema"]
                slope: {{type: number, minimum: -10, maximum: 10}}
                xs:
                  type: array
                  minItems: 1
                  maxItems: 12
                  items: {{type: number, minimum: -100, maximum: 100}}
        outputs: ["results/response.json"]
        timeout_seconds: 120
        checks: []
      respond-raw:
        name: respond-raw
        description: The same program, which backs no declared capability.
        argv: ["python3", "respond.py", "--plan", "{{plan}}"]
        parameters:
          - name: plan
            type: path
            required: true
        outputs: ["results/response.json"]
        timeout_seconds: 120
        checks: []
"""


def manifest(
    *,
    perturbations: tuple[tuple[str, str], ...] = (("seeds", ""),),
    timeout: int = 60,
    command: str = "respond",
) -> str:
    """The science repository's capability declaration, with the knobs tests turn."""

    lines = []
    for kind, name in perturbations:
        lines.append(f"        - kind: {kind}")
        if name:
            lines.append(f"          name: {name}")
        lines.append(f"          description: the {kind} {name} reaches every y")
    attested = "\n".join(lines) if lines else "        []"
    return f"""\
schema: research-os-capabilities-v1
capabilities:
  - id: synthetic.response
    version: 1
    title: A seeded linear response
    command: {command}
    parameters:
      - name: plan
        description: the composed grid and slope
    result:
      artifact: results/response.json
      format: json
      schema:
        type: object
        required: [records, summary]
        properties:
          records:
            type: array
            items:
              type: object
              required: [x, y, group]
              properties:
                x: {{type: number}}
                y: {{type: number}}
                group: {{type: string}}
                wall_seconds: {{type: number}}
          summary:
            type: object
            required: [count]
            properties:
              count: {{type: integer}}
    observables:
      - name: points
        kind: records
        path: records
        fields:
          - {{name: x, type: number}}
          - {{name: y, type: number}}
          - {{name: group, type: string}}
          - {{name: wall_seconds, type: number, unit: s, deterministic: false}}
      - name: count
        kind: scalar
        path: summary.count
        type: integer
    determinism: seeded
    determinism_notes: RESEARCH_OS_SEED_0 seeds the noise; wall_seconds varies
    replication:
      perturbations:
{attested}
    resources:
      timeout_seconds: {timeout}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_manifest(repo: Path, text: str) -> str:
    (repo / "research-capabilities.yaml").write_text(text, encoding="utf-8")
    _git(repo, "add", "research-capabilities.yaml")
    _git(repo, "commit", "-qm", "capabilities")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def science_repo(tmp_path: Path, runtime_xdg: Path, runtime_project: str) -> Path:
    repo = tmp_path / "science"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "respond.py").write_text(RESPOND_SCRIPT, encoding="utf-8")
    capsule = repo / ".research"
    capsule.mkdir()
    (capsule / "project.yaml").write_text("id: science\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    commit_manifest(repo, manifest())
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=runtime_project), encoding="utf-8"
    )
    return repo


# ------------------------------------------------------------ the answers --
ANALYSIS: dict[str, Any] = {
    "analysable": True,
    "estimand": "the slope of the response in x",
    "population": "the synthetic linear-response instances the plan draws",
    "target_claim": "the response rises with x",
    "observables": [
        {
            "name": "points",
            "source": "results/response.json",
            "kind": "records",
            "path": "records",
            "fields": ["x", "y"],
        }
    ],
    "reductions": [
        {
            "name": "slope",
            "op": "ols_coefficient",
            "observable": "points",
            "response": "y",
            "terms": ["x"],
            "coefficient": "x",
        }
    ],
    "primary_statistic": "slope",
    "success": {"comparator": ">", "threshold": 0.5},
    "failure": {"comparator": "<", "threshold": 0.1},
    "support": [{"observable": "points", "min_records": 3, "min_distinct": {"x": 3}}],
}


def design(
    slope: float,
    *,
    mode: str = "normal",
    seeds: tuple[int, ...] = (7,),
    variation: str = "",
) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "testable": True,
        "command": "respond",
        "command_parameters": {
            "plan": {"mode": mode, "slope": slope, "xs": [0, 1, 2, 3, 4]}
        },
        "seeds": list(seeds),
        "variables": [
            {"name": "x", "role": "manipulated", "levels": [0, 1, 2, 3, 4]},
            {"name": "noise seed", "role": "controlled", "levels": list(seeds)},
        ],
        "sampling": "five evenly spaced x, one draw each",
        "dataset_identity": "synthetic linear response",
        "falsification_criterion": "no positive slope",
        "repetitions": 1,
    }
    if variation:
        answer["variation_kind"] = variation
        answer["variation_detail"] = f"{variation}: a fresh draw"
    return answer


def router(
    runtime_db: Database,
    *,
    analysis: dict[str, Any] | None = None,
    primary: dict[str, Any] | None = None,
    replication: dict[str, Any] | None = None,
) -> ScriptedRouter:
    answers = {
        TEMPLATES["analysis_designer"].identity: analysis or ANALYSIS,
        TEMPLATES["experiment_designer"].identity: primary or design(1.0),
    }
    if replication is not None:
        answers[TEMPLATES["replication_designer"].identity] = replication
    return ScriptedRouter(
        answers={}, answers_by_prompt=answers, store=RuntimeStore(runtime_db)
    )


def context_for(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    scripted: ScriptedRouter,
) -> Any:
    return _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=_idea(portfolio, runtime_project),
        router=scripted,
        repo=repo,
    )


def advance(context: Any, role: ExperimentRole = ExperimentRole.PRIMARY) -> Any:
    version = context.portfolio.require_version(context.idea_id)
    return empirical.advance(context, version, role=role)


def outcomes(context: Any, role: ExperimentRole | None = None) -> list[Any]:
    return list(context.portfolio.outcomes(idea_id=context.idea_id, role=role))


def chains(context: Any) -> tuple[Any, ...]:
    return sciencechain.science_chains(
        context.portfolio,
        context.artifacts,
        idea_id=context.idea_id,
        idea_version=1,
        evidence=context.portfolio.list_evidence(
            idea_id=context.idea_id, idea_version=1
        ),
    )


def measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repo: Path,
    tmp_path: Path,
    *,
    slope: float = 1.0,
    replication: dict[str, Any] | None = None,
) -> Any:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        repo,
        tmp_path,
        router(runtime_db, primary=design(slope), replication=replication),
    )
    step = advance(context)
    assert step.ok, step.detail
    return context


# ===================================================== executable contract --
def test_an_executable_contract_is_frozen_bound_run_and_read_in_order(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The whole chain, in the only order the database accepts it."""

    head = _git(science_repo, "rev-parse", "HEAD")
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.state is ExperimentState.INTERPRETED
    assert experiment.plan_digest

    # Three distinct frozen objects, each naming the one before it.
    plan = portfolio.get_science_object(experiment.plan_digest)
    design_row = portfolio.get_science_object(plan.parent_digest)
    contract = portfolio.get_science_object(design_row.parent_digest)
    assert (contract.kind, design_row.kind, plan.kind) == (
        ScienceObjectKind.CONTRACT,
        ScienceObjectKind.DESIGN,
        ScienceObjectKind.PLAN,
    )
    assert (
        len({contract.object_digest, design_row.object_digest, plan.object_digest}) == 3
    )
    assert contract.frozen_at <= design_row.frozen_at <= plan.frozen_at
    assert plan.frozen_at <= experiment.created_at

    # The exact capability binding: which declaration, at which commit.
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    assert plan.capability_ref == "synthetic.response@1"
    assert chain.code_commit == head
    binding = chain.plan.payload["capability"]
    assert binding["observables"] == {"points": "points"}
    assert binding["nondeterministic_fields_read"] == []
    assert binding["result_path"] == "results/response.json"
    # The contract says WHAT: the estimand, the population, both criteria and
    # the inconclusive region; the design says HOW; the plan says WHERE.
    assert chain.contract.payload["population"] == ANALYSIS["population"]
    assert chain.contract.payload["success_criterion"]["threshold"] == 0.5
    assert chain.contract.payload["inconclusive_region"]["state"] == "INCONCLUSIVE"
    assert chain.design.payload["repetitions"] == 1
    assert chain.design.payload["comparison_groups"][0]["name"] == "x"
    assert chain.plan.payload["spec_digest"] == experiment.spec_digest

    # The runner's receipt names the plan, and the execution ran its commit.
    receipt = portfolio.receipt_for_job(experiment.job_id)
    assert receipt.plan_digest == plan.object_digest
    document = json.loads(context.artifacts.get_text(receipt.receipt_artifact_id))
    assert document["science"]["plan_digest"] == plan.object_digest
    assert document["science"]["capability_ref"] == "synthetic.response@1"
    assert document["code"]["base_commit"] == head

    # And one system-computed outcome, bound to all of it.
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.SUPPORTED
    assert outcome.reason == "success_criterion_held"
    assert outcome.receipt_id == receipt.receipt_id
    assert outcome.plan_digest == plan.object_digest
    assert outcome.design_digest == design_row.object_digest
    assert outcome.contract_digest == contract.object_digest
    assert outcome.result_sha256
    assert outcome.estimate == pytest.approx(1.0, abs=0.02)

    (record,) = chains(context)
    assert record.admissible, record.problems


def test_a_capability_manifest_is_read_from_the_commit_not_the_working_tree(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """An uncommitted edit to the declaration declares nothing."""

    (science_repo / "research-capabilities.yaml").write_text(
        manifest().replace("name: y, type: number", "name: y, type: string"),
        encoding="utf-8",
    )
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.SUPPORTED


# ========================================================== the outcomes --
@pytest.mark.parametrize(
    ("slope", "state", "conclusion"),
    [
        (1.0, PrimaryOutcome.SUPPORTED, EmpiricalConclusion.SUPPORTS),
        (0.0, PrimaryOutcome.REFUTED, EmpiricalConclusion.CONTRADICTS),
        (0.3, PrimaryOutcome.INCONCLUSIVE, EmpiricalConclusion.INCONCLUSIVE),
    ],
)
def test_the_primary_outcome_is_computed_from_the_frozen_rule(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    slope: float,
    state: PrimaryOutcome,
    conclusion: EmpiricalConclusion,
) -> None:
    """Deterministic SUPPORTED, REFUTED and INCONCLUSIVE. No model is asked."""

    context = measured(
        portfolio, runtime_db, runtime_project, science_repo, tmp_path, slope=slope
    )
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.conclusion is conclusion
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is state
    assert outcome.estimate == pytest.approx(slope, abs=0.02)
    # The reading happened after the designs and never asked anyone.
    roles = [str(item.role) for item in context.models.requests]
    assert roles == ["analysis_designer", "experimentalist"]
    (record,) = chains(context)
    assert record.chain_intact, record.problems
    assert record.admissible is (
        state in {PrimaryOutcome.SUPPORTED, PrimaryOutcome.REFUTED}
    )


def test_an_execution_that_crashes_is_execution_failed_and_never_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=design(1.0, mode="crash")),
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.EXECUTION_FAILED
    assert outcome.plan_digest
    assert not portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)


# ======================================================= invalid evidence --
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("malformed", "result_malformed"),
        ("offschema", "result_schema_violation"),
        ("silent", "result_missing"),
    ],
)
def test_a_result_off_its_declaration_is_invalid_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    mode: str,
    reason: str,
) -> None:
    """Malformed output, the wrong result schema, no result: never read as science.

    The execution happened and completed; what it produced is not the result
    its capability declares. The decision rule is never applied to it.
    """

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=design(1.0, mode=mode)),
    )
    step = advance(context)
    assert step.ok, step.detail
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert outcome.reason == reason
    assert outcome.result_sha256 is None
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.conclusion is EmpiricalConclusion.INSUFFICIENT
    document = json.loads(context.artifacts.get_text(experiment.analysis_artifact_id))
    assert document["science"]["outcome"] == "INVALID_EVIDENCE"
    assert document["analysis_result"] is None, "the rule was applied to it"
    (record,) = chains(context)
    assert not record.admissible


def test_a_result_replaced_after_the_execution_is_not_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A result from the wrong execution: bytes the runner did not hash at exit."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=design(0.0)),
    )
    version = portfolio.require_version(context.idea_id)
    step = empirical.design(context, version)
    assert step.ok, step.detail
    step = empirical.submit(context, step.experiment)
    assert step.ok, step.detail
    workspace = Path(step.experiment.workspace_path)
    forged = {
        "records": [{"x": x, "y": 2.0 * x, "group": "a"} for x in range(5)],
        "summary": {"count": 5},
    }
    (workspace / "results" / "response.json").write_text(json.dumps(forged))
    step = empirical.interpret(context, step.experiment)
    assert not step.ok
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert not portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)


def test_the_database_refuses_an_outcome_that_reads_another_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A result bound to the wrong execution is refused below the application."""

    first = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    second = measured(
        portfolio, runtime_db, runtime_project, science_repo, tmp_path / "b", slope=0.0
    )
    (theirs,) = portfolio.list_experiments(idea_id=first.idea_id)
    (mine,) = portfolio.list_experiments(idea_id=second.idea_id)
    (outcome,) = [item for item in outcomes(second) if item.receipt_id]
    foreign = portfolio.receipt_for_job(theirs.job_id)
    with pytest.raises(RuntimeDatabaseError):
        portfolio.record_outcome(
            project_id=runtime_project,
            idea_id=mine.idea_id,
            idea_version=1,
            role=ExperimentRole.PRIMARY,
            state=PrimaryOutcome.SUPPORTED,
            reason="forged",
            experiment_id=mine.experiment_id,
            contract_digest=outcome.contract_digest,
            design_digest=outcome.design_digest,
            plan_digest=outcome.plan_digest,
            capability_ref=outcome.capability_ref,
            capability_digest=outcome.capability_digest,
            receipt_id=foreign.receipt_id,
            result_sha256="0" * 64,
            record_artifact_id=outcome.record_artifact_id,
        )


def test_an_execution_with_no_trusted_receipt_is_invalid_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The runner never recorded it: nothing vouches for what the workspace holds."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    version = portfolio.require_version(context.idea_id)
    step = empirical.submit(context, empirical.design(context, version).experiment)
    assert step.ok, step.detail
    # A crash between the executor returning and the runner recording its
    # receipt, simulated with the database's own triggers suspended.
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute("delete from execution_receipts")
    step = empirical.interpret(context, step.experiment)
    assert not step.ok
    assert step.failure_class is FailureClass.ARTIFACT_MISSING
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert outcome.receipt_id is None


# ================================================ changed frozen upstream --
def test_a_changed_frozen_design_stops_the_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The design's stored bytes no longer hash to its digest: nothing runs."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    version = portfolio.require_version(context.idea_id)
    experiment = empirical.design(context, version).experiment
    plan = portfolio.get_science_object(experiment.plan_digest)
    design_row = portfolio.get_science_object(plan.parent_digest)
    stored = context.artifacts.path_for(design_row.artifact_id)
    stored.chmod(0o644)
    payload = json.loads(stored.read_text())
    payload["repetitions"] = 9
    stored.write_bytes(sciencechain.canonical(payload))
    step = empirical.submit(context, experiment)
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "frozen design" in step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.INVALID_EVIDENCE
    assert portfolio.receipt_for_job(experiment.job_id or "") is None


def test_frozen_objects_and_an_experiments_plan_never_change(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    for statement in (
        "update science_objects set capability_ref = 'x@1'",
        "delete from science_objects",
        "update science_outcomes set state = 'REFUTED'",
        (
            f"update idea_experiments set plan_digest = null "
            f"where experiment_id = '{experiment.experiment_id}'"
        ),
    ):
        with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
            conn.execute(statement)


def test_nothing_is_frozen_before_what_it_realises(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The database's ordering rule: a design needs its contract, a plan its design."""

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    plan = portfolio.get_science_object(experiment.plan_digest)
    design_row = portfolio.get_science_object(plan.parent_digest)
    artifact = context.artifacts.put_bytes(b"{}", media_type="application/json")
    cases = [
        # A plan whose parent is a contract, not a design.
        ("PLAN", design_row.parent_digest, "cap@1", "rcap-v1:x", "d"),
        # A design whose parent does not exist.
        ("DESIGN", "rscontract-v1:" + "0" * 64, None, None, None),
        # A design with no parent at all.
        ("DESIGN", None, None, None, None),
    ]
    prefix = {"PLAN": "rsplan-v1:", "DESIGN": "rsdesign-v1:"}
    for kind, parent, ref, digest, spec in cases:
        with pytest.raises(RuntimeDatabaseError):
            portfolio.freeze_science_object(
                object_digest=prefix[kind] + artifact.artifact_id,
                kind=ScienceObjectKind(kind),
                project_id=runtime_project,
                idea_id=context.idea_id,
                idea_version=1,
                artifact_id=artifact.artifact_id,
                parent_digest=parent,
                capability_ref=ref,
                capability_digest=digest,
                spec_digest=spec,
            )
    # A parent frozen *after* its child.
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(
            "insert into science_objects (object_digest, kind, project_id, idea_id, "
            "idea_version, parent_digest, artifact_id, frozen_at) values "
            "(%s, 'DESIGN', %s, %s, 1, %s, %s, now() - interval '1 day')",
            (
                "rsdesign-v1:" + artifact.artifact_id,
                runtime_project,
                context.idea_id,
                design_row.parent_digest,
                artifact.artifact_id,
            ),
        )


def test_a_revised_idea_gets_a_new_contract_and_keeps_no_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A change upstream is a new identity; downstream evidence does not transfer."""

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (first,) = portfolio.list_experiments(idea_id=context.idea_id)
    old_contract = portfolio.get_science_object(
        portfolio.get_science_object(
            portfolio.get_science_object(first.plan_digest).parent_digest
        ).parent_digest
    )
    revised = portfolio.append_version(
        idea_id=context.idea_id,
        fields=idea_fields(
            adjudication_types=[AdjudicationType.EMPIRICAL],
            mechanism="the response is linear in x with a revised mechanism",
        ),
        origin_role="test",
    )
    assert revised.version == 2
    step = advance(context)
    assert step.ok, step.detail
    second = portfolio.get_experiment(idea_id=context.idea_id, idea_version=2)
    new_contract = portfolio.get_science_object(
        portfolio.get_science_object(
            portfolio.get_science_object(second.plan_digest).parent_digest
        ).parent_digest
    )
    assert new_contract.object_digest != old_contract.object_digest
    assert new_contract.idea_version == 2
    # Version 2's chains are its own: version 1's reading is not among them.
    v2 = sciencechain.science_chains(
        portfolio,
        context.artifacts,
        idea_id=context.idea_id,
        idea_version=2,
        evidence=portfolio.list_evidence(idea_id=context.idea_id, idea_version=2),
    )
    assert {item.experiment_id for item in v2} == {second.experiment_id}


# ============================================================ capability --
def test_an_observable_no_capability_declares_is_capability_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A model cannot declare that an unavailable observable exists.

    The analysis reads a field the capability does not declare. Resolution
    refuses it before any design is asked for, names the exact field, keeps
    the frozen analysis, and records CAPABILITY_LIMITED.
    """

    analysis = json.loads(json.dumps(ANALYSIS))
    analysis["observables"][0]["fields"] = ["x", "y", "duality_gap"]
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, analysis=analysis),
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert "duality_gap" in step.detail
    (contract,) = portfolio.list_contracts(idea_id=context.idea_id)
    assert contract.state is ContractState.BLOCKED_CAPABILITY
    assert any("duality_gap" in item for item in contract.capability_request["unmet"])
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert outcome.contract_id == contract.contract_id
    assert not portfolio.list_experiments(idea_id=context.idea_id)
    assert [str(item.role) for item in context.models.requests] == ["analysis_designer"]

    # Asking again while nothing about the declarations changed costs nothing.
    step = advance(context)
    assert not step.ok and "still waiting" in step.detail
    assert len(context.models.requests) == 1


def test_a_numeric_reading_of_a_string_field_is_capability_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    analysis = json.loads(json.dumps(ANALYSIS))
    analysis["observables"][0]["fields"] = ["x", "y", "group"]
    analysis["reductions"][0]["response"] = "group"
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, analysis=analysis),
    )
    step = advance(context)
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert "reads field 'group' as a number" in step.detail


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (manifest(timeout=7200), "ceiling"),
        (manifest(command="undeclared"), "does not declare"),
        (
            "schema: research-os-capabilities-v1\ncapabilities: []\n",
            "does not validate",
        ),
    ],
    ids=["needs-longer-than-the-host-allows", "host-never-declared-it", "invalid"],
)
def test_a_contract_no_host_can_execute_is_capability_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
    text: str,
    fragment: str,
) -> None:
    """A capability the host cannot run, one it did not authorise, or none valid."""

    commit_manifest(science_repo, text)
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert fragment in step.detail, step.detail
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.CAPABILITY_LIMITED
    assert not portfolio.list_experiments(idea_id=context.idea_id)


# ================================================================ budget --
def test_an_execution_a_budget_cannot_cover_is_budget_limited(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
        limit_value=Decimal(0),
    )
    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    step = advance(context)
    assert not step.ok
    assert step.failure_class is FailureClass.BUDGET_EXHAUSTED
    (outcome,) = outcomes(context)
    assert outcome.state is PrimaryOutcome.BUDGET_LIMITED
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert outcome.experiment_id == experiment.experiment_id
    assert experiment.job_id is None, "nothing ran"


# =========================================================== replication --
def test_an_independent_replication_is_verified_attested_and_measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """VERIFIED delivery, ATTESTED use, MEASURED agreement -- three findings."""

    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=design(1.0, seeds=(11,), variation="seed"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    replication = portfolio.get_experiment(
        idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
    )
    primary = portfolio.get_experiment(idea_id=context.idea_id, idea_version=1)
    # The replication tests the same frozen contract, under its own design
    # (which names the primary's) and its own plan.
    mine = sciencechain.verify_plan(portfolio, context.artifacts, replication)
    theirs = sciencechain.verify_plan(portfolio, context.artifacts, primary)
    assert mine.contract.digest == theirs.contract.digest
    assert mine.design.payload["parent_design_digest"] == theirs.design.digest
    assert mine.plan.digest != theirs.plan.digest

    manifest_doc = json.loads(
        context.artifacts.get_text(replication.execution_manifest_artifact_id)
    )
    source = manifest_doc["capability"]["attestation_source"]
    assert source["kind"] == "capability" and source["ref"] == "synthetic.response@1"
    assert manifest_doc["capability"]["perturbation_attestation"] == ["seeds"]

    (assessment,) = portfolio.replication_assessments(
        idea_id=context.idea_id, idea_version=1
    )
    assert assessment.configuration_independent  # VERIFIED
    assert assessment.perturbation_attested  # ATTESTED
    assert assessment.agrees is True  # MEASURED

    records = {item.role: item for item in chains(context)}
    assert records[ExperimentRole.REPLICATION].admissible
    assert records[ExperimentRole.REPLICATION].agrees_with_primary is True


def test_a_replication_varying_what_the_capability_does_not_attest_gets_no_plan(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """An unsupported perturbation is refused before anything runs."""

    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=design(0.9, seeds=(7,), variation="plan"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    # The capability attests seeds, so a redesign varying them could succeed.
    assert step.failure_class is FailureClass.MODEL_OUTPUT_INVALID
    assert "replication perturbation" in step.detail
    assert "'plan'" in step.detail
    assert (
        portfolio.get_experiment(
            idea_id=context.idea_id, idea_version=1, role=ExperimentRole.REPLICATION
        )
        is None
    )
    limited = outcomes(context, ExperimentRole.REPLICATION)
    assert [item.state for item in limited] == [PrimaryOutcome.CAPABILITY_LIMITED]


def test_a_capability_that_attests_nothing_cannot_be_replicated(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    commit_manifest(science_repo, manifest(perturbations=()))
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=design(1.0, seeds=(11,), variation="seed"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert "no perturbation at all" in step.detail


def test_a_replication_that_contradicts_its_primary_is_measured_and_disclosed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A replication contradiction: both readings kept, disagreement MEASURED."""

    from research_os.portfolio.gates import evaluate
    from tests.test_portfolio_gates import _config

    commit_manifest(
        science_repo, manifest(perturbations=(("seeds", ""), ("parameter", "plan")))
    )
    context = measured(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        replication=design(0.0, seeds=(11,), variation="plan"),
    )
    step = advance(context, ExperimentRole.REPLICATION)
    assert step.ok, step.detail
    states = {item.role: item.state for item in outcomes(context) if item.receipt_id}
    assert states == {
        ExperimentRole.PRIMARY: PrimaryOutcome.SUPPORTED,
        ExperimentRole.REPLICATION: PrimaryOutcome.REFUTED,
    }
    (assessment,) = portfolio.replication_assessments(
        idea_id=context.idea_id, idea_version=1
    )
    assert assessment.agrees is False
    records = {item.role: item for item in chains(context)}
    assert records[ExperimentRole.REPLICATION].admissible
    assert records[ExperimentRole.REPLICATION].agrees_with_primary is False

    evidence = portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)
    result = evaluate(
        version=portfolio.require_version(context.idea_id),
        live_reviews=(),
        objections=(),
        evidence=evidence,
        succeeded_stages=frozenset(),
        config=_config(),
        science_chains=chains(context),
    )
    assert any(note.startswith("MEASURED:") for note in result.notes), result.notes


def test_legacy_evidence_from_an_unbound_execution_cannot_validate(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """With no manifest, the pre-v1 route still measures -- and no v1 gate accepts it."""

    _git(science_repo, "rm", "-q", "research-capabilities.yaml")
    _git(science_repo, "commit", "-qm", "no capabilities")
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    assert experiment.plan_digest is None
    assert not outcomes(context)
    (record,) = chains(context)
    assert not record.admissible
    assert record.capability_ref is None
    assert any("no frozen execution plan" in item for item in record.problems)
    kinds = {
        row.kind
        for row in portfolio.list_evidence(idea_id=context.idea_id, idea_version=1)
    }
    assert EvidenceKind.EXPERIMENT in kinds


# ================================================== pins and derivations --
def test_the_execution_runs_the_commit_its_plan_froze(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """HEAD moving between the plan and the run does not move what runs."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    version = portfolio.require_version(context.idea_id)
    experiment = empirical.design(context, version).experiment
    frozen = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    # A later commit doubles every slope. The plan bound the earlier one.
    (science_repo / "respond.py").write_text(
        RESPOND_SCRIPT.replace('plan["slope"] * x', '2 * plan["slope"] * x'),
        encoding="utf-8",
    )
    _git(science_repo, "commit", "-qam", "a later change")
    assert _git(science_repo, "rev-parse", "HEAD") != frozen.code_commit
    step = empirical.submit(context, experiment)
    assert step.ok, step.detail
    step = empirical.interpret(context, step.experiment)
    assert step.ok, step.detail
    receipt = portfolio.receipt_for_job(step.experiment.job_id)
    document = json.loads(context.artifacts.get_text(receipt.receipt_artifact_id))
    assert document["code"]["base_commit"] == frozen.code_commit
    (outcome,) = [item for item in outcomes(context) if item.receipt_id]
    assert outcome.estimate == pytest.approx(1.0, abs=0.02)


def test_a_receipt_must_name_its_experiments_plan(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    receipt = portfolio.receipt_for_job(experiment.job_id)
    job = RuntimeStore(runtime_db).create_external_job(
        project_id=runtime_project,
        executor="local",
        spec_digest=experiment.spec_digest,
        run_dir="/tmp/second",
    )
    fields = {
        key: getattr(receipt, key)
        for key in (
            "experiment_id",
            "idea_id",
            "idea_version",
            "role",
            "command",
            "command_digest",
            "spec_digest",
            "base_commit",
            "delivered_digest",
            "inputs_digest",
            "outputs_digest",
            "receipt_artifact_id",
        )
    }
    with pytest.raises(RuntimeDatabaseError):
        portfolio.record_execution_receipt(
            receipt_id="XRCT-20260101T000000Z-00000001",
            job_id=job.job_id,
            action_id=None,
            run_id=None,
            work_id=None,
            exit_code=0,
            manifest_artifact_id=None,
            manifest_digest=None,
            parent_receipt_id=None,
            plan_digest=None,
            **fields,
        )


def test_a_chain_whose_contract_the_analysis_does_not_derive_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The frozen contract object must be the one the frozen analysis derives."""

    from research_os.portfolio.contracts import AnalysisSpec

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    (experiment,) = portfolio.list_experiments(idea_id=context.idea_id)
    chain = sciencechain.verify_plan(portfolio, context.artifacts, experiment)
    version = portfolio.require_version(context.idea_id)
    analysis = AnalysisSpec.model_validate(ANALYSIS)
    sciencechain.verify_plan(
        portfolio, context.artifacts, experiment, version=version, analysis=analysis
    )
    # The same chain shape, over a contract saying a different estimand.
    other = {**dict(chain.contract.payload), "estimand": "something else entirely"}
    contract = sciencechain.freeze(context, ScienceObjectKind.CONTRACT, other)
    design_payload = {
        **dict(chain.design.payload),
        "contract_digest": contract.object_digest,
    }
    design_row = sciencechain.freeze(
        context, ScienceObjectKind.DESIGN, design_payload, parent=contract.object_digest
    )
    plan_payload = {
        **dict(chain.plan.payload),
        "design_digest": design_row.object_digest,
        "contract_digest": contract.object_digest,
    }
    plan = sciencechain.freeze(
        context,
        ScienceObjectKind.PLAN,
        plan_payload,
        parent=design_row.object_digest,
        capability=(chain.plan.row.capability_ref, chain.plan.row.capability_digest),
        spec_digest=experiment.spec_digest,
    )
    forged = experiment.model_copy(update={"plan_digest": plan.object_digest})
    sciencechain.verify_plan(portfolio, context.artifacts, forged)  # shape holds
    with pytest.raises(sciencechain.ChainError, match="derive"):
        sciencechain.verify_plan(
            portfolio, context.artifacts, forged, version=version, analysis=analysis
        )


def test_an_analysis_without_a_population_keeps_the_digest_it_had() -> None:
    """Adding the field moved no digest frozen before it existed."""

    import hashlib

    from research_os.portfolio import scicontract
    from research_os.portfolio.contracts import AnalysisSpec

    legacy = {key: value for key, value in ANALYSIS.items() if key != "population"}
    spec = AnalysisSpec.model_validate(legacy)
    payload = spec.model_dump(mode="json")
    payload.pop("population")
    # Nor did the execution shape, added after it (docs/SCIENCE_EXECUTION.md
    # §2a): absent, it is omitted from the digest the same way.
    payload.pop("execution_shape")
    expected = (
        "panalysis-v1:"
        + hashlib.sha256(scicontract.canonical_bytes(payload)).hexdigest()
    )
    assert scicontract.analysis_digest(spec) == expected
    stated = AnalysisSpec.model_validate(ANALYSIS)
    assert scicontract.analysis_digest(stated) != expected


def test_validation_reads_only_the_bytes_the_runner_hashed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """Result validation names bytes by the runner's digest, on its own."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db),
    )
    version = portfolio.require_version(context.idea_id)
    experiment = empirical.design(context, version).experiment
    step = empirical.submit(context, experiment)
    assert step.ok, step.detail
    chain = sciencechain.verify_plan(portfolio, context.artifacts, step.experiment)
    workspace = Path(step.experiment.workspace_path)
    data = (workspace / "results" / "response.json").read_bytes()
    import hashlib

    good = [("results/response.json", hashlib.sha256(data).hexdigest(), len(data))]
    assert sciencechain.validate_result(
        chain, workspace=workspace, recorded_outputs=good
    ).ok
    wrong = [("results/response.json", "0" * 64, len(data))]
    check = sciencechain.validate_result(
        chain, workspace=workspace, recorded_outputs=wrong
    )
    assert not check.ok and check.reason == "result_changed"


def test_a_reading_with_no_recorded_outcome_is_not_an_intact_chain(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute("delete from science_outcomes")
    (record,) = chains(context)
    assert not record.chain_intact
    assert any("no system-computed outcome" in item for item in record.problems)


def test_an_implementation_repair_is_a_new_plan_of_the_same_design(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """A repair changes implementation fields only, and is a new identity."""

    context = context_for(
        portfolio,
        runtime_db,
        runtime_project,
        science_repo,
        tmp_path,
        router(runtime_db, primary=design(1.0, mode="crash")),
    )
    step = advance(context)
    assert not step.ok
    failed = step.experiment
    assert failed.state is ExperimentState.OPERATIONALLY_FAILED
    repaired = empirical.repair_implementation(context, failed, timeout_seconds=90)
    assert repaired.plan_digest and repaired.plan_digest != failed.plan_digest
    old = portfolio.get_science_object(failed.plan_digest)
    new = portfolio.get_science_object(repaired.plan_digest)
    assert new.parent_digest == old.parent_digest, "the same frozen design"
    assert new.capability_digest == old.capability_digest
    chain = sciencechain.verify_plan(portfolio, context.artifacts, repaired)
    assert chain.plan.payload["implementation"]["timeout_seconds"] == 90


def test_a_governed_designer_is_shown_only_what_a_capability_declares(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    """The analysis designer sees the typed catalogue; the designer, backed commands."""

    context = measured(portfolio, runtime_db, runtime_project, science_repo, tmp_path)
    analysis, design_request = context.models.requests
    assert "DECLARED CAPABILITIES" in analysis.prompt
    assert "respond-raw" not in analysis.prompt
    assert "respond: " not in analysis.prompt, "no raw command catalogue"
    assert "respond: Draw a seeded linear response" in design_request.prompt
    assert "respond-raw" not in design_request.prompt


def test_the_operator_can_check_a_manifest_against_the_host_before_a_run(
    runtime_project: str,
    science_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from research_os.experiment import commands as experiment_cli

    monkeypatch.setattr(
        experiment_cli, "_project", lambda _value: (runtime_project, science_repo)
    )
    args = argparse.Namespace(project="x", config=None, commit=None)
    assert experiment_cli._capabilities(args) == 0
    out = capsys.readouterr().out
    assert "synthetic.response@1" in out and "OK" in out and "rcap-v1:" in out
    commit_manifest(science_repo, manifest(command="undeclared"))
    assert experiment_cli._capabilities(args) == 1
    assert "NOT USABLE" in capsys.readouterr().out
