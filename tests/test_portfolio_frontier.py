"""The research frontier: recorded events become new ideas, and nothing waits.

The discovery report's §AB.7 measured the defect this file exists for:
``max_depth`` was 0 across 254 real ideas, because the only producer of a child
was the BRANCH stage and ``select_stage`` reaches it after meta-review and
replication, which nothing real ever got past. Recursive discovery was not
broken; it was unreachable.

What is held here, each through production code:

- an INSUFFICIENT measurement raises a frontier request; the tick buys a
  follow-up explorer for it through the queue; the child has a lineage edge,
  depth one, provenance naming the request, and a scientific contract of its
  own -- while the parent's contract is byte-identical;
- a falsifier's objection that asks a question does the same for a rejected
  idea, without editing it;
- ideas that are HUMAN_READY or blocked on a capability do not stop the tick
  buying unrelated work;
- every idea has a provenance, deduplication records convergence instead of
  discarding it, and provenance cannot be rewritten;
- the blind explorer is shown no seed, no idea and no hypothesis, and an idea
  it produces is admitted with no human parent;
- an idea with nothing left to run is given an explicit state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import extensions as portfolio_extensions
from research_os.portfolio import frontier, runner
from research_os.portfolio.allocation import ADVANCE_IDEA, FOLLOW_UP
from research_os.portfolio.config import load_config
from research_os.portfolio.contracts import FollowUpOutput
from research_os.portfolio.models import (
    AdjudicationType,
    EdgeKind,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentRole,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    ProvenanceBasis,
    RequestBasis,
    RequestState,
    Stage,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.daemon import Daemon
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.executors import LocalExecutor
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields, portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]

GRID_SCRIPT = """\
import json, pathlib, sys
plan = json.loads(pathlib.Path(sys.argv[sys.argv.index("--plan") + 1]).read_text())
out = pathlib.Path("results/grid.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"cells": [
    {"size": s, "difficulty": d, "iterations": 10 + 2*s + 3*d + 0.5*s*d}
    for s in plan["sizes"] for d in plan["difficulties"]]}))
"""

EXPERIMENTS_YAML = """\
schema_version: 1
limits:
  require_explicit_execute: false
projects:
  {project}:
    default_executor: local
    commands:
      grid:
        name: grid
        description: Run a size x difficulty factorial and write every cell.
        argv: ["python3", "grid.py", "--plan", "{{plan}}"]
        parameters:
          - name: plan
            type: generated
            required: true
            max_bytes: 4096
            input_schema:
              type: object
              additionalProperties: false
              required: ["sizes", "difficulties"]
              properties:
                sizes: {{type: array, minItems: 1, maxItems: 8,
                         items: {{type: number, minimum: 0, maximum: 100}}}}
                difficulties: {{type: array, minItems: 1, maxItems: 8,
                                items: {{type: number, minimum: 0, maximum: 100}}}}
        outputs: ["results/grid.json"]
        timeout_seconds: 120
        checks: []
