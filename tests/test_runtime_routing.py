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

from research_os.automation.models import Access, RoleSetting
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetExhaustedError, BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import (
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelRole,
)
from research_os.runtime.models import BudgetScope, ModelCallStatus
from research_os.runtime.routing import (
    _ADAPTER_ROLE,
    CriticalCapabilityUnavailableError,
    ModelRouter,
    ProviderCallFailedError,
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
    role_settings: dict[str, object] | None = None,
    require_independence: bool = False,
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
        role_settings=role_settings,  # type: ignore[arg-type]
        require_independence=require_independence,
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
    # Raised, not returned. A provider that did not answer is not a response
    # a caller may reason about: eleven callers used to reason about it, and
    # the one in the planner node concluded DONE_FOR_NOW. See
    # `ProviderCallFailedError`.
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(_request())
    assert raised.value.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert raised.value.attempted is True

    # And recorded regardless, which is what this test has always been about.
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


def test_every_runtime_model_call_is_read_only_and_tool_less(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """The actual enforcement of role least privilege.

    `policy.ROLE_PERMISSIONS` specifies what each role may hold; this is what
    makes it true. Every request the router builds is `read_only=True` with no
    `access`, which the v1 adapter contract resolves to `CONTEXT_ONLY` and which
    force-empties the tool set. A model that cannot read a file, run a command
    or reach the network cannot exceed its role whatever the role says -- and no
    amount of prompt injection in the material under review can change that.
    """

    from research_os.automation.models import Access

    provider = _provider("only", "a")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"only": provider},
        profiles=(ProviderProfile(name="only", family="a", tier=3),),
    )
    for role, capability in (
        (ModelRole.PLANNER, Capability.PLANNING),
        (ModelRole.SKEPTIC, Capability.CRITIQUE),
        (ModelRole.AUTHOR, Capability.SYNTHESIS),
        (ModelRole.SCIENTIFIC_REVIEWER, Capability.CRITIQUE),
        (ModelRole.EXTRACTOR, Capability.STRUCTURED_EXTRACTION),
    ):
        router.complete(
            _request(role=role, capability=capability, criticality=Criticality.NORMAL)
        )

    assert provider.calls, "no request reached the adapter"
    for request in provider.calls:
        assert request.read_only is True, f"{request.role} was not read-only"
        assert request.access is Access.CONTEXT_ONLY, (
            f"{request.role} got {request.access}"
        )
        assert request.tools == (), f"{request.role} was handed tools: {request.tools}"


