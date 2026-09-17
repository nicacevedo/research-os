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
   provider runs
settle(cost) -> HELD becomes SETTLED at the reported cost
```

A reservation the ledger refuses raises :class:`BudgetExceededError` *before*
the provider is invoked, which is what makes the cap hard rather than
retrospective. It is an ``AutomationError`` on purpose: both controllers
already fail a run on one, terminally, and
``coding.failure_class_for`` maps a reason containing "budget" onto
``BUDGET_EXHAUSTED``, which is not repaired. A budget that retries is not a
budget.

**The residual, stated plainly.** No provider quotes a price before it bills,
so a single call can cost more than the per-call ceiling reserved for it. The
overrun is then *recorded* -- ``settle`` charges the actual -- and the next
``authorize`` sees it, so the excess is bounded by one call rather than by the
whole run. The ceiling also ratchets: it is the configured value or the largest
cost this authority has actually observed, whichever is larger, so a run whose
first call costs 3 USD reserves 3 USD for its second. What cannot be promised
is that the *first* expensive call is refused, and nothing that bills after the
fact can promise it.

**Crashes.** A worker that dies between ``authorize`` and ``settle`` leaves a
HELD reservation, which ``available`` already excludes -- pessimism in the
crash window is the right direction of error for money -- and which
``BudgetLedger.reconcile_stale`` releases later. The accounting is conservative
and recoverable, never optimistic and lost.

This module deliberately knows nothing about proposals or coding. It knows
about a registry of adapters and a budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
)
from research_os.errors import BudgetExceededError
from research_os.runtime.budgets import (
    BudgetExhaustedError,
    BudgetLedger,
    Dimension,
    Grant,
)

LOG = logging.getLogger("research_os.runtime.spend")

#: What one delegated model call is assumed to cost until one is observed.
#:
#: A number, because a reservation needs one and no provider will quote. Chosen
#: above the observed mean of the pilots (0.13 USD/call over twelve calls) by
#: enough that ordinary calls are not refused, and low enough that a 6 USD run
#: budget authorises roughly a dozen rather than two. The ratchet below is what
#: handles the case where it is wrong.
DEFAULT_PER_CALL_CEILING_USD = Decimal("0.50")


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
    settled_usd: Decimal = field(default_factory=lambda: Decimal(0), init=False)
    refusals: int = field(default=0, init=False)
    largest_call_usd: Decimal = field(default_factory=lambda: Decimal(0), init=False)
    """The most expensive single call settled here, and the reservation floor.

    The ratchet. Without it a run whose calls cost 3 USD each would reserve
    0.50 every time and discover the overrun once per call.
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

        ceiling = max(self.per_call_ceiling_usd, self.largest_call_usd)
        calls: tuple[Grant, ...] = ()
        cost: tuple[Grant, ...] = ()
        try:
            calls = self.budgets.reserve_all(
                dimension=Dimension.MODEL_CALLS,
                amount=1,
                run_id=self.run_id,
                project_id=self.project_id,
                work_id=self.work_id,
            )
            cost = self.budgets.reserve_all(
                dimension=Dimension.MODEL_COST_USD,
                amount=ceiling,
                run_id=self.run_id,
                project_id=self.project_id,
                work_id=self.work_id,
            )
        except BudgetExhaustedError as exc:
            self.budgets.release_all(cost)
            self.budgets.release_all(calls)
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
            self.budgets.release_all(cost)
            self.budgets.release_all(calls)
            raise
        return Authorization(calls=calls, cost=cost, reserved_usd=ceiling)

    # ------------------------------------------------------------- settling --
    def settle(self, grant: Authorization, *, cost_usd: float | None) -> None:
        """Record what the call actually cost, and give back what it did not use."""

        self.budgets.settle_all(grant.calls)
        self.settled_calls += 1
        if cost_usd is None:
            # No number from the provider. Release rather than settle at the
            # estimate, matching the runtime router, because the *backstop*
            # reconciles from the run's own invocation records afterwards and
            # charging here as well would double-count. What must not happen is
            # recording a zero, and this records nothing.
            self.budgets.release_all(grant.cost)
            return
        spent = Decimal(str(cost_usd))
        self.budgets.settle_all(grant.cost, actual=spent)
        self.settled_usd += spent
        self.largest_call_usd = max(self.largest_call_usd, spent)
        if spent > grant.reserved_usd:
            LOG.warning(
                "a delegated %s call cost %s against a %s reservation; the next "
                "call reserves the larger amount",
                self.action,
                spent,
                grant.reserved_usd,
            )

    def abandon(self, grant: Authorization) -> None:
        """The provider raised. Count the call, do not invent a cost.

        Exactly what ``ModelRouter.complete`` does on the same event, and for
        the same reason: an adapter that raised produced no result and reported
        no cost, so the money is unknown. If the run record later shows one, the
        after-the-fact reconciliation charges it.
        """

        self.budgets.settle_all(grant.calls)
        self.settled_calls += 1
        self.budgets.release_all(grant.cost)

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

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        grant = self.authority.authorize()
        try:
            result = self.inner.invoke(request)
        except BaseException:
            self.authority.abandon(grant)
            raise
        self.authority.settle(grant, cost_usd=result.total_cost_usd)
        return result
