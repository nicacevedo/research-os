# Research OS v1 — build record

What was built between `ca177af` (V01_BASE) and `a322751`, what went wrong, and
what is still outstanding. Written to be reviewed rather than to reassure: the
failed attempts are here in as much detail as the successes, because two of them
were fixed wrongly more than once and the reasoning that produced them is the
part worth reading.

A rendered version of this document is published as an artifact; this file is
the copy that travels with the code.

| | |
| --- | --- |
| branch | `architecture/research-os-v1` |
| base | `ca177af` |
| head | `a322751` |
| commits | 11 |
| files changed | 120 |
| insertions | 36,166 (against 138 deletions) |
| tests | 1,961 passing |
| independent reviews | 4 run, 3 returned FAIL, 1 outstanding |
| released | **no** — not merged, not pushed, not tagged |

---

## 1. The short version

The software works and is exercised by 1,961 passing tests. It has also been
wrong, repeatedly, in ways that only showed up under an independent reviewer or
a live model provider — and the single most important defect took three attempts
to fix, each of which I believed was correct when I wrote it.

The R0 scientific kernel is frozen as required. Exactly one kernel file changed,
`validate.py`, and that change is a pure extraction: `claim_approval()` returns
the same answer the acceptance gate already computed inline, so the paper layer
reuses one definition of "accepted" instead of reimplementing it. All 341 R0
tests pass unchanged.

---

## 2. The eleven commits

| SHA | WP | What |
| --- | --- | --- |
| `947e48f` | WP1 | Reproducible literature intelligence |
| `05228f8` | WP2 | Scientific proposal pipeline |
| `486ebc0` | WP3 | Experiment and HPC execution |
| `b6fb29a` | WP4 | Explicit cross-project insights |
| `791f505` | WP5 | Evidence-grounded writing workflow |
| `949a295` | WP6 | Unified research orchestration |
| `feba39d` | WP7 | Routing and operations |
| `bde5817` | WP8 | End-to-end validation against real providers |
| `16cdb82` | REV1 | Repair what the independent review found |
| `4be0b4d` | REV2 | Repair what the second independent review found |
| `a322751` | REV3 | Repair what the third independent review found |

**WP1 — literature.** OpenAlex, Crossref and arXiv clients; scholarly identity
precedence (doi → arxiv → openalex → pmid → strict title+author+year); SQLite
FTS5 with a porter tokenizer and BM25; PDF fetch and text extraction; a
read-only literature analyst behind an untrusted-external-text fence.
36 files, +9,690.

**WP2 — proposals.** Structured proposals grounded in capsule objects and
literature keys, an independent assessor, and human-only promotion into a DRAFT
capsule object. A proposal may never propose a Review. 16 files, +4,542.

**WP3 — experiments.** Declared commands in `experiments.yaml` outside every
worktree, whole-token parameter substitution, local and Slurm executors over
`sbatch`/`squeue`/`sacct`, candidate evidence packets that are never capsule
Evidence. 17 files, +5,113.

**WP4 — insights.** Nomination and human promotion of durable findings, quoted
into other projects' prompts behind a fence that names whose findings they are.
10 files, +2,249.

**WP5 — writing.** Source packets built only from Claims a human actually
accepted, four grounding checks (manifest, citations, identifiers, numbers), a
contrary-evidence check, an independent writing reviewer. 15 files, +4,408.

**WP6 — orchestration.** `researchctl research`: a typed task DAG, a closed
dispatch table with no default branch, separate budgets for model calls / write
tasks / experiments / cluster submissions, and human checkpoints. Delegates to
existing controllers rather than duplicating them. 17 files, +4,303.

**WP7 — operations.** A real `doctor` across every subsystem, `storage
--reclaim`, `research resume` for crashed runs, and enforcement of the
wall-clock budget that had been declared but never applied. 13 files, +1,751.

**WP8 — validation.** A consolidated security regression suite, a synthetic
complete research project, a live-provider smoke run, and a read-only pilot
against a real registered project. 16 files, +2,707 / −142.

### Scale

| Area | Files | Lines added |
| --- | ---: | ---: |
| Source | 82 | 22,664 |
| Tests | 30 | 12,552 |
| Documentation | 8 | 950 |

66 new source modules across 7 packages; 26 new test files. The CLI grew from
10 top-level commands to 17, with 57 subcommands across seven groups.

---

## 3. The run lock: three wrong fixes

One defect consumed three review rounds. Each fix was written with a confident
explanation of why it was correct. Each explanation was wrong in the same way.

The motivating failure was real and silent: a run was cancelled in one terminal
while executing in another, `cancel` reported CANCELLED, the executing process
finished its next task, wrote the record again, and the run ended
READY_FOR_HUMAN. No error, no log line, and the decision was gone.

**Attempt 1 — `O_CREAT|O_EXCL` with a staleness takeover.** If a lock exists,
read its pid; if that process is gone, unlink and create your own. *Wrong
because* two claimants both read the stale file, both conclude the owner is
dead, both unlink — and the second unlink removes the first claimant's **fresh**
lock. Caught by review 1.

**Attempt 2 — hard-link the winner into place.** Create a uniquely-named file
and `os.link` it to the lock path, since `os.link` fails if the destination
exists. *Wrong because* I kept the `unlink` immediately before the link, so the
destination never existed when anyone linked. The docstring argued for a
property the code did not have. Caught by review 2, which reproduced two
simultaneous holders directly against the function.

**Attempt 3 — `fcntl.flock`, file unlinked on release.** The lock moved into the
kernel: no staleness to detect, and `SIGKILL` releases it automatically. Correct
so far — but release still unlinked the file before dropping the lock. *Wrong
because* a flock is held on an **inode**, not a name. Unlinking the name while
holding the lock leaves the holder locking an inode nobody can reach, while the
next arrival creates a fresh inode at the same path and locks that.

```
review 3    100 violations / 5,536 acquisitions  (1.8%)
control       0 violations / 4,863 acquisitions  (unlink removed)
reproduced   57 violations /   690 acquisitions  (8.3%)
```

**Attempt 4 — `fcntl.flock`, file never removed.** The lock file is permanent,
a few hundred bytes per run id. That is the price of the guarantee.

```
after fix     0 violations /   599 acquisitions
```

### The pattern

Every version removed a name another process was about to use. Three different
mechanisms, one mistake. In attempts 2 and 3 the docstring arguing the design
was sound was itself the strongest signal that the property had never been
measured.

### The test was the real failure

The first lock test passed against all three broken versions. It spawned racers
that each made one attempt, with the winner holding for 400 ms — so every loser
was refused long before any release, and no process was ever mid-acquire while
another released. That is the only window that exists.

The replacement loops acquire/release with an `O_EXCL` witness file inside the
critical section, and found the failure in roughly one acquisition in fifty.

---

## 4. Three wrong diagnoses of a degenerate plan

Running a read-only pilot against the real `ccao-covariance-regressivity`
project, the live planner returned a one-task plan whose summary, title, goal and
query were all the word `test` — after 13,381 output tokens of genuine planning.
Everything downstream then ran on it: a literature search for "test" reached
three providers and retrieved 60 works.

| # | Diagnosis | Change | Outcome |
| --- | --- | --- | --- |
| 1 | The prompt's key-by-key shape table read as the task | Reframed as "mechanics, not the task", moved later | still `test` |
| 2 | The table induces form-filling whatever the framing | Replaced with per-kind prose | still `test` |
| 3 | The schema is too expensive (13 required keys) | Required only `id`, `kind`, `title`, `goal` | still `test` |
| 4 | **Replay both archived prompts under identical conditions** | *(an experiment, not a change)* | cause found |

The replay inverted the theory completely. The prompt that originally produced
an excellent four-task plan produced `test` on replay; the prompt that produced
`test` produced an excellent plan. **The prompt was never the variable.** A
provider's structured-output enforcement can converge on the smallest
schema-valid object, silently, at any time.

**What I should have done first:** run #2 — the run that produced the excellent
plan — *already contained* the shape table I blamed in diagnosis 1. A single
`diff` of the two archived prompts would have refuted my first theory before I
acted on it. I had the evidence and theorised over it three times instead.

The fixes that shipped are the right response regardless of cause:
`assert_plan_says_something` refuses a plan whose fields are entirely
placeholder tokens, and a refused plan gets one bounded re-ask carrying the
validator's exact objection. Deliberately narrow — a terse but real goal passes,
and so does "Testing the estimator", since only whole-token matches count.

It then worked against the live provider on the next run:

```
plan_rejected            the plan summary is 'test', which is a placeholder…
plan_correction_started
plan_correction_accepted
plan_accepted
```

---

## 5. Independent review, round by round

### Round 1 — seven findings

| ID | Sev | Finding | Fix |
| --- | --- | --- | --- |
| S1 | high | Experiments ran in the canonical checkout; every caller passed it, so the documented isolation was nobody's behaviour — and the containment scan therefore refused any project with a `.venv` | Experiments create their own worktree; `--worktree` is an override recorded as `isolated=False` |
| S2 | high | A write-enabled worker could forge a controller-attributed section in its own reviewer's prompt — report, diff and worktree file bodies all sat in bare markdown fences | Three fences added to the central tuple; all three channels routed through `render_data_block` |
| S3 | med-high | Stale-lock takeover race | Attempted; not actually closed until round 3 |
| S4 | medium | A killed run lost its delegated task's model-call spend | Reconcile from the event ledger before re-dispatching |
| S5 | medium | The analyst's correction truncated the validator message at 128 chars, cutting off the identifier needing repair | Dedicated 2,000-char limit |
| S6 | medium | Literature reached the write-enabled paper writer unfenced, justified by a citation check that only proves the key is in the local index | Fenced; justification corrected |
| S7 | low-med | `ISOLATED_WRITE` had no tool allowlist, so a configured `Bash` would pass | `WRITE_TOOLS` enforced |

### Round 2 — one unfixed, two partial, six new

| ID | Status | What the reviewer established |
| --- | --- | --- |
| S3 | open | The `os.link` fix kept the unlink; two holders reproduced directly |
| S4 | partial | Reconciliation ran only under `--retry`; the default path still lost the spend |
| S7 | partial | Enforced at invocation but not at config load, so a bad config failed mid-run after a worktree existed |
| N1 | new | **Introduced by the S1 fix.** Isolated experiment worktrees were never released, invisible to `doctor` and `--reclaim` |
| N2 | new · high | The same S2 escape, live in the *paper* reviewer — the gate that stops a writing worker approving its own draft |
| N3 | new · low | Same construct in `paper/writer.py` and `automation/executor.py` (self-injection, not escalation) |
| N4 | new · low | `paper cleanup` derived the lock path by changing a suffix, naming a file that never existed, and leaked the real lock |
| N5 | new · low | Reconciliation could charge the same interrupted attempt twice |
| N6 | **not a defect** | I reported a literal `\n` as a bug. Review 3 showed `render_data_block` re-joins with a newline regardless, so the prompt was byte-identical. **My claim was wrong.** |

### Round 3 — the measurement round

