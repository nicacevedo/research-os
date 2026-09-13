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
