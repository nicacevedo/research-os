"""What the pre-qualification hostile review reproduced, and what holds instead.

Independent reviewers were asked one question of the qualification candidate
(de74113): can it falsely create, attribute, freeze, execute, interpret,
review or recursively continue scientific evidence while appearing valid.
Each test here is one of their reproductions, run through the production code
they used, with the assertion turned round -- and each carries a control, so
the repair cannot pass by over-correcting.

Five defects, in three families:

- **a ceiling that did not see the spend.** A call the provider billed and then
  marked ``is_error`` released its reservation and charged nothing, so an
  explicit project ceiling reported compliance over any amount of real spend;
- **a gate that counted a thing twice.** The novelty top-up reading -- the same
  research-question search that fills VALIDATED's source count -- also passed
  as HUMAN_READY's independent "second terminology path"; a work the shared
  index had merged was two sources, and a "new" one; and an idea kept
  VALIDATED after its own second-line verification raised a standing fatal
  objection to its claim;
- **a measurement that was not one.** A retry after a worker died mid-run
  adopted the killed run's workspace, so a program that resumes from a result
  on disk had its dead predecessor's checkpoint read as a complete,
  preregistered measurement.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.literature.models import AuthorRecord
from research_os.literature.store import LiteratureStore, resolve_keys
from research_os.portfolio import frontier, gates, litintel, runner, stages, synthesis
from research_os.portfolio.config import load_config
from research_os.portfolio.curator import BANK_ROOT, snapshot
from research_os.portfolio.models import (
    ActionStatus,
    AdjudicationType,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    IdeaEvidence,
    IdeaOrigin,
    IdeaStatus,
    QualityTier,
    RequestState,
    ReviewerRole,
    Severity,
    Stage,
)
from research_os.portfolio.stages import snapshot_for
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.track import _basis_for, advance_idea
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetExhaustedError, BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.interfaces import (
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelRole,
)
from research_os.runtime.models import BudgetScope, ModelCallStatus
from research_os.runtime.routing import (
    ModelRouter,
    ProviderCallFailedError,
    ProviderProfile,
)
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.portfolio_helpers import idea_fields, portfolio, record_review, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import _advance as _empirical_advance
from tests.test_portfolio_empirical import _context as _empirical_context
from tests.test_portfolio_empirical import _idea as _empirical_idea
from tests.test_portfolio_empirical import _router as _empirical_router
from tests.test_portfolio_empirical import design_answer, project_repo
from tests.test_portfolio_literature_intel import _context as _lit_context
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TwoPathRouter,
    _drive,
    _Entry,
    _matrix,
    _Packet,
    _router,
    _why,
    _Work,
    checkpoint_tables,
)
from tests.test_portfolio_tick import _tick
from tests.test_portfolio_track import _advance

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]


# =============================================================================
# 1. A failed call the provider billed for is spent.
# =============================================================================
ROLES = ("planner", "reviewer", "analyst", "coder", "literature")


def _billing_router(
    db: Database, tmp_path: Path, *, project_id: str, response: ScriptedResponse
) -> tuple[ModelRouter, FakeProvider]:
    store = RuntimeStore(db)
    provider = FakeProvider(
        name="one", family="a", responses={role: [response] for role in ROLES}
    )
    router = ModelRouter(
        adapters={"one": provider},  # type: ignore[dict-item]
        # What `profiles_from_adapters` builds: the default 0.05 estimate.
        profiles=(ProviderProfile(name="one", family="a", tier=3),),
        store=store,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        budgets=BudgetLedger(db),
        # A portfolio run has no run-scope budget; the project ceiling is the
        # one a person typed.
        run_id=store.create_run(project_id=project_id, objective="idea-track").run_id,
        project_id=project_id,
        # High, so the breaker does not stop the repeated case before the
        # ledger is asked. The ledger is what is under test.
        failure_threshold=1_000,
    )
    return router, provider


def _request() -> ModelRequest:
    return ModelRequest(
        role=ModelRole.SKEPTIC_REVIEWER,
        capability=Capability.CRITIQUE,
        prompt="review this",
        prompt_version="skeptic@1",
        criticality=Criticality.NORMAL,
        independence=Independence.DIFFERENT_CONTEXT,
        json_schema={"type": "object"},
    )


def _explicit_ceiling(db: Database, project_id: str, usd: str) -> BudgetLedger:
    """What `researchctl runtime budget <project> --max-cost-usd <usd>` writes."""

    ledger = BudgetLedger(db)
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=project_id,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(usd),
        explicit=True,
    )
    return ledger


def _project_cost(ledger: BudgetLedger, project_id: str) -> Any:
    record = ledger.get(
        scope=BudgetScope.PROJECT,
        scope_id=project_id,
        dimension=Dimension.MODEL_COST_USD,
    )
    assert record is not None
    return record


def test_a_failed_call_the_provider_billed_for_is_charged_to_the_ceiling(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The Claude CLI's ``is_error`` envelope still carries ``total_cost_usd``."""

    ledger = _explicit_ceiling(runtime_db, runtime_project, "10.00")
    router, _ = _billing_router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        response=ScriptedResponse(
            structured=None,
            exit_code=1,
            error="error_max_structured_output_retries",
            total_cost_usd=3.00,
        ),
    )

    with pytest.raises(ProviderCallFailedError):
        router.complete(_request())

    budget = _project_cost(ledger, runtime_project)
    assert budget.spent == Decimal("3.00")
    assert budget.reserved == 0
    # The provenance row says what it cost, not only that it failed.
    (call,) = [
        item
        for item in RuntimeStore(runtime_db).list_model_calls(limit=10)
        if item.status is ModelCallStatus.FAILED
    ]
    assert Decimal(str(call.cost_usd)) == Decimal("3.00")


