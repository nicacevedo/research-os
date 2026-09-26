# Research OS -- architecture integrity invariants

Live specification. `DESIGN_INVARIANTS.md` is the constitution and is
unchanged by this document; `docs/CAPSULE.md` remains authoritative on
anything scientific. What this document adds is narrower and more
mechanical: the ten load-bearing properties the *autonomous* layers -- the
runtime (`research_os.runtime`) and the discovery portfolio
(`research_os.portfolio`) -- must hold for anything they record to be
evidence at all, and where each one is enforced.

They were written down after the final adversarial review of `37e8afe`
reproduced six HIGH and four MEDIUM defects (frozen evidence:
`~/.local/state/research-os-qualification/37e8afe…/evidence/60-part-e-review/`).
Every one of those defects is a violation of one of the invariants below, and
the table at the end maps each defect to the invariant it broke.

Each invariant is stated once, with:

- **threat** -- the failure class it exists to prevent;
- **enforced by** -- the code that makes it true, by module and function;
- **tests** -- where the property is demonstrated, adversarially;
- **PASS requires** -- the evidence that must exist before anyone may claim
  the invariant holds.

The same mapping is machine-readable in `docs/architecture_invariants.yaml`,
and `tests/test_architecture_invariants.py` fails if a named enforcement
point or test stops existing.

---

## INV-01 -- Budget authority

> No externally initiated model or action expenditure may consume authority
> beyond what the applicable run, project, system, idea and lineage limits
> authorise -- including when the caller times out, crashes, loses the
> response, or cannot learn the provider's outcome. Unknown external spend
> fails closed.

**Threat.** A spend whose cost is unknown is treated as a spend of zero, so
the authority it may have consumed is handed back and spent again. (H5: a
call killed at its timeout released its whole reservation; five calls capped
at 0.60 USD ran under a 1.00 USD ceiling with the ledger showing nothing
spent.)

**Enforced by.**

- `runtime.budgets.BudgetLedger.reserve(pending=True)` and
  `BudgetLedger.mark_submitted` -- a reservation records, durably and *before*
  the external call starts, whether the work was handed over
  (`budget_reservations.submitted_at`, `sql/0037`);
- `runtime.routing.ModelRouter.complete` -- a failure is settled at its
  reported cost when the provider reported one, released only when the
  provider provably never started (`InvocationResult.invoked is False`), and
  otherwise settled at the whole reservation (`BudgetLedger.settle_unknown`);
  an adapter that raised is charged in full;
- `runtime.budgets.BudgetLedger.reconcile_stale` -- a dead worker's
  reservation is released only if it was never submitted and is otherwise
  settled at its whole amount; idempotent because a reservation leaves
  `HELD` exactly once;
- `runtime.spend.BudgetedProvider` / `DelegatedSpendAuthority` -- the same
  rule for calls made inside delegated controllers;
- `automation.providers.ClaudeCodeProvider.invoke` -- the only source of the
  "never started" evidence: the executable could not be executed.

**PASS requires.** Through the real router, one timed-out call and no more
under a ceiling that covers one, in all five scopes; process death (real
`os._exit`) before submission released, and after submission, during
processing and after the response but before settlement charged in full;
repeated reconciliation changing nothing.

## INV-02 -- Evidence ownership

> Model-authored text can never impersonate or create system-authored
> scientific evidence, provenance, execution records, review state,
> literature records, replication records or promotion state.

**Threat.** A model writes a field that a human reader, or the system
itself, mistakes for something the system computed. (H2: a research question
containing a newline forged a second `> executed evidence:` header on
`HUMAN_READY.md`; a claimed difference forged a `## Reviews` section.)

**Enforced by.**

- `portfolio.curator` renders every page through `_Page`: structure
  (headings, the computed provenance header, section order) is emitted only by
  trusted code from database records, and every model-authored value passes
  through `_untrusted`, which makes it one line and Markdown/HTML-inert and
  places it after a trusted label, so it can never begin a line;
  `_Page.render` refuses to produce a page whose structural lines differ from
  the ones the renderer emitted;
