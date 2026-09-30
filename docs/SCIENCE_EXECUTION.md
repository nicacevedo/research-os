# Research OS -- empirical science execution (v1)

Live specification of how an autonomous idea becomes a measured, replicated,
system-computed result: typed scientific capabilities, the three frozen
objects, mechanical capability resolution, trusted execution, result
validation, deterministic outcomes, replication, and the gates that read all
of it. `docs/CAPSULE.md` remains authoritative on capsule science;
`docs/ARCHITECTURE_INVARIANTS.md` on the integrity substrate this builds on,
which this layer uses and does not redesign.

```text
independent idea -> dedup -> falsification -> literature -> curation
  -> CAPABILITY ENVELOPE (derived)         what one execution / one campaign holds (§1a)
  -> proposed analysis + stopping rule     by the analysis author, shown the envelope
  -> PRE-FREEZE SHAPE CHECK                VALID | EXECUTION_SHAPE_MISMATCH | CAPABILITY_LIMITED (§2a)
       (a mismatch goes back to the author once; nothing refused is frozen)
  -> SCIENTIFIC CONTRACT (frozen)          what is tested, bound to the envelope
  -> EXPERIMENTAL DESIGN (frozen)          how it is tested
  -> CAPABILITY RESOLUTION                 EXECUTABLE + exact binding | CAPABILITY_LIMITED + exact unmet
  -> EXECUTION PLAN (frozen)               how the design maps onto a declared capability
       or EXECUTION CAMPAIGN (frozen)     several units of that capability (§3a)
  -> trusted execution (receipt names the plan)      -- one receipt per unit
  -> result validated against the capability's declared schema  -- per unit
  -> (campaign) deterministic aggregation by the capability's declared rule
  -> system-computed PRIMARY OUTCOME
  -> replication: frozen plan + manifest, separate trusted execution, receipt
  -> replication assessment: VERIFIED + ATTESTED, and MEASURED agreement
  -> scientific review board -> meta-review -> deterministic gate -> HUMAN_READY
```

Code: `research_os/capability.py` (generic, kernel-level),
`research_os/portfolio/sciencechain.py` (objects, verification, outcomes,
gate records), `research_os/portfolio/campaign.py` (campaign compilation and
aggregation), `research_os/portfolio/empirical.py` (the route),
`research_os/portfolio/gates.py` (the gate), `sql/0047_science_chain.sql`
and `sql/0048_science_campaigns.sql` (the database's own refusals), `research_os/portfolio/qualification.py` and
`qualification_v1.yaml` (the v1 qualification contract).

---

## 1. Typed scientific capabilities

A science repository declares what it can execute and observe in
`research-capabilities.yaml` at its root. Research OS reads it **from a
committed tree** (`git cat-file` at a commit), never from a working copy or a
worktree a command ran in, and pins that commit.

Two keys, held by two parties, both required before anything runs:

| file | where | who | decides |
|---|---|---|---|
| `experiments.yaml` | host config home, outside every worktree | the operator | WHAT MAY RUN: program, argv, parameters, time ceiling |
| `research-capabilities.yaml` | the science repository, committed | the researcher | WHAT IT MEASURES: result artifact and schema, typed observables, inputs, determinism, attested perturbations, resources |

A capability names the host command that runs it. The operator's authority
over what executes (`docs/EXPERIMENTS.md`) is therefore unchanged; the
capability adds, in types a machine can check, what the execution produces.

### Schema `research-os-capabilities-v1`

```yaml
schema: research-os-capabilities-v1
capabilities:
  - id: domain.name            # ^[a-z][a-z0-9]*([.-][a-z0-9]+)*$
    version: 1
    title: ...
    command: host-command      # must be declared in experiments.yaml
    parameters: [{name, description, unit}]   # optional notes; if present, exactly the command's parameters
    inputs: [{path, description}]             # immutable inputs: tracked files, hashed at the pinned commit
    result:
      artifact: results/x.json                # a declared output of the command ...
      # artifact_parameter: out               # ... or the path parameter that names it
      format: json
      schema: {...}            # REQUIRED; the closed subset experiment.generated honours
    observables:
      - {name, kind: scalar, path, type: number|integer|string|boolean, unit, deterministic}
      - {name, kind: records, path, fields: [{name, type, unit, deterministic, description}]}
    determinism: deterministic | seeded | nondeterministic
    determinism_notes: ...
    replication:
      perturbations:           # the researcher's ATTESTATION, one sentence each
        - {kind: seeds, description}
        - {kind: parameter, name, description}
        - {kind: implementation, description}
      comparison: same_outcome
    resources: {timeout_seconds, cpus, memory_mb}
    execution:                 # optional: what ONE execution holds, at most (§1a)
      records: [{observable: solves, max: 240}]
      inputs:
        - name: instances      # a bounded input of one execution ...
          observable: solves
          max: 4
          fields: [instance_label, n, p, seed, ...]   # ... and the record fields it fixes
          across_units:        # what each campaign unit difference renews
            - {varies: plan, fields: [instance_label, n, p, seed, ...]}
            - {varies: seeds, fields: [instance_label, seed]}
    campaign:                  # optional: several executions may form one measurement (§3a)
      max_units: 6
      unit_varies: [seeds, plan]              # attested perturbations units may differ in
      aggregation:
        - {observable: solves, rule: concatenate, identity: [instance_label, ...]}
        - {observable: cells, rule: sum}      # numeric scalars: sum | min | max
```

A `campaign` block is checked when read: `unit_varies` is a subset of the
declaration's own attested perturbations and never `implementation` (every
unit runs one command); every aggregation names a declared observable once;
records concatenate and their `identity` names declared fields; scalars are
numbers. Absent, a capability runs one execution per measurement, and the
block is omitted from the capability digest so no earlier digest moves.

