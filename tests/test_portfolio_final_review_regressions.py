"""What the final hostile release review reproduced, and what now holds instead.

Three independent reviewers were asked one question of the frozen build: find
a concrete path by which it can falsely create, attribute, freeze, execute,
interpret, review, promote, or recursively continue scientific evidence while
appearing valid. Each test here is one of their reproductions, run through
the production code they used, with the assertion turned round.

The output-containment and wedge findings have files of their own
(``test_portfolio_output_containment.py``, ``test_portfolio_tick.py``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from research_os.automation import gitutil
from research_os.portfolio import litintel, runner
from research_os.portfolio.allocation import CURATE
from research_os.portfolio.config import load_config
from research_os.portfolio.curator import BANK_ROOT, snapshot
from research_os.portfolio.models import (
    ActionStatus,
    AdjudicationType,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentState,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    QualityTier,
    RequestBasis,
    RequestState,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import ModelCallStatus, WorkStatus
from research_os.runtime.queue import WorkQueue
from research_os.runtime.refs import AUTONOMOUS_BANK_BRANCH
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_capsule
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_curator_lifecycle import _daemon, _drain
from tests.test_portfolio_empirical import CRASHING_SCRIPT
from tests.test_portfolio_literature_intel import WORKS, FixtureCorpus
from tests.test_portfolio_literature_intel import _context as _lit_context
from tests.test_portfolio_scientific_contract import (
    GRID_SCRIPT,
    _analysis_answer,
    _design_answer,
    _router,
    grid_repo,
)
from tests.test_portfolio_scientific_contract import _advance as _contract_advance
from tests.test_portfolio_scientific_contract import _context as _contract_context
from tests.test_portfolio_scientific_contract import _idea as _contract_idea
from tests.test_portfolio_tick import _tick

__all__ = [
    "grid_repo",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]


def _job(runtime_db: Database, project: str, tag: str) -> str:
    return (
        RuntimeStore(runtime_db)
        .create_external_job(
            project_id=project, executor="local", spec_digest=tag * 64, run_dir="/x"
        )
        .job_id
    )


# ---------------------------------------- a revision is a different claim --
def test_a_page_counts_only_the_measurements_of_the_version_it_shows(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """v1 was measured and replicated; v2 is a different claim, never measured."""

    idea, _ = seed_idea(portfolio, runtime_project)
    for kind, tag in ((EvidenceKind.EXPERIMENT, "a"), (EvidenceKind.REPLICATION, "b")):
        portfolio.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=kind,
            strength=EvidenceStrength.SUPPORTS,
            summary="v1 measurement",
            job_id=_job(runtime_db, runtime_project, tag),
        )
    portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(
            title="A different claim",
            core_idea="Something else entirely.",
            falsifier="Refuted if a retrieved paper already states it.",
        ),
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.HUMAN_READY)

    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/bank/HUMAN_READY.md"]
    assert "A different claim" in page
    assert "executions performed: 0" in page
    assert "2 supporting" not in page


def test_revising_a_validated_idea_returns_it_to_investigating(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """Through the production revision path, `run_discover`."""

    runtime = RuntimeStore(runtime_db)
    idea, _ = seed_idea(
        portfolio,
        runtime_project,
        adjudication_types=[AdjudicationType.EMPIRICAL],
        research_question="Does method A beat method B on dataset D?",
    )
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.EXPERIMENT,
        strength=EvidenceStrength.SUPPORTS,
        summary="v1 measured: A beats B",
        job_id=_job(runtime_db, runtime_project, "c"),
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    router = ScriptedRouter(
        answers={
            "scientific_discovery": {
                "can_be_made_precise": True,
                "refined": {
                    "title": "a different claim",
                    "research_question": "Does method C beat method B on dataset E?",
                    "core_idea": "C exploits structure E has and D lacks.",
                    "mechanism": "C prunes by a bound that is tight on E only.",
                    "falsifier": "Exhibit E instances where B beats C.",
                },
            }
        },
        store=runtime,
    )
    context = runner.TrackContext(
        config=load_config(),
        portfolio=portfolio,
        runtime=runtime,
        models=router,
        artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
        project_id=runtime_project,
        idea_id=idea.idea_id,
        run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
    )
    outcome = runner.run_discover(context, runner.build_snapshot(context))
    assert outcome.ok, outcome.detail

    after = portfolio.require_idea(idea.idea_id)
    assert after.current_version == 2
    assert after.status is IdeaStatus.INVESTIGATING
    assert not runner._evaluate(context).at_least(QualityTier.VALIDATED)
    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/bank/VALIDATED.md"]
    assert "Does method C beat method B on dataset E?" not in page


# ------------------------------------------------ a rerun is disclosed --
def _commit(repo: Path, script: str, message: str) -> None:
    (repo / "grid.py").write_text(script, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True
    )


def test_a_reading_after_one_operational_failure_says_so(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """One failure and a passing rerun of the same preregistration.

    The disclosure fired only for `attempts > 1`, and `attempts` counts the
    recorded failures -- so the commonest case, the one the code's own
    comment warns can turn "retry until it runs" into "retry until it
    passes", was not disclosed at all.
    """

    _commit(grid_repo, CRASHING_SCRIPT, "break it")
    idea = _contract_idea(portfolio, runtime_project)
    context = _contract_context(
        portfolio,
        runtime_db,
        tmp_path,
        runtime_project,
        idea,
        _router(
            runtime_db,
            analysis=_analysis_answer(),
            design=_design_answer([1, 2, 4], [1, 3]),
        ),
        grid_repo,
    )
    first = _contract_advance(context)
    assert not first.ok
    assert first.experiment.state is ExperimentState.OPERATIONALLY_FAILED
    assert first.experiment.attempts == 1

    _commit(grid_repo, GRID_SCRIPT, "fix it")
    second = _contract_advance(context)
    assert second.ok, second.detail
    assert second.conclusion is EmpiricalConclusion.SUPPORTS
    (row,) = [
        item
        for item in portfolio.list_evidence(idea_id=idea)
        if item.evidence_id == second.evidence_id
    ]
    assert "executed 2 time(s)" in row.summary
    assert "the earlier 1 attempt(s) failed operationally" in row.summary


# -------------------------------- a quotation is evidence from its source --
def test_a_quotation_is_credited_to_the_work_it_was_found_in(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    runtime = RuntimeStore(runtime_db)
    origin = runtime.record_model_call(
        provider="claude", role="blind_explorer", status=ModelCallStatus.OK, model="o"
    )
    idea, version = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(adjudication_types=[AdjudicationType.NOVELTY_OR_LITERATURE]),
        origin_role="blind_explorer",
        origin_call_id=origin.call_id,
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    request = litintel.ask(
        portfolio,
        project_id=runtime_project,
        idea_id=idea.idea_id,
        version=version.version,
        question="q",
        source_ref=f"novelty:{idea.idea_id}:v1",
        basis=RequestBasis.LITERATURE,
    )
    quote = "reach the same optimum by a different support path"
    assert quote in " ".join(WORKS["openalex:W11"]).lower()
    assert quote not in " ".join(WORKS["openalex:W10"]).lower()
    answer = {
        "answer": "x",
        "claims": [
            {
                "kind": "FINDING",
                "statement": "Screening provably preserves support paths.",
                "work_keys": ["openalex:W10", "openalex:W11"],
                "excerpt": quote,
                "relation_to_idea": "SUPPORTS",
            }
        ],
    }
    router = ScriptedRouter(answers={"literature_reader": answer}, store=runtime)
    assert litintel.answer_request(
        _lit_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=FixtureCorpus(),
    ).ok
    (row,) = portfolio.list_evidence(idea_id=idea.idea_id, idea_version=1)
    assert row.literature_key == "openalex:W11"


# ------------------------------------ a project's repository is its own --
def test_a_bank_is_never_committed_into_another_projects_repository(
    runtime_db: Database, pg_dsn: str, tmp_path: Path
) -> None:
    """The stored path now holds another project's checkout: nothing is done in it."""

    path = tmp_path / "study"
    RuntimeStore(runtime_db).upsert_project(
        project_id="alpha-project", repo_path=str(path)
    )
    make_capsule(path, project_id="beta-project")
    store = PortfolioStore(runtime_db)
    for n in range(3):
        seed_idea(store, "alpha-project", title=f"alpha private idea {n}")
    daemon = _daemon(runtime_db, pg_dsn, tmp_path)
    WorkQueue(runtime_db).enqueue(project_id="alpha-project", kind=CURATE, payload={})
    _drain(daemon)
    assert not gitutil.branch_exists(path, AUTONOMOUS_BANK_BRANCH)
    with runtime_db.tx() as conn:
        (row,) = conn.execute(
            "select status, result from work_items where kind = %s", (CURATE,)
        ).fetchall()
    assert str(row["status"]) == str(WorkStatus.SUCCEEDED)
    assert "no resolvable repository" in str(row["result"])


