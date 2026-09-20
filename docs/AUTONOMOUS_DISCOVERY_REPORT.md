# Autonomous discovery: build report

What was built on `architecture/autonomous-discovery-v1`, what it was tested
against, and — the part that matters most — what has **not** been demonstrated.

This report uses five words precisely and never interchangeably:

```text
implemented           the code exists and imports
unit-tested           its behaviour is asserted against constructed inputs
integration-tested    asserted end to end against a real PostgreSQL and a
                      scripted provider
dogfood-proven        run against a real project with a real provider
unattended-proven     run unsupervised for a sustained period
```

**Nothing in this report is dogfood-proven or unattended-proven.** No model
provider was called, no scientific project was touched, and the daemon was
never left running. The verdict in §U is chosen accordingly.

---

## A. Starting checkpoint

Verified rather than assumed, before anything was modified:

```text
branch            rc/thesis-pilot
local SHA         dc6cfe36437b805eecfc93bf1dfbfac7987d3a05
remote SHA        dc6cfe36437b805eecfc93bf1dfbfac7987d3a05  (origin/rc/thesis-pilot)
working tree      clean apart from an untracked .claude/
runtime schema    head 0018
full suite        3963 passed, 8 skipped, 693s, exit 0
researchd         active (running) since 2026-09-20 15:27, against the
                  disposable cluster under ~/.local/state/research-os/devdb
```

The researcher's live control plane was left alone. All work happened on a new
worktree:

```text
branch    architecture/autonomous-discovery-v1
base      dc6cfe36437b805eecfc93bf1dfbfac7987d3a05
path      /home/nicacevedo/research/research-os-discovery
```

`rc/thesis-pilot` is unchanged and unpushed. Every test runs against a
`pgserver` cluster the test session starts; `tests/conftest.py`'s existing
guard refuses a DSN naming any database the run did not create, and it was not
weakened.

## B. Final architecture

`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` is the live specification.
`docs/adr/0001`–`0004` record the four hard-to-reverse choices.

```text
SCIENTIFIC TRUTH   Git-tracked capsule files            unchanged
      ^  human act only: propose promote / review
AUTONOMOUS BANK    research-os/autonomous branch        NEW
      ^  deterministic Curator, sole serialized writer
IDEA STATE         PostgreSQL: 9 tables                 NEW
      ^  deterministic allocation, bounded idea tracks
OPERATIONAL TRUTH  queue, leases, ledger, budgets       R5, unchanged
```

No dependency was added. No technology left `ARCHITECTURE.md` §12's postponed
list. The runtime does not import the portfolio, and a test parses the package
to say so.

## C. Schema and invariants

Six migrations, 0019–0024, one concern each. Nine tables.

| invariant | mechanism | test |
|---|---|---|
| a review points to an exact idea version | composite FK + `reviewed_content_digest` | `test_a_material_revision_stales_every_review` |
| material revision stales a review | digest comparison in SQL | same |
| **changed evidence stales a review** | `reviewed_evidence_digest` | `test_swapping_the_evidence_under_a_review_stales_it` |
| **a superseded prompt stales a review** | `prompt_version` vs this build | `test_a_review_by_a_superseded_prompt_is_not_live` |
| lineage is acyclic | `unique (idea_id, depth)` + composite FKs + depth check | `test_the_database_refuses_a_lineage_cycle`, `..._an_edge_whose_depth_is_a_lie`, `..._depth_cannot_be_updated` |
| one active track per idea | partial unique index | `test_an_idea_has_at_most_one_track_in_flight` |
| same scientific basis is idempotent | partial unique index on `ACTIVE`/`SUCCEEDED` | `test_the_same_basis_runs_once_but_a_failure_does_not_poison_it` |
| same review request is idempotent | unique index on the five-part key | `test_recording_the_same_review_twice_is_one_review` |
| a numerical witness is not a proof | check constraint | `test_a_numerical_witness_cannot_be_recorded_as_proof` |
| literature evidence names a retrieved source | check constraint | `test_literature_evidence_must_name_a_retrieved_source` |
| a retired idea says why | check constraint | `test_retiring_an_idea_requires_a_reason` |

Status: **implemented, unit-tested, integration-tested.**

