# Research OS closure report — `rc/thesis-pilot`

Structured as the brief's §37 asks: A–N, evidence first. This is not the
specification (`docs/RUNTIME.md`) and not the build record
(`docs/INTEGRATION_BUILD_RECORD.md`). It is the evidence a reader needs to
decide whether to accept this branch.

The previous cycle's verdict was `AUTONOMOUS_RUNTIME_BETA` and its §J listed
five pieces of substantive remaining work. Four are closed here. The fifth —
giving `design_experiment` an executor it can use — was never code.

---

## A. Starting and final SHAs

```text
base          integration/autonomous-runtime-vnext @ b6109c1
branch        rc/thesis-pilot, in worktree /home/nicacevedo/research/research-os-rc
```

Established by inspection before anything was modified:

```text
origin/main                        b3fd03c   (tag v1.0.0)
origin/release/v1.1.0-autonomy     847ddec
origin/r5/autonomous-runtime       76882c3   (= 7e7bfc9 + three PDF commits)
origin/integration/…-vnext         b6109c1   the strongest integrated beta line
merge-base(r5, integration)        7e7bfc9
```

`r5/autonomous-runtime` is *not* an ancestor of the integration line: the three
commits on top of it add PDFs to `docs2/` and nothing else. The integration
branch is the strongest base and this branch is taken from it.

**Running processes.** 107 eight-day-old `cursorsandbox` wrappers in
`futex_wait_queue`, and one orphaned `pgserver` from a pytest run a day earlier.
None was killed. All work was done in a separate worktree.

**Baseline before any change:** `3521 passed, 18 skipped`.

Commits, in order:

```text
d3856b7  Make one project mean one acceptance profile, on every path
7265ec4  Let a researcher say no, and make "no" mean something
4ca4ad2  Give the effect tables their own project, so a pruned run does not take them
ae58bac  Make the delegated cost cap bite before the money is spent
3238ce2  Record the four closures, and the defect the first one exposed
b6eac27  Make the objective's cost cap survive its own successor cycles
b39ed62  Repair what the independent architecture review found
```

## B. Beta-gap status

All four of §J's correctness gaps verified open in code first, then closed.

| gap | status | evidence |
|---|---|---|
| one acceptance profile per project | **closed** | `tests/test_unified_check_profile.py`, 12 tests across direct automation, runtime coding, resumed coding and worktree coding |
| human proposal decline | **closed** | `tests/test_proposal_decline.py`, 16 tests; the terminal check is in the writer, not only the command |
| provenance-safe retention | **closed** | schema 0015 and `tests/test_runtime_retention.py`, 7 tests |
| hard delegated budget | **closed** | `research_os/runtime/spend.py` and `tests/test_runtime_delegated_budget.py`, 19 tests |

**And three defects found while closing them, two of them mine.**

**The runtime's coding action could never succeed.** Driving `run_coding_task`
against a real `AutomationController` — which no test had done — fails
`POLICY_REFUSED` every time, because worktree isolation creates a branch and
the fingerprint hashed all refs into one opaque entry. The branch, the diff and
a passing review were on disk and correct. It survived four adversarial reviews
because the only test of that handler substituted a controller that creates no
worktree. **A double that removes the mechanism under test removes the test.**

**An objective's cost cap did not survive its own successor cycles.** Measured
on a real objective: cycle 0 held the researcher's `--max-cost-usd 6` and cycle
1 held the default 25, so an objective capped at 6 was exposed to 281.

**And then the repair for that one bricked projects.** Deriving the standing
project ceiling from the first objective's cap made
`runtime start --max-cost-usd 0.50` write a 6 USD *lifetime* ceiling, with no
command able to raise it. An independent review found it. Reverted, and
`researchctl runtime budget` now exists.

## C. Schema migrations

Fifteen forward-only, checksum-verified files; `RUNTIME_SCHEMA_VERSION` is
`0015`. Exercised three ways: from empty (`applied 15 migration(s)` on a fresh
cluster, observed), as an upgrade over 0014 with rows already written the old
way, and in both suite orders.

0015 gives `artifact_links`, `tool_invocations` and `model_calls` their own
`project_id`, derived inside each insert from the run so no call site can
disagree with it, with a cascading foreign key so deleting a project still
erases everything. `run_id` keeps its value rather than becoming null, because
`artifact_links_identity_idx` is unique over a `coalesce(run_id, '')` and
nulling it collapses two surviving rows onto one identity — PostgreSQL would
refuse the second delete, and a prune that cannot run is worse than a label
that outlives its row.