def test_billed_failures_cannot_spend_past_an_explicit_ceiling(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The reproduction: ten calls, 9.00 USD billed, 0.00 recorded, under 1.00."""

    ledger = _explicit_ceiling(runtime_db, runtime_project, "1.00")
    router, provider = _billing_router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        response=ScriptedResponse(
            structured=None, exit_code=1, error="error_max_turns", total_cost_usd=0.90
        ),
    )

    for _ in range(10):
        try:
            router.complete(_request())
        except ProviderCallFailedError:
            continue
        except BudgetExhaustedError:
            break

    budget = _project_cost(ledger, runtime_project)
    # 0.90 is charged, 0.10 is left, the 0.05 estimate still fits, and the
    # second 0.90 is charged: the overshoot is one call, the ceiling holds.
    assert len(provider.calls) == 2
    assert budget.spent == Decimal("1.80")
    assert budget.exhausted


def test_a_failed_call_that_reports_no_cost_is_released_not_charged(
    runtime_db: Database, tmp_path: Path, runtime_project: str
) -> None:
    """The control. No number from the provider is not a number.

    A process that could not authenticate or never produced an envelope
    reports nothing, and charging the estimate for it would let a provider
    outage exhaust a person's ceiling on work that was never done. The
    unchanged policy, shared with ``runtime/spend.py``.
    """

    ledger = _explicit_ceiling(runtime_db, runtime_project, "1.00")
    router, _ = _billing_router(
        runtime_db,
        tmp_path,
        project_id=runtime_project,
        response=ScriptedResponse(
            structured=None, exit_code=1, error="not logged in", total_cost_usd=None
        ),
    )

    with pytest.raises(ProviderCallFailedError):
        router.complete(_request())

    budget = _project_cost(ledger, runtime_project)
    assert budget.spent == 0
    assert budget.reserved == 0


# =============================================================================
# 2. The novelty top-up is the first search again, not a second path.
# =============================================================================
_T0 = datetime(2026, 9, 25, tzinfo=UTC)


def _literature_row(
    key: str, *, call: str, minute: int, claim_id: str | None = None
) -> IdeaEvidence:
    return IdeaEvidence(
        evidence_id=f"IEVI-{call}-{key}",
        idea_id="PIDEA-x",
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary=f"{key} from {call}",
        literature_key=key,
        source_call_id=call,
        claim_id=claim_id,
        created_at=_T0 + timedelta(minutes=minute),
    )


FIRST_AUDIT = (
    _literature_row("openalex:W1", call="MCALL-audit", minute=0),
    _literature_row("openalex:W2", call="MCALL-audit", minute=0),
)
#: The top-up: a verified reading of the research question, under its own call.
TOP_UP = (
    _literature_row("openalex:W3", call="MCALL-reader", minute=5, claim_id="PLCL-1"),
)


def test_a_novelty_top_up_reading_is_not_a_second_terminology_path() -> None:
    assert not gates._second_terminology_path((*FIRST_AUDIT, *TOP_UP), 1)


def test_a_second_audit_that_finds_a_work_neither_search_cited_is_one() -> None:
    """The control: the real second path, which ``run_replicate`` performs."""

    second = (_literature_row("openalex:W4", call="MCALL-second", minute=9),)
    assert gates._second_terminology_path((*FIRST_AUDIT, *TOP_UP, *second), 1)
    # And without the top-up at all, which is the ordinary case.
    assert gates._second_terminology_path((*FIRST_AUDIT, *second), 1)


def test_a_second_audit_that_only_refinds_the_top_ups_work_is_not_new() -> None:
    """The work the top-up read was found by the research-question search."""

    second = (_literature_row("openalex:W3", call="MCALL-second", minute=9),)
    assert not gates._second_terminology_path((*FIRST_AUDIT, *TOP_UP, *second), 1)


THIN = {
    "openalex:W1": ("Title of W1", "Abstract of W1 about support trajectories."),
    "openalex:W2": ("Title of W2", "Abstract of W2 about screening orderings."),
    "openalex:W3": (
        "Title of W3",
        "We compare the support trajectories of pricing and working sets directly.",
    ),
}


class ThinIndex:
    """An index that returns the same three works whatever it is asked.

    Against it a genuine second terminology path finds nothing new, so nothing
    that *is* one can satisfy the rule.
    """

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 12) -> _Packet:
        self.queries.append(query)
        return _Packet(
            query=query,
            entries=tuple(
                _Entry(_Work(key, title, abstract))
                for key, (title, abstract) in list(THIN.items())[:limit]
            ),
        )


class TwoOfThreeScout(TwoPathRouter):
    """The scout's matrix names two of the three works it was shown."""

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            self.answers = {
                **self.answers,
                "literature_scout": _matrix(("openalex:W1", "openalex:W2")),
            }
            return super(TwoPathRouter, self).complete(request)
        return super().complete(request)


READER = {
    "answer": "W3 compares the trajectories directly.",
    "claims": [
        {
            "kind": "FINDING",
            "statement": "W3 compares support trajectories of pricing and working sets.",
            "work_keys": ["openalex:W3"],
            "excerpt": "compare the support trajectories of pricing and working sets",
            "relation_to_idea": "SUPPORTS",
        }
    ],
    "disagreements": [],
    "gaps": [],
}


def test_the_top_up_does_not_carry_an_idea_to_human_ready_on_a_thin_index(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The reviewer's reproduction, end to end through the production track.

    The audit names two of three required sources; the tick's settle asks the
    literature; the reader names the third. Before the repair the idea reached
    HUMAN_READY at its first meta-review, before REPLICATE ran, over an index
    that has nothing a second search could find.
    """

    idea, _ = seed_idea(portfolio, runtime_project, falsifier=LITERATURE_FALSIFIER)
    base = _router(runtime_db)
    router = TwoOfThreeScout(answers=base.answers, store=RuntimeStore(runtime_db))
    router.answers = {**router.answers, "literature_reader": READER}
    index = ThinIndex()

    trace = _drive(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea.idea_id, router, index
    )
    assert "retrieved source" in trace[-1], _why(trace)

    current = portfolio.require_idea(idea.idea_id)
    snap = snapshot_for(portfolio, current)
    assert snap is not None
    assert frontier.settle(portfolio, current, snap, load_config()) == (
        "WAITING_FOR_LITERATURE"
    )
    (request,) = [
        item
        for item in portfolio.list_requests(project_id=runtime_project)
        if item.source_ref.startswith("novelty:")
    ]
    result = litintel.answer_request(
        _lit_context(portfolio, runtime_db, tmp_path, runtime_project, router),
        request.request_id,
        literature=index,
    )
    assert result.ok, result.detail
    assert portfolio.require_request(request.request_id).state is (
        RequestState.CONSUMED
    )
    version = portfolio.require_version(idea.idea_id)
    evidence = portfolio.list_evidence(
        idea_id=idea.idea_id, idea_version=version.version
    )
    # The top-up did its own job: VALIDATED's source count is now met.
    assert len(gates._distinct_literature_keys(evidence)) == 3
    assert not gates._second_terminology_path(evidence, 1)

    for _ in range(30):
        step = advance_idea(
            runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=index,
        )
        trace.append(f"{step.stage} ok={step.ok} {(step.detail or step.reason)[:70]}")
        if step.stage is None or not step.ok:
            break
    final = portfolio.require_idea(idea.idea_id)
    assert Stage.REPLICATE in portfolio.succeeded_stages_for_version(
        idea_id=idea.idea_id, idea_version=version.version
    ), _why(trace)
    assert final.status is IdeaStatus.VALIDATED, _why(trace)
    assert final.quality_tier is QualityTier.VALIDATED, _why(trace)


# =============================================================================
# 3. A work the index merged is one source, under every reader.
# =============================================================================
PREPRINT = "arxiv:2001.01234"
PUBLISHED = "doi:10.1000/xyz"


def _author(name: str) -> list[AuthorRecord]:
    return [AuthorRecord(position=0, name=name)]


def _merged_index() -> None:
    """The shared index, on disk where production reads it, holding one merge.

    A preprint, then its journal version, then a record naming both ids: the
    store folds the preprint into the published work and keeps the losing key
    as an alias.
    """

    abstract = "We compare support trajectories of pricing and working sets."
    with LiteratureStore.open() as store:
        store.ingest(
            provider="arxiv",
            payload={"a": 1},
            fields={
                "title": "Support trajectories of pricing and working sets",
                "abstract": abstract,
                "publication_year": 2020,
            },
            identifiers={"arxiv": "2001.01234v1"},
            authors=_author("Jane Roe"),
        )
        store.ingest(
            provider="crossref",
            payload={"c": 1},
            fields={
                "title": "Comparing trajectories of pricing and working-set methods",
                "abstract": abstract,
                "publication_year": 2021,
            },
            identifiers={"doi": "10.1000/xyz"},
            authors=_author("Jane Roe"),
        )
        store.ingest(
            provider="openalex",
            payload={"link": 1},
            fields={
                "title": "Comparing trajectories of pricing and working-set methods",
                "publication_year": 2021,
            },
            identifiers={
                "openalex": "W99",
                "doi": "10.1000/xyz",
                "arxiv": "2001.01234",
            },
            authors=_author("Jane Roe"),
        )
        assert store.resolve_key(PREPRINT) == PUBLISHED


def _cite(store: PortfolioStore, idea_id: str, key: str, *, call: str) -> IdeaEvidence:
    return store.add_evidence(
        idea_id=idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary=f"{key}, cited by {call}",
        literature_key=key,
        source_call_id=call,
    )


def _scout_call(runtime_db: Database) -> str:
    return (
        RuntimeStore(runtime_db)
        .record_model_call(
            provider="claude", role="literature_scout", status=ModelCallStatus.OK
        )
        .call_id
    )


def test_a_work_the_index_merged_is_one_source_and_not_a_new_one(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    first, second = _scout_call(runtime_db), _scout_call(runtime_db)
    # Before the merge: the first audit cites the preprint and one other work.
    _cite(portfolio, idea.idea_id, PREPRINT, call=first)
    _cite(portfolio, idea.idea_id, "openalex:W20", call=first)
    _merged_index()
    # After it: the second path's search returns the published work.
    _cite(portfolio, idea.idea_id, PUBLISHED, call=second)

    evidence = portfolio.list_evidence(idea_id=idea.idea_id, idea_version=1)
    assert gates._distinct_literature_keys(evidence) == {PUBLISHED, "openalex:W20"}
    assert not gates._second_terminology_path(evidence, 1)
    # The stage machine counts what the gate counts, or the tick and the
    # track disagree about what runs next.
    snap = snapshot_for(portfolio, portfolio.require_idea(idea.idea_id))
    assert snap is not None
    assert snap.literature_keys() == {PUBLISHED, "openalex:W20"}
    # History is kept: the row still records the key its search returned.
    with portfolio.db.tx() as conn:
        stored = {
            row["literature_key"]
            for row in conn.execute(
                "select literature_key from idea_evidence where idea_id = %s",
                (idea.idea_id,),
            ).fetchall()
        }
    assert PREPRINT in stored


def test_without_an_index_keys_are_read_as_written(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The control: no index is no merge, and nothing is invented or created."""

    from research_os.literature.store import database_path

    idea, _ = seed_idea(portfolio, runtime_project)
    _cite(portfolio, idea.idea_id, PREPRINT, call=_scout_call(runtime_db))
    _cite(portfolio, idea.idea_id, PUBLISHED, call=_scout_call(runtime_db))

    evidence = portfolio.list_evidence(idea_id=idea.idea_id, idea_version=1)
    assert gates._distinct_literature_keys(evidence) == {PREPRINT, PUBLISHED}
    assert not database_path().exists()
    assert resolve_keys(["arxiv:unknown"]) == {"arxiv:unknown": "arxiv:unknown"}


# =============================================================================
# 4. VALIDATED does not outlive a fatal objection to its claim.
# =============================================================================
FAMILIES = ("openai", "google", "anthropic")
BOARD = (ReviewerRole.METHODOLOGY, ReviewerRole.NOVELTY, ReviewerRole.SKEPTIC)


def _track_context(
    store: PortfolioStore,
    runtime_db: Database,
    project: str,
    idea_id: str,
    tmp_path: Path,
) -> runner.TrackContext:
    runtime = RuntimeStore(runtime_db)
    return runner.TrackContext(
        config=load_config(),
        portfolio=store,
        runtime=runtime,
        models=None,  # type: ignore[arg-type]
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=runtime),
        project_id=project,
        idea_id=idea_id,
        run_id=runtime.create_run(project_id=project, objective="regression").run_id,
    )


def _succeed(store: PortfolioStore, idea_id: str, version: int, stage: Stage) -> None:
    basis = (
        _basis_for(store, idea_id=idea_id, version=version, stage=stage)
        if stage is Stage.META_REVIEW
        else f"regression-basis:{stage}:{version}"
    )
    action = store.open_action(
        idea_id=idea_id, idea_version=version, stage=stage, basis_digest=basis
    )
    store.complete_action(action_id=action.action_id, status=ActionStatus.SUCCEEDED)


def _validated_mathematical_idea(
    store: PortfolioStore, runtime_db: Database, project: str, tmp_path: Path
) -> tuple[str, runner.TrackContext]:
    """An idea whose rows really earn VALIDATED, one stage from REPLICATE."""

    runtime = RuntimeStore(runtime_db)
    origin = runtime.record_model_call(
        provider="claude", role="blind_explorer", status=ModelCallStatus.OK, model="o"
    )
    idea, _ = store.create_idea(
        project_id=project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(adjudication_types=[]),
        origin_role="blind_explorer",
        origin_call_id=origin.call_id,
    )
    sharpen = runtime.record_model_call(
        provider="claude", role="scientific_discovery", status=ModelCallStatus.OK
    )
    store.append_version(
        idea_id=idea.idea_id,
        fields=idea_fields(adjudication_types=[], title="sharpened"),
        origin_call_id=sharpen.call_id,
        origin_role="scientific_discovery",
        origin_stage="discover",
    )
    store.set_adjudication_types(
        idea_id=idea.idea_id, version=2, types=[str(AdjudicationType.MATHEMATICAL)]
    )
    job = runtime.create_external_job(
        project_id=project, executor="local", spec_digest="s2" * 32, run_dir="/x"
    )
    store.add_evidence(
        idea_id=idea.idea_id,
        idea_version=2,
        kind=EvidenceKind.DERIVATION,
        strength=EvidenceStrength.SUPPORTS,
        summary="the orderings differ on a 2x2 instance",
        literature_key="derivation:1",
    )
    store.add_evidence(
        idea_id=idea.idea_id,
        idea_version=2,
        kind=EvidenceKind.EXPERIMENT,
        strength=EvidenceStrength.SUPPORTS,
        summary="an executed search exhibited the divergent instance",
        job_id=job.job_id,
    )
    for index in range(3):
        store.add_evidence(
            idea_id=idea.idea_id,
            idea_version=2,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.CONSISTENT_WITH,
            summary=f"retrieved source {index}",
            literature_key=f"openalex:W{index}",
        )
    for role, family in zip(BOARD, FAMILIES, strict=True):
        record_review(
            store,
            idea_id=idea.idea_id,
            version=2,
            role=role,
            family=family,
            model=f"model-{family}",
        )
    for stage in (
        Stage.DEDUP,
        Stage.NOVELTY_SCREEN,
        Stage.FALSIFY,
        Stage.DISCOVER,
        Stage.ADJUDICATE,
        Stage.LITERATURE_AUDIT,
        Stage.EVIDENCE,
        Stage.REVIEW_BOARD,
        Stage.META_REVIEW,
    ):
        _succeed(store, idea.idea_id, 2, stage)
    context = _track_context(store, runtime_db, project, idea.idea_id, tmp_path)
    # Positive control: the rows earn VALIDATED, and REPLICATE is next.
    assert runner._evaluate(context).at_least(QualityTier.VALIDATED)
    store.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)
    assert stages.select_stage(runner.build_snapshot(context), load_config())[0] is (
        Stage.REPLICATE
    )
    return idea.idea_id, context


