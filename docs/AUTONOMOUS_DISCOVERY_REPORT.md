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
provider was called, no scientific project was modified, and the daemon was
never left running. One qualification, because §O depends on it: a real
project was *read* — cloned, read-only, into a scratch directory — and the
deterministic half of the portfolio was run against its capsule with no
provider anywhere. That found two defects and it is still not a dogfood. The
verdict in §U is chosen accordingly.

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
| an objection is answered by someone else | role comparison in `resolve_objection` | `test_an_objection_cannot_be_resolved_by_the_role_that_raised_it` |
| an objection resolves only forward | check constraint | `test_an_objection_cannot_be_resolved_by_a_version_that_never_named_it` |

Status: **implemented, unit-tested, integration-tested.**

The acyclicity mechanism deserves a note. An earlier draft claimed a `CHECK`
was sufficient; it is not — a `CHECK` sees only its own row, so a writer that
invents the two depths satisfies it. What makes it sound is the composite
foreign keys on `(idea_id, depth)`, which make an invented depth a referential
error and make `ideas.depth` immutable while an edge depends on it. An
independent architecture review found the original claim; three tests now hold
the corrected one.

One further correction belongs here rather than in §H, because it is about how
the rows are *read*. `PortfolioStore.live_reviews` takes the four liveness
filters as parameters, and an independent test audit found six callers relying
on their defaults while two of the four defaulted to off — so "live" meant one
thing in the gate and another everywhere else. The parameters now default to a
sentinel that means strict, and a caller that wants a looser reading has to say
so at the call site. The architecture document's claim that "'Live' is defined
once" was false when it was written and is true now.

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
- When the origin call cannot be identified at all,
  `classify_independence` returns `Independence.NONE`. An independent security
  review found it returning the plausible-looking `DIFFERENT_CONTEXT` instead —
  failing open, and in the direction that flatters the system. A review whose
  provenance is unknown now counts for nothing. The lookup itself is
  `RuntimeStore.get_model_call`, one indexed read, rather than the 500-row
  scan of the first implementation.
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

It also hid one, and that is worth stating plainly because it is the standard
failure mode of a gate test. The control builds its rows directly, so it proves
the gate *accepts* a complete idea and says nothing about whether any
production path can assemble one. An independent test audit asked the second
question and the answer was no: on `novelty_or_literature` — the only route
that can execute on this host — `run_replicate` could not produce a second
literature audit that `_replication_met` would count, so `HUMAN_READY` was
unreachable in practice while all twenty-four gate tests passed. It was
reproduced standalone before it was fixed. `_second_terminology_path` and a
`second_path=True` audit close it, and the test that now holds it open is
`test_every_status_on_the_way_is_written_by_production_code`, which drives an
idea the whole way and asserts the exact ladder

```text
CANDIDATE → PROMISING → INVESTIGATING → REVIEW → VALIDATED → HUMAN_READY
```

with every one of those six statuses written by production code and none of
them by the test. Two more production defects fell out of writing it: nothing
performed `CANDIDATE → PROMISING` at all, and `META_REVIEW` was version-scoped
so no stage re-evaluated the gate after replication.

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

An independent security review then took the exemption apart, and four things
about it were wrong. The prefix test was an unanchored `startswith`, so a ref
merely *containing* the reserved name was exempt. The exemption was total, so
deleting the bank branch during a coding run was invisible as well as creating
it. The tip check was skipped on the digest-equal early return and skipped
again whenever no tip was on record, which is exactly the state an attacker
would arrange. And a repository with no portfolio has no Curator, so anything
found under that namespace there is unexplained by construction. All four are
closed: `is_reserved_ref` is anchored, a reserved ref is recorded in the
fingerprint under a constant value so its *disappearance* is drift while its
appearance is not, `_verify_tip` runs before the shortcut and refuses a branch
it does not recognise, and `_ensure_worktree` checks both the branch and
`--git-common-dir` before adopting a directory. `UnexpectedBankTipError` is
recorded as a `PORTFOLIO_BANK_TIP_UNEXPECTED` event and is not retried.

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

