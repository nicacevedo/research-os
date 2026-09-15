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