Checked when read (`parse_manifest`): strict UTF-8, no duplicate keys, closed
objects, unique ids and commands (a command backs at most one capability),
every observable present at its path in the declared result schema with a
compatible type, every perturbation carrying its attestation sentence, no
schema keyword the checker does not honour. Checked against the host
(`check_against_command`): the command exists, parameters agree, the result
artifact is something the command writes (so the runner hashes it into the
receipt), attested parameters exist, the capability's time need fits the
command's ceiling.

An `execution` block is checked when read too: every bound names a declared
`records` observable and only its declared fields; a field is bounded by one
input at most; what a campaign unit renews is a difference
`campaign.unit_varies` allows and a field of that input; a declaration with
no campaign states no renewal. Absent, it is omitted from the capability
digest, so no earlier digest moves (`cg.cells@1` at CG 5eb3923 still hashes to
the `rcap-v1:84ccf7cc...` the second final qualification recorded).

`researchctl experiment capabilities PROJECT [--commit SHA]` prints each
declared capability, its digest and whether the host can run it -- the check a
person makes before a run.

Nothing in `capability.py` names any science. The first capability,
`cg.cells@1`, lives in the column-generation repository.

### Resolution: EXECUTABLE or CAPABILITY_LIMITED

`capability.resolve(Requirements, loaded=..., commands=...)` is ordinary,
deterministic code with exactly two answers:

- `EXECUTABLE` with a **Binding**: capability id, version, digest
  (`rcap-v1:`), manifest sha256, pinned commit, result path, which declared
  observable each analysis observable binds to, and which fields read are
  declared nondeterministic;
- `CAPABILITY_LIMITED` with every **Unmet** requirement of every candidate,
  said exactly ("reads field 'duality_gap', which 'solves' does not declare").

The requirements come from the frozen analysis
(`sciencechain.requirements_from_analysis`): for each observable, its source,
kind and path; every field read anywhere (required fields, inclusion rules,
reduction fields, regression response and terms, `where` clauses); and the
subset read as numbers. Plus, once known: the design's command and parameter
values, a replication's intended variation (in the attestation vocabulary)
and the host's per-experiment time ceiling.

**A model cannot declare that an observable exists.** The analysis designer
is shown the typed catalogue (`catalogue_lines`) and told that only these
observables exist; resolution runs right after the analysis is frozen and
before any design is asked for, and an analysis that reads anything no
capability declares is `CAPABILITY_LIMITED` -- the contract is blocked with
the exact unmet list, the analysis stays frozen, and asking again costs
nothing until the manifest or the host's commands change. (A change that
moves the capability envelope the analysis was checked against supersedes
the unread contract instead, and the analysis is authored and checked again:
§2a.)

A project whose repository declares **no** manifest runs the pre-v1 route: it
still measures, and its evidence carries no plan, so no v1 gate accepts it
(§6). An **invalid** manifest is `CAPABILITY_LIMITED`, never "absent".

## 1a. What one execution holds: execution bounds and the capability envelope