The acyclicity mechanism deserves a note. An earlier draft claimed a `CHECK`
was sufficient; it is not — a `CHECK` sees only its own row, so a writer that
invents the two depths satisfies it. What makes it sound is the composite
foreign keys on `(idea_id, depth)`, which make an invented depth a referential
error and make `ideas.depth` immutable while an edge depends on it. An
independent architecture review found the original claim; three tests now hold
the corrected one.

## D. Agent roles

Fourteen role contracts in `portfolio/contracts.py`, fourteen versioned
templates in `portfolio/prompts.py`, each with a JSON schema derived from its
contract so the two cannot drift.

```text
blind_explorer  seeded_explorer  failure_mining_explorer
scientific_discovery  novelty_screener  literature_scout  falsifier
methodology_reviewer  novelty_reviewer  skeptic_reviewer
replicator  meta_reviewer  duplicate_adjudicator  brancher
```

Three omissions are enforced by types rather than by discipline:

- the blind explorer's template declares no bank field, so `render` refuses
  one;
- no reviewer's template or packet has a field for another reviewer's verdict;
- no template anywhere accepts an adjudication type, a quality tier or an
  independence class — all three are computed from rows.

Status: **implemented, unit-tested.** Not one of these prompts has been sent to
a real provider.

## E. IdeaTrackGraph

One bounded stage per invocation, then END. Eleven stages, chosen by
`stages.select_stage`, which is pure, total and asks no model.

The review board is three nodes, which is the one place LangGraph's
checkpointing pays. The docstring says what it does *not* buy: durable
idempotency across calls comes from the database — each reviewer node asks
whether its review is already recorded and skips — which survives a process,
machine or code change in a way a checkpoint does not.

`test_portfolio_track.py` drives a whole idea through the real graph against a
real PostgreSQL checkpointer, seventeen tests. Status: **implemented,
integration-tested.**

## F. PortfolioGraph

Implemented as a **bounded deterministic pipeline, not a LangGraph**, with
§26's node names. This is a recorded deviation from the brief and ADR-0004
gives the reason: a tick makes no model call, has no interrupt, completes in
milliseconds and rebuilds its input from the database next pass. There is
nothing to checkpoint, and writing checkpoint rows every tick per project
forever would be a cost buying no property.

It reaches the daemon through the existing `schedules` table — event, work
item, handler — rather than a new pass in the daemon's ordering.
`test_portfolio_daemon.py` drives that chain. Status: **implemented,
integration-tested.**

## G. Reviewer independence

Recorded, never assumed, and the honest limit is surfaced as a number.

- `idea_versions.origin_call_id` and `idea_reviews.call_id` name the two
  calls. `ModelResponse` gained a `call_id` field so the comparison is
  possible at all.
- `independence_vs_origin` reuses `runtime.interfaces.Independence` verbatim.
  A review whose call *is* the origin call raises rather than degrading.
- `board_independence` counts distinct `(family, model)` pairs across the
  three reviewers. **When it is 1 — which is every board on this machine —
  nothing rendered anywhere uses the word "independent".** The gate says so in
  a note, the digest repeats it per idea, `researchctl ideas` prints it, and
  the Curator's page header carries it.

Status: **implemented, integration-tested.** What is *not* demonstrated is
independence itself: this host has one provider family, so every board this
system could produce today is one model three times.

## H. Quality gates

`portfolio/gates.py`. Deterministic, reads rows, never prose.

```text
PROMISING     question, mechanism, falsifier; screen run; no standing FATAL
VALIDATED     + three live, non-negative reviews
              + the union of evidence rules over every declared type
              + a novelty audit with >= 3 distinct retrieved sources
              + no standing CRITICAL or FATAL
HUMAN_READY   + every MAJOR+ objection answered
              + replication appropriate to the type
              + limitations stated, significance stated
```

Six things the gate refuses that a system of this shape would otherwise allow,
each with a test:

1. three reviews that all say REVISE are not three reviews;
2. a mathematical idea needs an *executed* check, not a prose derivation;
3. `CONSISTENT_WITH` never satisfies substantive evidence or replication;
4. declaring a second adjudication type adds requirements and removes none;
5. replication by the call that produced the original is not replication;
6. a meta-reviewer recommending `HUMAN_READY` with no reviews gets `CONTINUE`.

The positive control — `test_a_complete_idea_reaches_human_ready` — is what
makes the other twenty-three capable of failing, and it found two real defects
on its first run.

Status: **implemented, unit-tested, integration-tested.**

## I. Authority model

