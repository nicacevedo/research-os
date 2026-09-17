# Research OS architecture report — final hardening pass

Continues `docs/RC_THESIS_PILOT_REPORT.md`, whose verdict was
`AUTONOMOUS_RUNTIME_BETA` and whose §N listed the release blockers. This report
covers the work done against those blockers, states what is proven and how, and
says plainly what is not done.

Evidence first. Where a claim rests on a command, the command is here.

---

## A. Starting and final state

```text
Research OS   /home/nicacevedo/research/research-os-rc   rc/thesis-pilot
              e44d420  at start, clean, == origin
              01a5a05  at the time of writing, pushed

science       /home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection
              research/2026-reassessment @ 783d4d2, clean, == origin
              unmodified by this pass
```

Both repositories were fetched and confirmed `0 0` against their remotes before
anything was touched. No merge to `main`, no force push, no history rewrite.

---

## B. Release-blocker 1 — test-state isolation

### B.1 What was actually wrong

The previous report found that a single test was writing into the real proposal
store and fixed that test. The invariant was still false, and the reason was
the isolation fixture itself.

`tests/conftest.py::isolate_xdg_env` was autouse and it **unset** the four
`RESEARCH_OS_*_HOME` overrides, so that one test could not leak an override
into the next. But `paths._override_or_default` falls back to `Path.home()`
when an override is absent. "Isolated" was therefore implemented as "resolve to
the researcher's real directories", and every test that did not opt into
`automation_home`, `runtime_xdg` or `data_home` wrote real records to
`~/.local/state/research-os`.

`tests/runtime_helpers.py::runtime_xdg` documented this in its own docstring —
"`isolate_xdg_env` only *unsets* the overrides, which makes an unredirected
test fall back to the researcher's real directories rather than fail" — and
worked around it for the runtime tests only. 43 test modules used none of the
three opt-in fixtures.

### B.2 The fix

Isolation is now the default and requires no opt-in.
`conftest.isolate_research_os_state` is autouse and unconditional, and
redirects all four roots into a pytest-owned directory.

It patches through its own `pytest.MonkeyPatch` rather than the `monkeypatch`
fixture, because that fixture is one shared object: a test calling
`monkeypatch.undo()` would otherwise revert the redirect along with its own
patches and silently de-isolate itself. `tests/test_crash_recovery.py` had
already had to work around that hazard, and a first version of this fix hit it.

### B.3 Proof — behaviour, not environment variables

Two guards, neither of which inspects an environment variable.

**Per test.** A `pytest_runtest_call` wrapper calls all 25 path-deriving
functions across 9 families and fails if any resolved outside pytest's
`basetemp`. It runs in that hook rather than in a fixture finalizer because
pytest's `monkeypatch` tears down *before* an autouse fixture that does not
depend on it, so a finalizer would see the baseline environment rather than
whatever the test left behind — which is the state that matters.

**Per session.** `pytest_configure` / `pytest_sessionfinish` inventory the four
real directories before and after the run and fail the run if anything
appeared. This also catches a write that never goes through `research_os.paths`.

`tests/test_state_isolation.py` proves the guards are not vacuous:

- one test reproduces the old default — unset the overrides, assert
  `paths.state_home()` *is* the real directory — and asserts the guard rejects
  it;
- one test writes records through the real `AssessmentStore` and
  `ResearchStore` and then checks the real directories directly;
- one test runs pytest in a subprocess on a test that de-isolates itself and
  asserts the run comes back red with the guard's message. This is what would
  catch the hook being renamed, unregistered, or turned back into a fixture.
- one test greps the product for the four root functions and requires each
  importing module to appear in the guard's inventory, so a new store cannot be
  added without being guarded.

### B.4 Measured result

```text
uv run pytest -q          3637 passed, 18 skipped
reverse module order      558 passed   (ls tests/test_runtime_*.py | sort -r)
uv run ruff check .       All checks passed!
uv run ruff format --check .   304 files already formatted
```

Real user state before and after each full run, inventoried recursively
(worktrees to run level only):

```text
ADDED:   0
REMOVED: 0
```

Four full-suite runs were taken this way. All four added nothing.