The second final qualification (66d5704) had two independently generated
lineages reach the evidence stage. Both analysis authors froze
`fixed_single_execution` for a support requirement one `cg.cells@1`
execution cannot hold -- ten instances where a plan holds four, twelve
distinct seeds where one execution draws four -- and the experiment
designer, the only role that had been shown the per-execution bound (in the
host command's plan schema), refused both, correctly, after the analysis
could no longer change. A campaign within the human-set bounds could have
held either. The bound existed; it was not stated anywhere the analysis
author could compute with.

**Execution bounds** are the researcher's statement, in the declaration, of
what one execution holds -- in terms of the records an analysis reads:

- `records` -- at most this many records of a `records` observable;
- `inputs` -- a bounded input of one execution (instances in a plan, noise
  draws, grid points): at most `max` of it, and the record fields it fixes.
  One execution's records hold at most `max` distinct combinations of those
  fields, and records selected by *different* values of one of them come
  from *different* slots of it -- two analyses of two families share the
  four instances, they do not each get four;
- `across_units` -- which of those fields each campaign unit difference
  renews. Another plan is other instances altogether; another seed offset
  draws new instances of the same sizes. Not stated means every allowed
  difference renews everything (the envelope's upper bound, never a
  refusal); stated and empty means no unit adds a new value.

Like every other part of the declaration, the bounds are attested rather
than proved: a declaration that says one execution holds more than its
program writes is a wrong declaration, found when the data are read. `cg.cells@1`
states the bounds its host command's plan schema already enforced -- four
instances, five lambda ratios, two arms, four tolerances, three repetitions,
240 solves -- and CG's own tests check them against that schema and the
code. No capacity was added.

**The envelope** (`capability.execution_envelope`, schema
`research-os-execution-envelope-v1`, digest `renv-v1:`) is derived by
ordinary code from three things and nothing else: the declaration committed
at the pinned commit, the host's declared commands (whether each backs its
capability, and its time ceiling) and the human-set bounds
(`max_experiment_seconds`, `max_campaign_units`, `max_campaign_seconds`). Per
capability it states what one execution holds and may run for, whether a
campaign is available here and of how many units (the smallest of the
capability's limit, the person's unit bound, and how many unit ceilings fit
the person's campaign time -- the arithmetic the campaign compiler refuses
beyond), what units may differ in and what each difference renews, which
observables combine across units, and the attested perturbations. A model
may be shown it; nothing a model writes is an input to it, so nothing a
model writes can enlarge it. Its digest covers the commit, the manifest's
sha256 and the bounds: any of them changed is another envelope.

## 2. The three frozen objects

Each is content-addressed: its digest is `<kind>-v1:<sha256 of its canonical
JSON bytes>`, the artifact store holds exactly those bytes, and the row in
`science_objects` is keyed by the digest. A changed object is a new object.

**Scientific contract** (`rscontract-v1:`, schema
`research-os-science-contract-v1`) -- WHAT: the hypothesis (idea version
record and content digest), the question, the estimand, the population, the
target claim, the primary statistic, the success criterion and failure
criterion (comparator, threshold, whether it applies to the point estimate or
to every value of a frozen uncertainty interval), the explicit
**inconclusive region** (neither criterion, both, or unmet frozen support /
undefined statistic), the stopping rule, the missing-data rule, and the
analysis digest. Derived only from the idea version and the frozen analysis,
so a primary and every replication of it share one contract.

**Experimental design** (`rsdesign-v1:`) -- HOW: the contract digest; for a
replication, the primary's design digest; the primary analysis definition
(observables, reductions, support, analysis digest); the required observables;
the exclusion rules; the data (dataset identity, sampling); comparison groups
(manipulated variables and levels), controls and measured variables;
repetitions; the seed policy; the perturbation (replication); the
falsification criterion; the measurement's parameters (composed documents by
digest).

**Execution plan** (`rsplan-v1:`) -- WHERE: the design and contract digests;
the full capability binding including the declaration itself and its result
schema; the pinned code commit; the host command's identity and digest, argv
and parameters; the exact immutable inputs (composed documents and declared
input artifacts, each by sha256); expected outputs and the result artifact;
the configuration (env, environment, seeds, cwd); implementation fields; the
specification and variation digests.

### Order and ancestry, enforced below the application (`sql/0047`)

- a DESIGN's parent must be a CONTRACT and a PLAN's a DESIGN, of the same
  project and idea version, frozen no later than it (`clock_timestamp`);
- a PLAN carries exactly one capability and one specification digest;
- objects are immutable (no update, no delete);
- an experiment's `plan_digest` must be a PLAN of its idea version whose
  specification digest is the experiment's, and never changes;
- a receipt of a plan-bound execution must name that plan.

So: contract frozen -> design frozen -> capability binding -> plan frozen ->
experiment recorded -> execution -> receipt. `empirical._freeze_science_chain`
builds them in that order and resolves the binding between design and plan;
a design whose command, parameters or (replication) variation no declared
capability supports gets **no plan** and nothing runs.

An upstream change is a new identity: a revised idea version derives a new
contract digest, hence a new design and plan, and the gate reads only the
chains of the version it evaluates. Evidence bound to the old chain never
transfers.

### Verification

`sciencechain.verify_plan` re-loads plan, design and contract, re-hashes each
artifact against its address, checks canonical form, schema and identity,
checks every link (plan -> design -> contract, specification digest, role,
capability declaration digest, command) and -- given the idea version and the
contract row's verified analysis -- **re-derives** the contract object and the
design's analysis digest, so the chain cannot say something its contract row
does not. It runs before execution (`submit`), before reading (`interpret`),
and at readiness (the gate).