def test_independence_accounting_survives_a_router_rebuild(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """The bug that produced false independence provenance.

    A router is built per work item, so a cycle that crashes and resumes gets a
    new one. The DB-derived families were read once and then discarded, so a
    rebuilt router routed the reviewer to the producer's own family and recorded
    it as DIFFERENT_FAMILY with a note naming a provider that had in fact
    already answered. Found and reproduced by an independent review.
    """

    families = {"one": "family-one", "two": "family-two", "three": "family-three"}
    adapters = {name: _provider(name, family) for name, family in families.items()}
    profiles = tuple(
        ProviderProfile(name=name, family=family, tier=3)
        for name, family in families.items()
    )
    group = "explore:RRUN-x:0"

    def build() -> ModelRouter:
        return _router(
            runtime_db,
            tmp_path,
            run_id=run_id,
            project_id=runtime_project,
            adapters=adapters,
            profiles=profiles,
        )

    first = build().complete(
        _request(
            role=ModelRole.SEEDED_EXPLORER,
            capability=Capability.SYNTHESIS,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )

    # A fresh router, as a resumed cycle gets, then two calls in the group.
    rebuilt = build()
    second = rebuilt.complete(
        _request(
            role=ModelRole.BLIND_EXPLORER,
            capability=Capability.SYNTHESIS,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    third = rebuilt.complete(
        _request(
            role=ModelRole.SKEPTIC,
            capability=Capability.CRITIQUE,
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    answered = {first.provider, second.provider, third.provider}
    assert len(answered) == 3, (
        f"a rebuilt router reused a family that had already answered: {answered}"
    )
    for response in (second, third):
        if response.independence is Independence.DIFFERENT_FAMILY:
            assert response.provider not in {first.provider}, (
                "claimed a different family while using the producer's provider"
            )


def test_a_rebuilt_router_reports_degradation_rather_than_fresh_context(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """With one family, a resumed reviewer must still say so.

    Before the fix it read `already={}` and returned "first call in group; no
    producer to differ from yet" -- suppressing a degradation that had already
    happened.
    """

    adapters = {"only": _provider("only", "one-family")}
    profiles = (ProviderProfile(name="only", family="one-family", tier=3),)
    group = "explore:RRUN-y:0"

    def build() -> ModelRouter:
        return _router(
            runtime_db,
            tmp_path,
            run_id=run_id,
            project_id=runtime_project,
            adapters=adapters,
            profiles=profiles,
        )

    build().complete(
        _request(
            role=ModelRole.SEEDED_EXPLORER,
            capability=Capability.SYNTHESIS,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    resumed = build().complete(
        _request(
            role=ModelRole.SKEPTIC,
            capability=Capability.CRITIQUE,
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert resumed.independence_note.startswith("DEGRADED")
    assert "not independent review" in resumed.independence_note
    call = next(
        c
        for c in RuntimeStore(runtime_db).list_model_calls(run_id=run_id)
        if c.role == str(ModelRole.SKEPTIC)
    )
    assert call.independence_note and call.independence_note.startswith("DEGRADED")


# ------------------------------------------------ the researcher's models ----
def _role_setting(
    *, provider: str, model: str | None, effort: str | None
) -> RoleSetting:
    return RoleSetting(
        provider=provider,
        model=model,
        effort=effort,
        read_only=True,
        access=Access.CONTEXT_ONLY,
        tools=(),
    )


def test_the_configured_role_model_and_effort_reach_the_provider(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """Every runtime call used to go out with no ``--model`` and no ``--effort``.

    So the provider CLI's own default answered -- including for the three
    runtime roles that map onto the v1 planner, whose default v1.1 changed from
    ``sonnet`` to ``opus`` on thirty measured calls because every
    structured-output exhaustion and every placeholder plan in that benchmark
    came from the smaller model. Those three roles are exactly the ones that
    issue schema-constrained requests.
    """

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("claude", "anthropic")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": adapter},
        profiles=(ProviderProfile(name="claude", family="anthropic", tier=3),),
        role_settings={
            "planner": _role_setting(provider="claude", model="opus", effort="high")
        },
    )
    router.complete(
        ModelRequest(
            role=ModelRole.PLANNER,
            capability=Capability.PLANNING,
            prompt="what next",
            prompt_version="planner@1",
            criticality=Criticality.NORMAL,
        )
    )
    assert adapter.calls[-1].model == "opus"
    assert adapter.calls[-1].effort == "high"


def test_a_configured_model_is_dropped_when_another_provider_answers(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """A model alias is provider-specific, so it must not travel.

    ``resolve_roles`` already drops the model when it re-homes a role onto a
    different provider, for the same reason: ``opus`` means nothing to another
    vendor's CLI, and passing it would fail the call rather than downgrade it.
    The router chooses the provider -- criticality and independence are
    properties of the request -- so it has to make the same decision.
    """

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("codex", "openai")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"codex": adapter},
        profiles=(ProviderProfile(name="codex", family="openai", tier=3),),
        role_settings={
            "planner": _role_setting(provider="claude", model="opus", effort="high")
        },
    )
    router.complete(
        ModelRequest(
            role=ModelRole.PLANNER,
            capability=Capability.PLANNING,
            prompt="what next",
            prompt_version="planner@1",
            criticality=Criticality.NORMAL,
        )
    )
    assert adapter.calls[-1].model is None, (
        "a provider-specific alias reached a different provider"
    )
    # Effort is provider-independent in the adapter contract, so it survives.
    assert adapter.calls[-1].effort == "high"


def test_a_role_with_no_configuration_uses_the_profile_model(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("claude", "anthropic")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": adapter},
        profiles=(
            ProviderProfile(
                name="claude", family="anthropic", model="profile-default", tier=3
            ),
        ),
        role_settings={},
    )
    router.complete(
        ModelRequest(
            role=ModelRole.PLANNER,
            capability=Capability.PLANNING,
            prompt="what next",
            prompt_version="planner@1",
            criticality=Criticality.NORMAL,
        )
    )
    assert adapter.calls[-1].model == "profile-default"
    assert adapter.calls[-1].effort is None


# ------------------------------------------- required review independence ----
def test_prefer_records_the_degradation_and_runs(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """The default, and the right one for a one-family machine.

    Failing closed here would mean no scientific review ever happens, and a
    recorded ``DEGRADED ... This is not independent review`` is more useful than
    a refusal.
    """

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("claude", "anthropic")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": adapter},
        profiles=(ProviderProfile(name="claude", family="anthropic", tier=3),),
    )
    group = f"review:{run.run_id}"
    # The producer answers first, so its family has already been used.
    router.complete(
        ModelRequest(
            role=ModelRole.AUTHOR,
            capability=Capability.SYNTHESIS,
            prompt="draft",
            prompt_version="author@1",
            criticality=Criticality.NORMAL,
            independence_group=group,
        )
    )
    response = router.complete(
        ModelRequest(
            role=ModelRole.SCIENTIFIC_REVIEWER,
            capability=Capability.CRITIQUE,
            prompt="review",
            prompt_version="scientific_reviewer@1",
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert response.ok
    assert "not independent review" in response.independence_note


def test_require_refuses_a_same_family_critical_review(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """Fails closed into an external dependency, and says what to install.

    A scientific review quietly performed by the producer's own family is worse
    than one that did not happen: the first looks like it happened.
    """

    from research_os.runtime.routing import IndependenceUnavailableError

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("claude", "anthropic")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": adapter},
        profiles=(ProviderProfile(name="claude", family="anthropic", tier=3),),
        require_independence=True,
    )
    group = f"review:{run.run_id}"
    router.complete(
        ModelRequest(
            role=ModelRole.AUTHOR,
            capability=Capability.SYNTHESIS,
            prompt="draft",
            prompt_version="author@1",
            criticality=Criticality.NORMAL,
            independence_group=group,
        )
    )
    with pytest.raises(IndependenceUnavailableError) as raised:
        router.complete(
            ModelRequest(
                role=ModelRole.SCIENTIFIC_REVIEWER,
                capability=Capability.CRITIQUE,
                prompt="review",
                prompt_version="scientific_reviewer@1",
                criticality=Criticality.CRITICAL,
                independence=Independence.DIFFERENT_FAMILY,
                independence_group=group,
            )
        )
    message = str(raised.value)
    assert "Install a second provider family" in message
    assert "review_independence: prefer" in message


def test_require_does_not_bind_on_routine_work(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    """Otherwise a literature extraction stops because no second vendor exists.

    Which has nothing to do with review independence, and would make the mode
    unusable for the deployments that want it.
    """

    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    adapter = _provider("claude", "anthropic")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": adapter},
        profiles=(ProviderProfile(name="claude", family="anthropic", tier=3),),
        require_independence=True,
    )
    group = f"extract:{run.run_id}"
    router.complete(
        ModelRequest(
            role=ModelRole.AUTHOR,
            capability=Capability.SYNTHESIS,
            prompt="draft",
            prompt_version="author@1",
            criticality=Criticality.NORMAL,
            independence_group=group,
        )
    )
    response = router.complete(
        ModelRequest(
            role=ModelRole.EXTRACTOR,
            capability=Capability.STRUCTURED_EXTRACTION,
            prompt="extract",
            prompt_version="extractor@1",
            criticality=Criticality.ROUTINE,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert response.ok


def test_require_is_satisfied_when_a_second_family_exists(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    store = RuntimeStore(runtime_db)
    run = store.create_run(project_id=runtime_project, objective="o")
    claude = _provider("claude", "anthropic")
    codex = _provider("codex", "openai")
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run.run_id,
        project_id=runtime_project,
        adapters={"claude": claude, "codex": codex},
        profiles=(
            ProviderProfile(name="claude", family="anthropic", tier=3),
            ProviderProfile(name="codex", family="openai", tier=3),
        ),
        require_independence=True,
    )
    group = f"review:{run.run_id}"
    producer = router.complete(
        ModelRequest(
            role=ModelRole.AUTHOR,
            capability=Capability.SYNTHESIS,
            prompt="draft",
            prompt_version="author@1",
            criticality=Criticality.NORMAL,
            independence_group=group,
        )
    )
    reviewer = router.complete(
        ModelRequest(
            role=ModelRole.SCIENTIFIC_REVIEWER,
            capability=Capability.CRITIQUE,
            prompt="review",
            prompt_version="scientific_reviewer@1",
            criticality=Criticality.CRITICAL,
            independence=Independence.DIFFERENT_FAMILY,
            independence_group=group,
        )
    )
    assert reviewer.ok
    assert reviewer.provider != producer.provider
    assert reviewer.independence is Independence.DIFFERENT_FAMILY


# ---------------------------------------- a provider that did not answer ----
#
# Three ways `ModelRouter.complete` can fail to get an answer, and one way it
# can get a bad one. The first three raise and the fourth returns, because
# only the fourth is an answer -- see `ProviderCallFailedError`. Each is
# checked separately because the classes differ, and the class is what picks
# the backoff.


def test_a_provider_that_cannot_be_invoked_is_transient_not_unknown(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """An adapter that raises used to fall through to UNKNOWN, which is terminal.

    The daemon's classifier maps exception *types*, and a subprocess failure is
    an `OSError` or whatever else the adapter produced -- no mapping, so
    `UNKNOWN`, whose policy is FAIL_PERMANENTLY. A provider process that could
    not be spawned is about as transient as a failure gets, and it was the one
    failure this runtime would never retry.
    """

    class Exploding(FakeProvider):
        def invoke(self, request: object) -> object:  # type: ignore[override]
            raise OSError("the provider executable could not be spawned")

    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"boom": Exploding(name="boom", family="a")},
        profiles=(ProviderProfile(name="boom", family="a", tier=3),),
    )
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(_request())
    assert raised.value.failure_class is FailureClass.PROVIDER_TRANSIENT
    assert raised.value.attempted is True
    # Recorded, and the breaker advanced, exactly as for any other failure.
    assert RuntimeStore(runtime_db).list_model_calls(run_id=run_id)[0].status is (
        ModelCallStatus.FAILED
    )


def test_a_timed_out_provider_is_classified_as_a_timeout(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """A timeout and an outage want different backoffs, so they are different classes."""

    slow = FakeProvider(
        name="slow",
        family="a",
        responses={
            "planner": [
                ScriptedResponse(
                    structured=None,
                    exit_code=None,
                    timed_out=True,
                    error="provider timed out after 600s",
                )
            ]
        },
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"slow": slow},
        profiles=(ProviderProfile(name="slow", family="a", tier=3),),
    )
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(_request())
    assert raised.value.failure_class is FailureClass.PROVIDER_TIMEOUT
    assert RuntimeStore(runtime_db).list_model_calls(run_id=run_id)[0].status is (
        ModelCallStatus.TIMEOUT
    )


def test_being_refused_by_an_open_breaker_carries_its_deadline_and_costs_nothing(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """The routing refusal, which is the path that must not spend an attempt.

    No invocation happens, so there is nothing to charge for -- and the
    breaker already knows when it will lift, which is the only number that
    makes the retry schedule correct. Before this the refusal carried neither,
    and the item's three attempts were spent inside a five-minute cooldown.
    """

    store = RuntimeStore(runtime_db)
    health = None
    for _ in range(3):
        health = store.record_provider_result(
            "sick", ok=False, error="500", threshold=3, cooldown=300
        )
    assert health is not None and health.cooldown_until is not None

    adapters = {"sick": _provider("sick", "a")}
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters=adapters,
        profiles=(ProviderProfile(name="sick", family="a", tier=3),),
    )
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(_request())
    assert raised.value.attempted is False, (
        "an attempt was charged for a call that was never made"
    )
    assert raised.value.retry_at == health.cooldown_until
    assert raised.value.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    # Nothing was invoked, so nothing was recorded against the run.
    assert store.list_model_calls(run_id=run_id) == ()


def test_critical_work_refused_while_cooling_still_carries_the_deadline(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """`CriticalCapabilityUnavailableError` is a provider failure with a clock.

    It is raised when the only provider strong enough is unavailable, and
    "unavailable" is frequently "in a cooldown that ends in four minutes". A
    sibling class without a deadline would have been retried blind.
    """

    store = RuntimeStore(runtime_db)
    health = None
    for _ in range(3):
        health = store.record_provider_result(
            "strong", ok=False, error="500", threshold=3, cooldown=300
        )
    assert health is not None

    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"strong": _provider("strong", "a")},
        profiles=(ProviderProfile(name="strong", family="a", tier=3),),
    )
    with pytest.raises(CriticalCapabilityUnavailableError) as raised:
        router.complete(_request(criticality=Criticality.CRITICAL))
    assert isinstance(raised.value, ProviderCallFailedError)
    assert raised.value.retry_at == health.cooldown_until
    assert raised.value.attempted is False


def test_a_refusal_with_no_provider_at_all_names_no_deadline(
    runtime_db: Database, tmp_path: Path, run_id: str, runtime_project: str
) -> None:
    """Waiting does not install a provider, and the failure must not imply it does.

    ``retry_at`` is None when nothing is merely cooling. That is the
    difference between "wait four minutes" and "this machine has no provider
    that can do this", and a retry policy that could not tell them apart would
    wait forever for one that will never exist.
    """

    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"weak": _provider("weak", "a")},
        profiles=(ProviderProfile(name="weak", family="a", tier=1),),
    )
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(_request(criticality=Criticality.CRITICAL))
    assert raised.value.retry_at is None
    assert raised.value.attempted is False


# ------------------------------------------------- the adapter role table --
#
# Found by the first dogfood, not by this file. Every test above routes a role
# the coding pipeline already had, and `ScriptedRouter` -- which every one of
# the 244 discovery-portfolio tests uses -- never reaches `_ADAPTER_ROLE` at
# all. So the seam between a role existing and a role being routable had no
# test on either side of it, and twelve roles fell through it.
def test_every_model_role_has_an_adapter_role() -> None:
    """A role the router cannot bucket is a role that cannot be called.

    ``_ADAPTER_ROLE`` is subscripted, not ``.get``-ed, so a missing entry is a
    ``KeyError`` raised inside ``complete()`` after the budget is reserved and
    before any provider is asked. On the first dogfood that was twelve of the
    fourteen roles the discovery portfolio added: an idea could be generated
    and then nothing could be done to it -- no screen, no falsifier, no
    review -- while the suite stayed green.

    This is the cheap half of the fix and the one that holds: adding a
    ``ModelRole`` without a line in the table fails here.
    """

    missing = sorted(role for role in ModelRole if role not in _ADAPTER_ROLE)
    assert not missing, (
        "these ModelRole members have no _ADAPTER_ROLE entry, so every call "
        f"made with one raises KeyError inside the router: {missing}"
    )


@pytest.mark.parametrize(
    "role",
    [
        ModelRole.NOVELTY_SCREENER,
        ModelRole.FALSIFIER,
        ModelRole.SCIENTIFIC_DISCOVERY,
        ModelRole.LITERATURE_SCOUT,
        ModelRole.METHODOLOGY_REVIEWER,
        ModelRole.NOVELTY_REVIEWER,
        ModelRole.SKEPTIC_REVIEWER,
        ModelRole.REPLICATOR,
        ModelRole.META_REVIEWER,
        ModelRole.DUPLICATE_ADJUDICATOR,
        ModelRole.BRANCHER,
        ModelRole.FAILURE_MINING_EXPLORER,
    ],
)
def test_a_portfolio_role_routes_through_the_real_router(
    runtime_db: Database,
    tmp_path: Path,
    run_id: str,
    runtime_project: str,
    role: ModelRole,
) -> None:
    """The other half: drive each portfolio role through ``ModelRouter`` itself.

    The exhaustiveness test above would pass against a table whose entries
    named buckets no adapter serves. This one makes the call.
    """

    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"one": _provider("one", "a")},
        profiles=(ProviderProfile(name="one", family="a", tier=3),),
    )
    response = router.complete(_request(role=role, capability=Capability.CRITIQUE))

    assert response.ok, response.error
    assert response.call_id is not None
    call = RuntimeStore(runtime_db).get_model_call(response.call_id)
    assert call is not None
    assert call.role == role


def test_a_routing_failure_after_reserving_releases_the_reservation(
    runtime_db: Database,
    tmp_path: Path,
    run_id: str,
    runtime_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was asked, so nothing may stay held.

    The dogfood's second finding, and a consequence of the first. Both grant
    sets are taken before the provider is chosen; the one line that stood
    between the reservation and the invocation was outside the guard that
    releases them. Eight failed stages left $0.40 of a $15 project ceiling
    reserved forever, and the portfolio's eventual stop would have been
    reported as ``PAUSED_BUDGET_EXHAUSTED`` -- money it never spent.

    The failure is injected rather than reproduced through a missing table
    entry, because the table is exhaustive now and this must keep holding for
    whatever raises there next.
    """

    ledger = BudgetLedger(runtime_db)
    ledger.set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal("15.00"),
    )
    router = _router(
        runtime_db,
        tmp_path,
        run_id=run_id,
        project_id=runtime_project,
        adapters={"one": _provider("one", "a")},
        profiles=(
            ProviderProfile(
                name="one", family="a", tier=3, estimated_cost_usd=Decimal("0.05")
            ),
        ),
    )

    def boom(*_args: object, **_kwargs: object) -> tuple[str | None, str | None]:
        raise KeyError(ModelRole.NOVELTY_SCREENER)

    # The class, not the instance: ``ModelRouter`` has ``__slots__``.
    monkeypatch.setattr(ModelRouter, "_model_and_effort", boom)

    with pytest.raises(KeyError):
        router.complete(_request())

    for dimension in (Dimension.MODEL_COST_USD, Dimension.MODEL_CALLS):
        budget = ledger.get(
            scope=BudgetScope.PROJECT, scope_id=runtime_project, dimension=dimension
        )
        if budget is None:
            continue
        assert budget.reserved == Decimal(0), (
            f"{dimension} kept a reservation for a call that never happened"
        )
        assert budget.spent == Decimal(0)