| ID | Status | What the reviewer established |
| --- | --- | --- |
| S3 | open | Release unlinked before unlocking. Measured 1.8% violations with a control run isolating the cause |
| N1 | partial | Gated on `TERMINAL_STATES`, missing `PREPARED` — a run refused by the containment scan, i.e. the likeliest orphan of all |
| N5 | partial | The ledger mixed an inner run's *total* with a reconciliation *delta*, so a second interruption charged the difference twice |
| N2 | class open | A fourth bare fence: `proposal/context.py` quoted CHARTER.md and STATE.md to the proposal assessor, which is a gate |
| — | new | `experiment cleanup` had no state guard: a live cluster job's working directory could be force-removed |
| — | tests | The lock test could not observe the race; `HOSTILE_FIELDS` had rotted to delimiters no fence used; the class test grepped ` ```diff ` so could not see a bare fence |
| — | docs | The README claimed `--reclaim` reaches paper worktrees; drafts are keyed by draft id and the sweep cannot find them |

### What the reviews could not break

Stated explicitly across all three rounds: nothing automated writes under
`.research/` (three independent layers, the last from the observed diff);
nothing authors a Review (`reviewer_kind: "human"` is hardcoded behind an
interactive-terminal gate); nothing marks a Claim accepted; literature never
reaches the coder; no escape from the acceptance-command grammar; and the
`promptdata` serializer itself is sound — a delimiter with an embedded control
character fails because scrubbing runs before neutralisation, and truncation can
only cut, never synthesise. Every S2-class finding was a call site that bypassed
the module, never the module.

---

## 6. What live providers found

Four defects came from running against the live `claude` CLI and a real
registered project rather than fake providers. None would have surfaced in the
test suite.

| Exercise | Result | What it established |
| --- | --- | --- |
| Live smoke: planner → analyst → coder → checks → reviewer | pass | Two-task plan, 11 analyst findings, `pytest` exit 0, `git diff --check` exit 0, reviewer PASS, 0 repairs. Canonical checkout verifiably unchanged |
| Synthetic complete project | pass | Empty repo → research run → human promotion → real experiment → hand-written Evidence and Claim → human approval → grounded results section. Capsule byte-identical |
| Read-only pilot, real project (7 runs) | 4 defects | Schema too demanding; analyst self-inconsistency with no repair path; degenerate plans; silent cancel overwrite. The project stayed at `8733ad75` with a clean tree throughout |
| Experiment on a `.venv` project | pass | The case refused outright before S1. Now `isolated: True`, result in the worktree, no `results/` in the checkout |

The pilot's best run read EVI-0008's real numbers — a curvature ratio of 4.26×
to 40.04× against a ≥1.20 accept threshold, M = 0.0118, 1 of 45 cells over 1% —
and proposed literature, analysis and a checkpoint asking which evidence gap to
fund next. It then hit the account's session limit mid-analysis and failed
closed, recording the exact external reason and 3 of 12 model calls spent.

---

## 7. The long tail

Roughly thirty smaller defects were found and fixed during construction, mostly
by the test suite catching them immediately. A representative selection:

- **uv lock guard.** TOCTOU: `settle()` trusted the symlink status from
  `observe()`, so project code could swap `uv.lock` for an outbound symlink and
  have the restore write through it. Fixed with `O_NOFOLLOW`; a follow-up added
  an `st_nlink > 1` refusal for the hardlink variant.
- **Literature identity.** NFKD left combining marks that split words ("Réponse"
  → "re ponse"); `[a-z0-9]+` reduced CJK and Cyrillic titles to nothing, risking
  false fallback merges; a UNIQUE violation on `works.arxiv_id` during merge;
  `field_provenance.superseded` computed inverted.
- **Literature search.** Known-item evaluation failed on unstemmed AND queries —
  fixed with a porter tokenizer in both FTS tables plus a one-shot OR relaxation
  reported as `any-term`.
- **Display boundary.** `terminal_safe` applied after `json.dumps` produced
  invalid `\x` escapes; machine JSON now uses `ensure_ascii=True`. A fast path
  checked `CONTROL_CHARS` rather than `CONTROL_CHARS - keep`.
- **Experiment spec.** `_assert_relative` raised `ExperimentSpecError` inside
  pydantic validators, so `model_validate` could raise two different exception
  types.
- **Research planner.** `experiment_parameters` was missing from the plan schema
  entirely; a quality floor based on word counts broke 46 legitimate tests and
  was narrowed to placeholder detection; `"..."` escaped that detection because
  stripping punctuation left an empty token.
- **Research controller.** The delegated `Budget` could exceed its own `le=100`
  ceiling; a cluster-submission budget was checked *after* `sbatch` rather than
  before; a write task was charged before the source packet was built, so a
  project with nothing accepted lost an allowance.
- **Test helpers.** Keyword collisions between preset task fields and caller
  overrides; `assumptions or [default]` masking an explicit empty list; a Slurm
  test that emptied `PATH` and thereby removed `git`; a scripted import
  replacement that corrupted `from collections.abc import …`.

---

## 8. Status

### Verified

- 1,961 tests pass; `ruff check`, `ruff format --check` and `git diff --check`
  clean.
- R0 kernel frozen: one file changed, pure extraction, 341 R0 tests unchanged.
- Lock exclusivity measured: 0 violations in 599 contended acquisitions, from
  57/690 before.
- Experiment isolation confirmed through the real CLI on a `.venv` project.
- A full pipeline run leaves `.research/` byte-identical; a write task leaves the
  checkout and its HEAD untouched.

### Not done

- **WP11 release.** Nothing merged into `r0/kernel-v1`, nothing pushed, no
  `research-os-v1` tag. Held pending the fourth review.
- **Fourth review.** Running at the time of writing; no verdict yet.
- **The spec's A–O report sections.** I no longer hold their verbatim titles in
  context — only the record that fifteen were required. This document covers the
  ground, but its headings are a reconstruction, not a claim to match the spec's.

### Known limits

- **Worktree isolation is Git isolation, not an OS sandbox.** Acceptance checks
  run project code — including code a model just wrote — with your permissions.
- **The symlink scan cannot catch a link created and deleted within one
  invocation.** The real bound on a write worker is its tool set: file tools
  only, no command tool, enforced at config load and again at invocation.
- **The pre-dispatch event does not record the inner run id.** It cannot: the
  id is derived inside `AutomationController.start`, after the event is written.
  So a `start` that creates the run directory and then fails leaves an inner
  store that `researchctl research cleanup` -- which walks `automation_run_started`
  -- will not reach. Recording it would mean the research controller deriving the
  id itself, duplicating `make_run_id`'s inputs outside the function that owns
  them, which is how the run-lock went wrong three times. Residual risk: leaked
  disk in a run directory after a dispatch that died mid-start, visible to
  `researchctl storage` and removable by hand.
- **Review independence is degraded.** Only `claude` is installed, so the
  reviewer is a different model of the same family. Every run says so before it
  starts, not after.
- **The run lock is advisory and local.** `flock` is unreliable over NFS;
  runtime state lives under the local state home by design.
- **`_HELD` is process-global, not thread-safe.** Every entry point is a
  single-threaded CLI command today; recorded so a future threaded caller sees it
  beforehand.

---

## 9. Assessment

The architecture held up. Across three adversarial reviews, no finding touched
the scientific authority boundary: nothing automated can write the capsule,
author a Review, or accept a Claim, and the reviewers tried. The prompt-data
serializer survived direct attack. The failures were all in the layer around
it — call sites that bypassed a sound module, a lock written three times, and
cleanup for things just created.

What did not hold up was my own verification. Two of my tests passed against code
later proved broken, and I twice wrote a docstring arguing for a property the
code did not have. The reviews were not catching carelessness so much as
catching confident reasoning that had never been measured — and in the one case
where the measurement was finally run (the prompt replay), it inverted a theory
already acted on three times.

The honest summary is that this codebase is in good shape *because* it was
reviewed by something with no stake in it shipping, four times, and that the
value of each round did not decline: round 3 found a defect at 1.8% incidence
that rounds 1 and 2 had both nominally closed.

---

# Part II — Release audit

Added during the release-closure pass. Part I above is the implementation
history and is unchanged; nothing in it has been rewritten to look cleaner in
hindsight.

## 10. Review 4 and the delta review

Review 4 was interrupted by a provider session limit and produced no report, so
a fresh final release-gate review was run against `af913a4`.

**Verdict: FAIL**, on one blocker and one repair-before-release item. It also
mutation-tested the concurrency work and could not break it.

### What it could not break

- **The lock.** 8 processes, 2,400 acquisitions, an `O_EXCL` witness *and* a
  read-modify-write counter inside the critical section: 0 violations, 0 lost
  updates. Its control — the same harness against a `_release` that unlinks
  before unlocking — produced 1,633 violations (68%) and 767/2400 on the
  counter. Reentrancy traced; the documented thread-unsafety confirmed
  unreachable (no `threading`, `asyncio` or `concurrent.futures` anywhere in
  `src/`).
- **Lock deletion paths.** A complete inventory of every deletion in `src/`:
  nothing can reach `run-*.lock`, nothing removes `locks_root()`, and no code
  iterates the locks directory at all.
- **Cleanup interaction.** `auto cleanup`, `research cleanup` and
  `storage --reclaim` all route through the run-locked cleanup; `experiment
  cleanup` is a separate id namespace guarded on `ACTIVE_STATES`.
- **The new lock tests, under mutation.** Injected via `sitecustomize` so spawned
  children were mutated too: `LOCK_SH` failed 7/14, the stale-takeover bug 7/14,
  unlink-on-release 3/14.
- **Scientific integrity**, traced rather than read from docstrings: two writers
  under `.research/`, both TTY-gated; `reviewer_kind: "human"` in exactly one
  place; nothing writes `ACCEPTED`; `scope_violations` refuses `.research/`
  before consulting any allow-list, and the paper layer reuses that same
  function.

### BLOCKER — a fourth unfenced channel, and then a fifth

`paper/reviewer.py` interpolated the paper writer's manifest id lists with no
sanitiser and **no delimiter at all** — so there was nothing to escape from; the
writer simply emitted newlines and forged this prompt's own `DETERMINISTIC
CHECKS THE CONTROLLER RAN` section, in the gate that stops a writing worker
approving its own draft.

Fixed in two layers: `SourceManifest` now refuses an id that is not id-shaped
(the root cause — `written_paths` has had a validator since it was written), and
the prompt renders what survives through `render_data_block`.

The class-wide test meant to catch this could not: it searched for markdown
fences, and this interpolation had none. Replacing it with a property test —
*the controller's own section headings appear at most once as standalone lines
outside every data block* — found a **fifth instance immediately**: `WHAT THE
WRITER WAS ASKED TO DO` renders the instruction multi-line and unfenced, and
that instruction is a research task's goal, written by the research planner.

An audit found the same shape at **fifteen sites across nine files**: every
goal, completion condition and instruction reaching an analyst, coder, writer,
planner or either reviewer. `prompt_safe_block` makes delimiters inert and
deliberately keeps newlines — which is exactly what lets an unfenced one open a
line and forge a heading. `TASK_FENCE` was added and applied to all fifteen.

That is the lesson of four rounds in one sentence: each previous round fixed the
instance it was shown, and the class survived to be found again.

### REPAIR — `charged_total`, wrong for the second time

The third review found the ledger mixing a total with a delta. The fix moved
both to "a total" — but the finished event's total was one *inner run's*, not
the task's, so a task with two delegated runs still compared incomparable
numbers and over-charged. Both now go through `_delegated_total`, the single
definition. The previous regression test could not see it: with one delegated
run the two quantities coincide.

### Smaller items from the same review

- A same-second run-id collision made a prompt `resume --retry` fail. Found
  again by this pass's own new test, so fixed with an `attempt` discriminator
  rather than deferred.
- The writing reviewer's access position was never asserted, where every other
  controller asserts it. `_reviewer_setting` now refuses anything but
  `CONTEXT_ONLY`.
- The automation planner's lone raw `{goal}`, fenced with the rest.
- A false claim in the stress test's own docstring: it did **not** catch the
  unlink bug in eight trials — the deterministic waiter test catches it every
  time, and the 8% figure came from the release probe. Corrected, and its
  acquisition floor raised from 50 to 120, clear of what it actually reaches.

## 11. Concurrency release gate

| Property | Result |
| --- | --- |
| Primitive | `fcntl.flock(LOCK_EX\|LOCK_NB)` on a stable path |
| Lock file unlinked on release? | **No** — never, by design; that is the correctness argument |
| Deletion paths reaching it | none (full inventory, twice, by two parties) |
| Mutual exclusion | deterministic test + stress |
| Waiter across release | deterministic; the detector for the bug that escaped three rounds |
| Path/inode identity | asserted directly across acquire/release/re-acquire |
| Process death | `SIGKILL` a holder; kernel releases; next caller acquires |
| Cleanup interaction | `auto cleanup` refused mid-run via the same lock |
| Release probe | 24,000 acquisitions over 5 repetitions, ~800k contended attempts, **0 violations** |
| Detector proof | shipped → `REFUSED`, shared inode; broken → `ACQUIRED`, different inode |

## 12. Validation results

| Exercise | Result |
| --- | --- |
| Full suite | **2,202 passed** in 730s |
| Ruff check / format / `git diff --check` | clean |
| R0 scientific regression | **411 passed**; digests and models byte-unchanged |
| Synthetic end-to-end, real CLI | **22/22** |
| Real-provider bounded smoke | **PASS** — the one bounded repair fired and re-reviewed |
| Real-project read-only pilot | **PASS** — project byte-identical |

The real-provider run is worth recording precisely: planner `sonnet`, analyst
`sonnet`, coder `opus`, reviewer `claude-sonnet-5`, independence
`DEGRADED_SAME_PROVIDER_FAMILY`. The reviewer returned `PASS_WITH_REPAIR`, the
single bounded repair ran, checks passed again (`pytest` exit 0,
`git diff --check` exit 0), and `changed_paths` equalled `allowed_paths`
exactly. The canonical checkout stayed at its initial commit with
`raise NotImplementedError` intact; the implementation exists only on the
branch.

## 13. The read-only pilot, and one more degenerate plan

The first pilot run against the real project produced a plan whose fields were
`"Test task"`, `"Test goal."` and `"test query"` — and the placeholder guard let
it through, because it required *every* token to be a placeholder and "task",
"goal" and "query" are ordinary words. The run then searched three providers for
"test query" and reported success.

The guard now refuses a field that contains a placeholder token and nothing but
structural filler besides. Deliberately still narrow: "Read the field",
"Testing the estimator", "Assess the query planner's fallback behaviour" and
"Implement the goal-seeking solver" all pass.

The re-run produced a genuinely substantive plan — a literature query on
diagonal-Hessian approximations for rank-one covariance penalties tied to
EVI-0007 and EVI-0008, an analysis reading the real gate artifacts behind
EVI-0008/EVI-0009 and the manuscript lines `STATE.md` flags as stale, and a
checkpoint asking which gap to fund next — and stopped at that checkpoint.

Both runs left the project byte-identical: HEAD `8733ad75`, zero modified files,
capsule SHA `d6687d55ce9b084c` before and after.

## 14. Final finding ledger

Every substantive finding raised across four reviews and the release audit, with
its disposition. Nothing is omitted because later code changed.

| # | Finding | Disposition | Regression |
| --- | --- | --- | --- |
| S1 | Experiments ran in the canonical checkout; `.venv` projects refused outright | FIXED | `test_an_experiment_runs_in_an_isolated_worktree_by_default`, `test_a_project_with_a_virtualenv_is_not_refused` |
| S2 | Worker could forge a controller section in the automation reviewer's prompt | FIXED | `test_a_worker_cannot_forge_a_controller_section_in_the_reviewer_prompt` |
| S3 | Run-lock race (three wrong fixes) | FIXED | `test_a_process_that_opened_before_release_cannot_hold_alongside_the_next` + 13 more |
| S4 | Killed run lost its delegated spend | FIXED | `test_an_interrupted_delegated_task_is_charged_on_both_resume_paths` |
| S5 | Analyst correction truncated the identifier to repair | FIXED | `test_the_correction_carries_the_identifier_the_worker_must_fix` |
| S6 | Literature unfenced to the paper writer | FIXED | `tests/test_paper.py` |
| S7 | `ISOLATED_WRITE` had no tool allowlist | FIXED | `test_an_isolated_write_worker_cannot_be_given_a_command_tool` |
| N1 | Experiment worktrees orphaned and invisible | FIXED | `test_a_worktree_left_by_a_run_that_never_started_is_reclaimable` |
| N2 | Same forged-section defect in the paper reviewer | FIXED | `test_no_prompt_builder_wraps_untrusted_text_in_a_markdown_fence` |
| N3 | Same construct, self-injection sites | FIXED | as above |
| N4 | `paper cleanup` leaked its lock file | FIXED | `test_paper_cleanup_releases_the_worktree_and_its_lock` |
| N5 | Reconciliation double-charged | FIXED (twice) | `test_two_delegated_runs_on_one_task_are_not_double_charged` |
| N6 | Alleged literal `\n` in the paper writer | **FALSE_POSITIVE — VERIFIED** | byte-identical output re-confirmed this pass |
| R4-1 | Manifest ids interpolated raw into the writing reviewer | FIXED | `test_a_writer_cannot_forge_a_controller_section_in_its_reviewers_prompt` |
| R4-2 | `charged_total` unit mixture | FIXED | as N5 |
| R4-3 | Same-second run-id collision on retry | FIXED | `test_two_delegated_runs_on_one_task_are_not_double_charged` |
| R4-4 | Writing reviewer's access position unasserted | FIXED | `_reviewer_setting` |
| R4-5 | Stress-test docstring made a false claim | FIXED | docstring corrected, floor raised |
| P1 | Degenerate plan of bare placeholders | FIXED | `test_a_plan_made_of_placeholders_is_refused` |
| P2 | Degenerate plan dressed in real words | FIXED | `test_a_plan_dressed_up_in_real_words_is_still_refused` |
| D1-1 | Sixth instance: `checks` rendered raw in both writing prompts | FIXED | `test_no_worker_authored_text_reaches_a_prompt_outside_a_data_block` |
| D1-2 | `TASK_FENCE` rewrite falsified the automation reviewer's own paragraph | FIXED | prose corrected at `automation/reviewer.py` |
| A1 | Seventh instance: `unresolved_caveats` sanitised but unfenced | FIXED | as D1-1 |
| — | Fifth unfenced channel: all task text | FIXED | `test_every_reviewer_prompt_renders_worker_text_through_the_boundary` |
| F1 | `unresolved_caveats` outside every data block (same as A1, found twice) | FIXED | `test_no_worker_authored_text_reaches_a_prompt_outside_a_data_block` |
| F2 | `packet.limitations` rendered multi-line outside a fence | FIXED | as F1 |
| F3 | `severity` / `check` bypassed `prompt_safe` inside the new fence | FIXED | fail-closed either way; closed for symmetry |
| F4 | `_hostile_grounding` docstring overclaimed at its second call site | FIXED | docstring corrected |
| F5 | Framing paragraph still false: the worker report is marked neither kind | FIXED | `test_the_automation_reviewer_prompt_holds_only_the_two_kinds_it_declares` |
| F6 | `CHECK_OUTPUT_FENCE` mislabelled at both new uses | FIXED | `CHECK_RESULT_FENCE`; manuscript moved to `REPOSITORY_FENCE` |
| F7 | `automation_dispatch_attempted` had no test anywhere | FIXED | `test_a_dispatch_is_numbered_before_it_is_attempted` |
| F8 | Two fixtures documented a state the controller can no longer produce | FIXED | docstring corrected |
| F10 | The stated reason for the FILLER trim was untested | FIXED | added to `test_real_prose_containing_a_structural_word_is_not_refused` |
| F11 | `paper/writer.py::build_repair_prompt` had no test at all | FIXED | covered by F1's pair builder; mutation-tested |
| F12 | This ledger omitted the findings its own commit repaired | FIXED | these rows |
| F9 | New event records `attempt` but not the inner run id | **DEFERRED** | stated below |
| G1 | F5's detector rendered a stubbed context and asserted a subset against a pre-approved allow-set; the F5 relapse passed it | FIXED | real context packet, set equality; relapse now caught |
| G2 | The framing paragraph's rewrite dropped the clause covering repository file content, which the prompt does carry | FIXED | as G1 |
| G3 | `_worker_prompt_pairs` omitted `build_writer_prompt`, whose manuscript is worker-authored and whose reader is write-enabled | FIXED | prompt added; unfencing it now caught |
| G4 | `..._burns_its_number` hand-wrote its own event; its closing assertion reduced to `make_run_id(X) == make_run_id(X)` | FIXED | rewritten to kill a real dispatch inside `start` |
| G5 | Eighth instance: `order.title`, written by the research planner, outside every block in four prompts | FIXED | as G3 |
| G6 | `[diff truncated for review]` sat outside the block; its presence is worker-influenced | FIXED | moved inside, matching the automation reviewer |
| G7 | Manuscript path separators in `build_writer_prompt` unsanitised | FIXED | `prompt_safe` |

### The property that ended the series

Seven findings across five reviews are one defect: worker-authored text reaching
the part of a prompt that speaks in the controller's own voice. Each round was
found in a prompt the previous round had not examined -- automation reviewer,
writing reviewer, proposal assessor, manifest ids, the deterministic-check list,
then the caveat list -- and each fix was verified against the headings of the
prompt that had just been broken. That is testing the instance.

The release audit replaced it with the property. `_worker_prompt_pairs` renders
each of the four prompts a worker string reaches -- both reviewers and, more
importantly, both *repair* prompts, whose reader is write-enabled -- twice from
identical structure, varying only the worker-authored fields. The assertion is
that everything outside every data block is byte-identical between the two
renderings: the controller's own voice is written entirely by the controller.

It needs no list of headings to maintain, it covers a prompt the day that prompt
joins the builder, and it catches the folded single-line injection a
heading-shaped search cannot see -- which is exactly how finding A1 was found,
after the heading test had passed.

Mutation-tested against every one of the known defect sites, each restored to
its pre-fix rendering one at a time:

| mutant restored | caught |
| --- | --- |
| `paper/reviewer.py` manifest ids unfenced (R4-1) | yes |
| `paper/reviewer.py` checks unfenced (D1-1) | yes |
| `paper/writer.py` repair checks unfenced (D1-1, second copy) | yes |
| `paper/reviewer.py` caveats unfenced (A1) | yes |
| `automation/reviewer.py` diff unfenced | yes |
| `automation/executor.py` repair diff unfenced | yes |
| `automation/executor.py` check output unfenced | yes |

Two of those escaped the first version of the test, and both escapes were the
same mistake this record has now logged four times: the harness did not vary the
field carrying the defect. The first version varied only the caveat list, so the
manifest-id mutant went unseen; the fix was to drive every worker-authored field
of the manifest, bypassing the validators on purpose so that the test stays a
test of the prompt boundary rather than a second test of the validators.

### Five reviews, and what finally ended it

The second delta review returned no BLOCKER and four REPAIR-BEFORE-RELEASE
items. One of them, F1, was the same defect the property test had already found
and fixed an hour earlier and under a different name -- two independent methods
converging on the same line, which is the first time in this build that
happened.

The other three are worth separating, because they are not the same kind of
problem:

- **F5 was the third prose-only repair of one paragraph.** The automation
  reviewer's framing paragraph described its own blocks, and twice a review
  found the description untrue of the prompt: first because task text had moved
  into blocks the paragraph said carried no controller authority, then because
  the paragraph named a marker word -- UNTRUSTED -- that the implementer's
  report does not carry, and named a kind of block (captured command output)
  that this prompt does not have at all. Both earlier repairs were wording, and
  wording cannot be rerun. The third repair ships with a detector:
  `test_the_automation_reviewer_prompt_holds_only_the_two_kinds_it_declares`
  enumerates the fences the rendered prompt actually contains and fails if it
  holds a kind the paragraph does not describe.

- **F7 and F11 were repairs with no detector.** Deleting the line that writes
  `automation_dispatch_attempted` left every attempt numbered 1 and the whole
  suite green; `paper/writer.py::build_repair_prompt` appeared in no test at
  all, so half of the previous round's BLOCKER repair -- the half whose reader
  is write-enabled -- could be reverted unnoticed. Both are now mutation-proven:
  removing the event fails
  `test_a_dispatch_is_numbered_before_it_is_attempted`, and unfencing the
  repair prompt's check list fails the pair-builder property.

That is the pattern the whole build record keeps recording. Four tests in this
project have passed by not exercising what they named -- the lock racer, the
`HOSTILE_FIELDS` cross-product, `issues=[]`, and the first version of the pair
builder. The correction is not more tests; it is asserting the property rather
than the instance, and then mutating the implementation to prove the assertion
can fail.

### The gate that caught the detectors

The final release gate returned no BLOCKER and two REGRESSIONs, and both were
about the two detectors the previous commit had just shipped as its headline
work. Neither detected what its docstring claimed.

`test_the_automation_reviewer_prompt_holds_only_the_two_kinds_it_declares` was
written so that "a prompt that gains a third kind fails here rather than in a
fifth review". It could not. It rendered `context_text` as the literal string
`"(context)"`, so the `REPOSITORY FILE` blocks the production prompt carries on
every real run -- `AutomationController._invoke_reviewer` passes a full rendered
context packet -- never appeared in the prompt it enumerated. And it asserted a
*subset* against an allow-set that pre-approved `REPOSITORY_FENCE` and
`CHECK_OUTPUT_FENCE`, two kinds the paragraph names nowhere. Reinstating the
exact defect it was written for -- the acceptance-check list back inside a
`CHECK OUTPUT (UNTRUSTED PROGRAM OUTPUT)` block -- passed the whole file.

Worse, the paragraph it was guarding had become false in a new way. The rewrite
that closed F5 dropped the clause "file content from its worktree", which the
previous wording had, while keeping the "Two kinds" framing. The prompt carries
three.

`_worker_prompt_pairs` opened "Every prompt a worker-authored string reaches".
It built four and there are five: `build_writer_prompt` is handed the manuscript
as a previous writer invocation left it on disk, which is worker-authored text
going back into a *write-enabled* reader. Unfencing it left the entire suite
green.

The repairs are structural rather than another instance. The kind-enumeration
test now builds its context with the same `build_context` call the controller
makes, and asserts set *equality*, so an undescribed block fails and so does a
described one going missing. The pair builder gained the writer's prompt, and
gained the order's own free text -- `goal`, `completion_condition`, `title` --
which it had been holding fixed, making every `TASK_FENCE` site invisible to it.

Varying that text immediately failed the property, which is how the eighth
instance of this defect was found: `order.title`, written by the research
planner, interpolated on the `TASK T-001: ...` line outside every data block in
four prompts. The task id is regex-pinned and stays in the controller's voice;
the title moved inside the block.

Three tests in this project have now been *proven* by an outside reviewer to
pass without exercising what they name, on top of the four found earlier. The
count matters more than any individual fix: the lesson the build record keeps
re-learning is that a test written alongside its fix tends to encode the fix's
assumptions, and only mutation -- restoring the defect and demanding the test
fail -- distinguishes a detector from a decoration.

### Deferred, with stated residual risk

- **Experiment worktree created before its store record.** A crash in that
  window orphans a worktree, lock and branch with no record, invisible to
  `experiment cleanup` and `reclaim`. Reordering means writing the record twice;
  deferred rather than done at release time. Residual risk: leaked disk after a
  crash in a millisecond window, recoverable by hand.
- **`_is_interactive` is `sys.stdin.isatty()`.** Anything that allocates a PTY
  satisfies it. It is a usability and safety guard, not authentication — the
  binding rule that agents must not record human Reviews lives in `AGENTS.md`.
  Now stated in `SECURITY.md`.
- **`_HELD` is process-global, not thread-safe.** Unreachable today; recorded so
  a future threaded caller sees it beforehand.
- **Review independence is degraded.** Only one provider family is installed.
  Every run says so before it starts.

---

# Part III — Operational hardening for v1.0.0

The repository became public between Part II and this. That changed what the
work was: v1 was already built and tagged, so what remained was to screen what
had been published, close the two crash windows Part II deferred with stated
residual risk, and find out by *running* the system whether it is useful rather
than only safe.

The last of those produced most of this section. Three defects below were found
by pilots and none by review.

## 15. The public-history audit

No scanner was installed — no `gitleaks`, no `trufflehog`, no
`detect-secrets` — so the scan was written: every unique blob reachable from
every ref, decompressed and matched against credential patterns for the
providers this project could plausibly touch, plus a Shannon-entropy sweep for
credential-shaped strings nobody thought to pattern-match.

484 blobs, 484 scanned, **zero credible secrets**. The entropy sweep's 97
candidates were all long Python identifiers (`allowed_check_programs`,
`min_interval_seconds`). The single `password`-shaped hit was
`api_key="secret-token"` in `tests/test_lit_http.py`, a fixture.

The privacy sweep found three things worth naming and none worth rewriting
history for:

- `ni.acevedo.villena@gmail.com` in `pyproject.toml`. Deliberate package author
  metadata, and normal for a published package. Recorded so the choice is a
  choice.
- `.cursor/plans/r0_kernel_plan_cd53bab0.plan.md`, deleted before publication
  and still reachable in three blobs. It contains `/home/nicacevedo/...` and the
  repository's own public remote URL. Neither is a disclosure; the path is now
  gitignored. Rewriting public history for a cosmetic path would cost every
  existing clone and buy nothing.
- No IP addresses, no institutional endpoints, no manuscripts, no datasets, no
  provider transcripts.

Largest blob in the entire history: 88 KB. Whole `.git`: 6 MB. Nothing binary,
nothing licensed, nothing generated was ever committed.

**The repository has no licence**, and none was invented. A public repository
without one grants no rights to anyone, which is a decision the author has to
make and an automated change must not. The README now says so plainly, and
`tests/test_packaging.py` asserts only that the metadata and the tree cannot
*disagree* about the answer.

One finding was not a secret and mattered anyway: the repository's **default
branch, `main`, was still the initial bootstrap commit** — twelve files and an
empty `README.md`. Every visitor to the public page had been reading that. It is
an ancestor of the release, so fast-forwarding it discards nothing.

## 16. The two crash windows

Both were reported at the end of Part II as deferred with stated residual risk.
Both have the same invariant: *before an irreversible side effect can become
orphanable, the thing that owns it must already exist on disk.*

**Outer/inner run dispatch.** A research run learned its inner automation run's
id from `start()`'s return value. A crash inside `start` — after the run
directory existed — left a directory whose id lived only in the dead process,
and the id is a digest over a timestamp nobody recorded, so it could not even be
recomputed. The parent now reserves the identity, writes it to its own ledger,
and passes it in. `reserved_dispatches` reports every reservation that never
finished and says whether the directory is actually present, so a reader never
infers a crash point.

That also closed a budget leak nobody was looking for: delegated spend was
reconciled only from runs that reported *starting*, so a run killed inside
`start` spent model calls nothing counted.

**Experiment worktree and store registration.** `create_worktree` registers a
worktree in the researcher's own repository and locks it, and that happened
before `ExperimentStore.create`. The record is now written first, in a new
`PREPARING` state, with the worktree path derived rather than observed.

Writing the path down before creating it exposed two defects that were invisible
while it was only ever read back:

- `_worktree_run_id` documented itself as returning "a stable id" and read the
  clock for the timestamp half. Two calls a second apart disagreed. A defensive
  equality check in `run()` caught it on the first test run.
- An experiment retried inside the same second collided on its run id, because
  experiment ids had no attempt discriminator — the one automation ids gained,
  in Part I, for exactly this reason. Before the reordering the first attempt
  never wrote a record, so the collision was unreachable; writing the record
  first made retrying-after-failure the one thing that could not work.

**The lock.** Worktree locks and run locks share a directory and have opposite
lifecycles. Part I spent three wrong fixes learning that unlinking a held
`flock`'s *name* lets the next arrival lock a different inode at the same path.
Every lock removal added here therefore derives its target from the worktree
path and re-checks the derived filename, so no recovery path can reach a run
lock even if handed one. Proven live: a `storage --reclaim` released three
worktrees while all 36 run-lock inodes stayed byte-identical.

`experiment cleanup` also used to return early when the worktree was absent,
leaving the lock a crash between locking and creating had left — which made that
path permanently unusable by a run whose owner had died.

`tests/test_crash_recovery.py` injects a failure at each boundary rather than
manufacturing the final state, because the question is not whether an orphan can
be cleaned up once found. One of those tests found a bug in *itself* first:
`monkeypatch.undo()` reverts the fixture's environment relocation too, so the
assertion after it was reading the researcher's real state directory.

## 17. The bounded grounding correction

The Part II pilot produced a proposal citing `L-002`, a literature key that did
not exist, and the grounding validator refused it. That refusal is correct and
is preserved. What was missing was the ability to fix one wrong reference
without a human restarting the run.

One attempt. Same evidence packet, same goal, no new authority. A second
invented identifier fails closed.

The design decision worth recording is the **trigger**. It is computed from the
payload — `grounding_violations` re-derives which citations were unsupplied —
never from the exception's message. A classifier built on error text is one
rewording away from treating some *other* refusal as correctable, and the whole
value of the bound is that it applies to exactly one failure mode.

Provenance needed one correction of its own: the refused payload is written out
as itself, because a provider answering through a structured-output schema
returns no text at all, so relying on the text file would have preserved the
correction and lost the thing it corrected.

**It fired in production during Pilot B**, on a proposal citing `PR-002` and
`PR-003` as capsule objects. One correction was spent; the corrected output was
still invalid, for a different reason; the run failed closed with no second
attempt. Exactly case B of the regression suite, unrehearsed.

## 18. Literature transport

The Part II pilot reached Crossref, timed out on arXiv, and got a 429 from
OpenAlex. Two of those are the network being the network. The third cost the
entire arXiv leg, because a request that never *completed* was reported
unavailable without a single retry — while a 503 was retried. There is no
principle behind that distinction.

A transport failure now retries on the same bounded terms. A settled refusal —
not HTTPS, offline mode — still is not retried, and that distinction is now in
the type system rather than in a comment.

`Retry-After` is also honoured in its HTTP-date form. Reading only the seconds
form meant a date-form header fell through to a two-second backoff: a client
that believed it was being polite retrying far sooner than it had been asked to.

No HTTP response cache was added. The literature store already is the cache, and
a second layer in front of it would be a new place for the two to disagree.

## 19. What the pilots found

Three defects, none of which review had found.

**Pilot B, first attempt.** A read-only run against a real project died on its
first planner call with `error_max_structured_output_retries` — the provider's
own structured-output machinery giving up — and was abandoned with thirteen of
fourteen model calls unspent. The bounded re-ask that already existed covered a
plan that *arrived and was refused*, not one that never arrived. The same single
re-ask now covers both, with the same prompt, because nothing was rejected and
so there is nothing to correct.

**Pilot C, first attempt.** The plan named a declared experiment command and
omitted its required `seed`. The validator checked that the command *name* was
declared and stopped there — its own docstring already made the argument for
finishing the check. The run spent three model calls and stopped a human before
failing on something knowable the instant the plan was parsed. Refused at
planning time now, where the bounded correction can repair it.

**Pilot C, second and third attempts — not a system defect.** The synthetic
repository was `src`-layout, and the planner chose a bare `pytest` rather than
`uv run pytest`. A bare acceptance command runs in the inherited environment by
design, so the project's own package was not importable and its tests could not
collect. The controller behaved correctly throughout: checks failed, one bounded
repair ran, the repair fixed the import and introduced a lint error, and the run
failed closed rather than handing over code that fails the project's own gates.

That is worth recording as an operational note rather than a fix: **for a
`src`-layout Python project, an acceptance command must be `uv run pytest`, not
`pytest`.** The fixture was rebuilt flat-layout rather than the prompt tuned,
because tuning a prompt until a stochastic model passes is not evidence.

## 20. Mutation proofs

Twelve protections were disabled one at a time and the suite re-run. All twelve
were detected; none was missed.

Removing the child-run reservation, writing the experiment record after the
worktree, letting an invented identifier through validation, allowing a second
grounding correction, skipping the correction's budget check, letting lock
release take a caller-supplied path, making reclaim blind to a lock with no
worktree, making the derived worktree id read the clock again, stopping
transport retries, ignoring a date-form `Retry-After`, turning an unreachable
source into an empty `OK` result, and letting the two declared versions drift.

Three more were run against the pilot-driven fixes: removing the provider retry,
unbounding it, and skipping its budget check. All three detected.

## 21. Residual risks after v1.0.0

- **No licence.** Human decision. Nothing is granted until it is made.
- **A `src`-layout project needs `uv run pytest`.** A plan naming a bare
  `pytest` will fail to import the project under test. The failure is loud and
  fails closed; it is a planning trap, not a hole.
- **The planner inserts human checkpoints readily.** Two of four pilots stopped
  at one. That is the designed behaviour and it is correct — one of them was a
  genuine scientific decision — but a run intended to complete unattended has to
  say so in its goal.
- **Review independence is still degraded.** Only `claude` is installed. Every
  run says `DEGRADED_SAME_PROVIDER_FAMILY` before it starts.
- **Worktree isolation is not an OS sandbox**, and project checks execute
  project code with the user's permissions. Unchanged, and now stated on the
  first screen of the README rather than only in `SECURITY.md`.
- **`_HELD` is process-global, not thread-safe.** Unreachable today.

## 22. The independent delta review, and what it found

One read-only review of `research-os-v1..HEAD`, no write authority, fresh
context, strongest local model. Verdict: **PASS WITH BOUNDED REPAIR**, three
concrete defects.

The first run of that review had to be abandoned: given permission to run the
suite, it spent its budget re-running fourteen minutes of tests instead of
reading the diff. Re-scoped to six files with an explicit ban on the full suite,
it returned in under six minutes. Its last words before being stopped were
"now let me test the budget-cap boundary I suspect is unenforced" — which was
correct, and is the fourth item below.

**F1 — a live experiment preparation could be reclaimed out from under itself.**
The fix for one race opened another. `PREPARING` is deliberately not in
`ACTIVE_STATES`, which is what lets recovery see a crashed preparation; it also
made a preparation *in progress* look idle, so a concurrent
`storage --reclaim` could delete the worktree a running process was in the
middle of creating, and release its lock.

Refusing to reclaim `PREPARING` unconditionally would have put the orphan the
state exists to expose straight back out of reach, so the answer had to
distinguish a live preparation from a dead one. The kernel already answers
exactly that question: a `flock` is released when its holder dies. The
preparation now holds this run's lock across the whole window, and both
`diagnostics` and `experiment cleanup` ask `runlock.is_held` before touching it
— asked twice, because a preparation can begin between reclaim selecting a run
and reclaim deleting it. No staleness heuristic, no timeout, no note to misread.

The first regression written for this did not discriminate: it inspected
reclaim's view *before* `create_worktree` ran, so there was nothing on disk to
offer and the assertion passed however the idle rule was written. Mutating the
rule proved it. The test now looks after the worktree exists and while the
record still says `PREPARING`, and catches all three mutations — including the
over-correction that never reclaims.

**F2 — a failed proposal task charged the run nothing.** The charge sat after
the call that raised. A planner that reliably cites a nonexistent key could
spend the literature call, the proposal call and the bounded correction, fail,
be retried, and spend three more against a ledger that had not moved — a
model-controlled path to unbounded real spend under a budget that only counted
successes. Proposal errors now carry what they cost and the research controller
charges it before re-raising. The automation dispatch path already reconciled
its uncharged inner spend; this one was the gap beside it.

**F3 — a `Retry-After` header could crash the request that received it.**
`str.isdigit()` is True for characters `float` refuses: `Retry-After: ²` passed
the check and raised `ValueError` out of a request that had already reached the
provider, as a type no caller catches. The date-form branch added in this
release was written total; the seconds-form branch beside it was not.

**F4 — informational, accepted as-is.** A payload with both a grounding
violation and an unrelated structural defect still enters the correction, whose
prompt lists only the grounding errors. Cost is exactly one budget-checked,
non-recursive call, and the second failure is reported honestly.

**The budget ceiling, found by the abandoned first reviewer and reproduced.**
`max_model_calls` was checked in one place — the bounded correction, where it
was first needed — and nowhere else. A run asked for a ceiling of one made two.
A ceiling enforced at one of four call sites is not a ceiling; it is a parameter
whose name promises something the code does not do. It is now checked before
every spend, and `MINIMUM_CALLS[PROPOSAL]` rose from 2 to 3 to match what a
proposal with literature actually costs, so the entry gate and the ceiling agree.

**What the review could not break**, recorded because it is the more useful
half: no argument to `release_worktree_lock` can name a run lock (sha256 hex
cannot contain `r`, `u`, `n` or `-`), and the real fix was removing the old
`unlink(record.lock_path)` — a field persisted outside the repository that could
previously have directed an arbitrary unlink. Exactly one grounding correction,
with `ProposalGroundingError` subclassing `ProposalValidationError` checked
specifically for whether it could re-arm the handler; it cannot. Planner
invocations bounded at two by exhaustion of all four orderings. The refused
proposal cannot forge a fence: `json.dumps` escapes newlines before
`prompt_safe_block` scrubs delimiters before `render_data_block` refuses an
ambiguous fence outright. Reserved run ids re-validated against `RUN_ID_RE`
before any path is built. Transport retries and 5xx retries share one counter,
so they cannot be alternated to exceed the bound. Settled refusals raise outside
the retry `try` and are never retried.

---

# Part IV — v1.1, the operational autonomy candidate

Branch `release/v1.1.0-autonomy`, from `b3fd03c` (v1.0.0). **Not merged, not
tagged.** Fourteen commits, 8,655 insertions against 89 deletions, 2,362 tests
to 2,552.

## 23. What v1.1 is for

Five things a model was being asked to judge that the controller can know.

Three of them are the same defect wearing different clothes, and both v1.0.0
pilots that went wrong went wrong this way. A `src`-layout project's tests only
import under `uv run`; the planner wrote a bare `pytest`; the run failed closed
having verified nothing. A repository with no `.research/` capsule was handed
the scientific universe; the only identifiers available to the worker were ones
it invented, and the grounding validator refused them. Neither was a model being
careless. Both were a model being asked a question it had no way to answer, and
being held to an answer it was never given.

So: a deterministic **ProjectProfile**, a controller-chosen **provenance mode**
with a capsule-less **TechnicalAssessment** beside the scientific proposal,
controller-owned **validation profiles**, a typed **checkpoint policy** so
"unattended" is a property rather than a hope, and **persistent literature
pacing** so a provider's "wait an hour" survives the process that heard it.

## 24. The fourteen commits

| SHA | what |
| --- | --- |
| `91397ec` | deterministic project profiles |
| `7b0134f` | grounded capsule-less assessments |
| `5efa76d` | deterministic validation profiles |
| `ae56391` | scientific-only autonomy policy |
| `31a93bf` | persist literature provider pacing |
| `fa23102` | repair what the first CCAO pilot found |
| `3da5d37` | repair what the cuPDLP pilot found |
| `c25d493` | stop quoting a repository's file list twice |
| `2c612e3` | stop the profile contradicting itself |
| `089bbe1` | close the plan guard a live run walked through |
| `adb87da` | make every broken reference correctable, not just some |
| `72600d9` | tell the correction worker to renumber what it drops |
| `307236f` | tell the planner what a declared experiment requires |
| `2da74e1` | say which ids belong in which proposal field |

The first five are the planned work. The last nine are all pilot repairs, which
is the honest ratio: the design took five commits and finding out what was wrong
with it took nine. The user-facing documentation (`README.md`,
`ARCHITECTURE.md`, `docs/RESEARCH.md`, `docs/LITERATURE.md`) landed inside
`adb87da` rather than in a commit of its own — an accident of sequencing, noted
here because a reader looking for it by commit message will not find it.

## 25. The pilots, and the nine defects they found

Nine defects reached production code. Not one was found by the test suite, by
review, or by reading. Every one was found by pointing the thing at a real
repository.

**A shouty prompt made the planner produce garbage.** Pilot A against the CCAO
capsule failed twice under `scientific_only`, both times with the documented
degenerate plan: ten to twelve thousand output tokens of real work returned as
`{"summary": "test", ...}`. That could have been stochasticity, so it was
measured rather than assumed. Same goal, same project, same budgets: v1.0.0
planned richly first try in 10,034 output tokens; v1.1 under `standard` planned
richly first try in 9,130; v1.1 under `scientific-only` degenerated three times
out of three, at 10,722–12,643 tokens. The schema additions are in both v1.1
arms, so they are not implicated. The difference was 624 characters of emphatic
three-paragraph prose, opening in capitals, restating one rule three ways. It
says the rule once now, in 119 characters. Re-measured: no placeholder in three
attempts. `fa23102`.

**A refusal with no repair path.** Pilot B reached the assessment worker, which
returned seven observations, one of which rested on nothing. Refused correctly
— an ungrounded observation is an opinion — and terminally, because the
correction trigger knew about citations that were *wrong* and not about
citations that were *absent*. Fifty-five retrieved works and two completed
analyses thrown away over something the controller could describe exactly.
`3da5d37`.

**The same file list, twice.** Sizing the assessment prompt for Pilot C *before*
running it: 199,551 characters for a 5,402-file repository, of which ~196,000
were two copies of the same 1,200 paths. `c25d493`.

**A profile that contradicted itself.** Reading a real planner prompt during
Pilot B: `manuscripts: no -- tracked manuscript sources were found`, and
`python_project: no -- pyproject.toml or an importable package directory is
tracked`. One detail string per capability, written for whichever branch its
author had in mind and printed for both, inside a block whose opening line is
"these are facts, do not contradict them". `2c612e3`.

**The worst one.** A cuPDLP run reached `READY_FOR_HUMAN` on this plan:

```json
{"summary": "test summary two",
 "tasks": [{"id": "T-001", "kind": "literature",
            "title": "t", "goal": "g", "query": "q"}]}