"""

EMPIRICAL_FALSIFIER = (
    "Run the size-by-difficulty factorial and measure iteration counts; the idea "
    "is wrong if the interaction coefficient is not positive."
)


@pytest.fixture
def checkpoint_tables(pg_dsn: str) -> str:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    return pg_dsn


@pytest.fixture
def grid_repo(tmp_path: Path, runtime_xdg: Path, runtime_project: str) -> Path:
    repo = tmp_path / "grid-project"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "grid.py").write_text(GRID_SCRIPT, encoding="utf-8")
    capsule = repo / ".research"
    capsule.mkdir()
    (capsule / "project.yaml").write_text("id: grid\n", encoding="utf-8")
    (capsule / "CHARTER.md").write_text(
        "# Charter\n\nUnderstand how solver effort scales with instance size.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    config_home = Path(str(runtime_xdg)).parent / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "experiments.yaml").write_text(
        EXPERIMENTS_YAML.format(project=runtime_project), encoding="utf-8"
    )
    return repo


class Corpus:
    """Three retrieved sources for any question, so the audit can pass."""

    KEYS = ("openalex:W1", "openalex:W2", "openalex:W3")

    def search(self, query: str, *, limit: int = 12) -> Any:
        from types import SimpleNamespace

        entries = tuple(
            SimpleNamespace(
                work=SimpleNamespace(
                    key=key, title=f"Title {key}", abstract=f"On {key}"
                ),
                excerpt="",
            )
            for key in self.KEYS[:limit]
        )
        return SimpleNamespace(
            query=query, entries=entries, work_keys=tuple(e.work.key for e in entries)
        )


def _matrix(keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        "rows": [
            {
                "proposed_component": f"component {index}",
                "closest_known_result": f"nearest to {key}",
                "relation": "different",
                "precise_difference": "the interaction, not the main effect",
                "source_key": key,
                "confidence": 0.8,
            }
            for index, key in enumerate(keys)
        ],
        "queries": ["size difficulty interaction", "scaling of solver effort"],
        "summary": "nothing measures the interaction",
    }


def _analysis() -> dict[str, Any]:
    return {
        "analysable": True,
        "estimand": "the size-by-difficulty interaction in iteration count",
        "observables": [
            {
                "name": "cells",
                "source": "results/grid.json",
                "kind": "records",
                "path": "cells",
                "fields": ["size", "difficulty", "iterations"],
            }
        ],
        "reductions": [
            {
                "name": "interaction",
                "op": "ols_coefficient",
                "observable": "cells",
                "response": "iterations",
                "terms": ["size", "difficulty", "size:difficulty"],
                "coefficient": "size:difficulty",
            }
        ],
        "primary_statistic": "interaction",
        "success": {"comparator": ">", "threshold": 0.25},
        "failure": {"comparator": "<", "threshold": 0.1},
        "support": [
            {
                "observable": "cells",
                "min_records": 4,
                "min_distinct": {"size": 2, "difficulty": 2},
            }
        ],
    }


def _design(sizes: list[float], difficulties: list[float]) -> dict[str, Any]:
    return {
        "testable": True,
        "command": "grid",
        "command_parameters": {"plan": {"sizes": sizes, "difficulties": difficulties}},
        "variables": [
            {"name": "size", "role": "manipulated", "levels": sizes},
            {"name": "difficulty", "role": "manipulated", "levels": difficulties},
        ],
        "sampling": "full factorial",
        "falsification_criterion": "no positive interaction",
    }


CHILD = {
    "title": "Does the cold-start protocol pin the measurement to one size?",
    "research_question": (
        "Is iteration count's dependence on difficulty itself a function of the "
        "number of instance sizes the protocol can generate?"
    ),
    "core_idea": (
        "The measurement collapsed to a single size because the protocol only "
        "generates one; varying the generator exposes whether the interaction exists."
    ),
    "mechanism": "The generator fixes n, so size never varies within a sweep.",
    "falsifier": EMPIRICAL_FALSIFIER,
    "why_it_matters": "Without size variation no interaction can be identified.",
}


def _router(runtime_db: Database, *, design: dict[str, Any], **overrides: Any) -> Any:
    refined = {k: v for k, v in idea_fields().items() if k != "adjudication_types"}
    refined.update(
        {
            "research_question": "Does solver effort have a size-by-difficulty interaction?",
            "core_idea": "Iteration count grows super-additively in size and difficulty.",
            "falsifier": EMPIRICAL_FALSIFIER,
        }
    )

    warm = {
        **refined,
        "title": "Warm starts and inherited active sets",
        "research_question": "Is warm-starting ever slower than cold-starting?",
        "core_idea": "A warm start inherits an active set that must be unlearned.",
        "mechanism": "Stale active constraints cost extra pivots before progress.",
    }

    class Router(ScriptedRouter):
        def complete(self, request: Any) -> Any:  # type: ignore[override]
            if str(request.role) == "scientific_discovery":
                # One sharpening per idea, told apart by the idea's own text:
                # two ideas sharpened into one statement are one idea, and
                # deduplication would rightly collapse them.
                chosen = (
                    warm
                    if "Warm starts" in request.prompt
                    else {**refined, **CHILD}
                    if "cold-start protocol" in request.prompt
                    else refined
                )
                self.answers = {
                    **self.answers,
                    "scientific_discovery": {
                        "can_be_made_precise": True,
                        "minimum_decisive_action": "run the factorial",
                        "refined": chosen,
                    },
                }
            if str(request.role) == "follow_up_explorer":
                # One child per parent, told apart by the parent's text. The
                # same child proposed for two parents is one idea, recorded
                # as convergence on the second -- correctly -- and a test that
                # wants a child of each must not ask for the same one twice.
                child = (
                    {
                        **CHILD,
                        "title": "Does an inherited active set grow with size?",
                        "research_question": (
                            "Does the cost of unlearning an inherited active set "
                            "scale with the number of features?"
                        ),
                        "core_idea": "Stale constraints accumulate with dimension.",
                    }
                    if "Warm starts" in request.prompt
                    else CHILD
                )
                self.answers = {
                    **self.answers,
                    "follow_up_explorer": {
                        "children": [child],
                        "relations": ["DERIVED_FROM"],
                    },
                }
            if str(request.role) == "literature_scout":
                import re

                keys = tuple(
                    dict.fromkeys(re.findall(r"openalex:W\d+", request.prompt))
                )
                self.answers = {**self.answers, "literature_scout": _matrix(keys)}
            return super().complete(request)

    answers: dict[str, Any] = {
        "duplicate_adjudicator": {"verdict": "distinct", "rationale": "different"},
        "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
        "falsifier": {"summary": "no cheap kill", "objections": [], "attempted": ["x"]},
        "scientific_discovery": {
            "can_be_made_precise": True,
            "minimum_decisive_action": "run the factorial",
            "refined": refined,
        },
        "follow_up_explorer": {"children": [CHILD], "relations": ["DERIVED_FROM"]},
        "blind_explorer": {"candidates": [], "nothing_to_propose": "not needed here"},
        "failure_mining_explorer": {"candidates": [], "nothing_to_propose": "none"},
    }
    answers.update(overrides)
    return Router(
        answers=answers,
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: _analysis(),
            TEMPLATES["experiment_designer"].identity: design,
        },
        store=RuntimeStore(runtime_db),
    )


def _plane(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    repo: Path,
    router: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Daemon:
    """The control plane, composed with the portfolio as `researchd` is.

    Two seams, both stated: the literature index is a fixture corpus (no
    network, no credentials), and the executor is an uncontained local one,
    because containment has its own adversarial suite and a deterministic
    test must not depend on the host's user namespaces.
    """

    portfolio_extensions.register()
    monkeypatch.setattr(portfolio_extensions, "_literature", lambda: Corpus())
    monkeypatch.setattr(
        portfolio_extensions, "_executors", lambda context: {"local": LocalExecutor()}
    )
    return Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: router,
        owner="frontier-test-worker",
    )


def _run_until_idle(daemon: Daemon, *, passes: int = 60) -> None:
    for _ in range(passes):
        report = daemon.tick()
        if not report.did_something:
            return


def _trace(portfolio: PortfolioStore, runtime_db: Database, idea_id: str) -> str:
    """What happened to one idea, for an assertion message."""

    idea = portfolio.require_idea(idea_id)
    lines = [
        f"{idea.idea_id} {idea.status} {idea.operational_state} v{idea.current_version}"
    ]
    for action in portfolio.list_actions(idea_id=idea_id):
        lines.append(
            f"  {action.stage} {action.status} {action.failure_class or ''} "
            f"{(action.detail or '')[:160]}"
        )
    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select kind, status, last_error from work_items order by created_at"
        ).fetchall()
    for row in rows:
        lines.append(
            f"  work {row['kind']} {row['status']} {(row['last_error'] or '')[:160]}"
        )
    return "\n".join(lines)


def _tick(runtime_db: Database, pg_dsn: str, tmp_path: Path, project: str) -> Any:
    return tick(
        db=runtime_db,
        project_id=project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
    )


# =========================================================== recursion ---
def test_an_insufficient_measurement_opens_a_depth_one_child_with_its_own_contract(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    grid_repo: Path,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route that was unreachable, through the tick, the queue and the daemon.

    The parent's design collapses the size variable, so its frozen analysis
    reads INSUFFICIENT. That event raises a request; the portfolio buys a
    follow-up explorer for it; the child is a new idea one level deeper,
    which then gets a contract of its own. Nothing here is set by the test.
    """

    router = _router(runtime_db, design=_design([3], [1, 2, 3, 4, 5, 6]))
    daemon = _plane(runtime_db, pg_dsn, tmp_path, grid_repo, router, monkeypatch)
    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(grid_repo)
    )
    parent, _ = seed_idea(portfolio, runtime_project, falsifier=EMPIRICAL_FALSIFIER)
    unrelated, _ = seed_idea(
        portfolio,
        runtime_project,
        title="An unrelated direction",
        research_question="Is warm-starting ever slower than cold-starting?",
        core_idea="Warm starts inherit a bad active set.",
        falsifier=EMPIRICAL_FALSIFIER,
    )

    def parent_has_a_child() -> bool:
        return any(
            edge.parent_idea_id == parent.idea_id
            and edge.child_idea_id != parent.idea_id
            for edge in portfolio.edges_of(parent.idea_id)
        )

    for _ in range(60):
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
        _run_until_idle(daemon)
        if parent_has_a_child():
            break

    experiment = portfolio.get_experiment(
        idea_id=parent.idea_id,
        idea_version=portfolio.require_idea(parent.idea_id).current_version,
    )
    assert experiment is not None, "the parent was never measured:\n" + _trace(
        portfolio, runtime_db, parent.idea_id
    )
    assert experiment.conclusion is EmpiricalConclusion.INSUFFICIENT
    parent_contract = portfolio.require_contract(experiment.contract_id)

    requests = portfolio.list_requests(project_id=runtime_project)
    raised = [item for item in requests if item.source_ref == experiment.experiment_id]
    assert raised, _trace(portfolio, runtime_db, parent.idea_id)
    assert raised[0].state is RequestState.CONSUMED, (
        "\n".join(
            f"{item.request_id} {item.kind} {item.basis} {item.state} {item.source_ref} "
            f"{item.resolution}"
            for item in requests
        )
        + "\n"
        + "\n".join(
            f"{i.idea_id} d{i.depth} {i.origin} {i.status}"
            for i in portfolio.list_ideas(project_id=runtime_project)
        )
    )
    assert raised and raised[0].basis is RequestBasis.INSUFFICIENT
    assert raised[0].state is RequestState.CONSUMED

    children = [
        portfolio.require_idea(edge.child_idea_id)
        for edge in portfolio.edges_of(parent.idea_id)
        if edge.parent_idea_id == parent.idea_id
    ]
    assert children, "no child was opened"
    child = children[0]
    assert child.depth == 1
    assert child.origin is IdeaOrigin.FOLLOW_UP
    assert child.lineage_root == parent.lineage_root
    edges = [
        e for e in portfolio.edges_of(child.idea_id) if e.child_idea_id == child.idea_id
    ]
    assert edges and edges[0].parent_idea_id == parent.idea_id
    assert edges[0].kind is EdgeKind.DERIVED_FROM
    reasons = portfolio.provenance_of(child.idea_id)
    assert reasons[0].basis is ProvenanceBasis.INSUFFICIENT
    assert reasons[0].request_id == raised[0].request_id

    # The parent: settled explicitly, never edited.
    settled = portfolio.require_idea(parent.idea_id)
    assert settled.status is IdeaStatus.PARKED
    assert settled.revisit_if and "follow-up" in settled.revisit_if
    assert portfolio.require_contract(parent_contract.contract_id) == parent_contract
    assert (
        len(portfolio.list_versions(parent.idea_id))
        == portfolio.require_idea(parent.idea_id).current_version
    )

    # And the unrelated idea was worked on meanwhile.
    touched = {item.stage for item in portfolio.list_actions(idea_id=unrelated.idea_id)}
    assert Stage.FALSIFY in touched, "unrelated frontier work was starved"

    # Drive the child: it gets a contract of its own.
    for _ in range(30):
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
        _run_until_idle(daemon)
        if portfolio.live_contract(
            idea_id=child.idea_id,
            idea_version=portfolio.require_idea(child.idea_id).current_version,
            role=ExperimentRole.PRIMARY,
        ):
            break
    child_contract = portfolio.live_contract(
        idea_id=child.idea_id,
        idea_version=portfolio.require_idea(child.idea_id).current_version,
        role=ExperimentRole.PRIMARY,
    )
    assert child_contract is not None, (
        "the child never received a scientific contract:\n"
        + _trace(portfolio, runtime_db, child.idea_id)
    )
    assert child_contract.contract_id != parent_contract.contract_id
    assert child_contract.hypothesis_digest != parent_contract.hypothesis_digest


