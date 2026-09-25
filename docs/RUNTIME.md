# Research OS autonomous runtime (R5)

Live specification of `research_os.runtime`, `researchctl runtime` and
`researchd`. `docs/CAPSULE.md` remains authoritative on anything scientific;
where this document appears to disagree with it about science, it is wrong.

## 1. The authority model

Five kinds of state, five owners, and the separation is the design rather than a
tidy diagram drawn afterwards.

```text
SCIENTIFIC TRUTH   ->  Git-tracked capsule files in the project repository
OPERATIONAL TRUTH  ->  PostgreSQL
WORKFLOW STATE     ->  LangGraph checkpoints (in PostgreSQL, separate tables)
ARTIFACT BYTES     ->  content-addressed store under the data home
DERIVED INDEX      ->  SQLite (the literature index), rebuildable
```

The rule that makes it worth enforcing:

```text
near-100% operational autonomy  !=  100% epistemic authority
```

The runtime is meant to need no human hand for scheduling, retrying, recovering,
submitting, polling, collecting, routing or continuing. It is meant to be
*unable* to decide that a claim is true. Those are different properties, and the
second is not a limitation on the way to removing it.

Concretely, the runtime may not:

- write a capsule file that records scientific acceptance;
- author a `Review` with `reviewer_kind: human`;
- move a Claim to `accepted`;
- change a preregistered primary endpoint after seeing results;
- push or merge to a canonical branch.

Each of those is an `A2` action (§7) and requires a recorded human decision. The
check is not a convention: `research_os.runtime.kernel` is the only module that
can reach a capsule, it holds no write path for these objects, and
`tests/test_runtime_authority.py` asserts the boundary as a property of the
package rather than as a habit.

### Why PostgreSQL, and why not for science

The queue needs `for update skip locked`, leases need a server-side clock,
repository serialisation needs a lock that outlives the process holding it and
is released when that process dies. SQLite provides none of the three. This is
the demonstrated requirement that moved PostgreSQL off the postponed list in
`ARCHITECTURE.md` §12.

Nothing scientific moved with it. A runtime that could accept a Claim by
updating a row would be a runtime that can manufacture agreement, and the
provenance recorded afterwards would not undo it.

## 2. Layout

```text
research_os/runtime/
  __init__.py       schema version constant; imports nothing heavy
  config.py         DSN, artifact root, tunables, budget defaults
  clock.py          injectable time -- for observation only, never for deadlines
  ids.py            RRUN-/WORK-/EVT-/APRV-/IVK-/MCALL-/XJOB-/BDGT-/SCHED- ids
  db.py             pooled psycopg handle; the only place driver errors are classified
  migrations.py     numbered SQL, advisory-locked, checksum-verified
  sql/              the schema
  models.py         typed records; the enums the schema checks against
  store.py          projects, runs, events, approvals, model calls, jobs, schedules, providers
  queue.py          the durable work queue
  idempotency.py    the invocation ledger
  locks.py          advisory locks for repository/capsule/index mutation
  failures.py       the failure taxonomy and its retry policy
  findings.py       typed, citable, noncanonical runtime findings
  capsulewatch.py   noticing that a person changed the canonical science
```

Two modules that are *not* here and belong to the v1 layers on purpose:

```text
research_os/sandbox.py          OS-level containment for commands we did not write
research_os/proposal/basis.py   what a proposal rested on, and whether it still holds
```

The sandbox is a command runner's concern, and both the v1 coding pipeline and
the runtime's local executor go through it; putting it under
``research_os/runtime`` would have contained the autonomous path and left
``researchctl auto`` -- which has always run a project's acceptance commands --
uncontained. The proposal basis is scientific-object reasoning, and keeping it
out of ``promote.py`` preserves "nothing imports the one door except the command
a person runs".

Dependency direction, enforced by `tests/test_runtime_layering.py`:

```text
graphs  ->  capability interfaces  ->  runtime/adapters  ->  external systems
```

The scientific kernel depends on none of it. `research_os.runtime` imports the
kernel; the kernel does not import the runtime, and never may.

## 3. What the queue promises

**At-least-once execution with idempotent visible side effects.** Not
exactly-once. Between "the external world changed" and "our database knows it
changed" there is a window and a process can die in it; no queue design closes
it. What closes it is §4.

Four properties, each tested in `tests/test_runtime_queue.py`:

| property | why |
|---|---|
| claiming charges the attempt | work whose execution crashes the worker must run out of attempts, not out of workers |
| every deadline is computed by PostgreSQL | two workers with skewed clocks must not disagree about whether a lease is dead |
| an expired lease cannot be renewed | the renewer may already have been replaced; renewing would give one item two owners |
| completion is guarded by `lease_owner` | a slow worker must not overwrite the work of whoever took over |

A worker that dies stops renewing, its deadline passes, and
`reclaim_expired()` returns the item to the queue — or fails it as
`worker_crash` if its attempts are gone. Nothing has to detect that the process
died, which is good, because nothing reliably can.

## 4. Idempotent side effects

Every externally visible effect goes through `InvocationLedger.run`:

```text
claim(key)  ->  durable IN_FLIGHT row, before anything happens
  COMPLETED     -> return the stored result; perform nothing
  IN_FLIGHT     -> someone else holds it; this worker does not act
  ABANDONED     -> outcome unknown: reconcile, or refuse
  FAILED        -> the effect provably did not happen; retry is safe
```

The key is derived from the *action*'s stable identity —
`idempotency_key("slurm.submit", work_id, spec_digest)` — never from the attempt.
Keying on anything per-attempt yields a ledger that records every duplicate
faithfully and prevents none.

`ABANDONED` is not `FAILED`. It means the effect may or may not have happened.
Where the runtime can go and look (`squeue` knows whether a job exists, `git
log` knows whether a commit was made) a reconciler establishes the truth. Where
it cannot, the action is **refused**, because guessing wrong submits the same
experiment twice.

`tests/test_runtime_idempotency.py` proves this with a real child process that
performs a real side effect and is killed with `os._exit` before recording it.

## 5. Locking

| lock | scope | taken by |
|---|---|---|
| `REPOSITORY_MUTATION` | one canonical checkout | worktree creation, commits, integration |
| `PROJECT_CAPSULE` | one project's `.research/` | capsule writes |
| `DERIVED_INDEX` | one rebuildable index | literature index rebuilds |

Read-only scientific work — literature search, repository inspection,
diagnostics, hypothesis generation, review of a frozen packet — takes no lock
and runs in parallel. That is where the runtime's throughput comes from, and it
is safe because none of it can corrupt anything.

PostgreSQL advisory locks rather than the `flock` the v1 layers use on run
files, for a reason that is a property and not a preference: a worker killed
mid-mutation has its session closed by the server and the lock goes with it.
`tests/test_runtime_locks.py` asserts that by `SIGKILL`ing a real holder.

No transaction is ever held open across a model call, an `sbatch`, a test run or
a human decision.

## 6. Failure classification

`research_os.runtime.failures` maps every failure class to exactly one response.
The completeness of that map is a test, so adding a class without deciding what
to do about it fails the suite.

The load-bearing property is what the taxonomy *cannot* express:

> There is no `FailureClass` for a refuted hypothesis, and there never will be.

An experiment that runs correctly and refutes its hypothesis has succeeded: the
code worked, the data were valid, the answer was no. A system that retries it
burns compute for the same answer; a system that retries it with different
parameters until it stops saying no has become a machine for manufacturing
positive results. Falsification is recorded as evidence through the scientific
kernel and never reaches retry policy at all.
`tests/test_runtime_failures.py` asserts the absence.

## 7. Autonomy levels

| level | meaning | examples |
|---|---|---|
| `A0` | autonomous, read-only or reversible | literature search, repository inspection, diagnostics, candidate hypotheses |
| `A1` | autonomous, isolated and bounded side effects | isolated worktree edits, a permitted experiment, candidate artifacts, bounded compute |
| `A2` | explicit human scientific authority required | post-hoc primary-endpoint change, material preregistration change, promotion of a contested claim, objective change, publication |

Nearly all routine work is `A0`/`A1`. `A2` is rare by construction, and each
`A2` action names the decision it needs, so "the system asked me something" is
always answerable with "about what, and why now".

## 8. Test setup

The runtime tests need a real PostgreSQL, because the properties being tested
are properties of one. Getting it requires no system service, no container
runtime and no root: `pgserver` is a dev-group wheel containing a PostgreSQL
binary distribution, and the fixture initialises a cluster in a temporary
directory on a unix socket. The runtime itself never imports it — the runtime
only ever sees a DSN.

```bash
uv sync --all-groups --extra runtime
uv run pytest -q tests/test_runtime_*.py
```

Set `RESEARCH_OS_SKIP_PG_TESTS=1` to skip them; they skip themselves with a
reason if `pgserver` is not installed.

## 8a. Budgets, and what each dimension actually enforces

Not all five behave the same way, and the difference matters when reading a
report.

| dimension | enforcement |
|---|---|
| `model_calls` | reserved before the call, settled after. A call cannot happen without capacity |
| `model_cost_usd` | a call that declares `max_cost_usd` reserves **that whole ceiling** against run, project, system and any scope it names (the portfolio names its idea and lineage, `sql/0036`) before it starts, is refused if any of them cannot cover it, and is capped at the same number by the provider; settled at the provider's reported cost, including a failure the provider billed. A call that declares nothing reserves the profile's estimate -- see below |
| `external_jobs` | reserved before submission |
| `work_items` | reserved before a local experiment |
| `wall_clock_seconds` | **charged after the fact**, per cycle entry |