```

It validated, executed, searched crossref and openalex for `"q"`, retrieved
nineteen works, and reported success. The guard had refused `"test"` on the
previous attempt and accepted `"test summary two"` for the same reason it
accepted `"t"`: it required at least one token to be a *known placeholder word*
and graded the rest against a word list. Three releases have now found this hole
from three angles — `"test"`, then `"Test task"`, then this — and each repair
added one more word. Two rules replace it, neither depending on having guessed a
word: a field under three characters says nothing whatever those characters are,
and a field whose every token is structural says nothing specific by
construction. The trade is asymmetric on purpose. `089bbe1`.

**Three shapes of one failure.** Pilot C's assessment pointed an uncertainty at
`"open_question"` — a sibling field name rather than an observation id. That
made three pilots finding three broken-reference shapes, each tempting a
one-instance repair. The set is now stated as a set: every reference that does
not resolve is correctable, because they all have one repair; every shape
problem is not, because there is nothing to reground. `adb87da`.

**A prompt asking for something another validator refuses.** Pilot C's
correction did exactly what it was told — dropped the observation that could not
be grounded — and left the ids running OB-001, OB-006. `72600d9`.

**A rule the planner was never told.** Pilot E was refused on both planning
attempts for omitting the `seed` its declared experiment requires. The refusal
is right. The prompt listed only command *names*: the controller knew the
command needed a seed in 0..7, never said so, and then refused the plan for not
knowing. v1.0.0 completed that check and left the telling. `307236f`.

**Three fields that look alike.** Pilot E's proposal correction removed the
invented citations it was shown and then wrote a capsule id into
`addresses_items`, which names proposed items. Prompt change only — no proposal
validation was relaxed and no new failure was made correctable, because
extending the proposal layer's *trigger* the way the assessment layer's was
extended would change scientific-pipeline behaviour an independent review has
already accepted. `2da74e1`.

## 26. What the pilots established

| pilot | project | mode | result |
| --- | --- | --- | --- |
| A | CCAO capsule, 1 Claim, 2 Experiments | `scientific_project` | READY_FOR_HUMAN, 5/12 calls |
| B | cuPDLP.jl, 14 files, Julia, no capsule | `repository_assessment` | READY_FOR_HUMAN, 12 grounded observations |
| C | 5,402-file results repository, no capsule | `repository_assessment` | READY_FOR_HUMAN, 11 grounded observations |
| D | throwaway `src`-layout uv project | `repository_assessment` | READY_FOR_HUMAN, 0 repairs |
| E | synthetic capsule project | `scientific_project` | READY_FOR_HUMAN, full pipeline |

Across all five, and every failed attempt at them: **zero canonical project
mutation**, verified by comparing `HEAD`, the tree object and the full index
digest before and after. Zero automatic Claim acceptances, zero automatic
Reviews, zero writes under `.research/`, zero merges, zero pushes.

**Pilot A** produced the boundary arithmetic the goal asked for: Gate E.4's
measured max M exceeds `M_accept_max` by 18.04% relative in exactly one cell of
45, while the curvature ratio sits 3.5x past its own boundary everywhere — so
the INDETERMINATE verdict is fragile only to a small relaxation of one of its
four thresholds and robust to the other three. It proposed a per-cell sweep to
verify that, because EVI-0008 reports only extrema, and it flagged as an open
governance question whether stating flip-point arithmetic reads as advocacy for
a threshold change. It changed no threshold and no verdict. One grounding
correction fired and is recorded in the proposal's own summary.

**Pilot B** asked whether the solver's reported residuals are measured in the
original problem space or the space `preprocess.jl` leaves the problem in, and
marked it `blocked_by_evidence: true` — there is no test, no CI and no benchmark
harness to answer it with. No `PR-`, `CLAIM-`, `EVI-` or `HYP-` token appears
anywhere in the object.

**Pilot C** recommended a single scoped change with its exact paths, and
separately flagged a file named `box_cookies.txt` under a validation log
directory as a possible committed session token, marked `requires_human`. That
is the researcher's decision and this session did not open the file.

**Pilot D** is the src-layout regression as an operational fact. The plan set
`required_checks: ["tests"]` and authored no command; the controller resolved
`["uv", "run", "pytest", "-q"]`; the check ran and returned exit 0, which is
what a bare `pytest` cannot do against that fixture; zero repairs; reviewer
PASS.

**Pilot E** ran Planner → literature (40 works, all three providers) → Analyst →
a real local experiment producing a candidate evidence packet → Coder →
controller-owned `uv run pytest -q` → Reviewer PASS → a grounded 11-item
proposal assessed PASS → READY_FOR_HUMAN. Nine of sixteen model calls, no
checkpoint, no manual intervention after execution began.

**The writer stage was deliberately not exercised in Pilot E.** A `paper` task
requires an accepted Claim, an accepted Claim requires a qualifying *human*
Review, and this agent must not author one — not even against a throwaway
fixture, because the act is what it is regardless of what it is performed on.
The paper layer is covered by `test_complete_research_project.py`, where the
human acts are performed by the test. This is a boundary being respected, not
coverage being dropped.

## 27. What the pilots could not be made to do

The planner reasoned correctly about the policy without being told to in prose.
Pilot B's final plan: *"no checkpoint task (nothing prespecified and no Claim
exists to accept, so any checkpoint would be discretionary and refused)"*.
Pilot E's: *"No checkpoint is included: this project holds no Claim, so there is
nothing for a human to accept mid-run"*. Pilot D's: *"No checkpoint: the
researcher's instruction already fixes the one decision that mattered."*

One CCAO attempt produced two successive plans each containing a discretionary
checkpoint. It was refused, re-asked once with the deterministic reason, refused
again, and failed explicitly. That is WP4.5's specified behaviour and not a
regression: one correction, no loop.

Adapt-Q was refused outright because its working tree had one modified notebook.
The v1.0.0 clean-tree preflight, doing its job.

## 28. Mutation proofs

Ten protections were disabled one at a time. Nine were detected. **One was
not**, and finding it is the most useful thing in this section.

| # | mutation | detected by |
| --- | --- | --- |
| M1 | discovery wins over declared capability | 2 profile precedence tests |
| M2a | capsule-id grounding check removed | 5 assessment tests inc. the PR-002 replay |
| M2b | repository-file grounding check removed | untracked-file and deleted-file regressions |
| M3 | discovered `tests` profile reverted to bare `pytest` | 8 tests inc. the end-to-end run |
| M4a | discretionary checkpoint allowed through `scientific_only` | 5 tests |
| M4b | `HARD_CHECKPOINT_KINDS` emptied | 2 tests |
| M4c | a claimed hard kind believed without corroboration | 5 tests |
| M4d | `scientific_only` refuses hard checkpoints too | 3 tests inc. the Gate E.4 regression |
| M5a | `BEGIN IMMEDIATE` downgraded to `BEGIN DEFERRED` | **initially MISSED** |
| M5b | the service no longer reserves before it asks | 2 tests |
| M5c | the cache is never consulted | 2 tests |
| M6 | ungrounded-observation detection removed | 3 tests |
| M7 | one capability detail printed for both branches | 2 tests |
| M8a | the minimum-field-length rule removed | 4 cases |
| M8b | the old require-a-placeholder-token rule restored | 3 cases inc. the live payload |
| M9 | internal-reference detection removed | 4 tests |
| M10 | experiment parameters dropped from the prompt | 1 test |

**M5a is the one that matters.** The lock test accepted *any* database error,
and a deferred transaction also fails — just later, from the write, after it has
already read a row another run was replacing. The test passed under the
mutation. It now asserts *which* failure: a run that takes the lock first fails
at the `BEGIN` with this module's own error; a run that reads first fails at the
`UPDATE` with a raw `sqlite3.OperationalError`. A second test holds a
reservation open mid-flight and watches the other connection fail to enter.
Re-mutated: detected by both.

## 29. Validation results

| gate | result |
| --- | --- |
| `uv run pytest -q` | 2,552 passed (2,362 at v1.0.0; none removed or disabled) |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean |
| `git diff --check` | clean |
| R0 kernel | 480 passed |
| security regressions | 580 passed |
| prompt/data boundary | 206 passed |
| command policy | 52 passed |
| lock/concurrency | 14 passed |
| crash recovery | 19 passed |
| grounding | 42 passed |
| ProjectProfile | 27 passed |
| TechnicalAssessment | 42 passed |
| validation profiles | 33 passed |
| checkpoint policy | 28 passed |
| literature pacing | 34 passed |
| clean clone, full suite | 2,552 passed |
| clean clone, hermetic subset with no credentials | 905 passed |

The literature store migrated 1 → 2 on the real pre-existing database and the
pacing state is live: three sources recorded, arXiv showing a real failure at
19:11:18 and a real success at 20:16:15, which is the "a failure is not a rate
limit" property demonstrated rather than asserted. Thirty-six of seventy-two
archived searches carry a `cache_key`; the thirty-six written before the
migration carry `''` and are correctly never served as a cache hit.

## 30. Residual risks after v1.1

- **No licence.** Human decision, unchanged from v1.0.0. Nothing is granted
  until it is made.
- **The default `sonnet` planner is unreliable on these prompts.** Across today
  it degenerated or gave up on structured output in roughly half its attempts
  against real repositories, and cuPDLP.jl was already failing this way at
  v1.0.0 — two attempts, two failures, before any v1.1 code existed. Pilots B
  through E were run with a configured `opus` planner, which is a researcher's
  ordinary configuration choice and is recorded as one. The controller's
  behaviour under the failure is correct throughout: it refuses, spends its one
  re-ask, and fails closed. What it cannot do is make the answer arrive.
- **An all-filler field is now refused.** "First results" as a task *title* is
  refused where it was not before. The cost of that false positive is one
  re-run; the false negative it replaces was a run reporting success having
  searched for `"q"`.
- **A large repository still makes a large prompt.** 101,704 characters for
  5,402 tracked files, after halving. The 1,200-path cap on the citable list is
  a documented bound, not a tuned one, and a repository past it has paths an
  observation cannot rest on.
- **The proposal layer's correction trigger was deliberately left narrow.** An
  internal cross-reference error in a *proposal* is still terminal where the
  same error in an *assessment* is now correctable. Widening it would change
  reviewed scientific-pipeline behaviour, which this release does not do. The
  prompt was clarified instead.
- **Review independence is still degraded.** Only `claude` is installed. Every
  run says `DEGRADED_SAME_PROVIDER_FAMILY` before it starts, and so does the
  delta review of this release.
- **Worktree isolation is not an OS sandbox.** Unchanged.
- **10.8 GB of finished-run worktrees are held** after today's pilots.
  `researchctl storage --reclaim` releases them; they were left in place so the
  external reviewer can inspect the pilot evidence.

## 31. The independent delta review, and the twelve things it found

One read-only review of `v1.0.0..2da74e1`, fresh context, strongest local model,
no write authority and an explicit ban on running the test suite — the same
scoping that made the v1.0.0 review usable after its first attempt spent its
budget on pytest. Verdict: **PASS WITH BOUNDED REPAIR**, twelve findings. Three
graded DEFECT, seven MINOR, two INFORMATIONAL. All twelve were repaired.

Independence: **`DEGRADED_SAME_PROVIDER_FAMILY`**. Only one model family is
installed on this machine. That is the gap the external review exists to close
and it is the reason this candidate is not being merged.

### The three defects, which are one defect

Every one of them was the controller claiming to know something it did not.

**The capsule test accepted a directory on disk.** `capsule_present` was
`".research/project.yaml" in tracked or (root / ".research").is_dir()` — two
substrates, one of them the working tree, inside a module whose stated premise
is that a file somebody left lying around cannot change what kind of project
this is. A crashed `init-project`, a hand-made directory or a symlink flipped
it. And it never asked whether the capsule could be *read*, while
`build_science_context` decides the same fact by asking exactly that. When they
disagreed, one prompt carried both *"This project holds a Research Capsule, so
scientific objects exist and may be cited by their identifiers"* and *"no
Research Capsule at .research, so this project holds no scientific state"* —
under a heading saying these are facts and not to contradict them. The worker's
only available citations were then invented ones: the capsule-less failure this
release exists to close, reached from the other side. The researcher could not
override it either, because the config key is per project id and a project whose
capsule will not parse has no id to key on.

**`cross_project_promotion` was a free pass.** Every other hard kind is
corroborated against something the controller reads. This one was refused only
when there was no capsule at all — so any checkpoint in any capsule project
could be relabelled `cross_project_promotion`, and an unattended run would stop
to ask "shall I continue?" with the ledger recording it as hard. That is the
single failure `scientific_only` exists to prevent, surviving inside the feature
built to prevent it. `costly_authorization` had the weaker version: one
experiment task anywhere made the label eligible for every checkpoint in the
plan, including ones placed after the experiment had already run, and including
runs that were never authorised to spend anything.

**Check discovery offered `uv run pytest` to projects that declare no pytest.**
A `tests/` directory was enough. Because a profile then exists, the plan is
*forced* to name the check and forbidden from writing its own — so the command
cannot spawn, the bounded repair burns on an environment error, and the run
fails closed having verified nothing. The v1.0.0 trap, reintroduced through the
profile instead of through the planner, with the profile printing
`pytest_available: no` beside it.

### The nine smaller ones

A cache-served retrieval wrote no `SearchRecord`, three lines under a docstring
promising every path records one. `network_calls` counted a refused reservation
and a real 429 the same way, so it answered the quota question wrongly in
exactly the case it was asked. The long-`Retry-After` early return fires for
five statuses and read the header for one. A successful search erased a recorded
quota reset while the stale count beside it survived. The pacer leaked raw
`sqlite3.Error`, which is not in `WORKER_ERRORS` and would leave a research task
`RUNNING` rather than `FAILED`; and a failed `COMMIT` left the transaction open
under a restored isolation level, so the next write would silently join it. A
truncated tracked-file list was reported as a deterministic *absence*.
`MINIMUM_CALLS[PROPOSAL]` stated a requirement that is false for the dispatch it
refuses. A symbol could not be written `solve()`.

And the placeholder guard, which this release had **over-corrected**. Dropping
the "at least one token must be a placeholder" half also refused "Query the
data", "Results summary" and "First results" — ordinary titles — costing a
re-ask on a plan that was fine and then a failed run on the second identical
phrasing. The requirement is restored. It still catches every degenerate field
any live pilot produced, because every one of them contained "test", and the
length rule still catches the class no word list can.

### One suggestion not taken

A nesting guard on the pacing transaction was written, broke a test that
legitimately left an uncommitted write on the shared connection, and was
removed. The review had already verified `_immediate()` is never nested, so it
was speculative hardening rather than a repair of anything found.

### What the review could not break

Recorded because it is the more useful half, and because it is what the external
reviewer should try to break next. No model-originated value reaches
`ProjectProfile`, a `CheckProfile` argv, or a checkpoint's eligibility decision
— the reviewer traced every input and found `git ls-files`, `tomllib`, and a
config file outside every worktree. A `CLAIM-0001` cannot be got through a
capsule-less assessment: `capsule_ids` is hard-coded empty and
`_reject_unsupplied` compares against it. A `FileRef` cannot escape the
repository: field-level rules reject absolute, traversing and backslash paths,
and the survivor must then be an exact member of `git ls-files`. `PlannedCommand`
declaring `required` means `extra="forbid"` would *not* have caught a model
setting it — the explicit `parse_plan` guard is the only thing that does, and
`parse_plan` is the only construction site fed a model payload. `_reach_checkpoint`
does not consult the policy at all, so no branch can drop a hard checkpoint. The
replan bound is `for correction in (False, True)`: structurally two iterations.
The migration is correct against a real v1 database and `cache_key != ''` means
a pre-migration row can never be mistaken for a cache entry. And there is no
path that spends a model call without charging it — the reviewer enumerated
every raise site either side of the first `_invoke`.

### Mutation proofs for the repairs

Nine more mutations, one per repair. Eight detected on the first attempt; **one
was not**, and it is the same lesson as `M5a`:

| # | mutation | detected by |
| --- | --- | --- |
| M11 | `capsule_present` accepts an on-disk `.research/` again | 3 tests |
| M12 | `capsule_present` stops asking whether the capsule parses | 2 tests |
| M13 | `cross_project_promotion` needs only a capsule again | 2 tests |
| M14 | a `tests/` directory alone justifies `uv run pytest` again | **initially MISSED** |
| M15 | a cache hit is not archived | 1 test |
| M16 | a 5xx `Retry-After` is not persisted | 1 test |
| M17 | `quota_reset_at` is clearable again | 1 test |
| M18 | `network_calls` filters on status again | 1 test |
| M19 | the failed `COMMIT` leaves the transaction open | 1 test |

M14 was missed because every fixture in the check-profile tests declared pytest,
so no test distinguished "declared" from "has a tests directory". Two now do,
one of them an invariant asserting that a discovered profile never contradicts
the capability printed beside it — which is the property that was actually
violated, rather than the instance.

### After the repairs

Full suite 2,581 passing, `ruff check`, `ruff format --check` and
`git diff --check` clean. Two further consecutive workflows against real
projects — the `src`-layout check-profile pilot and the capsule-less cuPDLP.jl
assessment — both `READY_FOR_HUMAN`, no new controller defect, and every pilot
project's tree and index digest still identical to before the campaign began.

## 32. The planner reliability gate, and the one value it moved

### The discrepancy

v1.1 shipped with `planner.model: sonnet` and every pilot that *succeeded* from
B onwards had `opus` configured by hand. §30 recorded that as a residual risk in
the honest but unusable form: "roughly half its attempts." Roughly half of an
unrecorded denominator, gathered while the code under it was changing, is an
anecdote. A released default has to rest on something better, so the question
was measured before the candidate was frozen.

### Protocol

Five fixtures, one per category the campaign had exercised, each taken from a
specific archived run of the v1.1 validation campaign. Nothing was hand-written:
for each fixture the prompt and **every validator input** — `ProjectProfile`,
provenance mode, budgets, checkpoint policy, available check profiles, declared
experiment commands and their required parameters, and the capsule counts the
checkpoint eligibility rule reads — were re-derived by calling the same
functions `ResearchController.start` calls, and the result was digest-compared
against the prompt the real run actually sent.

| fixture | category | source run | chars | prompt sha256 |
| --- | --- | --- | --- | --- |
| `F1-ccao-capsule` | capsule-aware CCAO scientific project | `RR-20260914T154755Z-7cfc4cb0` | 77,499 | `3189110d3ce4401b…` |
| `F2-cupdlp-capsuleless` | capsule-less cuPDLP repository assessment | `RR-20260915T003602Z-30656094` | 11,934 | `8f7cf55b67e0f783…` |
| `F3-large-assessment` | large repository assessment (5,402 files) | `RR-20260914T190927Z-234b0a63` | 31,398 | `968406f609d8432b…` |
| `F4-srclayout-code` | `src`-layout implementation workflow | `RR-20260914T201232Z-6a71d9b9` | 9,406 | `57948dcdd94b6d98…` |
| `F5-experiment-write` | synthetic capsule project, experiment + write | `RR-20260915T004424Z-1df69b1d` | 13,530 | `d9b1d96bc788f131…` |

The reconstructions differed from the archived text in two ways and no others: a
context `generated_at` timestamp, and — for F1 and F3 — prompt wording that
later v1.1 repair commits had changed. Benchmarking the shipped prompt is the
point, so the reconstruction is what was sent, and it is never the easier text:
F1 and F3 gained the sentence telling the planner that an omitted experiment
parameter is refused. F5's declared experiment command lives in the pilot's own
config home and was restored from it; without that the fixture would silently
have lost the declared-experiment half of its category, which is exactly the
kind of quiet simplification the protocol forbids.

Thirty real calls through `ClaudeCodeProvider`, the installed CLI at 2.1.272,
`--json-schema PLAN_SCHEMA`, `--no-session-persistence`, no tools, no write
permission anywhere, cwd outside every project. Model identifiers were verified
against the CLI rather than assumed: the alias `sonnet` resolves to
**`claude-sonnet-5`** and `opus` to **`claude-opus-5`**, both installed and
authenticated.

A response counts as valid only if the *production* controller would accept it.
The judge imports and runs, in order, `parse_research_plan`,
`assert_plan_says_something`, `validate_research_plan`, `to_tasks`,
`ResearchController._assert_budget_could_finish`, and the `ResearchRun`
forward-only-graph validator. Nothing was re-implemented and no success
criterion was invented after seeing a result. Before a single call was spent the
judge was checked against archived evidence: all five real accepted pilot plans
pass it, a placeholder plan and `error_max_structured_output_retries` classify
as deterministic failures, and a discretionary checkpoint under
`scientific_only` classifies as a *correct refusal* — so no correct refusal can
ever be scored as a reliability failure or trigger an escalation.

Three policies. **A** is the shipped default plus the controller's existing one
bounded re-ask at the same model. **B** is the same controller policy with
`opus`. **C** is the default once, then `opus` — and only after a provider
structured-output failure or a production degenerate-plan rejection; after any
other refusal C re-asks at the same model, because escalating past a budget,
command-policy or scientific-checkpoint refusal is not a reliability measure.

A and C make the same first call — same model, same frozen prompt, same schema,
same adapter. It is one random draw, so it was drawn once and scored for both;
likewise C's second attempt where C does not escalate *is* A's second attempt.
That keeps the comparison paired and the call count honest. The cap was 30 and
the run stopped on reaching it, which cost one cell: policy B's second
repetition of F5 was never measured, and the table says so.

### Results

| policy | cells | real calls | final valid | first-pass valid | structured-output failures | degenerate plans | final failures | median latency | cost / accepted plan |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A `sonnet` + one re-ask | 10 | 16 | **7/10** | 4/10 | 3 | 3 | 3 | 60 s | $0.34 |
| B `opus` + one re-ask | 9 | 10 | **9/9** | 8/9 | **0** | **0** | **0** | 61 s | **$0.32** |
| C `sonnet` → `opus` bounded | 10 | 16 | **8/10** | 4/10 | 3 | 3 | 2 | 63 s | $0.34 |

Per cell, with `S` = `claude-sonnet-5` and `O` = `claude-opus-5`:

| fixture | rep | A | B | C |
| --- | --- | --- | --- | --- |
| `F1-ccao-capsule` | 1 | S:ok | O:ok | S:ok |
| `F1-ccao-capsule` | 2 | S:ok | O:ok | S:ok |
| `F2-cupdlp-capsuleless` | 1 | S:ok | O:ok | S:ok |
| `F2-cupdlp-capsuleless` | 2 | S:struct-out → S:ok | O:ok | S:struct-out → O:ok |
| `F3-large-assessment` | 1 | S:struct-out → S:**checkpoint** | O:ok | S:struct-out → O:ok |
| `F3-large-assessment` | 2 | S:degenerate → S:ok | O:ok | S:degenerate → O:ok |
| `F4-srclayout-code` | 1 | S:ok | O:ok | S:ok |
| `F4-srclayout-code` | 2 | S:degenerate → S:ok | O:ok | S:degenerate → O:ok |
| `F5-experiment-write` | 1 | S:checkpoint → S:**struct-out** | O:checkpoint → O:ok | S:checkpoint → S:**struct-out** |
| `F5-experiment-write` | 2 | S:checkpoint → S:**degenerate** | *not measured (cap)* | S:checkpoint → S:**degenerate** |

Three things in that table decided the release.

**Every structured-output exhaustion and every placeholder plan came from the
smaller model.** Six failures of those two kinds, six from `claude-sonnet-5`,
zero from `claude-opus-5`. The degenerate plans were literally the shape §4 and
§25 describe: `{"summary": "test"}` and a task titled `"test"`, arriving after a
rich prompt the model had every fact it needed to answer. The guard caught all
three, which is the guard working and not the planner working.

**The stronger model needed fewer calls, not more.** Sixteen against ten,
because the smaller model's failures are paid for twice — once in the wasted
attempt and once in the re-ask. It was also cheaper *per accepted plan* and no
slower: 61 s against 60 s median, and 47,818 output tokens against 102,514,
since a structured-output retry loop bills for every attempt it abandons.

**Only the stronger model had no systematic failure on a project mode.** Both
`sonnet` policies lost `F5-experiment-write` in both repetitions — the mode with
an experiment and a write task, which is the most authority a plan can ask for.
That is criterion 3 of the selection rule, and it is the one that is not about
averages.

The `checkpoint` refusals are worth naming precisely, because they are *not* a
planner reliability failure and were deliberately not scored as one. In three
cells the planner labelled a checkpoint `claim_acceptance` or `human_review` in
a project holding no Claim, and `eligibility_failure` refused it — the
scientific-authority corroboration added in this release, doing exactly its job
on live output from both models. `claude-opus-5` recovered from it on the one
bounded correction; `claude-sonnet-5` did not, in either repetition, failing
instead into a structured-output exhaustion and a placeholder plan.

### The decision, by the stated rule

Reliability first: B (9/9) > C (8/10) > A (7/10). First-pass: B (8/9) > A = C
(4/10). No systematic mode failure: B only. Total calls: B (10) < A = C (16).
B wins the first four criteria outright and loses only the last one,
"lower-capability-model usage" — which is ranked last precisely so that it
cannot outvote the four above it.

The missing cell does not change this. Had B also failed `F5` rep 2, B would be
9/10, still strictly ahead of C's 8/10 and A's 7/10 and still on the fewest
calls. The decision is robust to the one thing the cap prevented measuring.

Bounded escalation is therefore *not* implemented. It recovered two of A's three
losses and still spent sixteen calls, so against B it buys routing code, a
wasted attempt and a re-ask latency in exchange for less reliability than simply
asking the stronger model first. §8's own instruction — prefer the simplest
policy among those of equivalent reliability — points the same way when the
simplest policy is also the most reliable one. No router, no trigger table, no
new failure semantics.

### What changed

One value, in the one place that is authoritative:

```
src/research_os/automation/config.py  _default_roles()["planner"].model
    "sonnet" -> "opus"