## 2a. The analysis is checked against the envelope before it is frozen

```text
scientific evidence requirement      the analysis's own support requirements
-> committed capability envelope      §1a, derived
-> proposed analysis / stopping rule  the analysis author, shown the envelope
-> mechanical feasibility check       portfolio.shape.check, ordinary code
-> revision, if the shape was wrong   the author again, shown exactly why
-> frozen executable analysis         bound to the envelope it was checked against
-> design -> execution or campaign
```

**Shown before it chooses.** Under a manifest, the analysis author's prompt
(`analysis_designer@4`) carries the envelope beside the typed catalogue,
before it chooses a stopping rule, a sample or perturbations. It states the
execution shape it plans (`execution_shape`: the capability, the number of
units -- exactly, or left to the design -- what differs between units, the
per-execution capacity it relies on, a rationale). Scientific independence
is kept: it is told to decide what the data must show from the question, not
from the envelope, and that a requirement nothing here can hold is to be
kept and recorded as such rather than weakened.

**Checked before it freezes** (`portfolio.shape`). From the analysis's
support requirements (`min_records`, `min_distinct`) and its observables'
inclusion rules, ordinary code computes a *lower bound* on what the
analysis needs of each bounded quantity -- observables selecting different
values of one field of a bounded input need different slots of it, so their
needs add -- and compares it with what one execution, and what an allowed
campaign in the stated shape, can hold:

| verdict | when | then |
|---|---|---|
| `VALID_SINGLE_EXECUTION` | one execution holds every requirement | frozen |
| `VALID_CAMPAIGN` | an allowed campaign, in the stated shape, holds them | frozen |
| `EXECUTION_SHAPE_MISMATCH` | the stated shape cannot -- or claims a capacity, a campaign size or a unit difference the envelope does not provide -- and another allowed shape can | refused; the author is shown why, once |
| `CAPABILITY_LIMITED` | neither one execution nor any allowed campaign can | refused; not asked again under this envelope |
| `UNRESOLVED` | nothing to check (not analysable, or it reads nothing a capability declares) | frozen, and capability resolution then blocks it exactly as before (§1) |

A refusal is a certainty about that shape; a valid verdict means only that
the envelope does not rule it out -- the design is still compiled and
checked (§3a), and the data still read against the frozen support. A
model's claim is checked and never used: a stated per-execution capacity
above the committed one is refused even when the requirement would fit.

**Nothing refused is frozen, and nothing is converted.** A refused proposal
is recorded as an immutable **draft** (`analysis_drafts`, `sql/0049`), with
the envelope it was refused under and the structured reason -- never as a
contract. The database refuses a contract whose analysis is a draft refused
under the same envelope, or under any envelope when the contract names
none, and refuses a draft while a contract is live for its version and role.
The analysis is never rewritten for its author, a single execution is never
turned into a campaign for it, and the hypothesis is not touched.

**The revision uses the existing retry, and is bounded.** A mismatch fails
the evidence stage as `MODEL_OUTPUT_INVALID`, which the queue already
retries; the retry's author is shown its refused proposal and the
mechanical reason (`refused_analysis`, fenced as refused output) and may
revise how it is executed. At most two proposals of one version and role are
refused under one envelope (`shape.MAX_REFUSED_DRAFTS`): the second refusal
is `POLICY_REFUSED`, and every later attempt under that envelope refuses at
no cost without asking anyone. `CAPABILITY_LIMITED` is `CAPABILITY_DENIED`
at once and, under the same envelope, again at no cost -- an impossible
analysis is not bought twice. A changed envelope is a new question and the
drafts under the old one do not count against it. Both refusals block the
idea (`BLOCKED_EXTERNAL`) under the existing stage-failure rules;
`researchctl portfolio resume` is what reconsiders it.

**Frozen, and bound.** A frozen analysis's document records the check it
passed (`execution_shape_check`) and the drafts it revises, with which of
their fields it changed (`revises`); its contract row carries the envelope
digest (`scientific_contracts.envelope_digest`), which the database never
lets change, and `scicontract.verify` re-reads the binding from the bytes
and refuses a record whose verdict is not one an analysis is frozen under.
Before a design exists, a contract whose envelope is no longer the one in
force -- another commit, declaration, command ceiling or human bound -- is
not designed under it: unread, it is superseded and the analysis authored
and checked again (the rule a retired prompt already has). Once designed,
the plan pins its own commit and declaration; once read, nothing is
re-decided. The contract object carries `execution_shape` when it was
stated, so the preregistered stopping rule says how it is realised. A
replication inherits its primary's analysis and is checked against the
envelope in force; not being revisable, an inherited analysis the envelope
can no longer execute is refused with nothing frozen.