**What the cost ceiling does and does not promise.** No provider quotes a
price before it bills, so a hard monetary bound needs something that stops the
spend. For a call that declares a ceiling there are two: the ledger will not let
it *start* unless every applicable budget can cover the whole ceiling, and the
Claude CLI is invoked with `--max-budget-usd` at the same number. The CLI checks
that cap after each model response, so the response in progress when the cap is
crossed completes and is billed -- the call ends as `error_max_budget_usd`,
recorded at what it cost, classified `BUDGET_EXHAUSTED` (terminal; never retried,
never counted against the provider's health). A ceiling can therefore be exceeded
by the part of **one model response** per concurrently running call that crosses
its own per-call cap, and that excess is recorded exactly. It is not exceeded by
whole calls, or by a call that should never have started.

Every call the discovery portfolio makes declares a ceiling (its stage's, its
explorer's, its follow-up's). The objective cycle's graph nodes and actions do
not: they reserve the provider profile's estimate (0.05 USD), which is an
estimate and not a bound, and a project ceiling can be overshot by one such
call's actual cost -- recorded, and seen by the next reservation. The delegated
controllers (`runtime/spend.py`) reserve a ratcheting per-call ceiling and do not
pass it to the provider, because a coding session is many turns of tool use;
their residual is stated in that module.

Wall clock is the exception and it is worth being explicit. Nothing interrupts a
running graph on elapsed time; a cycle's duration is bounded by the per-call and
per-experiment timeouts, and the budget records what it used so the *next*
continuation decision can see it. That is weaker than the v1 research layer,
which checks elapsed time before every invocation, and the difference is
deliberate: interrupting a graph mid-node would leave exactly the
half-completed-effect state the invocation ledger exists to avoid.

It is charged per *entry*, not from the run's start, and it is charged when a
cycle stops at a human gate as well as when it finishes — both of which were
bugs an independent review found.

Two scopes are created by default: a per-run budget from `runtime.yaml`, and a
per-*project* cost ceiling of `max_model_cost_usd x max_cycles_per_objective`.
The project ceiling exists because `should_continue` ignores run scope when
deciding to continue — the successor gets a fresh run budget — so without it one
objective's exposure was the per-run cost times the cycle ceiling, with nothing
warning.

## 9. researchd, the control plane

One local process, one loop, deterministic work. `deploy/researchd.service` is a
systemd **user** unit, shipped and not installed.

```text
researchd --once          one pass, for tests and cron
researchd                 the loop
researchctl runtime daemon  the same thing, through the CLI
```

`Daemon.tick()` is one pass of everything and returns a report, which is what
makes the control plane testable without threads. `run_forever` calls it and
sleeps only when a pass found nothing to do.

The pass order is deliberate:

```text
recover   expired leases, stale invocations, stale reservations,
          stale experiment interpretations
observe   each project's capsule digest -> CAPSULE_CHANGED when it moved
ingest    unconsumed events -> queued work
schedule  due schedules -> events (never work directly)
poll      external jobs -> reconciled status, and an event when finished
surface   pending approvals -> one notification each, ever
claim     one due work item -> run it under a renewing lease
```

Recovery comes first so a restarting daemon cannot pick up new work while old
work sits stranded. Claiming comes last so every pass leaves the system
recovered even if the worker then dies.

Observation comes second, before ingest, so a scientific change a person just
made becomes an event in the *same* pass that notices it. Observing after
ingest would make every change wait a full tick -- invisible in a test, and
indistinguishable from "it did not work" to a researcher who has just promoted
something. It is paced by ``capsule_observe_seconds`` (30 by default) rather
than run on every pass, because hashing a capsule is cheap and not free.

**It calls no model.** That is how invariant 2 survives a background process
existing, and `tests/test_runtime_daemon.py` asserts it by parsing `daemon.py`
for a `complete` call. Frontier reasoning happens inside a claimed work item
against a reserved budget, never in the scheduler.

**The event-to-work table** is a handful of lines in `daemon.py`, so "why did
this run" is answerable by reading it. The dispatch from a work *kind* to the
method that runs it is a module constant beside it, for a reason worth
recording: the two used to be separate -- a mapping built inside ``_run_item``
and a test listing the runnable kinds by hand -- so adding a kind meant editing
two places, and forgetting the second made a test fail for a reason unrelated to
the defect it was written to catch. One subtlety: the continuation work item's
dedup key is per *run*, not per event, because a run may have at most one
successor however many events claim it finished — and during the first real
end-to-end run, two did.

## 10. Executors

A graph node submits an `ExecutionSpec` and never a shell command.

| executor | behaviour |
|---|---|
| `LocalExecutor` | runs it now, returns a finished handle; nothing to reconcile |
| `SlurmExecutor` | freezes the spec, writes an immutable run directory and manifest, writes the batch script, submits, returns a job id |

Submission never blocks. The `external_jobs` row is created in `SUBMITTING`
*before* `sbatch` is invoked, so a crash in that window leaves a row with no
scheduler id to investigate rather than a job nobody knows about. Such a row is
marked `UNKNOWN` and **not resubmitted**: the submission may well have
succeeded.

Slurm's raw states are mapped to a `FailureClass` as well as a status, because
the v1 `ExecutionState` mapping collapses distinctions the retry policy needs:

| raw state | status | failure class | what happens |
|---|---|---|---|
| `COMPLETED` | COMPLETED | none | done, whatever the science said |
| `PREEMPTED` | CANCELLED | `slurm_preempted` | resubmit unchanged |
| `NODE_FAIL` | FAILED | `slurm_node_failure` | resubmit unchanged |
| `OUT_OF_MEMORY` | FAILED | `slurm_out_of_memory` | needs more resources |
| `TIMEOUT` | TIMED_OUT | `slurm_timeout` | needs more resources |
| `CANCELLED` | CANCELLED | none | a person did that; do nothing |
| anything unlisted | UNKNOWN | none | keep polling; form no opinion |

## 11. Provider routing

A graph node asks for a capability and a criticality. It never names a vendor;
`tests/test_runtime_layering.py` reads the graph package to check.

**Criticality is a floor.** `CRITICAL` work is never silently answered by a
weaker model — the request fails and names what it wanted. A scientific review
quietly performed by a cheaper model is worse than one that did not happen,
because the first looks like it happened.

**Independence is reported, never assumed.** A caller asks for
`DIFFERENT_FAMILY`; the router gives the strongest separation available and
records what it *achieved*. On a machine with one provider family, a review is
recorded as `DEGRADED ... This is not independent review.` and the note reaches
the run report.

**And it can be required.** `review_independence` in `runtime.yaml`:

| value | behaviour |
|---|---|
| `prefer` | the default. Strongest separation available, degradation recorded |
| `require` | a `CRITICAL` request that cannot get a different family **fails** |

`require` fails closed into `WAITING_FOR_EXTERNAL_DEPENDENCY`, not into an
error: no amount of retrying produces a second provider family, so the honest
terminal state is "the runtime did everything it could and what is missing is
something you install". It binds on `CRITICAL` work only -- making it bind on a
literature extraction would stop a run for a reason that has nothing to do with
review independence.

`prefer` is right for a one-family machine and `require` is right for a
deployment that has configured two and wants a missing one to be an outage
rather than a silent downgrade. **This build has one family**, so under the
final high-autonomy profile `require` would refuse every critical review here;
that is a genuine external prerequisite and `runtime doctor` says so.

**Tier is configured priority, not measured performance.** Every available
provider is assumed tier 3; a researcher who knows better sets one. So "answered
at tier 3" means the configuration permitted it, and not that anyone
benchmarked it. The distinction is stated by `runtime doctor` as well as here,
rather than fixed by building a benchmark: a generalised model-benchmarking
platform is a research project of its own, and nothing has yet demonstrated a
routing failure that would justify it.

Every call is recorded: provider, the model that actually answered, role,
criticality, independence group, prompt version, the hash of the prompt, the
hash of the raw output, tokens, cost, latency, status — for failures too.

## 12. Prompts

Versioned artifacts with identities that reach provenance
(`scientific_reviewer@1`). One module rather than a directory of files, because
the *material* travels as fenced data rather than interpolated prose.

The blind explorer's template is defined by what it omits: it declares only
`question` and `data_description`, and `render` refuses any field it did not
declare. Passing it the project's current hypothesis is an error, not a quiet
loss of independence.

Three fences were added to `automation/promptdata.FENCES` for the runtime —
frontier state, hypothesis proposals, experiment results. Adding to that tuple
is what makes a delimiter inert inside *every* block, and the existing property
tests grew to cover them automatically.

## 13. Observability

```text
researchctl runtime status        what is running, waiting, failed
researchctl runtime runs          the cycles, newest first
researchctl runtime run <id>      one cycle: work, decisions, jobs, model calls, budget, events
researchctl runtime approvals     the prepared decision packets
researchctl runtime approve <id>  / decline <id>
researchctl runtime jobs          external jobs
researchctl runtime costs         what has been spent, and what is left
researchctl runtime events        the operational event log
researchctl runtime findings      what the runtime observed, and what each rests on
researchctl runtime doctor        whether the runtime can run here
```

Every view has a deterministic `--json` counterpart. Every string that came from
a model or a fetched document passes through `research_os.textsafe` on the way
to a terminal, with newlines and tabs stripped too — a table cell is not a place
for either.

`runtime findings` prints a heading saying what a finding is *not*, every time
it renders. A table of scientific-sounding statements with identifiers is
exactly what a reader might mistake for a project's record, and the identifiers
make that mistake easier rather than harder.

`runtime doctor` follows `researchctl doctor`'s contract: an absent capability
is a WARN and exits zero. It reports:

- how many policy actions have no handler in this build -- currently none, so
  the gap between "the authority rules know about this" and "this build can do
  it" is closed and the line says so rather than disappearing;
- which model actually answers each role, and any substitution, because the
  provenance records the model the *provider* named rather than the alias that
  was asked for, so a substitution cannot be spotted from the ledger alone;
- that tiers are configured rather than measured;
- whether containment works here, with the probe's own reason and the remedy;
- whether any project declares `check_profiles` that a runtime coding cycle
  does not yet honour (§17).

## 14. Getting a database

```bash
# a real one
export RESEARCH_OS_RUNTIME_DSN='postgresql://...'

# or a disposable local one, no root and no container runtime
researchctl runtime dev-db start
researchctl runtime migrate
researchctl runtime doctor
```

`dev-db` is `pgserver`, a dev-group wheel containing a PostgreSQL binary
distribution. The runtime only ever sees a DSN and never imports it.

## 14a. Closing the loop: finding -> proposal -> person -> successor cycle

The property this release exists for, and the one the first pilot did not have.
R5 shipped with the frontier-unchanged stop, and `docs/R5_BUILD_RECORD.md` §8
was explicit that it was a mitigation rather than a solution: the runtime could
not change the frontier its own planning was derived from, because it could not
write a capsule and had no way to *ask* for one to be written.

It now has one, and the loop has five links.

```text
1. a runtime finding          FIND-...   typed, digested, noncanonical
2. a grounded proposal        PROP-...   cites finding ids; validated against them
3. WAITING_FOR_SCIENTIFIC_DECISION       the cycle concludes; nobody is blocked
4. the researcher promotes               `researchctl propose promote` -- interactive
5. researchd observes, and a successor cycle opens
```

### 1. Runtime findings

`research_os/runtime/findings.py`. A finding is "this runtime observed this,
produced by this action, in this cycle, resting on these artifacts". It has no
status a person could accept, no acceptance rule, never reaches `.research/`,
and participates in no scientific digest.

A finding also carries a bounded **producer-authored excerpt**, and that word
is the design. A `critique_hypotheses` cycle wrote an eleven-kilobyte artifact
holding six substantive alternatives and the finding said `6 alternative
explanation(s) for 5 target(s)` -- forty-four characters, and all the proposal
layer could read. The proposal grounded in it reported the hole itself, in its
own `PR-002`: "Only that summary is available to this proposal; the text of the
six alternatives is not." Refusing to reason from invisible content was right.
Hiding the content was the defect.

The excerpt is chosen by the handler that produced the result, not scraped by
the prompt layer. An artifact is bytes with a media type; a generic layer
reading them would have to guess which part of a ten-kilobyte document is the
finding, for every schema any handler might ever write. The handler already
knows -- it built the structure a moment earlier. It travels in the existing
structured result under one reserved key, so it inherits the checkpointing and
the idempotency ledger rather than needing its own. It is bounded, fenced,
labelled noncanonical in the entry that carries it, and in both the finding
digest and the grounding digest -- but only when non-empty, so every finding
recorded before excerpts existed keeps the digest it was already cited under.

What it buys is auditable grounding. The v1 proposal grounding allowlist has
always had a `finding_ids` field and has always refused a citation to an id it
was not given -- and nothing ever supplied one, so an autonomous result reached
the proposal layer as prose concatenated into the natural-language `goal`. "What
is this proposal resting on" was answerable only by reading a model's sentence.
Now:

```text
proposed scientific change
  -> proposal item        cites finding_id, checked against the allowlist
  -> runtime finding       runtime_findings
  -> artifact / capsule object / literature key / experiment job
  -> bytes, by content hash
```

**Where findings come from.** `graphs/cycle.FINDING_FOR_ACTION` maps the
actions whose outcome is an *observation* to a finding kind, and
`perform_action` records one from the handler's own `detail` and `data` after a
successful action. The handler's own sentence, not a paraphrase: a summariser
between the observation and the citation would be one more place for a claim to
drift from its evidence.

Deliberately not every action. A submission is not an observation, so
`run_local_experiment` and `submit_cluster_experiment` are absent and
`interpret_results` is the one that reads what came back. `propose_capsule_
change` and `nominate_insight` are absent because their output is an *ask*, and
a finding about having asked would be a citable observation with nothing behind
it. `draft_manuscript` is absent for the same reason, and `design_experiment`
because a specification is a plan whose preregistration artifact is already
durable and already looked up by digest.

The planner is told the finding *count*, not the findings. It is choosing an
action, and handing it the text would invite it to plan from a finding's
content rather than from the project's state.

Findings are **immutable and deduplicated by content**. The digest excludes the
run and the cycle deliberately: the runtime recomputes an unchanged frontier on
every cycle, and a cycle index in the digest would mint seven citable
identifiers for one fact -- the seven-cycle pilot in §16, one layer down.

A finding's `summary` is untrusted text. It reaches a prompt behind
`RUNTIME_FINDING_FENCE` and a terminal through `research_os.textsafe`, and
`tests/test_runtime_proposals.py` attempts the obvious attack: a finding that
closes its own fence and declares a new citable identifier. The delimiters are
inert, and the allowlist is controller-authored text outside every fence, so a
worker that obeys the injected instruction is refused by the validator.

### 2. `propose_capsule_change`, and why it is thin

It delegates to the v1 `ProposalController`. That controller already does the
authority-preserving path -- deterministic grounding validation, one bounded
grounding correction that is a constant rather than a parameter, one independent
assessment, a store outside every project, and a human promotion that produces a
*draft* and nothing stronger. A second proposal engine behind a nicer interface
would be a second set of grounding bugs.

What the runtime adds is what the v1 layer cannot know about:

| addition | why |
|---|---|
| grounding in runtime findings | the allowlist had the field and nothing supplied it |
| a reserved proposal identity | `make_proposal_id` is deterministic in project, goal and *second*, and a retry does not reproduce the second |
| immutable links to the findings | so the chain above is traversable from the database rather than from prose |
| surviving a failed assessor | the controller stores the proposal and *then* assesses, so an assessor failure raises after the valuable artifact exists |

That last one is also from the first pilot. The assessor exhausted its
structured-output retries after the proposal had been stored; the work item was
recorded FAILED, and the proposal survived only because the idempotency ledger
consults the reconciler on its FAILED path. It worked, by accident of a
mechanism built for something else. A `ProviderInvocationError` now looks for
the reserved proposal before failing: if it is there the action *succeeds*, and
`assessed=False` is stated in the data and in the detail. A proposal that
reached a person without the independent assessment is a weaker thing than one
that passed, and nothing said so where a reader would look.

The handler writes no capsule file, does not import
`research_os.proposal.promote`, authors no Review, accepts no Claim, and changes
no scientific status. `tests/test_runtime_authority.py` asserts all of that by
parsing the package, including that the module makes no filesystem write of its
own.

### 3. Replay safety, and the key that makes it work

The reservation key is `propose_capsule_change:<run>:<cycle>` and nothing else.
Two things about that are deliberate, and the second was a bug in the first
version.

**Nothing per-attempt**, because keying on anything a retry does not reproduce
yields a ledger that records every duplicate faithfully and prevents none.

**Nothing that can change between attempts either.** The first version included
the grounding digest. A crashed attempt is retried after the daemon has ticked,
a tick can record new findings, the digest would differ, the key would differ,
the reserved id would differ, and the reconciler would find no proposal and
create a second one for the same decision. The grounding digest belongs in the
proposal's *basis snapshot*, where changing it is supposed to be detected.

`tests/test_runtime_proposals.py` kills a real process between the proposal
being written and the ledger recording it, asserts that a retry *before*
recovery refuses -- an `IN_FLIGHT` row means "someone may still be doing this"
-- and then runs the daemon's own recovery pass and asserts the retry adopts the
existing proposal and asks no model.

### 4. Stale scientific basis

A proposal is asynchronous: written by one bounded run, promoted by a person,
possibly much later. In between, the canonical science can move.

`research_os/proposal/basis.py` snapshots what the proposal actually cited: the
project identity, each referenced object's status and project-scoped
`subject_digest`, each one's schema version, the charter when the proposal was
grounded in it, and the digest of the supplied findings. Before a promotion is
offered or executed, it is recomputed.

Two decisions worth stating:

**Not the repository HEAD.** `base_commit` is on the proposal and comparing it
was the obvious check and the wrong one. A proposal is about scientific objects,
and failing it because somebody fixed a typo in the README would teach a
researcher to click past the warning. `tests/test_runtime_proposals.py` commits
an unrelated README change and asserts the basis is still fresh.

**Status is in the basis, and the kernel's semantic digest excludes it.** Both
are right and they answer different questions. `semantic_projection` leaves
`status` out so a Review does not go stale when an object is moved
administratively -- the science it reviewed is unchanged. A *proposal's* premise
is the opposite: "this project has an open question about X" stops being true
the moment that Question is answered, and the Question's statement does not move
when it is.

A stale basis raises `StaleProposalError`, names what changed, and points at
regeneration. `--accept-stale-basis` exists for the researcher who has read the
change and decided it does not affect the item; it is reachable only from the
interactive command, and the CLI prints what changed before offering it.

A proposal written before basis snapshots existed reports `checkable=False`
rather than `fresh`, because reporting it as fresh would assert a check that
never ran.

### 5. Automatic continuation

Human scientific *authority* is intentional and stays. Human *choreography* is
not, and was the last piece of routine orchestration in the system. Before this,
the researcher promoted a proposal and then had to type a continue command,
because nothing told the runtime.

**The direction of the dependency is the design.** The obvious fix is for the
promotion command to notify the runtime, and that would make the scientific
kernel depend on PostgreSQL and on a daemon being up -- which invariant 7 and §2
both forbid, and which would mean a researcher could not promote anything while
the database was down. So the kernel is not changed at all. The runtime
*observes*.

`research_os/runtime/capsulewatch.py` computes two digests from one read:

| digest | over | answers |
|---|---|---|
| `capsule_digest` | every object's id, status and `subject_digest`, plus the charter and state documents | did the canonical science change at all |
| `frontier_digest` | the unresolved work, the existing function | is there different work to do |

The first decides whether to **emit**; the second decides whether to **act**. A
charter rewrite is recorded and opens no cycle, because a successor over an
identical frontier is the seven-cycle pilot again.

`capsule_observations` is a compare-and-set target, serialised by `select ...
for update` on the project's own row. The first observation is deliberately not
a change: the runtime has no idea whether what it is looking at is new, and
treating "I have never looked" as "a person just changed something" would open a
successor cycle on every fresh database.

`CAPSULE_CHANGED` is deduplicated per `(project, new digest)` -- per digest
rather than per project, so a *second* change is a second advance rather than
being swallowed by the first one's key.

The successor is a **new cycle with recorded lineage**, never a revival of the
parked thread. Eligibility requires all of:

1. the objective's latest cycle has **finished**. One still holding a live
   interrupt is waiting for a different answer and is woken by
   `SCIENTIFIC_DECISION_RECORDED`;
2. it has **no successor already**, and it is the newest run of its objective
   — chosen by a *total* order, because two runs can share `created_at` to the
   microsecond and both would otherwise be advanced;
3. the **frontier moved since that cycle recorded one**;
4. `should_continue`'s other bounds permit it -- lineage depth against
   `max_cycles_per_objective`, and the project budget. `BUDGET_EXHAUSTED` is
   deliberately not eligible: the science moving does not create budget.

A refusal records why. "Nothing happened and the log says nothing" is how the
missing continuation looked from the outside, and reproducing that with a
different cause would not be an improvement.

### Which two frontier digests get compared, and why the first pilot got it wrong

Point 3 above says "since *that cycle* recorded one", and the wording is the
whole of a defect the first closed-loop pilot found.

`should_continue` owns the progress check, and it had exactly one form of it:
compare the run's digest with its **parent's**. For the ordinary continuation
that is right — a cycle that has just concluded is its own "now", and this is
the seven-cycle stop in §16.

For an *advance* it is wrong. The parked cycle and its parent recorded the same
digest, as every real successor does: the runtime cannot write a capsule, so
its own work never moves the frontier its planning is derived from. Comparing
the two asks "did that old cycle learn anything", and the answer is no — which
is precisely why it stopped and waited. So the continuation refused at the
exact moment the wait had ended. `CAPSULE_CHANGED` fired, `frontier_changed:
True`, the advance ran, and no cycle opened.

`should_continue` now takes `observed_frontier`, the frontier as measured by
the caller, and the comparison becomes "has the frontier moved since this cycle
recorded one" — which is the question both callers actually mean. The
continuation path passes nothing and is unchanged; the advance passes what it
measured.

An **empty** measurement is explicitly not a change. `observed_digests` returns
an empty frontier when the capsule could not be read — mid-edit, a checkout in
progress — and an earlier version's `if frontier and ... == ...` let that fall
through to "changed", opening a successor over a frontier nobody had measured.
Unknown is not changed.

## 14b. Which experiment an interpretation is of

`interpret_results` used to answer "which experiment" with `order by finished_at
desc limit 1`. That is association by temporal coincidence, and invariant 11 --
every experiment traceable to code, configuration, data and version -- is not
satisfied by a scientific reading attached to whichever job the scheduler
happened to reap last. Three things went wrong with it, and all three were
reachable:

- two jobs finishing while a cycle plans gave the interpretation to whichever
  row came back first;
- a job interpreted in cycle 3 was interpreted again in cycle 4, because nothing
  recorded that it had been;
- a result could be compared against a preregistration belonging to a different
  experiment.

`experiment_interpretations` replaces it:

```text
interpretation_id  job_id  project_id  run_id  work_id
spec_digest        interpreter_version  artifact_id
status             detail  created_at   completed_at
UNIQUE (job_id, interpreter_version)
```

`interpreter_version` is *inside* the identity key, because changing how a
result is read is a legitimate reason to read the same experiment again -- and
the second reading is a different interpretation rather than a correction of the
first. Both are kept, so a person can see that two readers disagreed.

Selection is **oldest eligible first**, where eligible means "terminal, and no
*completed* interpretation at this reader version". Oldest-first is a stable
total order (ties broken by `job_id`) over a set that only grows at one end, so
two workers asking at the same time get the same answer and a backlog drains in
the order it formed. Excluding a job because *any* row exists would strand one
whose reader crashed mid-reading.

The order of operations is the crash-safety property:

```text
1. resolve which job      an explicit job_id beats selection, and a name that
                          does not resolve is an error, not an invitation to
                          pick something else
2. claim (job, version)   durably, before reading anything
3. if already COMPLETED   return its artifact; read nothing again
4. criteria                from the stored preregistration, by spec digest
5. write the artifact
6. mark the claim complete
```

A process that dies between 5 and 6 leaves an `IN_PROGRESS` claim and an
artifact. The next attempt re-derives the *same* artifact bytes -- the inputs
are the job row, the stored preregistration and the claim's own id, all
immutable -- so the content-addressed store returns the same id and step 6
attaches it to the existing claim. One logical interpretation, one artifact,
whatever the process did.
`tests/test_runtime_interpretation.py` proves it with a real process killed by
`os._exit` inside that window.

A missing preregistration leaves the claim `IN_PROGRESS` rather than completing
it. The job is still owed a reading; marking it read would hide the missing
preregistration permanently.

The daemon's recovery pass marks long-running claims `ABANDONED` and
deliberately does not delete or re-claim them: the row is the only record that a
reading of this experiment was begun, and the next attempt needs it to reconnect
the artifact.

## 14c. Containment

`SECURITY.md` and §16 have always said the coding pipeline is not an OS sandbox.
`research_os/sandbox.py` is the abstraction that fixes that where a host allows
it, and reports honestly where it does not.

**One place, two questions.** `probe()` says which technology this host can
actually provide; `contain(argv, spec=..., mode=...)` returns the argv that runs
a command inside it. No sandbox-specific flag appears anywhere else, which is
what makes it replaceable -- the day rootless Podman is available, a backend is
added there and no handler changes.

**Deny by default.** A contained command gets its worktree read-write, the
explicit inputs it was given read-only, an isolated `/tmp` with a throwaway
`HOME`, and a read-only operating system. It gets no home directory, no SSH
keys, no SSH agent, no Git credentials, no provider credentials, no unrelated
environment and no network. Network is a capability the caller asks for.

**Three modes, and the third one is not a courtesy.**

| mode | behaviour |
|---|---|
| `required` | contained or not run |
| `preferred` | contained where possible; the absence recorded on every command result |
| `off` | not contained, by a deliberate configured choice |

`sandbox.mode` in `automation.yaml` sets it, default `preferred`.
**High-autonomy runtime execution overrides it to `required`** -- for the
coding pipeline and for local experiments -- because there nobody is watching,
and `preferred` there would mean model-written code running with the
researcher's credentials unattended.

**Both command runners go through it.** `automation/checks.py`'s
`run_acceptance_command` is the choke point for the coding pipeline, and it is
shared with `researchctl auto`, so containment lands on the path a person has
always been able to run as well as the autonomous one. `runtime/executors.py`'s
`LocalExecutor` contains declared experiment commands. Every `CommandResult`
carries `contained` and `containment`: "the tests passed" and "the tests passed
inside a sandbox" are different facts about a run.

**Both measurements below were superseded on this host, and are kept.** The
targeted AppArmor profile in `deploy/apparmor-bwrap` now grants `bwrap` the
`userns` capability while the global unprivileged-userns restriction stays
on, so the first of the two no longer holds here: `runtime doctor` selects
bubblewrap, `runtime containment-audit` records 38/38 adversarial checks
held through the production `contain()` adapter, and the discovery
portfolio's first real experiment ran inside it with the network denied and
fifty-two packages installed from the throwaway cache overlay
(`docs/AUTONOMOUS_DISCOVERY_REPORT.md` §X.5). `ROADMAP.md` retires the
"no OS containment" entry on the same evidence. The second measurement --
`systemd-run --user`'s directives being accepted and silently ineffective --
is unchanged, and is why that backend is still never selected.

They are left below rather than rewritten because the *reasoning* is what
the section is for: a probe that looks for a binary rather than running it
would have reported containment on the host these were taken on.

**It refuses to pretend, and that is the load-bearing property.** Two
measurements from an earlier state of this build's host:

- `bwrap` is installed and **cannot contain anything here**. Ubuntu 24.04 ships
  `kernel.apparmor_restrict_unprivileged_userns=1`, so a non-setuid `bwrap` gets
  a user namespace it has no capabilities in and fails at the uid map. The
  binary is present, `--version` works, and it isolates nothing. So the probe
  *runs* the technology rather than looking for its file.
- `systemd-run --user -P -p ProtectHome=tmpfs -p PrivateNetwork=yes` starts the
  unit successfully and the contained process **sees the real home directory and
  the real network**. The user manager's namespacing needs the same user
  namespaces the kernel is refusing, and it does not fail the unit when it
  cannot get them. That backend is therefore never selected. It is probed and
  reported so that nobody wires it up believing the directives bind.

`bwrap --unshare-user-try` is exactly the same shape and is deliberately not
used: a containment that reports success without containing is worse than none,
because the second is a known risk and the first is a wrong belief that
decisions get made on.

So on this host, containment is **unavailable**, high-autonomy execution of
model-written code is **refused**, and the canonical-state fingerprint from §16
remains as defence in depth -- it still detects a capsule or Git-ref change
after the fact, it still prevents nothing, and it still sees nothing that
happens outside the repository. `tests/test_sandbox.py` runs the policy and
argv tests everywhere and skips the ten escape attempts with the probe's exact
reason, which is the release evidence rather than a coverage gap.

## 15. What the runtime cannot do

All eight `A2` actions turn out to be ones the *person* performs, because
performing them means writing canonical scientific state, merging to a canonical
branch, or publishing — and the runtime has no method for any of those.

So approval does not unlock execution. It unlocks a recorded decision and the
exact command to run:

```text
accept_claim                    -> researchctl review <CLAIM-ID>
change_primary_endpoint         -> edit the manifest, record a Decision
integrate_to_canonical_branch   -> review the candidate branch and merge it yourself
publish_externally              -> submit it yourself
```

`tests/test_runtime_registry.py` asserts that no `A2` action has a handler. That
is the property, stated as a test rather than as a promise.

## 15a. The four gaps the beta left open, and what closing them found

`docs/RELEASE_CANDIDATE_REPORT.md` §J listed five pieces of remaining
substantive work and called the release `AUTONOMOUS_RUNTIME_BETA`. Four of
them are closed on `rc/thesis-pilot`; the fifth (giving `design_experiment` an
executor it can use) is an infrastructure question, not code.

**One project, one acceptance profile.** `AutomationController._accept_plan`
is the one place a plan becomes work orders, whoever wrote it, so it is the
only place the substitution can be made once. A project that explicitly
declares `check_profiles` gates every path on them. A plan whose commands are
already a subset of the declared argv is left alone, which is what preserves
the research layer's narrowing: a task that named `required_checks: [tests]`
had it resolved from these same profiles, so its one command stands. The rule
is about the *commands*, not about the caller, because a check on the caller
is a check a future caller can fail to make.

**And the defect that closing it exposed.** Driving `run_coding_task` against a
real `AutomationController` — which no test had done — fails
`POLICY_REFUSED`, every time. `canonical_fingerprint` hashed `git show-ref`
into one `<git-refs>` entry, and worktree isolation creates a branch in the
canonical repository, so every honest coding run looked like an escape. The
runtime's only code-writing capability could not succeed, and the guard that
detected it had survived four adversarial reviews because the one test of that
handler used a controller that creates no worktree. **An escape detector that
fires on every honest run is not a detector**, and the general lesson is the
one this document keeps relearning: a double that removes the mechanism under
test removes the test.

The fingerprint is now one entry per ref. Strictly stronger — a refusal names
what moved instead of the word "refs" — and it makes the exemption expressible:
`refs/heads/automation/<reserved run id>/`, named from the reservation before
the pipeline starts, so the attempt that adopts a crashed predecessor's branch
computes the same namespace. Anything else still fails.

**Decline.** A proposal lifecycle with only one half of a decision in it made
"I read this and rejected it" indistinguishable from "nobody has looked", and
the cross-cycle deduplication asks exactly that question — so a rejected
proposal stayed pending forever and the runtime never proposed about those
findings again. `researchctl propose decline` requires a TTY and a reason, and
`test_no_runtime_module_declines_a_proposal` asserts structurally that nothing
here can reach the writer. The authority is not the same as promotion's and it
is not lesser: a decline writes nothing into a capsule, but it *closes* a
question, and a runtime able to close its own unanswered proposals could report
an empty queue it produced by refusing itself.

**Retention.** 0015 gives `artifact_links`, `tool_invocations` and
`model_calls` their own `project_id`, so pruning a run stops destroying the
record of what the run caused. The consequential one was `artifact_links`: the
preregistration guard reached the project through the run, so a prune made the
guard refuse that experiment permanently while the document sat intact in the
content-addressed store. `run_id` keeps its value rather than becoming null,
because `artifact_links_identity_idx` is unique over `coalesce(run_id, '')` and
nulling it can collapse two surviving rows onto one identity, which makes
PostgreSQL refuse the delete. A prune that cannot run is worse than a label
that outlives its row.

**Delegated budget.** `research_os.runtime.spend` wraps the provider adapters.
Both delegated controllers take a registry and call `invoke` once per model
call, so the adapter is the chokepoint they already share, and wrapping it
needs no change to either and covers any third controller written later. Each
call reserves before the provider is asked; a refused reservation raises
`BudgetExceededError` *before* the call, which is the whole difference between
a budget and a report. `charge_delegated_spend` keeps the provenance rows it
alone can write and becomes a reconciliation for calls the authority did not
see. The residual is that no provider quotes a price before it bills, so one
call can exceed its ceiling; the ceiling then ratchets to the largest observed
cost, bounding the excess by one call rather than repeating it.

## 15b. What the third adversarial review found, and what was left open

Two independent read-only reviewers were run against this branch: one on the
mathematics of the scientific pilot, one on this diff. The architecture review
returned three HIGH findings, four MEDIUM and three LOW. Six are fixed in
§15a and above. The rest are recorded here rather than silently carried,
because a finding nobody wrote down is a finding that gets re-found.

**Fixed, and each one is a test.**

- The project cost ceiling was derived from the first objective's
  `--max-cost-usd` and could never be raised, so `runtime start
  --max-cost-usd 0.50` wrote a 6 USD *lifetime* ceiling and bricked the
  project. This branch introduced that. The ceiling now comes from the
  configuration, is raised and never lowered, and `researchctl runtime budget`
  exists so that it can be changed at all.
- Exempt refs were dropped from the fingerprint entirely, so a *move* of the
  run's own branch was invisible -- and `git update-ref` moves a branch that is
  checked out in another worktree, which `push` and `branch -f` refuse. Two
  changes: exempt refs are recorded rather than skipped, and `branch_drift`
  asks Git whether each order's branch is still where the controller wrote down
  that it was. The second is what actually closes it, because a create and a
  create-then-move give the same two snapshots.
- `git show-ref` does not list HEAD, and `--head` lists it by resolved sha, so
  repointing HEAD at another branch at the same commit changed nothing either
  reported while moving the canonical checkout onto a branch the guard would
  never inspect again. `git symbolic-ref -q HEAD` is now in the fingerprint.
- The subset exemption applied to planner-authored plans, so a planner emitting
  one of two declared checks skipped the other with no event recorded. It now
  applies only to controller-supplied plans, and `runtime doctor` no longer
  claims "every path resolves the same set".
- The capsule exclusion was a substring match, so `.research/claims/runtime/`
  was invisible to the fingerprint, loaded by the capsule scanner as a real
  scientific object, and not gitignored. Exact top-level match now.
- `charge_delegated_spend` could not run when `controller.start` raised -- and
  `start` makes the planner call, and this branch added two new ways for it to
  raise afterwards. It is inside the `try` now.
- The decline authority guard was an AST scan for a call named
  `record_decline`, which `getattr(store, "record_" + "decline")` defeats while
  the runtime already holds a live `ProposalStore`. The terminal check moved
  **into** `ProposalStore.record_decline`, so it is enforced rather than
  linted; the AST scan is kept as defence in depth and now also bans the name
  in any string.

**Open, recorded, not fixed.**

- **A decline can be forged by appending one line to `declines.jsonl`.** The
  file is under `~/.local/state/`, outside every repository, so neither
  `canonical_fingerprint` nor `git status` covers it, and `declines()`
  validates JSON shape only. On an uncontained host, code executed during
  acceptance commands could mark every item of every proposal decided. Closed
  under `SandboxMode.REQUIRED`, which is what high autonomy sets and what this
  host cannot provide. The fix is a signature or a digest chain over the
  ledger, which is a design decision about how much of the runtime's state is
  tamper-evident, and it should be made for promotions and declines together
  rather than for one of them in a hurry.
- **`--max-cost-usd` bounds a cycle, not an objective.** The successor chain
  inherits it, so the exposure is the cap times
  `max_cycles_per_objective`, and both numbers are printed. Making it bound the
  whole objective means one budget shared by every cycle in the chain, which is
  a change to the reservation path in `routing.py` and `spend.py`.
- **`charge_delegated_spend` reconciles with two scalars and clamps at zero.**
  A negative residue -- the authority and the invocation record disagreeing --
  is information, and it is being discarded. Not reachable today: the wrapped
  registry is constructed inline per action and never escapes, so there is no
  path by which the authority sees a call the record does not.
- **`_equivalent_pending_proposal` opens and validates every proposal on the
  machine, every cycle.** O(all proposals ever created) with no index and no
  bound before the load.
- **Nothing in `src/` deletes a `research_runs` row.** 0015 traded three
  foreign keys and their insert-time validation for a retention capability that
  exists only as an operator running SQL by hand. That may still be the right
  call -- the provenance loss it prevents is real and was demonstrated -- but
  the migration's framing of pruning as "an ordinary retention action" is
  stronger than the codebase supports.
*(The refusal-feedback gap that was listed here is now fixed; see below.)*

## 15c. Being refused taught the objective nothing, twice

Worth its own section because it was found by running the system on real work
rather than by reading it, because it happened twice with two different guards,
and because both guards were correct.

```text
run_local_experiment   refused: "no preregistered design to run;
                        design_experiment must come first"
                        -> the successor cycle planned run_local_experiment

design_experiment      refused: "parameter 'out' must be relative, not
                        '/home/nicacevedo/.local/share/research-os/...'"
                        -> the successor cycle supplied another absolute path
```

Neither refusal is a defect. The preregistration guard is the mechanism that
makes "the criteria were fixed before the result" true, and the declared-
parameter type check is what `docs/EXPERIMENTS.md` means by "a value that does
not fit is refused, never escaped". Both messages name exactly what is wrong.

The defect is that a successor cycle is a new LangGraph thread seeded with
identity alone, so the refusal -- the single most informative thing that had
happened to the objective -- was invisible to the next planner. This is §16's
"seven cycles, one cycle's worth of information" in a second form: there the
frontier could not move, here the reason it could not move was recorded and
thrown away.

`RuntimeStore.last_refused_action` reads it from `tool_invocations`, not from
graph state, because graph state is precisely what the successor does not have.
`plan_one_action` renders it as one field and the planner prompt (version 3)
says what to do with it: satisfy what the refusal asks for, or choose a
different action, and say which.

The parent's refusal only, and clipped. One cycle back stops the immediate
repetition; a growing transcript of failures in a planning prompt is how a
planner starts reasoning about its own failures instead of about the project.

## 15d. An unresolved target is not automatically an experiment

The second thing running the system on real work found, after §15c, and the
more expensive one.

```text
HYP-0002   "the pricing subproblem has a finite infimum if and only if
            ||X' psi||_inf <= lambda_1"
planned    design_experiment, six times, six distinct spec digests
cost       about four dollars, ninety minutes
moved      nothing
```

No measurement decides a biconditional. `planner@5` could not stop it, and it
is worth being precise about why: `planner@5` closed the *digest* loop -- it
stopped a seventh preregistration of a design the project already had, by
showing the planner which hypothesis each stored design tested rather than its
digest. Every one of those six designs was different. Digest deduplication saw
six distinct pieces of work, correctly, and they were six distinct pieces of
the wrong work.

```text
unresolved hypothesis  !=  empirical experiment required
```

**Where the classification comes from.** `research_os/runtime/adjudication.py`
reads the target's own `statement` and `falsification`, weighting the falsifier
twice, because the falsifier is the sentence in which the researcher already
wrote down what would settle the thing. A hypothesis saying "Prove that ... for
every dual point the algorithm can reach" has *stated* that it is adjudicated
by proof. Reading that is not inference.

It is deterministic. The same object classifies the same way on every host and
in every cycle, and the verdict carries the literal words it matched, so a
person who disagrees can argue with the evidence rather than with an oracle.
Against the thesis capsule as committed, all seven hypotheses classify
correctly with no tuning.

**What it must never do, and structurally cannot.** An `AdjudicationKind` is
planning metadata about a capsule object. It is not stored in the capsule, it
participates in no scientific digest, it has no status a person could accept,
and it cannot retire, support or refute anything. It decides which *verb* the
runtime reaches for. If it is wrong the cost is a cycle spent on the wrong kind
of work -- a cost the system already pays -- and never a false scientific
statement.

Adding an `adjudication:` field to `Hypothesis` was the alternative, and it
would have meant a canonical schema change, a migration of every capsule on
disk, and a new way for an automated system to write a scientific-sounding
label into files a person is supposed to own.

**Two halves, and the prompt half is not the enforcement.** `planner@6` is
shown the block so it routes correctly the first time. `validate_plan` refuses
when it does not -- after every authority check, because this is the only
refusal there that is about scientific *fit* rather than about permission, and
conflating the two would send a planner hunting for a permission it already
has.

```text
design_experiment     refused when NOTHING the plan addresses could be
                      settled by measurement. A plan with a legitimate
                      empirical half is not refused for its other half.
derive_mathematics    refused for a target this project already holds a
                      derivation finding for -- by proposition, not by
                      digest, which is §15c's lesson one layer down.
```

Both refusals name the action that *would* answer the question, because §15c
established that the refusal text is what the successor cycle reads.

**Conservative in the direction that matters.** `UNDETERMINED` blocks nothing,
`MIXED` blocks nothing on its own, and a plan addressing nothing classifiable
is never refused. A target this build cannot classify behaves exactly as it did
before this module existed.

One asymmetry is deliberate and was a correction: repeat-work suppression is
checked *before* the classification filter, not after. Whether a derivation has
already been done is a fact about this project's findings, and it does not
become unknown because the hypothesis is worded without any signal vocabulary.
Checking it behind the `UNDETERMINED` filter left the loop reachable through
the one door that was still open.

**Somewhere to route to.** `derive_mathematics` is A0 and holds `READ_REPO`,
like the critique: it reads the capsule, asks a model, writes an artifact and a
noncanonical finding. `deriver@1`'s outcome enum is
`DERIVED | REFUTED_BY_COUNTEREXAMPLE | NOT_DERIVABLE_AS_STATED | INCOMPLETE`.
There is no `SUPPORTED`, because a deriver that could report a degree of
support would be reporting an experiment it did not run, and the schema
*requires* any suggested numerical witness to say what it would and would not
establish. `DERIVED` is a model's report that a derivation went through. It
reaches the capsule the way everything else does: a proposal, and a person.

## 15e. The human gate, made durable

Three defects the live thesis runtime exposed, all of them about the same thing:
a system that stops for a person has to *stay* stopped, and has to know why it
stopped without asking a model again.

### The frontier could not see what had already been asked

`frontier@1` received two things: the deterministic capsule frontier, and the
current cycle's previous action result. A real cycle asked it whether another
autonomous cycle was warranted. It ranked five candidates, put an audit of
whether the two outstanding proposals already covered the open questions at the
top, and wrote this into the finding it recorded:

> Absent HYP-0006's pre-specified test, the correct answer would have been
> WAIT_HUMAN. This judgement rests on the quoted frontier alone, as all
> file-inspection tools were disabled and the underlying artifacts could not be
> read.

So the highest-ranked action was one the role structurally could not perform,
and the recommendation it did give -- `START_NEXT_CYCLE` -- was qualified on
material it could not open. A role that ranks the next action was reasoning
about a project it could not see.

**The fix is not file access.** The material was structured scientific state
this runtime already held. `sciencecontext.noncanonical_science` had been
assembling it for the planner since the loop was closed, and the frontier was
simply not given it. `frontier@2` receives the same three labelled, fenced
categories the planner does -- completed findings, outstanding proposals,
preregistered designs -- plus the controller-authored census outside every
fence. It gained no path, no repository handle and no tool, and
`tests/test_runtime_frontier_context.py` asserts both halves: the context is
there, and nothing else is.

**One category was not enough as it stood.** A proposal reached the prompt as
the reservation row: `proposal_id`, `run_id`, `created_at`, and two counts of
findings. Every field is operational, so "is Q-0004 already in front of the
researcher" was not answerable from any of them -- which is exactly the audit
the frontier asked for files to perform. `sciencecontext.proposal_view` now
reads the proposal document by the id the reservation gives, and each entry
carries its proposed items with:

| field | what it decides |
|---|---|
| `addresses` | which capsule objects this item is about -- coverage, as a set intersection |
| `decided_by_human` | whether a person has promoted or declined it, so a settled item stops counting |
| `basis` | `current` / `STALE` / `unchecked`, so a proposal resting on moved objects stops counting |

Three answers on the basis and not two, for the reason §14a.4 gives about
`checkable=False`: "we did not check" must not read as "we checked and it is
fine". A caller with no repository path gets `unchecked`; the path comes from
the runtime's own context and never from the `project_path` recorded inside the
proposal document, because a reader that dereferenced a path it read out of a
model-written document would be letting the document choose what gets opened.

A proposal that cannot be read reports `items_unavailable` and says so in the
census, because a reader that cannot tell "no item addresses this" from "I
could not read the items" will treat the second as the first, conclude a
direction is uncovered, and spend a cycle on a question already in front of a
person.

The planner gets the items too. It has the same coverage question and had the
same five fields to answer it with.

### A repeated assessment was a new scientific object

A finding is deduplicated on `(project_id, digest)` and the digest is over the
finding's content. That is right whenever the content *is* the observation, and
wrong for a handler whose result contains a model's prose. Ask the frontier the
same question over the same scientific state twice: it reaches the same
recommendation over the same candidates, phrases the rationale differently, and
the artifact holding that rationale hashes differently -- so the digest differs,
and a second citable identifier is minted for one observation. The planner is
shown at most `MAX_PLANNER_FINDINGS`, so twenty repetitions fill the window and
push the project's real findings out of it, with nothing about the project
having changed. Operational repetition wearing scientific progress as a
costume, which is §16's pilot finding one layer down.

So a handler that knows which part of its result is the observation may say so.
`RuntimeFinding.semantic_key` is producer-authored, beside the excerpt and for
the same reasons, and when it is set the digest is computed from it, the
project, the kind and the producing action, and from nothing else.
`actions.review.assessment_identity` is the one producer that sets one, over:

```text
frontier_digest        the scientific state assessed, over sorted identifiers
recommendation         the conclusion the runtime acts on
ranked (action, sorted addresses), in rank order
```

and deliberately not over the rationales, the qualitative scores, the artifact
ids, the run, the cycle or the clock. Rank order *is* material, which is the
conservative direction: collapsing it would let a genuine change of mind about
what to do first be deduplicated away.

Two properties follow and are asserted. A finding with no stated identity keeps
the digest it has always had, byte for byte, so nothing a proposal already
rests on is restated. And a repeat keeps the *first* occurrence's wording,
because a finding is immutable and superseded rather than updated -- if a later
assessment's substance differs, its key differs and it is a new finding.

### A conclusion lived only in the message that carried it

The worst of the three, and the one that had already happened.

A cycle's recommendation was returned in `CycleResult`, which is memory, and
written into the `RESEARCH_CYCLE_FINISHED` event payload, which is an
operational message. The daemon copied it again into the `continue_objective`
work item, and `should_continue` read *that* -- two copies away from the row
that concluded it, in a queue row outliving the process, the build, and any
later correction.

`RRUN-20260918T054218Z-cb4962f4` asked the frontier whether another cycle was
warranted, was told `WAIT_HUMAN` with the reason that the questions were
already in front of the researcher, and recorded that in
`FIND-20260918T054359Z-313e1ec9`. The build of the day reached `WAIT_HUMAN`
only through `requires_human_promotion`, so the cycle concluded
`START_NEXT_CYCLE`, and that is the word that reached the event, the work item,
and the successor the daemon opened. `conclude` now honours the frontier's own
recommendation -- and that fix did nothing for the work item already on the
queue. It still said `START_NEXT_CYCLE`, and the next restart would still have
believed it.

A recommendation that lives only in a message cannot be reconciled, because
there is nothing to reconcile it against. So `research_runs.next_recommendation`
records what the run concluded, in the **same row update** that records that it
concluded, and `_work_continue_objective` reads the run:

```text
run.next_recommendation is not null  ->  authoritative; the payload is advisory
run.next_recommendation is null      ->  a build predating the column finished
                                         this run; fall back to the payload and
                                         say so in the result
```

The work item's result records `parent_recommendation_source`, and
`superseded_payload_recommendation` when the two disagreed -- because a
continuation decision taken from a message rather than from a run is a thing an
auditor should be able to find. The event still carries the recommendation, for
a reader following the ledger; it is no longer the authority on what the run
decided.

`parent_recommendation` and not `recommendation`, because
`_cycle_result_payload` already uses the latter for the *successor's*
conclusion on the success path -- two different facts under one name, which is
how an auditor ends up reading the wrong cycle.

`_work_continue_objective` also gained the `has_successor` pre-check and the
`SuccessorExistsError` catch that `_work_advance_objective` has had since
`sql/0014`. The two handlers disagreed about one invariant, and the live state
found the gap: a researcher cancelled the successor this item had opened, the
item's lease expired, and reclaiming it would have hit the unique index as an
*exception* -- three failed attempts and a dead-lettered work item, for a system
behaving exactly as designed. A cancelled successor counts as a successor.
Cancelling a research cycle is a person's act, and a queue row is not entitled
to undo it by being retried.

### What two independent reviews of this work found

Both were given the diff and told to attack it. The three worth recording are
each an instance of the same lesson: an assertion placed one layer away from
the property it names will pass while the property is false.

**The items were being clipped out of the prompt.** A proposal view is one
block *entry*; `prompt_safe_block` clips each entry at `DEFAULT_FIELD_CHARS`;
twelve items at `indent=2` serialise to 6 550 characters. Measured on the real
shape, **three of twelve** reached the model, the JSON was cut mid-object, and
`census` -- controller-authored, outside every fence -- reported all twelve. So
the set intersection this section is about was computed over a quarter of the
data for exactly the two live proposals that motivated it, and a question
already in front of the researcher read as uncovered.

The fix is in four parts and only one of them is a number: the item view
carries the fields coverage needs and not the adjudication aids, the title
bound drops to 80, both render sites serialise compactly, and
`PromptTemplate.block_limits` gives this block a measured
`PROPOSAL_BLOCK_CHARS`. The part that keeps it fixed is a test that renders a
worst-case proposal through the real graph and asserts every item id and every
address appears in the prompt. The test that missed it asserted on the view
object and said "so nothing is silently hidden".

**The identity fix had been applied to one exit of four.** `assess_frontier`
has three degraded exits -- empty frontier, budget or routing failure, unusable
response -- and none set a semantic key. Two of them conclude
`START_NEXT_CYCLE`, and their summaries embed the exception text:
`BudgetExhaustedError` names the run and the remaining budget, so the summary
was *guaranteed* unique per cycle. A provider outage minted a new citable
finding every cycle, which is this section's own defect on the path most likely
to repeat. All four exits are keyed now, over the state and the failure class
and never the message.

**And the identity covered less than the assessment did.** `frontier_digest` is
over capsule identifiers; the capsule cannot move without a promotion; so it is
constant for a whole autonomous session -- while this release had just given the
role three further categories. Two assessments either side of a researcher
declining an item would have been one finding, keeping the earlier rationale.
The key now carries `decision_context_digest` as well: per readable proposal,
its id, its undecided items, and the class of its basis. Findings stay out,
deliberately -- they change every cycle, so including them is the churn wearing
a different costume. What is in the key is what moves only when a person acts or
the science does.

**What it costs, measured on the real pilot rather than argued.** A reviewer's
strongest remaining assumption was that this block could take the frontier
prompt to ~40 KB, on a role called every cycle -- a cost fix that costs. Against
the live thesis capsule and its two real proposals (eleven items and twelve):

```text
instruction                  3 431 chars   (frontier@2, up from ~700)
outstanding proposals        6 109 chars   (both proposals, 23 of 23 items)
whole prompt                11 081 chars   ~2 770 tokens
```

Both proposals render in full, `33ec307e` reports `current` and `b9c26fcd`
reports `STALE` -- correctly: it was written before EXP-0001 completed, and
`EXP-0001 (specified -> completed)`, `HYP-0001 (active -> supported)` and
`HYP-0005 (active -> rejected)` have moved under it. So the coverage question
this section exists for is answerable from the prompt, and the older proposal
is visibly not a live decision. Three thousand tokens against a cycle that
costs about fifty cents and was being started for the wrong reason.

Three smaller ones, each with a test: `items_unavailable` carried
`proposals_root()` into a prompt that tells the role it has no filesystem, and
a test asserted its arrival; `semantic_key` was an extension point with no
shape contract, so a producer setting a constant would have merged every
finding of its kind, permanently and citably; and the frontier was shown its
own prior assessments in a block labelled as completed work, which on the one
decision that governs spending and stopping is a self-confirmation loop.

## 16. What two adversarial reviews and a real pilot changed

Recorded here because the findings are more useful than the fixes, and because
several were things the documentation asserted and the code did not do.

### The pilot's finding: seven cycles, one cycle's worth of information

The first real pilot ran against a live capsule and chained **seven** cycles,
each recomputing an identical frontier and concluding `START_NEXT_CYCLE`,
stopping only at `max_cycles_per_objective`. Fifteen model calls, 2.65 USD.

The cause is structural and worth internalising: **the runtime cannot change the
frontier its own planning is derived from.** It cannot write a capsule, so it
cannot retire a hypothesis, record an experiment, or move a claim. So "is there
still work outstanding?" is always yes, and a continuation policy that reads
only that will run until a ceiling stops it.

Continuation now hashes the frontier at conclusion and refuses a successor when
it is unchanged, saying that what remains needs a person. `frontier_digest` is
on the run row, so the reason is inspectable afterwards.

### Authority findings

| finding | what it was |
|---|---|
| preregistration skippable | `if declared and declared != digest` — omitting `spec_digest` skipped the check, and the design came from plan parameters, so the planner supplied both halves of the comparison. Now required, and must match a preregistration this runtime stored. |
| model-authored `argv` | the experimentalist wrote the command and it ran with `cwd` set to the canonical checkout. Now it selects one of the commands the *researcher* declared in `experiments.yaml`, with parameter values the v1 resolver validates. |
| `approve` had no guard | no TTY check and `decided_by="researcher"` hard-coded — a fabricated attribution on a scientific-authority record. Now refuses a non-TTY unless `--i-am-a-person`, and records the real user and host. |
| prose from an invalid capsule | `quotable_claims` checked only for a readable project identity, not `report.ok`. |
| a resume could assert a verdict | the approvals table is now the only verdict; an unrecorded decision is not granted. |
| `APPLIED` overwrote the verdict | applying a decision replaced `GRANTED`/`DECLINED` with `APPLIED`, so a replayed node read a *declined* gate back as granted. `applied_at` carries it now. |

One reported finding was **wrong**: `apply_decision` does check `human_executes`.

### The escape this runtime detects and cannot prevent

The coding pipeline runs the project's acceptance commands *after* the builder
has written files in scope, so `pytest` executes Python a model wrote one step
earlier, with the researcher's environment. Worktree isolation protects the
canonical checkout from the *builder*; it is not an OS sandbox, and
`SECURITY.md` has always said so. What R5 changes is that nobody decides to run
it.

A sandbox is the fix and this deployment has none. So `actions/coding.py` hashes
the canonical capsule and every Git ref before the pipeline and again after, and
a difference fails the action as `POLICY_REFUSED` with the paths named — and as
a policy refusal rather than a code failure, so it is not repaired and retried,
because repairing it would run the same escaping code again.

**This detects; it does not prevent.** It sees nothing that happens outside the
repository.

### Concurrency findings

| finding | what it was |
|---|---|
| the attempt cap was self-defeating | `_recover` emitted `WORKER_RECOVERED` for items it had just marked `FAILED`, and that event created a *new* work item with a *fresh* attempt budget. An item that killed three workers was replaced by one that would kill three more. |
| the ledger had no ownership guard | a slow worker could overwrite the record of whoever took over — and a late `mark_failed` over a completed action becomes permission to do it again. |
| `FAILED` bypassed the reconciler | but `perform` routinely raises *after* the effect lands (an `sbatch` that succeeded, then a database blip), so this submitted the same experiment twice. The exact failure the ledger exists to prevent. |
| events were lost on a crash | consumed in one transaction, enqueued in another, and nothing re-emits them. Events are leased now, like work items. |
| two workers, one thread | nothing serialised entry to one LangGraph thread; `RUNNING -> RUNNING` is not a guard. Added `LockClass.RESEARCH_RUN`. |
| every lock blocked forever | `wait=True` everywhere with no `lock_timeout`, and since `tick` runs work inline, one stuck holder froze the whole control plane. Every `RepositoryBusyError` handler was unreachable. `wait=False` is the default now. |
| escaped locks outlived their holder | the pool never cleared session state, and a session-scoped advisory lock is session state. |
| worker identity was not unique | host and pid, and it *is* the queue's only ownership guard. Pid reuse would have let a stale worker renew a lease it did not hold. Now includes a per-instance token. |
| the wall clock double-charged | charged from `started_at`, which never advances, so each resume billed the whole run again; the `least()` clamp then hid the overrun. |
| unreachable interpretation | `interpret_results` had no `ActionKind`, and the citation audit looked for its input in graph state, which does not cross a cycle boundary. Artifacts are linked to their run now, and both find their input in the durable record. |

### The third review: what the loop-closure work itself got wrong

Three independent adversarial audits ran against the integration branch, one per
area. Their findings are below, and none of them was about the feature set --
every one was about a claim this documentation or a docstring made that the code
did not support.

**Authority.**

| finding | what it was |
|---|---|
| the citable ids lived inside the fence | `render_supplied_findings` says the duplication *is* the mechanism: the fenced block is what the worker reads, the plain list is what it may cite, and the second must not be influenceable by the first. Only the *correction* prompt carried a list. The prompt that writes the proposal said "the finding ids in the evidence blocks below" — the ids existed in exactly one place, next to text a model wrote. |
| the record showed text the worker never read | the prompt clipped a finding at `MAX_FINDING_CHARS` (1500); the stored `SuppliedFinding` kept up to `MAX_SUMMARY_CHARS` (4000). A conclusion placed past the prompt's cut appeared in `propose show` and never reached the worker. `clipped_statement` makes the two one string. |
| grounding was checked in one direction | justified by a caller supplying ids whose text travels in a separate `analysis_data` block. There is no such caller, and nothing passes `analysis_data` to `build_proposal_prompt` at all. Both directions now: citable implies quoted, quoted implies citable. |
| the links over-claimed | `runtime_proposal_links` said "the findings this proposal actually cited"; the writer inserted the whole packet. Eight offered, two cited, six rows asserting a dependence that did not exist. `cited` separates them — `0011`. |
| every runtime proposal sorted as 1970 | a reserved id's timestamp is derived from the reservation key so a retry can recompute it, so it is a deliberate placeholder. Listing by id put every runtime proposal ahead of everything the researcher proposed. The listing reads `created_at` now. |
| a refusal with no callers | `refuse_scientific_authority` advertised "the list of refused actions is greppable". There is no list; the enforcement is the absence of write methods, the `human_executes` policy, and a structural test. The docstring says that now. |
| no cross-cycle dedup | two cycles grounded in the same findings produced two proposals saying the same thing. An equivalent *pending* proposal now short-circuits; a promoted or declined one does not, because re-offering either is arguing with a decision. |

**Containment.**

| finding | what it was |
|---|---|
| `executor: slurm` was a one-word bypass | `build_executors` raises the local executor to `required` at high autonomy and passed the mode only to the local one. The Slurm path never reached `contain()`, and nothing in this process can contain a process on a compute node. It now *refuses* at `required`, as `WAITING_FOR_EXTERNAL_DEPENDENCY`. |
| the batch job inherited the daemon's environment | sbatch defaults to `--export=ALL`, so the runtime's provider keys and DSN were copied into every job. `--export=NONE` plus the frozen spec's own exports. |
| containment made the command vanish | `uv` installs to `~/.local/bin`, which is in neither the read-only OS binds nor the sandbox PATH, and uv's cache is under a home the sandbox replaces with a tmpfs. A contained acceptance run exited 127, recorded as a failure of the code the worker had just written. The program is bound at its own path and uv's cache and interpreter directories are made writable. |
| invisible characters survived the display boundary | `terminal_safe` escapes control characters, which is the *terminal's* threat model. `\u202e` contains none and reverses everything after it, so a finding read one way and archived another — at the boundary the authority model exists to protect. `DECEPTIVE_CHARS` covers bidi, zero-width, separator and tag characters, deliberately and narrowly. |
| `--share-net` promised a middle setting | there is none. Bubblewrap has no packet filter and this build adds none; `network=True` is the host's network namespace, including 127.0.0.1. |

**Concurrency.**

| finding | what it was |
|---|---|
| two workers, one experiment, one identity | `eligible_job_for_interpretation` then `claim_interpretation` is a check-then-act. Both workers did the whole reading and the unique constraint discarded the loser's. `claim_next_interpretation` does both in one transaction with `for update of j skip locked`. |
| the uid-scoped rlimit, a second time | `process_limit_preexec` was applied to *uncontained* children too. `RLIMIT_NPROC` is counted per uid, not per process tree, so against this user's existing 1119 threads a ceiling of 512 meant the child could not create its first thread: `uv` aborted with SIGABRT and seventeen tests failed with "required acceptance commands failed". The same mistake as `LimitNPROC=256` in the systemd-run probe, in different code. Written down in the function now: there is no per-process-tree process limit in POSIX rlimits. |
| `lock_run` serialised nothing | it took a transaction-scoped advisory lock inside its own `tx()`, which committed before the function returned. The lock was released before `has_successor` ran and long gone before `start_cycle` inserted. Replaced by a partial unique index — `0014` — so the loser fails at the insert whenever it arrives. |
| the preregistration horizon | the guard scanned this project's newest 500 preregistrations and compared each `spec_digest` in Python. Past 500 the older ones fall out of the window, and the older ones are exactly the experiments still waiting to be interpreted; the refusal was permanent and said "no preregistration found", which was false. The digest is in the artifact role now, so the lookup is an indexed equality match — `0012`. |
| two reads, one claimed moment | `observed_digests` said both digests came from one read of the capsule and called `validate()` twice. A researcher's commit landing between them paired a capsule digest from before it with a frontier from after. |
| pruning a run erased interpretations | `external_jobs.run_id` cascaded, alone among the schema's external-effect relations, and `experiment_interpretations.job_id` cascades from it. Deleting one old run destroyed the record that its experiments had been interpreted, while the project and the artifacts survived — `0013`. |
| two constraints the proof did not cover | `test_each_status_constraint_matches_its_python_enum` is parametrized over `ENUM_CONSTRAINTS`, so a constraint absent from the dictionary was never checked. `runtime_finding_refs_kind_ck` and `runtime_proposal_reservations_status_ck` had no enum. The reverse test now asks the database what value lists it has. |
| the reconciler recomputed the id | correct only for as long as two functions agreed. It reads the reservation row and derives only when there is none. |

### What the second pilot found, which no audit and no test did

The second pilot ran three times before it produced a verdict, and each failure
was a real finding.

**The budget was silent about what the run cost.** `runtime run` reported *one*
model call for a cycle that had made four, and a run started with
`--max-cost-usd 6` reported a tenth of what it had spent. The router reserves
`MODEL_CALLS` and `MODEL_COST_USD` before every call *it* makes; it makes none
of the calls inside a delegated action. `propose_capsule_change` hands the work
to the v1 `ProposalController` and the coding action to `AutomationController`,
both of which own their own providers and their own per-action call ceilings,
and neither had ever touched the runtime's ledger.

Nothing was unbounded -- each delegated action passes a ceiling -- and the
*cost* cap did not apply to those calls at all, which is what makes this a
defect rather than a reporting nit. `BudgetLedger.charge_all` records a spend
that has already happened, past the limit when it has to, and reports which
budgets it broke; `charge_delegated_spend` calls it with the invocation records
the v1 controllers return, on the success path and on the failure path both,
because a failed proposal's model calls cost exactly as much as a successful
one's. It cannot refuse -- the money is gone -- so the cap now bites on the call
*after* the overrun, which is weaker than a reservation and is the strongest
thing that is true.

Found by reading a pilot's run report next to its provider invocations. Nothing
asserted that the two agreed, and three adversarial audits did not look.

The size of it, measured on the second pilot after the fix: twelve calls and
2.3888 USD, of which five rows marked `delegated:propose_capsule_change` account
for 1.6478. The same run under the previous code would have reported 0.7410 --
a 3.2x under-report on a cycle with no failures in it.

**The cross-cycle proposal dedup had never once matched anything.** The check
compared `FindingPacket.digest` against the `finding_packet_digest` a proposal
stores in its basis snapshot, and those are two different functions over
different material -- the runtime's hashes `(finding_id, RuntimeFinding.digest)`
and v1's `supplied_findings_digest` hashes `(finding_id, kind, statement,
rests_on)` under its own prefix. They cannot be equal, so the comparison
returned `None` every time and the mechanism was decoration. It now compares
v1's digest against v1's, which is the one the proposal stores and the one a
promotion re-checks; the two values also stopped sharing a name in the run
payload, because one name for two digests is how a run report and a stored basis
came to disagree about "the" digest.

Two things follow from it working. **A promotion is not an answer to the items
it did not touch:** the pilot promoted one of nine items, the dedup skipped the
proposal because it "had a promotion", and the successor cycle re-asked the
other eight over an unchanged packet. The predicate is now "still has items
nobody has acted on". And **this cycle's own reserved id is excluded**, because
a crashed earlier attempt of the same cycle leaves exactly that directory and it
has to reach the recovery path -- which settles the reservation and writes the
finding links -- rather than being reported as somebody else's equivalent
proposal.

A third thing follows that is *not* fixed here: **there is no record of a
decline.** `ProposalStore` writes promotions and nothing else, so a researcher
who reads a proposal and rejects it cannot say so, and it stays pending forever.
An earlier docstring asserted that "one they declined is answered", describing a
state the system cannot represent. `docs/RELEASE_CANDIDATE_REPORT.md` §J records
the gap.

**A legitimate planner decision killed the harness.** Attempt two: the planner
chose `propose_capsule_change`, the worker's output put a capsule id
(`HYP-0001`) where a proposed item id belongs, the validator refused it -- which
is exactly what `test_proposal_grounding_correction.py` exists to assert -- and
the action failed correctly. The pilot script then died at phase 2 under
`set -o pipefail`, with no verdict, because "phase 1 produced no proposal" exits
3. A harness that a legal outcome kills is a harness that can only report the
outcome it expected. It now reports and explains it, verifies the researcher's
project is still unchanged, and exits with that code.

**The project-untouched guard cried wolf about the wrong repository.**
`fingerprint_source` read `git rev-parse HEAD` and `git status --porcelain`
unconditionally, which for a project vendored *inside* another repository
reports the enclosing repository's HEAD and dirty-file list. Attempt three
passed all four phases and then failed with "the pilot modified the
researcher's real project" -- because a commit had landed in the outer
repository while it ran. Every `.research` hash was byte-for-byte identical. Git
state is now included only when the source really is its own work-tree root, and
the file hashes -- extended to cover the whole project, not only `.research` --
are the check either way.

**The checksum guard refused its own author.** Attempt one reached phase 4 and
stopped on `migration 0014 was applied with a different checksum`: this session
had edited `0014` between phase 3 and phase 4, and the pilot's disposable
database had already applied the original. The mechanism worked, on the person
who wrote the rule, minutes after writing it. The operational lesson is narrow
and real -- a long-running pilot holds a migrated database across its phases.

### The fourth review: the final adversarial pass

An independent review of the finished branch, told to attack this release's own
report rather than the diff. It found two escapes, and one of them was the worst
defect in the release.

| severity | finding | what it was |
|---|---|---|
| critical | the acceptance gate had no display boundary | `src/research_os/cli.py` contained **no use of `terminal_safe` at all**, so `researchctl review` -- the one command that records human acceptance of a Claim -- interpolated `claim.statement` raw immediately above the `approve/revise/reject` prompt. Model text reaches a capsule file through a promotion, `_dump` writes it with `allow_unicode=True`, and a statement ending `\x1b[1A\x1b[2K` erases and rewrites the line above it: the sentence on screen when the reviewer answers is not the sentence being digested, while the digest prints underneath saying it is. Every report module wrapped its output; the highest-authority screen did not. |
| critical | containment granted host code execution | `uv_support_paths` put `~/.cache/uv` and `~/.local/share/uv` in **`writable`**, so they were bound `--bind`, not `--ro-bind`. The managed interpreter there is what nine virtual environments on this machine execute and its `lib/python3.12` is writable by this user, so contained model-written code could drop a `sitecustomize.py` and the next `uv run` *anywhere on the host* would execute it outside the sandbox. Strictly worse than the `.git`-hook escape: `canonical_fingerprint` sees none of it, nothing has to be checked out, and it reaches every project on the machine. Introduced by this release, three hours before the review. |
| high | a failed coding run charged nothing | `AutomationController.execute` fails the run on disk and re-raises, so the charge placed after it was unreachable on every ordinary failure -- a reviewer returning FAIL, acceptance commands failing, any `ProviderInvocationError`. `CODE_EXCEPTION` maps to REPAIR, so the uncharged spend repeated per attempt. `try/finally`, reading the invocations from the store. |
| high | `UV_CACHE_DIR` chose an arbitrary bind | the variable was read from the daemon's environment and used verbatim as a path, with no absolute or `..` check: `UV_CACHE_DIR=/` bound the whole filesystem read-write. Closed by the same change that made the uv paths read-only. |
| high | 0014's precheck was not atomic with its index build | two statements in READ COMMITTED with no `lock table`, so a successor committed between them is invisible to the precheck and fatal to the build -- the operator gets verbatim the raw error the precheck exists to replace. `lock table research_runs in share mode` first. |
| medium | 0013's FK rebuild could not finish under load | ACCESS EXCLUSIVE against a pooled `lock_timeout=30s`, in one transaction with 0011-0014, so one daemon tick's row lock rolled back the whole upgrade. `set local lock_timeout = '0'`. |
| medium | `redact_dsn` printed the password in full | for a libpq URI with no path component (`postgresql://host?password=...`) the query string was swallowed into the authority and never parsed; a quoted keyword value leaked its tail. Both reach `researchd`'s startup log. |
| medium | a parameter could choose the program | `_argv_is_plain` checks the *unsubstituted* argv, so a command declared `argv: ["{tool}", ...]` passed the bare-name rule and substitution then put a supplied value in argv[0] -- defeating the rule's stated purpose. Re-checked after substitution. |
| medium | `_program_binding` would bind an arbitrary path | `argv[0] = "/"` produced `--ro-bind /`; `./../../../../etc/shadow` produced `--ro-bind /etc/shadow`. Closed by rejecting `..` and requiring a regular executable file. |
| medium | `DECEPTIVE_CHARS` missed thirteen ranges | including `U+2064`, which reproduces the *exact* identifier collision the set's own docstring describes, and `U+E0100`-`U+E01EF`, the variation selector supplement -- the tag range stopped one code point short of it. |
| medium | `research/report.py` escaped per-field and incompletely | `section`, `read_paths`, `allowed_paths` and the *keys* of `experiment_parameters` were raw. `allowed_paths` is the line a researcher reads to learn what an autonomous task may write. |
| low | several | `charge_all` took a `work_id` it never used and both callers discarded its return; the `cited` upsert OR-ed across retries, re-acquiring the padding `0011` removes; two indexes had no reader that could use them; `0014`'s `if not exists` matched on name only; `render_approval` escaped values and not keys; the notifier logged unescaped; `insight search --packet` silently folded characters away. |

**What it did not find, having tried:** no authority escape, no double-charge in
the new budget accounting, no orphan-row or weakened-FK defect in 0013's
rebuild, no second module reaching the capsule, and no `A2` action with a
handler. It independently re-derived that the migration upgrade test's split at
`0005` is correct, and confirmed the full-suite count.

**One finding is reported and not fixed.** `artifact_links.run_id` is part of
that table's primary key, so it cannot be made nullable without a key
migration -- and the preregistration guard scopes its lookup through that table.
Pruning a run therefore still makes the guard refuse permanently. The refusal
message was corrected to say the link is unreachable instead of claiming no
preregistration exists; the migration carries the warning;
`docs/RELEASE_CANDIDATE_REPORT.md` §J carries the work.

### A suggestion that was wrong, and why

The review proposed passing `role=` to `authorize` so `ROLE_PERMISSIONS` would
be enforced. Trying it refused literature search and every coding task.

An *action's* permissions say what the runtime needs to perform it —
`NETWORK_READ` to query OpenAlex, `RUN_LOCAL` to run a project's tests. A
*role's* say what a model may be handed. The extractor does not "hold" the
network and the author does not "hold" the test runner; the runtime does, on
their behalf. Intersecting them is a category error.

Role least privilege is enforced at the provider boundary instead, and more
strongly than a permission check would: every model request this runtime builds
is `read_only=True` with no `access`, which resolves to `CONTEXT_ONLY` and
force-empties the tool set. Every runtime model call is tool-less — it cannot
read a file, run a command, or reach the network, whatever its role says.
`tests/test_runtime_routing.py` asserts it for five roles.

## 17. The provider-failure lifecycle

Added after the pilot of 2026-09-19, which is the first time this runtime ran
unattended across a real infrastructure failure. Everything in §14a worked: a
researcher committed a question, the capsule observer noticed exactly one
change, the digests moved, the parked objectives were found, successor
uniqueness and replay deduplication held. Then a provider went away for five
minutes, and the runtime said things that were not true.

This section is the contract that replaced the assumptions.

### What happened

An interactive Claude Code session was running on the same workstation as
`researchd`, sharing one OAuth token. Their refreshes collided:

```text
Failed to refresh OAuth token: another Claude Code process is refreshing it
or exited mid-refresh.
```

Three planner calls failed, the breaker opened for 300 seconds, and five
defects followed from one root cause plus four gaps it fell through.

| # | defect | consequence |
|---|---|---|
| D1 | a failed model call became `plan_refusal`, and `conclude` mapped any refusal to `DONE_FOR_NOW` | three runs reported as `SUCCEEDED / DONE_FOR_NOW`, and `runtime status` told the researcher they had "finished cleanly" and a decision was theirs |
| D2 | retry backoff (30s, 60s) fitted inside the breaker cooldown (300s) | every attempt fell in a window where routing was guaranteed to refuse; a transient failure became permanent by arithmetic |
| D3 | an exhausted work item left its run `RUNNING` with no live work and no reaper | three runs in flight for twenty-one hours |
| D4 | `parked_objectives` excludes objectives whose newest run is in flight | those three objectives left the research frontier permanently; five parked objectives became two |
| D5 | `advance_objective` isolated only `SuccessorExistsError` | the fifth objective was never evaluated and received no disposition |
| — | every failed cycle still opened a lineage link | six of twelve cycles spent on runs in which no science occurred |

D1 is the one that matters. The other four are recoverable; D1 is a false
scientific statement, made to a person, by a system whose entire purpose is to
be trusted with the parts of science that are not judgement.

### I1. A conclusion requires an execution

> No run may reach `SUCCEEDED`, `DONE_FOR_NOW` or `WAITING_FOR_SCIENTIFIC_DECISION`
> because a provider or model call failed.

Enforced at one place rather than eleven. **The router raises when the provider
did not answer**, and returns a response only when it did:

| what happened | what `ModelRouter.complete` does |
|---|---|
| the adapter could not be invoked | raises `ProviderCallFailedError(PROVIDER_TRANSIENT)` |
| the provider exited non-zero, or reported an error | raises `ProviderCallFailedError(PROVIDER_UNAVAILABLE)` |
| the provider timed out | raises `ProviderCallFailedError(PROVIDER_TIMEOUT)` |
| the provider answered, but not in the shape asked for | **returns** a response with `error="malformed structured output"` |

The last row is the whole distinction. A model that answered badly is a fact
about the model, and a node may reason about it. A provider that did not answer
is not an answer at all, and there is nothing to reason about — so it leaves
the graph as an exception and reaches the work queue, which has the right
vocabulary for it: a failure class, a retry policy and a schedule.

Before this, eleven call sites each decided what a non-`ok` response meant.
Most called it `MODEL_OUTPUT_INVALID`, which is wrong quietly. The planner node
called it a refusal, which was wrong loudly.

**And the handlers stopped catching it.** Nine action handlers wrapped their
model call in `except (BudgetExhaustedError, RoutingError)` and returned an
`ActionOutcome`. That looked careful and was the opposite, for two reasons an
independent audit found after the first version of this fix:

- an `ActionOutcome` carries a failure class and nothing else — not the
  breaker's `cooldown_until`, not whether an invocation happened. Both are
  what the queue needs, so every provider failure through that door was
  rescheduled by the linear backoff alone and charged an attempt it had not
  spent. D2, intact, on a second path;
- `assess_frontier_ranked` returned **`succeeded`** with
  `recommendation: START_NEXT_CYCLE`, so an outage there produced a cycle that
  concluded `DONE_FOR_NOW`, recorded a citable finding, was offered to the
  next capsule change as parked, and opened a successor. D1 and the
  cycle-budget loss together, on the action the planner reaches for most.

So `RoutingError` now propagates out of every handler. `BudgetExhaustedError`
is still caught, and the difference is the point: a budget refusal is a policy
answer, no amount of waiting changes it, and degrading to the deterministic
frontier is the honest thing to do. An outage is temporary and the honest
thing is to wait.

`parse_literature` is the one exception, and keeps a per-*document* catch: one
unreadable paper among four must not lose the other three. When nothing at all
was extracted it re-raises the first outage rather than reporting it, so both
properties hold at once.

### I2. Infrastructure and science are different states

No new states were added; four existing ones were being conflated.

| state | means |
|---|---|
| `SUCCEEDED / DONE_FOR_NOW` | the cycle ran and there is nothing further it may usefully do |
| `SUCCEEDED / WAITING_FOR_SCIENTIFIC_DECISION` | the cycle ran and reached a gate only a person can pass |
| `RUNNING`, retry scheduled | transient infrastructure blockage; the queue is working on it |
| `FAILED / FATAL_INFRASTRUCTURE_ERROR` | the cycle could not be run, and recovery gave up |

`FATAL_INFRASTRUCTURE_ERROR` is deliberately **not** accepted by
`parked_objectives`: a run that concluded nothing is not offered to the next
capsule change as though it had.

### I3/I4. No run stays in flight with nothing to run

`Daemon._reconcile_runs` runs every tick, between `_recover` and
`_observe_capsules`. It asks one question, of every run, without naming any:

> Is this run `RUNNING` or `CREATED`, with no live work item, no outstanding
> external job, and untouched for `run_reconcile_grace_seconds`?

Deliberately not *why*. A worker that segfaulted, a daemon killed between
`open_cycle` and the ingest pass, a provider that vanished mid-cycle, an item
that exhausted its attempts — all land in the same state, and a reconciler with
a case per cause would have missed the next one.

Two exclusions are load-bearing and were both found by review rather than by
design. A run holding the **advisory run lock** is being executed right now —
`_work_advance_objective` runs cycles inline, so between `open_cycle` and the
ingest pass a healthy run has no work item at all, and a cycle slower than the
grace period would otherwise be diagnosed as stranded. And the **active
external-job** list is derived from `ACTIVE_JOB_STATUSES` rather than written
out: the hand-written version named `QUEUED`, which is not a member and which
the check constraint rejects, and omitted `SUBMITTING`, `PENDING` and
`UNKNOWN` — the statuses a cluster job actually waits in.

Two dispositions:

- **reschedule**, while the run is under `max_run_reschedules` *and* the
  blocking condition has visibly lifted. A fresh `run_cycle` item re-enters the
  **same** run at its own LangGraph checkpoint. No successor, no lineage link,
  no cycle charged, and the planning already done is reused;
- **abandon**, past the bound: `FAILED / FATAL_INFRASTRUCTURE_ERROR`, with the
  failure class and error in `detail`.

Idempotence comes from a dedup key naming the reschedule ordinal, which two
daemons compute identically; the bound is counted from `RUN_RESCHEDULED` events
in the append-only ledger, so a restart cannot reset it.

Recovery needs no capsule change and no SQL. That is the point: a capsule
change is a *scientific* act, and needing one to recover from a network failure
would mean the runtime could only be repaired by doing science.

### I5. Retry against availability, not against a stopwatch

The backoff and the breaker were two clocks that had never been compared. Now:

```text
scheduled_at = max(now + backoff × attempts, provider cooldown_until)
```

and, separately, **a call that never happened does not cost an attempt**. When
routing refuses because every eligible provider is cooling, the item is
*deferred* — rescheduled past the deadline with the attempt refunded, bounded by
`max_parks` so an outage that never ends still terminates. A real invocation
that fails still costs its attempt; it is simply not rescheduled into a window
where it cannot succeed.

**And the deadline is looked up, not only inherited.** The router attaches the
breaker's `cooldown_until` to what it raises, which is enough for the planner
call and not enough in general: a handler that catches a provider error and
reports it, or a broad `except ResearchOSError` two frames up, yields a
failure with the right class and no deadline. So `_finish_failed` asks the
breaker directly whenever a provider-class failure arrives without one. That
makes cooldown-aware scheduling a property of the failure *class* rather than
of how carefully each of nine call sites preserved an exception — which is the
same reasoning as I1, applied to the retry policy instead of to the terminal
state. It returns nothing when some provider is already usable, so an ordinary
transient error still retries on its ordinary backoff.

Note what was *not* done: `max_attempts` was not raised until the numbers
happened to overlap. That hides the same defect behind different magic numbers
and breaks again the first time either setting changes.

While fixing this, a second defect surfaced: `provider_failure_threshold` and
`provider_cooldown_seconds` were settings nothing read. The router called the
store with default arguments that happened to equal them, so changing either in
`runtime.yaml` changed nothing. They are wired now.

### I6. One objective's failure is one objective's failure

`_work_advance_objective` isolates every objective, records a disposition for
each as an `OBJECTIVE_DISPOSITION` event keyed by `(objective run, capsule
digest)`, and only then re-raises the first infrastructure failure so the item
is retried. Objectives that advanced already have successors, so the retry
skips them: isolation does not cost idempotence.

Dispositions: `ADVANCED`, `STILL_PARKED(reason)`, `FAILED_TO_ADVANCE(reason)`.
"Never reached" is no longer expressible.

A related observability gap is closed with it. `_finish_failed` recorded
`WORK_FAILED` only when the item had a `run_id`, and `advance_objective` has
none — so the one work item that explains how five objectives became two
emitted no event at all, while the three cycle failures around it did. The
trail looked complete.

### I8. Infrastructure does not spend the cycle budget

> A failed provider refresh, network failure or breaker cooldown must not count
> against `max_cycles_per_objective`.

`max_cycles_per_objective` is a *scientific* allowance. It exists because the
runtime cannot change canonical state, so repeating a cycle repeats its cost
without adding information (§16). An OAuth collision adds no information
either, and must not be charged the same way.

The mechanism is structural rather than a counter: **recovery re-enters the
existing run**, and a run has exactly one lineage link whatever happens inside
it. `lineage_depth` is measured over `parent_run_id`, so a run rescheduled
fifty times is still one cycle.

What *does* still cost lineage is a successor opened by a cycle that genuinely
concluded. That is correct, and it is why I1 matters to I8: before the fix, a
cycle that failed on a provider concluded `DONE_FOR_NOW`, and the runs it went
on to open were real lineage spent on nothing.

### I9. The operator surface says what is true

`runtime status` put every parked run under **"WAITING FOR A SCIENTIFIC
DECISION (finished; you are next)"** and added "These finished cleanly." That
was true of most rows and catastrophically false for the rest.

Three sections now, and the claim "you are next" is made only for the terminal
state that means it:

- **STALLED (in flight, but nothing is running)** — runs the reconciler is about
  to repair, or cannot. Shown first, because a stranded run is neither of the
  states around it and is the only one here that is a fault rather than a
  question;
- **WAITING FOR A SCIENTIFIC DECISION** — `WAITING_FOR_SCIENTIFIC_DECISION`
  only;
- **PARKED (nothing is owed; they resume when the science moves)** — everything
  else, with its recommendation and the reason it stopped.

### Provider authentication, and what this deployment can promise

The trigger was not random noise. `researchd` invokes the provider CLI as a
subprocess, inheriting this user's environment, so it reads the same
`~/.claude` and the same OAuth token as every interactive session on the
machine. Concurrent refreshes collide. This will recur.

What was checked rather than assumed, on this machine:

- `claude auth status` reports `configDirectory: ~/.claude` and
  `authMethod: claude.ai` — shared credential state, confirmed;
- `CLAUDE_CONFIG_DIR` **is** honoured: pointed at an empty directory the CLI
  reports `loggedIn: false` and a config directory of its own. An isolated
  credential store for the daemon is therefore possible;
- the CLI documents `ANTHROPIC_API_KEY`, and `claude setup-token` creates a
  long-lived token.

All three require the researcher to authenticate. Installing a credential is a
human act, this runtime does not copy secrets, and nothing here may enter Git —
so isolation is **available and not configured**, and `deploy/researchd.service`
says how to configure it.

That is why the recovery path above is the load-bearing answer rather than the
fallback. The acceptable outcomes for concurrent use are:

```text
A. independent credentials      -> both processes work
B. shared credentials collide   -> the provider is briefly unavailable, work
                                   waits, work resumes, and no lifecycle state
                                   is corrupted
```

B is what this deployment provides, and B is what the regression suite holds.
Unacceptable — contention producing `DONE_FOR_NOW`, a decision request, a
permanent orphan, or a lost objective — is what each of I1 through I9 forbids.

### What the two independent reviews found in this fix

Recorded because the findings are more useful than the fixes, and because four
of them were defects the first version of this closure introduced.

| review | finding | disposition |
|---|---|---|
| test | the action layer dropped `retry_at` and `attempted`, so D2 survived on every path through a handler | fixed: `RoutingError` propagates |
| test | `assess_frontier_ranked` turned an outage into `succeeded / START_NEXT_CYCLE` | fixed: only the budget degrades |
| test | `lineage_depth(run_id)` on a parentless run is `0` unconditionally — the cycle-budget assertion could not fail | fixed: measured over every run of the objective, with a control that moves it to 1 |
| test | the end-to-end reachability check scanned rows the scenario never touches, and could not detect D4 | fixed: measured through `parked_objectives`, with a positive control asserting D4 reproduced first |
| test | the replay test replayed the *ingest* dedup key, so the handler ran once | fixed: the handler is invoked three times directly |
| test | one test asserted the behaviour of its own test double | deleted; the real proof is in `test_runtime_routing.py` |
| test | nothing exercised `default_model_factory`, so the newly-wired breaker settings could be deleted with the suite green | fixed: a test through the real factory, and one through the real router and daemon with no router double |
| security | `printf 'CLAUDE_CONFIG_DIR=%%s\n'` writes a literal `%s` — the documented remedy for the incident did not work | fixed |
| security | `stranded_runs` tested external jobs against a status the schema forbids and missed three real ones | fixed: derived from the enum |
| security | a crash between the reschedule enqueue and its event wedged the run permanently | fixed: the event is recorded either way, and it has its own dedup key |
| security | `.gitignore` did not match `researchd.env`, the file the new guidance fills with secrets | fixed: `*.env` |
| security | `next_recommendation` was the one field rendered without `_safe` | fixed |
| security | the unit's hardening block claims a filesystem boundary that does not bind in a user manager on this host | corrected, with the measurement in the file |
| security | `sandbox.py:_declared_base` lets a model-written `pyvenv.cfg` choose which host Python prefix is bound | **not fixed, reported.** Containment is not implicated in this incident and the mission scopes it out. Read-only exposure of a real Python runtime, not an escape. It belongs in a containment work package with the `SECURITY.md` §190 wording it contradicts |
