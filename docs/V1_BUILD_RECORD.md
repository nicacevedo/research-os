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
