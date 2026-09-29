"""The early capability-feasibility signal: planning metadata, derived by code.

``research_os.portfolio.feasibility`` matches what an idea says a settling
measurement needs (``contracts.EvidenceNeeds``, stated by the sharpening stage
against the catalogue it is shown) with the science repository's *committed*
capability declarations. The allocator uses it only to order scarce
advancement work between otherwise equal ideas: a capability-limited idea is
kept in the bank, keeps its status, and is still advanced when nothing
executable of comparable value waits.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.capability import parse_manifest
from research_os.portfolio import allocation, feasibility
from research_os.portfolio.allocation import AdvancementPosition, SaleAuthority
from research_os.portfolio.models import (
    AdjudicationType,
    Feasibility,
    IdeaStatus,
    Stage,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_allocation_lanes import _config, deep, fresh
from tests.test_portfolio_allocation_lanes import plan as lane_plan
from tests.test_science_campaigns import manifest, science_repo

__all__ = [
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "science_repo",
]


@dataclasses.dataclass(frozen=True)
class _Loaded:
    manifest: Any
    sha256: str = "0" * 64
    commit: str = "0" * 40


@dataclasses.dataclass(frozen=True)
class _Command:
    name: str = "draw"
    parameters: tuple[Any, ...] = ()
    outputs: tuple[str, ...] = ("results/draw.json",)
    timeout_seconds: int = 120


@dataclasses.dataclass(frozen=True)
class _Parameter:
    name: str = "plan"
    type: str = "generated"
    required: bool = True


def loaded(*, campaign: bool = True) -> Any:
    text = manifest(campaign="default" if campaign else None)
    return _Loaded(parse_manifest(text.encode("utf-8")))


COMMANDS = {"draw": _Command(parameters=(_Parameter(),))}


def assess(needs: dict[str, Any] | None, **kwargs: Any) -> feasibility.Assessment:
    return feasibility.assess(
        needs,
        loaded=kwargs.pop("loaded", loaded()),
        commands=kwargs.pop("commands", COMMANDS),
        adjudication=kwargs.pop("adjudication", (AdjudicationType.EMPIRICAL,)),
        **kwargs,
    )


# ------------------------------------------------------------- the match --
@pytest.mark.parametrize(
    ("needs", "signal", "text"),
    [
        (
            {"data": "new_execution", "fields": ["x", "y"], "independent_draws": 1},
            Feasibility.CURRENTLY_EXECUTABLE,
            "declares every field",
        ),
        (
            {
                "data": "new_execution",
                "fields": ["x", "y", "seed"],
                "independent_draws": 4,
            },
            Feasibility.LIKELY_EXECUTABLE_WITH_CAMPAIGN,
            "a campaign of up to 6",
        ),
        (
            {
                "data": "new_execution",
                "fields": ["x", "iterations"],
                "independent_draws": 1,
            },
            Feasibility.CAPABILITY_LIMITED,
            "iterations",
        ),
        (
            {"data": "existing_records", "fields": ["x"], "independent_draws": 1},
            Feasibility.CAPABILITY_LIMITED,
            "records that already exist",
        ),
        ({"data": "none"}, Feasibility.UNKNOWN, "needs no measurement"),
        (None, Feasibility.UNKNOWN, "no structured evidence requirement"),
    ],
)
def test_the_signal_is_the_committed_catalogue_against_the_structured_need(
    needs: dict[str, Any] | None, signal: Feasibility, text: str
) -> None:
    result = assess(needs)
    assert result.signal is signal
    assert text in result.basis


def test_many_draws_without_campaign_support_is_not_called_limited() -> None:
    """One execution might hold them; that is for the design to show, not for this."""

    result = assess(
        {"fields": ["x", "y"], "independent_draws": 4}, loaded=loaded(campaign=False)
    )
    assert result.signal is Feasibility.UNKNOWN


def test_what_is_not_a_measurement_or_has_no_catalogue_is_unknown() -> None:
    needs = {"fields": ["x"]}
    assert (
        assess(needs, adjudication=(AdjudicationType.MATHEMATICAL,)).signal
        is Feasibility.UNKNOWN
    )
    assert assess(needs, loaded=None).signal is Feasibility.UNKNOWN


def test_a_contract_capability_resolution_refused_is_definitive() -> None:
    result = assess(
        {"fields": ["x", "y"], "independent_draws": 1}, blocked_on_capability=True
    )
    assert result.signal is Feasibility.CAPABILITY_LIMITED


def test_a_later_capability_that_can_hold_the_draws_is_found() -> None:
    """Two capabilities report every field; only the second declares campaigns."""

    parsed = loaded().manifest
    (with_campaign,) = parsed.capabilities
    without = with_campaign.model_copy(update={"id": "aaa.draw", "campaign": None})
    both = _Loaded(parsed.model_copy(update={"capabilities": (without, with_campaign)}))
    assert min(item.id for item in both.manifest.capabilities) == "aaa.draw"
    result = assess(
        {"fields": ["x", "y"], "independent_draws": 4},
        loaded=both,
    )
    assert result.signal is Feasibility.LIKELY_EXECUTABLE_WITH_CAMPAIGN
    assert result.capability == with_campaign.ref


def test_a_capability_the_host_cannot_run_answers_nothing() -> None:
    result = assess({"fields": ["x"]}, commands={})
    assert result.signal is Feasibility.CAPABILITY_LIMITED
    assert "runnable" in result.basis


def test_a_model_cannot_make_a_field_exist_by_naming_it() -> None:
    """The need is the model's claim; only the committed declaration decides."""

    claimed = {"fields": ["x", "historical_rows_2023"], "independent_draws": 1}
    assert assess(claimed).signal is Feasibility.CAPABILITY_LIMITED
    # The same words, and a manifest that declares the field: now it exists.
    richer = (
        manifest()
        .replace(
            "          - {name: seed, type: integer}\n      - name: count",
            "          - {name: seed, type: integer}\n"
            "          - {name: historical_rows_2023, type: integer}\n      - name: count",
        )
        .replace(
            "                seed: {type: integer}\n          summary:",
            "                seed: {type: integer}\n"
            "                historical_rows_2023: {type: integer}\n          summary:",
        )
    )
    declared = _Loaded(parse_manifest(richer.encode("utf-8")))
    assert assess(claimed, loaded=declared).signal is Feasibility.CURRENTLY_EXECUTABLE


