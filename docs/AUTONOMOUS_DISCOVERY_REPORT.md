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

**This was true of the first revision of this report and is no longer true.**
That revision said: *nothing in this report is dogfood-proven or
unattended-proven; no model provider was called, no scientific project was
modified, and the daemon was never left running.* On 2026-09-21 all three were
performed — a real-provider dogfood on two materially different real projects,
a 3.5-hour unattended soak, and a scientific-quality audit of what the system
produced. §O–§R record what happened and §V records the eight defects that
running it found. The sentence is kept rather than deleted because the gap it
described is the reason the rest of this document is worth reading.

What is still **not** dogfood-proven is named precisely in §R and §U: six of
the eleven stages — adjudicate, literature audit, evidence, review board,
meta-review and replicate — have still never run against a real model, and no
idea has reached `VALIDATED`.

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

**Performed, 2026-09-21.** A real provider was called 173 times against two
real projects. This section records the first; §P the second.

```text
project    cg-sparse-regression, cloned --no-hardlinks, detached HEAD,
           `origin` removed so a push has nowhere to go
capsule    the real one: CHARTER.md plus 27 objects
seed       the two falsification questions from .research/STATE.md, verbatim
budget     $15.00 project ceiling
ran        01:12 -> 05:12 local, one `researchd`, unattended
```

The researcher's working copy was not modified — still clean at `ad4d66c` —
and the clone's own `research/2026-reassessment` branch is untouched.

### O.1 What the first real call produced

The seeded explorer's first call ($0.22, 145s, `claude-sonnet-5`) returned
three candidates, all on the researcher's actual open questions, one of them
citing `docs/2026/TIMELINE.md §T0` by section. **The largest single unknown in
the previous revision of this report — whether fourteen prompts that had never
met a model produce anything — is answered, and the answer is yes.**

### O.2 The falsifier kills, and its reasons are good

This is the economic question §5 of the dogfood procedure says decides whether
the layer works at all, and the answer is the strongest result here.

```text
66 ideas generated       48 rejected, 12 promising, 6 candidate
falsifier                63 calls, $8.99, ~116s each
cheap ladder             dedup $0.00 + screen $0.02 + falsify $0.15 per idea
```

Every one of the first seven rejections on this project was read in full and
assessed. **All seven are scientifically sound.** They include: that a proposed
equivalence test is vacuous because complementary slackness guarantees the
result for any correct reformulation; that column generation and
Celer/skglm/sklearn may not be solving the same problem, so a matched
duality-gap is not a common yardstick; that a design has no control variable
and so cannot distinguish its own named rival explanation; and that a mechanism
misattributes to column-generation theory a monotonic prediction that theory
does not make.

The second of those is the concern the project's own `CHARTER.md` raises about
the comparison class. The falsifier reached it independently of the charter
section that states it.

### O.3 The loop closes: rejections become better ideas

The failure-mining explorer read the rejections and produced, among others,
*"Column generation as a cardinality-targeted (support-size-indexed) LASSO
solver, benchmarked against homotopy/LARS rather than λ-indexed solvers"* —
which is a direct, constructive answer to the rejection that said the four
solvers may not solve the same problem. Homotopy and LARS *are*
support-size-indexed. The portfolio learned from its own kill.

Seventeen of the sixty-six ideas came from that explorer.

### O.4 A revision driven by a CRITICAL objection

`PIDEA-...-9fa13621` reached `PROMISING`. Its v1 proposed settling a question by
benchmarking; a reviewer objection said the empirical arm was unnecessary
because the claim follows from a short KKT derivation. The sharpening pass
rewrote the falsifier into exactly that derivation — *"a derivation and
close-reading exercise requiring no code execution or benchmarking"* — and
added the escalation condition under which a benchmark would then be warranted.

And the five objections **still stand**. The revision claims to answer them;
`resolve_objection` requires a different role to agree, and none has. The
anti-laundering property holds against a real model, not only against a
scripted one.

### O.5 The one number this exercise exists to calibrate

`duplicate_similarity: 0.72`, which `config.py` calls "a starting value, not a
measurement".

```text
46 idea pairs across both projects
max trigram Jaccard observed     0.377
median                           ~0.20
pairs reaching the 0.72 threshold  0
```