Every one of those ceilings is per idea, per lineage or per project, and
exploration is none of the three — which is how it came to be the one thing
with no bound at all. Two now hold it: one explorer in flight at a time, and
a `PAUSED_NO_FRONTIER` stop once exploration has stopped producing ideas. See
§N.8; the point worth keeping is that a ceiling table can look complete and
have a hole in it wherever the work does not belong to one of the nouns the
table is indexed by.

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
researchctl portfolio enable | status | top | pause | resume | digest
researchctl ideas list | show | lineage | rejected | validated | human-ready
researchctl seed add | list
```

`portfolio enable` was missing until the CLI was read as a researcher would
use it, and its absence was the most consequential defect in the build.
`ensure_schedule` had existed since the first commit with no production
caller: every test reached it directly, so a project could be seeded, acquire
portfolio state, report `RUNNING`, and never be ticked by anything. The
headline property of the whole layer — that the portfolio continues without
the researcher advancing cycles — was unreachable from the command line, and
the suite was green. `portfolio status` now says "not scheduled" when nothing
will tick a project, and `seed add` says there is no next pass rather than
promising one.

`researchctl portfolio digest` rather than `researchctl digest show`, because
the kernel already owns `researchctl digest <OBJECT-ID>`. The deviation from
the brief's spelling is recorded rather than resolved by cleverness.

The digest is rendered deterministically from stored fields; no model writes a
word of it. Top ideas are a Pareto front over three dimensions — novelty,
computed evidence strength, and literature confidence — thinned by diversity,
rather than an ordering by the scheduling utility. It was six until a security
review pointed out that three of them were the model's own scores, which let
an idea reach the researcher by rating itself highly. Every entry carries its
board independence.

Status: **implemented, integration-tested.**

## N. Test evidence

```text
baseline (dc6cfe3)   3963 passed, 8 skipped, 693s
this branch          see §T for the figure at the final commit
new portfolio tests  14 files, 244 test functions, 7.9k lines
new portfolio code   11.6k lines across 20 modules
new/changed kernel   runtime/{refs,workkinds,extensions}.py, service.py,
                     policy, interfaces, routing, store, models, registry,
                     daemon, coding, and two test files extended
```

Six defects were found *by* the new tests rather than by review, and a seventh
by reading the command set. Each is recorded in the commit that fixed it:

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
   kernel-only install works with two dependencies;
7. no command that starts a portfolio — see §M. This one was found by reading
   the command set rather than by running anything, which is the honest
   account of it: no test could have found it, because every test called
   `ensure_schedule` directly;
8. **an explorer bought every cadence, forever.** The allocator's
   leftover-capacity branch fires whenever a slot is free and the candidate
   pool is under its *ceiling* — on a quiet portfolio, every tick. Nothing
   counted explorers already queued, so each tick added another at $0.60
   whether or not the previous one had started. The only thing that stopped
   it was the project budget, which is true and is the wrong diagnosis: the
   researcher would read `PAUSED_BUDGET_EXHAUSTED` for what is actually
   "every idea this explorer generates is a near-duplicate". One explorer in
   flight at a time, plus a `barren_explorations` count that pauses as
   `PAUSED_NO_FRONTIER`, are the two bounds. Found by running the tick
   twice against the real project (§O) and watching the queue grow;
9. and `PAUSED_NO_FRONTIER` could not previously fire at all. Its condition
   required an empty candidate pool with nothing allocated, and an empty pool
   with a free slot is exactly when the allocator buys a blind explorer. The
   status was reachable in the enum, the CLI and the documentation, and not
   in the code. The dead branch is gone and the status now has a trigger that
   works.

Two came from the independent design reviews before implementation: the
evidence-swap hole under a live review, and the Curator/escape-check collision.

### What the four independent reviews found

Two read the design before any of it was written; two read the implementation
after it was finished. All four were read-only, and none of them could edit the
thing it was judging.

```text
architecture (design)         evidence swap under a live review
                              acyclicity CHECK insufficient -> composite FKs
                              Curator vs canonical_fingerprint  (blocker)
                              allocator tie-broken by a model
                              model-chosen adjudication type
                              work_items.dedup_key permanently unique
scientific workflow (design)  objections had nowhere to live
                              REJECT and PARK with no stated reason
                              BudgetScope.IDEA was not a one-line addition
test audit (implementation)   HUMAN_READY unreachable on the only live route
                              no test showed promotion at all
                              live_reviews strict only by convention
                              two vacuous assertions, one 500-row scan
                              novelty_floor documented and never read
