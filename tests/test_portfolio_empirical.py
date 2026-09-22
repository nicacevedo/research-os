"""The empirical route: an idea that can only be settled by measuring something.

Every adjudicated idea both real projects have produced is ``empirical``, and
until this module's subject existed the evidence stage refused all of them.
What is tested here is the whole path -- design over a *declared* command, a
frozen decision rule, a contained run in a disposable worktree, arithmetic
applied to what it wrote, one version-bound evidence row -- with real
subprocesses, real Git worktrees, real content hashes and a real database.

The distinctions these tests exist to hold, each of which is a way a system
like this quietly starts manufacturing results:

- **an execution that did not happen is never evidence.** A crashed executor,
  an unreachable provider and a host that cannot contain are three operational
  failures, and none of them writes a row;
- **the rule is fixed before the number exists.** It is stored in the
  preregistration, applied by ordinary Python, and a specification that
  changed after preregistration does not run;
- **one measurement per idea version.** A replay finds the row rather than
  designing a second experiment;
- **a replication varies something.** An identical rerun is refused in code,
  not discouraged in a prompt.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from research_os.portfolio import empirical
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    AdjudicationType,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentRole,
    ExperimentState,
    IdeaOrigin,
    IdeaStatus,
    QualityTier,
    ReviewerRole,
    Stage,
)
from research_os.portfolio.stages import TrackSnapshot, select_stage
from research_os.portfolio.store import DuplicateExperimentError, PortfolioStore
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.executors import LocalExecutor
from research_os.runtime.failures import INFRASTRUCTURE, FailureClass
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.models import BudgetScope
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


# ------------------------------------------------------------- the project --
MEASURE_SCRIPT = """\
import json, pathlib, sys
seed = int(sys.argv[sys.argv.index("--seed") + 1])
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
# A deterministic "measurement": the overlap falls as the seed rises, so a
# test can choose which side of a preregistered threshold it lands on.
out.write_text(json.dumps({"summary": {"overlap": 0.9 - 0.1 * (seed % 9)}}))
print("measured")
"""

CRASHING_SCRIPT = """\
import sys
print("the node fell over", file=sys.stderr)
raise SystemExit(7)
"""

SILENT_SCRIPT = """\
print("I claim success and write nothing")
"""

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  {project}:
    default_executor: local
    commands:
      measure:
        name: measure
        description: Measure support overlap on a synthetic instance family.
        argv: ["python3", "measure.py", "--seed", "{{seed}}", "--out", "{{out}}"]
        parameters:
          - name: seed
            type: integer
            required: true
            minimum: 0
            maximum: 65535
          - name: out
            type: path
            required: true
        outputs: []
        timeout_seconds: 120
        checks: ["outputs_exist"]
"""


def _git_project(path: Path, *, script: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "tests"], cwd=path, check=True)
    (path / "measure.py").write_text(script, encoding="utf-8")
    # A capsule, so "nothing wrote under .research/" is a claim with something
    # behind it rather than a statement about an absent directory.
    capsule = path / ".research"
    capsule.mkdir()
    (capsule / "project.yaml").write_text(
        "schema_version: 1\nproject_id: measured\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


@pytest.fixture
def project_repo(
    tmp_path: Path, runtime_xdg: Path, runtime_project: str, request: Any
) -> Path:
    """A committed project whose one declared experiment is a small program."""

    script = getattr(request, "param", MEASURE_SCRIPT)
    repo = _git_project(tmp_path / "measured-project", script=script)
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=runtime_project), encoding="utf-8"
    )
    return repo


# ---------------------------------------------------------------- answers --
def design_answer(
    *,
    seed: int = 1,
    out: str = "results/run.json",
    success: tuple[str, float] = ("<=", 0.5),
    failure: tuple[str, float] = (">", 0.5),
    rule: bool = True,
    variation_kind: str = "",
    command: str = "measure",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "testable": True,
        "command": command,
        "command_parameters": {"seed": seed, "out": out},
        "seeds": [seed],
        "primary_endpoint": "support overlap between the two trajectories",
        "falsification_criterion": "identical supports at every penalty level",
        "dataset_identity": "synthetic instance family A",
    }
    if rule:
        payload["decision_rule"] = {
            "output_path": out,
            "metric_path": "summary.overlap",
            "metric_description": "fraction of shared supports",
            "success": {"comparator": success[0], "threshold": success[1]},
            "failure": {"comparator": failure[0], "threshold": failure[1]},
        }
    else:
        payload["no_decision_rule_reason"] = (
            "the trajectories differ qualitatively and no single scalar "
            "summarises the difference"
        )
    if variation_kind:
        payload["variation_kind"] = variation_kind
        payload["variation_detail"] = f"{variation_kind}: drawn from a fresh family"
    return payload


def _router(
    runtime_db: Database, *, design: dict[str, Any] | None = None, **kwargs: Any
) -> ScriptedRouter:
    answers = {"experimentalist": design if design is not None else design_answer()}
    return ScriptedRouter(answers=answers, store=RuntimeStore(runtime_db), **kwargs)


# ---------------------------------------------------------------- context --
def _idea(
    store: PortfolioStore,
    project_id: str,
    *,
    adjudication: list[Any] | None = None,
) -> str:
    idea, _version = store.create_idea(
        project_id=project_id,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            adjudication_types=adjudication or [AdjudicationType.EMPIRICAL]
        ),
        origin_role="blind_explorer",
    )
    return idea.idea_id


def _context(
    *,
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    project_id: str,
    idea_id: str,
    router: Any,
    repo: Path | None,
    executors: dict[str, Any] | None = None,
) -> Any:
    from research_os.portfolio.runner import TrackContext

    runtime = RuntimeStore(runtime_db)
    run = runtime.create_run(project_id=project_id, objective="empirical-test")
    return TrackContext(
        config=load_config(),
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=runtime),
        project_id=project_id,
        idea_id=idea_id,
        run_id=run.run_id,
        repo_path=repo,
        executors=(executors if executors is not None else {"local": LocalExecutor()}),
        ledger=InvocationLedger(runtime_db),
        budgets=BudgetLedger(runtime_db),
    )


#: The stages an idea has behind it by the time the evidence branch is what
#: runs next. `LITERATURE_AUDIT` is among them because `select_stage` reaches
#: it *before* evidence for every adjudication type, so a snapshot without it
#: is one the machine never actually sees.
REACHED_EVIDENCE: frozenset[Stage] = frozenset(
    {
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.DISCOVER,
        Stage.LITERATURE_AUDIT,
    }
)


def _with_sources(store: PortfolioStore, idea_id: str, *, count: int = 3) -> tuple:
    """Enough retrieved literature keys to clear the novelty floor.

    The audit runs before the evidence stage whatever the adjudication type,
    and a corpus that could not produce ``novelty_min_sources`` distinct keys
    stops the track there -- correctly, and before anything empirical
    happens. A test about the evidence branch therefore has to get past it.
    """

    for index in range(count):
        store.add_evidence(
            idea_id=idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.SUPPORTS,
            summary=f"the closest retrieved work differs ({index})",
            literature_key=f"openalex:W{index}",
        )
    return store.list_evidence(idea_id=idea_id, idea_version=1)


def _advance(context: Any, *, role: ExperimentRole = ExperimentRole.PRIMARY) -> Any:
    version = context.portfolio.require_version(context.idea_id)
    return empirical.advance(context, version, role=role)