Unchanged in shape. Fifteen `ActionKind` members added, all A0 except
`curate_idea_bank` (A1), each with a `Dispatch.PORTFOLIO` so `runtime doctor`
does not report fifteen gaps that are not gaps. One authority table for the
whole system.

`tests/test_portfolio_authority.py` asserts, by parsing the package: no module
writes a human Review, promotes a proposal or an insight, or reimplements the
acceptance rule; the Curator imports nothing that can reason; the digest has no
way to obtain a model narrative; the allocator has no `models` parameter; and
the Curator refuses any path under `.research/`.

Status: **implemented, unit-tested.**

## J. Idea bank and Curator

`.research-os/autonomous/` on branch `research-os/autonomous`, written from a
worktree under the state home.

The finding that made this non-trivial came from an independent architecture
review and was a shipping blocker: `canonical_fingerprint` hashes every ref in
the project repository across a whole coding run, so a Curator commit during
one would make that coding run report an escape it did not commit — the same
failure `coding.py`'s own docstring records having had once before, with a
different writer. The namespace is now reserved in `runtime/refs.py` and
excluded from the fingerprint, and the blind spot that creates is covered on
the other side: the Curator records the commit it wrote and refuses a tip it
does not recognise.

`test_curating_does_not_make_a_concurrent_coding_run_report_an_escape` is the
regression, and `test_a_real_escape_is_still_detected` is its control.

Status: **implemented, integration-tested** against a real Git repository.

## K. Budgeting and diversity

Ceilings at portfolio/project (existing `BudgetScope.PROJECT`), run (existing),
idea and lineage (the allocator, before enqueue), and stage (the request's
`max_cost_usd`).

`BudgetScope.IDEA` was specified in an earlier draft and **downgraded
deliberately**: adding it would not have been a one-line enum change — three
enforcement paths in `budgets.py` hardcode the scope triple and `ModelRouter`
carries no idea id — and `BudgetScope.WORK_ITEM` already exists, is in the
check constraint, and is used by nothing. The allocator sums
`idea_actions.cost_usd` instead. The overshoot that allows is bounded by one
stage's own ceiling, and that bound is stated rather than hidden.

Diversity is tracked over `lineage_root`, `adjudication_types` and a
*normalised* subproblem label, all deterministic. Free-text model labels are
not used anywhere a bound depends on them.

Status: **implemented, integration-tested.**

## L. Failure and recovery semantics

`tests/test_portfolio_chaos.py`, seventeen tests covering §34's eight failures
and asserting the six prohibitions. What each failure leaves behind:

| failure | state afterwards |
|---|---|
| provider unavailable | `BLOCKED_PROVIDER`, status unchanged, no retire reason, no review, no disposition |
| the outage clears | the tick returns the idea to `IDLE` |
| malformed model output | no version appended, no review, no evidence |
| worker death mid-stage | action reclaimed, idea `IDLE`, basis not poisoned, revision not consumed |
| replayed tick | one action, one work item |
| a reviewer fails | two live reviews are two, and the gate says which is missing |
| Curator crash | reset to tip; the half-written file is never committed |
| budget exhausted | portfolio paused, idea still `PROMISING`, no retire reason |

Status: **implemented, integration-tested** against injected failures.

## M. CLI and digest

```text
researchctl portfolio status | top | pause | resume | digest
researchctl ideas list | show | lineage | rejected | validated | human-ready
researchctl seed add | list
```

`researchctl portfolio digest` rather than `researchctl digest show`, because
the kernel already owns `researchctl digest <OBJECT-ID>`. The deviation from
the brief's spelling is recorded rather than resolved by cleverness.

The digest is rendered deterministically from stored fields; no model writes a
word of it. Top ideas are a Pareto front over six dimensions, thinned by
diversity, rather than an ordering by the scheduling utility. Every entry
carries its board independence.

Status: **implemented, integration-tested.**

## N. Test evidence

```text
baseline (dc6cfe3)   3963 passed, 8 skipped, 693s
this branch          see §T for the figure at the final commit
new portfolio tests  13 files, 211 tests
new/changed kernel   runtime/{refs,workkinds,extensions}.py, service.py,
                     policy, interfaces, routing, store, models, registry,
                     daemon, coding, and two test files extended
```

Six defects were found *by* the new tests rather than by review, and each is
recorded in the commit that fixed it:

1. `discover` looping forever, because the test was "has discover run against
   this version" rather than "is it precise";