def test_a_falsifier_question_opens_a_child_of_a_rejected_idea_without_editing_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """A kill that asks a question: the question becomes a new idea."""

    from research_os.portfolio.track import advance_idea

    idea, _ = seed_idea(portfolio, runtime_project)
    router = _router(
        runtime_db,
        design=_design([1, 2], [1, 2]),
        falsifier={
            "summary": "the claim does not survive",
            "objections": [
                {
                    "severity": "FATAL",
                    "summary": "a known theorem already implies the orderings coincide",
                    "target": "CLAIM",
                    "follow_up_question": (
                        "Does the theorem still hold when the screening bound is "
                        "not monotone in the dual violation?"
                    ),
                }
            ],
            "attempted": ["a subsuming theorem"],
        },
    )
    for _ in range(6):
        result = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
        )
        if result.stage is None:
            break
    rejected = portfolio.require_idea(idea.idea_id)
    assert rejected.status is IdeaStatus.REJECTED
    (request,) = portfolio.list_requests(project_id=runtime_project)
    assert request.basis is RequestBasis.FALSIFIER_OBJECTION
    assert request.source_idea_id == idea.idea_id

    runtime = RuntimeStore(runtime_db)
    run = runtime.create_run(project_id=runtime_project, objective="follow-up")
    outcome = frontier.run_follow_up(
        frontier.FrontierContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=runtime),
            project_id=runtime_project,
            run_id=run.run_id,
        ),
        request.request_id,
    )
    assert outcome.ok, outcome.detail
    (child_id,) = outcome.created
    child = portfolio.require_idea(child_id)
    assert child.depth == 1 and child.lineage_root == idea.lineage_root
    assert (
        portfolio.provenance_of(child_id)[0].basis
        is ProvenanceBasis.FALSIFIER_OBJECTION
    )
    # The rejected parent is exactly what it was.
    after = portfolio.require_idea(idea.idea_id)
    assert after.status is IdeaStatus.REJECTED
    assert after.current_version == rejected.current_version
    assert after.retire_reason == rejected.retire_reason
    # And the prompt had no way to revise it: the explorer's contract has no
    # field for the parent, only for new children.
    assert set(FollowUpOutput.model_fields) == {
        "children",
        "relations",
        "nothing_to_propose",
    }