# ============================================================= the happy path
def test_a_real_measurement_becomes_one_version_bound_evidence_row(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Design, run, analyse, record -- with a real subprocess in between.

    The whole point of the bridge in one test. What is asserted is not that a
    row appeared but *what is in it*: the conclusion came from arithmetic on a
    number the run wrote, the row names the execution and the exact idea
    version, and the analysis document that shows the working is addressable.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        # seed 5 -> overlap 0.4, which is <= 0.5 and not > 0.5.
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.SUPPORTS, step.detail

    experiment = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert experiment is not None
    assert experiment.state is ExperimentState.INTERPRETED
    assert experiment.command == "measure"
    assert experiment.preregistered

    evidence = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert len(evidence) == 1
    row = evidence[0]
    assert row.kind is EvidenceKind.EXPERIMENT
    assert row.strength is EvidenceStrength.SUPPORTS
    assert row.job_id == experiment.job_id
    assert row.idea_version == 1
    assert row.artifact_id == experiment.analysis_artifact_id

    # And the arithmetic is on the record, not just its conclusion.
    document = json.loads(context.artifacts.get_text(row.artifact_id))
    assert document["observed"] == pytest.approx(0.4)
    assert document["decision_rule"]["metric_path"] == "summary.overlap"
    assert document["experiment_id"] == experiment.experiment_id
    assert document["outputs"], "the run's declared output was not hashed"


def test_the_measurement_can_refute_the_idea_and_that_is_a_success(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A refutation is a working experiment, and the stage reports ``ok``.

    The failure taxonomy has no member for a refuted idea and this is where
    that has to show: the same machinery, the same prespecified rule, a value
    on the other side of it, and a ``CONTRADICTS`` row rather than anything
    that looks like a malfunction.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        # seed 1 -> overlap 0.8, which is > 0.5 and not <= 0.5.
        router=_router(runtime_db, design=design_answer(seed=1)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.CONTRADICTS
    assert step.failure_class is None
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.strength is EvidenceStrength.CONTRADICTS


def test_a_rule_that_is_satisfied_both_ways_is_inconclusive_not_support(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The positive control for the two-predicate design.

    A single success predicate makes everything that is not a success a
    refutation, and a success predicate that covers every value makes
    everything a success. Requiring the refutation condition separately is
    what makes "this rule decides nothing" detectable -- and here both hold,
    so the answer is INCONCLUSIVE and the gate gets no substantive evidence.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(
            runtime_db,
            design=design_answer(seed=5, success=("<", 99.0), failure=("<", 99.0)),
        ),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INCONCLUSIVE
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.strength is EvidenceStrength.INCONCLUSIVE


def test_a_design_with_no_machine_checkable_rule_can_only_be_insufficient(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Saying "this has no single number" is honest, recorded, and costs the top.

    The brief asks for the reason to be recorded rather than a rule
    fabricated. It is -- and the price is that arithmetic has nothing to do,
    so the strongest available conclusion is INSUFFICIENT and the idea cannot
    be validated on it.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(rule=False)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT
    experiment = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert experiment is not None
    assert not experiment.preregistered
    assert experiment.no_rule_reason
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.strength is EvidenceStrength.INCONCLUSIVE


# ================================================== one experiment, one only
def test_an_empirical_idea_version_gets_exactly_one_experiment_spec(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    _advance(context)
    _advance(context)
    _advance(context)

    experiments = portfolio.list_experiments(idea_id=idea_id, idea_version=1)
    assert len(experiments) == 1
    assert len(portfolio.list_evidence(idea_id=idea_id, idea_version=1)) == 1


def test_the_schema_refuses_a_second_experiment_for_one_version_and_role(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The positive control for the sentence above.

    The uniqueness is a unique index and not a caller's memory, so this shows
    the index refusing rather than the driver declining. A test that only
    drove the driver would pass against a build whose index had been dropped.
    """

    idea_id = _idea(portfolio, runtime_project)
    common = {
        "idea_id": idea_id,
        "idea_version": 1,
        "project_id": runtime_project,
        "role": ExperimentRole.PRIMARY,
        "command": "measure",
        "workspace_path": "/tmp/nowhere",
        "decision_rule": None,
        "no_rule_reason": "a test",
    }
    portfolio.create_experiment(
        spec_digest="a" * 64, variation_digest="b" * 64, **common
    )
    with pytest.raises(DuplicateExperimentError):
        portfolio.create_experiment(
            spec_digest="c" * 64, variation_digest="d" * 64, **common
        )


def test_replaying_the_submission_reuses_the_job_rather_than_running_twice(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Restart and replay must not produce a second execution.

    Driven through the invocation ledger, which is the mechanism: the second
    call finds a COMPLETED invocation under the same key and returns its
    stored result without asking the executor anything. The executor counts
    its own invocations, so "it did not run twice" is measured rather than
    inferred from the absence of a second row.
    """

    class CountingExecutor(LocalExecutor):
        submissions: int = 0

        def submit(self, spec, *, run_dir):  # type: ignore[override]
            type(self).submissions += 1
            return super().submit(spec, run_dir=run_dir)

    CountingExecutor.submissions = 0
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
        executors={"local": CountingExecutor()},
    )

    first = _advance(context)
    assert first.ok, first.detail
    experiment = portfolio.require_experiment(first.experiment.experiment_id)

    # Force the row back to a state the driver will try to submit from, the
    # way a worker that died after the executor returned and before the row
    # was updated would leave it.
    portfolio.update_experiment(
        experiment.experiment_id, state=ExperimentState.PROPOSED, detail="replayed"
    )
    second = empirical.submit(
        context, portfolio.require_experiment(experiment.experiment_id)
    )

    assert second.ok, second.detail
    assert CountingExecutor.submissions == 1, (
        "the replay ran the experiment a second time; the invocation ledger "
        "did not stop it"
    )
    with runtime_db.tx() as conn:
        jobs = conn.execute(
            "select count(*) as n from external_jobs where spec_digest = %s",
            (experiment.spec_digest,),
        ).fetchone()
    assert jobs["n"] == 1


# ======================================================= operational failure
@pytest.mark.parametrize("project_repo", [CRASHING_SCRIPT], indirect=True)
def test_an_executor_crash_is_operational_and_never_evidence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A non-zero exit is a fact about the program, not about the idea.

    This is the single most important assertion in the file. A system that
    recorded "the experiment failed" as evidence against a hypothesis would
    reject ideas for infrastructure reasons, and the refusal has to be
    visible in the rows: no evidence at all, and a conclusion of
    OPERATIONALLY_BLOCKED on the experiment.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)

    assert not step.ok
    assert step.conclusion is EmpiricalConclusion.OPERATIONALLY_BLOCKED
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=1) == ()
    experiment = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert experiment is not None
    assert experiment.state is ExperimentState.OPERATIONALLY_FAILED
    assert experiment.conclusion is None

    # And the repository is byte-identical after a *failed* measurement too.
    # A soak that failed three experiments would otherwise leave three
    # worktrees and three branches in the researcher's repository, and the
    # diagnosis is not in them -- the logs and the frozen manifest are in the
    # run directory under the data home.
    assert not Path(experiment.workspace_path).exists()
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=project_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not any(name.startswith("automation/") for name in branches), branches
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == "", status.stdout


@pytest.mark.parametrize("project_repo", [SILENT_SCRIPT], indirect=True)
def test_a_run_that_writes_nothing_is_insufficient_rather_than_a_refutation(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Exit zero is not a result if the file that was promised is not there."""

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    assert step.conclusion is EmpiricalConclusion.INSUFFICIENT
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.strength is EvidenceStrength.INCONCLUSIVE


def test_a_provider_that_did_not_answer_is_operational_and_writes_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """An outage must not be able to impersonate a measurement.

    The design call is the one model call on this route. When it raises, the
    stage fails with a class the failure taxonomy calls infrastructure, no
    experiment row is created, and nothing is recorded about the idea.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, unavailable=True),
        repo=project_repo,
    )

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert step.failure_class in INFRASTRUCTURE
    assert portfolio.list_experiments(idea_id=idea_id) == ()
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=1) == ()


@pytest.mark.parametrize("project_repo", [CRASHING_SCRIPT], indirect=True)
def test_a_failed_run_stays_recoverable_and_is_not_redesigned(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery resubmits the same specification; it does not ask again.

    The experiment row keeps its preregistration and its digest through an
    operational failure, so the retry runs the test that was written down.
    Designing again would be a second commitment made after seeing that the
    first one failed, which is the shape of a post-hoc change.
    """

    idea_id = _idea(portfolio, runtime_project)
    router = _router(runtime_db, design=design_answer(seed=5))
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=router,
        repo=project_repo,
    )

    first = _advance(context)
    assert not first.ok
    experiment = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert experiment is not None
    assert experiment.state is ExperimentState.OPERATIONALLY_FAILED
    designs = len(router.requests_for("experimentalist"))

    # Repair the cause the way a person would, and try again. Nothing else:
    # no ledger row deleted, no state forced, no workspace cleared by hand.
    # A retry after a *recorded* operational failure is a new attempt, the
    # attempt is in the idempotency key, and the stale workspace is released
    # so the retry runs the repaired tree rather than the broken one.
    (project_repo / "measure.py").write_text(MEASURE_SCRIPT, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fix the script"], cwd=project_repo, check=True
    )

    second = _advance(context)

    assert second.ok, second.detail
    assert len(router.requests_for("experimentalist")) == designs, (
        "the retry asked for a new design instead of re-running the one that "
        "was preregistered"
    )
    again = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert again is not None
    assert again.experiment_id == experiment.experiment_id
    assert again.spec_digest == experiment.spec_digest
    assert again.state is ExperimentState.INTERPRETED


def test_a_host_with_no_executor_refuses_rather_than_reasoning_about_the_result(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    from research_os.portfolio import runner

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db),
        repo=project_repo,
        executors={},
    )
    assert context.can_execute is False
    snapshot = runner.build_snapshot(context)

    outcome = runner.run_evidence(context, snapshot)

    assert not outcome.ok
    assert outcome.failure_class is FailureClass.CAPABILITY_DENIED
    assert "execute" in outcome.detail


# ============================================================ the guard rails
def test_a_command_the_project_did_not_declare_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """No model writes a command, and the refusal is ordinary code.

    The catalogue comes from ``experiments.yaml`` under the config home,
    outside every worktree, so this is the boundary that makes the rule true
    by construction. What is checked here is that the boundary is *consulted*.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(command="rm-rf-everything")),
        repo=project_repo,
    )

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.POLICY_REFUSED
    assert "does not declare" in step.detail or "not a command" in step.detail
    assert portfolio.list_experiments(idea_id=idea_id) == ()


def test_a_decision_rule_over_a_file_the_command_never_writes_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A rule that could never evaluate is refused before the run, not after.

    Discovering after a measurement that its endpoint was unreadable leaves a
    result and no rule, which is the state in which someone reads the numbers
    and decides what they meant.
    """

    idea_id = _idea(portfolio, runtime_project)
    design = design_answer(out="results/run.json")
    design["decision_rule"]["output_path"] = "results/somewhere-else.json"
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design),
        repo=project_repo,
    )

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.POLICY_REFUSED
    assert "does not write" in step.detail


def test_a_specification_that_changed_after_preregistration_does_not_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Two hashes disagreeing is what "was this the test we said" looks like.

    The experiment row's digest and the digest of the stored preregistration
    are compared before anything is submitted. Moving one of them -- which is
    what a post-hoc edit would do -- stops the run with a missing-authority
    failure rather than measuring something nobody committed to.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = empirical.design(
        context, portfolio.require_version(idea_id), role=ExperimentRole.PRIMARY
    )
    assert step.ok, step.detail
    with runtime_db.tx() as conn:
        conn.execute(
            "update idea_experiments set spec_digest = %s where experiment_id = %s",
            ("f" * 64, step.experiment.experiment_id),
        )

    result = empirical.submit(
        context, portfolio.require_experiment(step.experiment.experiment_id)
    )

    assert not result.ok
    assert result.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=1) == ()


def test_the_canonical_repository_is_byte_identical_afterwards(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The experiment runs in a disposable worktree, never in the checkout.

    Checked three ways, because each catches something the others do not: the
    canonical fingerprint (the capsule and every ref), ``git status`` (a
    tracked file edited or an untracked one dropped), and the absence of the
    declared output from the checkout it was never supposed to be written in.
    """

    from research_os.runtime.actions.coding import canonical_fingerprint

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    experiment_prefix = ("refs/heads/automation/",)
    before = canonical_fingerprint(project_repo, owned_ref_prefixes=experiment_prefix)

    step = _advance(context)
    assert step.ok, step.detail

    after = canonical_fingerprint(project_repo, owned_ref_prefixes=experiment_prefix)
    assert after == before
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == "", status.stdout
    assert not (project_repo / "results").exists(), (
        "the experiment wrote its results into the researcher's checkout"
    )
    assert (project_repo / ".research" / "project.yaml").read_text(
        encoding="utf-8"
    ) == "schema_version: 1\nproject_id: measured\n"


def test_the_disposable_workspace_is_gone_and_its_outputs_are_not(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Throwing the worktree away must not throw the result away with it."""

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)
    assert step.ok, step.detail

    experiment = portfolio.require_experiment(step.experiment.experiment_id)
    assert not Path(experiment.workspace_path).exists()
    document = json.loads(context.artifacts.get_text(experiment.analysis_artifact_id))
    (stored,) = document["stored_outputs"]
    assert context.artifacts.verify(stored["artifact_id"])
    assert json.loads(context.artifacts.get_text(stored["artifact_id"]))["summary"][
        "overlap"
    ] == pytest.approx(0.4)


def test_a_result_artifact_is_addressed_by_its_content(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Immutability is the store's and it is structural, so this shows it.

    An artifact's id *is* the hash of its bytes. Changing the stored file
    changes nothing about what the id claims, and ``verify`` is what notices
    -- which is the property an evidence row depends on when it cites one.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)

    assert context.artifacts.verify(row.artifact_id)
    target = context.artifacts.path_for(row.artifact_id)
    target.chmod(0o600)
    target.write_text("tampered", encoding="utf-8")
    assert not context.artifacts.verify(row.artifact_id), (
        "a changed artifact still verified against its own address"
    )


# ================================================================== revision
def test_revising_the_idea_supersedes_the_measurement_of_the_old_version(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """An experiment measures one version's prediction and does not transfer.

    Two halves, and both matter. The in-flight measurement of the superseded
    text stops being something the portfolio waits for; and the evidence the
    old version did accumulate stays on the old version, so the new one's
    gate sees none of it.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    pending = empirical.design(
        context, portfolio.require_version(idea_id), role=ExperimentRole.PRIMARY
    )
    assert pending.ok, pending.detail

    portfolio.append_version(
        idea_id=idea_id,
        fields=idea_fields(
            core_idea="A materially different mechanism.",
            adjudication_types=[AdjudicationType.EMPIRICAL],
        ),
        origin_role="scientific_discovery",
    )

    stale = portfolio.require_experiment(pending.experiment.experiment_id)
    assert stale.state is ExperimentState.SUPERSEDED
    assert portfolio.get_experiment(idea_id=idea_id, idea_version=2) is None
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=2) == ()


def test_an_interpreted_measurement_survives_a_revision_as_a_record(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """What was measured was measured. Only *open* experiments are retired."""

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail

    portfolio.append_version(
        idea_id=idea_id,
        fields=idea_fields(
            core_idea="Sharpened.", adjudication_types=[AdjudicationType.EMPIRICAL]
        ),
        origin_role="scientific_discovery",
    )

    kept = portfolio.require_experiment(step.experiment.experiment_id)
    assert kept.state is ExperimentState.INTERPRETED
    assert kept.conclusion is EmpiricalConclusion.SUPPORTS


# =============================================================== replication
def test_a_replication_must_vary_something_and_an_identical_rerun_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Rerunning the same command with the same seeds is not a replication.

    Checked on the *variation* digest rather than the specification digest,
    because the workspace path is inside the second one and is named for the
    experiment -- so the two always differ and a test on them would pass on
    the directory name.
    """

    idea_id = _idea(portfolio, runtime_project)
    identical = design_answer(seed=5, variation_kind="seed")
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=ScriptedRouter(
            answers_by_prompt={
                "experiment_designer@5": design_answer(seed=5),
                "replication_designer@5": identical,
            },
            store=RuntimeStore(runtime_db),
        ),
        repo=project_repo,
    )
    first = _advance(context)
    assert first.ok, first.detail

    second = _advance(context, role=ExperimentRole.REPLICATION)

    assert not second.ok
    assert second.failure_class is FailureClass.POLICY_REFUSED
    assert "identical" in second.detail
    assert (
        portfolio.get_experiment(
            idea_id=idea_id, idea_version=1, role=ExperimentRole.REPLICATION
        )
        is None
    )


def test_a_replication_that_varies_the_seed_produces_its_own_execution(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The row the HUMAN_READY gate asks for: a REPLICATION naming its own job.

    And it is not simply the first interpretation quoted again. The second
    measurement has its own specification, its own execution, its own
    analysis and its own design call -- which is what
    ``gates._replication_met`` requires when it asks for a source distinct
    from the work being replicated.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=ScriptedRouter(
            answers_by_prompt={
                "experiment_designer@5": design_answer(seed=5),
                "replication_designer@5": design_answer(
                    seed=14, out="results/replication.json", variation_kind="seed"
                ),
            },
            store=RuntimeStore(runtime_db),
        ),
        repo=project_repo,
    )
    first = _advance(context)
    assert first.ok, first.detail

    second = _advance(context, role=ExperimentRole.REPLICATION)

    assert second.ok, second.detail
    primary = portfolio.require_experiment(first.experiment.experiment_id)
    replication = portfolio.require_experiment(second.experiment.experiment_id)
    assert replication.variation_digest != primary.variation_digest
    assert replication.job_id != primary.job_id
    assert replication.origin_call_id not in {None, primary.origin_call_id}

    rows = {
        item.kind: item
        for item in portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    }
    assert EvidenceKind.REPLICATION in rows
    assert rows[EvidenceKind.REPLICATION].job_id == replication.job_id
    assert rows[EvidenceKind.REPLICATION].source_call_id == replication.origin_call_id


def test_the_replication_designer_is_not_shown_what_the_first_one_concluded(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """A replicator that read the conclusion is producing a second opinion."""

    idea_id = _idea(portfolio, runtime_project)
    router = ScriptedRouter(
        answers_by_prompt={
            "experiment_designer@5": design_answer(seed=5),
            "replication_designer@5": design_answer(
                seed=14, out="results/replication.json", variation_kind="seed"
            ),
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=router,
        repo=project_repo,
    )
    _advance(context)
    _advance(context, role=ExperimentRole.REPLICATION)

    (request,) = router.requests_for_prompt("replication_designer@5")
    assert "SUPPORTS" not in request.prompt
    assert "0.4" not in request.prompt
    assert "deliberately not shown" in request.prompt


def test_replication_runs_only_when_the_policy_asks_for_it(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The replicate stage is reachable only from VALIDATED, and only once.

    A deterministic property of the stage machine, asserted on the machine
    rather than by driving a track: `STAGE_MINIMUM_STATUS` puts REPLICATE
    behind VALIDATED, so an idea that has not earned that tier never buys a
    second measurement.
    """

    from research_os.portfolio.config import STAGE_MINIMUM_STATUS

    assert STAGE_MINIMUM_STATUS[Stage.REPLICATE] == "VALIDATED"
    idea_id = _idea(portfolio, runtime_project)
    version = portfolio.require_version(idea_id)
    snapshot = TrackSnapshot(
        status=IdeaStatus.INVESTIGATING,
        version=version,
        succeeded_stages=REACHED_EVIDENCE,
        evidence=_with_sources(portfolio, idea_id),
        revision_count=1,
    )
    stage, _why = select_stage(snapshot, load_config())
    assert stage is not Stage.REPLICATE


# ==================================================================== budget
def test_a_submission_settles_its_reservation_and_a_refusal_releases_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Reserved, then settled or released -- never neither.

    A routing failure leaking its reservation is a defect this layer has
    already had once: the portfolio eventually reports exhausted money it
    never spent. Measured on the project budget, because that is the scope a
    leak accumulates in.
    """

    budgets = BudgetLedger(runtime_db)
    budgets.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
        limit_value=Decimal(10),
    )
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)
    assert step.ok, step.detail

    record = budgets.get(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
    )
    assert record is not None
    assert record.reserved == Decimal(0), "the reservation was never settled"
    assert record.spent == Decimal(1)


def test_a_submission_that_could_not_start_releases_what_it_reserved(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The other half, and the one that leaks if nobody checks it."""

    from research_os.runtime.executors import ExecutorError

    class BrokenExecutor(LocalExecutor):
        def submit(self, spec, *, run_dir):  # type: ignore[override]
            raise ExecutorError("the executor could not start anything")

    budgets = BudgetLedger(runtime_db)
    budgets.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
        limit_value=Decimal(10),
    )
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
        executors={"local": BrokenExecutor()},
    )

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.EXECUTOR_FAILED
    record = budgets.get(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
    )
    assert record is not None
    assert record.reserved == Decimal(0), "the reservation leaked"
    assert record.spent == Decimal(0), "a submission that never happened was charged"