def test_a_fatal_objection_from_the_replicator_rejects_a_validated_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    checkpoint_tables: str,
    pg_dsn: str,
    tmp_path: Path,
) -> None:
    idea_id, context = _validated_mathematical_idea(
        portfolio, runtime_db, runtime_project, tmp_path
    )
    router = ScriptedRouter(
        answers={
            "replicator": {
                "verdict": "REJECT",
                "summary": "the reconstruction fails",
                "objections": [
                    {
                        "severity": "FATAL",
                        "target": "CLAIM",
                        "summary": (
                            "the derivation assumes a total order the 2x2 "
                            "instance does not have; the claim is false"
                        ),
                    }
                ],
            }
        },
        store=RuntimeStore(runtime_db),
    )
    result = _advance(
        portfolio, runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router
    )
    assert result.stage is Stage.REPLICATE and result.ok, result.detail
    assert portfolio.open_objections(idea_id=idea_id, minimum=Severity.FATAL)
    assert not runner._evaluate(context).at_least(QualityTier.PROMISING)

    _tick(runtime_db, pg_dsn, tmp_path, runtime_project)

    after = portfolio.require_idea(idea_id)
    assert after.status is IdeaStatus.REJECTED
    assert "fatal objection to the claim stands" in (after.retire_reason or "")
    page = snapshot(runtime_db, runtime_project)[f"{BANK_ROOT}/bank/VALIDATED.md"]
    assert idea_id not in page
    packet = synthesis.build_packet(portfolio, runtime_project)
    assert packet is None or idea_id not in {item["idea_id"] for item in packet.ideas}