```

`docs/AUTOMATION_MVP.md` now shows the shipped default and says why. Nothing
else in production changed: no new field, no routing, no change to
`PLAN_SCHEMA`, to the placeholder guard, to `_planned`'s two-attempt bound, or
to how a call is charged. The planner role is read by four workers — the
research planner, the automation planner, the assessment worker and the proposal
worker — and all four now default to the stronger model, which is the same
argument applied to the same kind of output.

Two properties preserved on purpose. `planner:` in `automation.yaml` still wins
outright, and a planner model this machine cannot reach fails the run with the
provider's own error rather than being answered by whatever else is installed —
`_resolved_model` records the model that *answered*, not the alias requested, so
a substitution cannot hide in the ledger.

### The tests, and what they are really for

`tests/test_planner_model_policy.py`, sixteen tests. Three pin the default and
that changing it did not widen the planner's authority — still read-only, still
`CONTEXT_ONLY`, still no tools. Two pin the researcher's override. Three pin
honesty about which model answered. Eight pin the thing that matters most: **no
rejection of any kind escalates the planner.** The two failure classes an
escalating router would have reacted to are tested by name, and so is the one
the benchmark hit most often — a checkpoint refused by the scientific-authority
guard, where reaching for a stronger model because a scientific boundary was
inconvenient is the move this system must never make.

Five mutations, all caught:

| mutation | caught by |
| --- | --- |
| M-R1 escalate to `opus` after a rejected plan | 4 tests |
| M-R2 escalate to `opus` after a provider failure | 3 tests |
| M-R3 allow a third planner attempt | 2 tests |
| M-R4 revert the default to `sonnet` | 3 tests |
| M-R5 echo the requested alias into the ledger | 1 test |

M-R1 and M-R2 are the interesting pair: they are the two halves of the policy
that was *considered and rejected*, so the suite would notice it arriving by
accident as readily as it would notice it arriving on purpose.

### Revalidation under the actual default

Both pilots below were started with **no `automation.yaml` at all** —
`load_config(None)` returns `source: None`, `explicit_roles: frozenset()` — so
the planner they used is the shipped default and nothing else. No CLI flag
overrides a role model, and none was available to use.

**Pilot 1 — capsule-aware, `RR-20260915T052122Z-fc9cf373`.** The registered CCAO
project, `--max-write-tasks 0 --max-experiments 0 --checkpoint-policy
scientific-only`, goal: assess the current Gate E.4 sensitivity evidence and
produce the single highest-value grounded next research action without changing
the prespecified threshold, verdict, files or scientific state.

`READY_FOR_HUMAN`, **6 of 12 model calls**. The planner was `claude-opus-5` and
its only event is `plan_accepted`: no `plan_retry_started`, no
`plan_correction_started`, accepted on the first attempt. Three tasks —
literature, analysis, proposal — all `done`. Zero checkpoints of any kind, so no
discretionary stop and no hard one either. `write_tasks_used` 0,
`experiments_used` 0. The proposal spent its one bounded grounding correction
(`INV-0002` refused, `INV-0003` accepted) and produced ten items grounded on
twenty capsule identifiers including `CLAIM-0001` and `REV-0001` plus literature
DOIs; the proposal's own assessment ran on `claude-sonnet-5` and the delegated
analysis worker on `claude-sonnet-5`, both unchanged by this release. The
actionable result names the one asymmetry in the gate — the curvature-ratio arm
passing with a minimum margin of 4.255 against `R_accept_min` 1.20 while the
penalty-contribution arm fails in exactly one cell of forty-five, fold 1 at
rho=100, M = 0.011803871804554139 against `M_accept_max` 0.01 — and asks for a
read-only enumeration of the acceptance cells before anything else.

**Pilot 2 — capsule-less, `RR-20260915T054626Z-88ff0ab7`.** cuPDLP.jl at
`05ee41f4`, same three constraints, goal: assess the repository and produce one
grounded highest-value next technical or research action without modifying the
repository.

`READY_FOR_HUMAN`, **5 of 12 model calls**. Planner `claude-opus-5`, again
`plan_accepted` on the first attempt with no retry and no correction. Four tasks
— literature, two analyses, proposal — all `done`. `capsule_present: False`,
`project_id: None`, and the proposal dispatched to the assessment worker:
`mode: repository_assessment`, a `TechnicalAssessment` with twelve observations
grounded on **fourteen repository files at `05ee41f4`** and twenty literature
DOIs, and `capsule_ids: []` — the fail-closed emptiness holding, with no
scientific identifier invented or required. It spent its one bounded grounding
correction (`INV-0001` refused, `INV-0002` accepted). Its `open_question`
carries `blocked_by_evidence: true`, which §14 of this session's brief names as
an acceptable grounded outcome: the recommended action is to build the
measurement floor — record the exact invocation surface, capture three exit
statuses verbatim, then add a load test and one end-to-end solve with an
asserted termination status.

**Neither project was touched.** `HEAD`, the `HEAD^{tree}` object, `git status
--porcelain`, the full `git ls-files -s` index digest and — for CCAO — the
digest over all twenty-four `.research/` files were captured before each run and
recompared after. Byte-identical in every field, and the CCAO project was also
recompared mid-run while the proposal worker was live.

| | Pilot 1 (capsule) | Pilot 2 (capsule-less) |
| --- | --- | --- |
| planner model | `claude-opus-5` | `claude-opus-5` |
| planner attempts | 1 (accepted first pass) | 1 (accepted first pass) |
| model calls | 6 / 12 | 5 / 12 |
| final state | `READY_FOR_HUMAN` | `READY_FOR_HUMAN` |
| checkpoints | none | none |
| mode | `scientific_project` | `repository_assessment` |
| grounding | 20 capsule ids + literature | 14 repository files + 20 DOIs, `capsule_ids: []` |
| write tasks / experiments | 0 / 0 | 0 / 0 |
| project mutation | none | none |

No pilot was rerun. Neither needed the one rerun the acceptance gate allows.

### Large-context observation, measured not acted on

Re-measured on the same repository this release's Pilot C used:

| | value |
| --- | --- |
| tracked files (`git ls-files`) | 5,402 |
| citable paths enumerated in the assessment prompt | 1,200 (cut at `MAX_CITABLE_FILES`, and the prompt says it was cut) |
| paths the repository context carries into the planner prompt | 200 |
| largest archived assessment prompt | **113,648 characters** |
| planner prompt for the same repository | 31,398 characters |
| provider/model outcome on the large prompt | `claude-opus-5`, succeeded, valid grounded assessment |

§30 recorded 101,704 characters for this repository; 113,648 is the largest
archived prompt for it and supersedes that figure as the measurement of record.
Nothing was implemented in response: no context compression, no embeddings, no
lexical ranking, no hierarchical selection. This stays a post-release
operational metric, and only repeated real-use evidence should move it.

### Validation results, final candidate

Figures below are the final ones, after the delta-review repairs described two
subsections down; the planner-default commit on its own was 2,602.

| gate | result |
| --- | --- |
| `uv run pytest -q` | **2,605 passed** (2,586 at the previous candidate; +19 new, none removed, disabled or skipped) |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean (199 files) |
| `git diff --check` | clean |
| R0 scientific integrity | 490 passed |
| security regressions | 580 passed |
| prompt/data boundaries | 191 passed |
| command policy | 52 passed |
| ProjectProfile | 34 passed |
| TechnicalAssessment | 45 passed |
| validation profiles | 36 passed |
| checkpoint policy | 33 passed |
| literature pacing | 47 passed |
| model-call budgets | 166 passed |
| planner parsing / degenerate guards | 160 passed |
| provider routing + planner model policy | 72 passed |

One correction to the record while we are here: §29's table says 2,552 for the
previous candidate and the external packet's own test table says 2,586. The
collected count at `2fa935ca` is **2,586**, so 2,552 was stale. §29 is left as
written — it is the history — and this is the number to trust.

A note on how the suite must be run, because it cost time to rediscover. Several
tests execute a bare `pytest -q` inside a real worktree, and `pytest` is only on
`PATH` under `uv run`. Invoking `.venv/bin/python -m pytest` produces 89
spurious failures at *any* commit, including an unmodified `2fa935ca`. `uv run
pytest -q` is the gate.

### Residual risks after the final candidate

- **No licence.** Unchanged, and still a human decision. Nothing is granted
  until it is made.
- **The planner default is now the most expensive model.** That is the measured
  trade and it is cheaper per accepted plan, not more expensive, but a
  researcher on a tight budget should know the lever is `planner:` in
  `automation.yaml` and that the benchmark says what they give up by pulling it:
  roughly three accepted plans in ten, concentrated on the modes that write.
- **The planner readily mislabels a checkpoint's kind.** In three of thirty
  benchmark calls — from *both* models — a checkpoint in a project with no Claim
  was labelled `claim_acceptance` or `human_review`. The eligibility rule
  refuses every one, which is the guard working; but it means a run's one
  bounded correction is sometimes spent on this rather than on a real planning
  error, and the smaller model did not recover from it in either repetition.
- **One benchmark cell was never measured.** Policy B's second repetition of the
  experiment-and-write fixture was cut by the 30-call cap. The decision is
  robust to it either way, and it is recorded rather than quietly dropped.
- **The benchmark is an operational release decision, not a statistical claim.**
  Ten cells per policy on one machine, one provider family, one day. It is
  enough to choose a default and not enough to characterise a model.
- **A possible credential in an external research repository.** A prior
  real-repository assessment flagged
  `analysis/berry_cmf_validation/logs/box_cookies.txt` in
  `soft-vertical-equity-constrained-mass-appraissal` as potentially holding
  session or token material. It was **not opened, read, copied, hashed, parsed
  or committed** by this session, and nothing about it is in this repository.
  A human must resolve it before that external repository is used again. It is
  not a Research OS defect and it caused no change here.
- **Review independence is still degraded.** Only `claude` is installed, so
  every run reports `DEGRADED_SAME_PROVIDER_FAMILY`, and so does this session's
  own delta review of the planner commit. That is the gap the external
  cross-family review exists to close.
- **Everything §30 lists that this session did not touch** still stands: the
  narrow proposal-layer correction trigger, the all-filler false positive,
  worktree isolation not being an OS sandbox.
- **10.8 GB of finished-run worktrees are still held** (measured at 11 G over 61
  worktrees), deliberately. `researchctl storage --reclaim` was **not** run, so
  the external reviewer can inspect the pilot evidence.

### The delta review of the planner commit, and the four things it found

One read-only review of `2fa935ca..b34e69a` — the planner-default commit and
nothing else — fresh context, strongest local model, no write authority, an
explicit ban on running the test suite, and an explicit ban on proposing
architecture. Five questions: routing authority, fallback triggers, budget
accounting, role model overrides, failure semantics. Plus one instruction that
earned its place: *assess the added tests adversarially and name any that would
still pass if the property in its name were broken.*

**Four of the five questions came back clean, with the reasoning shown.**
`_default_roles` is the single source and every consumer reads the resolved dict
rather than a literal (`research/controller.py:324`,
`automation/controller.py:512`, `assessment/controller.py:214`,
`proposal/controller.py:251`); the only other `RoleSetting(` naming a model is
coder-only. `setting` is bound once at `research/controller.py:324` and never
rebound inside `for correction in (False, True)`, so no path exists where the
second attempt uses a different model. `_invoke` asserts remaining budget, calls
once, charges once, and returns the updated run that both post-attempt guards
then read. And nothing anywhere keys off the *value* of the planner model:
`_assess_independence` reads only `coder` and `reviewer`, so the planner default
now equalling the coder default changes no behaviour.

**One concrete defect, in the fifth.** `ClaudeCodeProvider._resolved_model` could
record the requested alias when a different model answered, in two shapes:

- the provider bills the reply only to the auxiliary model, so the non-haiku
  filter empties the candidate list, `len(candidates) == 1` is false, and the
  function returns `"opus"`;
- the provider bills two substantive models, neither of them the one asked for,
  so `len(candidates) == 2` and the function again returns `"opus"`.

Both reproduced exactly as described. The consequence is a run record
attributing a plan to a model that never made it — pre-existing code, but this
release is what made it load-bearing, because a default naming one specific
model is the configuration where that goes wrong quietly.

The fix inverts which case falls back. Silence is now the *only* thing that
yields the alias: if `modelUsage` is absent or empty nothing was reported and
the alias is all the record can honestly carry, and otherwise the answer comes
from what was reported — the matching id where there is one, the single
substantive id where there is one, and otherwise every id the provider named,
joined with `+`. That last form reads oddly in a report precisely because
something odd happened, and it is checkable against the archived envelope beside
it. Nothing parses this field; it is display-only in `automation/report.py` and
`diagnostics.py`.

**Three of the sixteen tests were vacuous, and the review was right about all
three.** This is the useful part of the finding ledger, because each one is a
test that would have passed over a broken implementation:

> **"both are charged" was never asserted.** The bounded-attempts test checked
> only that two requests were made; because the second refusal raises, no run is
> returned and nothing read `model_calls_used`. Deleting
> `run = self._charge(store, run, 1)` outright kept it green. It now reads the
> charge off the run record the failed run left on disk.
>
> **The unreachable-model test did not test unreachability.** Its planner model
> was `"fake-planner"` and the string `"model 'opus' is not available"` was
> test-supplied decoration. Whether the installed CLI refuses an unreachable
> alias is the CLI's behaviour and not this repository's to assert, so the test
> was renamed for what it actually constrains — a provider error is not
> swallowed, is bounded at one re-ask, reaches the caller verbatim, and leaves a
> `FAILED` run with no tasks — and the honesty half was moved to where it can be
> proved.
>
> **The ledger test could not tell requested from resolved.** `FakeProvider`
> sets `resolved_model=request.model`, so `assert payload["model"] ==
> "fake-planner"` passed under the mutation `model=setting.model` — one of the
> five the commit message claimed to have killed. A provider double that reports
> a concrete id nobody asked for now distinguishes them, and the assertion is
> `!= "fake-planner"`.

**Two overstated claims in prose this session wrote**, both narrowed. The
docstring said `planner:` in `automation.yaml` "still wins" without
qualification; `resolve_roles` re-homes a role whose *provider* is missing and
drops the model with it, which is defensible — an alias is provider-specific —
but is not the configured model being honoured, so both the docstring and
`docs/AUTOMATION_MVP.md` now say so. And both claimed a run "fails with the
provider's own error rather than quietly answering from another model", which is
a claim about the installed CLI that nothing here enforces; the docs now
separate the half Research OS guarantees from the half it cannot.

**One arithmetic complaint, upheld.** The docstring table said "thirty real
planner calls" beside a calls column summing to 42, and set a `9/9` row against
two `/10` rows with no note. Both are explained above — A and C share their
first draw, and the cap left one B cell unmeasured — and the docstring now
explains them where it makes the claim, since a reader of `config.py` should not
have to find §32 to reconcile the table in front of them.

### Repairs, and the gates after them

| finding | severity | disposition |
| --- | --- | --- |
| `_resolved_model` echoes the alias when only the auxiliary model answered | MINOR | fixed, 3 regressions, 2 mutations |
| `_resolved_model` echoes the alias when two other models answered | MINOR | same fix |
| "both are charged" unasserted | MINOR | test now reads the charge off the abandoned run record |
| unreachable-model test tests something else | MINOR | renamed and re-scoped to what is enforceable |
| ledger test cannot distinguish requested from resolved | MINOR | provider double reporting a different id |
| docstring table does not reconcile | MINOR | shared-draw and missing-cell notes added |
| `automation.yaml` override claim unqualified | INFORMATIONAL | narrowed in docstring and docs |
| unreachable-model claim not enforceable here | INFORMATIONAL | split into the half that is |
| provider substitution drops a configured model | INFORMATIONAL | documented; behaviour unchanged, pre-existing and defensible |
| automation planner has no re-ask to escalate | INFORMATIONAL | test docstring scoped |
| fixture and helper duplication | INFORMATIONAL | `research_home` dropped for conftest's `automation_home` |

Four more mutations against the repairs, all caught:

| mutation | caught by |
| --- | --- |
| M-R6 delete the planner's `_charge` call | 5 tests (0 before the repair) |
| M-R7 ledger the configured role model instead of what answered | 1 test (0 before the repair) |
| M-R8 revert `_resolved_model` to echoing the alias | 2 tests |
| M-R9 read an empty `modelUsage` as an unnamed model | 1 test |

M-R6 and M-R7 are the two the review predicted would survive, and they did: both
were green against the original sixteen tests and both fail now.

Gates after the repairs: **`uv run pytest -q` 2,605 passed** (2,586 at the
previous candidate; +19, none removed, disabled or skipped), `ruff check`,
`ruff format --check` and `git diff --check` clean, and every targeted suite
re-run — R0 490, security 580, prompt/data 191, command policy 52,
ProjectProfile 34, TechnicalAssessment 45, validation profiles 36, checkpoint
policy 33, literature pacing 47, model-call budgets 166, planner guards 160,
provider routing and planner policy 72.

The review was not repeated. Its findings were a provider-honesty defect and a
set of tests that did not test what they said, both repaired and both
mutation-proved; nothing it found touched planner routing, budget accounting or
the release decision, and running a third same-family pass would substitute for
the cross-family review rather than prepare it.

**Independence: `DEGRADED_SAME_PROVIDER_FAMILY`.** Same family, different model,
frozen diff. It found a real defect and three vacuous tests, and it is still not
an independent review.
