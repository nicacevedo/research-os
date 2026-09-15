"""Provider routing: criticality floors, honest independence, full provenance.

Two properties here are load-bearing for the system's scientific claims.

**Critical work is never silently downgraded.** A scientific review quietly
performed by a weaker model is worse than one that did not happen, because the
first looks like it happened. So the request fails and says what it wanted.

**Independence is reported, not assumed.** The routing may be unable to deliver
what was asked for -- one provider installed, one family available -- and when
that happens the degradation is recorded in the call's provenance. A false claim
of independent review makes the whole apparatus decoration.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

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
    CriticalCapabilityUnavailableError,
    ModelRouter,
    ProviderProfile,
    RoutingError,
    profiles_from_adapters,
)
from research_os.runtime.store import RuntimeStore
from tests.fake_providers import FakeProvider, ScriptedResponse, UnavailableProvider


def _router(
    db: Database,
    tmp_path: Path,
    *,
    run_id: str,
    project_id: str,
    adapters: dict[str, FakeProvider],
    profiles: tuple[ProviderProfile, ...],
) -> ModelRouter:
    store = RuntimeStore(db)
    return ModelRouter(
        adapters=adapters,
        profiles=profiles,
        store=store,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        budgets=BudgetLedger(db),
        run_id=run_id,
        project_id=project_id,
    )


def _provider(name: str, family: str, *, payload: dict | None = None) -> FakeProvider:
    return FakeProvider(
        name=name,
        family=family,
        responses={
            role: [ScriptedResponse(structured=payload or {"ok": True})]
            for role in ("planner", "reviewer", "analyst", "coder", "literature")
        },
    )


@pytest.fixture
def run_id(runtime_db: Database, runtime_project: str) -> str:
    return (
        RuntimeStore(runtime_db)
        .create_run(project_id=runtime_project, objective="o")
        .run_id
    )


def _request(**overrides) -> ModelRequest:
    fields = {
        "role": ModelRole.PLANNER,
        "capability": Capability.PLANNING,
        "prompt": "decide something",
        "prompt_version": "planner@1",
        "criticality": Criticality.NORMAL,
        "independence": Independence.DIFFERENT_CONTEXT,
        "json_schema": {"type": "object"},
    }
    fields.update(overrides)
    return ModelRequest(**fields)


# ------------------------------------------------------------- criticality --
def test_critical_work_refuses_rather_than_using_a_weaker_model(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    cheap = {"cheap": _provider("cheap", "vendor-a")}
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=cheap,
        profiles=(ProviderProfile(name="cheap", family="vendor-a", tier=1),),
    )
    with pytest.raises(CriticalCapabilityUnavailableError, match="tier >= 3"):
        router.complete(
            _request(
                role=ModelRole.SCIENTIFIC_REVIEWER,
                capability=Capability.CRITIQUE,
                criticality=Criticality.CRITICAL,
            )
        )


def test_routine_work_prefers_the_cheaper_capable_provider(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    adapters = {"cheap": _provider("cheap", "a"), "dear": _provider("dear", "b")}
    profiles = (
        ProviderProfile(name="dear", family="b", tier=3, estimated_cost_usd=1.0),
        ProviderProfile(name="cheap", family="a", tier=3, estimated_cost_usd=0.01),
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    routed = router.route(
        _request(
            role=ModelRole.EXTRACTOR,
            capability=Capability.STRUCTURED_EXTRACTION,
            criticality=Criticality.ROUTINE,
            independence=Independence.NONE,
        )
    )
    assert routed.profile.name == "cheap"


def test_a_provider_that_cannot_do_the_capability_is_not_considered(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    adapters = {"narrow": _provider("narrow", "a")}
    profiles = (
        ProviderProfile(
            name="narrow",
            family="a",
            tier=3,
            capabilities=frozenset({Capability.STRUCTURED_EXTRACTION}),
        ),
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    with pytest.raises(RoutingError, match="planning"):
        router.complete(_request())


# ------------------------------------------------------------ independence --
def test_a_reviewer_is_routed_off_the_producers_family_when_one_exists(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    adapters = {
        "one": _provider("one", "family-one"),
        "two": _provider("two", "family-two"),
    }
    profiles = (
        ProviderProfile(name="one", family="family-one", tier=3),
        ProviderProfile(name="two", family="family-two", tier=3),
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    group = "review:HYP-0001"
    producer = router.complete(
        _request(
            role=ModelRole.SEEDED_EXPLORER,
            capability=Capability.SYNTHESIS,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    reviewer = router.complete(
        _request(
            role=ModelRole.SKEPTIC,
            capability=Capability.CRITIQUE,
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert producer.provider != reviewer.provider
    assert reviewer.independence is Independence.DIFFERENT_FAMILY
    assert "has not answered" in reviewer.independence_note


def test_one_family_yields_an_acknowledged_degradation_not_a_false_claim(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """The machine has one provider. Say so; do not claim independence."""

    adapters = {"only": _provider("only", "the-only-family")}
    profiles = (ProviderProfile(name="only", family="the-only-family", tier=3),)
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    group = "review:HYP-0002"
    router.complete(
        _request(
            role=ModelRole.SEEDED_EXPLORER,
            capability=Capability.SYNTHESIS,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    reviewer = router.complete(
        _request(
            role=ModelRole.SKEPTIC,
            capability=Capability.CRITIQUE,
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert reviewer.independence is Independence.DIFFERENT_CONTEXT
    assert reviewer.independence_note.startswith("DEGRADED")
    assert "not independent review" in reviewer.independence_note

    calls = RuntimeStore(runtime_db).list_model_calls(run_id=run_id)
    reviewer_calls = [call for call in calls if call.role == str(ModelRole.SKEPTIC)]
    assert reviewer_calls, "the degraded review was not recorded"
    assert reviewer_calls[0].independence_group == group


# ------------------------------------------------------------- provenance --
def test_a_successful_call_records_everything_needed_to_audit_it(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    adapters = {"one": _provider("one", "a", payload={"action": "assess_frontier"})}
    profiles = (ProviderProfile(name="one", family="a", model="one-v2", tier=3),)
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    response = router.complete(_request(independence_group="plan:1"))
    assert response.ok
    assert response.structured == {"action": "assess_frontier"}

    call = RuntimeStore(runtime_db).list_model_calls(run_id=run_id)[0]
    assert call.provider == "one"
    assert call.role == str(ModelRole.PLANNER)
    assert call.prompt_version == "planner@1"
    assert call.status is ModelCallStatus.OK
    assert call.input_digest and len(call.input_digest) == 64
    assert call.output_artifact_id and len(call.output_artifact_id) == 64
    assert call.latency_ms is not None

    # The prompt and the raw output are both retrievable by hash.
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    assert "decide something" in store.get_text(call.input_digest)
    assert store.exists(call.output_artifact_id)


def test_a_failed_call_is_recorded_too(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """A run that reports no failures because it recorded none is indistinguishable
    from a run that had none."""

    broken = FakeProvider(
        name="broken",
        family="a",
        responses={
            "planner": [ScriptedResponse(structured=None, exit_code=1, error="boom")]
        },
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"broken": broken},
        profiles=(ProviderProfile(name="broken", family="a", tier=3),),
    )
    response = router.complete(_request())
    assert not response.ok
    call = RuntimeStore(runtime_db).list_model_calls(run_id=run_id)[0]
    assert call.status is ModelCallStatus.FAILED
    assert call.error and "boom" in call.error


def test_a_missing_structured_response_is_recorded_as_malformed(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """Distinct from a failure: the provider answered, just not in the shape asked.

    The distinction matters because the retry policies differ -- one re-asks
    with the schema restated, the other backs off.
    """

    chatty = FakeProvider(
        name="chatty",
        family="a",
        responses={"planner": [ScriptedResponse(text="I would rather write prose")]},
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"chatty": chatty},
        profiles=(ProviderProfile(name="chatty", family="a", tier=3),),
    )
    response = router.complete(_request())
    assert response.error == "malformed structured output"
    call = RuntimeStore(runtime_db).list_model_calls(run_id=run_id)[0]
    assert call.status is ModelCallStatus.MALFORMED


# ---------------------------------------------------------------- budgets --
def test_a_call_spends_the_model_budget(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=2,
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"one": _provider("one", "a")},
        profiles=(ProviderProfile(name="one", family="a", tier=3),),
    )
    router.complete(_request())
    budget = ledger.get(
        scope=BudgetScope.RUN, scope_id=run_id, dimension=Dimension.MODEL_CALLS
    )
    assert budget is not None
    assert budget.spent == Decimal(1)
    assert budget.reserved == Decimal(0), "the reservation was not settled"


def test_an_exhausted_budget_stops_the_call_before_it_happens(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=0,
    )
    provider = _provider("one", "a")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"one": provider},
        profiles=(ProviderProfile(name="one", family="a", tier=3),),
    )
    with pytest.raises(BudgetExhaustedError):
        router.complete(_request())
    assert provider.calls == [], "the provider was invoked despite an exhausted budget"


# --------------------------------------------------------- provider health --
def test_a_provider_in_cooldown_is_not_routed_to(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    store = RuntimeStore(runtime_db)
    for _ in range(3):
        store.record_provider_result("sick", ok=False, error="500", threshold=3)
    adapters = {"sick": _provider("sick", "a"), "well": _provider("well", "b")}
    profiles = (
        ProviderProfile(name="sick", family="a", tier=3),
        ProviderProfile(name="well", family="b", tier=3),
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=profiles,
    )
    assert router.route(_request()).profile.name == "well"


def test_a_successful_call_clears_a_providers_failure_count(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    store = RuntimeStore(runtime_db)
    store.record_provider_result("one", ok=False, error="hiccup", threshold=3)
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"one": _provider("one", "a")},
        profiles=(ProviderProfile(name="one", family="a", tier=3),),
    )
    router.complete(_request())
    health = {record.provider: record for record in store.provider_health()}
    assert health["one"].consecutive_failures == 0
    assert health["one"].healthy is True


# -------------------------------------------------------------- discovery --
def test_unavailable_providers_get_no_routable_profile() -> None:
    """The v1 registry always lists every known provider, installed or not.

    Building a profile per registry key produced candidates whose ``invoke``
    raises ``NotImplementedError``. On a machine with one provider installed,
    two thirds of the routing table pointed at nothing.
    """

    registry = {
        "present": _provider("present", "a"),
        "absent": UnavailableProvider(name="absent", family="b"),
    }
    profiles = profiles_from_adapters(registry)
    assert [profile.name for profile in profiles] == ["present"]
