# Autonomous discovery: the portfolio layer

This is the live specification of the layer that sits **above** the R5
autonomous runtime and turns it from a machine that advances one objective into
a machine that runs a research *portfolio*.

It is not a redesign of the kernel. `DESIGN_INVARIANTS.md` is unchanged,
`docs/CAPSULE.md` remains authoritative on anything scientific, and no
technology left the postponed list in `ARCHITECTURE.md` §12. What this layer
adds is one object (the Idea), the machinery that grows and kills ideas, and
the durable bank that survives the researcher's absence.

Read `docs/RUNTIME.md` first. This document assumes the queue, the leases, the
idempotency ledger, the budget ledger, the provider router, the artifact store
and the failure taxonomy, and describes only what is new.

## Implementation status

This document states what the system does, and every enforcement claim names
the test that holds it. As of this revision the following exist and pass:

```text
sql/0019 .. 0030                     the schema, head 0030
research_os/portfolio/
    ids, digests, models, config     identity, the three digests, the bounds
    empirical                        the bridge to the experiment machinery
    store                            every read and write
    gates                            the deterministic quality gates
    dedup                            four layers, three of them arithmetic
    contracts, prompts               fourteen role contracts and templates
    packets                          the frozen review packet
    stages                           the stage machine: pure, total
    runner                           the stage handlers
    track                            the IdeaTrackGraph
    allocation, tick                 the deterministic portfolio pass
    curator                          the Git bank
    digest                           the periodic rendering
    commands, extensions             the CLI and the control-plane registration
research_os/runtime/
    refs, workkinds, extensions      three small additions the above needed
research_os/service.py               researchd's composition root

tests/
    test_portfolio_store.py          the seven §3.4 invariants
    test_portfolio_gates.py          the gates, with a positive control
    test_portfolio_dedup.py          determinism five ways
    test_portfolio_stages.py         the machine's domain, and termination
    test_portfolio_contracts.py      one refusal per way prose could get in
    test_portfolio_track.py          a whole idea, stage by stage
    test_portfolio_tick.py           capacity, bounds, pauses, determinism
    test_portfolio_curator.py        the bank, and the escape-check regression
    test_portfolio_digest.py         the Pareto front, and what it must say
    test_portfolio_chaos.py          §34's eight failures and six prohibitions
    test_portfolio_cli.py            the commands a researcher types
    test_portfolio_daemon.py         schedule -> event -> work -> handler
    test_portfolio_authority.py      what this layer structurally cannot do
    test_portfolio_promotion.py      the whole ladder, written by production
    test_portfolio_empirical.py      a measurement, and what it is not
```

**What is implemented and what is proven are different claims**, and this
document uses them precisely. Everything above is implemented and tested
against scripted providers on a real PostgreSQL. Much of it has since been
run against a real provider on two real projects, and
`docs/AUTONOMOUS_DISCOVERY_REPORT.md` §V–§X record what that found; its
evidence table distinguishes *implemented*, *unit-tested*,
*integration-tested*, *dogfood-proven* and *unattended-proven* per claim.

For §19 specifically, the state is: design, preregistration, contained
execution in a disposable worktree, the deterministic analysis, the
evidence row, the review board, the meta-review and replication are proven
end to end through `advance_idea` with a real subprocess and a scripted
provider. Against a *real* provider on a real project, design,
preregistration, contained execution and the operational-failure path are
proven, and a real idea has not yet reached a real conclusion -- for the two
reasons §X.8 names, both of which are declarations the researcher owns.

One capability is deliberately absent and says so at the point of use: the
evidence stage has no wiring to the derivation path, so a *mathematical* idea
stops below `VALIDATED` on this build with a message naming what is missing.
That is the correct behaviour rather than a gap papered over -- an idea that
cannot be settled here must not be validated on prose -- and §18 records it as
a limit.

The *empirical* route is no longer among them. §19 specifies it, and the
reason it was built before the derivation path is measured rather than
preferred: every adjudicated idea both real projects have produced is
`empirical`, so on this corpus the empirical route is not one of four, it is
the one that decides whether this layer produces validated science at all.

---

## 1. Goals and non-goals

### The property this layer exists to provide

```text
A researcher gives Research OS a charter and occasional seeds.
Research OS explores continuously, kills cheaply, deepens selectively,
verifies independently, preserves everything, and surfaces only its
strongest work.
An idea waiting for the researcher does not stop the portfolio.
```

Stated as a difference from R5: R5 advances **one objective** through bounded
cycles and stops when the objective needs a person. That is correct for one
objective and fatal for a portfolio -- the researcher's absence becomes the
system's idleness. This layer makes the unit of continuation the *portfolio*
rather than the objective, so one idea's need for human attention removes that
idea from the active set and nothing else.

### Goals

1. **Diverse generation.** Three explorer contracts with deliberately different
   inputs, one of which is deliberately blind to the existing bank.
2. **Cheap killing.** Deduplication, a cheap novelty screen and an adversarial
   falsifier run *before* anything expensive, and each may end a track.
3. **Selective deepening.** A deterministic allocator spends the next unit of
   work where it buys the most, subject to diversity and lineage constraints.
4. **Verification by separate reviewers.** Three reviewer contracts that cannot
   see each other's verdicts, a meta-reviewer that synthesises them, and
   deterministic gates the meta-reviewer cannot talk its way past. How
   *independent* those reviewers actually are is measured and reported rather
   than assumed -- see §10, and note that on a one-provider host the honest
   answer is "not very".
5. **Complete preservation.** Nothing curated is deleted. A rejected idea
   remains addressable and revivable, and says why it was rejected.
6. **Durable human-readable bank.** A deterministic Curator materialises the
   bank to a dedicated autonomous Git branch, never to the researcher's
   canonical branch.

### Non-goals

- **This layer accepts no science.** An Idea is a candidate direction. It never
  becomes a capsule object except by the two human acts that already exist:
  `researchctl propose promote` and `researchctl review`. `HUMAN_READY` means
  *Research OS believes this merits your attention*, and nothing more.
- **No new orchestration technology.** No Redis, RabbitMQ, Celery, Temporal,
  Kubernetes, vector database or UI. The existing queue, leases, ledger,
  budgets, router, schedules and daemon carry all of it. §14 lists the five
  things that changed in the kernel and why each was forced.
- **No scalar ranking of science.** A scheduling utility exists and is
  explicitly an operational number. The scientific record keeps the dimensions.
- **No agent reviews its own work.** Enforced in code, not in a prompt.

---

## 2. Where this sits

```text
SCIENTIFIC TRUTH      Git-tracked capsule files            (unchanged)
                             ^
                             | human act only: propose promote / review
                             |
AUTONOMOUS BANK       research-os/autonomous Git branch    (NEW, this layer)
                             ^
                             | deterministic Curator, sole serialized writer
                             |
IDEA STATE            PostgreSQL: ideas, versions, edges,  (NEW, this layer)
                      evidence, reviews, objections, actions
                             ^
                             | portfolio allocation, idea tracks
                             |
OPERATIONAL TRUTH     PostgreSQL: queue, leases, ledger,   (R5, unchanged)
                      budgets, model calls, artifacts
WORKFLOW STATE        LangGraph checkpoints                (R5, unchanged)
ARTIFACT BYTES        content-addressed store              (R5, unchanged)
DERIVED INDEX         SQLite literature index              (R1, unchanged)
```

Two directions are asserted by tests rather than by this paragraph:

- nothing under `research_os/portfolio` writes a capsule file, authors a
  Review, accepts a Claim, promotes a proposal or an insight, merges, or
  pushes -- `tests/test_portfolio_authority.py`, which parses the package the
  way `tests/test_runtime_authority.py` parses the runtime;
- nothing under `research_os/runtime` imports `research_os.portfolio`, so
  deleting this layer leaves a runtime that still migrates and still runs an
  objective cycle --
  `tests/test_runtime_layering.py::test_the_runtime_does_not_import_the_portfolio`.

---

## 3. The object model

### 3.1 Idea, and why it is a new type

An Idea is **a possible scientific direction**. It is distinct from every
capsule type because each of those already means something this does not:

| type | what it asserts |
|---|---|
| `Question` | the project has decided this is worth answering |
| `Hypothesis` | the project holds this as a candidate answer |
| `Evidence` | the project's interpretation of something observed |
| `Claim` | the project's position, gated on human review |
| `Review` | a reviewer's recorded verdict on a Claim |
| `Proposal` | a requested *change* to the capsule, awaiting promotion |

An Idea asserts none of these. It says only: *this direction might be worth
something, and here is what we have found out about it so far.* Modelling it as
a `Hypothesis` would make an explorer's first guess into a candidate answer the
project holds. Modelling it as a `Proposal` would make every candidate a
request for the researcher's attention, which is the opposite of the point.

An Idea that survives everything becomes a `Proposal` -- through
`propose_capsule_change`, the existing grounded path -- and a person promotes
it. That is the only route from this layer into science.

### 3.1a The name collides with a capsule type, and the collision is handled

`docs/CAPSULE.md` already defines a capsule object called **Idea**, stored at
`.research/ideas/IDEA-NNNN.yaml`, with statuses `draft / active / promoted /
discarded / superseded`. It is scientific state the researcher owns.

This layer's Idea is a different thing with the same English word, and
`research_os/runtime/ids.py` already states the rule that makes the difference
matter: *a runtime id and a scientific id must never be mistakable for one
another, because the entire authority model rests on them being different kinds
of thing.*

| | capsule Idea | portfolio Idea |
|---|---|---|
| id | `IDEA-0001` | `PIDEA-20260920T181500Z-3fa17b0c` |
| lives in | `.research/ideas/` on the canonical branch | PostgreSQL, materialised to `research-os/autonomous` |
| Python | `research_os.models.Idea` | `research_os.portfolio.models.PortfolioIdea` |
| authority | scientific state; a person wrote or promoted it | candidate direction; no scientific status at all |