def test_a_validated_idea_with_nothing_left_and_no_fatal_objection_keeps_it(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. Settling a finished VALIDATED idea is not parking it.

    A VALIDATED idea whose track has ended for any reason other than a fatal
    objection to its claim -- no route to a second-line verification on this
    host, say -- is a result for a person to read, and stays one.
    """

    idea_id, context = _validated_mathematical_idea(
        portfolio, runtime_db, runtime_project, tmp_path
    )
    monkeypatch.setattr(
        stages,
        "select_stage",
        lambda *_a, **_k: (None, "every stage this idea's state calls for has run"),
    )
    snap = runner.build_snapshot(context)
    assert (
        frontier.settle(portfolio, portfolio.require_idea(idea_id), snap, load_config())
        is None
    )
    assert portfolio.require_idea(idea_id).status is IdeaStatus.VALIDATED


# =============================================================================
# 5. A retry never reads what a killed execution left in its workspace.
# =============================================================================
#: The ordinary resume convention of research code: a result on disk is a
#: result already computed. It checkpoints half way, then finishes.
RESUMABLE = """\
import json, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
if out.exists():
    print("result already present; nothing to do")
    raise SystemExit(0)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"summary": {"overlap": 0.4}, "complete": False}))
out.write_text(json.dumps({"summary": {"overlap": 0.9}, "complete": True}))
print("measured")
"""


class _WorkerKilled(BaseException):
    """The worker process dying: nothing after this line of Python runs."""


@pytest.mark.parametrize("project_repo", [RESUMABLE], indirect=True)
def test_a_killed_runs_checkpoint_is_not_read_as_the_retrys_measurement(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The reviewer's reproduction: the first execution dies holding a checkpoint.

    Bubblewrap's ``--die-with-parent`` takes the program down with the
    worker, so the job row stays SUBMITTING and the invocation is abandoned
    by the daemon's recovery pass. Run to completion on a clean checkout the
    program measures 0.9, a refutation; the killed run's 0.4 would read as
    support.
    """

    from research_os.runtime.executors import LocalExecutor
    from research_os.runtime.idempotency import InvocationLedger

    class DiesMidRun(LocalExecutor):
        calls = 0

        def submit(self, spec, *, run_dir):  # type: ignore[override]
            type(self).calls += 1
            if type(self).calls == 1:
                out = Path(spec.cwd) / "results" / "run.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps({"summary": {"overlap": 0.4}, "complete": False})
                )
                raise _WorkerKilled()
            return super().submit(spec, run_dir=run_dir)

    idea_id = _empirical_idea(portfolio, runtime_project)
    context = _empirical_context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_empirical_router(runtime_db, design=design_answer(seed=1)),
        repo=project_repo,
        executors={"local": DiesMidRun()},
    )
    with pytest.raises(_WorkerKilled):
        _empirical_advance(context)
    InvocationLedger(runtime_db).abandon_stale(older_than_seconds=0)

    step = _empirical_advance(context)
    assert DiesMidRun.calls == 2, "the retry executed the program again"
    assert step.conclusion is EmpiricalConclusion.CONTRADICTS, step.detail
    rows = portfolio.list_evidence(idea_id=idea_id, idea_version=1)
    assert rows and all(row.strength is EvidenceStrength.CONTRADICTS for row in rows)


@pytest.mark.parametrize("project_repo", [RESUMABLE], indirect=True)
def test_an_uninterrupted_run_is_read_as_it_ran(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The control: a fresh checkout per execution changes nothing for one run."""

    idea_id = _empirical_idea(portfolio, runtime_project)
    context = _empirical_context(
        portfolio=portfolio,
        runtime_db=runtime_db,
        tmp_path=tmp_path,
        project_id=runtime_project,
        idea_id=idea_id,
        router=_empirical_router(runtime_db, design=design_answer(seed=1)),
        repo=project_repo,
    )
    step = _empirical_advance(context)
    assert step.conclusion is EmpiricalConclusion.CONTRADICTS, step.detail
