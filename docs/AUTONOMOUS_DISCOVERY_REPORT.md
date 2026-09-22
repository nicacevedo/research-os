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

### Q.4 What the soak did not reach, and why -- corrected

**This section said throughput, and throughput was not the cause.** The
correction is kept rather than rewritten away, because the wrong explanation
is instructive: it was plausible, it was partly true, and it would have sent
the next person to optimise the wrong thing.

What was true: at one work item per pass (`Daemon._claim_and_run` claims
`limit=1` and runs it inline) the control plane executes one model call at a
time, so `max_active_tracks: 8` bounds *allocation* and not *execution*.
`WorkQueue.claim` takes a `limit` and uses `for update skip locked` precisely
so several workers can run concurrently; the daemon does not use it. That is
real, and it is slow.

What was actually stopping the deep stages was a wedge. **The dedup key was
blind to the idea version.** After a revision the cheap ladder is owed again
-- `succeeded_stages_for_version` is version-scoped, so the stage machine
correctly asks for dedup, the screen and the falsifier against content that
is now different -- and those keys had been spent by the previous version's
runs, which had SUCCEEDED. `on conflict do nothing` refused them forever.
Every idea that reached `PROMISING` and was then sharpened was wedged
permanently at the bottom of its own re-run ladder, and being sharpened is
what happens to every idea that survives.

The evidence was in the tick reports the whole time and nothing was reading
it: eight allocations, zero enqueued, pass after pass. It became visible the
moment `work_refused` existed to print it.

§W records the fix and the two further defects that fixing it exposed.


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
full suite            4349 passed, 8 skipped, exit 0   (forward)
full suite            4349 passed, 8 skipped, exit 0   (--reverse)
ruff check .          all checks passed
ruff format --check   370 files already formatted
migrations            25 applied on a fresh cluster, head 0025
```

The figures before the second pass were 4,335 and head 0024. One caveat is
worth recording about how a green run was nearly mis-read: an earlier gate
run reported

```text
===== Research OS real-state contamination =====
7 entries appeared in the researcher's real Research OS directories
  .config/research-os/claude/sessions/972.json
  .config/research-os/claude/backups/.claude.json.backup....
```

with 4,347 tests passing. Nothing was contaminated by the tests. The dogfood
daemon was running *alongside* the suite, and the isolated provider home the
deployment uses sits at `~/.config/research-os/claude` -- inside one of the
four roots `tests/conftest.py` inventories. The guard was right to report new
entries; the location is the problem, and `deploy/researchd.service` now says
so where it recommends the path. The gate figures above are from a run with
the daemon stopped.

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

## U. Formal verdict (as of §V's revision; superseded by §Z)

> This section is the verdict recorded at an earlier revision and is kept
> because this document is a record rather than a summary. **§Z is the
> current one.** Both say `AUTONOMOUS_DISCOVERY_BETA`; they say it for
> different reasons, and the difference is the point of reading both.

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
portfolio          NOT met, and §W changes why.  Three of those six now run:
                   adjudicate, the literature audit (4 and 5 retrieved
                   sources against a floor of 3) and the evidence stage,
                   which correctly refuses. The review board, the
                   meta-review and replication have still never executed --
                   and §W.5 establishes that they *cannot* on real ideas
                   from these projects, because all five adjudicated ideas
                   need measurement and the experiment pipeline is unwired.
                   No idea has reached VALIDATED. What changed is that this
                   is now a known architectural gap rather than an unknown
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

### What the second pass changed

The verdict is unchanged for the third time, and the reason has moved again.

The first revision said beta because the dogfood, soak and audit had not
happened. The second said beta because they had, and found the layer's cheap
half working and its expensive half unproven. This one says beta because the
expensive half is now *understood*: it is unreachable for real science on this
build, and §W.5 says exactly why -- five of five adjudicated ideas need
something run, and nothing here can run anything.

That is worth more than another green line. "We do not know whether the review
board works" and "the review board cannot be reached, because real falsifiers
demand execution and the experiment pipeline is unwired" are different
statements, and only the second tells anyone what to build next.

Eight of fourteen roles have now met a real model, and the three defects §W.3
records were each found by fixing the one before it -- a version-blind dedup
key, an edge kind that violated its own schema, and a retrieval capability
connected to nothing. All three were invisible to a suite that was green, and
all three sat behind seams no test crossed because every test injects its own
double. That is the same lesson as §V and it has now repeated twice.

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

## W. The second pass: the two delegated findings, and what closing them found

§R.2 and §O.5 were left for a person because both were scientific judgements.
Both were delegated back. Closing them exposed three more defects, each
uncovered by fixing the one before it, and then a finding that changes what
this layer needs next more than any of them.

### W.1 The duplicate threshold, recalibrated on measurement

0.72 → 0.25. Before changing it, the prior question: can character-trigram
Jaccard separate duplicates at all? A cut point on a noisy ranking is
worthless. It can. Across **1,081 real pairs from two real projects** the top
of the distribution is dominated by genuine duplicates -- this portfolio asked
*"is the pricing rule just safe screening in disguise?"* four separate times
and *"is instance-size independence an artifact?"* twice, screening and
falsifying each as a new idea.

```text
p50 0.160   p90 0.231   p95 0.253   p99 0.315   max 0.381
lowest pair confirmed by reading as the same direction: 0.297
```

0.25 is p95. Lowering it costs less than it appears: the screen makes **one**
adjudicator call carrying up to six neighbours, not one per pair. The real
constraint is input quality -- two neighbours is a focused question, six is
noise.

**Result, measured:** the duplicate adjudicator went from *never having been
consulted in the system's existence* to 26 calls, all well-formed, and six
ideas are now `SUPERSEDED` rather than occupying tracks.

Two limits recorded rather than smoothed over. It is calibrated on two
projects and trigram overlap depends on the writing style of the model that
produced the text. And it does not catch everything: a *short* paraphrase of
the same question measures 0.239 and still passes. Character overlap cannot
see meaning and no cut point on it will.

### W.2 The falsifier no longer kills a question because its test is broken

Severity was the only axis, so "this idea is wrong" and "this idea's test is
wrong" were one decision. Objections now carry `target: CLAIM | TEST`
(migration 0025). The model reports a *fact about its own objection*; ordinary
Python routes on it, the way the adjudication type is read out of the
falsifier rather than chosen by it. `CLAIM` is the default, so silence still
kills.

Three bounds, and the third is the one that matters: every fatal objection
must be about the test, the revision ceiling applies, and **the gate did not
move** -- a fatal test objection still blocks promotion until a different role
agrees a later version answered it. Not killing an idea is not the same as
letting it through.

**Result, measured over 390 real objections:**

```text
CLAIM  61 FATAL   68 CRITICAL   138 MAJOR   54 MINOR
TEST    6 FATAL   21 CRITICAL    30 MAJOR   12 MINOR
```

The model is not using `TEST` to spare everything: it is 18% of objections and
9% of fatal ones. The `TEST` ones are the right kind -- *"the falsifier gives
no account of how many folds/seeds are needed"*, *"the design doesn't specify
which correction model is under scrutiny; it would trivially pass this check
by construction"*.

Writing the controls found a hazard worth its own test. With only half the
change -- test objections excluded from the track-ending guard, nothing
routing them to sharpening -- such an objection becomes *invisible* and the
idea proceeds to the expensive stages, which is worse than either the old
behaviour or the new. A partial revert produced exactly that.

### W.3 Three defects, each found by fixing the last

**The dedup key was blind to the idea version**, so every sharpened idea was
wedged. See §Q.4, which this corrects.

**Recording an adjudicated duplicate crashed.** `is_lineage` is
`kind not in ('CONTRADICTS','DUPLICATE_OF')`, so `MERGED_FROM` is a lineage
edge and `idea_edges_acyclic_ck` demands `child_depth > parent_depth`. Dedup
compares *siblings*, both at depth 0, so every merge violated it. §7 says a
semantic duplicate gets a `DUPLICATE_OF` edge and §4.1 agrees; the code
disagreed with both and was the only writer of `MERGED_FROM` anywhere.
Unreachable until W.1 made it reachable, then it failed on the first merge.

**And the shared literature index was wired to nothing.** The portfolio takes
retrieval by injection so a corpus-less deployment reports a missing
capability instead of crashing. Nothing was injected: `run_advance_idea`
passed `literature=None`, every deep audit failed `capability_denied`, and
since `novelty_or_literature` is the only route that can execute on this
build, **`VALIDATED` was unreachable in production.** The same shape as the
twelve unroutable roles -- a capability the system has, connected to nothing,
behind a seam no test crossed because every test injects its own double.

The first test for that wiring asserted `_literature()` returns a source,
which passes whether or not anything *calls* it -- which is how a capability
ends up wired to nothing. It now spies on the handler.

### W.4 Three more stages proven, and three that cannot be

```text
adjudicate         5 runs, no model call, reads the falsifier
literature_audit   3 runs; 5 and 4 distinct retrieved sources against a
                   floor of 3, with real source keys
evidence           3 runs, every one correctly capability_denied
review_board       never run
meta_review        never run
replicate          never run
```

Roles that have now met a real model: **8 of 14**, up from 6. New this pass:
`duplicate_adjudicator` (26 calls) and `literature_scout` (3).

The literature audit's success also answers the dogfood procedure's fourth
question, which had been open: `novelty_min_sources: 3` does **not** stop
everything. Retrieval reaches 4 and 5 distinct sources on real questions.

### W.5 And the finding that matters more than any defect here

**Every adjudicated idea is empirical. All five, across both projects. Not one
is literature-only.**

```text
{empirical}                        3
{empirical, mathematical}          1
{empirical, novelty_or_literature} 1
```

This was checked for a classifier defect and is not one. The falsifiers
genuinely demand execution:

```text
"run CG on a fine grid of λ/λ_max ... Record rounds/time(λ/λ_max)"
"compute within-cell Cov(e,y) with a bootstrap 95% CI (>=1000 resamples),
 Benjamini-Hochberg FDR at q=0.05"
