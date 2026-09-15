"""Choosing which model answers, and recording that it did.

A graph node says what it needs; this decides who provides it. The separation
exists so that model identity is not baked into graph semantics, and so that
routing, provenance, budgeting and independence accounting happen once instead
of at every call site.

Four rules, in the order they bind:

**Deterministic work never reaches here.** Nothing in this module is consulted
to hash a file, parse YAML, compute a frontier, rank by a criterion or run a
test. That is invariant 1, and the router cannot help enforce it -- but it is
worth stating where the model calls are, so its absence elsewhere is legible.

**Criticality is a floor, not a preference.** A `CRITICAL` request is never
silently answered by a cheaper model. If the capable provider is unavailable the
request *fails*, with a message naming what was wanted, because a scientific
review quietly performed by a weaker model is worse than a scientific review
that did not happen: the first looks like it happened.

**Independence is reported, never assumed.** A caller asks for
`DIFFERENT_FAMILY`; the router gives the strongest separation it can and records
what it actually achieved. A review that had to run on the producer's own family
is recorded as degraded, and the degradation appears in the run report. The
alternative -- claiming independence that was not obtained -- makes the whole
independent-review apparatus a decoration.

**Every call is provenance.** Provider, model, role, criticality, independence
group, prompt version, the hash of what went in, the hash of what came out,
tokens, cost, latency, status. Written whether the call succeeded or not,
because a run that reports no failures because it did not record them is
indistinguishable from a run that had none.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from research_os.automation.models import Role as AutomationRole
from research_os.automation.models import RoleSetting
from research_os.automation.providers import (
    InvocationRequest,
    ProviderAdapter,
    probe_registry,
    provider_family,
)
from research_os.errors import ResearchOSError
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger, Dimension, Grant
from research_os.runtime.interfaces import (
    ArtifactRef,
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from research_os.runtime.models import ModelCallStatus
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.runtime.routing")


class RoutingError(ResearchOSError):
    """Raised when no provider can serve a request at the required standard."""


class CriticalCapabilityUnavailableError(RoutingError):
    """Raised when critical work cannot be done well enough, so it is not done.

    Deliberately an error rather than a downgrade. The architecture degrades
    *explicitly*.
    """


class IndependenceUnavailableError(RoutingError):
    """Raised when required review independence cannot be obtained.

    A distinct class because the remedy is distinct and external: no amount of
    retrying, waiting or repairing produces a second provider family. The
    correct terminal state for a run that hits this is
    ``WAITING_FOR_EXTERNAL_DEPENDENCY`` -- the runtime did everything it could
    and what is missing is a thing the researcher installs.

    Only raised under ``review_independence: require``. The default reports the
    degradation instead, because on a one-family machine failing closed would
    mean no scientific review ever happens, and a recorded
    ``DEGRADED ... This is not independent review`` is more useful than a
    refusal.
    """


#: How the runtime's roles map onto the v1 adapter's five roles. The adapters
#: were written for the coding pipeline and take one of those; the runtime has
#: eleven roles and needs them all recorded, so the mapping is here and the
#: runtime's own role goes into provenance untouched.
_ADAPTER_ROLE: dict[ModelRole, AutomationRole] = {
    ModelRole.PLANNER: AutomationRole.PLANNER,
    ModelRole.BLIND_EXPLORER: AutomationRole.ANALYST,
    ModelRole.SEEDED_EXPLORER: AutomationRole.ANALYST,
    ModelRole.SKEPTIC: AutomationRole.REVIEWER,
    ModelRole.EXPERIMENTALIST: AutomationRole.PLANNER,
    ModelRole.SCIENTIFIC_REVIEWER: AutomationRole.REVIEWER,
    ModelRole.CODE_REVIEWER: AutomationRole.REVIEWER,
    ModelRole.AUTHOR: AutomationRole.CODER,
    ModelRole.REFEREE: AutomationRole.REVIEWER,
    ModelRole.EXTRACTOR: AutomationRole.LITERATURE,
    ModelRole.FRONTIER: AutomationRole.PLANNER,
    ModelRole.NOMINATOR: AutomationRole.REVIEWER,
}


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """What one configured provider is *declared* to do, and what it costs.

    **``tier`` is configured priority, not measured performance, and the name
    has misled a reader.** Higher means "prefer this for harder work". Nothing
    in this system has ever benchmarked a model to produce it: every available
    provider is assumed tier 3 by :func:`profiles_from_adapters`, and a
    researcher who knows better sets it. So a report saying "answered at tier 3"
    means "the configuration said this provider is suitable for critical work",
    and it does not mean anyone established that it is.

    That distinction is stated here, in ``docs/RUNTIME.md`` §11 and by
    ``researchctl runtime doctor``, rather than fixed by building a benchmark.
    A generalised model-benchmarking platform is a research project of its own,
    and the integration brief was explicit that it should not be built until
    actual routing failures demonstrate the need. What the tier has to be good
    enough for is stopping a routine extraction from going to the most
    expensive model available, and configuration is good enough for that.

    ``capabilities`` is declared the same way and carries the same caveat.
    """

    name: str
    family: str
    model: str | None = None
    #: Default 3, matching :func:`profiles_from_adapters`, and optimistic on
    #: purpose. The two disagreed at first -- the factory said 3 and this said 1
    #: -- so a hand-constructed profile could serve only ROUTINE work and
    #: everything else failed with "no healthy provider offers planning at tier
    #: >= 2", which reads like a configuration problem and is not one.
    tier: int = 3
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset(Capability)
    )
    estimated_cost_usd: float = 0.05


#: The minimum *configured* tier that may answer each criticality.
#:
#: A `CRITICAL` request will not be served below tier 3, whatever else is
#: available. What that enforces is a configuration statement rather than a
#: measurement -- see :class:`ProviderProfile` -- and the floor is still worth
#: having: it makes "this provider is not for critical work" expressible and
#: binding, which is the property a researcher with two providers of different
#: quality actually needs.
MINIMUM_TIER: dict[Criticality, int] = {
    Criticality.ROUTINE: 1,
    Criticality.NORMAL: 2,
    Criticality.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class Routed:
    profile: ProviderProfile
    independence: Independence
    note: str


class ModelRouter:
    """Routes typed model requests, records provenance, and spends the budget.

    Implements :class:`research_os.runtime.interfaces.ModelProvider`, so a graph
    node depends on one method and knows none of this.
    """

    __slots__ = (
        "_adapters",
        "_artifacts",
        "_budgets",
        "_loaded_groups",
        "_profiles",
        "_project_id",
        "_require_independence",
        "_role_settings",
        "_run_id",
        "_store",
        "_used_families",
        "_work_id",
    )

    def __init__(
        self,
        *,
        adapters: Mapping[str, ProviderAdapter],
        profiles: tuple[ProviderProfile, ...],
        store: RuntimeStore,
        artifacts: FilesystemArtifactStore,
        budgets: BudgetLedger,
        run_id: str,
        project_id: str,
        work_id: str | None = None,
        role_settings: Mapping[str, RoleSetting] | None = None,
        require_independence: bool = False,
    ) -> None:
        self._adapters = dict(adapters)
        #: The researcher's ``automation.yaml`` role settings, keyed by the v1
        #: role name. Consulted for the model and the effort of each call; the
        #: *provider* is still the router's choice, because criticality and
        #: independence are properties of the request and not of the config.
        self._role_settings = dict(role_settings or {})
        #: Whether critical work that asked for a different family must get one.
        self._require_independence = bool(require_independence)
        self._profiles = tuple(profiles)
        self._store = store
        self._artifacts = artifacts
        self._budgets = budgets
        self._run_id = run_id
        self._project_id = project_id
        self._work_id = work_id
        #: Which family already answered for each independence group, so a
        #: reviewer can be routed away from its producer. Populated lazily from
        #: the database rather than kept only in memory: a router is built per
        #: work item, so an in-memory-only map reset on every process boundary
        #: and a reviewer resumed after a crash was routed to the producer's own
        #: family and recorded as a fresh context rather than a degradation.
        self._used_families: dict[str, set[str]] = {}
        self._loaded_groups: set[str] = set()

    def _model_and_effort(
        self, request: ModelRequest, profile: ProviderProfile
    ) -> tuple[str | None, str | None]:
        """Which model answers, and at what effort.

        The researcher's configuration decides this and the router decides the
        *provider*, and the two have to be combined carefully because a model
        alias is provider-specific.

        So the configured model is applied only when the provider the router
        chose is the provider that role names. Otherwise the alias is dropped
        and the provider's own default answers -- which is precisely what
        ``automation.config.resolve_roles`` does when a role's provider is not
        installed, and for the same reason: ``opus`` means nothing to a
        different vendor's CLI, and passing it would fail the call rather than
        downgrade it.

        This exists because the runtime was passing neither. Every runtime model
        call went out with no ``--model`` and no ``--effort``, so the provider
        CLI's own default answered -- including for the three roles that map onto
        the v1 planner, whose default v1.1 changed from ``sonnet`` to ``opus``
        on thirty measured calls, recorded in ``docs/V1_BUILD_RECORD.md`` §32,
        because every structured-output exhaustion and every placeholder plan in
        that benchmark came from the smaller model. Those three roles --
        ``PLANNER``, ``EXPERIMENTALIST``, ``FRONTIER`` -- are exactly the ones
        that issue schema-constrained requests here.
        """

        adapter_role = _ADAPTER_ROLE[request.role]
        setting = self._role_settings.get(str(adapter_role))
        if setting is None:
            return profile.model, None
        if setting.provider != profile.name:
            # A different provider answered than the one this role names. The
            # alias does not travel; the effort does, because effort is a
            # provider-independent notion in the adapter contract.
            return profile.model, setting.effort
        return setting.model or profile.model, setting.effort

    # --------------------------------------------------------------- routing --
    def _candidates(self, request: ModelRequest) -> tuple[ProviderProfile, ...]:
        floor = MINIMUM_TIER[request.criticality]
        healthy = set(
            self._store.usable_providers(tuple(p.name for p in self._profiles))
        )
        return tuple(
            sorted(
                (
                    profile
                    for profile in self._profiles
                    if profile.name in self._adapters
                    and profile.name in healthy
                    and request.capability in profile.capabilities
                    and profile.tier >= floor
                ),
                key=lambda profile: (-profile.tier, profile.estimated_cost_usd),
            )
        )

    def route(self, request: ModelRequest) -> Routed:
        """Pick a provider, and say honestly how independent it is.

        Ordering: the strongest tier first for critical work, cheapest-first
        within a tier for routine work. Then, if independence was asked for,
        prefer a candidate whose family has not already answered in this group.
        """

        candidates = self._candidates(request)
        if not candidates:
            floor = MINIMUM_TIER[request.criticality]
            if request.criticality is Criticality.CRITICAL:
                raise CriticalCapabilityUnavailableError(
                    f"{request.role} is critical work needing {request.capability} at "
                    f"tier >= {floor}, and no healthy provider offers it. Refusing "
                    f"rather than answering critical work with a weaker model."
                )
            raise RoutingError(
                f"no healthy provider offers {request.capability} at tier >= {floor} "
                f"for {request.role}"
            )

        group = request.independence_group
        already = self._families_for(group) if group else set()
        if (
            request.independence
            in {
                Independence.DIFFERENT_FAMILY,
                Independence.DIFFERENT_MODEL,
            }
            and group
        ):
            fresh = [p for p in candidates if p.family not in already]
            if fresh:
                chosen = fresh[0]
                achieved = (
                    Independence.DIFFERENT_FAMILY
                    if already
                    else Independence.DIFFERENT_CONTEXT
                )
                note = (
                    f"{chosen.family} has not answered in group {group!r}"
                    if already
                    else f"first call in group {group!r}; no producer to differ from yet"
                )
                return Routed(profile=chosen, independence=achieved, note=note)
            chosen = candidates[0]
            if (
                self._require_independence
                and request.criticality is Criticality.CRITICAL
            ):
                # Fail closed into an external dependency. Not a retry and not a
                # downgrade: no second provider family will appear because we
                # asked again, and a critical scientific review performed by the
                # producer's own family is the one degradation this mode exists
                # to refuse.
                raise IndependenceUnavailableError(
                    f"{request.role} is critical work that requires "
                    f"{request.independence}, and every healthy provider is in a "
                    f"family that has already answered in group {group!r} "
                    f"({sorted(already)}). Refusing rather than recording a "
                    f"same-family review as independent. Install a second "
                    f"provider family, or set `review_independence: prefer` in "
                    f"runtime.yaml to accept a recorded degradation."
                )
            return Routed(
                profile=chosen,
                independence=Independence.DIFFERENT_CONTEXT,
                note=(
                    f"DEGRADED: {request.independence} was requested for group "
                    f"{group!r} but every healthy provider is in a family that has "
                    f"already answered ({sorted(already)}). Ran on {chosen.family} "
                    f"in a fresh context. This is not independent review."
                ),
            )
        return Routed(
            profile=candidates[0],
            independence=(
                Independence.DIFFERENT_CONTEXT
                if request.independence is not Independence.NONE
                else Independence.NONE
            ),
            note="fresh context",
        )

    def _families_for(self, group: str) -> set[str]:
        """Which provider families have already answered in this group.

        Read from ``model_calls`` once per group per router, then kept in
        memory. The database is the durable record, and it is what makes the
        accounting correct across the crash-and-resume that a bounded cycle is
        designed to survive.
        """

        if group in self._loaded_groups:
            return self._used_families.setdefault(group, set())
        families = {profile.name: profile.family for profile in self._profiles}
        with self._store.db.tx() as conn:
            rows = conn.execute(
                "select distinct provider from model_calls "
                "where independence_group = %s and status = 'OK'",
                (group,),
            ).fetchall()
        seen = {
            families.get(str(row["provider"]), str(row["provider"])) for row in rows
        }
        # Merged into the stored set, not returned beside it. The first version
        # returned `{} | seen` -- a fresh set -- and marked the group loaded,
        # so every later lookup took the hot path and found an *empty* set with
        # the database-derived families gone. The consequence was the worst kind:
        # a reviewer routed to the producer's own family and recorded as
        # `DIFFERENT_FAMILY` with a note naming a provider that had in fact
        # already answered. False independence provenance, which is the one
        # thing this accounting exists to prevent. An independent review found
        # it and reproduced it.
        merged = self._used_families.setdefault(group, set())
        merged |= seen
        self._loaded_groups.add(group)
        return merged

    # --------------------------------------------------------------- calling --
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Answer one typed request, recording provenance either way."""

        routed = self.route(request)
        profile = routed.profile
        adapter = self._adapters[profile.name]

        # Reserve both dimensions or neither. The first version took them in
        # two unguarded statements, so the *expected* outcome -- the cost budget
        # refusing -- leaked up to three HELD call-budget rows with no handle on
        # them, and a cost-exhausted run silently exhausted its call budget too
        # and then misreported which one ran out.
        grants: tuple[Grant, ...] = ()
        cost_grants: tuple[Grant, ...] = ()
        try:
            grants = self._budgets.reserve_all(
                dimension=Dimension.MODEL_CALLS,
                amount=1,
                run_id=self._run_id,
                project_id=self._project_id,
                work_id=self._work_id,
            )
            cost_grants = self._budgets.reserve_all(
                dimension=Dimension.MODEL_COST_USD,
                amount=profile.estimated_cost_usd,
                run_id=self._run_id,
                project_id=self._project_id,
                work_id=self._work_id,
            )
            prompt_ref = self._artifacts.put_text(
                request.prompt,
                role="prompt",
                producer=f"{request.role}:{request.prompt_version}",
            )
        except BaseException:
            self._budgets.release_all(cost_grants)
            self._budgets.release_all(grants)
            raise

        started = time.monotonic()
        model, effort = self._model_and_effort(request, profile)
        try:
            result = adapter.invoke(
                InvocationRequest(
                    role=_ADAPTER_ROLE[request.role],
                    prompt=request.prompt,
                    cwd=self._artifacts.root,
                    read_only=True,
                    timeout_seconds=request.timeout_seconds,
                    model=model,
                    effort=effort,
                    json_schema=dict(request.json_schema)
                    if request.json_schema
                    else None,
                )
            )
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            self._budgets.release_all(cost_grants)
            self._budgets.settle_all(grants)
            self._store.record_provider_result(profile.name, ok=False, error=str(exc))
            self._record(
                request,
                routed,
                status=ModelCallStatus.FAILED,
                prompt_ref=prompt_ref,
                output_ref=None,
                latency_ms=latency,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        latency = int((time.monotonic() - started) * 1000)
        raw = result.stdout or result.text or ""
        output_ref = self._artifacts.put_text(
            raw, role="model_output", producer=f"{profile.name}:{request.role}"
        )

        if not result.ok:
            status = (
                ModelCallStatus.TIMEOUT if result.timed_out else ModelCallStatus.FAILED
            )
            self._budgets.release_all(cost_grants)
            self._budgets.settle_all(grants)
            self._store.record_provider_result(
                profile.name, ok=False, error=result.error or "non-zero exit"
            )
            self._record(
                request,
                routed,
                status=status,
                prompt_ref=prompt_ref,
                output_ref=output_ref,
                latency_ms=latency,
                error=result.error or f"exit {result.exit_code}",
                resolved_model=result.resolved_model,
            )
            return ModelResponse(
                provider=profile.name,
                model=result.resolved_model or profile.model,
                independence=routed.independence,
                independence_note=routed.note,
                latency_ms=latency,
                error=result.error or f"exit {result.exit_code}",
            )

        if request.json_schema is not None and result.structured is None:
            self._budgets.settle_all(cost_grants, actual=result.total_cost_usd)
            self._budgets.settle_all(grants)
            self._store.record_provider_result(profile.name, ok=True)
            self._record(
                request,
                routed,
                status=ModelCallStatus.MALFORMED,
                prompt_ref=prompt_ref,
                output_ref=output_ref,
                latency_ms=latency,
                error="a structured response was required and none was returned",
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
                cost=result.total_cost_usd,
                resolved_model=result.resolved_model,
            )
            return ModelResponse(
                provider=profile.name,
                model=result.resolved_model or profile.model,
                text=result.text,
                independence=routed.independence,
                independence_note=routed.note,
                latency_ms=latency,
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
                cost_usd=result.total_cost_usd,
                error="malformed structured output",
            )

        self._budgets.settle_all(cost_grants, actual=result.total_cost_usd)
        self._budgets.settle_all(grants)
        self._store.record_provider_result(profile.name, ok=True)
        if request.independence_group:
            self._loaded_groups.add(request.independence_group)
            self._used_families.setdefault(request.independence_group, set()).add(
                profile.family
            )
        self._record(
            request,
            routed,
            status=ModelCallStatus.OK,
            prompt_ref=prompt_ref,
            output_ref=output_ref,
            latency_ms=latency,
            error=None,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            cost=result.total_cost_usd,
            resolved_model=result.resolved_model,
        )
        return ModelResponse(
            provider=profile.name,
            model=result.resolved_model or profile.model,
            text=result.text,
            structured=result.structured,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            cost_usd=result.total_cost_usd,
            latency_ms=latency,
            independence=routed.independence,
            independence_note=routed.note,
        )

    def _record(
        self,
        request: ModelRequest,
        routed: Routed,
        *,
        status: ModelCallStatus,
        prompt_ref: ArtifactRef,
        output_ref: ArtifactRef | None,
        latency_ms: int,
        error: str | None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost: float | None = None,
        resolved_model: str | None = None,
    ) -> None:
        self._store.record_model_call(
            run_id=self._run_id,
            work_id=self._work_id,
            provider=routed.profile.name,
            # What answered, not what was asked for. A profile's `model` is a
            # configuration hint and is usually empty; the adapter reports the
            # model the provider actually used, and that is what provenance
            # needs. "Reviewed by claude" is not an auditable statement.
            model=resolved_model or routed.profile.model,
            role=str(request.role),
            status=status,
            criticality=str(request.criticality),
            independence_group=request.independence_group,
            independence=str(routed.independence),
            independence_note=routed.note,
            prompt_version=request.prompt_version,
            input_digest=prompt_ref.artifact_id,
            output_artifact_id=output_ref.artifact_id if output_ref else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=Decimal(str(cost)) if cost is not None else None,
            latency_ms=latency_ms,
            error=(f"{error} [independence: {routed.note}]" if error else None),
        )
        if routed.note.startswith("DEGRADED"):
            LOG.warning("%s: %s", request.role, routed.note)


def profiles_from_adapters(
    adapters: Mapping[str, ProviderAdapter],
    *,
    tiers: Mapping[str, int] | None = None,
    probe: bool = True,
) -> tuple[ProviderProfile, ...]:
    """Build profiles for the providers this machine actually has.

    ``probe=True`` is the important default, and it was not the original one.
    The v1 registry always contains an entry for every *known* provider --
    ``MissingProvider`` placeholders for the ones that are not installed -- so
    building a profile per registry key produced routable candidates whose
    ``invoke`` raises ``NotImplementedError``. On a machine with one provider
    installed, two thirds of the routing table pointed at nothing.

    Every available provider is assumed capable of everything at tier 3 unless
    told otherwise. That is deliberately optimistic: a hard-coded table of
    vendor abilities is wrong within a month, and the researcher who configured
    a provider is better placed to tier it than this module is.
    """

    names = sorted(adapters)
    if probe:
        probes = probe_registry(dict(adapters))
        names = [
            name for name in names if getattr(probes.get(name), "available", False)
        ]
    return tuple(
        ProviderProfile(
            name=name,
            family=provider_family(name) or name,
            tier=(tiers or {}).get(name, 3),
        )
        for name in names
    )