Including a pair at **0.297** that is plainly the same research direction asked
twice by two different explorers: *"Formal equivalence of bounded pricing with
LASSO dual feasibility and existing working-set rules"* (seeded) and *"Column
generation as reinvented safe-screening/working-set LASSO solving"* (blind).

So the semantic duplicate adjudicator — layer 4, the one that exists precisely
for differently-worded duplicates — **has never been consulted and cannot be at
this threshold.** Character-trigram Jaccard over normalised text does not see
semantic duplication.

The threshold is **not changed here.** Choosing it is a judgement about how
aggressively research directions should be merged, and the measurement above is
what a person needs to make it. What did change is that the screen now records
its nearest-neighbour score, so "nothing close" is a measurement rather than an
assertion.

### O.6 Economics, measured

```text
exploration                           $2.48   16 explorer calls -> 66 ideas
cheap ladder (dedup/screen/falsify)  $11.15
sharpening (discover)                 $0.96
deep stages (audit/evidence/review)   $0.00   never reached
```

Killing is cheap and works. See §R for why the last line is zero, which is the
most important unresolved thing in this report.

## P. Second-project dogfood

**Performed.** `ccao-covariance-regressivity` — a research translation of the
Cook County Assessor's LightGBM valuation workflow, studying a covariance
penalty for regressivity. Materially different from the first in every way that
matters: applied econometrics rather than convex optimisation, an empirical
programme at manuscript stage, a capsule with 10 evidence records, 2
experiments, an accepted Claim and a human Review.

28 ideas, 18 rejected, 4 promising. The output is domain-appropriate in a way
that is hard to fake: it engages the project's own Cell A/B/C reference
convention, the frozen penalty grid, the G5a/G5b gate results and `EVI-0008`'s
`INDETERMINATE` verdict, and it raises IAAO ratio statistics (PRD, PRB),
subgroup sign heterogeneity across townships, heteroskedastic label noise, and
a stale centering constant.

Two rejections, both sound. One catches that a proposed curvature audit is
computed on leaf memberships produced by the very training run under test, so it
cannot reach the counterfactual partition. The other catches that for
homogeneous-in-y estimators the proposed rank statistic is algebraically
guaranteed to reproduce each period's own ordering, so the test is near-1 by
construction regardless of real instability.

The documented command order was corrected before this project was set up (§V.9)
and the corrected order worked first time.

## Q. Unattended soak

**Performed: 3 hours 58 minutes of continuous unattended operation on the final code**, one
`researchd`, two projects, 217 ticks, 173 model calls, $15.34.

Nothing was touched during it except two documented commands
(`portfolio pause` on the second project, and a `portfolio.yaml`
`candidate_pool_floor` change) recorded in the soak log with the reason.

### Q.1 It met a real outage, and that is the best evidence here

Partway through, the shared account hit its session limit:

```text
claude did not answer scientific_discovery: You've hit your session limit
```

This was not injected. What the runtime did:

```text
classified          provider_unavailable, not a scientific verdict
breaker             opened; 13 consecutive failures recorded
critical work       CriticalCapabilityUnavailableError -- "Refusing rather than
                    answering critical work with a weaker model"
scheduling          work rescheduled against cooldown_until, not a fixed backoff
ideas               29 REJECTED and 8 PROMISING, unchanged throughout
dispositions        none written
process             the daemon stayed up the whole time
recovery            automatic: healthy=t, consecutive_failures=0, and a
                    `discover` action ACTIVE again within minutes of the reset
```

`docs/RUNTIME.md` §17 is the contract written after the 2026-09-19 incident, in
which an OAuth failure was reported to a researcher as three finished research
runs. **It held, unattended, against a real multi-hour outage.** No idea was
rejected because the provider was unavailable.

### Q.2 No reservation leaked

```text
SETTLED   156    $7.80
RELEASED   25    $1.25
HELD        0    $0.00
```

Those 25 releases are the §V.2 fix working in production. Before it, each would
have been a permanent `HELD` row against the project ceiling.

### Q.3 Two daemons, one lock