- the system-owned counts on a page (executions, retrieved sources, reviewer
  models, objections, direction of executed evidence) are computed from rows
  (`curator._provenance`), never read from model text;
- literature search counts and confidence come from executed-retrieval rows,
  not from a model's list of queries (see INV-05);
- a replication's consumption receipt is refused when a model-composed input
  already contains the receipt key (see INV-07).

**PASS requires.** Adversarial payloads -- Markdown headings, a fake Reviews
section, fake executed-evidence and provenance lines, YAML and document
delimiters, HTML and comment markup, multi-line text -- in every
model-authored field leave every page's structure identical to the page
rendered without them.

## INV-03 -- Action uniqueness

> A scientific action has at most one valid live execution owner at a time.
> Elapsed wall-clock time alone is not proof that a worker crashed.
> Re-execution requires an explicit new attempt and execution identity.

**Threat.** A stage that is still running is declared dead and bought
again, and the first execution's late result lands beside the second's. (H3:
`idea_actions.work_id` was never written, so every stage older than
`stale_action_grace_seconds` was reclaimed as `worker_crash` while its worker
ran.)

**Enforced by.**

- `portfolio.track.advance_idea` -- takes the execution's session advisory
  lock (`locks.research_run_lock`, keyed by the stage's own run) *before* it
  opens the action and holds it until the action is closed, and records the
  owner on the action: `run_id`, `work_id`, the work item's `attempt` and its
  lease owner (`sql/0038`);
- `portfolio.store.PortfolioStore.owner_is_live` -- an owner is live while its
  session lock is held (PostgreSQL releases it when the session dies, so no
  timer decides it) or while the work item that bought it is `LEASED` to the
  same owner and attempt with an unexpired, renewed lease (the daemon's
  `LeaseKeeper` is the heartbeat);
- `PortfolioStore.stale_actions` / `reclaim_dead_actions` -- only an action
  whose owner is provably not live is reclaimed; elapsed time is a
  precondition for looking, never the proof;
- `PortfolioStore.open_action` -- takes over a provably dead owner's action
  atomically instead of refusing, and refuses while the owner is live, so a
  duplicate purchase never starts a second execution;
- `PortfolioStore.fenced` -- every write a stage makes goes through a store
  bound to its action, and every transaction re-checks, under a row lock,
  that the action is still `ACTIVE`: a late result from an attempt that lost
  ownership writes nothing (`StaleExecutionError`).

**PASS requires.** A stage running past the old grace period with a live
lease is not reclaimed; a renewed lease keeps it; worker death (a real
process killed with `os._exit`) makes it reclaimable; reclaim after true
expiry; a second worker bought for the same idea starts nothing; a late
result from an attempt that lost ownership writes no row.

## INV-04 -- Durable scientific decisions

> Every review, model or action response that changes scientific state
> corresponds to exactly one durable provenance record identifying the
> execution that produced it. No unrecorded answer affects state.

**Threat.** A response is applied while the record of it is dropped, or one
call's record is silently reused for another. (H4: a second meta-review of
an unchanged record was applied while its review row was discarded by
`on conflict do nothing`; M3: a re-run reviewer's objections were attached to
the first call's review.)

**Enforced by.**

- `portfolio.store.PortfolioStore.record_review` -- reviews are append-only
  and keyed by the model call that produced them (`sql/0040`): one row per
  call, a replay of the same call returns the same row, a different call is
  a different row with its own `attempt` and `supersedes_review_id`, and the
  owning `action_id`;
- `portfolio.runner._record_review` -- refuses to return a review that is not
  the row for this response's call, and attaches objections to that row;
- `portfolio.runner.run_meta_review` -- applies a recommendation only after
  its own review row is durable;
- `portfolio.stages.basis_review_ids` -- a meta-review's basis is the record it
  synthesises (content, evidence, the board), not its own output, so an
  unchanged record is never re-synthesised.

**PASS requires.** A declining meta-review is not re-asked on an unchanged
record; every meta-review call has exactly one review row naming its call;
objections of a re-run are attached to the re-run's own review.

## INV-05 -- Retrieval truth

> Literature search claims, confidence and independence gates derive from
> retrieval operations the system actually executed and recorded, never from
> queries or searches claimed in model-generated text.

**Threat.** A model's claim that it searched counts as a search. (H1: the
"second terminology path" was satisfied by two identical retrievals, or by a
retried first audit, because it compared the keys two scouts *cited*; M1:
`literature_confidence` and "over N queries" were computed from the scout's
own list of queries.)