The Python class is deliberately **not** named `Idea`, so no import can shadow
the capsule type and no reviewer reading a diff has to work out which one a bare
`Idea` means. `researchctl ideas` prints `PIDEA-` ids and says in its header
which kind of object it is listing.

The two meet at exactly one point, and it is the point this layer is for: a
`HUMAN_READY` portfolio Idea becomes a `Proposal` requesting that the capsule
gain a `Question`, an `Idea` or a `Hypothesis`. A person promotes it.

### 3.2 Identity and versioning

```text
ideas          stable identity + current pointers      (mutable pointers only)
idea_versions  immutable scientific content            (append-only)
```

An `idea_versions` row is never updated. A revision appends a version. Each
version carries two digests, both version-tagged so a changed digest
distinguishes changed science from a changed algorithm -- the requirement
`research_os/digests.py` states for capsule objects:

- **`content_digest`** (`pidea-content-v1:<sha256>`) — over the scientifically
  material fields only, listed in `portfolio/digests.py::MATERIAL_FIELDS`,
  plus the project id. The project is a key of the projection for the same
  reason it is one in the kernel: an idea copied into another project must not
  inherit the first project's reviewed identity.
- **`canonical_digest`** (`pidea-canonical-v1:<sha256>`) — over an aggressively
  *normalised* projection of `research_question` + `core_idea`, used for
  deduplication (§7). Two different phrasings of one idea collide here on
  purpose.

Deliberately **not** in `content_digest`, each with the reason recorded in
`digests.py::IMMATERIAL_FIELDS`: `next_best_action`, `dimensions`,
`addressed_objections`, provenance and timestamps. Status, quality tier,
operational state and spend are not on the version at all.

`tests/test_portfolio_store.py` asserts both directions: a change to any of
eight material fields moves the digest, and a change to the next action or the
assessed dimensions does not.

**Evidence links are not on the version and are not in `content_digest`.** They
are their own rows, and a review binds them through a *second* digest --
`reviewed_evidence_digest` (§3.4). An earlier draft of this document left them
out of both, which reopened the hole `docs/CAPSULE.md` closes for a Claim with
`evidence_digests`: the evidence under a standing approval could be replaced
wholesale and the approval still read as current.

### 3.3 The table set

Nine tables. The brief asked for seven concepts and warned against
proliferation; one concept (`idea_actions`) absorbed two that would otherwise
have been separate, and one (`idea_objections`) was added deliberately.

```text
ideas              identity, project, origin, depth, lineage root, status,
                   operational state, quality tier, retirement memory,
                   curation watermark
idea_versions      immutable content; content_digest; canonical_digest;
                   origin model call; the version's producing role
idea_edges         lineage (DERIVED_FROM, GENERALIZES, SPECIALIZES,
                   MERGED_FROM, REVIVES) and relations (CONTRADICTS,
                   DUPLICATE_OF)
idea_evidence      what a version rests on, with polarity
idea_reviews       version-bound reviews with the independence achieved
idea_objections    standing objections that survive the revision claiming
                   to answer them
idea_actions       every stage attempt: the active-track invariant AND the
                   scientific-basis idempotency key
portfolio_state    per-project portfolio status, bounds, counters
portfolio_seeds    researcher seeds not yet turned into ideas
portfolio_digests  the periodic autonomous digest
```

**Why `idea_objections` is its own table** rather than a JSON column on a
review. Three gates count unresolved objections, so the count has to be a
query. More importantly an objection has to *outlive* the review that raised
it: a revision stales every review of the prior version, so objections stored
inside reviews would be cleared by the same act that clears the approvals --
and "reword the mechanism until the objection goes away" would be a working
strategy against a stochastic reviewer. A standing objection row, keyed by the
normalised objection text, is what makes a kill stick.

**Why `idea_evidence` exists beside `runtime_findings`**, which also records
artifacts, capsule objects, literature keys and an experiment job. Two things
findings do not have: a *polarity* (`SUPPORTS` / `CONTRADICTS` /
`CONSISTENT_WITH` / `INCONCLUSIVE`) and a kind whose combination with polarity
is constrained. A finding is "here is something observed". An evidence row is
"and this is what it does to this idea". The connection is kept: an evidence
row carries the `finding_id` it came from, which is what lets the eventual
proposal cite a finding the grounding allowlist already checks.

### 3.4 What each invariant is enforced by

The brief lists seven invariants. Where each lives, and the test that proves
it:

| invariant | mechanism | test |
|---|---|---|
| review points to an exact idea version | composite FK `(idea_id, idea_version)` to `idea_versions`, plus `reviewed_content_digest` copied at write time | `test_a_material_revision_stales_every_review` |
| material revision makes prior review stale | `reviewed_content_digest <> current` — computed in SQL by `live_reviews`, never stored as a flag | same |
| **and changed evidence does too** | `reviewed_evidence_digest <> evidence_set_digest(current)` | `test_swapping_the_evidence_under_a_review_stales_it` |
| **and a superseded prompt does too** | `prompt_version` on the review, compared against this build's | `test_a_review_by_a_superseded_prompt_is_not_live` |
| idea lineage is acyclic | see below | `test_the_database_refuses_a_lineage_cycle` |
| only one active track per idea | partial unique index `on idea_actions (idea_id) where status = 'ACTIVE'` | `test_an_idea_has_at_most_one_track_in_flight` |
| same scientific-basis action is idempotent | partial unique index on `(idea_id, idea_version, stage, basis_digest) where status in ('ACTIVE','SUCCEEDED')` | `test_the_same_basis_runs_once_but_a_failure_does_not_poison_it` |
| same review request is idempotent | unique index on `(idea_id, idea_version, reviewer_role, reviewed_content_digest, reviewed_evidence_digest)` | `test_recording_the_same_review_twice_is_one_review` |
| same portfolio event cannot create duplicate tracks | the work item's `dedup_key` is the action id, and the action's two indexes above are the real prevention | §16 |

#### Acyclicity, and why a `check` alone would not do it

A PostgreSQL `CHECK` can only see columns of its own row. So
`check (child_depth > parent_depth)` on `idea_edges` is satisfied by a writer
that simply invents the two depths, and "`ideas.depth` has no UPDATE path" is
a Python convention, not a constraint. An earlier draft of this document
claimed the check alone was enough. It is not.

What makes it sound is three things together:

1. `ideas` carries `unique (idea_id, depth)`;
2. `idea_edges` has composite foreign keys `(parent_idea_id, parent_depth)` and
   `(child_idea_id, child_depth)` referencing it, so an invented depth is a
   referential-integrity error;
3. the same foreign keys' default `NO ACTION` refuses an `UPDATE` to
   `ideas.depth` that any edge depends on, which is column immutability
   enforced by the database rather than by convention.

Given those, a lineage edge strictly increases depth, and a cycle would require
`d₁ < d₂ < … < d₁`. `CONTRADICTS` and `DUPLICATE_OF` are relations rather than
lineage and are exempt, via a stored generated column `is_lineage` that every
traversal filters on. `DUPLICATE_OF` additionally has a partial unique index on
the child, so the survivor chain is a path with a fixed point.

Both recursive traversals carry a `CYCLE` clause anyway. It costs nothing, and
it removes the one way a corrupt row could hang a query — and, because the
daemon runs work inline, the control plane with it.

---

## 4. State machines

### 4.1 Scientific quality state

```text
                         ┌─────────────┐
   explorer output ────► │  CANDIDATE  │
                         └──────┬──────┘
             dedup / screen /   │   gate: precise, mechanism,
             falsifier FATAL    │   falsifier, screen passed,
                    │           │   no open blocking objection
                    ▼           ▼
              ┌──────────┐  ┌───────────┐
              │ REJECTED │  │ PROMISING │
              └────▲─────┘  └─────┬─────┘
                   │              │ allocated evidence work
                   │              ▼
                   │      ┌───────────────┐
                   │      │ INVESTIGATING │
                   │      └───────┬───────┘
                   │              │ evidence sufficient for review
                   │              ▼
                   │         ┌────────┐
                   ├─────────┤ REVIEW │
                   │         └───┬────┘
                   │             │ gate: deep audit + three live
                   │             │ non-negative reviews + no open
                   │             │ CRITICAL/FATAL objection
                   │             ▼
                   │       ┌───────────┐
                   │       │ VALIDATED │
                   │       └─────┬─────┘
                   │             │ gate: VALIDATED + replication +
                   │             │ MAJOR objections answered
                   │             ▼
                   │      ┌──────────────┐
                   │      │ HUMAN_READY  │
                   │      └──────────────┘
                   │
      any state ───┴──► PARKED      (reason + revisit_if required)
      any state ──────► SUPERSEDED  (reason required)
```

Transitions into `PROMISING`, `VALIDATED` and `HUMAN_READY` are **gated** (§9).
Transitions into `REJECTED`, `PARKED` and `SUPERSEDED` are not gated -- killing
an idea needs no ceremony, and that asymmetry is the whole design -- but they
do require a **reason**, and `PARKED` additionally requires `revisit_if`. That
is `docs/CAPSULE.md`'s retirement-memory rule applied to candidates, for its
reason: so the project can tell "we ruled this out" from "we forgot about it".
It is a check constraint, and `test_retiring_an_idea_requires_a_reason` proves
it.

`quality_tier` is a separate high-water mark, so an idea rejected after
reaching `PROMISING` is distinguishable from one rejected as a candidate. The
digest reports demotions from it.

`REJECTED` is not terminal in the sense of unreachable. A `REVIVES` edge
creates a *new* idea whose lineage names the rejected one, and
`carry_objections_forward` copies the rejected idea's standing objections onto
it -- so a revival inherits the obligation along with the lineage, and cannot
be used to launder a fatal objection into a clean slate. The rejected row is
never edited and never deleted.