"Execute the four-arm comparison ... recording wall-clock time and peak memory"
```

`runtime.adjudication.classify` is reading those correctly. Real research
questions, written by competent explorers against real projects, need things
run.

`select_stage` puts `evidence` before `review_board`, and the board is reached
only once `_evidence_sufficient` holds. For an empirical idea on a host that
cannot execute, that never becomes true. So the review board, the meta-review
and the replication stage are **not reachable on real ideas from these
projects** -- not because of a defect, and not for want of budget.

§S said the literature route "is the weakest of the four". The measurement
says something stronger and more actionable: **it is the route real falsifiers
almost never imply.** Five of five needed execution. On this evidence the only
wired route is close to a null path for genuine research, which makes wiring
the experiment pipeline the gating item for this layer producing validated
science -- not an enhancement.

They could have been reached by contriving a literature-only idea. That is
manufacturing coverage, it was explicitly forbidden, and it would have proved
nothing except that the code runs.

### W.6 What the system got right under all of this

Worth recording, because the list above is all defects.

The evidence stage, meeting an idea it cannot settle, said:

> this idea is settled by measurement and nothing on this host can execute
> anything. It stops here rather than being concluded from reasoning about
> what the measurement would have shown.

That is the single most important safety property in the layer -- §S's "an
idea that cannot be settled here must not be validated on prose" -- observed
declining in production rather than asserted in a docstring.

The literature failure before it was equally honest: *"no literature source is
configured, so novelty cannot be established. This idea will not reach
VALIDATED, which is the correct outcome rather than a failure of the idea."*
An infrastructure gap that refuses to read as a scientific verdict.

And across 285 reservations: 259 settled, 26 released, **0 held**.

### W.7 The final soak, and both terminal pauses observed

A last unattended run at the shipped breadth, to budget exhaustion. Both of
the portfolio's terminal pause states fired, and both are correct:

```text
cg-sparse-regression    PAUSED_BLOCKED_EXTERNAL
  every one of 3 live idea(s) is waiting on something outside this machine

ccao-covariance-regressivity  PAUSED_BUDGET_EXHAUSTED
  project budget for model_cost_usd is spent. Raise it with
  `researchctl runtime budget --max-cost-usd` and the next tick resumes;
  nothing here converts that into a scientific rejection.
```

`PAUSED_BLOCKED_EXTERNAL` is the branch §V.4 records as having been
unreachable -- the same dead-condition shape as `PAUSED_NO_FRONTIER` before
it. It has now fired in production, for precisely the right reason: all three
live ideas are empirical, all three are blocked on an execution capability
this host does not have, and the portfolio said so instead of spending.

The budget pause overshot its ceiling by $0.12 on $15.00, which is the
documented behaviour and not a leak: §K states the overshoot is bounded by one
stage's own `max_cost_usd` rather than hidden.

**Raising the budget would not buy the three unproven stages.** They are
gated on evidence sufficiency, which is gated on execution, which is not a
money problem. That is why this run was allowed to end at the ceiling rather
than asking for more.

### W.8 Final figures

```text
ideas               83   60 rejected, 9 superseded, 5 promising,
                         3 investigating, 6 candidate
model calls        298   $27.76 across two projects
roles exercised   8/14   + duplicate_adjudicator, literature_scout
stages exercised  7/11   + adjudicate, literature_audit, evidence
reservations       280 settled, 26 released, 0 held
capsules          both unchanged; registry and shared index untouched
```

Nine ideas are `SUPERSEDED` rather than occupying tracks, which is the
recalibrated duplicate screen and the corrected edge kind working together;
before this pass that number was one.

---

## X. The empirical execution path

§W.5 named the gating item: **every adjudicated idea across both real
projects is `empirical`**, the evidence stage refused all of them, and the
review board, the meta-review and replication were therefore unreachable on
real ideas. This section is what closing that took, what it reused, and what
running it found.

### X.0 The audit that came first

Before any abstraction, what already exists. Written down because the answers
decided what *not* to build.

**What experiment objects exist.** Three layers, and they do not overlap.
`docs/CAPSULE.md`'s `Experiment` is a scientific object a person wrote --
purpose, hypotheses, predictions made before the result. `research_os.
experiment.models` has `ExperimentRun` (one execution: argv, commit,
executor, artifacts by hash) and `EvidencePacket`, which is explicit that it
is a *candidate* and carries no verdict. `research_os.runtime` has
`ExecutionSpec` (frozen, digested), `external_jobs` (the operational record)
and `experiment_interpretations` (a durable claim binding one job to one
reader version, added by `sql/0006` and `0010` because association by
"whichever job finished last" was not traceability).

**How executable commands are declared.** `~/.config/research-os/
experiments.yaml`, per project, by name, with typed parameters. Its location
is the security property: it is outside every worktree, so a write-enabled
worker cannot add a command or widen one. `experiment/spec.py` substitutes
by whole token, validates every value against the declared type, and refuses
rather than escapes.

**How preregistrations are represented.** An artifact, stored under a role
carrying the spec digest, looked up by equality. `actions/experiments.py`
rebuilds the spec from it and compares hashes before submitting.

**How the executors work.** `runtime/executors.LocalExecutor` freezes the
spec, creates an immutable run directory with a manifest, runs the argv
under `sandbox.contain()` and returns a finished handle. `SlurmExecutor`
wraps the v1 `sbatch`/`squeue`/`sacct` adapter, submits without blocking and
lets the control plane reconcile; it honours `required` containment *by
refusing*, because nothing here can contain a process on a compute node.

**How artifacts are captured.** `runtime/artifacts.FilesystemArtifactStore`:
content-addressed, so an artifact's id is the hash of its bytes and
immutability is structural rather than enforced.

**How containment works.** `sandbox.py` over bubblewrap, with `SandboxSpec`
declaring writable, readable, protected and discarded paths; `.git` and
`.research` are bound read-only *inside* a writable checkout because an
adversarial review planted a `post-checkout` hook through the gap.

**How failures are retried and reconciled.** `runtime/failures.py` maps a
class to a response and has no member for a refuted hypothesis; the queue
schedules against it; `runtime/idempotency.py` makes a side effect happen at
most once and hands an abandoned one to a reconciler.

**How results become evidence today.** They do not, and that is deliberate
at both layers below this one: v1 produces a candidate packet for a person,
and R5 produces an interpretation that records the criteria and the exit
status and explicitly no verdict. The portfolio's gates need something
neither produces -- an evidence *row*, bound to an idea version, with a
strength a gate can read -- and producing it from arithmetic rather than
from prose is what §X.2 is about.

**The conclusion of the audit:** nothing needed replacing, one thing was
missing (which idea version asked for which measurement), and one executor
had a gap the coding pipeline had already closed for itself.

### X.1 What was built

One module and one table. Everything else already existed and is *used*
rather than reimplemented:

```text
new
  research_os/portfolio/empirical.py        the bridge, 2071 lines
  sql/0026_idea_experiments.sql             one table
  sql/0027_experiment_prompt_version.sql    design liveness, head 0027
  contracts.ExperimentDesign                + DecisionRule, DecisionPredicate
  prompts.EXPERIMENT_DESIGNER               reusing ModelRole.EXPERIMENTALIST
  prompts.REPLICATION_DESIGNER              reusing ModelRole.REPLICATOR
  models.ExperimentRole/State/Conclusion    + IdeaExperiment
  tests/test_portfolio_empirical.py         50 tests, real subprocesses

reused, unchanged
  experiments.yaml, experiment/spec.py      the declared commands
  runtime/executors.LocalExecutor           running one, contained
  sandbox.py, automation/checks.py          the containment and uv's needs
  automation/worktree.py                    the disposable workspace
  runtime/idempotency.py                    running it exactly once
  runtime/budgets.py                        reserve, settle, release
  runtime/artifacts.py                      immutable outputs and analyses
  runtime/store.external_jobs               the execution record
  actions/coding.canonical_fingerprint      "the checkout did not change"
  runtime/failures.py                       the taxonomy, not extended

changed
  runtime/executors.LocalExecutor           uv's interpreters, cache overlay
                                            and the linked-worktree repo, all
                                            of which the coding pipeline
                                            already solved and this executor
                                            did not
  portfolio/track.advance_idea              `repo_path` was accepted and then
                                            `del`-ed; executors and the two
                                            ledgers are now supplied
  portfolio/extensions.run_advance_idea     `can_execute=False` was hard-coded
  portfolio/commands._resume                also unblocks what it resumes
  portfolio/gates._replication_met          an INCONCLUSIVE second
                                            measurement verifies nothing;
                                            unreachable before this route
