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
  -> SCIENTIFIC CONTRACT (frozen)          what is tested
  -> EXPERIMENTAL DESIGN (frozen)          how it is tested
  -> CAPABILITY RESOLUTION                 EXECUTABLE + exact binding | CAPABILITY_LIMITED + exact unmet
  -> EXECUTION PLAN (frozen)               how the design maps onto a declared capability
  -> trusted execution (receipt names the plan)
  -> result validated against the capability's declared schema
  -> system-computed PRIMARY OUTCOME
  -> replication: frozen plan + manifest, separate trusted execution, receipt
  -> replication assessment: VERIFIED + ATTESTED, and MEASURED agreement
  -> scientific review board -> meta-review -> deterministic gate -> HUMAN_READY
```

Code: `research_os/capability.py` (generic, kernel-level),
`research_os/portfolio/sciencechain.py` (objects, verification, outcomes,
gate records), `research_os/portfolio/empirical.py` (the route),
`research_os/portfolio/gates.py` (the gate), `sql/0047_science_chain.sql`
(the database's own refusals), `research_os/portfolio/qualification.py` and
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
```

Checked when read (`parse_manifest`): strict UTF-8, no duplicate keys, closed
objects, unique ids and commands (a command backs at most one capability),
every observable present at its path in the declared result schema with a
compatible type, every perturbation carrying its attestation sentence, no
schema keyword the checker does not honour. Checked against the host
(`check_against_command`): the command exists, parameters agree, the result
artifact is something the command writes (so the runner hashes it into the
receipt), attested parameters exist, the capability's time need fits the
command's ceiling.

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
nothing until the manifest or the host's commands change.

A project whose repository declares **no** manifest runs the pre-v1 route: it
still measures, and its evidence carries no plan, so no v1 gate accepts it
(§6). An **invalid** manifest is `CAPABILITY_LIMITED`, never "absent".

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