One correction carried in 0015's comment because it cannot be made in place:
0013 said the fix needed a primary-key migration because
`artifact_links.run_id` was part of the key. 0002 had dropped that key two
releases earlier.

## D. Tests

```text
before                                      3521 passed, 18 skipped
after                                       3592 passed, 18 skipped
runtime suites, forward order                530 passed  (181.9 s)
runtime suites, reverse order                530 passed  (173.9 s)
migrations from empty                        applied 15 migration(s), fresh cluster
migrations over a populated 0014             tests/test_runtime_retention.py
ruff check / ruff format --check              clean
```

Commands, verbatim:

```bash
cd /home/nicacevedo/research/research-os-rc
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv run --frozen pytest -q tests/test_runtime_*.py
uv run --frozen pytest -q $(ls -r tests/test_runtime_*.py)
RESEARCH_OS_RUNTIME_DSN=... uv run --frozen researchctl runtime migrate
```

+71 tests, none lost, none weakened. Three existing tests were *changed*: two
to the stronger assertion the fix makes available, and one —
`test_the_project_ceiling_follows_the_objectives_cap` — **inverted**, because
it encoded the bricking regression and a passing test for a bug is the thing
most likely to re-introduce it.

## E. Crash and idempotency

Unchanged from the previous cycle and re-exercised: real process death via
`tests/runtime_scripts/`, the interpretation re-deriving identical artifact
bytes, the proposal reservation adopting a crashed attempt's directory.

Added: the coding pipeline's reserved-run-id namespace is recomputable from the
reservation alone, so the attempt that adopts a crashed predecessor's branch
exempts the same namespace — asserted in
`test_the_runs_own_branch_is_not_an_escape`.

## F. Budget behaviour

`reserve → execute → reconcile` now covers the delegated calls. A wrapped
provider reserves one `MODEL_CALLS` and a per-call `MODEL_COST_USD` ceiling
before the provider is asked; a refused reservation raises
`BudgetExceededError` **before** the call, which is the difference between a
budget and a report.

Measured: under a cap that authorises two calls, the provider is asked exactly
twice. Six concurrent workers against one remaining unit produce exactly one
spend.

**Residuals, stated.** No provider quotes a price before it bills, so one call
can exceed its ceiling; the ceiling then ratchets to the largest observed cost,
bounding the excess by one call rather than repeating it. And
`--max-cost-usd X` bounds a *cycle*: an objective may run up to
`max_cycles_per_objective` of them, so the exposure is `X × 12`, the flag's
help says so, and making it bound the whole objective is recorded as remaining
work.

## G. Retention behaviour

`tests/test_runtime_retention.py` prunes a run and asserts the four properties
§4.3 names: the preregistration stays findable **through the production
lookup** (the defect was in that lookup's `join`, so a test with its own query
would have passed throughout), the artifact link survives with its project and
its run label, the idempotency ledger survives and still refuses to replay, and
the cost row survives with its project.

Plus the two ways the fix could have been wrong: two runs sharing an artifact
link can both be pruned, and deleting a project still erases all three tables.

## H. Human authority

Unchanged and extended. The runtime cannot promote, cannot author a Review,
cannot accept a Claim, and now cannot decline a proposal — and that last one is
**enforced in the writer** rather than linted at the call site, because an
adversarial review showed the AST guard is defeated by
`getattr(store, "record_" + "decline")` while the runtime already holds a live
`ProposalStore`.

**Demonstrated on real research.** An autonomous cycle over the
sparse-regression project produced a twelve-item proposal and concluded
`WAITING_FOR_SCIENTIFIC_DECISION`. Nothing in this session promoted or declined
it. §N has the commands.

**And the staleness guard was exercised for real, not constructed.** The
proposal was written at `9d5474e`, and the project has since taken nine more
commits — new modules, new tests, a rewritten README, a closure report. Checked
afterwards:

```text
stale      False
reason     the basis is unchanged
unchecked  the runtime findings it cites were not re-checked
```

Which is exactly right, and is the distinction the previous cycle built it for:
the basis snapshots *the objects the proposal cites*, not the repository HEAD,
so nine unrelated commits do not invalidate it — and the one thing it cannot
verify without the operational database is reported rather than assumed.