One earlier full run showed 104 failures; the cause was invoking
`.venv/bin/python -m pytest` rather than `uv run pytest`, which leaves
`.venv/bin` off `PATH`, so automation subprocesses reported `pytest is not on
PATH`. Not a code defect, and that run also added zero real-state entries.

---

## C. Release-blocker 2 — contaminated user state

774 records removed, 5219 files. Backup, manifests and scripts:

```text
~/research/research-os-state-backups/2026-09-17-pytest-contamination/
```

| family | deleted | retained |
|---|---|---|
| assessments | 355 | 6 |
| research | 419 | 43 |
| proposals | 0 | 6 |
| experiments | 0 | 4 |

A record was deleted only when **two independent signals agreed**: its
canonical record file named a `project_path` under
`/tmp/pytest-of-<user>/pytest-<n>`, **and** a scan of every text file in it
found at least one pytest path and no path under `~/Documents/Github` or
`~/research`. The two signals were computed separately and agreed exactly
(355 and 419 both times). Age, count and naming patterns were not used.

Deliberately kept: 66 `runs/` and 65 `worktrees/` records whose `project_path`
is a Claude Code scratchpad (`/tmp/claude-.../scratchpad/smoke-*`). Those are
agent smoke runs, not pytest output, and fall outside the authorisation. Also
kept: 7 records where the two signals disagreed.

Verified after the fact:

- 5657 files extracted from the backup and hashed byte-identical **before** any
  deletion;
- all 438 retained files byte-identical to their pre-cleanup hashes;
- 0 files anywhere in `assessments/` or `research/` still name a pytest path;
- 6/6 assessments parse through `AssessmentStore`, 43/43 research runs through
  `ResearchStore`, and `researchctl propose list` shows all 6 real proposals
  including the human gate `PROP-19700101T000000Z-b9c26fcd`.

No user-state file is committed to Git.

---

## D. Release-blocker 3 — the science-context lag

The brief described this as completed work existing in project artifacts while
the capsule and planner behave as though it does not. That is accurate. The
live runtime database says the cause was three separate defects, and the first
one is not the one the symptom suggests.

### D.1 What the pilot's database actually contains

```text
findings for cg-sparse-regression: 1
  FIND-20260917T095202Z-f63a6014  inspection  cycle 0  inspect_repository
      "HEAD 9d5474e89f8a on research/2026-reassessment"

artifacts: 21, every one of them role=prompt or role=model_output
```

Not one scientific artifact. And the action ledger says why:

```text
cycle.inspect_repository        ok=True   HEAD 9d5474e89f8a on …
cycle.propose_capsule_change    ok=True   PROP-19700101T000000Z-b9c26fcd
cycle.run_local_experiment      ok=False  no preregistered design to run
cycle.run_local_experiment      ok=False  no preregistered design to run
cycle.design_experiment         ok=False  parameter 'out' must be relative, not '/home/…'
cycle.design_experiment         ok=False  parameter 'out' must be relative, not '/home/…'
cycle.propose_capsule_change    ok=True   an equivalent proposal is already waiting
```

**The runtime never completed the pricing adjudication.** It tried four times
and failed every time. The adjudication in the repository
(`docs/2026/UNIT_BALL_VERDICT.md`, `scripts/adjudicate_pricing.py`) was done by
an outer agent in an earlier session, not by Research OS. The deduplication on
the last line is the runtime working correctly.

### D.2 Defect one — the planner was handed a count

`plan_one_action` passed `findings_available: str(len(findings))` and nothing
else. The reasoning is in the code and is not silly: a planner chooses an
action rather than reasoning about evidence. The cost is that finished work
disappears, because the frontier is derived from capsule files and the capsule
cannot move without a person — so a planner holding only the frontier sees a
project where last cycle's work never happened.

New `research_os.runtime.sciencecontext` assembles three explicitly separate
categories and `planner@4` explains the difference:

```text
FRONTIER               canonical, from capsule files, moves only when a person promotes
COMPLETED FINDINGS     noncanonical, fenced as untrusted autonomous output
OUTSTANDING PROPOSALS  decisions already in front of a person
```

Bounded at 12 findings and 6 proposals, each finding labelled
`"noncanonical": true` in its own entry, with the true totals in a
controller-authored census line outside every fence. The instruction states
that a finding does **not** resolve the frontier — "a hypothesis still listed
as untested is still untested even if a finding below discusses it" — and that
a finding already cited by an outstanding proposal has been asked about.