# =========================================================== the stage machine
def test_an_interpreted_measurement_that_settles_nothing_ends_the_track(
    portfolio: PortfolioStore,
    runtime_project: str,
    runtime_db: Database,
    tmp_path: Path,
) -> None:
    """The eighth loop: an evidence stage that would be selected forever.

    An INCONCLUSIVE measurement leaves the evidence requirement unmet, and
    without this rule `select_stage` would choose EVIDENCE again on every
    tick, find the interpreted experiment, conclude the same thing and change
    nothing -- at no cost, indefinitely, with the idea reported as active.
    """

    idea_id = _idea(portfolio, runtime_project)
    version = portfolio.require_version(idea_id)
    sources = _with_sources(portfolio, idea_id)
    experiment = portfolio.create_experiment(
        idea_id=idea_id,
        idea_version=1,
        project_id=runtime_project,
        role=ExperimentRole.PRIMARY,
        command="measure",
        spec_digest="a" * 64,
        variation_digest="b" * 64,
        workspace_path="/tmp/nowhere",
        decision_rule={"metric_path": "x"},
        no_rule_reason=None,
    )
    # An interpreted experiment names its execution and its analysis --
    # `idea_experiments_running_ck` and `idea_experiments_interpreted_ck` --
    # so the fixture supplies both rather than working around them.
    job = RuntimeStore(runtime_db).create_external_job(
        project_id=runtime_project,
        executor="local",
        spec_digest="a" * 64,
        run_dir="/tmp/nowhere",
    )
    reached = TrackSnapshot(
        status=IdeaStatus.INVESTIGATING,
        version=version,
        succeeded_stages=REACHED_EVIDENCE,
        evidence=sources,
        revision_count=1,
        experiments=(experiment,),
    )
    # Before the measurement is read, the evidence stage is what runs next --
    # the proposed experiment on its own does not end anything.
    assert select_stage(reached, load_config())[0] is Stage.EVIDENCE

    artifacts = FilesystemArtifactStore(
        tmp_path / "artifacts", store=RuntimeStore(runtime_db)
    )
    analysis = artifacts.put_text("{}", media_type="application/json")
    interpreted = portfolio.update_experiment(
        experiment.experiment_id,
        state=ExperimentState.INTERPRETED,
        job_id=job.job_id,
        analysis_artifact_id=analysis.artifact_id,
        conclusion=EmpiricalConclusion.INCONCLUSIVE,
        detail="read",
    )
    assert interpreted.state is ExperimentState.INTERPRETED

    stage, why = select_stage(
        TrackSnapshot(
            status=reached.status,
            version=version,
            succeeded_stages=reached.succeeded_stages,
            evidence=sources,
            revision_count=1,
            experiments=(interpreted,),
        ),
        load_config(),
    )
    assert stage is None
    assert "ran and was read" in why


