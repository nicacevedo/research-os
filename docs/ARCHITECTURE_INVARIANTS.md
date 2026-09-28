# Research OS -- architecture integrity invariants

Live specification. `DESIGN_INVARIANTS.md` is the constitution and is
unchanged by this document; `docs/CAPSULE.md` remains authoritative on
anything scientific. What this document adds is narrower and more
mechanical: the ten load-bearing properties the *autonomous* layers -- the
runtime (`research_os.runtime`) and the discovery portfolio
(`research_os.portfolio`) -- must hold for anything they record to be
evidence at all, and where each one is enforced.

Two adversarial reviews shaped it:

- the final review of `37e8afe` reproduced six HIGH and four MEDIUM defects
  (H1-H6, M1-M4; frozen evidence under
  `~/.local/state/research-os-qualification/37e8afe…/evidence/60-part-e-review/`),
  and the first integrity round wrote these invariants and closed them;
- the independent review of `8e92e8c` returned
  `INDEPENDENT_INTEGRITY_REVIEW_FAIL` with seven HIGH, one MEDIUM and one LOW
  finding (R1-R9; frozen under
  `~/.local/state/research-os-qualification/8e92e8c…/independent-review/`,
  with its harness `test_adversarial.py` and log). Several of them showed an
  invariant stated more strongly than anything could establish. The second
  integrity round corrected those statements as well as the code: an
  invariant here now claims only what Research OS can actually show.

Each invariant is stated once, with:

- **threat** -- the failure class it exists to prevent;
- **enforced by** -- the code that makes it true, by module and function;
- **tests** -- where the property is demonstrated, adversarially;
- **PASS requires** -- the evidence that must exist before anyone may claim
  the invariant holds.

The same mapping is machine-readable in `docs/architecture_invariants.yaml`,
and `tests/test_architecture_invariants.py` fails if a named enforcement
point, test or mutant stops existing, if a finding maps to nothing, if a
frozen evidence file no longer hashes to what is recorded, or if a decision
point of the readiness gate is not named as an enforcement point. The mapping
is traceability, not proof: whether a test demonstrates its property is the
test's job, and whether the tests bite is `tests/integrity_mutations.py`'s.

---

## INV-01 -- Budget authority

> Every externally spend-bearing invocation the autonomous layers make
> carries a **provider-enforced hard cap no greater than its Research OS
> reservation**, recorded durably with the reservation's submission before
> the invocation begins. No such invocation reaches a provider with an
> absent, null or larger cap, and an adapter that cannot be handed one is
> refused (`PROVIDER_HARD_BUDGET_CAP_UNAVAILABLE`). The reservation is taken
> against every applicable run, project, system, idea and lineage limit
> before anything starts, and unknown spend -- a timeout, a crash, a lost
> response, a lost settlement -- is charged the whole reservation, never
> handed back. An estimated cost is never authority.

**Threat.** External spend beyond a person's ceiling: unknown spend treated
as zero (H5: a timed-out call released its reservation; five calls capped at
0.60 USD ran under a 1.00 USD ceiling), or an estimate reserved in front of a
provider nothing caps (R1: an objective-cycle call reserved a 0.05 USD
estimate and every delegated call 0.50, both reached the provider with
`max_budget_usd=None`, and a 1.20 USD call settled past 1.00 USD ceilings in
every scope; two such calls whose settlement was lost were charged 1.00 while
2.40 was billed).

**Enforced by.**

- `runtime.routing.ModelRouter.complete` -- reserves a request's declared
  ceiling, or `budgets.DEFAULT_CALL_CEILING_USD` when it declares none, and
  hands the provider exactly that number as `max_budget_usd` on every call;
  `ModelRouter._eligible` / `route` exclude an adapter that does not declare
  `hard_budget_cap` and raise `BudgetCapUnavailableError` (a capability
  refusal) when that leaves nothing;