# ------------------------------------------------------ in the allocator --
def _limited(candidate: Any) -> Any:
    return dataclasses.replace(candidate, feasibility=Feasibility.CAPABILITY_LIMITED)


def _executable(candidate: Any) -> Any:
    return dataclasses.replace(candidate, feasibility=Feasibility.CURRENTLY_EXECUTABLE)


def test_scarce_advancement_goes_to_the_equally_strong_executable_direction() -> None:
    limited = _limited(deep("A-limited", stage=Stage.EVIDENCE))
    executable = _executable(deep("Z-executable", stage=Stage.EVIDENCE))
    config = _config()
    assert allocation.utility(limited, config=config, active=[]) < allocation.utility(
        executable, config=config, active=[]
    )
    sold = lane_plan([*fresh(10), limited, executable], free_slots=1)
    assert [item.idea_id for item in sold] == ["Z-executable"]


def test_a_limited_direction_does_not_repeatedly_take_deep_execution() -> None:
    """Tick after tick, one completion slot, a fresh executable peer each time."""

    limited = _limited(deep("LIMITED", stage=Stage.EVIDENCE))
    bought: list[str] = []
    for tick in range(20):
        peer = _executable(deep(f"PEER-{tick:02d}", stage=Stage.EVIDENCE))
        sold = lane_plan(
            [*fresh(5, prefix=f"T{tick}-"), limited, peer],
            free_slots=1,
            authority=SaleAuthority(cost_usd=Decimal(100)),
            advancement=AdvancementPosition(protected_usd=Decimal(50)),
        )
        bought.extend(
            item.idea_id for item in sold if item.kind == allocation.ADVANCE_IDEA
        )
    assert "LIMITED" not in bought
    assert len(bought) == 20


def test_the_limited_direction_is_preserved_and_advanced_when_it_is_the_best_left() -> (
    None
):
    limited = _limited(deep("LIMITED", stage=Stage.EVIDENCE))
    sold = lane_plan([*fresh(5), limited], free_slots=1)
    assert [item.idea_id for item in sold] == ["LIMITED"]
    assert limited.idea.status is IdeaStatus.PROMISING


def test_a_much_stronger_limited_direction_still_outranks_a_weak_executable_one() -> (
    None
):
    """A tie-breaker in the completion lane, not a filter: value still decides."""

    strong = _limited(deep("STRONG", stage=Stage.EVIDENCE, novelty=1.0, objections=0))
    weak = _executable(deep("WEAK", stage=Stage.EVIDENCE, novelty=0.0, objections=20))
    sold = lane_plan([*fresh(5), strong, weak], free_slots=1)
    assert [item.idea_id for item in sold] == ["STRONG"]


def test_exploration_of_a_limited_idea_is_untouched() -> None:
    """Only advancement work is ordered by feasibility."""

    candidate = allocation.Candidate(
        idea=fresh(1)[0].idea,
        dimensions=fresh(1)[0].dimensions,
        diversity=fresh(1)[0].diversity,
        stage=Stage.DEDUP,
        reason="dedup",
        expected_cost=Decimal("0.25"),
        idle_seconds=0.0,
        open_objections=0,
        spent=Decimal(0),
        lineage_spent=Decimal(0),
    )
    config = _config()
    assert allocation.utility(
        _limited(candidate), config=config, active=[]
    ) == allocation.utility(candidate, config=config, active=[])