A second `researchd` was started by accident against the same database. It found
the advisory lock held, printed what was happening, exited zero, and left the
running one untouched — the property `deploy/researchd.service` documents,
confirmed against a real race rather than a test.

### Q.4 What the soak did not reach

At one work item per pass (`Daemon._claim_and_run` claims `limit=1` and runs it
inline), throughput is one model call at a time — roughly one stage per minute.
`max_active_tracks: 8` bounds *allocation*, not *execution*, so "eight tracks in
flight" means eight tracks queued and run serially. `WorkQueue.claim` takes a
`limit` and uses `for update skip locked` precisely so several workers can run
concurrently; the daemon does not use it.

That is why §O.6's last line is zero, and §R is about what it means.

## R. Scientific-quality audit

Performed against 66 real ideas, 47 real rejections, and every rejection reason
on the first project read in full.

### R.1 The judgement is good

Of the rejections assessed, **every one was scientifically sound** — no idea was
killed for a bad reason. The objections are the kind a competent referee raises:
identification failures, vacuous tests, unstated premises doing load-bearing
work, misattributed theory. On two unrelated domains.

### R.2 The system kills where a researcher would revise

The most important qualitative finding. At least three of the seven rejections
assessed on the first project are objections to the **stated falsifier**, not to
the **research question**: *"the test can only confirm what correctness already
requires"*, *"no control variable is included"*, *"treats a hoped-for result
from elsewhere as a load-bearing premise"*. A researcher reading those would fix
the test, not abandon the question.

The falsify stage has exactly two outcomes — `REJECT` if any objection is
`FATAL`, `CONTINUE` otherwise. `REVISE` exists in the disposition vocabulary and
is reachable only from `discover`, which `stages.select_stage` runs *after*
falsify. **The stage whose job is "make the idea precise enough to be settled"
runs only for ideas the falsifier has already spared.**

This is stated as a finding and **not fixed here**. Whether a weak falsifier
should kill a good question or send it to be sharpened is a scientific policy
decision about what this machine is for, it requires a prompt change that stales
every review bound to the current version, and it is the researcher's to make.

The mitigation that already exists is real and was observed: the rejected
content is not lost, the failure-mining explorer reads it, and §O.3 is that
working.

### R.3 One prompt's schema is measurably harder than the rest

```text
role                     calls   failures
falsifier                   63          4     6.3%
failure_mining_explorer     23          9    39.1%   (mostly during the outage)
scientific_discovery        14          4    28.6%   (all during the outage)
novelty_screener            59          0     0%
blind_explorer               8          0     0%
seeded_explorer              2          0     0%
```

Excluding the outage window, the falsifier is the only role that fails on its
own: `error_max_structured_output_retries`, the model unable to satisfy
`FalsifierOutput` after the CLI's internal retries. It is the most complex
schema of the six exercised. The failures are recoverable — the queue retries
and the second attempt succeeds — so this costs money and latency, not
correctness.

### R.4 The novelty screen does not discriminate

Eight of nine ideas on the first project were assigned novelty exactly `0.70`;
one got `0.30`. A screen whose output is very nearly a constant is not
separating anything, and its own detail says why: *"nothing was retrieved, so
this rests on model recall."* At $0.02 and 7 seconds it is cheap enough that
this is a calibration note rather than a defect, and the architecture is already
explicit that this number decides what to spend on next and never what an idea
*is*.

### R.5 And the thing the audit cannot say

**No idea reached `VALIDATED`. No review board has ever met a real model.**

Six of the eleven stages — `adjudicate`, `literature_audit`, `evidence`,
`review_board`, `meta_review`, `replicate` — have still never executed against a
real provider, on either project, in just under four hours and 173 calls. The literature
index was given the researcher's own 1,128 indexed works specifically so the
route to `VALIDATED` would be exercisable, and the throughput in §Q.4 meant it
was never reached.

So the entire review, validation and replication apparatus remains exactly where
the previous revision of this report left it: implemented, integration-tested
against a scripted provider, and unproven against a real one. That is the part
whose failure would be least visible and most consequential — a review board
that produces plausible, well-formed, empty verdicts satisfies every gate in
§H — and it is the reason §U says what it says.

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