- `runtime.spend.BudgetedProvider.invoke` -- the same for delegated
  controllers: the adapter's capability is checked before anything is
  reserved, and the request is handed on capped at the reservation (or the
  controller's own smaller cap). The ratchet towards what an uncapped
  provider had billed is gone. `runtime.actions.coding._controller` and
  `runtime.actions.proposals._controller` require the authority -- no
  unwrapped adapter reaches a delegated controller;
- `runtime.budgets.BudgetLedger.mark_submitted` -- a `model_cost_usd`
  reservation becomes submitted only with the cap it authorises, no greater
  than its amount, written in the same statement; `sql/0043` makes the
  database refuse it otherwise, and refuse rewriting the cap afterwards;
- `BudgetLedger.settle` / `settle_unknown` / `reconcile_stale` -- a reported
  cost is settled; a provider that reports more than its cap is charged what
  it reported and marked `reported_over_reservation`; anything unknown is
  charged in full, idempotently;
- `automation.providers.enforces_hard_budget_cap` -- an adapter that says
  nothing is read as unable to cap; `ClaudeCodeProvider` declares it and its
  probe refuses a CLI whose `--help` lacks `--max-budget-usd`.

**PASS requires.** The reviewer's three routes -- delegated, objective and
lost settlement -- bounded in all five scopes through the real router and
wrapper; two uncertain calls unable to share one final ceiling; each scope
alone refusing a call it cannot cover; an uncappable adapter refused by both
doors with nothing reserved; the ledger and the database refusing an uncapped
submission; only the router building an invocation in the autonomous layers;
timeout and process-death cases charged in full; reconciliation idempotent.

## INV-02 -- Evidence ownership

> Model-authored or program-authored content can never impersonate or create
> system-authored scientific evidence, provenance, execution records, review
> state, literature records, replication records or promotion state. The only
> execution receipt is the one Research OS's runner writes from what it
> observed.

**Threat.** Something that is not the system writes a field the system, or a
person, reads as the system's (H2: a research question forged a
`> executed evidence:` header; R2: a model-composed plan carried an escaped
receipt key that a program echoed, and the runtime accepted it as
provenance).

**Enforced by.**

- `portfolio.curator` renders every page through `_Page`: structure is
  emitted only by trusted code from records, every model-authored value
  passes through `_untrusted`, and `_Page.render` refuses a page whose
  structural lines differ from the ones the renderer emitted; the counts on a
  page come from rows (`curator._provenance`);
- `portfolio.provenance.write_receipt` -- the runner's receipt: the job it
  launched, the configuration it delivered, the inputs it materialised, the
  code identity, the declared command's digest and the output digests at
  exit; content-addressed and indexed by an immutable `execution_receipts`
  row (`sql/0046`) the database checks against the experiment and parent it
  names. No program output is read into it, and none is read as it;
- `portfolio.provenance.verified_receipt` -- a receipt is used only after
  its bytes re-hash to its address and every field matches its row.

**PASS requires.** Every adversarial payload in every model-authored field
leaving every page's structure what the records alone determine; the
reviewer's escaped, nested, echoed receipt reaching the output and being read
as nothing; a receipt that cannot be bound to another run or another parent,
changed or removed.

## INV-03 -- Action uniqueness

> A scientific action has at most one valid live execution owner at a time.
> Elapsed wall-clock time alone is not proof that a worker crashed.
> Re-execution requires an explicit new attempt and execution identity. A
> scheduler that loses the race to buy an action is told it lost.

**Threat.** A running stage declared dead and bought again (H3), or a lost
race surfacing as an operational database error (R9).