### 4.2 Operational state, which is orthogonal

```text
ACTIVE              a track is in flight
IDLE                nothing in flight; eligible for allocation
BLOCKED_PROVIDER    the provider this idea needs is in cooldown
BLOCKED_BUDGET      an idea- or lineage-scope ceiling is exhausted
BLOCKED_EXTERNAL    waiting on an external job or dependency
BLOCKED_DEPENDENCY  waiting on another idea in its lineage
```

`quality = PROMISING, operational = BLOCKED_PROVIDER` is exactly
representable, which the brief requires and which the R5 provider-failure
closure (`docs/RUNTIME.md` §17) established as necessary: an outage must never
be able to read as a scientific verdict.

**Budget exhaustion parks the work, not the idea.** It sets
`operational_state = BLOCKED_BUDGET` and leaves `status` untouched. Moving a
`PROMISING` idea to `PARKED` because money ran out would record a scientific
decision nobody made -- and would need a `retire_reason`, which is the schema
noticing the same thing.

### 4.3 Portfolio state

```text
RUNNING
PAUSED_BY_RESEARCHER        `researchctl portfolio pause`
PAUSED_BUDGET_EXHAUSTED     the project ceiling is gone; raising it resumes
PAUSED_NO_FRONTIER          no viable idea and no generative capacity left
PAUSED_BLOCKED_EXTERNAL     every allocatable idea is blocked externally
```

That list is closed and it is the entire set of reasons the portfolio may stop.
**There is no `WAIT_HUMAN`.** The way it is removed is by never creating it:
capacity is `active_count`, which counts ideas whose `operational_state` is
`ACTIVE`, and a `HUMAN_READY` idea has no track, so it occupies no slot and
blocks nothing. `test_a_human_ready_idea_frees_its_capacity_slot` asserts it at
the level where it is actually decided.

`PAUSED_BUDGET_EXHAUSTED` has an exit and it is a person's: raising the ceiling
with `researchctl runtime budget --max-cost-usd`. The next tick re-checks and
resumes. Budgets do not roll over or reset on their own, which is what makes
them budgets.

---

## 5. Role contracts

Fourteen roles. Eleven do the work the brief
names; the twelfth is the duplicate adjudicator §7 layer 4 calls; the
thirteenth is the dedup screen's tie-break, which is the same model with a
different question. Each is a versioned `PromptTemplate` with a declared JSON
output schema, routed through the existing `ModelRouter`, recorded in
`model_calls` with its prompt version. Malformed output fails through the
existing runtime semantics (`FailureClass.MODEL_OUTPUT_INVALID`) and never
becomes idea content.

### Generators

| role | receives | must not receive | produces |
|---|---|---|---|
| `blind_explorer` | charter, problem statement, accepted claims, constraints | **the idea bank**, existing hypotheses, the project's preferred direction | candidate directions |
| `seeded_explorer` | researcher seeds, best current ideas, negative findings, selected evidence | — | sharpened / combined directions |
| `failure_mining_explorer` | rejected ideas *with their retirement reasons*, failed experiments, contradictions, standing objections | — | directions that became interesting *because* something failed |

The blind explorer's blindness is enforced by `PromptTemplate.render` refusing
fields the template does not declare -- the existing mechanism, reused. A
caller cannot leak the bank into it by mistake, because there is no parameter
to leak it through.

Where the inputs come from, named rather than assumed: the charter and the
problem statement from `research_os.proposal.context`, which already reads
`CHARTER.md`; "accepted claims" from `ScientificKernelAdapter.quotable_claims`,
which already computes exactly that set; constraints from the project profile.

### Sharpeners

| role | job | failure is informative |
|---|---|---|
| `scientific_discovery` | turn a candidate into a precise question, candidate claim, mechanism, assumptions, alternatives, **falsifier**, minimum decisive next action | yes — "this cannot be made precise" reduces or ends the track |
| `literature_scout` | retrieve and read sources; produce a structured novelty matrix | — |
| `falsifier` | **kill the idea cheaply**: counterexample, subsuming theorem, simpler explanation, hidden assumption, identification failure, infeasible compute, irrelevant distinction | its *job* is the negative result |

`scientific_discovery` produces the falsifier; it does **not** produce the
adjudication type. See §8.

`literature_scout` output passes through
`research_os.literature.analyst.parse_literature_report` -- the existing
fail-closed citation check, which invalidates a whole report that cites a work
the packet did not contain. Reusing it rather than writing a new parser is the
difference between "no fabricated citations" being a property and being a
sentence in a prompt. The packet comes from the literature store, never from
the model.

### Reviewers

| role | asks | is given |
|---|---|---|
| `methodology_reviewer` | correctness, assumptions, identification, fair comparison, leakage, does the evidence support the conclusion, reproducibility, **is the adjudication type right** | the frozen packet |
| `novelty_reviewer` | closest known work, actual contribution, terminological equivalence, priority, does the claimed difference matter | the frozen packet **plus the retrieved literature evidence** |
| `skeptic_reviewer` | strongest counterargument, simplest competing explanation, most fragile assumption, what would reverse this, likely failure regime | the frozen packet |
| `replicator` | independent reconstruction along the second line §20 fixes for this type | the frozen packet, minus the original interpretation |
| `meta_reviewer` | synthesise a workflow disposition, preserving disagreement | the completed reviews |

**No reviewer's packet has a field for another reviewer's verdict.**
`ReviewPacket` is a frozen dataclass and has no such field; adding one would be
a visible change to a type its tests assert the shape of.

The meta-reviewer is the one role that sees verdicts, and only after all of
them are recorded.

---

## 6. IdeaTrackGraph

### One stage per invocation

`docs/RUNTIME.md` §12a records the two LangGraph properties that were measured
rather than assumed, and both apply here: a process killed inside a node
re-runs it, and a side effect before `interrupt()` is replayed. The track graph
inherits both mitigations unchanged -- every side effect goes through the
invocation ledger.

What it adds is a bound. One invocation advances one idea through exactly one
**stage**, then ends:

```text
        hydrate_idea
             │
             ▼
      select_stage   ── deterministic; reads the idea's state, never a model
             │
 ┌───────┬───────┬───────┬────────┬─────────┬──────────┬────────┬─────────┐
 ▼       ▼       ▼       ▼        ▼         ▼          ▼        ▼         ▼
dedup  screen falsify discover adjudicate evidence literature review   replicate
                                                      audit    board      /branch
 └───────┴───────┴───────┴────────┴─────────┴──────────┴────────┴─────────┘
                            │
                            ▼
                      meta_review          (only after review_board)
                            ▼
                       disposition          deterministic gate evaluation
                            ▼
                           END
```

`select_stage` is a pure function of `(status, current version, which stages
have succeeded against this version, which evidence exists, which reviews are
live, which objections stand)`. It is ordinary Python, it is total, and its
domain is enumerated by a test.

`review_board` is three nodes, not one -- `review_methodology`,
`review_novelty`, `review_skeptic` in sequence -- and that is what makes
LangGraph worth using here rather than a plain pipeline. A crash after the
second reviewer resumes at the third instead of paying for all three again.
Checkpointing at node granularity is the property; with one node per stage
elsewhere it buys nothing there, and that is stated rather than claimed away.

**Why not the whole pipeline in one invocation.** Two measured reasons:

1. *Recovery.* The kernel's proven unit of retry is the work item. A stage is a
   work item, so a provider outage during `falsify` retries `falsify` against
   the provider's own cooldown (`docs/RUNTIME.md` §17 I5), and a failed stage
   wastes at most one stage's spend rather than nine.
2. *Fairness.* One project must not starve another. The queue already
   interleaves work items; it cannot interleave inside one.

An earlier draft gave a third reason -- that a budget ceiling is only
enforceable at an invocation boundary -- and it was wrong. `ModelRouter.complete`
reserves before *every* call (`docs/RUNTIME.md` §8a), so a ceiling binds on
call two of nine regardless of invocation granularity.

### Dispositions

```text
REJECT       status ← REJECTED, with a reason; the track ends; lineage kept
PARK         status ← PARKED, with a reason and a revisit condition
REVISE       a new idea_version is appended; prior reviews go stale;
             standing objections do not
DEEPEN       schedules one further bounded stage; no status change
BRANCH       creates child ideas with lineage; the parent continues
DUPLICATE    a DUPLICATE_OF edge; status ← SUPERSEDED
PROMISING    gated
VALIDATED    gated
HUMAN_READY  gated
```

Every disposition **ends the graph invocation**. `DEEPEN` and `BRANCH` create
*rows*, not continued execution. The portfolio decides when they run.

`REVISE` is bounded: `max_revisions_per_idea` (§14) caps how many times one
idea may be rewritten, because "revise until a stochastic reviewer stops
objecting" is a loop and invariant 14 says every loop has a finite stop
condition.

---

## 7. Deduplication

Four layers,
cheapest first, and the durable identity is never a model's output.

```text
1. exact content identity     content_digest equality
                              → the candidate IS that version; no new idea
2. canonical hash             canonical_digest equality
                              → DUPLICATE_OF edge to the existing idea
3. lineage-aware              canonical_digest matches an ancestor or
                              descendant in the same lineage family
                              → DUPLICATE_OF edge, and the track ends
4. semantic adjudication      only when 1-3 miss AND a deterministic local
                              similarity screen exceeds the threshold
                              → a model is asked; its verdict becomes an edge
                                with provenance, never an identity
```

The local screen is character-trigram Jaccard over the normalised projection
(`digests.trigram_similarity`). Pure Python, deterministic, no dependency, no
embeddings, no vector store -- which is invariant 1 and keeps
`ARCHITECTURE.md` §12's postponed list intact.