**Prompt quality is measured for six of fourteen, and unmeasured for eight.**
The two explorers, the failure-mining explorer, the novelty screener, the
falsifier and the scientific-discovery role have now met a real model 173
times; §O.2 and §R.1 say what came back and it is good. The literature scout,
the three board reviewers, the meta-reviewer, the replicator, the duplicate
adjudicator and the brancher have not been called once. The original sentence
here — that a prompt producing plausible but useless output would pass every
test in this branch — is unchanged for those eight, and they include every
reviewer.

**Two of the worst defects in this build were found by running it, not by
testing it.** §N.7 and §N.8 — no way to start a portfolio, and an explorer
bought every cadence forever — were both invisible to 324 passing tests, and
both took under a minute to find once the commands were typed in the order a
researcher would type them. That is a statement about what remains: the parts
of this system nobody has *used* are the parts most likely to be wrong, and
the largest of those is everything downstream of a real model call.

**That prediction was right, and the count is now ten.** §V records eight more
defects found the same way, three of them release-blocking, all invisible to
4,308 passing tests. The largest — twelve of fourteen roles unroutable — was
literally "everything downstream of a real model call". The same prediction
still stands over `adjudicate`, `literature_audit`, `evidence`, `review_board`,
`meta_review` and `replicate`, which have still never executed against a real
provider. There is no reason to think those six are in better shape than the
six that had not been used before 2026-09-21.

**One work item at a time is the throughput ceiling.**
`Daemon._claim_and_run` claims with `limit=1` and runs the item inline, so the
control plane executes one model call at a time — about one stage a minute.
`max_active_tracks` bounds allocation, not execution, so "eight tracks in
flight" means eight queued and run serially. `WorkQueue.claim` was built for
concurrency (`for update skip locked`, a `limit` parameter) and the daemon does
not use it. This is why just under four hours and 173 calls did not reach the expensive
half of one project's ladder. A design and deployment question, not a defect,
and not addressed here.

**The falsifier's kill/revise boundary is unexamined policy.** §R.2. The system
rejects research questions whose *stated test* is weak, and the stage that
would fix the test runs only for ideas the falsifier has already spared. Every
rejection audited was defensible; several would have been revisions in a
researcher's hands.

**The dogfood ran on the researcher's own provider credential.** The isolated
context at `~/.config/research-os/claude` reports `loggedIn: false`, and a real
call through it returns *Not logged in*. The soak therefore shared a session
limit with interactive sessions and hit it (§Q.1). The runtime survived that
well; the contention is still a deployment defect, and
`deploy/researchd.service` documents three ways to remove it.

**Losing the operational database loses uncurated ideas.** Bounded by curating
after every tick that changed the bank, reported as a number by
`researchctl portfolio status`, and stated in ADR-0002 rather than hidden.

**The similarity threshold is no longer a guess; it is a measured mistake.**
§O.5: 46 real pairs across two projects, maximum observed 0.377 against a
threshold of 0.72, and a known-duplicate pair at 0.297. The semantic
adjudicator has never been consulted and cannot be at this value. The number is
left unchanged because choosing it is a scientific judgement about how
aggressively directions should be merged — but it must not be left at 0.72 by
inertia, and the screen now records its nearest-neighbour score so the next
person has data rather than a sentence.

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
being cosmetic. The 3.5-hour soak in §Q used one cluster for its whole life and
did not exercise this. `docs/AUTONOMOUS_DISCOVERY_DOGFOOD.md` §6 has the check and
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
pushed        the pre-dogfood commits; the dogfood fixes are not pushed
merged        no
rc/thesis-pilot  unchanged
```

The gates at the final commit:

```text
full suite            4335 passed, 8 skipped, exit 0   (forward)
full suite            4335 passed, 8 skipped, exit 0   (--reverse)
ruff check .          all checks passed
ruff format --check   370 files already formatted
migrations            24 applied on a fresh cluster, head 0024
```

Against the 4,308-test figure this branch carried before the dogfood, that is
27 new tests in six files. **One existing test was changed**, and it is
recorded rather than buried: `tests/test_auto_providers.py`'s `envelope()`
fixture built a usage block with a single input field, which is the assumption
the parser it tests was also making (§V.7). Correcting the parser required
correcting the fixture. No other existing test was modified to accommodate
anything.

Every regression test added here was run against the unfixed code and observed
to fail. That is stated because a regression test nobody has seen fail proves
nothing, and this report's §H already records a gate suite that passed for
twenty-four tests while the property it was written for was unreachable.

Nothing was pushed, nothing was merged, and no existing history was rewritten.
The researcher's capsule at
`/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection`
is still clean at `ad4d66c`; the second project is still clean at `8733ad7`;
neither was written to, and both dogfoods ran against `--no-hardlinks` clones
with `origin` removed.

## U. Formal verdict

Against the brief's own vocabulary:

```text
AUTONOMOUS_DISCOVERY_BETA
```

**Unchanged from the previous revision, and for a different reason.** That
verdict was chosen because the dogfood, the soak and the scientific-quality
audit had not been performed. All three have now been performed. The verdict
does not move, because of what they found.

Against the criteria one at a time:

```text
engineering        met.  4,335 tests forward and reverse, migrations green,
                   real provider failure survived unattended, zero reservation
                   leaks across 181 reservations, real-state contamination zero