**Enforced by.** `portfolio.track.advance_idea` (the session advisory lock
held for the action's life, the owner recorded before external work);
`PortfolioStore.owner_is_live` / `_owner_state`; `stale_actions` /
`reclaim_dead_actions` (only a provably dead owner is reclaimed);
`PortfolioStore.open_action` -- takes over a provably dead owner atomically,
refuses a live one, and translates the partial unique index's violation into
`ActiveTrackExistsError`; `PortfolioStore.fenced` (every write re-checks
ownership under a row lock).

**PASS requires.** A stage past the old grace period with a live owner not
reclaimed; a killed worker reclaimable only when its lease truly expires; a
duplicate purchase starting nothing, and the loser of the insert race getting
the lost-race error; a late result from an attempt that lost ownership
writing no row.

## INV-04 -- Durable scientific decisions

> Every review, model or action response that changes scientific state
> corresponds to exactly one durable provenance record identifying the
> execution that produced it. **A review decision and every objection it
> raised are one atomic durable event**: they commit together or not at all,
> no review is valid or complete without all of its objections, and no
> objection is added to a review afterwards. Objection provenance is never
> deduplicated across reviews.

**Threat.** A response applied while its record is dropped or reused (H4,
M3), or a review recorded without the objections it raised: R3 -- a later
reviewer's FATAL objection worded like an earlier MINOR one was folded into
the MINOR row and the earlier review; R4 -- a worker that died between the
review commit and the objection commit left a completed `PASS_WITH_OBJECTIONS`
review of FATAL severity with no objection.

**Enforced by.**

- `PortfolioStore.record_review` -- one row per call (`sql/0040`), written
  in one transaction with every objection the review raised, each at its
  ordinal; the review's severity must be the worst of them;
- `sql/0044` -- a review states its `objection_count`, and a deferred
  constraint refuses at commit any review whose rows do not number exactly
  that or whose worst row is not its severity; objections cannot be added to
  a review later; what a review and its objections said is immutable (an
  objection's resolution is the only change it takes); the normalised-text
  key is a stickiness key a revision names, never identity;
- `portfolio.runner._record_review` -- hands the review and its objections to
  the store in one call and acts only on the event's own rows;
  `run_meta_review` applies a recommendation only after its own row is
  durable; `stages.basis_review_ids`.

**PASS requires.** Same-words MINOR-then-FATAL and FATAL-then-MINOR kept as
separate objections with their own reviews and severities; a crash before
commit leaving no review; retries, replays, meta-review retries and role
re-runs each keeping their own objections; the database refusing a review
committed without its objections, and any rewrite.

## INV-05 -- Retrieval truth: distinct executed retrieval paths

> Literature search claims, confidence and independence gates derive from
> retrieval operations the system actually executed and recorded, never from
> searches claimed in model-generated text. A second, **distinct executed
> retrieval path** is established only under a stated operational rule.
> Research OS does not claim, and cannot establish, that two searches are
> semantically independent.

**Threat.** A model's claim of a search counted as a search (H1, M1), or a
superficial rewording of an executed search counted as a distinct path (R7:
`lasso support overlap` followed by `overlap support lasso` passed the old
"second terminology path").

**Enforced by.** `PortfolioStore.begin_retrieval` / `complete_retrieval` /
`fail_retrieval` (every search a durable `literature_retrievals` row, written
before it runs); `idea_evidence.retrieval_id`; `runner.run_literature_audit`
(confidence is the count of executed searches); `gates._distinct_retrieval_path`
over `digests.retrieval_terms` and `digests.same_term`.

**The distinct-path rule.** A completed retrieval `R2` of purpose
`second_path` (set by the `REPLICATE` stage) qualifies only if, against the
first-path searches of the version (the screen, the audit and its retries,
the readings):

1. it is its own successful execution: completed, owned by an action that
   ran no completed first-path search;
2. **at least one of its normalised content terms is new**:
   `retrieval_terms` applies NFKC, casefolds, splits on anything that is not
   a letter or digit, drops the canonical stopwords and one-letter tokens,
   folds one trailing plural `s`, and discards duplicates and order; a term
   is new only if it is `same_term` as no term of *any* first-path search,
   failed ones included -- two terms are the same when equal or when one is a
   prefix of the other and both have three characters or more. So order,
   punctuation, case, whitespace, repetition, plurals, stopwords, shared
   stems and recombinations of first-path words are not new terms;
3. its result digest matches no completed first-path search;
4. it retrieved at least `new_literature_keys` works no first-path search
   retrieved and no reading cited, and its own audit assessed them.

The rule is lexical and deliberately conservative -- it calls some genuinely
different words the same (`gene`/`general`), which only makes a path harder
to establish. It establishes two executions, some different words, and new
sources assessed. It does not establish independent meaning, and the gate's
own message says so.

**PASS requires.** The same search twice, a retried audit, a cached result, a
claimed search and every superficial variant refused; a second path with no
newly assessed work, or from the same execution, or unsuccessful, refused; a
separate successful search with a new content term that found and had
assessed a new work passing.

## INV-06 -- Contract before result, and frozen ancestry

> Primary estimands, decision rules and every other preregistered commitment
> used to judge a result are frozen before the result is available. A
> replication's execution manifest is frozen before it runs and cannot change
> once its execution has a result. **The ancestry from contract and manifest
> to supporting evidence is transitive and re-verified at readiness**:
> evidence whose ancestry changed -- a repointed manifest, a rebound
> execution, a different parent result, changed bytes -- does not count.

**Threat.** A rule or a replication configuration chosen or changed after
its answer is known (R6: a replication's manifest pointer was changed after
its evidence existed and the gate still passed).

**Enforced by.** `portfolio.scicontract` and `empirical._preregistered` /
`_verified_contract`; `PortfolioStore.freeze_contract`;
`empirical.freeze_replication_manifest` (the manifest -- parent experiment,
parent receipt, parent evidence and analysis, code identity, declaration and
attestation, inputs, delivered configuration, intended variables -- stored by
content hash before the executor runs); `PortfolioStore.set_execution_manifest`,
refused by `sql/0046` once the execution has a result; `empirical.interpret`
with `provenance.workspace_outputs_match` (an output that differs from what
the runner hashed at exit is refused before it is read);
`provenance.replication_provenance` (the chain re-verified at readiness).

**PASS requires.** The contract, lineage and mutation suites passing; the
manifest frozen before execution and refused a new pointer after; a stale or
foreign execution, a changed parent result, a changed artifact and an output
swapped before reading each breaking the chain or the reading.

## INV-07 -- Replication: configuration independence, perturbation validity, agreement

> A replication is judged on three separate findings, and Research OS claims
> each only to the degree it can establish it:
>
> 1. **configuration independence** -- *proved*: a separate execution, bound
>    to a manifest frozen before it ran, was delivered (by the Research OS
>    runner, per its receipts) a configuration that differs from its parent
>    execution's in exactly what the manifest says it varies;
> 2. **perturbation validity** -- *attested, never proved*: that the
>    computation uses what was varied. The generic runtime cannot establish
>    that arbitrary code used a delivered seed or parameter in its
>    mathematics. Validity is the researcher's attestation
>    (`perturbation_attestation` on the declared command, frozen into the
>    manifest) and is recorded as `ATTESTED_BY_RESEARCHER`, never as proof;
> 3. **agreement** -- the scientific readings compared, recorded separately.
>
> A replication counts as an independent replication only with (1) and (2).
> Different output bytes, and anything a program reports about itself, are
> never evidence of either.

**Threat.** A second run read as independent on evidence that cannot
establish it: differing bytes (H6), or a program's report of what it used
(R2, Case C: a program that ignored its seed and echoed it into the receipt
object the first repair trusted was accepted as independent and SUPPORTS).

**Enforced by.** `empirical.independence_variables` (the intended variation,
from the two frozen preregistrations); `empirical.freeze_replication_manifest`;
`provenance.assess_replication` (the three findings from the two receipts and
the manifest); `empirical.assess_agreement`; `empirical.interpret` (not
independent -> `INSUFFICIENT` with the reason); `experiment.spec.CommandSpec`
(the attestation must name `seeds`, `implementation` or a declared
parameter).

**Attestation source for a plan-bound execution** (`docs/SCIENCE_EXECUTION.md`
§5). When an execution is bound to a declared capability through a frozen
plan, the researcher's attestation frozen into the manifest is that
capability's declared perturbations -- each carrying the researcher's sentence
on how the computation uses it, committed with the code it describes and
pinned by the capability digest in the plan (`attestation_source` in the
manifest). The meaning is unchanged: attested, never proved. An execution no
plan governs still takes it from the command's `perturbation_attestation`.

**Case C, exactly.** A program that is delivered seed 14, ignores it and
reports seed 14: with no attestation it is `INSUFFICIENT` (configuration
independent, perturbation unattested); with the researcher's attestation it
counts, recorded as `ATTESTED_BY_RESEARCHER` with what is not proved written
beside it, and with its identical values recorded as agreement. That is the
trust boundary: Research OS guarantees delivery to a separate execution, not
causal use.

**PASS requires.** A delivered-but-unattested seed and a program's own
receipt each not a replication; Case C recorded as attested, not proven; a
used seed with identical values independent and agreeing; a different result
independent and disagreeing; a variation the runner did not deliver not
configuration independence.

## INV-08 -- Evidence completeness, read by the gates themselves

> Required reviewer, search and replication evidence cannot be silently
> replaced, skipped, masked or inferred from another successful stage. A
> mandatory failed or missing stage remains visible to promotion and
> readiness logic. **The readiness gates themselves read the trusted objects
> behind a requirement** -- the review events, the executed retrievals, the
> replication chains -- and the presence of a row elsewhere in the database
> is never enough.

**Threat.** One stage's success hiding another's failure (M2), or readiness
inferred from a row existing (R3/R4: a FATAL objection missing from a live
review; R5: a replication row with no manifest and no receipt satisfied the
gate after upgrade).

**Enforced by.** `track._accumulate`, `track.finish_board`,
`runner.finish_review_board`, `stages.board_state`;
`PortfolioStore.live_reviews` (a review whose objection set cannot be shown
complete is never live); `gates._incomplete_review_events` (a live review
whose own standing objections do not match its count and severity is not
counted, and blocks even `PROMISING`); `gates._replication_met` (an executed
replication counts only with an admissible `ReplicationProvenance` --
not legacy, configuration independent, attested, and a chain
`provenance.replication_provenance` re-verified as it stands);
`runner._evaluate` (hands the gate those chains); `gates._science_chain_met`
over `sciencechain.science_chains` (an empirical measurement or replication
counts only on its re-verified science chain -- frozen contract, design and
capability-bound plan, receipt, validated result, decisive system-computed
outcome; `docs/SCIENCE_EXECUTION.md` §6); `gates._promising_unmet`,
`_validated_unmet`, `_human_ready_unmet`, `evaluate`, `permit`.

**PASS requires.** A board missing a reviewer not complete; a re-run
endorsement not masking a standing negative review; a legacy or incomplete
review never live; the gate refusing a live review missing its objection with
the database checks bypassed; an executed replication row without an
admissible chain -- absent, legacy, broken, unattested, or for another row --
not second-line verification.

## INV-09 -- Capability honesty

> An unsupported scientific operation produces an explicit, principled
> refusal or capability-limited state, never fabricated or substituted
> evidence. A missing or untrusted execution attestation is an evidence
> insufficiency, not a finding.

**Threat.** A missing capability papered over with something that looks like
evidence (R2: an echoed, model-origin receipt turned an unsupported
replication into supporting evidence).

**Enforced by** (existing, preserved): `runner.run_literature_audit` with no
source (`CAPABILITY_DENIED`), `EmpiricalConclusion.OPERATIONALLY_BLOCKED` and
the design refusal path, the objective cycle's refusal handling. Added:
`provenance.assess_replication` (no attested perturbation -> `INSUFFICIENT`
with its reason); `empirical.freeze_replication_manifest` (a primary with no
trusted receipt -> refused before anything runs, `CAPABILITY_DENIED`);
`routing.BudgetCapUnavailableError` (an uncappable provider is a capability
refusal, not spend).

**PASS requires.** The existing refusal tests passing; the unattested
replication `INSUFFICIENT`; the receipt-less primary's replication refused
before it runs; the uncappable provider refused.

## INV-10 -- Human authority

> Human-controlled operations -- promotion and release decisions, portfolio
> resume where it is human-owned, and budget or ceiling expansion -- stay
> outside autonomous self-granting authority. Nothing autonomous revives an
> idea on the strength of model-writable prose.

**Threat.** The machine raises its own ceiling, resumes itself, or promotes
its own work -- or revives work on a sentence nothing structural stands
behind (R8: legacy budget parks; and its neighbour, the lineage-room reviver
parsing a status out of model-writable `revisit_if`).

**Enforced by** (existing, preserved): no autonomous module imports the
promotion doors; `budgets.explicit` set only by `researchctl runtime budget`;
the authority scans. Added: `tick._revive_budget_parks` reads a ceiling a
person raised and revives only a structural budget park;
`frontier.revive_for_lineage_room` reads `park_reason` and `resume_status`,
never prose; `sql/0045` marks every park with no structural reason
`legacy_unknown`, revived by nothing autonomous and shown as a person's
decision.

**PASS requires.** The authority suites passing; a budget park revived only
by a ceiling a person raised; a legacy park revived by no ceiling or lineage
change and displayed as needing a person; model prose naming a lineage-room
status reviving nothing.

---

## Defect to invariant map

| finding | what failed | invariant(s) |
|---|---|---|
| H1 second terminology path from cited keys | a retried or identical retrieval counted as independent | INV-05, INV-08 |
| H2 forged bank provenance | model text created trusted page structure | INV-02 |
| H3 live stages reclaimed as crashed | elapsed time treated as proof of death | INV-03 |
| H4 meta-review applied without its row | an answer affected state with no durable record | INV-04 |
| H5 timed-out calls released their reservation | unknown spend treated as zero | INV-01 |
| H6 timestamp-different replication | byte inequality treated as independence | INV-07 |
| M1 confidence from claimed queries | model prose counted as searches | INV-05, INV-02 |
| M2 reviewer failure masked by a later success | a mandatory failure disappeared | INV-08 |
| M3 re-run objections on the first review | one call's record reused for another; still reproducible with identical words until the second round | INV-04 |
| M4 budget-parked ideas never revived | a structural block reason was prose | INV-10 |
| R1 uncapped delegated and objective calls | an estimate reserved in front of an uncapped provider; spend past every ceiling, and hidden after lost settlement | INV-01 |
| R2 model-composed receipt forgery | program output, echoing model input, accepted as execution provenance | INV-02, INV-07, INV-09 |
| R3 FATAL objection deduplicated as MINOR | objection provenance collapsed across reviews by text | INV-04, INV-08 |
| R4 review completed without its objection | review and objections written in separate transactions | INV-04, INV-08 |
| R5 legacy replication evidence trusted | the gate read a row, not the trusted chain behind it | INV-07, INV-08 |
| R6 manifest repointed after evidence | ancestry not frozen, and not re-verified at readiness | INV-06, INV-07 |
| R7 reordered query as second terminology | a digest over case and whitespace called "different terminology" | INV-05 |
| R8 legacy budget parks stranded | ambiguous history neither revived nor marked | INV-10 |
| R9 lost insert race leaked a database error | a lost race surfaced as an operational failure | INV-03 |

## Status at the second integrity round (2026-09-27)

What was done to show each invariant, on the candidate that closes R1-R9
(the counts and logs are kept outside the repository, beside the frozen
evidence, in `~/.local/state/research-os-qualification/integrity-repair-v2/`,
so this document does not change after the gates that check it have run):

- every R-finding has a permanent reproduction named in
  `docs/architecture_invariants.yaml`, plus neighbouring attacks, in
  `tests/test_integrity_v2_*.py` and the rewritten
  `tests/test_integrity_replication_causality.py`;
- the reviewer's own frozen harness (`test_adversarial.py`, which asserts
  each defect *exists*) passes in full against `8e92e8c` and fails its defect
  assertions against the new candidate, each failure classified there as the
  defect closed or its code path removed;
- `tests/integrity_mutations.py` holds the first round's mutants, updated
  where the code moved, and new ones attacking every enforcement point this
  round added -- the provider cap in the router and the wrapper, the
  uncappable-adapter refusal, the database cap trigger, the atomic review
  event in the store and the database, legacy review liveness, the gate's
  completeness check, the delivered-configuration and attestation findings,
  the gate's chain requirement, the manifest pointer freeze, the output
  check, the receipt parent trigger, the distinct-path fold and prefix
  rules, the legacy park reason and the lost-race translation;
- the full suite forward and in reverse, the targeted integrity suite, the
  migration upgrades from 0036 and 0042, and lint and format.

## What these invariants do not claim

- They do not make a model's judgement correct. A gate that checks the right
  kinds of evidence exist can be satisfied by work that is thorough and wrong.
- **INV-01's bound is the provider's cap, enforced by the provider.**
  Research OS guarantees that the maximum it *authorises* -- the cap it hands
  every provider -- never exceeds the reservation, and that reservations
  never exceed any scope's limit. How finely a provider honours its cap is
  the provider's own enforcement: the Claude CLI checks `--max-budget-usd`
  between model responses, so the response in progress when the cap is
  crossed completes and is billed. Such an overshoot is recorded at what the
  provider reported and marked `reported_over_reservation`, and it is the one
  way actual billing can exceed a ceiling. The human-driven v1 commands
  (`researchctl auto`, `research`, `propose`, `paper`, `assess`) hold no
  Research OS monetary reservation and are outside INV-01; every autonomous
  route into a provider is inside it.
- INV-01 errs towards over-counting: unknown outcomes are charged their whole
  reservation, visibly (`settlement_basis`) and correctably by a person.
- **INV-07 does not prove causal use.** Configuration independence is proved
  from what the runner delivered; perturbation validity is the researcher's
  attestation. A false attestation is outside what Research OS can detect,
  and the record says *attested*. The attestation is per declared input: an
  attested composed input counts even when the part of it that changed is
  one the computation ignores.
- A replication of a primary interpreted before trusted receipts cannot be
  established as independent; such an idea needs a new version for a fresh
  primary.
- **INV-05 is lexical.** It cannot tell synonyms apart and does not claim
  independent meaning.
- INV-03's session lock is a liveness signal of the process's database
  session. A process whose session was closed while it kept running is fenced:
  its later writes are refused, and an already started provider call may be
  paid for twice -- within its cap, and never as a second scientific record.
- A legacy park (`legacy_unknown`) is not revived by anything autonomous. Its
  history is kept; what happens to it is a person's decision.
- An action recorded with no owner at all -- only possible outside
  `track.advance_idea` -- cannot be proved alive or dead and is still
  reclaimed by age.