def test_one_event_raises_one_request_however_often_it_is_recorded(
    portfolio: PortfolioStore, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    first = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.ANOMALY,
        source_ref="PEXP-20260101T000000Z-00000001",
        question="why?",
        source_idea_id=idea.idea_id,
    )
    again = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.ANOMALY,
        source_ref="PEXP-20260101T000000Z-00000001",
        question="why, again?",
        source_idea_id=idea.idea_id,
    )
    assert again.request_id == first.request_id
    assert len(portfolio.list_requests(project_id=runtime_project)) == 1


def test_a_follow_up_is_declined_at_the_depth_bound(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    root, _ = seed_idea(portfolio, runtime_project)
    parent = root
    bound = load_config().bounds.max_lineage_depth
    for level in range(bound):
        parent, _ = portfolio.create_idea(
            project_id=runtime_project,
            origin=IdeaOrigin.FOLLOW_UP,
            fields=idea_fields(title=f"level {level}", research_question=f"q{level}?"),
            parent_idea_id=parent.idea_id,
            origin_role="follow_up_explorer",
        )
    request = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.RESULT,
        source_ref="deep",
        question="deeper?",
        source_idea_id=parent.idea_id,
    )
    router = ScriptedRouter(answers={}, store=RuntimeStore(runtime_db))
    runtime = RuntimeStore(runtime_db)
    outcome = frontier.run_follow_up(
        frontier.FrontierContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        request.request_id,
    )
    assert outcome.ok and "depth" in outcome.detail
    assert portfolio.require_request(request.request_id).state is RequestState.DECLINED
    assert router.requests == [], "a declined request costs no model call"