# ---------------------------------------------------------- end to end --
def test_discovery_records_the_need_as_metadata_not_as_the_idea(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    from research_os.portfolio import runner
    from research_os.portfolio.config import load_config
    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.store import RuntimeStore
    from tests.runtime_graph_helpers import ScriptedRouter

    runtime = RuntimeStore(runtime_db)
    idea, _ = seed_idea(portfolio, runtime_project, title="does the draw rise")
    router = ScriptedRouter(
        answers={
            "scientific_discovery": {
                "can_be_made_precise": True,
                "refined": {
                    "title": "the draw rises with x",
                    "research_question": "Does y rise with x across seeds?",
                    "core_idea": "The response has a positive slope.",
                    "mechanism": "The plan's slope is positive.",
                    "falsifier": "Measure y over x for four seeds; no rise refutes it.",
                },
                "evidence_needs": {
                    "data": "new_execution",
                    "fields": ["x", "y", "seed"],
                    "independent_draws": 4,
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
        repo_path=science_repo,
    )
    outcome = runner.run_discover(context, runner.build_snapshot(context))
    assert outcome.ok, outcome.detail
    # The sharpening stage was shown the committed catalogue it names fields from.
    (request,) = router.requests
    assert "capability synthetic.draw@1" in request.prompt
    needs = portfolio.evidence_needs(project_id=runtime_project)
    assert needs[(idea.idea_id, 2)] == {
        "data": "new_execution",
        "fields": ["x", "y", "seed"],
        "independent_draws": 4,
    }
    version = portfolio.require_version(idea.idea_id, 2)
    assert "evidence_needs" not in version.model_dump()


def test_the_tick_reads_each_candidate_signal_from_the_committed_manifest(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    runtime_project: str,
    science_repo: Path,
    tmp_path: Path,
) -> None:
    from research_os.portfolio.config import load_config
    from research_os.portfolio.tick import tick
    from research_os.runtime.store import RuntimeStore
    from tests.runtime_graph_helpers import make_config

    RuntimeStore(runtime_db).upsert_project(
        project_id=runtime_project, repo_path=str(science_repo)
    )
    answerable, _ = seed_idea(
        portfolio,
        runtime_project,
        title="answerable",
        adjudication_types=[AdjudicationType.EMPIRICAL],
    )
    unanswerable, _ = seed_idea(
        portfolio,
        runtime_project,
        title="needs 2023 rows",
        adjudication_types=[AdjudicationType.EMPIRICAL],
    )
    for item in (answerable, unanswerable):
        portfolio.set_status(idea_id=item.idea_id, status=IdeaStatus.PROMISING)
    portfolio.record_evidence_needs(
        idea_id=answerable.idea_id,
        idea_version=1,
        needs={"data": "new_execution", "fields": ["x", "y"], "independent_draws": 1},
    )
    portfolio.record_evidence_needs(
        idea_id=unanswerable.idea_id,
        idea_version=1,
        needs={"data": "existing_records", "fields": ["x"], "independent_draws": 1},
    )
    report = tick(
        db=runtime_db,
        project_id=runtime_project,
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
    )
    assert report.feasibility.get("CURRENTLY_EXECUTABLE") == 1
    assert report.feasibility.get("CAPABILITY_LIMITED") == 1
    # Kept, not rejected: the limited idea is still PROMISING in the bank.
    assert portfolio.require_idea(unanswerable.idea_id).status is IdeaStatus.PROMISING


def test_a_contract_blocked_under_another_command_set_is_not_an_answer(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    tmp_path: Path,
) -> None:
    """Blocked before the declared commands changed: worth asking again, so no penalty."""

    from research_os.portfolio.models import ExperimentRole
    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.store import RuntimeStore

    artifacts = FilesystemArtifactStore(
        tmp_path / "artifacts", store=RuntimeStore(runtime_db)
    )
    analysis = artifacts.put_text(
        "{}", media_type="application/json", role="analysis", producer="test"
    )
    idea, version = seed_idea(portfolio, runtime_project)
    contract = portfolio.create_contract(
        project_id=runtime_project,
        idea_id=idea.idea_id,
        idea_version=version.version,
        role=ExperimentRole.PRIMARY,
        hypothesis_digest=version.content_digest,
        analysable=True,
        analysis_digest="panalysis-v1:" + "1" * 64,
        analysis_artifact_id=analysis.artifact_id,
    )
    portfolio.block_contract_on_capability(
        contract.contract_id,
        capability_request={},
        command_set_digest="before",
        detail="no declared capability reports it",
    )
    key = (idea.idea_id, version.version)
    assert key in portfolio.capability_blocked_versions(project_id=runtime_project)
    assert key in portfolio.capability_blocked_versions(
        project_id=runtime_project, command_set_digest="before"
    )
    assert key not in portfolio.capability_blocked_versions(
        project_id=runtime_project, command_set_digest="after"
    )