**A semantic duplicate becomes a reference, not a disappearance.** The
duplicate row is written, marked `SUPERSEDED` with a reason, and given a
`DUPLICATE_OF` edge to the survivor. At most one such edge per idea, so the
chain has a fixed point; mutual duplication is still expressible and
`duplicate_survivor` carries a visited set for it.

`canonical_digest` normalisation is: NFKC, casefold, drop everything outside
`[a-z0-9]`, drop a fixed stopword list, sort the token multiset. Deliberately
lossy in exactly the ways that make rephrasings collide, and deterministic
across restarts, DB reconnects, event replay and model nondeterminism.

---

## 8. Adjudication routing

**The adjudication type is not a model's choice.**
`research_os.runtime.adjudication.classify` computes it from the idea's
*falsifier* -- weighted above the statement, because the falsifier is the
sentence that says what would settle the thing -- and its own docstring is
explicit that reading it is *reading, not inference*. This layer reuses
`AdjudicationKind` verbatim, `UNDETERMINED` included, and the vocabulary is a
check constraint on `idea_versions.adjudication_types`.

That matters more than tidiness. Every quality gate in §9 is parameterised by
the adjudication type, so a generator that could declare its own type could
choose its own evidentiary bar -- classifying an empirical claim as
`novelty_or_literature` routes it past preregistration, past contained
execution, and past any reproducible artifact.

```text
mathematical           derive → proof criticism → counterexample search →
                       numerical witness
                       A numerical witness is recorded as CONSISTENT_WITH, and
                       the database refuses to record it as SUPPORTS.

empirical              internal preregistration → contained implementation →
                       contained execution → deterministic analysis →
                       interpretation
                       All five are existing R5 actions. The preregistration is
                       stored before the run and `interpret_results` compares
                       against it -- the existing durable binding
                       (docs/RUNTIME.md §14b), unchanged.

novelty_or_literature  source retrieval → source reading → claim comparison →
                       novelty adjudication
                       Model memory establishes nothing, and the way that is
                       true is that a literature evidence row without a
                       retrieved source key or an artifact is refused by a
                       check constraint. It is not storable, so it cannot
                       ground anything.

diagnostic             observing this implementation. A fact about the
                       program, never on its own a reason to close a target --
                       which is what `AdjudicationKind.DIAGNOSTIC` already
                       means.

mixed                  split explicitly into component sub-ideas with
                       SPECIALIZES edges, each classified on its own falsifier

undetermined           routes nothing and blocks nothing; the stage that would
                       have acted on it instead asks scientific_discovery for
                       a sharper falsifier
```

**An experiment may not establish novelty.** The `VALIDATED` gate refuses a
novelty conclusion whose supporting evidence contains no `literature` rows --
a check on the evidence table, not an instruction in a prompt.

---

## 9. Quality gates

Deterministic minimum requirements, evaluated
by `research_os.portfolio.gates.evaluate`, which takes the idea, its current
version, its **live** reviews, its **standing objections** and its evidence,
and returns `GateResult(tier, passed, unmet, notes)`.

"Live" is defined once, in `PortfolioStore.live_reviews`, and means all four
of: the review binds the current version's content digest; it binds that
version's current evidence set; its prompt version is the one this build would
use; and it is younger than `review_max_age_seconds` when one is configured. A
review of a superseded version never counts, for any tier, ever.

All four are **on by default**, through a sentinel rather than a `bool`, so a
caller that wants a looser reading has to ask for it in writing at the call
site. An earlier build made two of the four opt-in, which meant the sentence
above was true of the gate and false of the six other callers -- defined once,
but read six ways.

### The severity scale, defined once

```text
MINOR      worth recording; blocks nothing
MAJOR      must be answered before HUMAN_READY
CRITICAL   must be answered before VALIDATED
FATAL      the idea as stated does not survive this; normally REJECT
```

An objection is **answered** only by `resolve_objection`, which requires: a
later version that named the objection in `addressed_objections`; a review of
*that* version; and that review being by a **different role** than the one that
raised it. The producing side cannot mark its own objection answered, and a
revision alone does not clear one.

### The tiers

```text
PROMISING     a research question, a mechanism and a falsifier, each non-empty
              the cheap novelty screen completed
              no standing objection at FATAL

VALIDATED     PROMISING, and:
              three live reviews — methodology, novelty, skeptic —
                each with a non-negative verdict (PASS or PASS_WITH_OBJECTIONS)
              the union of evidence requirements over every declared
                adjudication type (below)
              a deep literature audit: a novelty-matrix artifact plus at least
                `novelty_min_sources` distinct retrieved literature keys
              no standing objection at CRITICAL or FATAL

HUMAN_READY   VALIDATED, and:
              every standing objection at MAJOR or above is answered
              the replication required for the type (below) is present
              limitations are explicit and a significance is stated
              no standing objection at any blocking severity
```

### Evidence and replication, per type, with no discretionary words

The union over every declared type applies, so declaring two types adds
requirements and never removes them.

| type | `VALIDATED` needs | `HUMAN_READY` replication needs |
|---|---|---|
| `mathematical` | a `derivation` evidence row, **and** at least one machine-checkable artifact: an executed counterexample search with an `external_jobs` row and a spec digest | an independent derivation by a `replicator` that did not read the first, **or** a second executed search under a different parameterisation |
| `empirical` | an `experiment` evidence row naming an `external_jobs` row, and its interpretation bound to a stored preregistration | a second execution with its own `ExecutionSpec` digest, differing in seed or implementation |
| `novelty_or_literature` | `novelty_min_sources` distinct retrieved literature keys and a novelty matrix | a second terminology path: a retrieval whose query set is disjoint from the first |
| `diagnostic` | an `inspection` or `code` evidence row naming an artifact | an independent checker or an alternate implementation |
| `mixed` | the union over its components | the union over its components |
| `undetermined` | cannot reach `VALIDATED`; the gate says so and names the missing falsifier | — |

**A `CONSISTENT_WITH` evidence row never satisfies "substantive evidence" on
its own, and never satisfies replication.** Otherwise a universally quantified
claim reaches the top tier on two finite computations. The polarity is a column
and the gate reads it.

### The meta-reviewer cannot talk past this

`meta_review` returns a *recommendation*. `disposition` calls `gates.evaluate`
and takes the **minimum** of the recommendation and what the gate permits. A
meta-reviewer that recommends `HUMAN_READY` for an idea with zero reviews
produces a `PROMISING`-at-best outcome and an event recording the divergence.
The test for this uses a stub meta-reviewer that always recommends the top
tier.

---

## 10. Review independence, measured and reported

### What is recorded

For every review, and for the version it reviews:

```text
origin invocation      idea_versions.origin_call_id
review invocation      idea_reviews.call_id
model, provider        from model_calls, both rows
provider family        research_os.automation.providers.provider_family
prompt version         idea_reviews.prompt_version
context class          idea_reviews.context_class
```

`idea_reviews.independence_vs_origin` reuses
`research_os.runtime.interfaces.Independence` verbatim -- `none`,
`different_context`, `different_model`, `different_family` -- rather than
inventing a second vocabulary for the same idea. It is a *different question*
from `model_calls.independence`, which is the router's per-group value, and
that is why the column has a different name. It is derived from `model_calls`
at write time, never asked for twice.

There is no `SAME_CALL` value. A review whose model call *is* the origin call
is not a weak review, it is a defect, and `classify_independence` raises rather
than returning something storable.

### Three reviews are three roles, not three independent reviewers

This is the honest limit and it is stated here rather than implied away. Each
review's independence is measured against the *version's origin*, not against
the other reviewers. On a one-provider host all three are the same family, and
three samples from one model reading one packet are correlated in precisely the
dimension that matters: susceptibility to confident prose.

So the gate also computes `board_independence` — the number of distinct
`(provider_family, model)` pairs across the three live reviews — and it appears
in the gate result, in the digest, in `researchctl ideas show` and in the
Curator's output. **When it is 1, no rendered output uses the word
"independent".**

`review_independence: require` in `runtime.yaml` is a *router* setting, and it
acts before any review exists: a `CRITICAL` request that cannot get a fresh
family raises `IndependenceUnavailableError` and the stage fails closed into
`WAITING_FOR_EXTERNAL_DEPENDENCY`. So under `require` on a one-family host,
`review_board` produces nothing and no idea reaches `VALIDATED` — which is the
correct behaviour for a deployment that has decided a same-family review is
worse than none. Under `prefer`, reviews happen and the shortfall is recorded.
The gate never fabricates the difference.

---

## 11. The Portfolio Manager and the portfolio tick

### Allocation is deterministic, with no model in it at all

```text
inputs      quality dimensions, expected information value, cost, novelty
            uncertainty, standing objections, staleness, diversity, lineage
            concentration, available budgets

output      an ordered, bounded list of work items to enqueue
```

An earlier draft allowed a model to break ties within a band the deterministic
policy had already selected. That is removed. `researchd` runs the tick, and
`DESIGN_INVARIANTS.md`'s R5 record says of the daemon: *it calls no model*.
A tie-breaker would have made that false, in the one process where it is load
bearing, and the existing guard could not have caught it -- the AST test that
checks for a model call parses `daemon.py` only. That test is extended to the
portfolio package for the same reason.

**One explorer is in flight at a time.** The allocator buys at most one per
tick, which bounds a tick and bounds nothing across them: the leftover-capacity
branch fires whenever a slot is free and the pool is under its *ceiling*, which
on a quiet portfolio is every tick, so it bought an explorer every cadence
whether or not the previous one had started. `PortfolioStore.explorations_in_flight`
is the bound, and it costs nothing in the ordinary case, because an explorer
that finishes inside one cadence never blocks the next.