**One open hole, recorded.** A decline can be forged by appending a line to
`declines.jsonl`, which lives outside every repository and no fingerprint
covers. Closed under `SandboxMode.REQUIRED`; open otherwise.

## I. Containment

**Unchanged, and still a release blocker.** `kernel.apparmor_restrict_unprivileged_userns = 1`,
`/usr/bin/bwrap` not setuid, `podman`/`docker`/`apptainer` absent,
`systemd-run --user`'s sandboxing directives silently ineffective. High-autonomy
execution of model-written code is **refused** on this host, which is correct
and is also a statement that the release's headline capability is unexercised
here.

The escape-detection path was strengthened: per-ref fingerprinting, `HEAD`'s
symbolic target, a branch-drift cross-check against what the controller
recorded, and an exact rather than substring capsule exclusion. All of that is
detection, not containment, and the distinction is the same one the previous
report made.

## J. Slurm

**Not validated. `sbatch`, `squeue`, `sacct`, `scancel`, `srun`, `sinfo` are
all absent.** The eight `slurm_live` tests remain written and unexecuted. No
change from the previous cycle and nothing here pretends otherwise.

## K. Review independence

**One provider family.** `claude` authenticated; `codex` and `gemini` not
installed. `runtime doctor` reports `DEGRADED_SAME_PROVIDER_FAMILY` and the
autonomous run's own report says so verbatim.

The two adversarial reviews in this cycle were fresh, read-only contexts on the
**same provider family**. That is engineering diversity and it is **not**
cross-provider independence, and it is not counted as such. It was nonetheless
the highest-value thing in the cycle: between them they found the bricking
regression, the ref-move escape, the `HEAD` gap, the subset de-escalation, a
duality gap that went to −3.35e6, and a corollary that was wrong twice.

## L. Operational autonomy

Demonstrated on a real project, end to end and unprompted: `runtime start` →
cycle 0 (`DONE_FOR_NOW`, one finding) → **automatic successor** with recorded
lineage → cycle 1 → grounded twelve-item proposal →
`WAITING_FOR_SCIENTIFIC_DECISION`. No manual orchestration command between
them.

**One gap found by doing it.** On a second objective the planner chose
`run_local_experiment`, the action correctly refused —
`no preregistered design to run; design_experiment must come first` — and the
successor cycle, a new thread seeded only with identity, planned the identical
action again. The guard is right; the feedback path is missing. This is §16's
"seven cycles, one cycle's worth of information" in a new form.

## M. Final architecture verdict

```text
AUTONOMOUS_RUNTIME_BETA
```

Unchanged, and the reasons are unchanged. Three production-hardening axes
cannot be validated in this environment at all — containment, Slurm, a second
provider family — and one of them is a stated precondition of the autonomy
level the release is for. Nothing this cycle did touches any of them.

What *did* change is that four correctness gaps closed, three defects were
found (one of which meant the runtime's only code-writing capability could
never succeed), and the loop was demonstrated on **a second, materially
different real research programme** — a 2023 master's thesis with its own
history, its own data and nobody's fixture.

`AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE` additionally requires, and this host
cannot supply: containment, a scheduler, a second provider family, and one
human promotion actually performed.

`FULL_AUTONOMOUS_RESEARCH_OS_READY` additionally requires the sparse-regression
project to reach a defensible terminal state without routine human
choreography. It has not: `EXP-0001` is 7 of 30 cells and the human gate is
open.

**One thing this cycle is entitled to claim that the last one was not.** The
brief's §35 asks whether Research OS can "falsify, pivot or stop rather than
merely continue". On this project it did all three. The literature audit closed
the primary novelty gate on a 2000 paper; the benchmark's preregistered rule
**rejected** `HYP-0006`, a hypothesis this session wrote; and the scientific
report's recommendation is TRACK F, no paper. None of that was steered.

## N. Remaining genuine blockers

1. **No containment.** Needs a host that permits unprivileged user namespaces,
   or rootless Podman. Unchanged.
2. **No Slurm.** Unchanged.
3. **No second provider family.** Unchanged, and this cycle makes the cost of
   it concrete: two same-family reviews found nine real defects, and the one
   thing they could not do is be independent of the system that wrote the code.
4. **No human has used the gate.** A real proposal is waiting. Everything
   downstream of a promotion — the capsule change, the observation, the
   successor — is demonstrated only from a labelled stand-in.
5. **A decline is forgeable** outside a sandbox. §H.
6. **The refusal feedback path.** §L.