2. `adjudicate` appending a version to write a value derived from the
   falsifier, staling every review and re-running every cheap stage;
3. a `MIXED`-only idea selecting the evidence stage forever, because that type
   carries no evidence rule — found by the termination property;
4. the novelty matrix mapping everything but "same" to `CONSISTENT_WITH`, so a
   literature-adjudicated idea could never accumulate substantive evidence;
5. nothing moving `PROMISING` to `INVESTIGATING`, so an idea gathered evidence
   and was never reviewed;
6. importing `research_os.cli` pulling LangGraph, breaking the promise that a
   kernel-only install works with two dependencies.

Two came from the independent design reviews before implementation: the
evidence-swap hole under a live review, and the Curator/escape-check collision.

## O. Sparse-regression dogfood

**Not performed.** The project at
`/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection`
was not touched, no provider was called, and no seed was recorded against it.

This is the largest gap between what was built and what is proven, and it is
not a small one: every prompt in §D is untested against a real model, the
threshold in `duplicate_similarity` is a starting value rather than a
measurement, and whether the falsifier actually kills weak ideas cheaply is
exactly the kind of question only a dogfood answers.

## P. Second-project dogfood

**Not performed**, for the same reason.

## Q. Unattended soak

**Not performed.** `researchd` was not started against this branch, and the
researcher's running instance was deliberately left on `rc/thesis-pilot`.

## R. Scientific-quality audit

**Not performed.** There are no autonomously generated ideas to audit.

## S. Remaining risks

Stated as risks rather than as future work, because each is something a reader
should weigh before running this.

**The gates check shape, not truth.** Every requirement in §H is a check that
the right *kinds* of evidence exist and that enough separate readings happened.
None of them reads the science. A system of this shape can be satisfied by work
that is thorough and wrong, which is why the top tier is `HUMAN_READY` and not
`CORRECT`, and why the Curator stamps every page with what was actually
executed and retrieved.

**One provider family means no independent review.** Every board this machine
can produce is one model three times. The system reports that rather than
claiming otherwise, but the reporting does not make the review independent. A
second provider family is an install, not a code change.

**Two evidence routes are unwired.** A mathematical idea needs an executed
counterexample search and an empirical one needs the experiment pipeline;
neither is connected to the idea track in this build. Both stop below
`VALIDATED` with a message naming what is missing. That is correct behaviour
and it also means the only route to `VALIDATED` today is
`novelty_or_literature`, which is the weakest of the four.

**Prompt quality is unmeasured.** Fourteen prompts, none of which has met a
real model. A prompt that produces plausible but useless output would pass
every test in this branch.

**Losing the operational database loses uncurated ideas.** Bounded by curating
after every tick that changed the bank, reported as a number by
`researchctl portfolio status`, and stated in ADR-0002 rather than hidden.

**The similarity threshold is a guess.** 0.72 is chosen to be high enough that
two different directions in one subfield do not collide. Whether it is right is
a dogfood question.

**The daemon composition changed.** `researchd`'s entry point moved to
`research_os.service`. A deployment that invokes
`research_os.runtime.daemon:main` directly gets a control plane with no
portfolio handlers, which will fail portfolio work as `POLICY_REFUSED` rather
than doing something worse — but it is a deployment consideration and
`deploy/researchd.service` has not been re-validated against it.

## T. Git state

```text
branch        architecture/autonomous-discovery-v1
base          dc6cfe36437b805eecfc93bf1dfbfac7987d3a05
commits       five, listed in the log
pushed        no
merged        no
rc/thesis-pilot  unchanged
```

Nothing was pushed, nothing was merged, and no existing history was rewritten.

## U. Formal verdict

Against the brief's own vocabulary:

```text
AUTONOMOUS_DISCOVERY_BETA
```

Not `RELEASE_CANDIDATE`, and the reason is explicit in §43 of the brief: the
release criteria require dogfood, a soak and a scientific-quality audit, and
none of the three was performed. The engineering criteria are largely met — the
regression suite is green, recovery is tested, provider failure is tested,
budgets are tested, state contamination is zero — and the portfolio, bank and
scientific-workflow criteria are met *as implemented and integration-tested*.
What is entirely absent is evidence that the ideas this system produces are
worth anything, which is the criterion the brief's §46 says matters most.

The honest one-line summary: **the machine is built and its safety properties
are tested; whether it does good science is unknown, because it has not yet
been asked to.**