**Enforced by.**

- `portfolio.store.PortfolioStore.begin_retrieval` /
  `complete_retrieval` / `fail_retrieval` -- every search a stage performs is
  a durable `literature_retrievals` row (`sql/0039`): its identity, owning
  action and run, purpose, the exact query and its normalised digest, the
  backend, the limit, start and completion, and the retrieved keys with their
  digest -- written *before* the search and completed after it;
- `idea_evidence.retrieval_id` -- every literature row an audit writes names
  the retrieval that supplied its source;
- `portfolio.gates._second_terminology_path` -- the criterion below, over
  retrieval rows;
- `portfolio.runner.run_literature_audit` -- confidence is the number of
  distinct executed searches for the version, and the stage detail reports
  executed searches; the model's `queries` list is not read.

**The second terminology path, defined conservatively.** A
literature-adjudicated idea has a second, independent retrieval path only if
there is a completed retrieval `R2` whose purpose is `second_path` (issued by
the system's `REPLICATE` stage) such that, against every completed
first-path retrieval of the same version (the novelty screen, the audit and
any audit retry, and the readings):

1. `R2` is its own execution: a different retrieval id, owned by a different
   action;
2. `R2` used different words: its normalised query digest equals none of
   theirs;
3. `R2` retrieved a different result: its result digest equals none of
   theirs, so an identical or cached result reused is not a path;
4. `R2` retrieved at least `new_literature_keys` works that no first-path
   retrieval retrieved and no reading cited, and its own audit assessed them:
   they are cited by evidence rows bound to `R2`.

Citing different papers from the same retrieval satisfies none of these.

**PASS requires.** The same search referenced twice, a retried first audit,
an identical cached result reused, and a scout that claims queries no search
ran each fail the criterion; a genuinely separate retrieval that found and
assessed new sources passes it; confidence cannot be raised by prose.

## INV-06 -- Contract before result

> Primary estimands, success/failure/inconclusive rules and every other
> preregistered commitment used to judge a result are frozen before the
> result is available.

**Threat.** A rule is chosen, or changed, after its answer is known.

**Enforced by** (unchanged by this round, and not weakened by it).
`portfolio.scicontract` freezes and digests the analysis before the design
is asked for; `portfolio.empirical.build_spec` / `_preregistered` rebuild and
re-hash the specification before submission and again before
interpretation; `portfolio.store.PortfolioStore.freeze_contract`; the
replication inherits the primary's frozen analysis rather than re-asking.
The replication execution manifest added for INV-07 is frozen at submission,
before the replication runs, and re-verified by digest when it is read.

**PASS requires.** The existing contract, lineage and mutation suites
(`tests/test_portfolio_scientific_contract.py`,
`tests/test_portfolio_contract_lineage.py`,
`tests/test_portfolio_engine_mutations.py`) passing unchanged, and the
replication manifest frozen before its execution.

## INV-07 -- Replication causality

> A replication counts as an independent execution only with evidence that
> the intended replication configuration or perturbation was actually
> supplied to and consumed by the scientific computation. Different output
> bytes alone never prove independence.

**Threat.** A second run that ignored its new seed is recorded as an
independent replication because some byte of its output -- a timestamp --
differs. (H6: `empirical._byte_identical_primary` compared whole-file
digests.)

**Enforced by.**

- `portfolio.empirical.freeze_replication_manifest` -- before a replication
  runs, a manifest is frozen and stored by digest (`sql/0041`): the parent
  experiment, its specification and contract, the code identity (the
  workspace's base commit and the declared command), the immutable inputs,
  the environment, and the *independence variables* -- every seed, parameter,
  composed input or command that differs from the primary's frozen
  specification;
- `portfolio.empirical.assess_independence` -- execution independence is
  established only by a **consumption receipt**: a collected JSON output
  carrying a top-level `execution_receipt` object in which the computation
  reports the seeds, parameters and input digests it actually used, each
  equal to the manifest's intended value. A different declared command is
  evidenced by the execution record itself. A receipt pre-seeded by a
  model-composed input is refused;
- execution independence and agreement are separate: `assess_agreement`
  compares the scientific values the frozen analysis read (never file bytes,
  never metadata) and the two conclusions, and records the comparison;
  a replication with verified independence that agrees exactly is still a
  replication.

A replication whose command emits no receipt is recorded `INSUFFICIENT` with
the reason, which is INV-09's capability-limited state and not a failure of
the idea.

**PASS requires.** Seed changed in the manifest but ignored by the program:
not independent. Timestamps differ but the scientific values are identical
because the variation was ignored: not independent. The specified seed
consumed and identical values: independent, and agreeing. A genuinely
independent execution with a different result: independent, and the
disagreement recorded separately.

## INV-08 -- Evidence completeness

> Required reviewer, search and replication evidence cannot be silently
> replaced, skipped, masked or inferred from another successful stage. A
> mandatory failed or missing stage remains visible to promotion and
> readiness logic.

**Threat.** One reviewer's success hides another's failure. (M2: each board
node overwrote `failure_class`, so a board whose methodology reviewer was
unreachable was recorded `SUCCEEDED` and the idea moved to `REVIEW`.)

**Enforced by.**

- `portfolio.track._accumulate` -- a board keeps the first failure it saw;
  a later success cannot clear it;
- `portfolio.runner.finish_review_board` -- the board is complete only when
  every required role has a live, completed review of the current revision;
  otherwise the board fails, names the missing roles, resolves no objection
  and does not move the idea;
- `portfolio.stages.board_state` -- the per-role state of the board,
  explicit;
- `portfolio.gates._validated_unmet` -- every live review of a required role
  must endorse; one endorsement does not mask another reading of the same
  role that did not;
- INV-05's retrieval rows and INV-07's receipts, which a gate reads directly
  rather than inferring from a stage having succeeded.

**PASS requires.** A board missing a reviewer is not a completed board and
the idea does not advance; a re-run endorsement does not mask a standing
negative review of the same role.

## INV-09 -- Capability honesty

> An unsupported scientific operation produces an explicit, principled
> refusal or capability-limited state, never fabricated or substituted
> evidence.

**Threat.** Missing capability is papered over with something that looks
like evidence.

**Enforced by** (existing, preserved): `runner.run_literature_audit` with
no source (`CAPABILITY_DENIED`), `EmpiricalConclusion.OPERATIONALLY_BLOCKED`
and the design refusal path (`testable: false`), the objective cycle's
refusal handling. Added here: a replication whose command reports no
receipt is `INSUFFICIENT` with the stated reason (INV-07); a meta-review or
board that could not complete is a failed stage, not an inferred one
(INV-08).

**PASS requires.** The existing refusal tests
(`tests/test_portfolio_literature_intel.py`, `tests/test_portfolio_empirical.py`,
`tests/test_portfolio_scientific_contract.py`, `tests/test_runtime_actions.py`)
passing unchanged, and the new capability-limited replication test.

## INV-10 -- Human authority

> Human-controlled operations -- promotion and release decisions, portfolio
> resume where it is human-owned, and budget or ceiling expansion -- stay
> outside autonomous self-granting authority.

**Threat.** The machine raises its own ceiling, resumes itself, or promotes
its own work.

**Enforced by** (existing, preserved): nothing under `research_os/runtime`
or `research_os/portfolio` imports the promotion doors
(`tests/test_runtime_authority.py`, `tests/test_portfolio_authority.py`);
`budgets.explicit` is set only by `researchctl runtime budget`. Added here:
an authority test that no autonomous module writes a portfolio bound
override, sets an explicit ceiling, or forgives stage failures; and the
budget-park revival (M4) *reads* a ceiling a person raised and never raises
one.

**PASS requires.** The authority suites passing, including the new check.

---

## Defect to invariant map

| finding | what failed | invariant(s) |
|---|---|---|
| H1 second terminology path from cited keys | a retried or identical retrieval counted as independent | INV-05, INV-08 |
| H2 forged bank provenance | model text created trusted page structure | INV-02 |
| H3 live stages reclaimed as crashed | elapsed time treated as proof of death | INV-03 |
| H4 meta-review applied without its row | an answer affected state with no durable record | INV-04 |
| H5 timed-out calls released their reservation | unknown spend treated as zero | INV-01 |
| H6 timestamp-different replication | byte inequality treated as causal independence | INV-07 |
| M1 confidence from claimed queries | model prose counted as searches | INV-05, INV-02 |
| M2 reviewer failure masked by a later success | a mandatory failure disappeared | INV-08 |
| M3 re-run objections on the first review | one call's record reused for another | INV-04 |
| M4 budget-parked ideas never revived | a structural block reason was prose | INV-10 (a person's ceiling increase is what revives it) |

## Status at the integrity-closure round (2026-09-26)

Each invariant was checked against the evidence its **PASS requires** on the
tree that closed the ten findings:

| invariant | status | how it was shown |
|---|---|---|
| INV-01 | PASS | 16 adversarial tests incl. real `os._exit` deaths at four boundaries; the H5 reproduction; the 0036 upgrade test; 6 mutants killed |
| INV-02 | PASS | 21 payloads x every model-authored field x every page kind; both H2 reproductions; 5 mutants killed |
| INV-03 | PASS | 11 tests incl. a real killed worker and a terminated lock session; the H3 reproduction; 5 mutants killed |
| INV-04 | PASS | 5 tests; the H4 reproduction; 3 mutants killed |
| INV-05 | PASS | 13 tests incl. end-to-end retrieval provenance; the H1 and M1 reproductions; 7 mutants killed |
| INV-06 | PASS | existing contract, lineage and engine-mutation suites unchanged and passing; the replication manifest frozen before execution |
| INV-07 | PASS | the four named cases, a lying receipt, a model-composed receipt, a missing manifest; the H6 reproduction; 4 mutants killed |
| INV-08 | PASS | board completeness per role, a superseded-prompt review, masking by a re-run; the M2 reproduction; 3 mutants killed |
| INV-09 | PASS | the existing refusal tests unchanged; the no-receipt replication recorded INSUFFICIENT with its reason |
| INV-10 | PASS | package scans for bound writes, explicit ceilings and block lifts; the researcher's pause held; the M4 reproduction; 3 mutants killed |

Every one of the eleven original reproductions fails on `37e8afe` with the
defect's own assertion and passes here. `tests/integrity_mutations.py`
kills all 35 of its mutants; the two equivalent ones it records are
defence in depth, argued in its `EQUIVALENT` table.

## What these invariants do not claim

- They do not make a model's judgement correct. A gate that checks the right
  kinds of evidence exist can be satisfied by work that is thorough and wrong.
- INV-07's receipt is reported by the researcher's declared program. A
  program that lies about what it consumed is outside what the runtime can
  detect; the runtime ensures only that no *model* can write that receipt.
- INV-03's session lock is a liveness signal of the process's database
  session. A process whose session was closed while it kept running is
  treated as dead and fenced: its later writes are refused, and its already
  started provider call may be paid for twice. That is a cost, never a second
  scientific record.
- INV-01 errs towards over-counting. A call killed at its timeout is charged
  its whole ceiling even if it cost less, and a worker that died after the
  provider answered but before settling is charged the ceiling although the
  answer (lost with the process) named a smaller number. Both are visible in
  `budget_reservations.settlement_basis` and correctable by a person; the
  under-count they replace was neither.
- INV-07 needs the declared program's cooperation. A command that writes no
  `execution_receipt` still runs, and its replications are recorded
  `INSUFFICIENT` with the reason: the cg project's `benchmark` and
  `adjudicate-pricing` commands do not write one today, so a replication of
  theirs cannot count until their programs report what they consumed
  (`docs/EXPERIMENTS.md`). That is a capability limit stated, not a gap
  hidden.
- An action recorded with no owner at all -- only possible outside
  `track.advance_idea`, which records one every time -- cannot be proved
  alive or dead and is still reclaimed by age.
