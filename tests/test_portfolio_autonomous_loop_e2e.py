"""The whole autonomous loop, once, through the production control plane.

One deterministic scenario, driven only by ``tick`` (which decides and
enqueues) and ``Daemon.tick`` (which runs whatever was enqueued, through the
registered portfolio work kinds). The test sets up a repository with one
declared command, a fixture literature index and a scripted model router --
and after that inserts nothing, sets no status and calls no stage directly.
Every idea, contract, request, claim, synthesis and child below exists because
the loop produced it.

What the scenario must show, in order of the mission's loop:

1. **independent generation** -- the blind explorer, bought by the tick
   because the pool is empty, proposes the ideas; none has a human parent;
2. **analysis before design, frozen** -- each measurable idea gets a contract
   whose analysis was frozen before its design, and the designer never saw a
   threshold;
3. **INSUFFICIENT is preserved and becomes new science** -- a design that
   collapses a variable reads INSUFFICIENT under its own contract; that event
   raises a request; a follow-up child one level deeper gets a contract of
   its own; the parent's contract is byte-identical;
4. **literature intelligence feeds discovery** -- an idea whose novelty case
   is thin asks the literature; the verified reading's gap raises a request
   and becomes a new idea whose provenance names the claim;
5. **an undeclared capability is refused and the science is kept** -- the
   same idea, once its novelty is established, is not testable with the
   declared commands: its contract is capability-blocked with the frozen
   analysis intact, and a precise capability request recorded;
6. **nothing waits on it** -- unrelated ideas are worked on while it is
   blocked;
7. **gates, not models, decide status** -- an idea reaches VALIDATED through
   a three-family board and the deterministic gate;
8. **writer and referee return work to the frontier** -- a synthesis of the
   reviewed evidence is written, grounded and refereed; the referee's finding
   becomes a request and a new idea; the referee approved nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ContractState,
    EmpiricalConclusion,
    ExperimentRole,
    IdeaOrigin,
    IdeaStatus,
    OperationalState,
    ProvenanceBasis,
    RequestBasis,
    RequestKind,
    RequestState,
    SynthesisState,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import tick
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_frontier import (
    EMPIRICAL_FALSIFIER,
    _analysis,
    _design,
    _matrix,
    _plane,
    _run_until_idle,
    checkpoint_tables,
    grid_repo,
)

__all__ = [
    "checkpoint_tables",
    "grid_repo",
    "pg_dsn",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]


@pytest.fixture
def portfolio(runtime_db: Database) -> PortfolioStore:
    return PortfolioStore(runtime_db)


# ------------------------------------------------------------ the ideas --
def _idea(title: str, question: str, core: str, mechanism: str) -> dict[str, Any]:
    base = {k: v for k, v in idea_fields().items() if k != "adjudication_types"}
    base.update(
        {
            "title": title,
            "research_question": question,
            "core_idea": core,
            "mechanism": mechanism,
            "falsifier": EMPIRICAL_FALSIFIER,
        }
    )
    return base


#: Measured with one instance size: the interaction is unidentified.
COLLAPSED = _idea(
    "Super-additive solver effort",
    "Does solver effort have a size-by-difficulty interaction?",
    "Iteration count grows super-additively in instance size and difficulty.",
    "Harder instances need more pivots per added variable.",
)
#: Measured over a full grid: the interaction is there.
MEASURED = _idea(
    "Pivot cost compounds with conditioning",
    "Do ill-conditioned large instances cost more iterations than the sum of both?",
    "Conditioning and dimension compound multiplicatively in pivot counts.",
    "Each extra dimension multiplies the pivots an ill-conditioned basis needs.",
)
#: Thin prior literature, and no declared command can measure it.
WARM = _idea(
    "Warm starts and inherited active sets",
    "Is warm-starting ever slower than cold-starting on a warm path?",
    "A warm start inherits an active set that must be unlearned first.",
    "Stale active constraints cost pivots before any progress is made.",
)
CHILDREN = {
    "INSUFFICIENT": _idea(
        "A generator that varies instance size",
        "Does the interaction appear once the generator varies the number of features?",
        "The first measurement collapsed because the generator fixed dimension.",
        "A fixed generator pins size, so no interaction is identifiable.",
    ),
    "LITERATURE": _idea(
        "Active-set inheritance under non-monotone paths",
        "Does inherited active-set cost depend on whether the path is monotone?",
        "The reading found no work on non-monotone paths; measure that regime.",
        "Non-monotone paths leave more stale constraints behind.",
    ),
    "REFEREE_FINDING": _idea(
        "Controlling for the instance generator",
        "Is the compounding an artefact of the instance generator's coupling?",
        "A second generator decouples conditioning from dimension.",
        "One generator may couple the two factors by construction.",
    ),
}

CAPABILITY = {
    "testable": False,
    "untestable_reason": "no declared command runs a warm-started solver",
    "required_capability": {
        "name": "warm-start-sweep",
        "purpose": "measure iterations warm- versus cold-started on one path",
        "inputs": "a path of penalty values",
        "outputs": "results/grid.json with size, difficulty and iterations per cell",
        "why_declared_commands_do_not_suffice": "grid.py only cold-starts",
    },
}

READING = {
    "answer": "Warm starts are studied on monotone paths only.",
    "claims": [
        {
            "kind": "FINDING",
            "statement": f"Warm starts along monotone paths are studied in {key}.",
            "work_keys": [key],
            "relation_to_idea": "CONSISTENT_WITH",
        }
        for key in ("openalex:W1", "openalex:W2", "openalex:W3")
    ],
    "disagreements": [],
    "gaps": [
        {
            "statement": "No retrieved work measures warm starts on non-monotone paths.",
            "work_keys": ["openalex:W1", "openalex:W2"],
        }
    ],
}


class Corpus:
    """Three sources for any question -- except the warm-start audit, which gets one.

    The reader's question ("What published work bears on ...") finds all
    three, which is what lets the literature answer close the thin novelty
    case the audit left open.
    """

    KEYS = ("openalex:W1", "openalex:W2", "openalex:W3")

    def search(self, query: str, *, limit: int = 12) -> Any:
        from types import SimpleNamespace

        thin = "warm" in query.lower() and not query.startswith("What published")
        keys = self.KEYS[:1] if thin else self.KEYS
        entries = tuple(
            SimpleNamespace(
                work=SimpleNamespace(
                    key=key, title=f"Title {key}", abstract=f"On {key}"
                ),
                excerpt="",
            )
            for key in keys[:limit]
        )
        return SimpleNamespace(
            query=query, entries=entries, work_keys=tuple(e.work.key for e in entries)
        )


class LoopRouter(ScriptedRouter):
    """Answers every role from the prompt it is given. Holds no hidden plan.

    What each role returns depends on *which idea or event the prompt is
    about*, read from the prompt text -- the same information the real model
    would have. A follow-up explorer proposes one child per request basis and
    nothing afterwards, so the scenario is bounded by the script and not by
    the test stopping early.
    """

    proposed: set[str]
    explorer_calls: int

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        role = str(request.role)
        prompt = request.prompt
        if role == "blind_explorer":
            self.explorer_calls += 1
            self.answers["blind_explorer"] = (
                {"candidates": [COLLAPSED, MEASURED, WARM]}
                if self.explorer_calls == 1
                else {"candidates": [], "nothing_to_propose": "the pool is enough"}
            )
        elif role == "scientific_discovery":
            chosen = next(
                (
                    item
                    for item in (COLLAPSED, MEASURED, WARM, *CHILDREN.values())
                    if item["title"] in prompt
                ),
                COLLAPSED,
            )
            self.answers["scientific_discovery"] = {
                "can_be_made_precise": True,
                "minimum_decisive_action": "run the factorial",
                "refined": chosen,
            }
        elif request.prompt_version == TEMPLATES["experiment_designer"].identity:
            self.answers_by_prompt[request.prompt_version] = (
                _design([3], [1, 2, 3, 4, 5, 6])
                if COLLAPSED["title"] in prompt
                else CAPABILITY
                if WARM["title"] in prompt
                else _design([1, 2, 4], [1, 3])
            )
        elif request.prompt_version == TEMPLATES["replication_designer"].identity:
            self.answers_by_prompt[request.prompt_version] = {
                **_design([1, 3, 5], [2, 4]),
                "variation_kind": "grid",
                "variation_detail": "grid: other sizes and difficulties",
            }
        elif role == "literature_scout":
            keys = tuple(dict.fromkeys(re.findall(r"openalex:W\d+", prompt)))
            self.answers["literature_scout"] = _matrix(keys)
        elif role == "follow_up_explorer":
            basis = re.search(r"basis: ([A-Z_]+)", prompt)
            chosen = CHILDREN.get(basis.group(1) if basis else "")
            if chosen is None or chosen["title"] in self.proposed:
                self.answers["follow_up_explorer"] = {
                    "children": [],
                    "relations": [],
                    "nothing_to_propose": "this scenario asks for one child per basis",
                }
            else:
                self.proposed.add(chosen["title"])
                self.answers["follow_up_explorer"] = {
                    "children": [chosen],
                    "relations": ["DERIVED_FROM"],
                }
        elif role == "synthesizer":
            self.answers["synthesizer"] = _draft(prompt)
        elif role == "referee":
            self.answers["referee"] = {
                "verdict": "MAJOR_REVISION",
                "summary": "one instance generator carries every measurement",
                "findings": [
                    {
                        "finding_id": "F1",
                        "kind": "MISSING_CONTROL",
                        "severity": "MAJOR",
                        "statement_ids": ["S1"],
                        "summary": "no control for the instance generator",
                        "follow_up_question": (
                            "Is the compounding an artefact of the instance generator?"
                        ),
                    }
                ],
            }
        return super().complete(request)


def _draft(prompt: str) -> dict[str, Any]:
    """The writer, grounded in the record it was shown -- ids read from the prompt."""

    supporting = re.search(
        r"idea (PIDEA-\S+) \[(?:VALIDATED|HUMAN_READY)[^\n]*\n(?:  [^\n]*\n)*?"
        r"  evidence (IEVD-\S+) \[experiment/SUPPORTS\]",
        prompt,
    )
    assert supporting, "the writer was shown no supporting measurement"
    idea_id, evidence_id = supporting.group(1), supporting.group(2)
    return {
        "title": "What the reviewed evidence establishes",
        "statements": [
            {
                "statement_id": "S1",
                "kind": "FINDING",
                "text": "The measured interaction cleared the bar fixed before the design.",
                "cites": [evidence_id],
                "ideas": [idea_id],
            },
            {
                "statement_id": "S2",
                "kind": "LIMITATION",
                "text": "One instance generator produced every measurement.",
                "cites": [],
                "ideas": [idea_id],
            },
        ],
        "evidence_requests": [],
    }


def _router(runtime_db: Database) -> LoopRouter:
    router = LoopRouter(
        answers={
            "duplicate_adjudicator": {"verdict": "distinct", "rationale": "different"},
            "novelty_screener": {"likely_known": False, "rationale": "nothing close"},
            "falsifier": {
                "summary": "no cheap kill",
                "objections": [],
                "attempted": ["x"],
            },
            "methodology_reviewer": _pass("the methodologist"),
            "novelty_reviewer": _pass("the novelty reviewer"),
            "skeptic_reviewer": _pass("the skeptic"),
            "meta_reviewer": {
                "recommendation": "HUMAN_READY",
                "summary": "all three reviewers were satisfied",
                "unresolved_disagreements": [],
            },
            "brancher": {"children": [], "relations": []},
            "literature_reader": READING,
            "failure_mining_explorer": {"candidates": [], "nothing_to_propose": "none"},
            "literature_explorer": {"candidates": [], "nothing_to_propose": "none"},
        },
        answers_by_prompt={TEMPLATES["analysis_designer"].identity: _analysis()},
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
    router.proposed = set()
    router.explorer_calls = 0
    return router


def _pass(who: str) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "summary": f"{who} found nothing to object to",
        "objections": [],
    }


# -------------------------------------------------------------- the loop --
def test_the_autonomous_loop_runs_end_to_end_through_the_control_plane(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    grid_repo: Path,
    tmp_path: Path,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_os.portfolio import extensions as portfolio_extensions

    router = _router(runtime_db)
    daemon = _plane(runtime_db, pg_dsn, tmp_path, grid_repo, router, monkeypatch)
    monkeypatch.setattr(portfolio_extensions, "_literature", lambda: Corpus())
    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(grid_repo)
    )
    base = load_config()
    # The researcher's one setting: the pool floor that buys explorers. Three
    # candidates are all this scenario needs, and a floor the explorer can
    # never reach would buy a barren exploration every tick until the
    # portfolio paused itself for having no frontier -- correctly, and not
    # what this test is about.
    config = base.model_copy(
        update={"bounds": base.bounds.model_copy(update={"candidate_pool_floor": 1})}
    )

    def ideas() -> dict[str, Any]:
        return {
            portfolio.require_version(item.idea_id).title: item
            for item in portfolio.list_ideas(project_id=runtime_project, limit=100)
        }

    def done() -> bool:
        titles = ideas()
        syntheses = portfolio.list_syntheses(project_id=runtime_project)
        return (
            all(child["title"] in titles for child in CHILDREN.values())
            and bool(
                _contracts(portfolio, titles[CHILDREN["INSUFFICIENT"]["title"]].idea_id)
            )
            and any(item.state is SynthesisState.REFEREED for item in syntheses)
            and WARM["title"] in titles
            and portfolio.require_idea(titles[WARM["title"]].idea_id).operational_state
            is not OperationalState.BLOCKED_DEPENDENCY
            and any(
                item.state is ContractState.BLOCKED_CAPABILITY
                for item in _contracts(portfolio, titles[WARM["title"]].idea_id)
            )
        )

    for _ in range(80):
        tick(
            db=runtime_db,
            project_id=runtime_project,
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=config,
        )
        _run_until_idle(daemon)
        if done():
            break
    titles = ideas()
    trail = _trail(portfolio, runtime_db, runtime_project)
    assert done(), "the loop did not complete:\n" + trail

    # 1. Independent generation: the explorer made the first ideas, from
    #    nothing a person wrote.
    for item in (COLLAPSED, MEASURED, WARM):
        idea = titles[item["title"]]
        assert idea.origin is IdeaOrigin.BLIND_EXPLORER and idea.depth == 0, trail
        (why,) = portfolio.provenance_of(idea.idea_id)
        assert why.basis is ProvenanceBasis.BLIND_EXPLORATION
    assert not portfolio.list_seeds(project_id=runtime_project)

    # 2 + 3. The collapsed design read INSUFFICIENT under its frozen contract,
    #        and that event became a deeper idea with a contract of its own.
    parent = titles[COLLAPSED["title"]]
    (measured,) = [
        item
        for item in portfolio.list_experiments(idea_id=parent.idea_id)
        if item.role is ExperimentRole.PRIMARY
    ]
    assert measured.conclusion is EmpiricalConclusion.INSUFFICIENT, trail
    parent_contract = portfolio.require_contract(measured.contract_id)
    assert parent_contract.state is ContractState.FROZEN
    assert parent_contract.analysis_digest and parent_contract.design_digest
    designer_prompts = [
        item.prompt
        for item in router.requests
        if item.prompt_version == TEMPLATES["experiment_designer"].identity
    ]
    assert designer_prompts and not any(
        "0.25" in text or "0.1 " in text for text in designer_prompts
    ), "the designer was shown a threshold"
    child = titles[CHILDREN["INSUFFICIENT"]["title"]]
    assert child.depth == 1 and child.lineage_root == parent.lineage_root
    (reason,) = portfolio.provenance_of(child.idea_id)
    assert reason.basis is ProvenanceBasis.INSUFFICIENT
    assert portfolio.require_request(reason.request_id).source_ref == (
        measured.experiment_id
    )
    child_contracts = _contracts(portfolio, child.idea_id)
    assert child_contracts and all(
        item.contract_id != parent_contract.contract_id for item in child_contracts
    ), trail
    assert any(item.state is ContractState.FROZEN for item in child_contracts), trail
    assert all(
        item.hypothesis_digest != parent_contract.hypothesis_digest
        for item in child_contracts
    )
    assert portfolio.require_contract(parent_contract.contract_id) == parent_contract

    # 4. The thin novelty case asked the literature; the gap became an idea.
    warm = titles[WARM["title"]]
    asked = [
        item
        for item in portfolio.list_requests(project_id=runtime_project)
        if item.kind is RequestKind.LITERATURE and item.source_idea_id == warm.idea_id
    ]
    assert asked and asked[0].state is RequestState.CONSUMED, trail
    claims = portfolio.list_literature_claims(
        project_id=runtime_project, idea_id=warm.idea_id
    )
    gap = next(item for item in claims if str(item.kind) == "GAP")
    reading_child = titles[CHILDREN["LITERATURE"]["title"]]
    (why,) = portfolio.provenance_of(reading_child.idea_id)
    assert why.basis is ProvenanceBasis.LITERATURE and why.source_ref == gap.claim_id

    # 5. Then it was refused for want of a capability -- and kept its science.
    (blocked,) = [
        item
        for item in _contracts(portfolio, warm.idea_id)
        if item.state is ContractState.BLOCKED_CAPABILITY
    ]
    assert blocked.analysis_digest and blocked.design_digest is None
    assert blocked.capability_request["name"] == "warm-start-sweep"
    assert not [
        item for item in portfolio.list_experiments(idea_id=warm.idea_id) if item.job_id
    ], "nothing ran for an idea no declared command can measure"

    # 6. Nothing waited on it: other ideas were worked on after it blocked.
    blocked_at = blocked.updated_at
    later = [
        action
        for item in titles.values()
        if item.idea_id != warm.idea_id
        for action in portfolio.list_actions(idea_id=item.idea_id)
        if action.created_at > blocked_at
    ]
    assert later, "unrelated work stopped while one idea was blocked:\n" + trail

    # 7. A status the gates granted, on a three-family board.
    from research_os.portfolio.gates import board_independence

    reviewed = titles[MEASURED["title"]]
    assert portfolio.require_idea(reviewed.idea_id).status in {
        IdeaStatus.VALIDATED,
        IdeaStatus.HUMAN_READY,
    }, trail
    assert board_independence(portfolio.live_reviews(idea_id=reviewed.idea_id)) == 3

    # 8. The writer and the referee sent work back to the frontier -- and the
    #    referee approved nothing.
    (refereed,) = [
        item
        for item in portfolio.list_syntheses(project_id=runtime_project)
        if item.state is SynthesisState.REFEREED
    ][:1]
    finding = portfolio.list_requests(project_id=runtime_project)
    raised = [
        item
        for item in finding
        if item.basis is RequestBasis.REFEREE_FINDING
        and item.source_ref.startswith(refereed.synthesis_id)
    ]
    assert raised, trail
    referee_child = titles[CHILDREN["REFEREE_FINDING"]["title"]]
    (why,) = portfolio.provenance_of(referee_child.idea_id)
    assert why.basis is ProvenanceBasis.REFEREE_FINDING
    with runtime_db.tx() as conn:
        human = conn.execute(
            "select count(*) as n from idea_reviews where reviewer_role::text = 'HUMAN'"
        ).fetchone()
    assert human["n"] == 0

    # Every idea the loop made says why it exists.
    for item in titles.values():
        assert portfolio.provenance_of(item.idea_id), item.idea_id


def _contracts(portfolio: PortfolioStore, idea_id: str) -> list[Any]:
    with portfolio.db.tx() as conn:
        rows = conn.execute(
            "select contract_id from scientific_contracts where idea_id = %s "
            "order by created_at",
            (idea_id,),
        ).fetchall()
    return [portfolio.require_contract(str(row["contract_id"])) for row in rows]


def _trail(portfolio: PortfolioStore, runtime_db: Database, project: str) -> str:
    lines = []
    for idea in portfolio.list_ideas(project_id=project, limit=100):
        title = portfolio.require_version(idea.idea_id).title
        lines.append(
            f"{idea.idea_id} d{idea.depth} {idea.origin} {idea.status} "
            f"{idea.operational_state} {title!r}"
        )
        for action in portfolio.list_actions(idea_id=idea.idea_id)[-6:]:
            lines.append(
                f"    {action.stage} {action.status} {action.failure_class or ''} "
                f"{(action.detail or '')[:120]}"
            )
    for request in portfolio.list_requests(project_id=project):
        lines.append(
            f"  request {request.kind} {request.basis} {request.state} "
            f"{request.source_ref} {(request.resolution or '')[:100]}"
        )
    with runtime_db.tx() as conn:
        failed = conn.execute(
            "select kind, last_error from work_items where status = 'FAILED' "
            "order by created_at"
        ).fetchall()
    for row in failed:
        lines.append(f"  FAILED {row['kind']}: {(row['last_error'] or '')[:160]}")
    return "\n".join(lines)