def test_the_review_board_cannot_run_before_the_measurement_exists(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """Reviewers read evidence, so there has to be some.

    The gate that makes the whole empirical route load-bearing: without an
    executed experiment an EMPIRICAL idea never reaches REVIEW_BOARD, so the
    board cannot be exercised by an idea that was only argued about.
    """

    idea_id = _idea(portfolio, runtime_project)
    version = portfolio.require_version(idea_id)
    snapshot = TrackSnapshot(
        status=IdeaStatus.INVESTIGATING,
        version=version,
        succeeded_stages=REACHED_EVIDENCE,
        evidence=_with_sources(portfolio, idea_id),
        revision_count=1,
    )

    stage, _why = select_stage(snapshot, load_config())

    assert stage is Stage.EVIDENCE
    assert stage is not Stage.REVIEW_BOARD


def test_evidence_from_a_measurement_unblocks_the_review_board(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The positive control for the test above, and the point of the bridge.

    The same snapshot, after a real measurement, chooses the board. Without
    it the previous test would pass against a build in which REVIEW_BOARD is
    unreachable for every reason including the wrong ones.
    """

    from research_os.portfolio import runner

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = _advance(context)
    assert step.ok and step.conclusion is EmpiricalConclusion.SUPPORTS

    _with_sources(portfolio, idea_id)
    portfolio.set_status(idea_id=idea_id, status=IdeaStatus.INVESTIGATING)
    live = runner.build_snapshot(context)
    snapshot = TrackSnapshot(
        status=IdeaStatus.INVESTIGATING,
        version=live.version,
        succeeded_stages=REACHED_EVIDENCE,
        evidence=live.evidence,
        revision_count=1,
        experiments=live.experiments,
    )

    stage, _why = select_stage(snapshot, load_config())

    assert stage is Stage.REVIEW_BOARD


def test_the_reviewer_reads_the_measurement_and_not_an_authors_reading_of_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """What reaches a reviewer is the number and the rule, never a verdict.

    There is no experiment author to have an unsupported interpretation: the
    conclusion is arithmetic, and the evidence summary a reviewer reads
    carries the metric, the thresholds fixed beforehand and the output
    digests.
    """

    from research_os.portfolio import packets

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail

    packet = packets.build_packet(
        version=portfolio.require_version(idea_id),
        evidence=portfolio.list_evidence(idea_id=idea_id, idea_version=1),
        objections=(),
    )
    (line,) = packet.evidence_block()

    assert "summary.overlap = 0.4" in line
    assert "prespecified support <= 0.5" in line
    assert "sha256:" in line
    assert "the idea is supported" not in line.lower()


# ================================================================ unit checks
@pytest.mark.parametrize(
    ("document", "path", "expected"),
    [
        ({"a": {"b": 1.5}}, "a.b", 1.5),
        ({"rows": [{"v": 2}]}, "rows.0.v", 2.0),
        ({"a": {"b": True}}, "a.b", None),
        ({"a": {"b": "1.5"}}, "a.b", None),
        ({"a": {}}, "a.b", None),
        ({"rows": []}, "rows.0", None),
        ({"a": 1}, "a.b", None),
    ],
)
def test_a_metric_is_a_number_or_it_is_nothing(
    document: Any, path: str, expected: float | None
) -> None:
    """``True`` is ``1`` in Python, and a rule that compared a flag to a
    threshold would mean something nobody wrote down."""

    assert empirical.metric_from(document, path) == expected


def test_the_workspace_path_is_a_pure_function_of_the_experiment(
    runtime_xdg: Path,
) -> None:
    """``cwd`` is inside the specification digest, so it must not move.

    A path derived from the clock or from an attempt counter would give one
    experiment a different digest on every retry, and the preregistration
    comparison would then reject every legitimate resubmission.
    """

    del runtime_xdg
    one = empirical.workspace_for("PEXP-20260921T120000Z-0a1b2c3d")
    two = empirical.workspace_for("PEXP-20260921T120000Z-0a1b2c3d")
    other = empirical.workspace_for("PEXP-20260921T120000Z-deadbeef")
    assert one == two
    assert one != other


# ====================================================== the whole route, live
EMPIRICAL_QUESTION = (
    "Do column generation and working-set methods reach the same median "
    "support overlap when both are measured over the same seeds?"
)
EMPIRICAL_FALSIFIER = (
    "Measure the median support overlap over ten seeds on a preregistered "
    "instance regime and benchmark both methods; identical medians refute it."
)


@pytest.fixture
def checkpoint_tables(pg_dsn: str) -> str:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    return pg_dsn


class ThreeSourceLiterature:
    """Enough retrieved sources to clear the novelty floor, and no more."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 12) -> Any:
        from dataclasses import dataclass as _dc

        self.queries.append(query)

        @_dc(frozen=True)
        class _Work:
            key: str
            title: str
            abstract: str

        @_dc(frozen=True)
        class _Entry:
            work: _Work
            excerpt: str = ""

        @_dc(frozen=True)
        class _Packet:
            query: str
            entries: tuple

            @property
            def work_keys(self) -> tuple[str, ...]:
                return tuple(item.work.key for item in self.entries)

        keys = ("openalex:W1", "openalex:W2", "openalex:W3")
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, f"Title of {key}", f"Abstract of {key}"))
                for key in keys[:limit]
            ),
        )