**And exploration that produces nothing stops.** `barren_explorations` counts
successful explorer runs since the newest idea; past `max_barren_explorations`
the portfolio pauses as `PAUSED_NO_FRONTIER` and says the idea space looks
exhausted to the explorers available here. A new seed clears it, because a
seed is exactly the information the count says is missing. Before this the
budget ceiling was the only thing that stopped the loop, which is true, and
"you have spent your ceiling" is the wrong diagnosis for it.

A scheduling utility `U` is computed per candidate action. It is an
**operational number**: stored on `idea_actions`, never in the scientific
record, and the digest ranks by Pareto layer and diversity rather than by `U`.

The allocator's shape, as a policy with configurable weights:

```text
fresh exploration          keep the candidate pool above a floor
deepening                  advance the highest-utility PROMISING/INVESTIGATING
falsification/replication  the cheapest action that could kill or confirm
novelty closure            ideas whose novelty is the only open question
maintenance/revival        stale tracks, blocked tracks whose block has cleared
```

### Diversity

Tracked across `lineage_root` and `adjudication_types`, which are
deterministic, and across a *normalised* `subproblem` label -- run through the
same `digests.normalise` the canonical digest uses, so "spectral
regularisation" and "spectral penalty" are not two families. Free-text model
labels are not used unnormalised anywhere a bound depends on them.

Each candidate's utility carries a configurable penalty proportional to how
much of the active set already shares its coordinates, and a hard cap bounds
active descendants per lineage root. The digest's top-ideas selection is a
Pareto front with a diversity constraint rather than `order by U`.

The front's dimensions are `novelty`, `evidence_strength` and
`literature_confidence`, and `evidence_strength` is **computed from the
evidence and review rows**, never taken from a model's own score. An earlier
build ranked the front on five dimensions, three of them self-assessed, which
let an idea reach the researcher's attention by rating itself highly. Model
self-assessment is still stored and still shown; it no longer orders
anything.

### The tick reaches the daemon through the existing schedule machinery

```text
schedules row (per project, portfolio_tick_seconds)
   → PORTFOLIO_TICK_DUE event
   → EVENT_WORK entry → work item
   → WORK_HANDLERS entry → the tick
```

Three lines in `daemon.py` and one schedule row, rather than a new pass in the
daemon's deliberate ordering. `sql/0001_runtime.sql` says why the table exists:
*a schedule produces events; it never performs work directly, so a scheduled
action passes through exactly the same provenance, budget, locking and failure
handling as one a person asked for.* That is the whole argument.

### The tick itself

```text
hydrate_portfolio      read state, bounds, budgets, capsule observation
inspect_capacity       count ACTIVE tracks; compute free slots
reconcile              stale actions (stale_actions), cleared blocks
replenish              if the candidate pool is below floor, enqueue explorers
allocate               deterministic utility + diversity + bounds
launch                 enqueue idea-track work items, deduped on the action id
collect                fold completed dispositions into idea state
update_bank            enqueue curation when the uncurated count is non-zero
schedule_digest        if due
END
```

**This is a deterministic pipeline, not a LangGraph**, and that is a recorded
deviation from the brief's §26. The reason is `ARCHITECTURE.md` §12a's own
test: LangGraph earns its place through durable checkpointing across interrupts
and process death. This tick makes no model call, has no interrupt, completes
in milliseconds, is idempotent, and reconstructs its whole input from the
database next tick. There is nothing to checkpoint, and writing checkpoint rows
every tick per project forever would be a cost buying no property. The node
names above are §26's, the pipeline is bounded and ends, and `researchd`
invokes the next one.

---

## 12. The autonomous bank and the Curator

### Two views, one underlying set

```text
complete archive   every idea ever explored, including REJECTED and SUPERSEDED
reviewed bank      status in (VALIDATED, HUMAN_READY)
```

Nothing curated is deleted from either. Ideas generated since the last curation
are the stated exposure -- see §18 -- and `uncurated_count` reports it as a
number.

### The Curator

```text
runtime DB / artifacts
   → deterministic snapshot
   → Curator worktree (outside the researcher's ordinary tree)
   → research-os/autonomous branch
```

| property | mechanism |
|---|---|
| sole serialized writer | `repository_lock(db, repo_path)` — the *same* `LockClass.REPOSITORY_MUTATION` the coding pipeline takes for `git worktree add`, so the two serialise against each other rather than each holding a lock the other ignores |
| does not fail concurrent coding runs | `refs/heads/research-os/autonomous` is in `runtime/refs.py::RESERVED_REF_PREFIXES` and is excluded from `canonical_fingerprint`. Without it, curating during a coding run makes that run report an escape it did not commit — the identical failure `coding.py`'s own docstring records having had once before, with a different writer |
| the blind spot that creates is covered | the Curator records the commit it last wrote and **refuses to commit onto a tip it does not recognise**, naming the unexpected sha. The fingerprint guards refs nothing in this system writes; the one namespace this system writes guards itself |
| deterministic ordering | every collection sorted by id before rendering; no dict iteration order reaches a file |
| idempotent snapshot | the rendered tree's digest is computed first; a snapshot equal to the recorded one commits nothing |
| restart recovery | the worktree is reset to the branch tip before rendering, so a crash mid-write leaves no partial state |
| the human branch is untouched | the Curator worktree is checked out on the reserved branch and the Curator has no code path that names another ref |
| performs no scientific reasoning | the module imports no prompt, no router and no model provider; asserted by the authority test that parses the package |

### What it writes

```text
.research-os/autonomous/
  IDEAS.md                     the index, ordered by id
  ideas/<PIDEA-ID>.md          one record per idea: every version, lineage,
                               evidence, reviews, standing objections, the
                               disposition and the retirement reason
  bank/VALIDATED.md            the reviewed bank
  bank/HUMAN_READY.md
  digests/<PDIG-ID>.md         each periodic digest
  SNAPSHOT.json                the deterministic snapshot digest and counts
```

Under `.research-os/`, **not** `.research/`. A capsule directory on an
autonomous branch would be one the kernel validator tries to read and a person
could merge without noticing. The Curator refuses any path under `.research/`.

### Every page carries the same header, and it is not optional

`VALIDATED` is a scientific word, and the only other thing in this system that
is validated is a capsule Claim. A researcher reading
`bank/VALIDATED.md` in their own repository is not reading this specification.
So every bank page and every idea record begins with:

```text
> Produced autonomously by Research OS. No human has evaluated this.
> executions performed: N    sources retrieved: N    distinct reviewer models: N
> board independence: N      standing objections: N (K blocking)
```

The numbers are computed, not written. A test asserts the header is present on
every rendered page and that its counts match the database.

---

## 13. Authority

Unchanged in shape from `docs/RUNTIME.md` §7 and `research_os.runtime.policy`.
What changed is that this layer's verbs are now *in* that table rather than
beside it: fifteen `ActionKind` members, each with a level, a permission set
and a `dispatch` of `PORTFOLIO`.

One authority table for the whole system, because a second one would
eventually disagree with the first about what needs a person.
`runtime doctor`'s "actions with a policy but no handler" now filters on
`dispatch`, so a portfolio verb is not reported as a gap in the cycle graph.

### A0 — autonomous

`generate_ideas`, `deduplicate_ideas`, `screen_novelty`, `falsify_idea`,
`sharpen_idea`, `audit_novelty`, `review_idea`, `meta_review_idea`,
`replicate_idea`, `branch_idea`, `assign_quality_tier`, `retire_idea`,
`revive_idea`, `produce_portfolio_digest`.

All A0 for one uniform reason: a portfolio idea is a candidate direction, it is
not scientific state, and no action here can make it into any.

### A1 — autonomous within explicit ceilings

`curate_idea_bank` (`READ_REPO`, `WRITE_WORKTREE`). Plus the existing A1
actions an evidence stage delegates to: `run_local_experiment`,
`submit_cluster_experiment`, `edit_in_worktree`, `propose_capsule_change`.

### A2 — human

Unchanged, and this layer adds nothing to it. Material charter change; merge
into the canonical branch; accepting a Claim; external publication; external
communication; raising a ceiling.

**`HUMAN_READY` is A0.** Deciding that something merits attention is not
deciding that it is true.

---

## 14. What changed in the kernel, and what did not

**No dependency was added** to `pyproject.toml`. **No technology left the
postponed list.** No `BudgetScope` was added -- see §15.

Five changes, each forced by something measured:

| change | forced by |
|---|---|
| `external_jobs.contained`, `.containment`, `.wall_clock_seconds` (`sql/0030`) | an independent review found the analysis artifact recording `job.detail` under the key `containment` -- the literal string "completed" for a local run -- so the permanent record could not tell a contained measurement from an uncontained one, which is the condition `DESIGN_INVARIANTS.md` relies on for "the repository is byte-identical". `NULL` means unrecorded and is not `False` |
| `research_runs.run_kind` (`sql/0023`) | an idea-track stage needs a run row for budgets, provenance and `researchctl runtime run <id>`; without a discriminator, `parked_objectives` would open successor *cycles* for finished tracks and `stranded_runs` would enqueue `resume_cycle` for a thread that is not a cycle |
| `runtime/refs.py` + the `canonical_fingerprint` exemption | curating during a coding run made that run report an escape it did not commit |
| `policy.Dispatch` + fifteen `ActionKind` members | one authority table; `runtime doctor` must not report fifteen gaps that are not gaps |
| `RuntimeStore.create_run(run_kind=, thread_id=)` | the above, plumbed |
| `tests/test_runtime_layering.py`, `tests/test_runtime_schema.py` extended | the new package and the second half of the schema must be under the same checks as the first |

`LockClass` gained nothing: the Curator takes `REPOSITORY_MUTATION`, because a
different class would mean nothing serialises it against the coding pipeline.

---

## 15. Budget policy

