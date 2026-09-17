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

They are not inert, though: their command lines contain `uv run pytest -q`, so
`pgrep -f "pytest -q"` matches them forever. Any "is the suite still running?"
check built on that pattern reports `RUNNING` permanently, which cost one wrong
diagnosis in this session. Match on the interpreter path
(`research-os-rc/.venv/bin/python -m pytest`) instead.

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
7da6114  Write the branch's closure report
a78728a  Score the acceptance ledger, including the three rows that are not tested
98d9023  Record the order-independence and migration evidence
95d6d29  Record the staleness guard passing on a nine-commit gap
136d1fc  Tell the planner what the last cycle was refused for
406f2ce  Give the refusal-feedback finding its own section
1741c6f  Cover the project ceiling command with CLI tests
```

**Final state:** `3596 passed, 18 skipped`, `ruff check` and
`ruff format --check` clean over 302 files. The linked scientific closure
report is
`$THESIS_REPO_DIR/docs/2026/SCIENTIFIC_REPORT.md`, on branch
`research/2026-reassessment`.

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
after                                       3596 passed, 18 skipped   (539.8 s)
runtime suites, forward order                 534 passed  (231.3 s)
runtime suites, reverse order                 534 passed  (209.0 s)
migrations, dev cluster                      15 applied, latest 0015
migrations from empty                        applied 15 migration(s), fresh cluster
migrations over a populated 0014             tests/test_runtime_retention.py
ruff check / ruff format --check              clean
```

Commands, verbatim:

Note that `-p no:randomly` does nothing here: `pytest-randomly` is not
installed, so the default run is collection order and the forward/reverse pair
is the only order-independence evidence there is. Worth checking rather than
assuming, because a suite that only ever runs in one order has not been shown
to be order-independent by running it again.

```bash
cd /home/nicacevedo/research/research-os-rc
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv run --frozen pytest -q tests/test_runtime_*.py
uv run --frozen pytest -q $(ls -r tests/test_runtime_*.py)
RESEARCH_OS_RUNTIME_DSN=... uv run --frozen researchctl runtime migrate
```

**Run them through `uv run`, not through `.venv/bin/python -m pytest`.** The
difference is not cosmetic and it cost a diagnosis in this session. The
automation tests drive real coder tasks whose acceptance commands are declared
as bare executables -- `pytest -q` -- and executed as subprocesses. `uv run`
puts `.venv/bin` on `PATH`; `.venv/bin/python -m pytest` does not, so the
subprocess resolves nothing and the controller reports
`required acceptance commands failed: pytest -q` with
`error='pytest is not on PATH'`. That surfaces as **104 failures across the
`test_auto_*` suites** that look exactly like a regression in the check
pipeline and are not one. Confirmed by re-running the same file both ways:
`.venv/bin/python -m pytest tests/test_auto_repair.py` fails 17 of 33,
`PATH="$PWD/.venv/bin:$PATH"` passes 33.

+71 tests, none lost, none weakened. Three existing tests were *changed*: two
to the stronger assertion the fix makes available, and one —
`test_the_project_ceiling_follows_the_objectives_cap` — **inverted**, because
it encoded the bricking regression and a passing test for a bug is the thing
most likely to re-introduce it.

### D.1 A test was writing into the researcher's real proposal store

Found while reading the human gate this pilot was supposed to leave for the
researcher, which is the only reason it was found at all.

```text
proposals in the real store            67
written by one test                    61
the six real ones, buried              includes PROP-19700101T000000Z-b9c26fcd
```

`tests/conftest.py::isolate_xdg_env` is autouse and **deletes** the four
`RESEARCH_OS_*_HOME` overrides. Deleting them does not isolate a test; it
selects the default, and the default is the researcher's machine.
`proposals_root()` is `state_home() / "proposals"`, so
`test_a_capsule_project_still_uses_the_scientific_pipeline` -- which takes only
`tmp_path` and reaches the proposal store -- appended a proposal to
`~/.local/state/research-os/proposals` on every run it had ever had. Sixty-one
of them, burying the real gate in `researchctl propose list`.

Fixed narrowly: that test now relocates `RESEARCH_OS_STATE_HOME` into its own
`tmp_path`, with the reason in its docstring.

**Not fixed generally, deliberately.** The obvious general fix is to make the
autouse fixture *set* the four variables into `tmp_path` rather than delete
them. Two things argue against doing it in this session and both are
verifiable rather than cautious:

- `tests/test_paths.py` exists to test the default derivation and carries its
  own autouse fixture that deletes the same variables. A global fixture
  setting them would be fighting a local fixture clearing them, decided by
  fixture ordering.
- The near-equivalent shortcut -- pinning `HOME` globally so every fallback
  lands under `tmp_path` -- breaks more than it fixes. The automation suites
  run real `git` and `uv` subprocesses, and both read `HOME` for identity and
  for the package cache.

The durable fix is a `state_home` fixture required by every test that can
reach the proposal store, plus a check that the real store is untouched after
a suite run. That is test-infrastructure work with a full-suite blast radius,
and it is listed in §N rather than done here.

**The 61 rows are left in place.** They are the researcher's data directory,
they are unambiguously identifiable by their `/tmp/pytest-of-*` capsule paths,
and deleting them is not this session's call to make.

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

**One gap found by doing it, and closed.** On a second objective the planner
chose `run_local_experiment`, the action correctly refused —
`no preregistered design to run; design_experiment must come first` — and the
successor cycle, a new thread seeded only with identity, planned the identical
action again. The guard was right; the feedback path was missing. This was
§16's "seven cycles, one cycle's worth of information" in a new form.

Fixed by giving the planner what the last cycle was refused for:
`graphs/cycle.py::_previous_attempt` reads the parent run's last refused
`tool_invocation` and passes `"<action> was REFUSED: <detail>"` as a new
`previous_attempt` field; `prompts.py` carries the instruction paragraph and
the planner prompt goes to version 3.

**Verified on the real project, not a fixture.** `RRUN-20260917T111632Z-227d00d8`
re-ran the same objective under `planner@3`. It did not repeat the refused
action. It concluded `WAIT_HUMAN` /
`WAITING_FOR_SCIENTIFIC_DECISION` — the honest answer, since the
design it needed was the thing a human had to decide — at a cost of
0.262374 USD across two model calls.

One caveat on the strength of that evidence: it is a single run, and
"escalated instead of repeating" is weaker than "planned the action that was
actually missing". The regression protecting it is a unit test, not this run.

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
choreography. It has not, though it is closer than the last revision of this
line: `EXP-0001` is now **30 of 30 cells**, executed end to end by Research OS
in a worktree pinned at the freezing commit, and the human gate is still open.
The gate being open is the system working; the routine choreography around it
is what is not yet absent.

**One thing this cycle is entitled to claim that the last one was not — with
one leg of it withdrawn.** The brief's §35 asks whether Research OS can
"falsify, pivot or stop rather than merely continue".

- *Falsify.* `HYP-0005` is **rejected**, and by the strongest available route:
  not a new measurement but the researcher's own committed 2023 CSVs
  (`EVI-0001`), which show two of its three configurations stopping below
  `max_iter` and all three matching the CG objective to better than 5e-08. The
  capsule refused to record the rejection until qualifying evidence existed,
  which is the invariant doing its job.
- *Stop.* The literature audit closed the primary novelty gate on a 2000 paper,
  and the scientific report's recommendation is TRACK F, no paper.
- *Pivot.* The benchmark's headline changed under adversarial review from a
  claim about accuracy to a claim about speed at a matched objective, and the
  recommendation survived on better evidence.

**Withdrawn:** an earlier revision of this section also claimed the
preregistered rule "**rejected** `HYP-0006`, a hypothesis this session wrote".
It did not. The analysis script had printed `HYP-0006`'s id over a count of
recorded Lagrangian bounds, and `HYP-0006` is about whether the method
*terminates* through the pricing test — a different claim that the run detail
does not record. `HYP-0006` is back to open and the label is fixed. The
uncomfortable part is that this was the example this report reached for first,
and it was the one that did not hold; it was caught by a reviewer attacking the
science, not by the runtime and not by me.

## N. Remaining genuine blockers

0. **Test isolation selects the real machine by default.** Not a release
   blocker and listed first because it is the cheapest of these to fix and the
   only one this host can fix at all. `isolate_xdg_env` deletes the four path
   overrides instead of relocating them, so any test that reaches a Research OS
   directory without asking for a fixture writes to the researcher's own; one
   did, 61 times (§D.1). Wanted: a `state_home` fixture that every such test
   must take, and a post-suite assertion that the real store is unchanged.
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
