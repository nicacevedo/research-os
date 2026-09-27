"""One budget authority for the calls this runtime does not make itself.

The runtime's own router is reserve → execute → reconcile, and the ``where``
clause of the reservation *is* the check, so two workers cannot both be told
there is room for the last call. That shape was correct and it covered only the
calls the router makes.

It makes none of the calls inside a delegated action. ``propose_capsule_change``
hands the work to :class:`~research_os.proposal.controller.ProposalController`
and the coding action to
:class:`~research_os.automation.controller.AutomationController`; both own
their own providers, bound themselves with their own per-action *call* ceiling,
and never touched this ledger. A real pilot showed what that costs: ``runtime
run`` reported one model call for a cycle that made four, and a run started
with ``--max-cost-usd 6`` reported a tenth of what it had spent. Twelve calls,
2.3888 USD, of which 1.6478 was invisible -- a 3.2x under-report on an ordinary
cycle with no failures.

The previous release's answer was :meth:`BudgetLedger.charge_all`: record the
delegated spend *afterwards*, past the limit when it must, and report which
budgets it broke. That makes the ledger true and it is not a budget. A cap that
bites on the call after the overrun is a cap that authorised the overrun.

**What this module does instead.** It wraps the provider adapters. Every
delegated controller takes a ``dict[str, ProviderAdapter]`` and calls
``adapter.invoke`` exactly once per model call, so the adapter is the one
chokepoint both of them already pass through -- and wrapping it needs no change
to either controller, and covers any third one written later that takes a
registry. A wrapped adapter reserves before the call and settles after it:

```text
authorize()  -> 1 MODEL_CALLS, and a per-call MODEL_COST_USD ceiling, HELD
submitted()  -> marked handed-over, with the cap the provider is about to get
   provider runs, told to stop at that same ceiling
settle(cost) -> HELD becomes SETTLED at the reported cost
```

A reservation the ledger refuses raises :class:`BudgetExceededError` *before*
the provider is invoked. It is an ``AutomationError`` on purpose: both
controllers already fail a run on one, terminally, and
``coding.failure_class_for`` maps a reason containing "budget" onto
``BUDGET_EXHAUSTED``, which is not repaired. A budget that retries is not a
budget.

**The provider is capped at the reservation** (INV-01). The request the
controller built is handed on with ``max_budget_usd`` set to the reserved
ceiling (or to the controller's own cap, if that is smaller), so the provider
itself stops where the ledger's authority ends. An adapter that cannot be
capped -- one that does not declare ``hard_budget_cap`` -- is refused with
``PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE`` before anything is reserved.

What this replaced, and why it had to go: the first version reserved a 0.50
USD estimate, passed no cap, and let a 1.20 USD call settle past a 1.00 USD
run, project and system ceiling -- "recorded, and seen by the next
reservation", which is a ledger that is true about an overrun it authorised.
It also *ratcheted* the next reservation up to the largest cost observed, a
correction for a provider nothing bounded. With a cap there is nothing to
ratchet toward, so the ceiling is fixed: a delegated call that needs more than
it is stopped by the provider and the action fails ``BUDGET_EXHAUSTED``,
visibly.

**Crashes and unknown outcomes.** A worker that dies between ``authorize`` and
``settle`` leaves a HELD reservation, which ``available`` already excludes --
pessimism in the crash window is the right direction of error for money. The
reservation is taken *pending* and marked submitted immediately before the
provider is invoked, so ``BudgetLedger.reconcile_stale`` can tell a call that
never started (released) from one that did (charged in full). The same rule
applies while the worker is alive: a call that reports no cost -- a timeout, a
kill, an adapter that raised -- is charged its whole reservation, because
"no number" is not "zero" (``docs/ARCHITECTURE_INVARIANTS.md``, INV-01). The
accounting is conservative and visible, never optimistic and lost.

This module deliberately knows nothing about proposals or coding. It knows
about a registry of adapters and a budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from research_os.automation.providers import (
    PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE,
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    enforces_hard_budget_cap,
)
from research_os.errors import BudgetExceededError
from research_os.runtime.budgets import (
    DEFAULT_CALL_CEILING_USD,
    BudgetExhaustedError,
    BudgetLedger,
    Dimension,
    Grant,
    SettlementBasis,
    provider_cap,
)

LOG = logging.getLogger("research_os.runtime.spend")

#: What one delegated model call is authorised to spend: reserved, and handed
#: to the provider as its hard cap. The runtime-wide per-call ceiling
#: (``budgets.DEFAULT_CALL_CEILING_USD``), under the name this module has
#: always exported.
DEFAULT_PER_CALL_CEILING_USD = DEFAULT_CALL_CEILING_USD


class ProviderBudgetCapUnavailableError(BudgetExceededError):
    """A delegated call through an adapter that cannot be held to a hard cap.

    ``PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE`` (INV-01), raised before anything
    is reserved. A :class:`BudgetExceededError` so both delegated controllers
    fail the run on it terminally, as they already do for a refused budget --
    retrying cannot give an adapter a cap it does not have.
    """

    code = PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class Authorization:
    """Capacity taken for one call that has not happened yet."""

    calls: tuple[Grant, ...]
    cost: tuple[Grant, ...]
    reserved_usd: Decimal


@dataclass
class DelegatedSpendAuthority:
    """Reserves runtime budget for each call a delegated controller makes.

    One per delegated action, so ``settled_calls`` and ``settled_usd`` describe
    exactly the work that action delegated and the after-the-fact backstop can
    subtract them without guessing.
    """

    budgets: BudgetLedger
    run_id: str
    project_id: str
    action: str
    work_id: str | None = None
    per_call_ceiling_usd: Decimal = DEFAULT_PER_CALL_CEILING_USD

    settled_calls: int = field(default=0, init=False)
    #: What providers *reported*, and settled here. Only reported cost: the
    #: after-the-fact backstop subtracts this from the reported costs in the
    #: run's own invocation records, and a conservative charge for a call
    #: whose cost is unknown has no counterpart there.
    settled_usd: Decimal = field(default_factory=lambda: Decimal(0), init=False)
    #: What was charged in full because a call's outcome was unknown.
    unknown_usd: Decimal = field(default_factory=lambda: Decimal(0), init=False)
    refusals: int = field(default=0, init=False)
    largest_call_usd: Decimal = field(default_factory=lambda: Decimal(0), init=False)
    """The most expensive single call settled here. Observed, never authority.

    It used to be the reservation floor -- a ratchet towards what an uncapped
    provider had actually billed. Every call is capped at the reservation now,
    so a value above ``per_call_ceiling_usd`` means a provider billed past its
    cap, which the ledger records as ``reported_over_reservation``.
    """

    # ------------------------------------------------------------ reserving --
    def authorize(self) -> Authorization:
        """Take capacity for one call, or refuse the call.

        Both dimensions or neither, and the rollback is the reason: taking the
        call reservation and then failing the cost reservation would leak a
        HELD row with no handle on it, and a cost-exhausted run would silently
        exhaust its call budget too and then misreport which one ran out. The
        runtime's own router learned that the hard way and this follows it.
        """

        ceiling = Decimal(str(self.per_call_ceiling_usd))
        calls: tuple[Grant, ...] = ()
        cost: tuple[Grant, ...] = ()
        try:
            calls = self.budgets.reserve_all(
                dimension=Dimension.MODEL_CALLS,
                amount=1,
                run_id=self.run_id,
                project_id=self.project_id,
                work_id=self.work_id,
                pending=True,
            )
            cost = self.budgets.reserve_all(
                dimension=Dimension.MODEL_COST_USD,
                amount=ceiling,
                run_id=self.run_id,
                project_id=self.project_id,
                work_id=self.work_id,
                pending=True,
            )
        except BudgetExhaustedError as exc:
            self.budgets.release_all(cost, basis=SettlementBasis.REFUSED)
            self.budgets.release_all(calls, basis=SettlementBasis.REFUSED)
            self.refusals += 1
            # Re-raised as the automation layer's own budget error, because
            # both delegated controllers already treat that as terminal and
            # neither has ever heard of `BudgetExhaustedError`. Uncaught, it
            # would escape `_execute`'s `except AutomationError` and leave the
            # run non-terminal with a worktree nobody cleans up -- the same
            # shape of defect the `SandboxError` clause in that handler exists
            # to close.
            raise BudgetExceededError(
                f"the runtime budget refused a delegated {self.action} model "
                f"call before it was made: {exc}"
            ) from exc
        except BaseException:
            self.budgets.release_all(cost, basis=SettlementBasis.NOT_INVOKED)
            self.budgets.release_all(calls, basis=SettlementBasis.NOT_INVOKED)
            raise
        return Authorization(calls=calls, cost=cost, reserved_usd=ceiling)

    def submitted(self, grant: Authorization, *, cap: float) -> None:
        """Record that the call is being handed to the provider now, and its cap."""

        self.budgets.mark_submitted(
            grant.calls + grant.cost, provider_cap=Decimal(str(cap))
        )

    def not_invoked(self, grant: Authorization) -> None:
        """The provider provably never started: nothing was spent.

        The call is still *counted* -- the controller records the attempt, and
        the after-the-fact backstop charges any recorded call this authority
        did not -- but its cost reservation is released, on the one piece of
        evidence that allows it.
        """

        self.budgets.settle_all(grant.calls)
        self.settled_calls += 1
        self.budgets.release_all(grant.cost, basis=SettlementBasis.NOT_INVOKED)

    # ------------------------------------------------------------- settling --
    def settle(self, grant: Authorization, *, cost_usd: float | None) -> None:
        """Record what the call actually cost, and give back what it did not use."""

        self.budgets.settle_all(grant.calls)
        self.settled_calls += 1
        if cost_usd is None:
            # No number from a provider that ran. This used to *release* the
            # reservation, on the reasoning that the backstop would charge
            # from the run's records -- but the records carry the same
            # missing number, so a call killed at its timeout was charged by
            # neither. Charged in full instead, and counted apart from
            # `settled_usd` so the backstop's subtraction stays exact.
            self.budgets.settle_unknown(grant.cost)
            self.unknown_usd += grant.reserved_usd
            return
        spent = Decimal(str(cost_usd))
        self.budgets.settle_all(grant.cost, actual=spent)
        self.settled_usd += spent
        self.largest_call_usd = max(self.largest_call_usd, spent)
        if spent > grant.reserved_usd:
            LOG.warning(
                "a delegated %s call reported %s against a %s reservation that "
                "was also its provider cap: the provider billed past its cap",
                self.action,
                spent,
                grant.reserved_usd,
            )

    def abandon(self, grant: Authorization) -> None:
        """The provider raised after it was handed the call.

        Exactly what ``ModelRouter.complete`` does on the same event, and for
        the same reason: an adapter that raised produced no result and reported
        no cost, so the money is unknown -- and unknown is charged in full,
        never handed back (INV-01).
        """

        self.budgets.settle_all(grant.calls)
        self.settled_calls += 1
        self.budgets.settle_unknown(grant.cost)
        self.unknown_usd += grant.reserved_usd

    # ---------------------------------------------------------- the wrapper --
    def wrap(self, registry: dict[str, ProviderAdapter]) -> dict[str, ProviderAdapter]:
        """Return the same registry with every adapter budget-aware."""

        return {
            name: BudgetedProvider(inner=adapter, authority=self)
            for name, adapter in registry.items()
        }


@dataclass
class BudgetedProvider:
    """A provider adapter that must be paid for before it is used.

    Implements :class:`~research_os.automation.providers.ProviderAdapter` by
    delegation. ``probe`` is free -- it asks a binary whether it is installed --
    so it is passed straight through; charging for it would make
    ``runtime doctor`` spend a run's budget.
    """

    inner: ProviderAdapter
    authority: DelegatedSpendAuthority

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def family(self) -> str:
        return self.inner.family

    def probe(self) -> Any:
        return self.inner.probe()

    @property
    def hard_budget_cap(self) -> bool:
        return enforces_hard_budget_cap(self.inner)

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if not enforces_hard_budget_cap(self.inner):
            # Before anything is reserved: an estimate reserved in front of a
            # provider nothing can stop is not authority (INV-01).
            self.authority.refusals += 1
            raise ProviderBudgetCapUnavailableError(
                f"{PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE}: the {self.name} adapter "
                f"cannot hand its provider a hard spend cap, so the runtime "
                f"refused a delegated {self.authority.action} model call before "
                f"it was made"
            )
        grant = self.authority.authorize()
        # The provider is told to stop where the reservation ends -- or where
        # the controller's own cap does, if that is lower. Never above it.
        cap = provider_cap(grant.reserved_usd)
        if request.max_budget_usd is not None:
            cap = min(cap, float(request.max_budget_usd))
        try:
            capped = replace(request, max_budget_usd=cap)
            self.authority.submitted(grant, cap=cap)
        except BaseException:
            self.authority.not_invoked(grant)
            raise
        try:
            result = self.inner.invoke(capped)
        except NotImplementedError:
            # An adapter with no implementation raises before anything runs.
            self.authority.not_invoked(grant)
            raise
        except BaseException:
            self.authority.abandon(grant)
            raise
        if result.total_cost_usd is None and not result.invoked:
            self.authority.not_invoked(grant)
            return result
        self.authority.settle(grant, cost_usd=result.total_cost_usd)
        return result