def _matrix() -> dict[str, Any]:
    return {
        "rows": [
            {
                "proposed_component": f"component {index}",
                "closest_known_result": f"the nearest result to {key}",
                "relation": "different",
                "precise_difference": "trajectories rather than optima",
                "source_key": key,
                "confidence": 0.8,
            }
            for index, key in enumerate(("openalex:W1", "openalex:W2", "openalex:W3"))
        ],
        "queries": ["one phrasing", "another phrasing"],
        "summary": "no prior result measures this",
    }


def _pass(who: str) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "summary": f"{who} found nothing to object to",
        "objections": [],
    }


def _empirical_router(runtime_db: Database) -> ScriptedRouter:
    refined = {
        key: value
        for key, value in idea_fields().items()
        if key != "adjudication_types"
    }
    refined.update(
        {
            "research_question": EMPIRICAL_QUESTION,
            "core_idea": (
                "The two methods are measured over identical seeds and their "
                "median support overlap is compared."
            ),
            "falsifier": EMPIRICAL_FALSIFIER,
            "claimed_difference": "the trajectory is measured, not the optimum",
        }
    )
    return ScriptedRouter(
        answers={
            "duplicate_adjudicator": {"verdict": "distinct", "rationale": "different"},
            "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
            "falsifier": {
                "summary": "no cheap kill found",
                "objections": [],
                "attempted": ["a subsuming theorem", "a simpler explanation"],
            },
            "scientific_discovery": {
                "can_be_made_precise": True,
                "minimum_decisive_action": "measure the overlap",
                "refined": refined,
            },
            "literature_scout": _matrix(),
            "methodology_reviewer": _pass("the methodologist"),
            "novelty_reviewer": _pass("the novelty reviewer"),
            "skeptic_reviewer": _pass("the skeptic"),
            "meta_reviewer": {
                "recommendation": "HUMAN_READY",
                "summary": "all three reviewers were satisfied",
                "unresolved_disagreements": [],
            },
            "brancher": {"children": [], "relations": []},
        },
        answers_by_prompt={
            "experiment_designer@5": design_answer(seed=5),
            "replication_designer@5": design_answer(
                seed=14, out="results/replication.json", variation_kind="seed"
            ),
        },
        store=RuntimeStore(runtime_db),
        providers={
            "methodology_reviewer": "alpha",
            "novelty_reviewer": "beta",
            "skeptic_reviewer": "gamma",
        },
        models_by_role={
            "methodology_reviewer": "alpha-1",
            "novelty_reviewer": "beta-1",
            "skeptic_reviewer": "gamma-1",
        },
    )