safety/authority   met, and now dogfood-proven rather than asserted. Across
                   four hours the Curator wrote 0 paths under .research/, the
                   bank stayed an orphan branch, both projects' own branches
                   were untouched, no proposal was promoted, no Review was
                   authored, and a revision that claimed to answer five
                   objections did not resolve one of them
bank               met, dogfood-proven against two real repositories
portfolio          NOT met.  six of eleven stages -- adjudicate, literature
                   audit, evidence, review board, meta-review, replicate --
                   have never executed against a real provider. No idea has
                   reached VALIDATED. The half of the ladder that decides
                   whether an idea is any good is exactly as unproven as it
                   was before this exercise
scientific work    partially met.  Ideas were generated, screened, falsified,
                   killed and sharpened by a real model on two materially
                   different real projects, and §O and §R say the output is
                   good. Nothing was reviewed or validated
```

Two of five unmet. That is a beta.

### Why not RELEASE_CANDIDATE

§43 of the mission requires a dogfood, a soak and a scientific-quality audit
before a release candidate. Performing them is the precondition, not the
criterion; what they found is the criterion.

They found that the twelve of fourteen agent roles this layer added **could not
be called at all** against a real provider — the whole pipeline below idea
generation was dead code in production, under a green 4,308-test suite. They
found that a terminally failed stage wedged its idea permanently and
unrecoverably, so the first real defect would have ended any soak with no way
back short of a database edit. They found a budget leak that would have
reported the eventual stop as exhausted money that was never spent.

All three are fixed and held by tests that were watched failing. But a release
candidate is a claim that the remaining unknowns are small, and the largest
unknown is unchanged: **no review board in this system has ever met a real
model.** §V is nine defects' worth of evidence for what happens the first time
one does.

### Why not FULL_AUTONOMOUS_RESEARCH_OS_READY

By the project's own written policy, not by judgement. `ROADMAP.md` states that
cross-provider-family review "is required before any claim of independent
scientific review, and therefore before FULL", and that the Slurm path "must
not be described as production-ready until the live harness has run against a
real cluster". This host has one provider family and no scheduler. Every review
board it can produce is one model three times, which the system correctly
refuses to call independent.

### What changed, honestly

The machine is materially better than it was. Its cheap half — generate, screen,
falsify, sharpen — now works against a real model, on two unrelated real
projects, and produces science a researcher would recognise: it caught a
vacuous falsifier, a comparison-class error the project's own charter warns
about, an identification failure, and a misattributed theorem. It survived a
real multi-hour provider outage unattended without converting it into a
scientific verdict, and recovered by itself. Its authority boundaries held
against a real repository for just under four hours.

And the one-line summary of the previous revision needs only one word changed:

**the machine is built, its safety properties are tested and now observed, and
whether it does good science is half-known — the half that generates and kills
does, and the half that validates has still never been asked.**

## V. What running it found, and what closing it found

Eight defects, every one of them invisible to the 4,308-test suite that was
green when this branch was declared beta. Each is recorded with the commit that
fixed it, a regression test, and — because a regression test nobody has seen
fail proves nothing — confirmation that the test fails against the unfixed
code.

### V.1 Twelve of the fourteen portfolio roles could not be called at all

`ModelRouter._ADAPTER_ROLE` maps a `ModelRole` onto the five roles the v1
adapters take. The discovery portfolio added thirteen roles to `ModelRole` and
**twelve of them were never added to that table**, which is a bare subscript.
Every portfolio model call except the two explorers — the novelty screen, the
falsifier, the discovery pass, the literature scout, all three board reviewers,
the replicator, the meta-reviewer, the duplicate adjudicator and the brancher —
raised `KeyError` inside the router.

The consequence is not a degradation. The layer could generate ideas and then
do *nothing* to them: the entire scientific pipeline below generation was
unreachable against a real provider.

Why 4,308 tests missed it: `tests/runtime_graph_helpers.ScriptedRouter` replaces
`ModelRouter` wholesale in all 244 portfolio tests. It keys on the runtime role
string and never consults `_ADAPTER_ROLE`. **The seam between a role existing
and a role being routable had no test on either side of it.**

Closed by completing the table, and held open by two tests rather than one:
`test_every_model_role_has_an_adapter_role` parses the enum, and
`test_a_portfolio_role_routes_through_the_real_router` drives each of the twelve
through `ModelRouter` itself, because an exhaustive table naming buckets no
adapter serves would pass the first and fail in production.

### V.2 A routing failure leaked its budget reservation, permanently

`complete()` reserves the call budget and the cost budget, guards both under one
`except BaseException` that releases them, and then — one line *below* the guard
— called `_model_and_effort`, which is what subscripts the table above. So every
V.1 failure left both grants `HELD` with nothing able to release them.

Observed on the dogfood: eight `HELD` rows, `$0.40` of a `$15` project ceiling
gone, on a run that had spent `$0.34`. Left running, the portfolio would have
stopped as `PAUSED_BUDGET_EXHAUSTED` for money it never spent — the wrong
diagnosis, which is the same mistake §N.8 records.

The docstring on that guard already records this exact bug being fixed once
before, for the two reservations themselves. It is the same bug: anything that
can raise between the reservation and the invocation belongs under the release.

### V.3 A terminally failed stage wedged its idea forever, unrecoverably

The worst of the eight, and the one that would have ended a soak.

`work_items.dedup_key` is a permanent unique index and `enqueue` is
`on conflict (dedup_key) do nothing`. The portfolio's advance key was
`portfolio_advance_idea:{idea}:{stage}` — so the first terminal failure of a
pair spent that key for good. The allocator kept choosing the stage, the queue
kept silently refusing it, and `TickReport` counted only what it *created*, so
a pass that decided on eight things and bought none of them was indistinguishable
from a pass with nothing to do.

An hour of real ticks did exactly that, reporting `RUNNING` throughout. And the
failure mode is worse than a stall: **fixing V.1 did not recover it.** The keys
were still spent. A running deployment would have needed a database edit.

Three things close it.

- The key names the failure generation, so a retry is a different key.
- `Bounds.max_stage_failures` bounds the retries, because a counter in a key
  with nothing bounding it is invariant 14's forbidden loop wearing a new name.
  Past the ceiling the idea is `BLOCKED_EXTERNAL` — *not* `REJECTED` or
  `PARKED`, because those would write an infrastructure failure down as a
  decision about the science, which is the rule §4.2 already applies to an
  exhausted budget.
- `TickReport.work_refused` and a note, so deciding-and-not-doing is legible.

### V.3a And the first fix for V.3 was wrong, which the soak caught

The generation counter first counted `FAILED` rows in `idea_actions`. Two things
were wrong with that, and the second was found by the running system rather than
by review.

**A failure before the action row exists leaves no row.** `advance_idea` reads
the idea, the version and the snapshot, selects the stage and opens a run before
it opens the action row. A terminal failure in that window would not move the
count, the key would stay spent, and the wedge would be back in exactly the
shape the fix exists to prevent.

**And a retry that succeeded still counted.** During the soak a falsifier call
returned `error_max_structured_output_retries`; the queue retried the same work
item on its own backoff and attempt two succeeded — the R5 machinery working as
designed. But attempt one had already written a `FAILED` action row, so the
generation advanced and the tick bought a *second* work item for a stage that
had just succeeded.

Counting terminally failed **work items** fixes both: a work item always exists
by the time it can fail, and an item that succeeds on its second attempt is not
a failed item. `test_a_stage_that_succeeded_on_its_second_attempt_is_not_bought_again`
is the regression, and it was written from the soak's own rows.

### V.4 `PAUSED_BLOCKED_EXTERNAL` could not fire

The same shape as the dead `PAUSED_NO_FRONTIER` branch §N.9 removed, and it
survived that fix. The condition required `not allocations`, and a portfolio
with a free slot and a pool under its ceiling always allocates an explorer — so
the status was reachable in the enum, in the CLI, and in §4.3, and not in the
code.

It matters beyond a tidy enum: exploring past this spends $0.60 a cadence
producing ideas that meet the same blocked stage, and reports the eventual stop
as `PAUSED_BUDGET_EXHAUSTED`. The condition now asks what §4.3 says it asks —
every *allocatable idea* is blocked — and ignores exploration.

V.4 was reachable only because V.3's ceiling is the first thing in this layer
that produces `BLOCKED_EXTERNAL`. Closing one defect made the next one live.

### V.5 `portfolio status` called a failing portfolio healthy

Nine ideas, no tracks in flight, every `portfolio_advance_idea` item failed —
and the command a researcher of this layer would actually type printed:

```text
portfolio  cg-sparse-regression  RUNNING
tracks     0 in flight   (a HUMAN_READY idea holds no slot)
  candidate      9