# ========================================================== starvation ---
def test_waiting_ideas_do_not_starve_unrelated_frontier_work(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """HUMAN_READY and a capability block wait; everything else proceeds."""

    ready, _ = seed_idea(portfolio, runtime_project, title="ready")
    portfolio.set_status(idea_id=ready.idea_id, status=IdeaStatus.HUMAN_READY)
    blocked, _ = seed_idea(
        portfolio, runtime_project, title="blocked", research_question="b?"
    )
    portfolio.set_operational_state(
        idea_id=blocked.idea_id, state=OperationalState.BLOCKED_EXTERNAL
    )
    fresh, _ = seed_idea(
        portfolio, runtime_project, title="fresh", research_question="f?"
    )
    request = frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.REVIEWER_CRITICISM,
        source_ref="IOBJ-20260101T000000Z-00000001",
        question="what confounds the timing?",
        source_idea_id=ready.idea_id,
    )

    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    kinds = {(item.kind, item.idea_id) for item in report.allocations}
    assert (ADVANCE_IDEA, fresh.idea_id) in kinds
    assert (FOLLOW_UP, None) in kinds
    assert (ADVANCE_IDEA, ready.idea_id) not in kinds
    assert (ADVANCE_IDEA, blocked.idea_id) not in kinds
    assert report.status.name == "RUNNING"
    with runtime_db.tx() as conn:
        queued = conn.execute(
            "select payload from work_items where project_id = %s", (runtime_project,)
        ).fetchall()
    assert any(row["payload"].get("request_id") == request.request_id for row in queued)