**Realised, not reinterpreted.** The experiment designer (`@10`) is shown the
frozen execution shape and the smallest campaign the envelope says can hold
the support; a campaign design must have exactly the stated number of units,
differ between units only in the stated differences, and be one the
envelope says could hold the frozen support, or it is refused before it is
compiled (a retry). What a pair of units differs in is *everything* their
specifications differ in (§3a), never only what the capability attests, and
every pair that differs in anything the shape does not allow is reported.
Nothing here relaxes what the compiler checks.

## 3. Trusted execution

The existing runner and receipt (`provenance.write_receipt`, INV-02) with
three additions for a plan-bound execution:

- the disposable workspace is cut from the **plan's pinned commit**, whatever
  HEAD is by then, and the runner refuses if the workspace's base commit is
  not the plan's;
- every declared input artifact is re-hashed in the workspace against the
  plan before the process starts;
- the receipt carries a `science` block -- contract, design and plan digests,
  capability ref and digest, code commit, result artifact -- and its row the
  plan digest (checked on read and by the database).

The receipt already binds the action, run and work identity, the command
digest, code commit, delivered configuration, inputs and every output digest
at exit. Nothing a program writes is read into it.

## 3a. Multi-execution campaigns

A design whose valid sample -- independent seeds, more instances or
conditions than one bounded execution holds -- needs several executions of
the one capability it binds is a **campaign**. The first final
qualification (538c54f) was refused on exactly such a design: one execution
of `cg.cells@1` could have held the sample only by repeating identical
solves. The abstraction is the minimum, and not a workflow engine:

```text
scientific contract      stopping_rule: fixed_campaign
  -> experimental design  its units: each overrides parameters and/or seeds
  -> frozen campaign      a PLAN (schema research-os-execution-campaign-v1)
       -> unit 0 .. n-1   each the trusted runner's execution, its own receipt
       -> unit results    each validated against the declared schema
  -> aggregation          the capability's declared rule, by code
  -> primary outcome      bound to the campaign and every unit's receipt
```