```text
portfolio   → BudgetScope.PROJECT     (a portfolio is per project)
project     → BudgetScope.PROJECT
run         → BudgetScope.RUN         (one idea-track stage is one run)
idea        → the allocator, before the stage is enqueued
lineage     → the allocator, before the stage is enqueued
stage       → the request's max_cost_usd, from stage config
role        → the router's existing per-role model selection
```

**Idea and lineage ceilings are enforced in the allocator, not as budget
scopes,** and that is a deliberate downgrade from an earlier draft. Adding
`BudgetScope.IDEA` would not have been "a one-line enum addition": three
enforcement paths in `budgets.py` hardcode the scope triple, and `ModelRouter`
carries no idea id to reserve against. `BudgetScope.WORK_ITEM` already exists,
is in the check constraint, and is used by nothing -- which is what an enum
value without enforcement looks like.

So the allocator sums `idea_actions.cost_usd` per idea and per lineage root and
refuses to enqueue a stage that would exceed a ceiling. The overshoot that
allows is bounded by one stage's own `max_cost_usd`, which the router does
enforce per call. That bound is stated rather than hidden, and it is the honest
trade for not reshaping the budget ledger.

**Expensive stages require stronger gates.** `stage_policy` maps each stage to
the minimum tier that may enter it: `evidence` requires `PROMISING`,
`review_board` requires evidence to exist, `replicate` requires `VALIDATED`. So
money is spent in the order the governing principle states: explore broadly
(cheap), kill cheaply (cheap), deepen selectively (expensive), replicate
consequentially (most expensive).

**Exhaustion parks truthfully.** `BudgetExhaustedError` sets
`operational_state = BLOCKED_BUDGET` and leaves `status` alone.

---

## 16. Failure and recovery semantics

Every guarantee `docs/RUNTIME.md` §17 established is inherited, because the
failure path is the same path. What this layer adds is the set of *portfolio*
properties that must survive.

| failure | what must not happen | mechanism |
|---|---|---|
| provider unavailable | an idea is rejected, or reads as reviewed | `ProviderCallFailedError` leaves the graph as an exception; the stage retries against the provider's cooldown; `BLOCKED_PROVIDER`; no disposition written |
| daemon killed mid-stage | duplicate side effects; a lost idea | the invocation ledger; the lease is reclaimed and the stage re-runs; the basis index makes a repeat of a *succeeded* stage a no-op, and does not poison a *failed* one |
| a stage fails permanently | the idea sits ACTIVE forever, silently unallocatable | `stale_actions` — the portfolio's `stranded_runs`, reconciled in the tick |
| DB reconnect / duplicate event | two tracks for one idea | the active-track partial unique index |
| malformed model response | prose becomes idea content | schema validation before any write; `MODEL_OUTPUT_INVALID` |
| a reviewer fails | promotion on two reviews | the gate counts *live* reviews; two is not three |
| Curator crash | a partial or wrong bank commit | reset-to-tip before render; digest equality before commit; the repository lock released by the server on session death |
| Curator meets an unexpected tip | committing on top of something it did not write | refuses, and names the sha |
| experiment crash | a refutation retried until it stops refuting | unchanged: `FailureClass` has no member for "the science came out negative", by construction |
| budget exhausted | a scientific rejection | `BLOCKED_BUDGET`, status untouched |
| every explorer returns a near-duplicate | an explorer bought every cadence until the budget is gone, reported as `PAUSED_BUDGET_EXHAUSTED` | one explorer in flight at a time, and `PAUSED_NO_FRONTIER` after `max_barren_explorations` fruitless runs |
| one idea waits for a human | the portfolio stops | capacity counts ACTIVE tracks only; there is no portfolio `WAIT_HUMAN` to reach |

---

## 17. Human interaction

```text
researchctl portfolio enable <project>
researchctl seed add <project> --text ...
researchctl portfolio status [project]
researchctl portfolio top [project]
researchctl portfolio pause | resume <project>
researchctl portfolio digest [show <id> | list]
researchctl ideas list | show | lineage | rejected | validated | human-ready
```

`portfolio enable` is the only command that starts anything, and it is
separate from `seed add` on purpose: recording a direction costs nothing and
putting a project on the tick commits the machine to spending against the
researcher's budgets. It prints what that commitment is before it is made.
Enabling is *not* the undo for `pause`; a paused portfolio says so and names
`resume`, because the obvious command to type after finding a quiet portfolio
should not silently restart the thing the researcher stopped.

`resume` also returns every **blocked** idea of that project to `IDLE`, and
says how many. The tick lifts `BLOCKED_PROVIDER` by itself against provider
health it can observe and deliberately guesses at nothing else -- a raised
ceiling and an arrived capability are not facts it can read. A researcher
typing `resume` is that fact, and it is the only one in the system. Without
it a portfolio whose blocker had been fixed stayed stopped with no command
that started it, which is exactly what happened to the three ideas the
empirical route unblocked.

**Forgiving the ceiling is a watermark, not a reset**, and the distinction
is load-bearing. `Bounds.max_stage_failures` counts failed work items and
never decays, so unblocking alone was undone by the next tick. But the
count also feeds `work_items.dedup_key` against a permanently unique index:
reset it and the retry re-uses a spent key, which `enqueue` refuses
silently -- the wedge the count exists to close.
`portfolio_state.failures_forgiven_at` (`0029`) moves instead, and
`stage_failures` returns three numbers for three readers: all-time for the
key, recent for the ceiling, and recent *refusals* for the rule below.

**A refusal counts once.** The ceiling is right for a transient and wrong
for an answer: "no declared command can test this idea" does not become
true on the third attempt, and each attempt is a paid frontier call. So one
failure whose class is `CAPABILITY_DENIED`, `POLICY_REFUSED` or
`MISSING_SCIENTIFIC_AUTHORITY` -- the classes the failure taxonomy already
calls terminal -- blocks the idea, and `resume` is what reconsiders it.
This was unimplementable until stage failures stopped reaching the queue as
`UNKNOWN`: `_as_error` raised a bare `ResearchOSError` for anything that
was not a provider failure, so twenty-seven were recorded with no class.

`researchctl portfolio digest` rather than `researchctl digest show`: the
kernel already owns `researchctl digest <OBJECT-ID>`, which prints a capsule
object's semantic digest, and overloading it would make an object id named
`show` ambiguous. The deviation from the brief's spelling is recorded here
rather than resolved by cleverness.

The digest's prose is **deterministically rendered from stored fields**. No
model writes a word of it. That is the one artifact a returning researcher
reads instead of everything else, and an unlabelled model narrative there would
do the most damage per word.

---

## 18. What this layer cannot do

Stated positively, because an unfalsifiable claim of safety is worse than an
acknowledged limit.

- It cannot write a capsule file, record a Review, promote a proposal or an
  insight, merge, push, or touch the canonical branch.
- It cannot claim independent review on a single-provider host. It records
  `board_independence`, and when that is 1 nothing it renders says
  "independent".
- It cannot establish novelty from model memory: a literature evidence row
  without a retrieved source key or an artifact is refused by the database.
- It cannot record a numerical witness as proof, for the same reason.
- It cannot let a generator choose its own evidentiary bar *downward*. The
  adjudication type is computed by ordinary Python, from the falsifier **and
  the research question** -- the union of two readings, not one field. An
  independent review was right that the earlier wording overstated this and
  `classify_adjudication`'s own docstring is the honest version: the
  falsifier is written by a model, the classifier is keyword scoring, and
  the discovery prompt tells the model outright that the kind of work which
  would settle the idea is read from that sentence. What the union buys is
  the direction: steering can *add* a requirement and cannot remove one.
- It cannot clear an objection by rewording. A revision stales the approvals
  too, and a standing objection is answered only by a different role reviewing
  a version that named it.
- **It cannot survive the loss of the operational database completely.** Ideas
  curated to the autonomous branch survive; ideas generated or changed since
  the last curation do not. `researchctl portfolio status` reports the
  uncurated count so the exposure is a number rather than an assumption. §1's
  "nothing curated is deleted" is stated with that qualifier on purpose.
- **It cannot test a question the project has not declared a command for.**
  The empirical route chooses from `experiments.yaml` and fills in declared
  parameters; a model cannot write a command, which is the boundary that
  makes the route safe and is also the boundary that limits its reach. On
  `cg-sparse-regression` two of three real empirical ideas were refused as
  untestable for exactly this reason, each with an account of what a
  command would have to take. It also cannot know what a command *writes*:
  nothing declares an output schema, so a decision rule's metric path is a
  guess unless the command's own description says otherwise.
- It cannot settle a *mathematical* idea. That route needs an executed
  counterexample search and the derivation path, and neither is wired into
  the idea track; such an idea stops below `VALIDATED` with a message saying
  so. The *empirical* route is wired and §19 specifies it.
- It cannot tell whether an idea is *true*. Every gate above is a check that
  the right kinds of evidence and the right number of separate readings exist.
  None of them reads the science. That is what the researcher is for, and it is
  why the top tier is called `HUMAN_READY` rather than `CORRECT`.

---

## 19. The empirical route

An idea that can only be settled by measuring something needs a measurement.
Until this section's subject existed the evidence stage refused every such
idea with "the experiment pipeline is not wired into the idea track", which
made `review_board`, `meta_review` and `replicate` unreachable for them.

**Why this route and not another.** All five ideas adjudicated across the two
real projects are `empirical`, and not by classifier error: their falsifiers
ask for grids to be run, bootstraps to be resampled and wall clock to be
recorded. The literature route is not merely the weakest of the four here; it
is the one real falsifiers almost never imply.

### 19.1 What was built, and what was reused

One module, `research_os/portfolio/empirical.py`, and one table. Everything
else is machinery that already existed:

```text
reused                                          what it gives
------------------------------------------------------------------------
experiments.yaml + experiment/spec.py           the declared commands, and the
                                                parameter contract that makes a
                                                model unable to write one
runtime/executors.py (LocalExecutor)            running it, contained
sandbox.py + automation/checks.py helpers       the containment, and what a
                                                contained `uv run` needs
automation/worktree.py                          the disposable workspace
runtime/idempotency.py                          running it exactly once
runtime/budgets.py                              reserve, settle, release
runtime/artifacts.py                            immutable, content-addressed
                                                outputs and analyses
runtime/store.external_jobs                     the execution record
actions/coding.canonical_fingerprint            "the checkout did not change"
runtime/failures.py                             the taxonomy, unchanged
```

Nothing was added to the failure taxonomy, no technology left the postponed
list, and no second experiment framework exists.

### 19.2 The object

`idea_experiments` (migration `0026`) is the one thing none of the reused
machinery can express: *which idea version asked for this measurement, and
how far the asking has got.*

```text
experiment_id     PEXP-<stamp>-<hex>
idea_id, idea_version    the exact version. A revision does not inherit it.
role              PRIMARY | REPLICATION
state             PROPOSED | EXECUTABLE | RUNNING | COMPLETED
                  | OPERATIONALLY_FAILED | INTERPRETED | SUPERSEDED
command           the name the researcher declared
spec_digest       the frozen ExecutionSpec, hashed
variation_digest  the same, with the workspace path removed
decision_rule     the frozen, machine-checkable rule -- or
no_rule_reason    why this question does not admit one
job_id            the execution, in external_jobs
prompt_version    role@version of the prompt that designed it (0027)
analysis_artifact_id, conclusion, evidence_id
```

Three properties are schema rather than convention, because each is a rule
the layer above would otherwise be trusted to keep: one *live* experiment per
`(idea version, role)` -- a partial unique index, so a superseded design
stays as a record without occupying the name; a terminal state carries what
made it terminal; and a preregistration is a rule or a written reason there
is none.

### 19.3 The states are not one state

```text
experiment proposed          designed and preregistered, nothing run
experiment executable        a workspace exists for it
experiment running           submitted; the executor has not returned
experiment operationally     the measurement did not happen, and the row
  failed                     says with which failure class
experiment completed         the executor returned; nothing concluded yet
evidence interpreted         the frozen rule was applied and a conclusion
                             recorded
```

Only the last says anything scientific. A system with one "failed" state
cannot tell a refutation from a dead node, and a system that reached for one
would eventually report the node.

`RUNNING` is reachable in the machine and not on this build's path: the
portfolio submits to `LocalExecutor` only, which returns finished. Cluster
submission from an idea track would also mean holding a work slot across a
queue wait, which is a scheduling change this release does not make.

### 19.4 The rule is fixed before the number exists

The experiment designer is asked, *before* anything runs, for one number in
one file the run will write and two thresholds on it:

```text
decision_rule:
  output_path    a file this command actually writes -- a declared output, or
                 the value supplied for one of its `path` parameters
  metric_path    a dotted path into that file's JSON
  success        { comparator, threshold }   the idea's prediction held
  failure        { comparator, threshold }   it did not
```

**Two predicates and not one, and that is the design.** A single success
predicate makes every result that is not a success a refutation, which is
false: a measurement can miss both. Requiring the refutation condition
separately makes "neither" expressible, and makes a rule whose success
condition covers everything detectable -- both hold, and the answer is
`INCONCLUSIVE` rather than `SUPPORTS`.

Afterwards, ordinary Python reads the number and compares it. There is no
step at which a model reads an output and reports a verdict. Where a question
genuinely has no single machine-checkable number, the design says so in
`no_decision_rule_reason`; that is recorded, and it costs the idea the top of
the scale, because the strongest conclusion available without a rule is
`INSUFFICIENT`.

### 19.5 The conclusions, and the one that is not evidence

```text
SUPPORTS               the success predicate held and the failure one did not
CONTRADICTS            the reverse
INCONCLUSIVE           both, or neither
INSUFFICIENT           the run finished and did not answer: a missing output,
                       a metric that is not a number, no frozen rule
OPERATIONALLY_BLOCKED  the measurement did not happen
```

`EVIDENCE_STRENGTH_FOR_CONCLUSION` has no entry for the last one, so an
operational failure cannot become an evidence row by accident. An executor
that crashed, a provider that did not answer and a host that cannot contain
are three operational failures and none of them writes a row.

### 19.6 Where it runs, and what stays byte-identical

Each experiment gets a disposable Git worktree whose path is a pure function
of its `experiment_id` -- which matters because `cwd` is inside
`spec_digest`, so a path derived from the clock would give one experiment a
different digest on every retry and the preregistration comparison would
reject every legitimate resubmission.

The canonical checkout is fingerprinted before and after -- the capsule and
every Git ref -- and a change that is not this experiment's own worktree
branch appearing fails the stage as `POLICY_REFUSED` rather than being
retried. When the measurement has been read, the worktree **and its branch**
are removed: every output is already in the content-addressed store by
digest and the analysis document records the argv, the seeds, the base commit
and each output's hash, so the branch holds nothing that is not held better
elsewhere -- and an unattended portfolio would otherwise leave one ref per
experiment in the researcher's own repository, permanently.

### 19.7 Replication varies something, in code

For an empirical idea `replicate` is a *second designed experiment*, not a
model agreeing with the first. The replication designer is shown what the
first one ran and deliberately not what it concluded, must name what it
varied, and its resulting specification is compared on `variation_digest` --
argv, seeds, resources, expected outputs, with the workspace path removed.
An identical rerun is refused before it runs. A reproducibility check is a
useful thing and is not a replication, and this layer will not record one as
the other.

### 19.6a What the designer is shown about a command

Three things, and each was added because its absence was measured rather
than anticipated:

```text
parameters      type, required, bounds, and for a `path` the in-tree rule
inputs          the checkout's tracked data and configuration files
outputs         the numeric paths of a declared output the project has
                committed from an earlier run -- keys and types only
```

The third has a scientific edge on it. **The values are never shown.** A
threshold chosen to fit a result that already exists is a rule fixed after
the fact wearing the clothes of one fixed before, and the whole point of a
decision rule is that it precedes the number. A designer that wants to know
what value to expect has to reason about the science.

Only *declared* outputs and only *tracked* files: the researcher declares
what a command writes, and a leftover in somebody's working tree does not
get to describe it. A command whose output nobody has committed gets no
listing, which is the truth -- and the designer then refuses rather than
guessing a metric path, which is the behaviour observed and is correct.

### 19.7a A design has liveness, the way a review does

`idea_experiments.prompt_version` (migration `0027`) is `role@version` of the
prompt that produced the design, and an experiment that has not yet been read
is *stale* when that is no longer the current one -- exactly the rule
`PortfolioStore.live_reviews` applies through `CURRENT_REVIEW_PROMPTS`, for
exactly the reason it gives: a commitment produced by a prompt this build has
superseded is a commitment to a question no longer being asked.

A stale design is retired to `SUPERSEDED` and redesigned; the unique index is
partial so the successor can take the name, and the retired row stays as the
record of what was designed and why it stopped being asked for. An
`INTERPRETED` experiment is never stale: what was measured was measured, and
re-measuring it because a prompt's wording changed would be a second bite at
one question.

This is the fourth defect of one shape on this branch -- the version-blind
dedup key, the permanently-unique work item key, the released worktree's
surviving branch, and this. Each wedged something forever, and each was
invisible until the thing in front of it was fixed.

### 19.8 Termination

An interpreted measurement that does not meet the idea's evidence
requirement ends the track. Without that rule `select_stage` would choose
`EVIDENCE` again on every tick, find the same interpreted experiment,
conclude the same thing and change nothing -- at no cost, indefinitely, with
the idea reported as active. That is the eighth way a portfolio can loop and
the reason `TrackSnapshot.experiments` exists.

The stop is not a refutation. An `INCONCLUSIVE` or `INSUFFICIENT`
measurement leaves the question open, and the reason the researcher reads
says so.

### 19.9 Composed inputs: the measurement a plan may instantiate

§19.6a says the designer is shown a command's parameters, the checkout's
tracked inputs, and the numeric paths of a declared output. It did not say
what happens when the question the falsifier asks is *inside* a declared
command's reach and no tracked file expresses it. What happened is that the
portfolio stopped and a person wrote a file.

`ParameterType.GENERATED` is the parameter kind whose value is **content**
rather than a choice among what the repository already holds.

```text
type: generated        the researcher declares this, in experiments.yaml
input_schema:          the shape a composed document may have. Required.
max_bytes:             its ceiling, against canonical bytes. Finite.
```

The route:

```text
design          the model returns the document itself under the parameter's
                name -- a JSON object, never a path, never a string of JSON
freeze          checked against `input_schema`; canonicalised (sorted keys,
                compact separators, no NaN); bounded; hashed
place           Research OS chooses
                `.research-os/experiment-inputs/<param>-<sha16>.json`
preregister     `(path, sha256)` enters `ExecutionSpec.inputs`, and so the
                specification digest and the variation digest; the record
                names the digest, the store holds the bytes
materialise     written from the store into the disposable workspace,
                rehashed on the way in, `chmod 0444`
```

**The schema subset is closed and unknown keywords are refused, at
configuration time.** `type`, `enum`, `const`, `properties`, `required`,
`additionalProperties: false`, `items`, `minItems`, `maxItems`, `minimum`,
`maximum`, `minLength`, `maxLength`. A researcher who writes `pattern:` is
told it is not honoured rather than being left to believe it is -- the same
defect as a bound that is enforced and never stated, which this codebase
paid for five times before §AA.4.1.

**Objects are closed by default**, which is the opposite of JSON Schema's
own default and is deliberate: a composed document reaches a program the
researcher trusts, and a key nobody declared is exactly the one that should
not pass.

What this does not move:

- the program and the argv. A composed document is a *file the command
  reads*, and can never become an argument, a flag, or a second command;