Proposal visibility uses `store.created_proposals`, which reports `CREATED`
reservations only and deliberately says nothing about promotion: that is
canonical state and the capsule is already its one answer.

### D.3 Defect two — `inspect_repository` reported a commit hash

For a repository holding a finished literature audit, a scientific report and a
verdict document under `docs/`, the action whose job is to say what the
repository contains produced the finding quoted in D.1 and nothing more.

It now inventories the project's documents under a conventional root, registers
each in the content-addressed artifact store, and reports which no capsule
object references. Paths and digests, never contents. Bounded at
`MAX_REFS_PER_KIND`, so nothing is copied into the store and then dropped from
the provenance of the only record that cites it.

Run against the real thesis repository:

```text
documents found: 8, registered: 8
  docs/2026/LITERATURE.md           11692  referenced=False
  docs/2026/PENDING_DECISION.md      4331  referenced=False
  docs/2026/PROVENANCE.md           11818  referenced=False
  docs/2026/REPRODUCTION.md          7499  referenced=False
  docs/2026/RESEARCH_OS.md            773  referenced=False
  docs/2026/SCIENTIFIC_REPORT.md    39727  referenced=True
  docs/2026/TIMELINE.md             21962  referenced=False
  docs/2026/UNIT_BALL_VERDICT.md     9719  referenced=False
```

An unreferenced document is emphatically not canonical. A test writes a
document saying `HYP-0001` is settled and asserts the frontier still lists
`HYP-0001` as untested.

### D.4 Defect three — the experimentalist was graded on rules it was never shown

The command catalogue listed parameter *names*. The model supplied an absolute
path for `adjudicate-pricing`'s `out`, `spec._assert_relative` refused it,
`design_experiment` failed, and the next cycle supplied another absolute path
and failed identically — because nothing in the prompt had changed. A
constraint a model is graded on and never shown is a loop.

The catalogue now carries each parameter's type, requiredness, bounds, choices
and description, and `experimentalist@2` states the relative-in-tree-path rule
outright, naming it as the mistake that has actually been made here.

### D.5 The lifecycle tests

`tests/test_runtime_science_context.py`, 22 tests, covering the six properties
the brief asked for:

1. a completed action becomes a finding with its provenance;
2. the next planner sees its identity, its content and the action that made it;
3. it arrives typed, fenced and labelled noncanonical — three times over;
4. no cycle writes canonical scientific state (checked against capsule bytes,
   not against a promise), and no finding has an acceptance field or kind;
5. a human promotion moves the frontier and the finding does not;
6. the same observation twice is one finding, content-addressed, so a replayed
   cycle does not double its evidence.

---

## E. Release-blocker 4 — operational autonomy

### E.1 The defect, observed live

```text
capsule_observations for cg-sparse-regression
  observed_at    2026-09-17 06:51:24-03:00
  changes_seen   0
  capsule  stored 6e7f556470ad…ab95   now 1a62908e5bfc…ae35   CHANGED
  frontier stored 55fc0175e703…89e3   now bcaff0a49127…4ae0   CHANGED
```

The capsule changed — `HYP-0001` active→supported, `HYP-0005` active→rejected,
`EXP-0001` specified→completed, `EVI-0002` added — and the runtime never
noticed, because the daemon was not running and nothing was going to start it.
The researcher had to know whether `researchd` was up. That is the defect.

### E.2 One control plane per database

A PostgreSQL advisory lock (`LockClass.DAEMON`) held for the daemon's whole
life. Not a correctness guard — work claiming is `for update skip locked`,
leases expire and are recovered, and event ingestion is deduplicated, so two
daemons would compete rather than corrupt. It is what makes *starting*
idempotent, which is what a service manager needs: a second `researchd` finds
the lock held, says so, and exits zero.

The lock lives on the connection, so it is released however the process dies.
The database is closed outside the lock block, so a clean shutdown does not log
a failed unlock — a bug in the first version of this change.

### E.3 The service units

`deploy/researchd-db.service` is new: it brings the disposable local PostgreSQL
back after a reboot, ordered before the control plane. Without it an enabled
`researchd` comes up and retries forever against a cluster nobody started,
which is another way of requiring the researcher to remember a special command.