**Frozen.** The campaign plan binds everything a plan binds once -- contract
and design digests, capability, declaration and digest, code commit, command,
immutable input artifacts -- and, per unit, its argv, parameters, composed
inputs, expected outputs, result artifact, configuration (env, environment,
seeds, cwd), implementation bounds and its own specification and variation
digests, plus every token it differs from unit 0 in. Then the
aggregation rule for every observable the analysis reads, the missing-unit
rule (`refuse`), the stopping rule and the bounded resources (units, the sum
of the units' time ceilings, work items). Its digest is its content; the
campaign's specification digest is a function of its units'
(`campaign.campaign_spec_digest`); the units are frozen twice more, row by
row in `science_campaign_units` and in the design (`sql/0048`). Changing one
unit is a new plan, and evidence bound to the old one does not transfer.

**Compiled before anything runs** (`campaign.compile_campaign`), in order:
the contract's stopping rule is `fixed_campaign`; the capability declares
campaign support and allows this many units; every pair of units differs,
in something the capability *attests* its computation uses, and in nothing
the frozen analysis does not allow -- units that are the same execution are
refused, units that differ only in something unattested (a seed a
deterministic program ignores) are `CAPABILITY_LIMITED`: the capability
cannot provide the independent observations the design asks for, and units
that differ in anything not allowed are refused whatever else they differ
in; every observable the analysis reads has a declared rule to combine it;
and the campaign fits the human-set bounds `bounds.max_campaign_units` and
`bounds.max_campaign_seconds` (else `BUDGET_LIMITED`). A refusal that a
different design could avoid is a retry; nothing ran.

**Varied only as preregistered.** For every pair of a campaign's units:

```text
actual differences  <=  allowed differences     one allowed difference never
actual differences  !=  {}                      excuses another that is not
```

*Actual* is derived by code from the two frozen specifications
(`campaign.configuration`), never from a list anyone wrote, and each part of
a specification the program can read is attributed to the input that put it
there: the seeds and the `RESEARCH_OS_SEED_<n>` variables delivering them to
`seeds`; a composed document -- its content, by digest -- to the parameter
that composed it, so a change anywhere inside it (a slope nested in a plan)
is a difference in that parameter, and is reported at its path
(`plan (at plan.slope)`); an argument filled from a placeholder of the host
command's declared argv to that parameter, and a recorded parameter value to
its name. Anything no declared input accounts for -- a literal argument,
another environment variable, the expected outputs -- is a `spec.*`
difference no analysis can allow. Not science, and not compared: where a
unit ran (`cwd`), its name and label, its implementation bounds
(`timeout_seconds`, `resources`), identifiers and digests derived from the
rest, and the parameter the capability declares as its result's location
(`result.artifact_parameter`). *Allowed* is the frozen analysis's
`execution_shape.unit_varies` -- or, when it states none, whatever the
capability attests -- and never more than the capability attests
(`campaign.allowed_variation`). **Allowed is not required:** a v1 analysis
marks no difference as one every pair must have, so units differing in some
of what is allowed, and in nothing else, realise it; whether that can hold
the frozen support is §2a's question, and the data's.

The rule is applied three times, each independently of the others: by the
designer-side check against the frozen shape (§2a), by the compiler, and --
re-derived from the frozen plan's own bytes against the frozen contract's
execution shape and the plan's capability declaration -- by
`sciencechain.verify_plan`, which runs before any unit starts, before the
campaign is read and at readiness. A campaign plan frozen under a weaker
rule, recovered after a crash, retried, or a replication's, never runs and
is never admissible. The frozen analysis and plan are not changed by any of
this; a campaign that does not conform is refused before it runs.

**Run once, all of it.** Before unit 0 starts, one work item per unit left is
reserved against every applicable ledger, so a campaign the execution
authority cannot cover never starts. Each unit is then an ordinary plan-bound
execution (`empirical._run_unit`): its own idempotency key, a fresh checkout
of the plan's commit, its inputs re-hashed, its own job, its own receipt
naming the plan, the unit and the attempt (`sql/0048` checks the unit's
specification is the one the campaign froze for it), the canonical
fingerprint checked around it. Its result is stored by content as the runner
hashed it (`campaign_unit_results`) before the workspace goes. A unit that
fails ends the attempt as an operational failure and nothing is read -- the
next attempt runs every unit again; a unit whose result is not valid
evidence stops the campaign early.

**Read once.** `empirical.interpret_campaign` re-validates every unit's
stored result against the declared schema, combines them by the frozen rule
(`campaign.combine`: records concatenated in unit order, scalars summed or
their minimum or maximum), refuses an observation whose declared identity two
units both produced, and applies the frozen analysis once to the combined
result. The outcome is recorded before the reading, anchored on the
campaign's last unit, with every unit's receipt and result sha in
`science_outcome_units`; at commit the database refuses a reading that does
not name every unit of its plan, all of one attempt. Anything short of a
full, valid campaign is `INVALID_EVIDENCE`: a partial campaign is never read
as the primary analysis -- the contract's `on_missing` has one value,
`INSUFFICIENT`, and no rule allows a subset.

**Replicated unit by unit.** A replication of a campaign is a campaign of as
many units; unit *i* replicates the primary's unit *i*. Each replication unit
runs under its own manifest, frozen immediately before it, whose parent is
the primary's execution of the same unit as the primary's reading names it
(and the database checks the parent's unit index). No replication unit may
be *any* primary unit run again (`campaign.pair_with_primary`, and
`empirical._design_campaign` with resources removed): the primary's seeds
shifted by one position differ pair by pair and re-measure the primary's own
observations. Configuration independence and attestation are established per
pair (`provenance.assess_replication`), and the campaign counts as
independent only if every pair does; agreement is the frozen `same_outcome`
rule over the two campaign outcomes. The replication's own units are held to
the variation rule above exactly as the primary's are: its analysis is the
primary's, and so is what its units may differ in.

**Recovered, never guessed.** A unit's idempotency key names its attempt,
and the reconciler recovers only a job this attempt's invocation could have
created -- submitted after it began, and not bound by a receipt to another
attempt or unit: the unit's specification digest is the same in every
attempt. A job recovered without this unit's receipt is not read; the
attempt ends as an operational failure. A unit whose receipt was written and
whose result was not stored before the process stopped is stored on resume
if it was the last to run (its bytes are still where the runner hashed
them), and otherwise the attempt did not complete. None of these becomes a
reading.

**A campaign contract is a campaign.** Under `fixed_campaign` a
single-execution design is refused (a retry): the frozen rule said the
sample needs several executions. The designer is shown, per campaign
capability, how many units the human-set bounds permit on this host
(`empirical.campaign_bound_lines`: the capability's limit, `max_campaign_units`,
and how many unit time ceilings fit `max_campaign_seconds`), so it is not
paid to propose one the compiler must refuse.

**Re-verified at readiness.** For a campaign, the gate's chain
(`sciencechain.campaign_reading_problems`) re-verifies every unit receipt,
checks each stored result re-hashes and is the output its receipt recorded,
re-validates it, and rebuilds the combined result from those bytes, which
must hash to what the outcome names. The v1 qualification gates read campaign
evidence with unchanged meaning: every unit is a completed plan-bound
execution with a verifying receipt (Q12, Q13, Q16, Q17), the outcome's
result is the combined result that passed validation (Q14), and the chain
the gates read is the one above (Q15, Q18, Q25, Q26). The specification file
and its digest are unchanged.

## 4. Result validation, then the deterministic outcome

Before any reading, `sciencechain.validate_result` requires that the result
artifact the plan names was hashed by the runner at exit, that the bytes read
now are those bytes, that they are strict JSON (no NaN/Infinity), that they
satisfy the capability's declared schema, and that every bound observable is
present where declared (a finite number for a scalar, a list of records for
records). Otherwise the outcome is `INVALID_EVIDENCE` and the decision rule
is never applied.

Then the frozen analysis is evaluated by `portfolio/analysis.py` (unchanged)
and mapped, by code, to one of seven states:

| state | when |
|---|---|
| `SUPPORTED` | success criterion holds, failure criterion does not |
| `REFUTED` | failure holds, success does not |
| `INCONCLUSIVE` | the inconclusive region: both, neither, or unmet frozen support / undefined statistic on a valid result |
| `EXECUTION_FAILED` | crash, timeout, non-zero exit, no executor |
| `CAPABILITY_LIMITED` | no declared capability can produce what the contract reads, or the host cannot run it |
| `INVALID_EVIDENCE` | the result is missing, malformed, off its schema, or the chain behind it broke |
| `BUDGET_LIMITED` | a budget a person set could not cover the next step |

Each determination is an immutable `science_outcomes` row plus a
content-addressed outcome document. A reading (`SUPPORTED`, `REFUTED`,
`INCONCLUSIVE`) must name its whole chain -- the three objects, the
capability, the receipt and the validated result's sha256 -- and the
database checks that the receipt is of that experiment under that plan and
that the design and contract are that plan's. One reading per receipt. The
outcome is recorded **before** the reading, so a crash leaves an outcome the
retry finds, never a reading without one.

No model is asked whether a threshold was crossed. Models may interpret,
critique and propose follow-ups afterwards; they cannot make an outcome.

## 5. Replication

```text
primary result -> replication design (frozen, names the primary's design)
  -> capability resolution: the variation must be one the capability attests
  -> replication plan (frozen) -> manifest (frozen before the run, sql/0041/0046)
  -> separate trusted execution -> receipt (names plan, manifest, parent receipt)
  -> result validation -> system-computed outcome
  -> assessment
```

The three findings of INV-07, unchanged in meaning:

- **VERIFIED** -- configuration independence: a separate execution, bound to a
  manifest frozen before it ran, was delivered a configuration differing from
  its parent's in exactly what the manifest varies (from the runner's
  receipts);
- **ATTESTED** -- perturbation validity: the researcher states the computation
  uses the varied input. For a plan-bound execution the attestation is the
  bound capability's declared perturbations (each with its sentence), frozen
  into the manifest with the capability digest (`attestation_source`); for a
  pre-v1 execution, the command's `perturbation_attestation`. Recorded as
  `ATTESTED_BY_RESEARCHER`, never as proof. Research OS does not claim to
  prove that arbitrary code used a parameter;
- **MEASURED** -- agreement under the capability's frozen comparison rule
  `same_outcome`: the two system-computed outcomes are the same state.

A replication that varies what its capability does not attest is refused
**before it runs** (`CAPABILITY_LIMITED`; a redesign may retry if the
capability attests anything, otherwise the idea is blocked). A replication
of a primary with no frozen plan is refused. Replication readiness requires
the current trusted chain: legacy or unverified evidence cannot satisfy it.

## 6. What the gate reads

`EVIDENCE_RULES[EMPIRICAL]` and `REPLICATION_RULES[EMPIRICAL]` require
`requires_science_chain`. `runner._evaluate` hands the gate
`sciencechain.science_chains(...)`, re-verified now for every executed
experiment and replication row: receipt present and verified, the experiment
still this version's live one and still naming the reading, the plan chain
verified, the receipt naming the plan and its commit, a recorded outcome
naming the same chain, its document re-hashing.

- `VALIDATED` (empirical) needs a substantive executed EXPERIMENT row whose
  chain is **admissible**: intact, capability-bound, decisive outcome
  (`SUPPORTED` or `REFUTED`). An `INCONCLUSIVE` or `INVALID_EVIDENCE` reading is
  recorded and does not validate.
- `HUMAN_READY` (empirical) additionally needs the replication row's own
  admissible science chain and its admissible replication provenance (INV-07).
- MEASURED disagreement is recorded on the chain and **disclosed as a note**,
  not a bar -- the same policy the gate already applies to the direction of
  evidence ("what a replication that contradicts its primary means is a
  question for a person").

`gates._science_chain_met` is mapped under INV-08 in
`docs/architecture_invariants.yaml`.

## 7. The v1 qualification contract

`research_os/portfolio/qualification_v1.yaml` -- 26 mandatory gates and the
advisory generalisation dimensions (second provider, second empirical domain,
mathematical track), each `automated`, `evidence` or `both`. HUMAN_READY is
mandatory (Q26): at least one idea descending from an autonomously originated
root must be HUMAN_READY now, by a gate-permitted meta-review disposition on
its current version, and that same lineage must carry the whole empirical
chain (falsification, executed retrieval, an allocator purchase, a primary and
a replication reading on intact chains with admissible replication
provenance, a complete board). The primary need not be SUPPORTED, and
replication agreement is reported, not required. Its sha256 is pinned in
`tests/test_qualification_spec.py`; the
zero-state snapshot of a live qualification records it, and evaluation refuses
a changed spec. `researchctl portfolio qualification PROJECT --evidence DIR`
evaluates it read-only (`--snapshot-zero-state` before the run,
`--isolation-report` after).

## 7a. Feasibility: planning metadata, not evidence

`portfolio/feasibility.py` gives each idea version one of
`CURRENTLY_EXECUTABLE`, `LIKELY_EXECUTABLE_WITH_CAMPAIGN`, `CAPABILITY_LIMITED`
or `UNKNOWN`, by ordinary code over the committed manifest and the idea's
structured requirement (`contracts.EvidenceNeeds`: new execution or existing
records, the catalogue fields a settling measurement must report, how many
independent draws), which the sharpening stage states against the catalogue
it is shown and which is stored beside the idea (`idea_evidence_needs`), not
in it -- or, once one exists, from its own contract's capability-limited
state under the current declared commands and manifest (a contract blocked
before they changed is a question worth asking again, not an answer). Every
capability that reports every field is considered: many independent draws
are `LIKELY_EXECUTABLE_WITH_CAMPAIGN` if any of them declares campaigns. A
model's words cannot make a field exist; nothing here changes a
hypothesis, freezes anything or rejects anything. The allocator subtracts a
configurable penalty from *advancement* work on a capability-limited idea
(`weights.capability_limited_penalty`), so scarce completion resources go
first to an equally strong direction this laboratory can test, and the
limited idea stays in the bank and is still advanced when nothing executable
of comparable value waits.

## 8. Invariants of this layer

Machine-readable in `docs/science_invariants.yaml`; held by
`tests/test_science_invariants.py` (every enforcement point exists, every
named test exists, every mutant in `tests/science_mutations.py` is mapped and
still applies).

- **SCI-01 capability honesty** -- no execution without a mechanical binding
  to a declared, host-authorised capability; an observable, field or numeric
  reading no capability declares is `CAPABILITY_LIMITED`, with the exact unmet
  requirement.
- **SCI-02 frozen chain** -- contract, design and plan are content-addressed,
  immutable and frozen in order, the database refuses anything else, the
  execution runs the plan's commit, and every link re-verifies before
  execution, reading and readiness.
- **SCI-03 validation before acceptance** -- a result is read under the rule
  only if it is the runner-hashed artifact and satisfies its declared schema.
- **SCI-04 system-computed outcomes** -- seven states, by code, bound to the
  chain, one reading per execution.
- **SCI-05 the gates read the chain** -- empirical VALIDATED and HUMAN_READY
  require admissible science chains.

The capability envelope and the pre-freeze check of §1a and §2a are held by
`tests/test_capability_planning.py`, the live-failure regression
`tests/test_capability_planning_regression.py` (against the committed
`cg.cells@1` declaration) and the no-cost stage-machine run
`tests/test_capability_planning_stage_machine_e2e.py`, and each enforcement
point by a mutant in `tests/capability_planning_mutations.py` (a fifth
harness, checked to still apply by
`test_every_capability_planning_mutant_still_applies`).

The campaign guarantees of §3a and the allocation and feasibility policy of
`docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` are held by
`tests/test_science_campaigns.py`, `tests/test_campaign_stage_machine_e2e.py`,
`tests/test_portfolio_allocation_lanes.py` and
`tests/test_portfolio_feasibility.py`, and each enforcement point by a mutant
in `tests/campaign_allocation_mutations.py` -- a fourth harness beside the
integrity, science and qualification ones, checked to still apply by
`test_every_campaign_and_allocation_mutant_still_applies`.

## 9. What this does not claim

- It does not prove a program writes what its declaration says: that is
  checked after execution, and a program whose output does not fit is
  `INVALID_EVIDENCE`.
- Attestation is attestation: a false one is outside what Research OS can
  detect, and the record says *attested*.
- It does not make a frozen analysis a good one. A thorough, wrong analysis
  passes every check here.
- A capability manifest is trusted as the researcher's because it is read
  from the canonical checkout's committed tree, which no autonomous component
  may move (the canonical fingerprint refuses any ref change). A person who
  commits a wrong manifest has made a wrong declaration.
- `repetitions` and the other design fields are what the designer stated and
  the plan realises; the data's support is checked by the frozen support
  requirements, not by trusting the statement.
