# ADR 0004 — Portfolio scheduling is deterministic; a model may only break ties

**Status:** accepted, amended the same day — see *Amendment* below
**Date:** 2026-09-20
**Context:** the autonomous discovery portfolio layer

## Decision

The allocator is ordinary Python. It computes, per candidate action, a scalar
scheduling utility from quality dimensions, expected information value, cost,
staleness, blocking objections, diversity and lineage concentration; it applies
hard bounds (active ideas per project, active descendants per lineage, children
per branch step, depth without new evidence, spend ceilings, novelty floor,
duplicate threshold); and it emits an ordered, bounded list of work items.

A model may be consulted **only** to permute a band the deterministic policy has
already selected. The constraints are re-applied after the permutation, so a
tie-breaker that returns items outside the band changes nothing.

*Amended, below: there is no tie-breaker. The allocator consults no model at
all.*

The portfolio tick is a bounded deterministic pipeline, **not** a LangGraph.

## Why this and not the alternatives

**Not an LLM portfolio manager.** A free-form model deciding the whole portfolio
is a system whose resource allocation cannot be explained, reproduced, bounded
or tested, and whose behaviour changes when a vendor ships a new checkpoint. It
also makes every one of §23's anti-explosion bounds advisory.

**Not a single opaque score in the scientific record.** The utility exists and
is stored on `idea_actions` as an operational number. The scientific record
keeps novelty, impact, plausibility, falsifiability, tractability, evidence
strength, reproducibility, reviewer confidence and literature confidence
separately, and the human-facing top-ideas selection is a Pareto front with a
diversity constraint rather than `order by utility`.

**Not a LangGraph for the tick.** `ARCHITECTURE.md` §12a adopted LangGraph for
one measured property: durable checkpointing across interrupt and process death.
A portfolio tick makes no model call, has no interrupt, completes in
milliseconds, is idempotent, and rebuilds its whole input from the database next
tick. There is nothing to checkpoint. Creating checkpoint rows every tick per
project forever would be a cost buying no property, which is the shape of
adoption §12 exists to prevent. This is a deliberate, recorded deviation from
the mission brief's §26; the pipeline keeps §26's node names, is bounded, ends,
and is re-invoked by `researchd`, which is what §26 is for.

## Consequences

- Allocation is reproducible: the same portfolio state produces the same plan,
  which is what makes `tests/test_portfolio_tick.py` able to assert fairness,
  diversity and bound enforcement at all.
- A provider outage does not touch allocation at all.
- Tuning is editing weights in configuration, not rewriting a prompt.

## Reversibility

High. The tie-breaker is an injected optional callable; the weights are
configuration. Replacing the policy is replacing one pure function whose
signature is `(candidates, state, bounds) -> ordered candidates`.

---

## Amendment, 2026-09-20: the tie-breaker is removed, and two bounds are added

**There is no model tie-breaker.** An independent architecture review, reading
the decision above before it was implemented, pointed out that a model
permuting a band is still a model deciding what the machine spends money on,
and that the property `DESIGN_INVARIANTS.md` records of the daemon — *it calls
no model* — would become false in the one process where it is load bearing.
`research_os.portfolio.allocation` therefore has no `models` parameter, no
optional callable and no import that could obtain one, and
`tests/test_portfolio_authority.py` asserts that by parsing the module. The
title of this ADR is left as it was written; this paragraph is the correction.

**One explorer in flight at a time.** The decision above lists the hard bounds
and the list was incomplete. Every bound named there is per *idea* or per
*lineage*; exploration has neither, so nothing bounded it across ticks. The
leftover-capacity branch fires whenever a slot is free and the candidate pool
is below its ceiling — on a quiet portfolio, every tick — so each cadence
bought another explorer at full price regardless of whether the last one had
started. `PortfolioStore.explorations_in_flight` is the bound.

**And exploration that produces nothing stops.**
`PortfolioStore.barren_explorations` counts successful explorer runs since the
newest idea; past `max_barren_explorations` the portfolio pauses as
`PAUSED_NO_FRONTIER`. The project budget did stop the loop before this, which
is true and is the wrong diagnosis: "you have spent your ceiling" is not what
"every idea your explorers generate is a near-duplicate" should look like to
the researcher reading it.

Both were found by running the tick against a clone of a real project rather
than by a test, and `PAUSED_NO_FRONTIER` — which the enum, the CLI and this
document all described — turned out to have no reachable trigger until the
second of them supplied one.