def test_a_real_empirical_idea_traverses_experiment_evidence_and_review(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The route this whole module exists to open, driven end to end.

    Through ``advance_idea`` -- the production entry point the daemon calls --
    with the real stage machine choosing every step, a real subprocess taking
    the measurement, and the deterministic gates deciding what the rows
    permit. Nothing is inserted to reach a later stage: if the experiment did
    not run, the board is not reachable, and the assertion below would fail
    at the review rather than at the tier.
    """

    from research_os.portfolio.track import advance_idea
    from research_os.runtime.interfaces import Independence

    idea, _version = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            research_question=EMPIRICAL_QUESTION, falsifier=EMPIRICAL_FALSIFIER
        ),
        origin_role="blind_explorer",
    )
    router = _empirical_router(runtime_db)
    literature = ThreeSourceLiterature()

    trace: list[str] = []
    for _ in range(30):
        result = advance_idea(
            runtime_config=_runtime_config(pg_dsn, tmp_path),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=literature,
            repo_path=project_repo,
            executors={"local": LocalExecutor()},
        )
        trace.append(
            f"{result.stage} ok={result.ok} {(result.detail or result.reason)[:80]}"
        )
        if result.stage is None or not result.ok:
            break
    why = "\n  " + "\n  ".join(trace)

    version = portfolio.require_version(idea.idea_id)
    assert AdjudicationType.EMPIRICAL in version.adjudication_types, why

    # The measurement happened, in a disposable worktree, and was read.
    experiments = portfolio.list_experiments(
        idea_id=idea.idea_id, idea_version=version.version
    )
    primary = [item for item in experiments if item.role is ExperimentRole.PRIMARY]
    assert primary, why
    assert primary[0].state is ExperimentState.INTERPRETED, why

    # The board ran, on evidence that could only exist because it did.
    live = portfolio.live_reviews(idea_id=idea.idea_id)
    assert {item.reviewer_role for item in live} >= {
        ReviewerRole.METHODOLOGY,
        ReviewerRole.NOVELTY,
        ReviewerRole.SKEPTIC,
    }, why
    # And independently: three distinct families read the same frozen packet.
    from research_os.portfolio.gates import board_independence

    assert board_independence(live) == 3, why
    assert all(
        item.independence_vs_origin is not Independence.NONE
        for item in live
        if item.reviewer_role in _BOARD
    ), why

    # A meta-review synthesised them, and the gate decided what it was worth.
    metas = [item for item in live if item.reviewer_role is ReviewerRole.META]
    assert metas, why

    # A replication was scheduled only after VALIDATED, varied the seed, and
    # ran its own execution.
    replications = [
        item for item in experiments if item.role is ExperimentRole.REPLICATION
    ]
    assert replications, why
    assert replications[0].variation_digest != primary[0].variation_digest, why

    final = portfolio.require_idea(idea.idea_id)
    assert final.status is IdeaStatus.HUMAN_READY, why
    assert final.quality_tier is QualityTier.HUMAN_READY, why

    # The measurement did not touch the researcher's checkout.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == "", status.stdout


_BOARD = frozenset(
    {ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY, ReviewerRole.SKEPTIC}
)


def _runtime_config(pg_dsn: str, tmp_path: Path) -> Any:
    from tests.runtime_graph_helpers import make_config

    return make_config(pg_dsn, tmp_path / "artifacts")


def test_resume_returns_blocked_ideas_to_idle(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    """The command that says "the thing that was missing is here now".

    `tick._clear_blocks` lifts `BLOCKED_PROVIDER` against provider health it
    can observe and guesses at nothing else -- correctly, because neither a
    raised ceiling nor an arrived capability is a fact a tick can read. So
    the three real ideas this route unblocked would have stayed blocked
    forever: the host could run experiments and nothing told the portfolio.

    A terminal idea is not blocked, it is finished, and stays where it is.
    """

    from research_os.portfolio.models import OperationalState

    blocked = _idea(portfolio, runtime_project)
    finished = _idea(portfolio, runtime_project)
    portfolio.set_operational_state(
        idea_id=blocked, state=OperationalState.BLOCKED_EXTERNAL
    )
    portfolio.set_status(
        idea_id=finished,
        status=IdeaStatus.REJECTED,
        retire_reason="the falsifier killed it",
        revisit_if="a counterexample appears",
    )
    portfolio.set_operational_state(
        idea_id=finished, state=OperationalState.BLOCKED_EXTERNAL
    )

    count = portfolio.unblock_ideas(project_id=runtime_project)

    assert count == 1
    assert portfolio.require_idea(blocked).operational_state is OperationalState.IDLE
    assert (
        portfolio.require_idea(finished).operational_state
        is OperationalState.BLOCKED_EXTERNAL
    )


def test_a_long_explanation_is_clipped_and_a_long_endpoint_is_refused() -> None:
    """The dogfood's first real finding, as two assertions.

    Asked to design for an idea no declared command can test, the designer
    answered ``testable: false`` with a careful 2,400-character account --
    the most useful answer available -- and the contract discarded the whole
    response for being over a limit nothing had told it about. The retry
    produced the same answer and failed the same way.

    So an *explanation* is clipped: nothing reads it as evidence and losing a
    paragraph is cheaper than losing the answer. Anything that is *content*
    still refuses, because salvaging half of one of those is how an
    unsupported conclusion comes to look supported.
    """

    from research_os.portfolio.contracts import ContractError, ExperimentDesign

    long_reason = "no declared command measures this. " * 200
    design = ExperimentDesign(testable=False, untestable_reason=long_reason)
    design.check()
    assert len(design.untestable_reason) <= 4_000
    assert design.untestable_reason.endswith("[clipped]")

    with pytest.raises(Exception) as refused:
        ExperimentDesign(
            testable=True,
            command="measure",
            primary_endpoint="x" * 4_000,
            no_decision_rule_reason="none",
        )
    assert "2000" in str(refused.value)

    # And an untestable design still has to say *something*.
    with pytest.raises(ContractError, match="must say why"):
        ExperimentDesign(testable=False).check()


def test_a_resource_no_executor_reads_is_dropped_rather_than_refused() -> None:
    """The dogfood's second finding, and the same shape as the first.

    Asked for the resources its experiment needs, the designer wrote a
    *note* in a field for values -- and a length bound threw away an
    otherwise valid design, twice. What an executor reads is six keys; the
    rest reaches nothing, so carrying it would only put a paragraph of
    commentary inside the specification digest.
    """

    from research_os.portfolio.contracts import ExperimentDesign

    design = ExperimentDesign(
        testable=True,
        command="measure",
        primary_endpoint="support overlap",
        no_decision_rule_reason="none",
        resources={
            "cpus": "4",
            "machine": "a 2026 laptop; " + "the thesis's hardware is not. " * 20,
        },
    )
    design.check()

    assert design.resources == {"cpus": "4"}


def test_an_inconclusive_replication_does_not_count_as_verification(
    portfolio: PortfolioStore, runtime_project: str, runtime_db: Database
) -> None:
    """A second measurement that could not tell has verified nothing.

    Unreachable before the empirical route existed: the only writer of a
    REPLICATION row wrote one solely when its reconstruction *agreed*, so
    every such row was SUPPORTS and `_replication_met` never had to ask. A
    second execution that comes back INCONCLUSIVE is now an ordinary
    outcome, and without this it would satisfy the one requirement standing
    between VALIDATED and HUMAN_READY.
    """

    from research_os.portfolio.gates import REPLICATION_RULES, _replication_met
    from research_os.runtime.models import ModelCallStatus

    runtime = RuntimeStore(runtime_db)
    idea_id = _idea(portfolio, runtime_project)
    job = runtime.create_external_job(
        project_id=runtime_project,
        executor="local",
        spec_digest="e" * 64,
        run_dir="/tmp/nowhere",
    )
    call = runtime.record_model_call(
        provider="scripted",
        model="scripted-1",
        role="replicator",
        status=ModelCallStatus.OK,
        prompt_version="replication_designer@5",
    )
    rule = REPLICATION_RULES[AdjudicationType.EMPIRICAL]

    def _row(strength: EvidenceStrength) -> tuple:
        portfolio.add_evidence(
            idea_id=idea_id,
            idea_version=1,
            kind=EvidenceKind.REPLICATION,
            strength=strength,
            summary=f"a second execution: {strength}",
            job_id=job.job_id,
            source_call_id=call.call_id,
        )
        return portfolio.list_evidence(idea_id=idea_id, idea_version=1)

    inconclusive = _row(EvidenceStrength.INCONCLUSIVE)
    assert not _replication_met(rule, inconclusive, {"origin-call"})

    # The positive control: the same row, with a conclusion in it, counts.
    both = _row(EvidenceStrength.SUPPORTS)
    assert _replication_met(rule, both, {"origin-call"})


def test_the_catalogue_names_the_input_files_that_exist(
    project_repo: Path, runtime_project: str
) -> None:
    """The third finding, and the one that cost a whole execution.

    The first real design this route produced chose the right command and
    then named a plan file that does not exist. It went the whole way --
    contained, in a disposable worktree, fifty-two packages installed
    offline -- and ended in `FileNotFoundError`. It was reasoning about a
    repository it had never been shown.

    Tracked files only, so nothing a previous experiment left behind can be
    named as an input, and the capsule excluded, because `.research/` is
    scientific state rather than experimental data.
    """

    (project_repo / "plans").mkdir()
    (project_repo / "plans" / "sweep.yaml").write_text("arms: []\n", encoding="utf-8")
    (project_repo / "leftover.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "plans"], cwd=project_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a plan"], cwd=project_repo, check=True)

    listed = empirical.input_candidates(project_repo)

    assert "plans/sweep.yaml" in listed
    assert "leftover.json" not in listed, "an untracked file is not an input"
    assert not any(name.startswith(".research/") for name in listed), (
        "the capsule is scientific state, not experimental data"
    )

    catalogue = empirical.command_catalogue(
        empirical.declared_commands(runtime_project), repository=project_repo
    )
    assert any("plans/sweep.yaml" in line for line in catalogue)
    assert any("may name as an INPUT" in line for line in catalogue)


def test_an_experiment_designed_by_a_retired_prompt_is_redesigned(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth wedge of the same shape, closed the way the third was.

    The portfolio already treats a review produced by a superseded prompt as
    stale, because it answered a question no longer being asked. A design is
    the same object under a different name, and without this an improved
    designer could never reach an idea whose experiment the old one had
    already written: every attempt resubmits the old specification until the
    stage ceiling stops the idea.

    Found by improving the prompt during the dogfood and watching the fix be
    unable to reach the one idea it was for.
    """

    from research_os.portfolio.prompts import TEMPLATES

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    first = empirical.design(
        context, portfolio.require_version(idea_id), role=ExperimentRole.PRIMARY
    )
    assert first.ok, first.detail
    assert first.experiment.prompt_version == TEMPLATES["experiment_designer"].identity

    # The prompt is superseded, exactly as `CURRENT_REVIEW_PROMPTS` is
    # monkeypatched elsewhere to retire a reviewer's.
    superseded = replace(TEMPLATES["experiment_designer"], version=99)
    monkeypatch.setitem(TEMPLATES, "experiment_designer", superseded)

    step = _advance(context)

    assert step.ok, step.detail
    retired = portfolio.require_experiment(first.experiment.experiment_id)
    assert retired.state is ExperimentState.SUPERSEDED
    assert "superseded" in (retired.detail or "")
    live = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert live is not None
    assert live.experiment_id != retired.experiment_id
    assert live.prompt_version == superseded.identity
    assert live.state is ExperimentState.INTERPRETED, "the successor did not run"


def test_an_interpreted_experiment_is_not_redesigned_when_its_prompt_retires(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What was measured was measured.

    The positive control for the rule above, and the limit on it: staleness
    is about a *commitment not yet settled*. A reading that has been taken
    stands whatever its designing prompt has since become, and re-measuring
    it because a prompt's wording changed would be a second bite at one
    question.
    """

    from research_os.portfolio.prompts import TEMPLATES

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    done = _advance(context)
    assert done.ok and done.conclusion is EmpiricalConclusion.SUPPORTS

    monkeypatch.setitem(
        TEMPLATES,
        "experiment_designer",
        replace(TEMPLATES["experiment_designer"], version=99),
    )
    again = _advance(context)

    assert again.ok
    assert "already been interpreted" in again.detail
    assert len(portfolio.list_experiments(idea_id=idea_id, idea_version=1)) == 1


def test_a_diagnostic_idea_records_its_run_as_an_observation_of_the_program(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The kind follows the question, and DIAGNOSTIC asks a different one.

    `EVIDENCE_RULES[DIAGNOSTIC]` asks for "an observation of this
    implementation, addressable as an artifact" -- `CODE` or `INSPECTION`,
    not `EXPERIMENT`. Routing a diagnostic idea through this bridge and then
    writing its result as an experiment would run the measurement, spend the
    execution, and produce a row its own gate cannot read.

    The same entry is careful about what that row is worth, and so is this
    test: a diagnostic observation is a fact about the program and never on
    its own a reason to close a scientific target.
    """

    idea_id = _idea(
        portfolio, runtime_project, adjudication=[AdjudicationType.DIAGNOSTIC]
    )
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.kind is EvidenceKind.CODE
    assert row.job_id is not None, "an observation of a program names the run"


def test_an_idea_that_is_empirical_and_diagnostic_records_an_experiment(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The positive control, and the limit of the rule above.

    Declaring a second adjudication type adds requirements and never removes
    them. An idea that is both has asked for two kinds of evidence, one run
    produces one, and the union rule is what stops that one row being
    counted as both.
    """

    idea_id = _idea(
        portfolio,
        runtime_project,
        adjudication=[AdjudicationType.EMPIRICAL, AdjudicationType.DIAGNOSTIC],
    )
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    step = _advance(context)

    assert step.ok, step.detail
    (row,) = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert row.kind is EvidenceKind.EXPERIMENT


def test_an_experiment_that_changed_the_checkout_is_refused_not_retried(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one failure that must not be retried, because retrying repeats it.

    A declared command runs project code with model-chosen parameter values.
    Containment and the disposable worktree are what stop it reaching the
    researcher's checkout; the fingerprint is what *notices* if they did not,
    and on a host with no containment they are all there is.

    Simulated by moving the fingerprint, because the real thing cannot be
    staged here -- the sandbox denies it and the worktree hides it, which is
    the point. What is under test is the response: `POLICY_REFUSED`, which
    the failure taxonomy makes terminal, and no evidence.
    """

    from research_os.runtime.actions import coding

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )

    real = coding.canonical_fingerprint
    seen: list[int] = []

    def drifting(repo: Path, **kwargs: Any) -> dict[str, str]:
        found = dict(real(repo, **kwargs))
        seen.append(1)
        if len(seen) > 1:
            found[".research/project.yaml"] = "something else entirely"
        return found

    monkeypatch.setattr(coding, "canonical_fingerprint", drifting)

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.POLICY_REFUSED
    from research_os.runtime.failures import Response, response_for

    assert response_for(step.failure_class) is Response.FAIL_PERMANENTLY
    assert "changed the canonical checkout" in step.detail
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=1) == ()


def test_a_host_that_cannot_contain_refuses_rather_than_running_uncontained(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """Containment stays mandatory, and refusing is what mandatory means.

    A declared command is the researcher's argument vector with validated
    parameter values in it, which is a weaker exposure than the coding
    pipeline's -- and it is still project code executing with model-chosen
    parameters, which is why `build_executors` raises the mode to
    ``required`` at high autonomy, where nobody is watching.

    What is under test is the response when the host cannot provide it.
    `CAPABILITY_DENIED` is terminal in the failure taxonomy -- no repair
    makes a kernel offer user namespaces -- the reservation is released, and
    no evidence is written. Running uncontained instead is the outcome this
    whole arrangement exists to not have.
    """

    from research_os.runtime.executors import ContainmentUnavailableError
    from research_os.runtime.failures import Response, response_for

    class UncontainableExecutor(LocalExecutor):
        def submit(self, spec, *, run_dir):  # type: ignore[override]
            raise ContainmentUnavailableError(
                "this host cannot provide OS-level containment and the "
                "configured policy requires it"
            )

    budgets = BudgetLedger(runtime_db)
    budgets.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
        limit_value=Decimal(10),
    )
    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
        executors={"local": UncontainableExecutor()},
    )

    step = _advance(context)

    assert not step.ok
    assert step.failure_class is FailureClass.CAPABILITY_DENIED
    assert response_for(step.failure_class) is Response.FAIL_PERMANENTLY
    assert step.conclusion is EmpiricalConclusion.OPERATIONALLY_BLOCKED
    assert portfolio.list_evidence(idea_id=idea_id, idea_version=1) == ()
    record = budgets.get(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.WORK_ITEMS,
    )
    assert record is not None
    assert record.reserved == Decimal(0) and record.spent == Decimal(0)