Three fixes to `deploy/researchd.service`, one a real bug:

- **`ProtectHome=read-only` was wrong.** It reads like hardening. `git worktree
  add` writes into the *source* repository's `.git/worktrees/`, under the
  researcher's project directory, so every automation write task on a
  service-run daemon would have failed on a read-only filesystem — and the
  failure would have looked like a Git problem. Home is now writable and the
  comment says what a real boundary would cost.
- **No start limit.** `Restart=on-failure` retried a permanently broken
  configuration every ten seconds forever, leaving `systemctl --user status`
  reporting "activating" rather than the truth. Six failures in ten minutes now
  reaches an explicit `failed` state.
- **`uv run` without `--frozen`**, so a service start could resolve a different
  dependency set than the one the tests ran against.

Both units pass `systemd-analyze verify`. Nothing installs them:
`test_packaging` walks the AST for a bare `"systemctl"` argv element or a
`"systemd/user"` path literal, so telling a researcher to run a command still
passes and running it for them does not.

`loginctl show-user` reports `Linger=no` on this machine, so even with the
units installed a reboot leaves the control plane stopped until the next login.
That is the researcher's decision to change and is documented in both units.

---

## F. Human authority

Unchanged, and re-verified rather than assumed:

```text
$ researchctl propose promote PROP-19700101T000000Z-b9c26fcd --item PR-006
researchctl propose promote requires an interactive terminal. Promotion writes
a file into your project's scientific record, and a non-human process must not
do that. Nothing has been written.
```

Nothing in this pass promoted, declined, reviewed or accepted anything.

### F.1 The proposal is stale, and the staleness check says so correctly

```text
fresh:           False
changed_objects: EXP-0001 (specified -> completed)
                 HYP-0001 (active -> supported)
                 HYP-0005 (active -> rejected)
```

The proposal was written at `9d5474e`, 16 commits back. Its own summary says
"EXP-0001 is specified, not run" and "no literature record was supplied"; both
are now false. Its `PR-005` asks for the pricing adjudication that has since
been done. Its `PR-006` — the round-by-round working-set equivalence trace —
has **not** been done, and both the project's own `docs/2026/PENDING_DECISION.md`
and the researcher's direction for this pass identify it as the single most
valuable outstanding experiment.

`PR-001` and `PR-002` are marked not promotable by the proposal itself.

---

## G. What is not done

Stated plainly rather than deferred.

**Blocked on the human gate.** The scientific programme — the PR-006
working-set equivalence trace, the solver component map, the hybrids, the
Elastic-Net derivation and equivalence test, the nuclear/atomic-norm audit, the
general theorem, and the paper/no-paper track selection — has not been started.
PR-006 is a preregistered experiment, and a preregistered experiment must exist
as a capsule object before the runtime will run it. That gate is the design
working, not an obstacle to route around.

**Not demonstrated.** Automatic post-promotion continuation. The mechanism is
tested in `tests/test_runtime_daemon.py`, and there is now a real unobserved
capsule change to exercise it against, but it has not been run on a real human
decision because no human decision has been made.

**Untouched by this pass.** Containment (this kernel refuses unprivileged user
namespaces; `bwrap` fails on loopback, `systemd-run`'s user-scope sandboxing is
silently ineffective, and neither podman nor docker is installed — so
containment remains BLOCKED, not PASS). Slurm (`slurm.enabled: false`).
Cross-provider review independence (one provider family installed).

**Not run.** The final adversarial science review and the final adversarial
architecture review. Both were to attack work that does not exist yet.

---

## H. Verdict

```text
AUTONOMOUS_RUNTIME_BETA
```

Unchanged, deliberately. The brief's bar for
`AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE` is five things, and three of them are
met: test isolation is proven, the science-context lifecycle is fixed, and the
local blockers in §B–§E are closed. The other two are not — human decision to
automatic continuation has not been demonstrated on a real decision, and no
full research chain has run without manual choreography. Calling this a release
candidate would be claiming the two that are missing.

The honest summary of this pass: the architecture defects that were making
autonomous science *impossible* are fixed, and the gate that makes it
*legitimate* is still closed, which is where it should be.
