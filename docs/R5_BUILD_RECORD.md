# R5 build record

What was built, what it was verified against, what it deliberately does not do,
and what remains. Written so a fresh reader — or a fresh agent — can reconstruct
the state without this conversation.

Follows the convention of `docs/V1_BUILD_RECORD.md`: a record of a point in
time, not a specification. `docs/RUNTIME.md` is the live specification.

## 1. Starting point, verified

```text
starting branch   r0/kernel-v1
starting SHA      b3fd03c549c329ad21efb7ae203a9acc7387efa0
v1.0.0 tag        b3fd03c549c329ad21efb7ae203a9acc7387efa0   (HEAD == tag == origin/main)
working tree      clean
baseline tests    `uv run --frozen pytest -q` -> 2362 passed
implementation    r5/autonomous-runtime
```

One thing worth recording about the baseline: running `pytest` *without*
`.venv/bin` on `PATH` produces 89 failures, because several tests shell out to
`pytest` as an acceptance command. That is an artefact of invocation, not a
defect. `uv run pytest` is the supported form and CI's.

`release/v1.1.0-autonomy` (2fa935c, +20 commits) exists and was **not** used as
a base. It is a separate in-flight release on the same kernel; R5 is additive
and lives almost entirely in a new package, so the two should merge with
conflicts only where both touch `cli.py`.

## 2. What was adopted, and what forced it

`ARCHITECTURE.md` §12a records this in full. In short:

| technology | the requirement that forced it |
|---|---|
| PostgreSQL | `for update skip locked`; server-side lease deadlines; a lock released by the server when its holder dies. SQLite has none of the three |
| LangGraph | durable checkpointing with interrupt and resume across process death — measured on the pinned version, not assumed |
| a background process | there is no other way to reclaim a lease from a worker that is gone |

Refused: Kubernetes, RabbitMQ, Redis, Celery, Temporal, a vector database,
mandatory containerisation, a web dashboard. None removes a bottleneck this
deployment has.

## 3. What the measurements changed

Four things were measured rather than read, and each changed the design.

**A process killed inside a node re-runs that node on resume.** Reproduced: the
effects log went `A` → `A A`. Hence the invocation ledger, and hence
`replay_safe` on every registered action.

**A side effect before `interrupt()` is replayed when the interrupt is
answered.** Reproduced: `A B_PRE` → `A B_PRE B_PRE`. Hence the human gate being
three nodes — prepare, interrupt, apply — and never one.

**There is no gap "between supersteps" to die in safely.** With
`durability="sync"` the checkpoint lands *after* the node returns, so dying at
the very end of a node still replays it. What the checkpoint guarantees is that
*earlier* nodes are not replayed. The consequence is a cost, not a bug: a crash
in a node that makes a model call means paying for that call twice.

**LangGraph's runtime context is not checkpointed.** So a live connection pool
reaches a node without being serialised. Verified by putting a
`threading.Lock` in it and checkpointing.

## 4. Layout

```text
src/research_os/runtime/
  config.py clock.py ids.py db.py migrations.py sql/   substrate
  models.py store.py queue.py idempotency.py locks.py  operational state
  budgets.py failures.py policy.py                     spending, failure, authority
  artifacts.py                                         content-addressed bytes
  interfaces.py context.py kernel.py                   capability boundaries
  prompts.py routing.py                                model requests and provenance
  graphs/ cycles.py checkpoints.py                     the bounded cycle
  executors.py leases.py notify.py                     execution and liveness
  actions/                                             the 18 capability handlers
  daemon.py commands.py report.py devdb.py             control plane and CLI
deploy/researchd.service                               shipped, never installed
pilots/run_pilot.sh                                    one command, one real project
```

The scientific kernel imports none of it. `tests/test_runtime_layering.py`
asserts the direction, that `researchctl` imports neither psycopg nor
LangGraph, and that the test fixtures cannot touch the researcher's real
directories.

## 5. The authority boundary, as implemented

```text
28 actions
├── 18 the runtime performs
├──  8 that require human authority — and all eight the person performs
└──  2 with a policy and no handler (named by `runtime doctor`)
```

That all eight gated actions turn out to be human-*executed* was not the plan;
it emerged from asking what each one would mean. Every one requires writing
canonical scientific state, merging to a canonical branch, or publishing — and
the runtime has no method for any of those. So approval unlocks a recorded
decision and the exact command to run, never an execution.
`tests/test_runtime_registry.py` asserts no `A2` action has a handler.

## 6. Verification