security (implementation)     unanchored reserved-ref prefix
                              tip check skipped on two paths
                              worktree adopted on the strength of a .git
                              independence failing open
                              adjudication type steerable by the generator
                              self-assessed scores driving the Pareto front
                              unfenced charter text reaching prompts
```

Every CRITICAL and HIGH finding is fixed. Fixing them surfaced three further
production defects that no reviewer saw and no test had yet reached: nothing
performed `CANDIDATE → PROMISING`; the literature audit looped when the corpus
could not reach `novelty_min_sources`; and `META_REVIEW` was version-scoped so
the gate was never re-evaluated after replication.

The reviews are worth this much space for one reason. Eleven of the findings
are defects the test suite could not have found, because in each case the test
and the code shared the same wrong assumption.

## O. Sparse-regression dogfood

**Not performed**, in the sense that matters: no provider was called, so not
one idea in this build was generated, screened, falsified or reviewed by a
model. Everything below `VALIDATED` on this branch was produced by a script
pretending to be one.

What *was* done is narrower and worth stating exactly, because it found two
real defects. The project at
`/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection`
was cloned read-only into a scratch directory (`--no-hardlinks`, detached
HEAD), against a second disposable PostgreSQL under a redirected state home,
and the **deterministic** half of the portfolio was run against the real
capsule — `CHARTER.md` plus 27 objects: 11 questions, 7 hypotheses, 4
assumptions, 2 decisions, 2 evidence records and 1 experiment:

```text
researchctl runtime dev-db start      a second cluster, not the researcher's
researchctl runtime migrate           24 migrations, head 0024
researchctl register-project          cg-sparse-regression
researchctl portfolio enable          SCHED-...  every 2 min
researchctl runtime budget --max-cost-usd 15.00
researchctl seed add                  one direction
six ticks, twelve simulated minutes   one explorer, RUNNING throughout
```

The first tick read the real project and allocated exactly what it should:
`seeded_explorer`, because "the candidate pool is 0, below the floor of 6: 1
researcher seed(s) unconsumed". The next five allocated nothing, because one
explorer was already in flight.

That is the behaviour *after* §N.8. What was actually observed first was two
ticks against this project and two explorer work items queued, neither
started — which is the defect, and one more tick would have bought a third.
The six-tick run above is the fix being checked against the same project.

The researcher's working copy was not modified — it is still clean at
`ad4d66c` — their registry was not written (last modified 2026-09-17), and
their `researchd` was left running and untouched throughout.

`docs/AUTONOMOUS_DISCOVERY_DOGFOOD.md` is the procedure for the real thing,
including why it was not run here: the `claude` CLI adapter authenticates from
`~/.claude/.credentials.json`, which `researchd` and every interactive session
share, and at the time of writing the researcher's live control plane was
advancing *this same project* with three runs already at attempt 1 of 5 after
`provider_unavailable`. Adding a third consumer to that token is avoidable,
and avoiding it is the researcher's call to make, not an agent's.

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

**And that route's replication is terminological, not independent.** Having
established that `HUMAN_READY` was unreachable on it, the fix was to make
`run_replicate` perform a second literature audit down a different terminology
path — different search vocabulary, a different call, sources compared against
the first set. That is a real check and it does catch a novelty claim that
survives only because of how it was phrased. It is not a replication in the
sense a scientist means: nothing is re-derived and nothing is re-run. The gate
counts it because on this route there is nothing else to count. An empirical
or mathematical idea, once those routes are wired, must not be allowed to
satisfy replication the same way, and `REPLICATION_RULES` keeps them separate
precisely so that cannot happen quietly.

**Prompt quality is unmeasured.** Fourteen prompts, none of which has met a
real model. A prompt that produces plausible but useless output would pass
every test in this branch.

**Two of the worst defects in this build were found by running it, not by
testing it.** §N.7 and §N.8 — no way to start a portfolio, and an explorer
bought every cadence forever — were both invisible to 324 passing tests, and
both took under a minute to find once the commands were typed in the order a
researcher would type them. That is a statement about what remains: the parts
of this system nobody has *used* are the parts most likely to be wrong, and
the largest of those is everything downstream of a real model call.

**Losing the operational database loses uncurated ideas.** Bounded by curating
after every tick that changed the bank, reported as a number by
`researchctl portfolio status`, and stated in ADR-0002 rather than hidden.

**The similarity threshold is a guess.** 0.72 is chosen to be high enough that
two different directions in one subfield do not collide. Whether it is right is
a dogfood question.

**The gate tests and the runner can share a wrong assumption.** This is the
defect class that produced the worst finding in §N, and it is structural
rather than fixed: a gate test constructs rows and asks whether the gate
accepts them, and it is blind by construction to whether anything can produce
those rows. One end-to-end promotion test now covers the live route. The other
three routes have no such test, because they cannot execute, so the same blind
spot is open on all three and will stay open until they are wired.

**Disposable clusters are not reliably disposed of, and a soak will meet
this.** `researchctl runtime dev-db stop` reported *stopped the cluster* on a
cluster whose postmaster was still running half an hour later, and this
machine carries around 130 `postgres` processes from `pytest` clusters two to
five days old. It is pre-existing kernel behaviour, outside this work package
and not changed by it, and it is written down here because a 72-hour
unattended soak that starts and stops disposable databases is where it stops
being cosmetic. `docs/AUTONOMOUS_DISCOVERY_DOGFOOD.md` §6 has the check and
the manual shutdown.

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
commits       twelve, listed in the log
pushed        no
merged        no
rc/thesis-pilot  unchanged, still dc6cfe3, still unpushed
```