def test_the_catalogue_shows_an_output_schema_without_showing_its_values(
    project_repo: Path, runtime_project: str
) -> None:
    """The third instance of one mistake, and the designer named it itself.

    Refusing an idea on 2026-09-22 it wrote: "its output schema is not
    declared, so any metric_path I named inside the file I create would be a
    guess rather than a preregistration." That is the correct standard, and
    it made every declared command unusable because nothing told it what any
    of them writes.

    A command's declared output path comes from `experiments.yaml`, which the
    researcher owns; this reads the file at that path when the project has
    committed one from an earlier run -- the same file the command writes.

    **And it shows keys, never values.** A threshold chosen to fit a result
    that already exists is not a preregistration, so the one thing this must
    not leak is the numbers.
    """

    results = project_repo / "results"
    results.mkdir()
    (results / "run.json").write_text(
        json.dumps(
            {
                "summary": {"overlap": 0.4242, "runs": 7, "converged": True},
                "cases": [{"seconds": 1.5}],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "results"], cwd=project_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a result"], cwd=project_repo, check=True)

    commands = empirical.declared_commands(runtime_project)
    declared = commands["measure"].model_copy(update={"outputs": ["results/run.json"]})
    catalogue = empirical.command_catalogue(
        {"measure": declared}, repository=project_repo
    )
    rendered = "\n".join(catalogue)

    assert "summary.overlap" in rendered
    assert "cases.0.seconds" in rendered, "a list is described by its first element"
    assert "summary.converged" not in rendered, "a boolean is not a metric"
    assert "0.4242" not in rendered, (
        "the values leaked; a threshold fitted to an existing result is not a "
        "preregistration"
    )
    assert "7" not in rendered.split("numeric paths")[-1].replace(
        "cases.0.seconds", ""
    ).replace("summary.runs", ""), "a value leaked"


def test_no_schema_is_shown_for_an_output_the_project_never_committed(
    project_repo: Path, runtime_project: str
) -> None:
    """The positive control, and the boundary that keeps this honest.

    Only a *tracked* file counts, so a leftover from a previous experiment in
    somebody's working tree cannot describe what a command writes, and a
    command whose output nobody has committed is one the designer is told
    nothing about -- which is the truth, and is why it then refuses rather
    than guessing a path.
    """

    results = project_repo / "results"
    results.mkdir()
    (results / "run.json").write_text('{"summary": {"overlap": 0.4}}', encoding="utf-8")

    commands = empirical.declared_commands(runtime_project)
    declared = commands["measure"].model_copy(update={"outputs": ["results/run.json"]})

    rendered = "\n".join(
        empirical.command_catalogue({"measure": declared}, repository=project_repo)
    )

    assert "summary.overlap" not in rendered


def test_the_record_says_whether_the_run_was_contained_and_how_long_it_took(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
    project_repo: Path,
) -> None:
    """An invariant asserted where it should be measured cannot be checked.

    `DESIGN_INVARIANTS.md` leans on containment -- `.git` and `.research/`
    are bound read-only, so the repository is byte-identical -- and
    `SandboxMode.PREFERRED` runs uncontained on a host with no backend, so
    the two cases both exist. The permanent analysis artifact recorded
    `job.detail` under the key `containment`, which for a local run is the
    literal string "completed", and the same string again under
    `wall_clock`. The duration `submit` measures had nowhere to go.
    """

    idea_id = _idea(portfolio, runtime_project)
    context = _context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_router(runtime_db, design=design_answer(seed=5)),
        repo=project_repo,
    )
    step = _advance(context)
    assert step.ok, step.detail

    experiment = portfolio.get_experiment(idea_id=idea_id, idea_version=1)
    assert experiment is not None
    job = context.runtime.get_external_job(experiment.job_id)
    assert job is not None

    assert job.contained is not None, "the row does not know whether it contained"
    assert job.wall_clock_seconds is not None
    assert job.wall_clock_seconds >= 0

    document = json.loads(context.artifacts.get_text(experiment.analysis_artifact_id))
    assert document["containment"] != job.detail
    assert "completed" != document["resource_usage"]["wall_clock_seconds"]
    assert float(document["resource_usage"]["wall_clock_seconds"]) >= 0
    if job.contained:
        assert "UNCONTAINED" not in document["containment"]
    else:
        assert document["containment"].startswith("UNCONTAINED")


def test_an_output_too_large_to_hash_is_not_called_unproduced(
    portfolio: PortfolioStore,
    tmp_path: Path,
) -> None:
    """Two different facts, and the damning one was reported for both.

    `_collect` hashes at most `MAX_COLLECTED_OUTPUTS` declared paths and
    skips anything over `MAX_COLLECTED_BYTES`. A file dropped by either
    bound is absent from `produced`, and the note read "declared outputs
    were not produced" -- a false statement about a run that did produce
    it, attached to an INSUFFICIENT conclusion that then looks like the
    command failed.
    """

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "made.json").write_text('{"a": 1}')
    spec = SimpleNamespace(outputs=("made.json", "never-written.json"))

    from research_os.portfolio import empirical as bridge

    monkey = bridge.MAX_COLLECTED_BYTES
    try:
        bridge.MAX_COLLECTED_BYTES = 2  # smaller than the file it produced
        analysis = bridge.analyse(
            experiment=SimpleNamespace(no_rule_reason="none was fixed"),
            rule=None,
            workspace=workspace,
            spec=spec,
            exit_code=0,
        )
    finally:
        bridge.MAX_COLLECTED_BYTES = monkey

    (absent,) = [n for n in analysis.notes if n.startswith("declared outputs were not")]
    (dropped,) = [n for n in analysis.notes if "not recorded" in n]
    assert "never-written.json" in absent
    assert "made.json" not in absent, "a file on disk was called unproduced"
    assert "made.json" in dropped