```text
uv run ruff check .                     clean
uv run ruff format --check .            clean
uv run --extra runtime pytest -q        3006 passed   (2362 at v1.0.0)
```

The runtime suites and roughly what each pins:

| suite | what it holds down |
|---|---|
| `test_runtime_schema.py` | migrations compose from empty; every check constraint agrees with its Python enum |
| `test_runtime_queue.py` | claiming charges the attempt; an expired lease cannot be renewed; a lost lease cannot overwrite a result |
| `test_runtime_idempotency.py` | a real child process performs a side effect and is killed with `os._exit`; the retry reuses it |
| `test_runtime_locks.py` | a real `SIGKILL`ed holder's lock is released by the server |
| `test_runtime_budgets.py` | ten threads, one unit of capacity, exactly one winner |
| `test_runtime_graph.py` | crash/resume, interrupt/resume across processes, checkpoint size, retention |
| `test_runtime_daemon.py` | one request becomes a finished cycle with no further instruction |
| `test_runtime_chaos.py` | 36 failure injections; the invariants after each |
| `test_runtime_authority.py` | the boundary, asserted by parsing the package |
| `test_runtime_actions.py` | the scientific constraints of each handler |
| `test_runtime_cli.py` | every view renders empty; approvals refuse a non-TTY; untrusted strings cannot repaint a terminal |

Where a crash is claimed, a real process dies. A simulated exception runs the
`finally` blocks whose absence is the failure mode.

**Order-independent**, and that was not free. The migration tests tamper with
`schema_migrations`, which `runtime_db` deliberately does not truncate —
re-migrating per test would cost seconds — so the tamper leaked into every later
test that called `migrate()`. In the default alphabetical order the CLI tests
run before the schema tests, so the suite passed by luck; running the runtime
suites in any other order broke five of them. Those tests now take a throwaway
database, and both the forward and reverse orderings pass:

```text
uv run --extra runtime pytest -q tests/test_runtime_*.py   359 passed
same files, reverse order                                  359 passed
uv run --extra runtime pytest -q                          3006 passed
```

## 7. Pilots

`pilots/run_pilot.sh <project> "<objective>" [cycles] [autonomy]`. Everything
the runtime writes goes into a pilot-local XDG root; the project's `.research/`
tree and Git HEAD are hashed before and after and the script fails if either
moved.

**Run against the real CCAO capsule, twice.**

| | before the frontier fix | after |
|---|---|---|
| cycles | 7 | **2** |
| model calls | 15 | **4** |
| cost | 2.65 USD | **0.84 USD** |
| why it stopped | hit `max_cycles_per_objective` | the frontier was unchanged |
| project repository | byte-for-byte unchanged | byte-for-byte unchanged |

The first run is the finding in `docs/RUNTIME.md` §16: every cycle recomputed an
*identical* frontier and concluded `START_NEXT_CYCLE`. The second is the fix
working on the same real capsule — both cycles recorded the same frontier digest
(`43866175b23b`), and the continuation work item's own result says why it
stopped:

```text
continued=False  the frontier is unchanged from the previous cycle. The runtime
                 cannot alter canonical scientific state, so repeating the cycle
                 would repeat its cost without adding information.
```

Neither run modified the project. The harness hashes `.research/` and every Git
ref before and after and exits non-zero on any difference; both diffs are empty.

**cuPDLP.jl has no capsule.** The frontier is derived from capsule files, so
there is nothing to derive. That is a genuine external prerequisite, not a
defect: `researchctl init-project` and some scientific state, both of which are
the researcher's to author.

## 8. What remains

Honest list, roughly by value.

1. **`propose_capsule_change` and `nominate_insight` have no handler.** These
   are the two actions that would let a cycle's findings reach a person as a
   reviewable proposal rather than as a report. Until they exist, the frontier
   cannot change as a *result* of the runtime's work — which is why the
   unchanged-frontier stop exists, and why it is a mitigation rather than a
   solution.
2. **No execution sandbox.** `docs/RUNTIME.md` §16 states the boundary. The
   detection is real; prevention needs infrastructure this deployment lacks.
3. **Slurm is untested against a scheduler.** No `sbatch` on this machine. The
   state mapping, the failure classification and the reconciliation are unit
   tested with a mock; the submission path has never met a real cluster.
4. **`interpret_results` picks the most recently finished job** rather than
   tracking which have been interpreted. A per-job flag is the obvious next
   step.
5. **Provider tiers are configuration, not measurement.** Everything available
   is assumed tier 3.
6. **One provider family on this machine**, so every critical review runs
   degraded — recorded as such, which is the point, but it is not independent
   review.