```

No dependency was added and nothing left `ARCHITECTURE.md` §12's postponed
list. `DESIGN_INVARIANTS.md` carries the change-control record and
`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` §19 the specification.

### X.2 The two properties that decide whether this is science

**The conclusion is arithmetic.** The design fixes one metric in one file and
two thresholds on it, *before* the run exists; ordinary Python reads the
number afterwards and compares. No model is ever asked what an output means.
Two predicates rather than one, so "neither held" is expressible and a
success condition that covers every value reports `INCONCLUSIVE` instead of
support.

**An execution that did not happen is never evidence.**
`EmpiricalConclusion.OPERATIONALLY_BLOCKED` exists for it and
`EVIDENCE_STRENGTH_FOR_CONCLUSION` has no entry for it, so a crashed
executor, an unreachable provider and a host that cannot contain each leave
the idea's rows untouched. Both properties were mutated to check the tests
see them: making a crashed run write evidence turns a `SystemExit(7)` into
`SUPPORTS`, and three tests fail.

### X.3 What was found by writing the tests

Four defects, each found by a test that failed the first time it ran.

**The workspace path did not match the experiment.** `design` reserved an id,
derived the disposable worktree from it and put that path in the
specification digest -- and then `create_experiment` minted a *different* id.
Every submission failed with "the workspace was created at ..., not at the
path this experiment already recorded". Found by the first test that ran an
experiment end to end.

**A new worktree branch read as an escape.** The canonical fingerprint was
taken before `ensure_workspace`, so the branch that worktree isolation *is*
appeared between the two readings and `after != before` called it drift. Two
fixes, and both were needed: fingerprint after the workspace exists, and
compare with `escaped()` rather than `!=`, because a reserved ref appearing
-- the Curator's first bank commit during a measurement -- is permitted and
inequality cannot express that.

**A released workspace could never be re-run.** `release_worktree` keeps the
branch, `create_worktree` refuses to reuse one, so an experiment whose
workspace had been released was permanently unable to have another. Invariant
6 of the brief -- a failed experiment stays recoverable -- fails on a branch
name.

**Logs were written into the thing being thrown away.** `LocalExecutor` puts
`logs/stdout.txt` under the `run_dir` it is given, and the first version
passed the *workspace*. So every run's output was destroyed with the
worktree, and a directory the experiment never declared was written into the
tree being measured. The run directory is now the runtime's own, under the
data home, where the manifest already lives.

### X.4 What was found by running it against a real provider

The three real empirical ideas of `cg-sparse-regression` were advanced
through `researchd`, against the real provider, on a clone of the real
project with the researcher's own `experiments.yaml`.

**Two contract bounds turned a good answer into a retry loop.** Asked to
design for an idea no declared command can test, the designer answered
`testable: false` -- which is the most useful answer available -- with a
2,400-character account of why, and the contract discarded the whole
response for being 400 characters over a limit nothing had told it about.
The retry produced the same answer and failed identically. Then, one idea
later, the same shape again: the designer wrote a *note* in `resources`
(`machine: "a 2026 laptop; the thesis's hardware and Gurobi 12 are not..."`)
and a 128-character value bound threw away an otherwise valid design.

This is the third time this codebase has paid for the same mistake -- a
constraint the model is graded on and never shown -- and
`_parameter_contract` already carries the note about the first. The fix is
three parts: explanations are clipped rather than refused (nothing reads them
as evidence); `resources` keeps only the six keys an executor actually turns
into a directive and drops the rest (the others reach nothing, and carrying a
paragraph would put a model's commentary inside the specification digest);
and both limits are now in the prompt.

### X.5 The real trace

Three real empirical ideas, advanced through `researchd` against the real
provider, on the clone of `cg-sparse-regression` with the researcher's own
`experiments.yaml`. The route ran in full and what it produced is below,
unedited in substance.

**Two ideas were refused, carefully, and that is the right answer.** The
designer read every declared command and said why none of them tests the
idea. In full, for `PIDEA-...-7c68ed14`:

> The falsifier needs, at fixed (n,p,k,SNR): several correlation structures
> (independent; AR(1) at rho in {0.3,0.6,0.9}; block at 2-3 settings), a
> common fine lambda/lambda_max grid on each, rounds/time and achieved s at
> every grid point, and the derived cross-design statistic R. No declared
> command takes a parameter that sets correlation structure, (n,p,k,SNR), or
> a lambda grid.
>
> `profile` has no parameters [...] With a single design there is no
> across-design ratio to form, so neither fold-change is defined.
>
> `adjudicate-pricing` exposes only out and seed_offset [...] I have no
> declared key in its output carrying rounds(lambda/lambda_max). Naming one
> would be a guess, and a guessed metric_path fails the design.
>
> `benchmark` is the only command that could carry this [...] authoring [a
> plan] would be writing the experiment myself, which this format forbids.

No evidence row, no experiment row, `capability_denied`, and the idea
untouched. That is the boundary working: **the empirical route's reach is
exactly the surface the researcher declared**, and this project's three
commands were written for the researcher's own hypotheses rather than for
the questions the portfolio asks.

**One idea produced a real experiment, and it ran.**
`PEXP-20260921T231202Z-492c3191` over the declared `benchmark` command, with
this preregistered decision rule:

```json
{"metric_path": "summary.memory_gated_min_speedup",
 "output_path": "results/2026/modern_conic_vs_decomposition.json",
 "success": {"comparator": ">=", "threshold": 1.2},
 "failure": {"comparator": "<=", "threshold": 1.0},
 "metric_description": "Worst-case (over instance sizes) ratio of median
   decomposition wall-clock to median modern-conic-solver wall-clock, with
   0.0 substituted at any size where the modern solver's peak RSS exceeds
   1.25x the decomposition's."}
```

That is a good rule: one number, two thresholds, a stated gap between them,
and a memory gate folded into the metric rather than argued about
afterwards. It was fixed before anything ran.

The execution went the whole way. A disposable worktree at the project's
HEAD; `uv run --frozen` contained under bubblewrap with the network denied
and **fifty-two packages installed from the throwaway cache overlay**; the
frozen manifest and both logs in the run directory under the data home; the
canonical checkout fingerprinted before and after. Then:

```text
FileNotFoundError: [Errno 2] No such file or directory:
  'plans/2026/modern_conic_vs_decomposition.yaml'
```

The designer had invented a plan path. The bridge recorded
`OPERATIONALLY_FAILED / executor_failed`, wrote **no evidence**, left the
idea's rows untouched, and kept the experiment recoverable. Exactly the
required behaviour, observed rather than asserted: *the executor failing is
not the idea being wrong.*

### X.6 The third instance of one mistake

The invented path is the same defect as the two contract bounds, and as the
absolute path `_parameter_contract` records from the thesis pilot: **a
constraint or an input the model is graded on and never shown.** The
catalogue described each command's parameters and named no file in the
repository, so a designer asked for a plan path had nothing to choose from
and wrote a plausible one. The refusal quoted above says so in words --
"No plan encoding this sweep is known to exist in the tree" -- about a tree
it had never been shown.

`empirical.input_candidates` now lists the checkout's tracked data and
configuration files, capsule excluded, bounded at sixty, and says plainly
that a path outside that list is an output. `experiments/EXP-0001-plan.json`
-- the real frozen plan, which exists and which the designer could not see --
is in it.

**Measured after, not only reasoned about.** The same idea, the same
provider, one design call with the listing in the prompt and nothing else
changed:

```text
before   plan = plans/2026/modern_conic_vs_decomposition.yaml   (invented)
after    plan = experiments/EXP-0001-plan.json                  (the real one)
```

Same command, same shape of rule, a path that exists. $0.27.

### X.6a And then the fix could not reach the idea it was for

The improved designer had nothing to design: `PEXP-...-492c3191` was already
on the row, holding the invented path, and every retry resubmits a
preregistered specification rather than making a new one -- correctly, since
re-designing after seeing a result is how a post-hoc change gets made.

But no result existed. The command never ran. And the design had been made by
a prompt this build had superseded, which the portfolio already has a rule
for: `idea_reviews.prompt_version` is part of liveness because "a review
produced by a prompt that has since been superseded is a review of a question
no longer being asked". An experiment is the same object under a different
name and had no such column.

Migration `0027` adds `idea_experiments.prompt_version` and makes the
identity index partial, so a stale design is retired to `SUPERSEDED` -- kept,
as the record of what was designed and why it stopped being asked for -- and
its successor can take the name. An `INTERPRETED` experiment is never stale:
what was measured was measured.

**That is the fourth defect of one shape on this branch.** The version-blind
dedup key, the permanently-unique work item key, the released worktree's
surviving branch, and this. Each wedged something forever; each was invisible
until the thing in front of it was fixed; and none was found by a test.

### X.6b One more, found by looking at the researcher's repository afterwards

A failed experiment left its worktree *and its branch* in the project, because
the workspace was released only on the path that succeeded. Three failed
experiments in a soak would leave three of each, permanently, in a repository
this layer promises to leave byte-identical.

The diagnosis was never in the worktree -- the argument vector, the frozen
manifest, the exit status and both logs are in the run directory under the
data home -- so the failure path now collects whatever the run did write, by
content hash, and then releases the workspace like every other path. The
byte-identical claim is unconditional, and the test asserts it after a
*failed* measurement as well as a successful one.

### X.7 What this does and does not establish

Established, against a real provider on a real project:

```text
design over a declared command                        yes
a machine-checkable rule fixed before the result      yes
preregistration stored, digested, re-hashed at submit yes
contained execution in a disposable worktree          yes, 52 packages offline
logs, manifest and exit status captured outside it    yes
an execution failure recorded as operational          yes, and no evidence
an idea no declared command can test, refused         yes, twice, with reasons
canonical capsule and every ref unchanged             yes
working tree clean afterwards                         yes
```

With one exception, which is §X.6b: the failed run left its worktree and its
branch behind, and they were removed by hand after the pass. That is fixed
and tested, and it is stated here rather than folded into the row above,
because "byte-identical" was the claim and it was not true of that pass.

Not established against a real provider, and the reason is the same each
time -- the declared surface does not yet carry a command these ideas can
use:

```text
a successful measurement of a real idea               no
evidence from one reaching the review board           no
meta-review and replication on empirical evidence     no
```

Those three *are* established end to end in
`tests/test_portfolio_empirical.py`, through `advance_idea` with the real
stage machine, the real gates and a real subprocess -- an idea goes
`dedup → novelty_screen → falsify → discover → adjudicate →
literature_audit → evidence(SUPPORTS) → review_board → meta_review →
replicate → review_board → meta_review → HUMAN_READY` with the measurement
taken by a real program. What is scripted there is the provider, not the
machinery.

So the honest claim is: **the empirical path is closed and exercised, and
has not yet carried a real idea to a real conclusion.**

### X.8 What stands between here and one, precisely

Two things, and neither is code in this repository.

**A declared command whose parameters span a question the portfolio asks.**
The refusal quoted in §X.5 spells out what that would be for one idea: a
committed plan that fixes (n,p,k,SNR), enumerates the correlation structures
and sweeps a common lambda grid. The three commands this project declares
were written for the researcher's own hypotheses, and the boundary that keeps
a model from writing one is the boundary that limits the route's reach. That
is the trade, and it is the right one.

**A declared command whose output a decision rule can read.** `benchmark`
writes JSON *Lines* -- one object per cell -- and its summarising step,
`scripts/analyse_benchmark.py`, is not declared. A rule over `benchmark`'s
output therefore cannot be evaluated whatever the designer names it, and the
deterministic analyser would report `INSUFFICIENT`: honest, and not a
refutation. The catalogue tells the designer each command's parameters and,
now, which input files exist; it cannot tell it what a command *writes*,
because nothing declares that.

Both are the researcher's to close and neither was closed here, because
declaring an experiment command for someone's project is exactly the act
`experiments.yaml` lives outside every worktree to prevent an agent from
performing.

**The third thing is money, and it is the reason the pass stopped where it
did.** The dogfood project's ceiling is $15.00, set by an earlier session; it
stood at $14.46 when this pass ended. Raising it spends the researcher's own
credit and is theirs to decide.

### X.9 Figures

```text
model calls          307 total, 29.58 usd across two projects (+9 calls,
                     +1.82 usd this pass, all `experimentalist`)
roles exercised      9/14   + experimentalist
stages exercised     7/11   evidence now runs rather than refusing
experiments          1 designed, preregistered, executed, and operationally
                     failed; 2 ideas correctly refused as untestable
executions           1 contained run, 52 packages installed offline, 0 network
capsules             both unchanged
repository           capsule, refs and working tree unchanged; the one
                     leftover branch was §X.6b, removed by hand, now fixed
reservations         settled or released, none held
```

### X.10 Verdict

```text
AUTONOMOUS_DISCOVERY_BETA
```

Unchanged, and for a narrower reason than last time. §W.5's gating item is
closed: the evidence stage no longer refuses the only kind of idea this
portfolio generates, and the path from an empirical idea version to a
version-bound evidence row, a review board, a meta-review and an independent
replication runs end to end -- through the production entry point, with the
real stage machine, the real gates and a real subprocess taking the
measurement.

What holds the verdict is the sentence the brief set as the bar: *a real
empirical idea has traversed the experiment/evidence/review path.* One has
traversed design, preregistration and contained execution, and stopped at an
operational failure. It has not reached evidence, and no idea has reached a
review board on an experiment.

The two things standing in the way are declarations in the researcher's
`experiments.yaml` (§X.8) and a budget ceiling that is theirs to raise. None
of the three is a defect in this layer, and none is an agent's to decide.

---

## Y. The overnight run

Starting point: `75b8e8f`, the empirical bridge, with the human-owned
experiment catalogue corrected to expose four commands rather than three --
`benchmark`, `analyse-benchmark`, `adjudicate-pricing`, `profile`.

The purpose was to finish the real empirical traversal §X.7 could not, and
to push the layer as far toward release as the science and the machinery
justify. Six defects were found on the production path, none of them
reachable from a suite that was green at 4,402 tests. Four were found in the
first twenty minutes.

### Y.1 Twenty-one defects: the first ten, which this author found

**An explicit project cost ceiling was silently raised.** The operator set
`--max-cost-usd 50.00`; a *reconciler-rescheduled* objective cycle -- one
nobody started -- raised it to 300.00, being `25 x 12` from the shipped
configuration. `cycles.apply_default_budgets` raises a project ceiling and
never lowers it, which is right and exists because deriving it from one
objective's cap once bricked a project. What it left open is the other
direction, and §13 puts unbounded budget changes among the acts a human
performs. `budgets.explicit` (`0028`) tells a number a person typed from one
the runtime derived; the first is left alone, and an objective that needs
more stops on `BUDGET_EXHAUSTED`.

**`portfolio resume` could not undo what it said it undid.** The
stage-failure ceiling counts failed work items and never decays, so `resume`
returned three ideas to IDLE and the next tick re-blocked them on the same
historical count. Their evidence stage had failed three times *while the
experiment route did not exist*, so the route arriving could never reach
them. The fix is a watermark (`0029`) and not a reset, and that distinction
is load-bearing: `work_items.dedup_key` is built from the failure count
against a permanently unique index, so resetting it would make the retry
re-use a spent key and `enqueue`'s `on conflict do nothing` would refuse it
silently -- the wedge the count exists to close.

**The designer refused every command because none declared what it writes.**
Its own words, refusing an idea: *"its output schema is not declared
(declared outputs: none), so any metric_path I named inside the file I
create would be a guess rather than a preregistration."* The catalogue now
reads the declared output the project has *committed* from an earlier run
and lists its numeric paths -- keys and types only, never values, because a
threshold chosen to fit a result that already exists is a rule fixed after
the fact wearing the clothes of one fixed before.

**Every portfolio stage failure reached the queue as `unknown`.**
Twenty-seven of them. `_as_error` raised a bare `ResearchOSError` for
everything that was not a provider failure, and `Daemon._classify` falls
through to `UNKNOWN`. Three losses at once: `portfolio status` could not say
why anything failed, the retry policy could not tell an outage from a policy
answer, and the allocator could not tell a stable refusal from a transient.

**So the allocator bought the same refusal three times.** `max_stage_
failures` is a ceiling on *retries*, right for a transient and wrong for an
answer: "no declared command can test this idea" does not become true on the
third attempt, and each attempt is a paid frontier call. Six identical
refusals were bought across two sessions. One refusal now blocks, using the
classes the taxonomy already calls terminal, and `portfolio resume`
reconsiders.

**The falsifier reached for CLAIM when its objection named the test.** See
§Y.3; it is the one change here driven by the scientific-quality audit
rather than by the machinery, and **the one with no regression test**. That
is deliberate and worth stating rather than hiding: no deterministic test
asserts that a model picks the better of two labels, and writing one that
pinned a fixed response would test the fixture. What *is* tested is the
mechanism the fix depends on -- `test_a_prompt_version_bump_stales_the_board
_and_the_track_recovers` -- because bumping to `falsifier@3` is what stops a
verdict produced under one-sided guidance from counting as current. The
evidence for the change itself is the measured rate: 425 real objections,
79% CLAIM / 21% TEST, and 63 CLAIM against 10 TEST among the fatal ones.
Nine of the ten defects have a regression test that fails on the broken
implementation; this one has a measurement and says so.

**A stage's account of itself was cut mid-word at 500 characters.** Found by
trying to obey Phase 1 -- "record precisely what human-owned capability is
missing" -- and discovering that the record does not contain it. Every
`detail` in the database was exactly 500 long at the maximum, because four
call sites had each chosen a literal, the smallest won, the column is
`text`, and the contract that produces the string permits four thousand. So
the three refusals that are the entire return on the overnight run reach the
researcher as `(master_solve_seconds, pr` and `which choose among this
project's curr`. The bound now lives once, at the store boundary, is the
producing contract's own number, and appends `[clipped]` when it bites --
because a reader must be able to tell a reason that ended from a reason that
was cut. The run row stays terse on purpose and says so: it is operational
telemetry, and widening it is R5's call.

**And the bank rendered those refusals outside their own bullet.** A stage
that refuses writes numbered prose with blank lines between the reasons;
`render_idea` interpolated it into `- {detail}` unindented, and in Markdown
a blank line ends a list item. On the real page at `42cb8426` the history
stops at the first refusal, its reasons float free as body text, and the
next action starts a second list. Two spaces per continuation line. Both of
these are the same shape as the four in §X.6: *a constraint nobody chose,
applied where nobody would look for it*.

**The cheap screen scored novelty it had not read.** Measured over 108 real
screens: 74 ran with an empty retrieval packet. The stage said so in its
own detail -- *"nothing was retrieved, so this rests on model recollection
only"* -- and then wrote exactly the `0.7` it would have written with eight
works in hand. The allocator reads the dimension, not the prose, so the
caveat was recorded everywhere except where it would have had an effect.
It now assesses nothing when it read nothing, which is what `merged`
already required in as many words: *a stage that assesses nothing must
change nothing*. `select_stage` gates the screen on the action having
succeeded rather than on the dimension, so leaving it unset cannot loop,
and the deep audit -- which a gate reads and which requires retrieved
sources -- is unaffected.

**And `ideas show` would not say an idea was blocked.** Five ideas sat at
`BLOCKED_EXTERNAL` overnight; the command for reading one idea in full
printed `next: evidence` for one of them that had already failed that stage
five times and twice explained at length why it could not be done. Read
literally, it announced work that was about to start. Two lines now: the
operational state when it is not IDLE, and the most recent failed attempt
with its class, its count and its detail -- not filtered to the stage
`next` names, because the two differ exactly when the machine gave up
before reaching the stage it would choose. The aggregate was never missing;
`portfolio status` has counted blocked ideas all along. It was missing from
the view someone opens to ask about one idea.

Of these ten, **four were found by auditing rather than by running**: the
two above, the CLAIM/TEST calibration, and the truncation -- which was
found by trying to obey Phase 1 and discovering the record did not contain
what Phase 1 asked to be recorded.

Five more follow, and this author found none of them. §Y.13 has the four
an independent architecture review turned up in a single read-only pass --
every one of them a place where the permanent record stated something
untrue -- and the fifteenth, the reporting half of that review's second
finding, is in `650ec5e`: the bank pages printed how much executed
evidence an idea had and never which way it pointed.

**That is the number that should decide the verdict.** A reviewer given
the branch for one afternoon found four record-integrity defects in code
that had 4,402 green tests and had already been audited twice this
session. The defect surface is not exhausted, and nothing here establishes
where its edge is.

### Y.2 Phase 1, answered: the missing capability is human-owned

> **Read §Y.12 first.** The catalogue this run loaded declares three
> commands, not the four the brief describes: the correction landed in the
> researcher's real config and the dogfood reads its own. Everything below
> was produced against three. §Y.12 also shows, from the fourth command's
> own declaration, that having it would not have changed the answer.

No declared command can settle any of the three real empirical questions,
and the refusals are detailed, stable, and reproduced across two sessions
and three prompt versions. They are correct. The clearest is the one that
reasoned *against* the new output-schema listing rather than being helped by
it:

> Its committed numeric paths confirm this: `cases.0.first_master_ms`,
> `cases.0.last_master_ms`, `cases.0.master_solve_seconds` all describe a
> single solve path. The nearest proxy, first_master_ms/last_master_ms, is
> not S: the first master solve is cold only because it is simultaneously
> the smallest (fewest generated columns/cones), so that ratio mixes
> warm-start amortization with column-set growth -- and the growth direction
> is precisely the open uncertainty the design was meant to resolve. **It
> would answer a different question under the same name.**

That is a better judgement than the one this session's author made when
guessing that `profile` could answer that idea.

The chain the corrected catalogue was clearly meant to enable is
`benchmark -> analyse-benchmark`. It cannot close, and the reason is one
sentence: **`scripts/analyse_benchmark.py` prints its verdict and writes no
file.** At `ad4d66c` it has no `write_text`, no `json.dump` and no `--out`;
`benchmark` itself writes JSON *Lines*, which is not a document a metric
path addresses. A preregistered decision rule names one number in one JSON
document, and no declared command in this project produces one for the
questions the portfolio is asking.

That is one entry on the list, and the shortest. The full list came from
reading all six refusals of the night in full, which was only possible after
fixing the defect in §Y.1 that was discarding 83% of each of them. Every
item is the researcher's, and every item is named by a refusal rather than
inferred by this author:

```text
1. analyse_benchmark.py takes --out and writes its verdict as JSON, and
   analyse-benchmark declares that path in `outputs`
2. `benchmark` and `adjudicate-pricing` declare output schemas at all --
   both declare none, so no metric path can be preregistered against
   either, which four of the six refusals raise independently
3. a tracked benchmark plan enumerating correlation structures x a
   lambda/lambda_max grid -- or `correlation_structure`, `rho` and
   `lambda_ratio` as parameters on a command
4. achieved cardinality s in some output schema; no command reports
   support size, and two hypotheses are defined by it
5. a warm-start on/off toggle, or any solver-configuration parameter
6. a parameter selecting a hardware or solver generation; `resources`
   carries scheduler keys only
7. peak memory recorded anywhere at all
```

Items 3 through 7 are not flags. They are measurements this project has not
built, and no amount of prompting produces them. Declaring an experiment
command for someone's project is the act `experiments.yaml` lives outside
every worktree to prevent an agent from performing, and none was performed.

Three of the refusals go further and order their own remediation by least
change -- *"(a) a benchmark plan checked into the tree that enumerates the
correlation structures and a lambda/lambda_max grid, plus a declared output
schema exposing rounds, time, s and lambda/lambda_max per cell ... or (b)
correlation_structure, rho, and lambda_ratio parameters on
adjudicate-pricing"* -- and then say why neither is theirs to do: *"Either
is a change to experiments.yaml or to the tracked plan set, not something I
can supply as a parameter value."*

Items 6 and 7 come from the third idea, the wall-clock/memory ablation,
which was refused twice after the catalogue was corrected -- on grounds no
`--out` flag would answer:

> (1) No handle on hardware. Arms (a) and (b) are defined by running on
> original-era hardware, and arm (b) vs (c) IS the software-vs-hardware
> separation the idea exists to perform. No declared command accepts a
> hardware target, a machine generation, or a solver version.

> 1. PEAK MEMORY IS NOT MEASURED ANYWHERE. The falsifier turns on memory as
> much as on time: the claim fails "if it is faster only at the cost of
> materially higher peak memory." The only command with a declared output
> schema is `profile`, and its committed numeric paths are purely a timing
> decomposition.

The system is not confused about what it lacks. It is unusually precise
about it, and in six attempts it never once proposed to measure something
adjacent and call it the answer. Each refusal ends by saying so in its own
words, and these are the sentences that matter most in this document:

> Specifying `profile` and reading a first/last ratio would spend a run on a
> number the falsifier does not reference, which is why I am declining
> rather than substituting it.

> Running it and calling it a test of this idea would manufacture a verdict
> the measurement does not support, so I am declining to name a command
> instead.

> That control is explicitly labelled secondary in the proposal and cannot
> decide the portability claim, so running it and calling it a test of this
> hypothesis would misreport what was measured.

An autonomous system that will not spend a real budget to produce a number
it knows cannot answer the question is the property this architecture exists
to have. It is worth more than a traversal would have been, and it is the
one result of the overnight run that could not have been obtained by writing
a test.

### Y.3 The falsifier's CLAIM/TEST calibration, and the measurement behind it

Fourteen rejections read in full. Twelve well judged, and several better
than well judged: one refuted a proposal algebraically from
`Cov(e,y) = [Var(y)+Var(e)-Var(yhat)]/2`, showing the falsifier's own
trigger condition self-contradictory; one caught that the cited
bootstrap-inconsistency theory (Bickel & Freedman 1981; Athreya 1987) needs
infinite variance while the proposal's own assumption implies finite; one
refused a false dichotomy with a LARS counterexample; one showed a
motivating example already subsumed by Xu, Caramanis & Mannor 2010.

Two were objections to the *test*, recorded as fatal to the *claim*:

```text
"The falsifier's causal attribution is algebraically backwards..."   FATAL/CLAIM
"...The falsifier as specified produces a pattern consistent with
 either explanation"                                                FATAL/CLAIM
```

The second is an identification failure, which is the falsifier prompt's own
canonical `TEST` example. So the guidance was not missing and was not
unread: it was **unbalanced**. It warned against over-using `TEST` and never
against over-using `CLAIM`, and never said the two mistakes cost different
things. A wrong `CLAIM` at `FATAL` ends a question permanently; a wrong
`TEST` costs one bounded sharpening cycle with the objection still standing.
`falsifier@3` names the asymmetry.

Measured rather than anecdotal: across **425 real objections** from two
projects the split is 79% `CLAIM` / 21% `TEST`, and among fatal ones 63
`CLAIM` against 10 `TEST`.

One thing checked and found correct rather than fixed: no rejected idea
carries a `revisit_if`, and none should. The schema requires it on `PARKED`
and a rejection is revived as a *new* idea with a `REVIVES` edge, so the
record of what was rejected stays what it was.

### Y.4 The scientific-quality audit: 12 rejections and 11 survivors, read in full

(§Y.3 is the one policy change this audit produced; this is the audit.)

109 ideas across two projects, none VALIDATED, none HUMAN_READY, so the
audit is of what the portfolio *killed* and what it is still carrying. The
funnel first, because it says where the quality comes from:

```text
dedup            109 CONTINUE   25 DUPLICATE      $1.88
novelty_screen   108 CONTINUE    0 rejections     $3.00
falsify           41 CONTINUE   63 REJECT        $17.06
adjudicate         6 CONTINUE                     $0.00
```

**The falsifier does all of the killing**, at 61% of what it sees and 71% of
what the portfolio spends. The novelty screen rejects nothing by design --
"a screen that could demote an idea would be a novelty judgement made
without a retrieved source" -- and dedup removes one idea in five.

Twelve rejections sampled at random from 63 and read in full. All twelve are
defensible. Ten are strong. The two best are not the kind of thing a
keyword rule produces:

> The cited bootstrap-inconsistency theory (Bickel & Freedman 1981; Athreya
> 1987) requires the statistic's asymptotic distribution to be a
> non-Gaussian stable limit, i.e. infinite variance ... The proposal's own
> stated assumption (upper tail regular enough for EVT/GPD modeling) is
> fully compatible with finite variance (shape parameter < 0.5), a regime in
> which the ordinary case-resampling bootstrap ... is consistent.

> Solving Sherman-Morrison independently within each leaf's index set
> discards the cross-leaf terms induced by the shared rank-one vector u
> (since u generally has nonzero mass in multiple leaves) ... the method
> computes a different, still-approximate quantity while advertising it as
> the ground truth.

The first catches a citation used for a condition it does not establish.
The second catches an error in the precise step the proposal offered as its
contribution. Four more are identification failures stated exactly: a
ρ-sweep where correlation is a common cause of both rival mechanisms; a
design whose outcome is "analytically forced before any code runs"; a
contrast that "conflat[es] solver-generation/implementation quality with
formulation choice". One rejects a proposal for smuggling a normative
premise into a technical frame -- that between-neighborhood reallocation is
"laundering" -- and says the property-tax literature treats it as the
central litigated harm.

Two reservations, recorded because an audit that finds nothing is not an
audit:

1. `dd0e8118` was killed as pre-answered by Blitz/Celer, and the killer
   itself says the objection is *"FATAL only insofar as the thesis's pricing
   step is a standard Lasso-type dual"* -- a condition nothing then checked.
   The kill is probably right and is not *shown* to be right.
2. `4bee1ae8` was killed for insignificance rather than invalidity: its
   `why_it_matters` was blank and its question explicitly excluded the only
   consequence anyone had named. Defensible, and it is the one rejection in
   the sample that a person might reverse.

Neither is a threshold to tune. One is a missing verification step inside an
objection, one is a judgement call at the boundary, and §9 of the brief is
explicit that policy changes follow repeated measured failure modes rather
than single unattractive examples. Twelve of twelve defensible is not a
repeated failure mode.

The eleven live ideas were read the same way. What they show:

- **Pre-registered three-way decision rules, with a named null.** *"R > 3 →
  supported ... R < 1/3 → falsified ... 1/3 ≤ R ≤ 3 → both shift comparably;
  treat as a null result."* The third bucket is what stops a binary from
  manufacturing a verdict, and it was written unprompted.
- **Scope limits the idea imposes on itself.** *"Neither outcome by itself
  licenses a verdict on whether historical CG-vs-Celer/Blitz timing
  comparisons were fair, since no analogous single-factor toggle exists."*
- **Method that the falsifier did not ask for**: HAC-corrected inference,
  unit-root tests, Benjamini-Hochberg at q=0.05, bootstrap CIs at ≥1000
  resamples, sales-weighted materiality bars tied to IAAO conventions.
- **Revision that is actually responsive.** One idea is on version 5. Its
  `closest_prior_work` cites *its own two prior rejected versions* -- "n=3
  correlation; then the df/circularity fix without an autocorrelation
  control" -- and then cites Yule 1926 and Granger & Newbold 1974 for the
  control it added in response.

And the single clearest piece of evidence that the portfolio discriminates:
`7c527766` was **rejected** for conflating solver generation with
formulation choice, while `e9e1551d`, which asks a neighbouring question
with an explicit four-arm software-versus-hardware ablation, **survived**.
The same confound, caught in one design and controlled in the other, with
opposite outcomes.

### Y.5 Portfolio diversity, and the one structural thing it is missing

Three generators are actually running, not one: 69 ideas from the blind
explorer, 32 from failure mining, 8 from researcher seeds. 23 ideas carry
more than one version and the revision loop reaches version 5. Subject
matter is genuinely spread -- on `ccao` alone: leaf-level Hessian curvature,
stratified covariance under pooled nulling, tail materiality of a weighted
objective, feature-attribution equifinality, and autocorrelated moment
drift. These are not one idea in five costumes.

The missing structure is branching. **`max_depth` is 0 and
`count(distinct lineage_root)` equals the idea count in both projects: in
109 ideas, no idea has ever had a child.** This is not a defect in the
brancher, and it needs no change. `STAGE_MINIMUM_STATUS[BRANCH]` is
PROMISING, and `select_stage` reaches BRANCH only after EVIDENCE,
LITERATURE_AUDIT, REVIEW_BOARD, META_REVIEW and REPLICATE have been
considered. Every PROMISING idea in this portfolio stops at EVIDENCE,
because of §Y.2. So the portfolio is broad and completely flat, and it will
stay flat for exactly as long as the empirical route stays closed.

That is worth stating plainly as a release consideration: the depth of this
portfolio has never been exercised on real ideas, by anything. It is
covered by tests, and it has no production-path evidence whatsoever.

### Y.6 The operator surface, audited by using it

Every command below was run against the live dogfood, not a fixture.

What it gets right, and these are not small things:

- **Every idea listing is headed** *"Autonomous discovery candidates
  (PIDEA-...). These are not your capsule's Idea objects (IDEA-0001) and
  none of them is scientific state."* The A0 boundary is restated on the
  surface a person actually reads, not only in a design document.
- **`portfolio status` refuses to let a failure count imply a diagnosis**:
  *"30 portfolio work item(s) have failed; work has succeeded since the last
  of them, so these are history rather than a diagnosis."*
- **It says what losing the database would cost**: *"uncurated ideas are the
  only thing losing the operational database would lose."*
- **`ideas show` and `portfolio top` volunteer the weakness of their own
  evidence**: *"one model reviewed this; that is not independent review."*
- **`runtime doctor` refuses to overstate three separate things**: that
  provider tiers are *"CONFIGURED priority, not measured performance"*; that
  the installed reviewers are *"different models (sonnet reviewing opus)
  from the same family"*; and that bubblewrap is *"measured but not
  proven"* because the adversarial containment suite has not been run
  against this binary on this host. It also declines `systemd-run` on the
  grounds that *"a containment that reports success without containing is
  worse than none."*
- Naming no project when two are registered is an exit-1 refusal with a
  reason, not a guess.

What it got wrong, now fixed (§Y.1, tenth defect): `ideas show` printed
`next: evidence` for five ideas that were `BLOCKED_EXTERNAL`, one of them
after five failed attempts at that same stage, and showed neither the block
nor the refusal. The aggregate was never missing -- `portfolio status` has
counted blocked ideas all along -- but the per-idea view is where a person
goes to ask about one idea, and it read as work about to start.

One thing that looked like a defect and was not: `portfolio status` lists
the same refusal under two failure classes, `capability_denied
StageExecutionError` and `unknown ResearchOSError`. The timestamps settle
it -- `unknown` stops at 00:53:56 and `capability_denied` starts at
01:07:26, which is exactly when `cb794f9` landed. Pre-fix history, not a
live path.

### Y.7 The engineering gates, and what the soak actually was

Migrations were exercised twice, and the second is the stronger evidence.
A fresh database applies all 29 and reports head 0029; running `migrate`
again answers *"the operational schema is up to date"*. But the dogfood
database itself is the real upgrade path, and it was upgraded **under
load, five separate times, onto live scientific and operational state**:

```text
2026-09-21 00:25   0001..0024   (at clone)
2026-09-21 12:30   0025
2026-09-21 19:55   0026
2026-09-21 20:21   0027
2026-09-22 00:31   0028
2026-09-22 00:38   0029
```

`validate-project` returns OK on both project capsules after the entire
run.

**The soak did not reach its target and this is the honest accounting.**
Splitting the work-item record wherever nothing completed for ten minutes
gives five unattended segments:

```text
longest single run     5h 06m   (2026-09-21 00:33 -> 05:38)
second                 3h 01m
total daemon-active   10h 42m   across 5 segments
```

The brief asked for a minimum of eight hours unattended if available. The
longest single stretch was 5h 06m. Two things ended segments: the provider
session limit -- visible in the bank as *"You've hit your session limit ·
resets 5:10am"* -- and, more often, this author stopping the daemon to apply
one of the ten fixes. The second reason is not the machine's failure, but
it is not the eight hours either, and a run in which the code changed ten
times is not the run the brief asked for.

### Y.8 Two structural limits, neither of which is a defect

**Independence is surfaced, not gated.** `_validated_unmet` requires a live
non-negative review from each of the three `INDEPENDENT_REVIEW_ROLES`. It
requires three *roles*. It does not require three *models*.
`board_independence` counts distinct `(provider_family, model)` pairs, and
the gate attaches its result as a **note** -- *"every review of this idea
was produced by one model. That is not independent review, and nothing
rendered from this may call it so"* -- rather than as an unmet requirement.

The code does exactly what §10 of the architecture says it does, the note
reaches `ideas show`, `portfolio top` and `runtime doctor`, and
`gates.py:225` states the intent in as many words. So this is not a defect
and it has not been changed: nothing measured it failing, because no idea
has ever reached VALIDATED. It is recorded because it bounds the meaning of
a word. On this host `runtime doctor` reports
`DEGRADED_SAME_PROVIDER_FAMILY` -- *"no second model family is
installed"* -- so an idea that reached the board here would be validated by
three samples from one model, and the only thing standing between that and
the word "independent" is a note that renderers are trusted to print. It is
a policy question about what VALIDATED should mean, and it belongs to a
person.

**The empirical route is closed by a missing capability, and everything
downstream of it is therefore unexercised on real work.** Not the review
board, not the meta-review, not replication, not branching. All four have
test coverage, including an end-to-end drive that reaches HUMAN_READY; none
has production-path evidence. §Y.2 and §Y.5 give the two chains. They
reduce to the same sentence: nothing real got past EVIDENCE, so nothing real
reached anything after it.

### Y.9 The second project, which is the generality evidence

`ccao-covariance-regressivity` is a different science in a different
repository -- property-assessment regressivity under a covariance
constraint, against a LightGBM pipeline -- with its own charter, its own
capsule (carrying the researcher's own `CLAIM-0001` and `REV-0001`), its own
derived $15.00 ceiling, and its own bank branch. It produced 45 ideas, 27
of them rejected, through the same machinery.

The useful part is not that it worked twice. It is that **it stopped one
stage earlier, for a different reason, and was equally right to**:

```text
cg-sparse-regression    literature audit passes (17 sources, 26 evidence rows)
                        -> reaches EVIDENCE
                        -> refuses: no declared command fits the question

ccao-covariance-...     no literature source configured
                        -> refuses at LITERATURE_AUDIT
                        -> never reaches EVIDENCE at all
```

Its refusal is worth quoting because of its last clause:

> no literature source is configured, so novelty cannot be established.
> This idea will not reach VALIDATED, which is the correct outcome rather
> than a failure.

Two projects, two different missing human-owned capabilities -- one a
declared command, one a configured literature source -- caught at two
different stages, both classed `capability_denied`, both stopping below
VALIDATED, and both naming what is absent rather than proceeding without
it. A system that produced the same refusal twice would have shown that it
can repeat itself. This shows the refusal is computed from what the project
actually has.

It also means the second project is **weaker** evidence than it looks for
anything downstream: it never reached the empirical bridge, so it exercised
the explorer, dedup, novelty screen, falsifier, adjudicator, curator and
budget ledger on a second science, and nothing after that.

### Y.10 What this run established, as figures

Everything below is from the live dogfood database and the two project
repositories, not from a fixture. Figures are as of the end of the run;
the soak was still producing ideas while this section was drafted, so
earlier numbers quoted elsewhere in §Y are smaller and were correct when
written.

```text
ideas                    133      (cg and ccao)
versions                 168      one idea reached v5
actions                  490
reviews                  114      all from the falsifier; the board never ran
evidence rows             31      all literature; 0 numerical, 0 executed
model calls              431
work items              1160      1111 succeeded (95.8%)
spend                 $24.75 settled, $7.55 released, $0.10 held
ceilings               cg $50.00 (explicit, human-set, never raised)
                       ccao $15.00 (derived)
experiments                1      superseded; none ever ran to a conclusion
VALIDATED                  0
HUMAN_READY                0
schema                  0030      upgraded under load six times during the run
capsules                  OK      both validate after the entire run
human branches      unmoved      one reflog entry each, `clone:`
soak               6 segments, longest 5h 06m, 11h 31m total
```

Work-item success rate is 95.8%. Of the failures, 29 carry the
pre-fix `unknown` class, 7 are `provider_unavailable`, 4
`budget_exhausted` and 3 `capability_denied` -- and not one is a
scientific rejection, because the taxonomy has no member for one.

**Nothing in this table is a scientific result.** 105 reviews and 26
evidence rows sound like a body of work; every review is the falsifier
arguing with a proposal, and every evidence row is a retrieved citation.
The columns that would carry a finding -- numerical evidence, executed
evidence, experiments concluded, VALIDATED, HUMAN_READY -- are all zero,
and §Y.2 says why in seven items, none of which an agent may supply.

### Y.11 Recovery, tested by killing it

The daemon was sent `SIGTERM` mid-flight, with work in progress and no
graceful drain. What it left:

```text
1  work item LEASED, lease already expired
3  budget reservations HELD, $0.05 each, oldest ~50 min
0  ACTIVE actions with no live work item
0  experiments in SUBMITTED or RUNNING
0  ideas marked ACTIVE with no active action
```

That list is the point. A hard kill left exactly two kinds of debris, both
of which the runtime already knows how to collect, and **no scientific
state in an inconsistent condition** -- no idea stranded mid-track, no
experiment claiming to be running, no action orphaned from its work item.

On restart the daemon's first reconciliation pass logged:

```text
reclaimed 1 expired lease(s)
```

and earlier in the run, the same reconciler:

```text
released 3 stale budget reservation(s)
```

Across the whole run it released 34 reservations totalling $7.40 against
$22.05 settled -- reservations for work that was claimed, paid for in
advance, and then interrupted. The held balance fell 12 -> 9 -> 6 -> 3
under observation without intervention.

**No SQL was run against scientific state, at any point, for any reason.**
The only direct database writes this session made were reads, plus creating
and dropping one throwaway database (`gate_fresh`) to prove migrations
apply from empty.

### Y.12 The catalogue the run actually used was not the corrected one

Found late, by checking rather than assuming, and it corrects this
document as much as anything else.

The brief states that the human-owned catalogue has been corrected and
validated, and that it exposes `benchmark`, `analyse-benchmark`,
`adjudicate-pricing` and `profile`. **The catalogue the run loaded exposes
three of those four.** There are two files:

```text
~/.config/research-os/experiments.yaml                    4 commands   modified 2026-09-22 00:15
~/.local/state/.../dogfood/xdg/config/experiments.yaml    3 commands   modified 2026-09-21 19:55
```

The dogfood sets `RESEARCH_OS_CONFIG_HOME` to its own directory, which is
the entire point of a dogfood -- it must not read or write the
researcher's real configuration. So the correction landed in the real
catalogue and never reached the run. `declared_commands('cg-sparse-
regression')` returns `['adjudicate-pricing', 'benchmark', 'profile']`,
and that is why not one of the six refusals mentions `analyse-benchmark`:
the designer was never shown it.

**Every refusal in this document was therefore produced against a
three-command catalogue.** They remain correct *for what the designer was
shown*, which is the only thing a refusal can be correct about, but the
premise under which they were read in §Y.2 was wrong and is corrected
here.

Now the part that matters. Reading the missing declaration settles whether
it would have changed anything, and it does not:

```yaml
analyse-benchmark:
  argv: [..., "scripts/analyse_benchmark.py", "{input}"]
  parameters:
    - name: input
      type: path
      required: true
  outputs: []
  checks: []
```

It takes an `input` and **declares no outputs**, no `--out` parameter and
no `outputs_exist` check. That is item 1 of the seven in §Y.2, stated by
the researcher's own declaration rather than inferred from the script:
the command performs *"deterministic post-processing only"* and writes
nothing a preregistered metric path could address. `benchmark` likewise
declares `outputs: []` and writes JSON *Lines*, which is not a document a
metric path addresses.

So the `benchmark -> analyse-benchmark` chain could not have closed on the
corrected catalogue either, and the conclusion of §Y.2 survives its own
premise being wrong. **The catalogue was not re-pointed and nothing was
re-run.** Spending a real budget to reproduce a refusal that the
declaration already proves would be buying an answer twice -- which is
the mistake §Y.1's second defect exists to stop.

Two things for the researcher, both small and both theirs:

```text
1. the dogfood config home has a stale copy of the catalogue; the
   correction of 2026-09-22 00:15 is not in it
2. analyse-benchmark, as declared in the corrected file, still writes
   nothing -- so closing the chain needs the --out and the `outputs:`
   entry, not just the declaration
```

### Y.13 The independent architecture review, and the four things it was right about

An independent reviewer was given the branch, the six authority documents
and read-only access, and asked to break the four hard prohibitions. It
could not: it traced the call graph and found no path to a human `Review`
(`idea_reviews` has no `reviewer_kind` column at all), none to `propose
promote` or `insight promote` (no `subprocess` import in the package;
`automation.gitutil` is argv-only with `core.hooksPath=/dev/null`), none to
a write under `.research/` (one `write_text` in the package, guarded by
`_safe`), and none to the researcher's branch (the bank is rooted on the
empty tree via `commit-tree`).

It then found four things wrong that this author had not, all of them
places where **the record said something that was not so**. All four are
fixed in `9f3bf6c`; §Y.1's ten become fourteen.

**The containment claim was unfalsifiable from the record it points at.**
`DESIGN_INVARIANTS.md` leans on containment, `SandboxMode.PREFERRED` runs
uncontained on a host with no backend, so both cases exist -- and the
permanent analysis artifact recorded `job.detail` under the key
`containment`. For a local run `handle.detail` is the literal string
`"completed"`. Every analysis document in the store therefore says
`"containment": "completed"` and `"wall_clock": "completed"`, and the
monotonic duration `submit` actually measures was computed, put in the
ledger result, and dropped. The comment above it asserted that `job.detail`
*"carries what the submission recorded, which includes the duration"*,
which was false when written. Migration 0030 adds the three columns; NULL
means unrecorded and is deliberately not `False`, because inventing the
more alarming of two answers is still inventing one.

**The autonomy dial's premise does not hold for this layer.**
`build_executors` requires containment only at `autonomy: high`, and its
docstring gives the reason: *"Lower settings use the researcher's
configured mode, because there a person is at the keyboard."* That is true
of an objective cycle, which a person starts. It is false of the portfolio
daemon, which runs unattended by construction. So a researcher who lowered
autonomy *to be more careful* got model-parameterised project commands
running uncontained with the full `os.environ`. The portfolio now asks for
`REQUIRED` at every setting.

**A file the run wrote was reported as a file it did not.** `_collect`
hashes at most 32 declared outputs and skips anything over 256MB;
`analyse` derived *"declared outputs were not produced"* from what came
back. The workspace still exists at that point, so the two facts are now
told apart.

**And the Curator fetched from the network while holding the repository
lock.** `git fetch --all` against a bank branch that is an orphan with no
upstream -- and it moves `refs/remotes/*`, which `canonical_fingerprint`
reads. A curate pass picking up an upstream commit during a coding run
failed that run as an escape: precisely the false positive
`runtime/refs.py` was written to prevent, reintroduced by a line that did
nothing.

It also found two documents claiming more than the code does, both
corrected rather than implemented:

- Invariant 12 said *"portfolio, project and run ceilings through the
  existing ledger"*. `apply_default_budgets` is called by `runtime start`
  and `open_cycle` and by nothing in the portfolio, so a project that has
  only ever run the portfolio -- the case §13a calls intended -- has
  neither, and `budgets.reserve` treats an absent budget as unlimited. The
  idea ($8) and lineage ($40) ceilings are real and always apply.
- `ARCHITECTURE.md` said a `HUMAN_READY` idea *"becomes a `Proposal`, and a
  person promotes it"*. Nothing builds a `Proposal`. The error was in the
  safe direction, but a reader auditing the boundary should not be sent
  looking for an edge that is not there.

**Neither budget behaviour nor the missing edge was changed.** Creating
default ceilings changes what a deployment spends and refuses, and that
belongs to the person whose money it is.

Three findings were reported and deliberately not acted on, and they are
the most important ones for a release decision:

1. **The authority table does not govern this layer.**
   `research_os/portfolio/` contains no reference to `ActionKind`,
   `policy_for` or `authorize`. The objective cycle calls
   `authorize(action, autonomy=...)` and raises `ScientificGateError` on
   A2; the portfolio's fifteen `ActionKind` entries are read by `runtime
   doctor` and by a test, and by nothing on the execution path. Being
   listed in the table is not being governed by it. Wiring it is an
   architecture change and belongs to a person.
2. **The gates are blind to evidence direction.** `_substantive` and
   `_replication_met` count a row as qualifying if its strength is
   SUPPORTS *or* CONTRADICTS, and no gate reads which. An idea whose
   experiment and replication both refuted it can reach HUMAN_READY on
   three model verdicts, and `render_bank` prints the counts without the
   direction. This has never fired, because no idea has reached the board.
3. **The empirical route bypasses `experiment_interpretations`.** `sql/0006`
   and `sql/0010` exist to make "which experiment this interpretation is
   of" durable, with a uniqueness constraint so two workers resolve to one
   interpretation. `empirical.interpret` guards with a read-then-write
   instead. In practice the active-track index serialises it; it is a
   second answer to a question the schema already answered once.

### Y.14 Three fixes confirmed on the production path, after the fact

Not in a test. In the running dogfood, after the daemon was restarted on
the fixed code.

**The truncation.** The first refusal recorded after the fix is **3,174
characters** and is stored whole. Under the old code it would have reached
the researcher as 500 characters ending mid-sentence. It is also the
seventh consecutive refusal of the night and the most precise yet: it
observes that `profile` exposes exactly the right variables
(`cases.0.iterations`, `cases.0.n`, `cases.0.p`) and cannot be crossed with
anything because it has no parameters; that `benchmark` has the design
space but it lives in a plan file no agent may author; and that
`adjudicate-pricing` varies only the replicate dimension. Then it reasons
about the limits of the listing it was given --

> Only `cases.0` paths are listed, so I do not even know that more than one
> cell is written.

-- and separates the falsifier's second limb as source inspection rather
than a declared run, concluding that failing it *"leaves the historical-
attribution sub-question unsettled rather than resolved"*.

**The bank rendering.** The curator ran, re-rendered and committed at
`333b1a3`, and the continuation lines of both stored refusals are now
indented under their own bullet. Verified against the real page, not a
fixture.

**The Curator's fetch.** Removing it did not break curation: `333b1a3` is
80 files, written after the deletion.

Seven refusals now, across two sessions, three prompt versions and a
restart on new code. Not one of them has proposed to measure something
adjacent and call it the answer.


### Y.15 The scientific-workflow review, and the defect that should have ended the run

A second independent reviewer was given the branch, the scientific
documents, read-only access and permission to run individual test files.
It found six more. The first is the worst defect of this entire effort and
it is the one that matters for the verdict.

**A run that produced no number manufactured a positive result.** Python's
`json` accepts `NaN`, `Infinity` and `-Infinity`, which no other JSON
reader does, and `isinstance(float("nan"), float)` is `True`. So a
declared command whose solver diverged, wrote `{"overlap": NaN}` and
exited 0 reached the arithmetic. Confirmed against the real `analyse`:

```text
rule  !=0 / ==0    metric = NaN   ->  SUPPORTS
rule  >0.5 / <=0.5 metric = NaN   ->  INCONCLUSIVE
rule  >0.5 / <=0.5 metric = Inf   ->  SUPPORTS
```

The first line is the "not exactly zero" shape that `DecisionPredicate.
holds`'s own docstring singles out as intended and defensible. `NaN != 0`
is True and `NaN == 0` is False, so the success predicate fired and the
failure predicate did not. From there: `EvidenceStrength.SUPPORTS`, an
EXPERIMENT evidence row citing a real `external_jobs` id, `_substantive`
and `_executed` both satisfied, VALIDATED reachable. **This is exactly the
thing the operational/scientific boundary exists to prevent, arriving
through the one door nobody had checked** -- not an operational failure
becoming evidence, but a non-number becoming a number.

§19.5 already said the answer -- "a metric that is not a number" is
INSUFFICIENT -- and the code did not implement it.

Second-order, and nearly as bad: `json.dumps({"observed": nan})` emits a
bare `NaN` token, so the permanent, content-addressed analysis artifact --
the one the evidence row points at, the one that satisfies
`idea_experiments_interpreted_ck` -- **was not valid JSON**. `jq` rejects
it. Every reader that is not Python rejects it.

Three guards, each pinned by its own mutation, and the first mutation run
earned its keep: it showed that the finiteness check alone covered every
case the tests had, so the parse guard was untested. Rather than delete
it, the question it raised -- should a document whose *irrelevant* field
is NaN be read at all -- was settled deliberately (no: a bare NaN token is
not JSON) and a test now pins that answer.

**The preregistered rule was never verified.** §19.4's whole claim is that
the rule is fixed before the number exists. `submit` read the
specification back out of the immutable artifact and re-hashed it against
the row. `interpret` read the *rule* straight off the mutable row. So the
thing checked twice was what would run, and the thing never checked was
what the result would mean. The reviewer noted there was no test for it
because there was nothing to test. One function returns both now and
checks both; the test is the one that could not previously be written --
run the experiment, move the threshold in the database, try to read the
result.

**A missing provider family was recorded as a broken system.**
`IndependenceUnavailableError` is a sibling of `ProviderCallFailedError`,
not a subclass, so no portfolio handler caught it and it arrived at the
catch-all as `UNKNOWN` -> `FATAL_INFRASTRUCTURE_ERROR`. §10 has said the
answer is `WAITING_FOR_EXTERNAL_DEPENDENCY` all along and the objective
cycle does it; this layer did not. That is a deployment policy decision
recorded as a defect -- the shape the taxonomy exists to prevent.

**Three smaller ones.** `analyse`'s *summary* still said a run "did not
write" a rule output that was past the collection bound -- the same
falsehood this session had already fixed in the *notes*, one branch over,
so the evidence row carried the correction beside the false claim.
`build_snapshot` resolved review liveness twice from two different
configs, reintroducing at one call site the livelock a previous audit had
closed for six. And a worktree left by an experiment that a *revision*
superseded was never collected, because `supersede_experiments_below` is
pure SQL with no repository in hand -- the fourth instance of the family
§19.7a says was already paid for three times.

### Y.16 What the second review said about the tests, which is the useful part

The reviewer was asked why ~4,400 green tests missed everything above,
and the answer is better than "not enough tests". The suite uses a real
PostgreSQL, real Git worktrees, real subprocesses and real hashes, and
drives the production entry point end to end. The escapes are structural:

1. **The value domain is never adversarial.** Every number any test reads
   comes from one fixture that always writes a well-formed finite float.
   The two variants crash or write nothing. There is no script that emits
   malformed JSON, a string-typed number, or a NaN -- and the one unit
   table that enumerates bad metrics picks five shapes by hand and stops
   short of the two that mattered. *That is where the critical defect
   lived.*
2. **Verification is tested where it exists, not where it is absent.**
   There is a good test of the spec-digest rebuild and none of the rule,
   because there was no check to test. A suite organised as one test per
   enforcement is structurally incapable of finding a *missing*
   enforcement.
3. **The gate suite's positive control used the one type production
   refuses.** `_build` defaults to MATHEMATICAL, which `run_evidence`
   denies by §18, so `EVIDENCE_RULES[EMPIRICAL]` -- the rules every real
   adjudicated idea in this dogfood has -- had only refusal tests. Now
   fixed, and it is a recurrence of a shape `gates.py` already records an
   earlier audit finding: the code was fixed and the control was not
   generalised.
4. **Assertions that read a value the double was configured to produce.**
   `board_independence(live) == 3` tests that a `len(set())` counts, over
   a router handed three families unconditionally. It establishes nothing
   about the system obtaining three.
5. **The one seam that matters for §10 was mocked out.** `ScriptedRouter`
   ignores `independence_group` entirely and could not raise
   `IndependenceUnavailableError`. That is how the missing handler
   survived. The double can now fail that way.
6. **A property test that supplies its own post-conditions.** "Terminates
   from any starting point" passes complete evidence and complete reviews
   on every iteration, so the EVIDENCE branch is never the live one and
   the eighth loop §19.8 names is outside the property's domain.

Four of the six are the same disease: **the test decides what the world
hands the system, and hands it something reasonable.** A real provider,
a real solver and a real host do not.

### Y.17 Two findings recorded and deliberately not acted on

**A threshold can still be re-selected after a number is visible, by
revising the idea.** The measured value is written verbatim into the
evidence summary, that row is in the frozen review packet, a reviewer who
quotes the number in a CRITICAL objection carries it into `run_discover`
(which is not given the evidence block, but is given the objections), the
revision produces a new version, and `design` writes a **new rule with new
thresholds** for a question whose answer is now known. Bounded at
`max_revisions_per_idea`, so four further chances. Nothing records that
the new rule postdates a visible result.

Not fixed, because every available fix is a design decision: blind the
objection text, forbid a new rule after an interpreted measurement, or
mark such a rule in the record. The last is probably right and it is not
an agent's call to make at 2am.

**An objection is "answered" by a review that need not agree with
anything.** `_try_resolve_objections` requires a review of that version by
a different role that the revision named -- and applies no verdict filter,
so a CRITICAL objection can be closed by a review whose verdict is
REVISE or REJECT, and `candidates[0]` is whichever sorts first rather than
the most relevant. The code and §9 agree with each other here, so this is
a design weakness rather than a divergence; but "answered" reads far
stronger in the severity table than "somebody else reviewed a version
that claimed to address it".

## Z. Final release assessment

### Z.1 What each verdict would require

**`FULL_AUTONOMOUS_RESEARCH_OS_READY`** would mean the loop this system
exists to close has closed on real science: an idea generated, sharpened,
survived falsification, measured by a real experiment, read by arithmetic
against a rule fixed beforehand, reviewed by genuinely independent
reviewers, replicated a second way, and handed to a researcher as
something they could act on -- more than once, on more than one project,
without a person in the loop.

**`AUTONOMOUS_DISCOVERY_RELEASE_CANDIDATE`** would mean that path has run
end to end at least once on real work, that the defects being found are
getting smaller and rarer, and that a reviewer looking hard finds
refinements rather than false records.

**`AUTONOMOUS_DISCOVERY_BETA`** means what the accepted checkpoint already
meant: the machinery is real, it runs unattended, its boundaries hold, and
it has not yet done the thing.

### Z.2 What is true

The case *for* advancing is not nothing, and it is stronger than it was:

- The system ran unattended in eight segments, longest 5h 06m, and cost
  $23.45 against ceilings it never exceeded and a human ceiling it never
  raised.
- **The authority boundary held under adversarial audit.** An independent
  reviewer with the branch and six hours of context could not construct a
  path to a human `Review`, to a promotion, to a write under `.research/`,
  or to the researcher's branch. After 120 ideas and a thousand work
  items, both human branches still show exactly one reflog entry --
  `clone:` -- both capsules validate, and the explicit $50 ceiling is
  untouched.
- **The science criticism is good.** Twelve rejections read in full, twelve
  defensible, two of them catching errors -- a citation used for a
  condition it does not establish, a Sherman-Morrison step that drops
  cross-leaf terms -- that a careful referee might miss.
- **It refuses rather than substitutes.** Seven times, across two sessions,
  three prompt versions and a restart, asked to measure something it could
  not, it declined to measure something adjacent and call it the answer.
  That is the single most valuable behaviour observed, and it is worth
  more than a traversal would have been.
- It recovers from a hard kill by ordinary reconciliation, and it upgraded
  its own schema six times under load.

### Z.3 The verdict

**`AUTONOMOUS_DISCOVERY_BETA`.**

Unchanged from the accepted checkpoint, and the reasons are not close.

**Nothing completed.** Zero experiments concluded, zero board reviews,
zero replications, zero branches, zero VALIDATED, zero HUMAN_READY. The
review board, the meta-review, replication and branching have test
coverage including a drive that reaches HUMAN_READY, and **no
production-path evidence whatsoever**. A release candidate cannot be
declared for a path that has never run.

**The blocker is real but it is not an excuse.** It is human-owned --
seven named capabilities in §Y.2, five of them measurements this project
has not built -- and no agent may supply them. But the system's readiness
is not established by having a good reason for not being exercised.

**And the defect surface is not exhausted. This is the deciding fact.**
Twenty-one defects this session. Ten found by this author. Then two
independent reviewers, each given one afternoon and read-only access to
code carrying 4,402 green tests that had already been audited twice, found
**eleven more between them** -- and they did not overlap.

The first found four, every one of them the permanent record asserting
something untrue: every analysis artifact in the store said
`"containment": "completed"`, so the containment invariant
`DESIGN_INVARIANTS.md` leans on was unfalsifiable from the record it
points at, and a researcher *lowering* autonomy to be more careful got
uncontained execution.

The second found six, and the first of those is the one that should decide
this verdict: **a `NaN` metric produced `SUPPORTS`.** A declared command
whose solver diverged, wrote `{"overlap": NaN}` and exited 0 reached the
arithmetic, and under the "not exactly zero" rule shape the code
explicitly defends as intended, the success predicate fired. Evidence row,
gates, VALIDATED. The permanent artifact recording it was not valid JSON.
The same review found that the preregistered *rule* -- the entire content
of §19.4's claim -- was never checked against its artifact while the
specification was checked twice.

None of these is a refinement. Two independent passes, no overlap, and
both found record-integrity defects at the centre of the system's
scientific claims. **Nothing in this run locates the edge of that
surface.** A third review finding a seventh class is the expectation, not
the surprise, and a system whose measurements can be trusted is precisely
what has not been demonstrated.

**Three known gaps are recorded and unfixed**, each because it is a
person's decision rather than a defect: the authority table does not
govern this layer, the promotion gates do not read evidence direction, and
independence is surfaced rather than required -- which on this host, with
one provider family installed, means a board would be three samples from
one model.

### Z.4 What would move it

In order, and the first is the only one that is not this system's to do:

```text
1. the seven capabilities of §Y.2 -- above all a declared command that
   writes a JSON document with a declared output schema
2. one real idea through experiment -> evidence -> board -> meta-review
   -> replication, on the production path, start to finish
3. a second provider family, so "independent review" can be true
4. one full unattended run, eight hours or more, on code that does not
   change during it
5. adversarial value-domain tests: a fixture that writes malformed JSON,
   a string where a number belongs, a NaN, an oversized document. §Y.16
   is explicit that four of the six escape patterns are one disease, and
   the critical defect lived in the gap this item names
6. a review that finds only refinements
```

Items 2 through 4 are reachable only once item 1 exists. Item 5 is
reachable today, costs an afternoon, and is the one thing on this list
that would have caught the worst defect of the run.

