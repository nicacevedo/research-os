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
```

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
| `model_cost_usd` | reserved at the profile's estimate, settled at the provider's reported cost |
| `external_jobs` | reserved before submission |
| `work_items` | reserved before a local experiment |
| `wall_clock_seconds` | **charged after the fact**, per cycle entry |

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
recover   expired leases, stale invocations, stale reservations
ingest    unconsumed events -> queued work
schedule  due schedules -> events (never work directly)
poll      external jobs -> reconciled status, and an event when finished
surface   pending approvals -> one notification each, ever
claim     one due work item -> run it under a renewing lease
```

Recovery comes first so a restarting daemon cannot pick up new work while old
work sits stranded. Claiming comes last so every pass leaves the system
recovered even if the worker then dies.

**It calls no model.** That is how invariant 2 survives a background process
existing, and `tests/test_runtime_daemon.py` asserts it by parsing `daemon.py`
for a `complete` call. Frontier reasoning happens inside a claimed work item
against a reserved budget, never in the scheduler.

**The event-to-work table** is twelve lines in `daemon.py`, so "why did this
run" is answerable by reading it. One subtlety: the continuation work item's
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
researchctl runtime doctor        whether the runtime can run here
```

Every view has a deterministic `--json` counterpart. Every string that came from
a model or a fetched document passes through `research_os.textsafe` on the way
to a terminal, with newlines and tabs stripped too — a table cell is not a place
for either.

`runtime doctor` follows `researchctl doctor`'s contract: an absent capability
is a WARN and exits zero. It also reports how many policy actions have no
handler in this build, so the gap between "the authority rules know about this"
and "this build can do it" is visible rather than discovered when a planner
picks one.

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