def test_every_idea_blocked_still_lets_a_follow_up_run(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """PAUSED_BLOCKED_EXTERNAL must not swallow work that needs no idea slot."""

    blocked, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_operational_state(
        idea_id=blocked.idea_id, state=OperationalState.BLOCKED_EXTERNAL
    )
    frontier.raise_request(
        portfolio,
        project_id=runtime_project,
        basis=RequestBasis.INSUFFICIENT,
        source_ref="PEXP-20260101T000000Z-00000002",
        question="what could measure this?",
        source_idea_id=blocked.idea_id,
    )
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.status.name == "RUNNING"
    assert any(item.kind == FOLLOW_UP for item in report.allocations)


# ========================================================== provenance ---
def test_every_idea_has_a_reason_and_the_reason_cannot_be_rewritten(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    (reason,) = portfolio.provenance_of(idea.idea_id)
    assert reason.basis is ProvenanceBasis.BLIND_EXPLORATION
    with (
        pytest.raises(RuntimeDatabaseError, match="append-only"),
        runtime_db.tx() as conn,
    ):
        conn.execute(
            "update idea_provenance set basis = 'HUMAN_SEED' where provenance_id = %s",
            (reason.provenance_id,),
        )
    with (
        pytest.raises(RuntimeDatabaseError, match="append-only"),
        runtime_db.tx() as conn,
    ):
        conn.execute(
            "delete from idea_provenance where provenance_id = %s",
            (reason.provenance_id,),
        )


def test_provenance_survives_deduplication_as_convergence(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """The blind explorer re-derives a seeded idea: kept as a fact, not dropped."""

    existing, _ = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.SEEDED_EXPLORER,
        fields=idea_fields(),
        origin_role="seeded_explorer",
    )
    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(
        answers={
            "blind_explorer": {
                "candidates": [
                    {
                        key: value
                        for key, value in idea_fields().items()
                        if key != "adjudication_types"
                    }
                ]
            }
        },
        store=runtime,
    )
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        "blind_explorer",
    )
    assert outcome.ok and outcome.data["duplicates"] == 1
    assert len(portfolio.list_ideas(project_id=runtime_project)) == 1
    bases = [item.basis for item in portfolio.provenance_of(existing.idea_id)]
    assert bases == [ProvenanceBasis.SEEDED_EXPLORATION, ProvenanceBasis.CONVERGENCE]
    assert "blind" in portfolio.provenance_of(existing.idea_id)[1].detail


# ================================================================ blind ---
def test_the_blind_explorer_admits_an_idea_with_no_human_parent(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Independent generation: a boundary on what it is shown, then admission."""

    from research_os.portfolio.track import advance_idea

    portfolio.add_seed(
        project_id=runtime_project, text="SEEDTEXT-the researcher's hunch"
    )
    seed_idea(portfolio, runtime_project, title="BANKTEXT an existing idea")
    runtime = RuntimeStore(runtime_db)
    candidate = {
        "title": "Screening bounds and tie-breaking",
        "research_question": "Do screening bounds change the support path under ties?",
        "core_idea": "Ties break the monotonicity screening relies on.",
        "mechanism": "Equal dual violations make the ordering arbitrary.",
        "falsifier": "Search the literature for a published tie-breaking analysis.",
    }
    router = _router(runtime_db, design=_design([1, 2], [1, 2]))
    router.answers = {**router.answers, "blind_explorer": {"candidates": [candidate]}}
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
            charter="Understand support paths of sparse solvers.",
        ),
        "blind_explorer",
    )
    assert outcome.ok, outcome.detail
    (prompt,) = [r.prompt for r in router.requests if str(r.role) == "blind_explorer"]
    assert "SEEDTEXT" not in prompt and "BANKTEXT" not in prompt
    (created,) = outcome.data["created"]
    idea = portfolio.require_idea(created)
    assert idea.origin is IdeaOrigin.BLIND_EXPLORER and idea.depth == 0
    assert portfolio.edges_of(created) == ()
    (reason,) = portfolio.provenance_of(created)
    assert (
        reason.basis is ProvenanceBasis.BLIND_EXPLORATION and reason.source_ref is None
    )

    for _ in range(6):
        advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=created,
            models=router,
            literature=Corpus(),
        )
        if portfolio.require_idea(created).status is not IdeaStatus.CANDIDATE:
            break
    assert portfolio.require_idea(created).status is IdeaStatus.PROMISING, (
        "a blind idea must be admitted by the same gates as any other"
    )


def test_a_blind_explorer_that_cites_a_source_it_was_not_shown_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    existing, _ = seed_idea(portfolio, runtime_project)
    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(
        answers={
            "blind_explorer": {
                "candidates": [
                    {
                        "title": "t",
                        "research_question": "q?",
                        "core_idea": "c",
                        "derived_from": [existing.idea_id],
                    }
                ]
            }
        },
        store=runtime,
    )
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        "blind_explorer",
    )
    assert not outcome.ok
    assert "not shown" in outcome.detail


def test_a_failure_mined_idea_is_a_child_of_the_failure_it_names(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    failed, _ = seed_idea(portfolio, runtime_project)
    portfolio.set_status(
        idea_id=failed.idea_id, status=IdeaStatus.REJECTED, retire_reason="refuted"
    )
    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(
        answers={
            "failure_mining_explorer": {
                "candidates": [
                    {
                        "title": "What the refutation revealed",
                        "research_question": "Is the hidden assumption the real driver?",
                        "core_idea": "The refutation exposed an assumption about ties.",
                        "falsifier": "Search the literature for the assumption.",
                        "derived_from": [failed.idea_id],
                    }
                ]
            }
        },
        store=runtime,
    )
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=router,
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        "failure_mining_explorer",
    )
    assert outcome.ok, outcome.detail
    (created,) = outcome.data["created"]
    child = portfolio.require_idea(created)
    assert child.depth == 1 and child.lineage_root == failed.lineage_root
    (reason,) = portfolio.provenance_of(created)
    assert reason.basis is ProvenanceBasis.FAILURE_MINING
    assert reason.source_ref == failed.idea_id


# ======================================================== continuation ---
def _thin_novelty_case(portfolio: PortfolioStore, project: str) -> Any:
    """An idea whose audit ran and found one source where three are needed."""

    from research_os.portfolio.models import ActionStatus

    idea, _ = seed_idea(
        portfolio,
        project,
        falsifier="Search the literature for a published tie-breaking analysis.",
    )
    version = portfolio.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(
            falsifier="Search the literature for a published tie-breaking analysis."
        ),
        origin_role="scientific_discovery",
        origin_stage=str(Stage.DISCOVER),
    )
    portfolio.set_adjudication_types(
        idea_id=idea.idea_id,
        version=version.version,
        types=[str(AdjudicationType.NOVELTY_OR_LITERATURE)],
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    for stage in (
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.LITERATURE_AUDIT,
    ):
        action = portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=version.version,
            stage=stage,
            basis_digest=f"b:{stage}",
        )
        portfolio.complete_action(
            action_id=action.action_id,
            status=ActionStatus.SUCCEEDED,
            operational_state=OperationalState.IDLE,
        )
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=version.version,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="one source only",
        literature_key="openalex:W9",
    )
    return idea


def test_a_thin_novelty_case_asks_the_literature_before_it_parks(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The one dead end the portfolio can act on: ask, wait, and only then park.

    The audit found fewer sources than the gate needs. That used to be where
    the idea stopped. Now it raises a targeted literature request and waits
    on it (an operational state, not a verdict); when the answer brings
    nothing, the idea is parked -- explicitly, with why and when to look
    again, rather than left unallocatable in the state it happened to be in.
    """

    idea = _thin_novelty_case(portfolio, runtime_project)

    first = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert first.settled == 1
    waiting = portfolio.require_idea(idea.idea_id)
    assert waiting.status is IdeaStatus.PROMISING
    assert waiting.operational_state is OperationalState.BLOCKED_DEPENDENCY
    (request,) = portfolio.list_requests(project_id=runtime_project)
    assert request.kind.name == "LITERATURE" and request.source_idea_id == idea.idea_id

    # Nothing can answer it on this deployment: declined, and the idea freed.
    from research_os.portfolio import litintel

    runtime = RuntimeStore(runtime_db)
    litintel.answer_request(
        frontier.FrontierContext(
            config=load_config(),
            portfolio=portfolio,
            runtime=runtime,
            models=ScriptedRouter(answers={}, store=runtime),
            artifacts=FilesystemArtifactStore(tmp_path / "a", store=runtime),
            project_id=runtime_project,
            run_id=runtime.create_run(project_id=runtime_project, objective="x").run_id,
        ),
        request.request_id,
        literature=None,
    )
    assert portfolio.require_request(request.request_id).state is RequestState.DECLINED
    second = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert second.settled == 1
    parked = portfolio.require_idea(idea.idea_id)
    assert parked.status is IdeaStatus.PARKED
    assert "retrieved source" in (parked.retire_reason or "")
    assert parked.revisit_if and "literature index" in parked.revisit_if


def test_a_meta_review_that_rejects_rejects(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """A synthesis recommending REJECT used to leave the idea where it was."""

    from decimal import Decimal

    idea, version = seed_idea(portfolio, runtime_project)
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.PROMISING)
    runtime = RuntimeStore(runtime_db)
    router = ScriptedRouter(
        answers={
            "meta_reviewer": {
                "recommendation": "REJECT",
                "summary": "the reviewers agree the effect is an artefact",
                "unresolved_disagreements": [],
                "follow_up_questions": ["Is the artefact itself worth measuring?"],
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
    outcome = runner.run_meta_review(context, runner.build_snapshot(context))
    assert outcome.ok
    assert outcome.cost_usd >= Decimal(0)
    assert portfolio.require_idea(idea.idea_id).status is IdeaStatus.REJECTED
    (request,) = portfolio.list_requests(project_id=runtime_project)
    assert request.basis is RequestBasis.REVIEWER_CRITICISM
    assert "artefact" in request.question
    del version
