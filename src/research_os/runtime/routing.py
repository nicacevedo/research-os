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
from datetime import datetime
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
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import (
    ArtifactRef,
    Capability,
    Criticality,
    Independence,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from research_os.runtime.models import ModelCallStatus, ProviderHealth
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.runtime.routing")


class RoutingError(ResearchOSError):
    """Raised when no provider can serve a request at the required standard."""


class ProviderCallFailedError(RoutingError):
    """Raised when the provider did not answer. Never returned as a response.

    **This is the choke point the 2026-09-19 incident was missing.** Before it,
    a provider that failed came back as a ``ModelResponse`` with ``ok`` false,
    and the caller decided what that meant. Eleven callers decided eleven
    times. Most of them got it right and called it ``MODEL_OUTPUT_INVALID`` --
    which is wrong in a quiet way, because the model did not produce invalid
    output, it produced none. The planner node got it wrong in a loud way: it
    folded the failure into ``plan_refusal``, and "nothing I am permitted to
    do" concluded ``DONE_FOR_NOW``.

    An OAuth token that could not be refreshed became three research runs
    reported to a researcher as finished, with a scientific decision waiting
    for them that did not exist.

    So the router no longer offers that choice. "The provider answered
    something unusable" is a response; "the provider did not answer" is an
    exception, and the only place that may decide what to do about it is the
    work queue, which retries against the provider's actual availability.

    ``retry_at`` carries the breaker's ``cooldown_until`` when one is open, so
    the queue can schedule past guaranteed unavailability instead of spending
    its attempts inside it. ``attempted`` is false when routing refused before
    any invocation, which means the attempt should not be charged: being turned
    away by an open breaker is not a failed try.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass = FailureClass.PROVIDER_UNAVAILABLE,
        retry_at: datetime | None = None,
        attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retry_at = retry_at
        self.attempted = attempted


class CriticalCapabilityUnavailableError(ProviderCallFailedError):
    """Raised when critical work cannot be done well enough, so it is not done.

    Deliberately an error rather than a downgrade. The architecture degrades
    *explicitly*.

    A :class:`ProviderCallFailedError` because that is what it is -- no
    provider answered -- and because the reason may be temporary. The strongest
    provider being in a breaker cooldown is exactly how critical work becomes
    unservable on a machine whose weaker providers are healthy, and that heals
    by itself. Making it a sibling rather than a subclass would have meant
    retrying it without a cooldown deadline.
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
#: more roles than that and needs them all recorded, so the mapping is here and
#: the runtime's own role goes into provenance untouched.
#:
#: **This table must cover every** :class:`ModelRole`, and
#: ``test_every_model_role_has_an_adapter_role`` asserts that it does rather
#: than trusting this sentence. It is not documentation: the lookup below is a
#: bare subscript, so a role with no entry raises ``KeyError`` deep inside
#: ``complete()`` -- after the budget has been reserved and before any provider
#: has been asked.
#:
#: The first dogfood found exactly that. Twelve of the fourteen roles the
#: discovery portfolio added were missing here, so every portfolio model call
#: except the two explorers -- the screen, the falsifier, the discovery pass,
#: the literature scout, all three reviewers, the replicator, the meta-reviewer,
#: the duplicate adjudicator and the brancher -- died in the router. The whole
#: pipeline downstream of generating an idea was unreachable against a real
#: provider, and 4,308 tests passed, because the test doubles are adapters of
#: their own and never consult this table. A role added to the enum without a
#: line here now fails the suite.
#:
#: The bucket follows the template's declared ``Capability``: ``CRITIQUE`` is
#: REVIEWER, generation is ANALYST, and work over retrieved sources is
#: LITERATURE. It decides which model answers and nothing else -- the router
#: hard-codes ``read_only=True``, so an invocation's tool set is empty whatever
#: this says.
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
    # ANALYST, not REVIEWER: a derivation is produced work, and routing it as
    # review would put it in the pool the *critique* of it must come from.
    ModelRole.DERIVER: AutomationRole.ANALYST,
    # --- the discovery portfolio's roles ---------------------------------
    #
    # Generators, for the same reason DERIVER is one: what they produce is the
    # thing a reviewer later attacks, so they must not be drawn from the
    # reviewer pool.
    ModelRole.FAILURE_MINING_EXPLORER: AutomationRole.ANALYST,
    ModelRole.SCIENTIFIC_DISCOVERY: AutomationRole.ANALYST,
    ModelRole.BRANCHER: AutomationRole.ANALYST,
    # Work over retrieved sources, which is what LITERATURE already means here.
    # NOVELTY_SCREENER is the cheap "does this look known" pass and
    # LITERATURE_SCOUT the audit that reads a corpus; both are literature
    # questions, and keeping them out of ANALYST keeps the generator pool and
    # the pool that checks a generator's novelty claim distinct.
    ModelRole.NOVELTY_SCREENER: AutomationRole.LITERATURE,
    ModelRole.LITERATURE_SCOUT: AutomationRole.LITERATURE,
    # Critique. The falsifier is here despite its docstring saying it is not a
    # reviewer: that sentence is about the portfolio's own role taxonomy -- it
    # is not one of the three board reviewers a gate counts -- and what it does
    # to an idea is adversarial reading, which is what this bucket selects a
    # model for.
    ModelRole.FALSIFIER: AutomationRole.REVIEWER,
    ModelRole.METHODOLOGY_REVIEWER: AutomationRole.REVIEWER,
    ModelRole.NOVELTY_REVIEWER: AutomationRole.REVIEWER,
    ModelRole.SKEPTIC_REVIEWER: AutomationRole.REVIEWER,
    ModelRole.REPLICATOR: AutomationRole.REVIEWER,
    ModelRole.META_REVIEWER: AutomationRole.REVIEWER,
    # ANALYST: deciding whether two candidate directions are the same is a
    # judgement about text this system produced, not a reading of literature
    # and not a critique of science.
    ModelRole.DUPLICATE_ADJUDICATOR: AutomationRole.ANALYST,
    # ANALYST, and deliberately not PLANNER: the experimentalist routes to
    # PLANNER, and the author of a threshold should not be drawn from the
    # pool that authors the grid it will be applied to. On a one-family host
    # both are the same family anyway, and that is recorded, not hidden.
    ModelRole.ANALYSIS_DESIGNER: AutomationRole.ANALYST,
    # Generators, like the explorers: what they produce is attacked later.
    ModelRole.FOLLOW_UP_EXPLORER: AutomationRole.ANALYST,
    # Work over retrieved sources is what LITERATURE means here.
    ModelRole.LITERATURE_EXPLORER: AutomationRole.LITERATURE,
    ModelRole.LITERATURE_READER: AutomationRole.LITERATURE,
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
        "_cooldown_seconds",
        "_failure_threshold",
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
        failure_threshold: int = 3,
        cooldown_seconds: int = 300,
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
        #: The breaker settings, from ``runtime.yaml``.
        #:
        #: Passed in rather than left to `record_provider_result`'s defaults,
        #: which is what the first version did -- so
        #: ``provider_failure_threshold`` and ``provider_cooldown_seconds``
        #: were settings a researcher could write, `runtime doctor` would
        #: print, and nothing would read. Both defaults happen to equal the
        #: store's, which is why nobody noticed: changing either in
        #: configuration changed nothing at all.
        self._failure_threshold = int(failure_threshold)
        self._cooldown_seconds = int(cooldown_seconds)

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
    def _eligible(self, request: ModelRequest) -> tuple[ProviderProfile, ...]:
        """Every profile that *could* serve this request, health aside.

        Split out from :meth:`_candidates` so that "no provider can do this"
        and "no provider can do this *right now*" are answerable separately.
        The second has a deadline attached and the first does not, and a retry
        policy that cannot tell them apart either waits forever for a provider
        that will never exist or gives up on one that is back in four minutes.
        """

        floor = MINIMUM_TIER[request.criticality]
        return tuple(
            sorted(
                (
                    profile
                    for profile in self._profiles
                    if profile.name in self._adapters
                    and request.capability in profile.capabilities
                    and profile.tier >= floor
                ),
                key=lambda profile: (-profile.tier, profile.estimated_cost_usd),
            )
        )

    def _candidates(self, request: ModelRequest) -> tuple[ProviderProfile, ...]:
        eligible = self._eligible(request)
        healthy = set(self._store.usable_providers(tuple(p.name for p in eligible)))
        return tuple(profile for profile in eligible if profile.name in healthy)

    def _recovers_at(self, request: ModelRequest) -> datetime | None:
        """When the first provider that could serve this request comes back.

        ``None`` when no cooldown explains the refusal -- which means waiting
        will not help, and the failure is about configuration or installation
        rather than about weather.
        """

        eligible = self._eligible(request)
        if not eligible:
            return None
        cooling = self._store.provider_cooldowns(tuple(p.name for p in eligible))
        return min(cooling.values()) if cooling else None

    def route(self, request: ModelRequest) -> Routed:
        """Pick a provider, and say honestly how independent it is.

        Ordering: the strongest tier first for critical work, cheapest-first
        within a tier for routine work. Then, if independence was asked for,
        prefer a candidate whose family has not already answered in this group.
        """

        candidates = self._candidates(request)
        if not candidates:
            floor = MINIMUM_TIER[request.criticality]
            # No invocation happens on this path, so ``attempted=False``: the
            # work item must not be charged an attempt for being turned away.
            # ``recovers_at`` is when the refusal is known to lift, and it is
            # None when nothing is merely cooling -- the difference between
            # "wait four minutes" and "install a provider".
            recovers_at = self._recovers_at(request)
            waiting = (
                f" The soonest any of them is usable again is "
                f"{recovers_at.isoformat()}."
                if recovers_at is not None
                else ""
            )
            if request.criticality is Criticality.CRITICAL:
                raise CriticalCapabilityUnavailableError(
                    f"{request.role} is critical work needing {request.capability} at "
                    f"tier >= {floor}, and no healthy provider offers it. Refusing "
                    f"rather than answering critical work with a weaker model."
                    + waiting,
                    retry_at=recovers_at,
                    attempted=False,
                )
            raise ProviderCallFailedError(
                f"no healthy provider offers {request.capability} at tier >= {floor} "
                f"for {request.role}." + waiting,
                retry_at=recovers_at,
                attempted=False,
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

    def _record_health(
        self, provider: str, *, ok: bool, error: str | None = None
    ) -> ProviderHealth:
        """Advance the breaker for one provider, under the configured policy."""

        return self._store.record_provider_result(
            provider,
            ok=ok,
            error=error,
            threshold=self._failure_threshold,
            cooldown=self._cooldown_seconds,
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
            # Inside the guard, and it was outside it until the first dogfood.
            #
            # This is the last thing between reserving the budget and asking a
            # provider, it is pure computation, and it can raise: it subscripts
            # `_ADAPTER_ROLE`, so a role with no entry there raised `KeyError`
            # here with both grant sets already HELD and nothing to release
            # them. Every one of those failures took a stage ceiling out of the
            # project's budget permanently -- observed as eight HELD rows and
            # $0.40 of a $15 ceiling gone on a run that spent $0.34.
            #
            # The docstring above records this exact bug being fixed once
            # before for the two reservations themselves. It is the same bug:
            # anything that can raise between the reservation and the
            # invocation belongs under the release, and a line added below the
            # guard is how it comes back.
            model, effort = self._model_and_effort(request, profile)
        except BaseException:
            self._budgets.release_all(cost_grants)
            self._budgets.release_all(grants)
            raise

        started = time.monotonic()
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
            health = self._record_health(profile.name, ok=False, error=str(exc))
            call_id = self._record(
                request,
                routed,
                status=ModelCallStatus.FAILED,
                prompt_ref=prompt_ref,
                output_ref=None,
                latency_ms=latency,
                error=f"{type(exc).__name__}: {exc}",
            )
            # Re-raised *as a provider failure* rather than bare. The bare
            # exception was an `OSError` or whatever else the adapter's
            # subprocess produced, and the daemon's classifier has no mapping
            # for those -- so it fell through to `UNKNOWN`, whose policy is
            # `FAIL_PERMANENTLY`. A provider process that could not be spawned
            # is about as transient as a failure gets, and it was the one
            # failure this runtime would never retry.
            raise ProviderCallFailedError(
                f"{profile.name} could not be invoked for {request.role}: "
                f"{type(exc).__name__}: {exc}",
                failure_class=FailureClass.PROVIDER_TRANSIENT,
                retry_at=health.cooldown_until,
            ) from exc

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
            detail = result.error or f"exit {result.exit_code}"
            health = self._record_health(profile.name, ok=False, error=detail)
            call_id = self._record(
                request,
                routed,
                status=status,
                prompt_ref=prompt_ref,
                output_ref=output_ref,
                latency_ms=latency,
                error=detail,
                resolved_model=result.resolved_model,
            )
            # Everything above is unchanged: the call is recorded, the budget
            # is settled, the breaker is advanced. What changed is the last
            # line. This used to return a `ModelResponse` carrying the error,
            # and see `ProviderCallFailedError` for the three research runs
            # that cost.
            #
            # The invocation really happened, so `attempted` stays true and
            # the attempt is charged -- but `retry_at` carries the breaker's
            # deadline, so the *next* attempt is scheduled for when the
            # provider can answer rather than thirty seconds from now.
            raise ProviderCallFailedError(
                f"{profile.name} did not answer {request.role}: {detail}",
                failure_class=(
                    FailureClass.PROVIDER_TIMEOUT
                    if result.timed_out
                    else FailureClass.PROVIDER_UNAVAILABLE
                ),
                retry_at=health.cooldown_until,
            )

        if request.json_schema is not None and result.structured is None:
            self._budgets.settle_all(cost_grants, actual=result.total_cost_usd)
            self._budgets.settle_all(grants)
            self._record_health(profile.name, ok=True)
            call_id = self._record(
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
                call_id=call_id,
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
        self._record_health(profile.name, ok=True)
        if request.independence_group:
            self._loaded_groups.add(request.independence_group)
            self._used_families.setdefault(request.independence_group, set()).add(
                profile.family
            )
        call_id = self._record(
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
            call_id=call_id,
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
    ) -> str:
        """Write the provenance row, and return the id of what was written.

        Returning it rather than discarding it is what lets a caller name the
        call afterwards. The discovery portfolio's review-independence check
        needs exactly that: "was this review produced by the same call that
        produced the work" is not answerable from a provider name.
        """

        recorded = self._store.record_model_call(
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
        return recorded.call_id


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