The gates at the final commit, all four run against the tree as it stands:

```text
full suite            4308 passed, 8 skipped, 873s, exit 0   (forward)
full suite            4308 passed, 8 skipped, 838s, exit 0   (--reverse)
ruff check .          all checks passed
ruff format --check   370 files already formatted
migrations            24 applied on a fresh cluster, head 0024
```

Against the 3963-test baseline at `dc6cfe3`, that is 345 new tests and no
existing test changed to accommodate anything. The reverse-order run is the
one that matters for a layer with this much shared database state, and it
finds the same number as the forward run.

Nothing was pushed, nothing was merged, and no existing history was rewritten.

## U. Formal verdict

Against the brief's own vocabulary:

```text
AUTONOMOUS_DISCOVERY_BETA
```

Not `RELEASE_CANDIDATE`, and the reason is explicit in §43 of the brief: the
release criteria require dogfood, a soak and a scientific-quality audit, and
none of the three was performed.

Against the criteria one at a time, rather than in a sentence that averages
them:

```text
engineering        met.  suite green forward and reverse, migrations green,
                   recovery, provider failure and budgets tested, real-state
                   contamination zero
safety/authority   met.  asserted by parsing the package, not by assertion
portfolio          met for one idea travelling the whole ladder on the one
                   route that can execute, under a scripted provider; three
                   of the four routes stop below VALIDATED by design and have
                   no end-to-end test because they cannot run
bank               met.  deterministic, idempotent, orphan, integration-tested
                   against a real repository
scientific work    NOT met.  no idea in this build was generated, killed,
                   deepened or reviewed by a model. Everything above was
                   produced by a script pretending to be one
```

An earlier draft of this section said the portfolio, bank and
scientific-workflow criteria "are met as implemented and integration-tested".
That was wrong in the way §N describes: at the time it was written the
terminal promotion step had no test, and — as the reproduction later
confirmed — no working implementation on the only route that can execute. Both
are fixed and both are now tested. The sentence is corrected rather than
deleted, because a build report that quietly revises its own verdict is worth
less than one that records having been wrong.

One more thing belongs in a verdict and not in a risk list. The two worst
defects in this build — no way to start a portfolio, and an explorer bought
every cadence forever — were found in the last hour, by typing the commands
against a real project rather than by any of the 4,308 tests. Both had been
sitting under a green suite. That does not make the suite worthless; it makes
the suite's *coverage claim* narrower than a passing run looks. Read the
`engineering: met` line above as "everything the tests examine holds", not as
"the system works", and weigh the three unperformed exercises in §O–§R
accordingly: they are not paperwork.

What remains entirely absent is evidence that the ideas this system produces
are worth anything, which is the criterion the brief's §46 says matters most.

The honest one-line summary: **the machine is built and its safety properties
are tested; whether it does good science is unknown, because it has not yet
been asked to.**