- the destination. The caller supplies content and nothing else; a caller
  that names a path is refused, and the path Research OS chose is put
  through the same worktree-containment rule an untrusted value faces;
- the conclusion. The decision rule is still fixed before the result exists
  and still applied by ordinary code.

**Replication.** `variation_digest` includes the composed inputs, so a
replication that varies the plan varies by *content* rather than by a
filename that happens to carry a digest. `assert_varies` is unchanged and
now has something real to compare.

**Compatibility.** Both digests omit the key when there are no composed
inputs, so every preregistration written before this route existed rebuilds
to its original hash. `tests/test_experiment_generated_input.py` pins the
real stored specification of `PEXP-20260922T195752Z-4331c06a` to prove it.

### 19.10 The scientific contract: analysis first, design second, both frozen

§AB.5 of the report demonstrated the defect this section closes: the same
model call composed the plan *and* fixed the threshold, so a
"preregistered" `SUPPORTS` was reachable by choosing the grid. And §AB.3
located the other half: the only analysis the route could express was "read
one number out of one file", so a question whose falsifier asked for a
regression coefficient was `INSUFFICIENT` before anything ran -- a missing
*analysis* was still human-owned.

A measurement is now governed by a **scientific contract**
(`scientific_contracts`, migration `0031`; `portfolio/scicontract.py`):

```text
hypothesis   idea version content digest                  what is claimed
analysis     AnalysisSpec, frozen FIRST by analysis_designer   how it is read
design       DesignSpecification, frozen SECOND by the         how it is measured
             experiment designer, against the frozen analysis
contract     digest over all three                         what executions bind to
```

**The order is the fix.** `analysis_designer@1` is asked before any design
exists: estimand, raw observables (a scalar at a path, or a list of records
in JSON or CSV), inclusion rules, what an incomplete record does, reductions
from a closed set (`value`, `count`, `fraction`, `mean`, `median`, `std`,
`min`, `max`, `sum`, `quantile`, `difference`, `ratio`, `correlation`,
`ols_coefficient`), the primary statistic, an optional seeded percentile
bootstrap, the two predicates, and **support requirements** -- how many
records and how many distinct values of each compared variable the data must
hold. `experiment_designer@7` is then shown the analysis's *requirements*
(`scicontract.requirements_block`) and deliberately **not its thresholds**,
and its output contract has no field for a rule. A replication inherits the
primary's analysis by digest rather than fixing its own.

**Evaluation is ordinary Python** (`portfolio/analysis.py`). A missing
observable, a missing field, an undefined reduction (empty selection, zero
denominator, a regression the design does not identify) or unmet support is
`INSUFFICIENT`; `on_missing` has no other value. With an uncertainty,
`SUPPORTS` requires the whole interval to satisfy the success predicate.
Unmet support is the co-design defence checked against what was *measured*:
a grid that collapses the variable a statistic depends on reads
`INSUFFICIENT`, whatever number it would have produced.

**Immutability is the database's.** A trigger refuses any change to a
contract's analysis half after insert, to its design half once written, a
return to `ANALYSIS_FROZEN`, a change to `FROZEN` other than `SUPERSEDED`,
and a direct delete; a second trigger freezes what an experiment
preregistered (spec digest, rule, preregistration artifact, contract). The
application re-verifies every digest against the stored artifacts before
anything runs and before anything is read (`scicontract.verify`,
`empirical._verified_contract`), including that the specification about to
run realises the frozen design's argv, outputs, composed inputs and seeds.

**A post-result rule change is a new object.** A second preregistered
contract for the same idea version is refused by a partial unique index;
`empirical.amend_contract` creates an `EXPLORATORY` contract naming its
parent, and `empirical.reanalyse` re-reads the parent's stored outputs under
it, writing an artifact marked `confirmatory: false` and **no evidence row**.

**An implementation repair cannot touch the science.**
`empirical.repair_implementation` may change only
`scicontract.IMPLEMENTATION_FIELDS` (time limit, scheduler resources); it
re-executes the same contract as a new experiment and is refused if the new
specification does not realise the frozen design. The one repair made
automatically is a run that timed out under a tighter limit than is now
permitted, bounded by `MAX_IMPLEMENTATION_REPAIRS`.

**An undeclared capability is refused and kept.** When no declared command
can produce the analysis's observables, the contract becomes
`BLOCKED_CAPABILITY` with a capability request (from the designer, or derived
by code from the analysis when the project declares no command at all), the
analysis stays frozen, and asking again while `experiments.yaml` is unchanged
costs no model call.

**Provider neutrality.** No digest covers a provider, model, call id or
prompt identity; those are provenance recorded beside the digests.

Tests: `tests/test_portfolio_scientific_contract.py`; the mutation evidence
is in the closure report.

---

## 20. The research frontier: recorded events become new science

§AB.7 of the report measured why recursive discovery never happened on real
work: `max_depth` was 0 across 254 ideas, because the only producer of a
child was `BRANCH`, which `select_stage` reaches after meta-review and
replication. The frontier (`portfolio/frontier.py`, migration `0032`) makes a
child reachable from every event that raises a question.

**Frontier requests** (`frontier_requests`). An event is recorded where it
happens, by ordinary code reading a stored field, as one request with a
`basis`: `INSUFFICIENT` or `ANOMALY` (a primary measurement that could not
settle, or landed between the predicates), `REPLICATION` (a replication whose
conclusion differs from its primary's), `FALSIFIER_OBJECTION`,
`REVIEWER_CRITICISM` (an objection whose author wrote a `follow_up_question`;
a meta-review's `follow_up_questions`), `RESULT` (a meta-review recommending
`BRANCH`/`DEEPEN`), `LITERATURE` (a verified gap or disagreement, §21),
`REFEREE_FINDING` and `EVIDENCE_GAP` (§22). A unique index on
`(project, kind, basis, source_ref)` makes one event one request however
often it is replayed.

**The follow-up explorer** (`follow_up_explorer@1`, its own role). The tick
buys one per open request, one in flight at a time, *ahead of* idea work so a
busy portfolio cannot starve its own recursion. It is shown the parent idea,
the event and the parent's evidence, and returns children with their lineage
relation -- its contract has no field for the parent, so it cannot revise it.
Children pass deterministic deduplication; a duplicate is recorded as
`CONVERGENCE` on the existing idea. Bounds: `max_children_per_branch`, the
lineage's active ceiling, `max_lineage_depth` (default 6; deeper requests are
declined at $0), and a request failing `max_stage_failures` times is declined.

**A child never edits its parent.** It is a new idea -- new versions, new
contract, a lineage edge, provenance naming the request -- and the parent's
frozen hypothesis and contract are byte-identical afterwards
(`tests/test_portfolio_frontier.py`).

**Provenance** (`idea_provenance`, append-only by trigger). Every idea is
created with at least one reason (`BLIND_EXPLORATION`, `SEEDED_EXPLORATION`
naming the seeds, `FAILURE_MINING` naming the failure, `LITERATURE` naming
claims, each request basis naming the request, `BRANCH`, `REVIVAL`); the
migration backfills existing ideas from `origin`. Deduplication appends
`CONVERGENCE` to the survivor instead of discarding why the duplicate
existed.

**Explicit continuation.** `frontier.settle` gives an idea whose track has
nothing left to run an explicit state: `REJECTED` when a fatal objection to
its claim stands, otherwise `PARKED` with the reason and a revisit condition
-- except the thin-novelty dead end, which first asks the literature (§21)
and waits (`BLOCKED_DEPENDENCY`, an operational state). It runs at the end of
every stage and in the tick, so ideas left in limbo by earlier builds settle
too. A meta-review recommending `REJECT` or `PARK` now does so; `VALIDATED`
ideas with nothing left to run are closed for synthesis (§22).

**Explorer boundaries.** The blind explorer is shown the charter and nothing
from the bank, seeds or capsule hypotheses, and may cite nothing
(`derived_from` must be empty). The failure-mining explorer (v2) is shown
rejected ideas, standing objections, and the failed and inconclusive
measurements; a candidate naming a rejected idea becomes its child. Every
cited source is checked against what was supplied, fail-closed.

## 21. Literature intelligence

`portfolio/litintel.py`, migration `0033`. The shared index keeps *works*;
`literature_claims` keeps what they *say* for this project's questions:
`FINDING`, `METHOD`, `DATASET`, `LIMITATION`, `DISAGREEMENT`, `GAP`,
`OPEN_QUESTION`, each with the work keys it rests on (a check constraint
refuses none), a verification level (`CITED`; `QUOTED` when a verbatim excerpt
was found in the cited source's stored text) and immutability by trigger.

- **Targeted requests.** An idea asks a precise question (`litintel.ask`, a
  frontier request of kind `LITERATURE`). The tick buys an answer:
  discovery first when providers are configured (`LiteratureService.retrieve`,
  the existing A0 retrieval, behind an injected retriever), then an index
  search, then `literature_reader@1` answers from the packet alone. Ordinary
  code verifies every citation and quotation before anything is stored;
  one invented key or misquotation invalidates the whole reading. Claims
  bearing on the idea become `LITERATURE` evidence rows bound to its version.
- **Literature-driven discovery.** A verified gap or disagreement raises a
  `LITERATURE` follow-up request against the asking idea; and
  `literature_explorer@1`, shown only frontier claims and the charter,
  proposes depth-0 ideas that must cite the claims they grew from.
- **Novelty challenge.** Unchanged (§5, §9), plus: an audit that found too few
  sources asks the literature before the idea is parked.
- **Watching.** `litintel.ensure_watch` puts a `LITERATURE_WATCH_DUE`
  schedule on the existing table; its work raises targeted requests for the
  liveliest ideas, idempotent per day. Not enabled by default.

Deterministic tests use a fixture corpus and need no credentials
(`tests/test_portfolio_literature_intel.py`).