# -------------------------------------- an outage is not a disposition --
def test_a_discovery_outage_leaves_the_question_open_and_the_idea_waiting(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The providers are down and the local index has nothing yet.

    Declining on that retired the idea in one attempt -- the next tick parked
    it as "novelty cannot be established here" -- which is a provider failure
    written down as a disposition.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    v = portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(title="sharpened"),
        origin_role="scientific_discovery",
        origin_stage=str(Stage.DISCOVER),
    )
    portfolio.set_adjudication_types(
        idea_id=idea.idea_id,
        version=v.version,
        types=[str(AdjudicationType.NOVELTY_OR_LITERATURE)],
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    for stage in (
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.ADJUDICATE,
        Stage.LITERATURE_AUDIT,
    ):
        action = portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=v.version,
            stage=stage,
            basis_digest=f"b:{stage}",
        )
        portfolio.complete_action(
            action_id=action.action_id, status=ActionStatus.SUCCEEDED
        )
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=v.version,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="one retrieved work",
        literature_key="openalex:W1",
    )
    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    (request,) = portfolio.list_requests(project_id=runtime_project)
    assert (
        portfolio.require_idea(idea.idea_id).operational_state
        is OperationalState.BLOCKED_DEPENDENCY
    )

    class DownRetriever:
        def retrieve(self, query):
            raise ConnectionError(
                "api.openalex.org: temporary failure in name resolution"
            )

    class EmptyIndex:
        def search(self, query, *, limit=12):
            return SimpleNamespace(query=query, entries=(), work_keys=())

    result = litintel.answer_request(
        _lit_context(
            portfolio,
            runtime_db,
            tmp_path,
            runtime_project,
            ScriptedRouter(answers={}, store=RuntimeStore(runtime_db)),
        ),
        request.request_id,
        literature=EmptyIndex(),
        retriever=DownRetriever(),
    )
    assert not result.ok
    assert result.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert portfolio.require_request(request.request_id).state is RequestState.OPEN

    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    fresh = portfolio.require_idea(idea.idea_id)
    assert fresh.status is IdeaStatus.PROMISING
    assert fresh.status is not IdeaStatus.PARKED