```

The failures were visible in `researchctl runtime status`, which is a different
layer's operational view. `portfolio status` now reports failed portfolio work
and blocked ideas, for the same reason it already reports "not scheduled": a
silence there reads as health.

### V.6 `seed add` answered with a foreign-key violation

On a project that had been registered but had no portfolio,
`researchctl seed add` printed

```text
insert or update on table "portfolio_state" violates foreign key constraint
"portfolio_state_project_id_fkey"
```

in a command set where every other failure explains itself. Every test in
`test_portfolio_cli.py` uses a fixture that calls `upsert_project`, so the path
a researcher takes after `register-project` had never been walked.

It now names `portfolio enable`, and deliberately does not *do* it: enabling
commits the machine to spending against a budget and recording a direction does
not.

### V.7 `tokens_in` recorded 4 for a call that consumed ~32,000

The Claude adapter read `usage["input_tokens"]` alone. A real envelope splits
input across three fields, and the other two are the cached prefix. The first
call of the dogfood recorded `tokens_in = 4` against a call that returned
13,854 output tokens.

Cost was never affected — it comes from the CLI's own `total_cost_usd`, which
prices cached tokens correctly — so this is a provenance defect, not a budget
one. `docs/RUNTIME.md` §11 says that column carries the tokens a call used.

This is the one place an **existing test was changed**, and it is worth saying
plainly: `envelope()` built a usage block with one input field, so the fixture
and the parser shared the same wrong assumption. The fixture now looks like a
real envelope.

### V.8 A non-database exception was reported as a database error

`Database.tx()` wraps the whole `with` body, so an exception raised by *caller*
code inside it reaches `classify_db_error`. Reclassifying it is deliberate — it
must not be retried as though the server had blinked — but rendering it as
`str(exc)` alone erased the class, and a `KeyError`'s `str()` is the bare key.
V.1 therefore reached the operator as

```text
RuntimeDatabaseError: <ModelRole.NOVELTY_SCREENER: 'novelty_screener'>
```

a database error naming an enum member, with `KeyError` nowhere on the screen.

### V.9 The dogfood procedure's own command order does not work

`docs/AUTONOMOUS_DISCOVERY_DOGFOOD.md` §4 listed `register-project`,
`runtime budget`, `seed add`, `portfolio enable`. Three of those four fail in
that order: only `portfolio enable` creates the operational `projects` row that
the other two need. A document written so that running the dogfood would be "a
decision and twenty minutes rather than a project" failed on its second command.
Corrected, and run in the corrected order for the second project.
