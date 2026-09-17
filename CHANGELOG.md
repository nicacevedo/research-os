# Changelog

All notable changes to Research OS. Dates are release dates.

## [Unreleased] — rc/thesis-pilot

**Not released.** Branched from `integration/autonomous-runtime-vnext` at
`b6109c1`. This entry closes the four correctness gaps
`docs/RELEASE_CANDIDATE_REPORT.md` §J left open, and records one defect that
closing the first of them exposed.

### Fixed

- **One project, one acceptance profile, on every path.** v1.1 gave a project
  `projects.<id>.check_profiles` and one caller honoured it: `researchctl
  research run` resolved a task's named checks against them, while anything
  reaching `AutomationController` without a plan — which is every autonomous
  coding cycle — ran acceptance commands the automation planner had written.
  Closed in `_accept_plan`, the one place a plan becomes work orders however it
  arrived. A project that *explicitly* declares check profiles has them as the
  gate; a plan whose commands are already a subset of the declared argv stands,
  which preserves the research layer's deliberate narrowing. Discovery is
  unchanged: it is a guess at an unconfigured project and has no business
  overruling a planner that narrowed a command to the change it made. The
  project id is read from the capsule by path (`resolve_project_from_path`),
  because a path is the only thing every caller has.

- **The runtime's coding action could never succeed.**
  `canonical_fingerprint` hashed `git show-ref` into one opaque `<git-refs>`
  entry and compared it before and after the pipeline — and worktree isolation
  creates a branch in the canonical repository, because that is what worktree
  isolation *is*. So every honest coding run ended with a ref the guard had not
  seen and was failed as `POLICY_REFUSED`, with the branch, the diff and a
  passing review all on disk. It survived four adversarial reviews because the
  only test of that handler substituted a controller that creates no worktree.
  The fingerprint is now one entry per ref, which is strictly stronger — a
  refusal names the ref that moved — and makes the narrow exemption
  expressible: refs under `refs/heads/automation/<reserved run id>/`, a
  namespace named before the pipeline starts and recomputable by the attempt
  that adopts a crashed predecessor's branch. A new branch outside it, a moved
  ref, a deleted one and a capsule write all still fail.
  `tests/test_runtime_coding_pipeline.py` is the coverage whose absence was the
  defect.

- **A researcher can decline a proposal.** `ProposalStore` recorded promotions
  and nothing else, so "I read this and I do not want it" was the same state as
  "nobody has opened this yet" — and that is the question the cross-cycle
  deduplication asks. A declined proposal stayed pending forever, so the
  runtime kept offering it as the answer to every cycle with the same grounding
  and never proposed about those findings again. `DeclineRecord`,
  `declines.jsonl`, `decided_item_ids()`, and `researchctl propose decline`,
  which refuses a non-interactive terminal exactly as `promote` does and
  requires a reason. `test_no_runtime_module_declines_a_proposal` asserts
  structurally that nothing under `research_os.runtime` can reach the writer:
  a runtime able to close its own unanswered proposals could report an empty
  queue it produced by refusing itself.

- **Pruning a run no longer destroys the record of what it caused**
  (schema 0015). `artifact_links`, `tool_invocations` and `model_calls` all
  cascaded from `research_runs`. The consequential one was `artifact_links`:
  the preregistration guard reached the project *through the run*, so deleting
  a run removed the only link naming its preregistration and the guard then
  refused that experiment permanently, with the document intact in the
  content-addressed store. Each of the three now carries its own `project_id`,
  backfilled from the run and derived inside each insert so no call site has to
  remember it, with a cascading foreign key so deleting a *project* still
  erases everything. `run_id` keeps its value rather than becoming null:
  `artifact_links_identity_idx` is unique over a `coalesce(run_id, '')`, and
  content addressing makes two runs storing identical bytes under one role
  ordinary, so nulling it would collapse two rows onto one identity and
  PostgreSQL would refuse the second delete. A prune that cannot run is worse
  than a label that outlives its row.

- **The delegated cost cap bites before the money is spent.**
  `research_os.runtime.spend` wraps the provider adapters, which is the one
  chokepoint both delegated controllers already pass through, so each call
  reserves a MODEL_CALLS unit and a per-call MODEL_COST_USD ceiling *before*
  the provider is asked and settles at the reported cost afterwards. A refused
  reservation raises `BudgetExceededError` — an `AutomationError`, so both
  controllers already fail the run on it terminally — before the call happens.
  The previous release's `charge_all` recorded the spend afterwards, past the
  limit when it must, which made the ledger true and was not a budget.
  `charge_delegated_spend` keeps the provenance rows it alone can write and
  becomes a reconciliation for calls the authority did not see, charging the
  difference rather than the total. The residual is stated rather than hidden:
  no provider quotes a price before it bills, so one call can exceed its
  ceiling; the ceiling then ratchets to the largest observed cost, bounding the
  excess by one call instead of repeating it.

### Fixed, after an independent adversarial review of this branch

Three HIGH findings, four MEDIUM. The first is a regression this branch
introduced and is the most serious thing in it.

- **A cheap first objective bricked the project.** Deriving the standing
  project cost ceiling from the first objective's `--max-cost-usd` made
  `runtime start --max-cost-usd 0.50` write a 6 USD *lifetime* ceiling; the
  ceiling is created once, `spent` never resets, and nothing could raise it, so
  every later objective on that project failed `BUDGET_EXHAUSTED`, terminally.
  The safest command a researcher could type was the one that bricked their
  project. The ceiling now comes from the configuration, is raised and never
  lowered, and `researchctl runtime budget` exists so it can be changed at all.
- **The ref exemption hid moves, not just creations.** `git update-ref`
  repoints a branch that is checked out in another worktree -- `push` and
  `branch -f` refuse this; `update-ref` does not -- so an acceptance command
  could leave the branch the human is told to inspect pointing at a commit the
  reviewer never saw. Exempt refs are recorded rather than dropped, and
  `branch_drift` cross-checks each order's branch against the head commit the
  controller recorded. The second is what closes it: a create and a
  create-then-move give the same two snapshots.
- **HEAD was outside the fingerprint.** `show-ref` does not list it and
  `--head` lists it by resolved sha, so repointing HEAD at another branch at
  the same commit was invisible while moving the canonical checkout onto a
  branch the guard would never inspect again. `git symbolic-ref -q HEAD` is now
  included.
- **The subset exemption let a planner skip a declared check.** A project
  declaring `tests` and `lint`, and a planner emitting only the `tests` argv,
  satisfied "a subset stands" -- so lint never ran and nothing recorded it. The
  exemption now applies only to controller-supplied plans, which is the
  distinction `_plan` already knew and was discarding.
- **The capsule blind spot was a substring match.** `.research/claims/runtime/`
  was invisible to the fingerprint, loaded by the capsule scanner as a real
  scientific object, and not gitignored. Exact top-level match now.
- **The planner call was uncharged when `start` raised.** `controller.start`
  makes the planner call and can raise, and the charge only wrapped `execute` --
  the same hole fixed for `execute` in the previous release. `start` is inside
  the `try` now.
- **The decline guard was a lint.** `getattr(store, "record_" + "decline")`
  defeats an AST scan for the call, and the runtime already holds a live
  `ProposalStore`. The interactive-terminal check moved into
  `ProposalStore.record_decline`, so it is enforced whatever the caller is
  called; the AST scan is kept as defence in depth and now bans the name in any
  string.

Six findings are recorded and not fixed, with reasons, in `docs/RUNTIME.md`
§15b. The one worth naming here: **a decline can be forged by appending a line
to `declines.jsonl`**, which lives outside every repository and is covered by
no fingerprint. Closed under `SandboxMode.REQUIRED`; open otherwise; the fix is
tamper-evidence over the whole proposal ledger and should be designed for
promotions and declines together.

### Fixed, found by driving the loop on this project's real research

- **A refused action was planned again by the next cycle.** Each cycle opens a
  new planner thread seeded only with project identity, so a successor had no
  way to know what its predecessor had tried. Observed on a real objective: the
  planner chose `run_local_experiment`, the action correctly refused with
  `no preregistered design to run; design_experiment must come first`, and the
  successor planned the identical action. The guard was right and the feedback
  path was missing — §16's "seven cycles, one cycle's worth of information" in
  a new form.

  `graphs/cycle.py::_previous_attempt` now reads the parent run's last refused
  `tool_invocation` and passes `"<action> was REFUSED: <detail>"` to the planner
  as a new `previous_attempt` field; `prompts.py` gains the instruction
  paragraph and the planner prompt goes to **version 3**. Verified on the same
  objective under `planner@3`: it did not repeat the refused action, and
  concluded `WAIT_HUMAN` — which is correct, because the design it needed was
  the thing a human had to decide.

### Changed

- `runtime doctor`'s check-profile row was a divergence warning, then briefly
  an overclaim ("every path resolves the same set"), and is now a report of
  the resolved commands per project and of which plans keep their own subset.
- `researchctl runtime budget <project> [--max-cost-usd X] [--force]` shows or
  sets a project's standing ceiling. It refuses to set one at or below what has
  already been spent unless forced, because that stops all further work
  immediately.
- `RUNTIME_SCHEMA_VERSION` is `0015`.

### Tests

`3521 passed, 18 skipped` before; `3580 passed, 18 skipped` after, with the
runtime suites green in both orders (519 each way). No existing test was
weakened; two were updated to the stronger assertion the fix makes available.

## [Unreleased] — integration/autonomous-runtime-vnext

**Not released.** Not merged, not tagged. The package version is deliberately
not advanced here. The release-candidate closure report is
`docs/RELEASE_CANDIDATE_REPORT.md`, and its verdict is
`AUTONOMOUS_RUNTIME_BETA` rather than release candidate: the loop is closed and
demonstrated on real work, and three production-hardening axes -- OS
containment, Slurm, a second provider family -- cannot be validated on this
host at all. §I of that report says what would move it. This entry converges two lines that were developed in
parallel on top of v1.0.0 and had never met: the v1.1 autonomy release
candidate (`release/v1.1.0-autonomy`) and the R5 autonomous runtime
(`r5/autonomous-runtime`). Both sets of changes are below, unedited except for
heading depth, followed by what the convergence itself changed.

### What the convergence itself changed

The two lines above were developed in parallel and had never met. Merging them
was the smaller half of this work; the larger half was closing the loop R5
shipped without, and repairing what a semantic audit of the merged tree found.

#### Added

- **Runtime findings** (`research_os/runtime/findings.py`, schema 0007). Typed,
  digested, immutable, noncanonical observations with an identifier a proposal
  can cite. The v1 grounding allowlist has always had a `finding_ids` field and
  always refused a citation it was not given — and nothing ever supplied one, so
  an autonomous result reached the proposal layer as prose inside a
  natural-language goal. A proposed change now traces to a finding, to the
  artifacts, capsule objects, literature keys or experiment job it rests on, and
  to bytes by content hash.
- **`propose_capsule_change`.** A thin adapter over the v1 `ProposalController`,
  not a second proposal engine. It writes no capsule file, does not import
  `research_os.proposal.promote`, authors no Review and accepts no Claim;
  `tests/test_runtime_authority.py` asserts each of those by parsing the
  package. A cycle that produces one concludes
  `WAITING_FOR_SCIENTIFIC_DECISION` rather than `DONE_FOR_NOW`, because
  "finished" is the wrong word for "waiting for you".
- **`nominate_insight`.** Reuses the v1 insight subsystem and leaves `scope`,
  `assumptions` and `applicability` empty — those three fields *are* the
  judgement that a finding transfers, so `missing_for_promotion` tells the
  researcher what they must write. "Not worth nominating" is an expressible and
  common answer. Every action in the policy table now has a handler or is one a
  person performs.
- **Automatic continuation after a human scientific change** (schema 0008).
  `researchd` hashes each project's canonical capsule, emits exactly one
  `CAPSULE_CHANGED` per `(project, digest)`, and opens a **successor cycle**
  with recorded lineage — never a revival of the parked thread. The scientific
  kernel is unchanged and notifies nobody: a kernel that depended on PostgreSQL
  and a live daemon would be one a researcher could not use while either was
  down.
- **Durable experiment-interpretation identity** (schema 0006).
  `interpret_results` used to read whichever job finished most recently.
  Interpretations are now unique per `(job, interpreter version)`, selected
  oldest-eligible-first, bound to the exact `spec_digest`, and crash-safe: a
  process killed between writing the artifact and completing the claim
  reconnects the same artifact rather than producing a second scientific
  interpretation.
- **Stale-basis protection** (`research_os/proposal/basis.py`). A proposal
  records what it actually cited — project identity, each referenced object's
  status and project-scoped semantic digest, schema versions, the charter when
  used, the finding-packet digest — and promotion recomputes it. Deliberately
  *not* the repository `HEAD`: failing a proposal because someone fixed a README
  typo would teach a researcher to click past the warning.
- **OS-level containment** (`research_os/sandbox.py`). One abstraction, a
  bubblewrap backend, three modes (`required` / `preferred` / `off`), and
  deny-by-default: no home, no SSH keys, no SSH agent, no Git credentials, no
  provider credentials, no unrelated environment, no network. Both command
  runners go through it, so `researchctl auto` gains it too. High-autonomy
  runtime execution of model-written code overrides the configured mode to
  `required`.
- **`review_independence: require`.** A `CRITICAL` review that cannot obtain a
  different provider family fails into `WAITING_FOR_EXTERNAL_DEPENDENCY`
  instead of being recorded as degraded. `prefer` remains the default.
- **`researchctl runtime findings`**, and findings and interpretations in
  `runtime run <id>`. The findings view prints what a finding is *not*, every
  time: a table of scientific-sounding statements with identifiers is exactly
  what a reader might mistake for a project's record.
- **A live-Slurm harness** behind `-m slurm_live`. Written, never executed —
  there is no `sbatch` on this host.

#### Fixed

Found by a semantic audit of the merged tree, which is where these live: each is
a place where R5 called into a v1 layer that v1.1 had changed, and no test on
either line exercised the combination.

- **A paced literature provider became a permanent empty review.** v1.1 added a
  persistent pacer that reports a refused reservation as `RATE_LIMITED` with
  `attempted=False` — "we did not ask", which its own docstring insists is a
  different fact from "there is nothing". The runtime turned it into an empty
  result *and recorded it in the idempotency ledger as COMPLETED*, so every
  later cycle in that run short-circuited on the same key and never asked that
  provider again. It now raises, the ledger records `FAILED`, and the queue
  retries after the rate-limit backoff.
- **The runtime ignored every configured model and effort.** It passed neither
  `--model` nor `--effort`, so the provider CLI's own default answered —
  including for the three runtime roles that map onto the v1 planner, whose
  default v1.1 changed from `sonnet` to `opus` on thirty measured calls
  precisely because every structured-output exhaustion and every placeholder
  plan in that benchmark came from the smaller model. Those three roles are the
  ones that issue schema-constrained requests.
- **The coding action's crash reconciler was dead code.** It read
  `plan["_reserved_run_id"]`, which nothing ever set, so it returned `None`
  unconditionally and the crash window it existed to close was open: a crash
  between the worktree being created and the ledger recording it left an orphan
  branch and a retry that started a second automation run for one task. The id
  is now reserved from the action's stable identity and the reconciler
  re-derives it.
- **A daemon-driven cycle could not run an experiment at all.** `build_context`
  defaulted its executors to `{}` and nothing in the control plane supplied
  any, so a planned `run_local_experiment` was refused with "no local executor
  is available on this machine".
- **A dead duplicate index rebuild** in `runtime/actions/inspect.py` called
  `LiteratureStore()` — whose constructor requires a connection — and would
  have raised `TypeError` past the `except ResearchOSError` meant to catch it.
  Never registered, so never reached; removed rather than repaired. The live
  implementation is in `runtime/actions/literature.py`.
- **`CapsuleError` classified as `UNKNOWN`.** `kernel.frontier()` raises it and
  the frontier is consulted at the start of every cycle, so an unreadable
  capsule failed as "something we have not classified" rather than "your capsule
  does not parse".
- **The work-kind dispatch table was duplicated**, once inside `_run_item` and
  once in a test's hand-written list, so adding a kind made an unrelated test
  fail. It is one module constant now.

#### What the first closed-loop pilot changed

Run against the real CCAO capsule with a real provider. Ten of eleven asserted
properties passed first time; the two defects it found are the kind only a real
run produces.

- **The continuation refused at the exact moment the wait had ended.**
  `should_continue`'s progress check compared the parked cycle's frontier digest
  with its *parent's*. For an ordinary continuation that is right. For an
  advance it asks "did that old cycle learn anything" — and the answer is no,
  which is precisely why it stopped and waited. The two digests are identical
  for *every* real successor, because the runtime cannot move the frontier its
  own planning is derived from. `CAPSULE_CHANGED` fired, `frontier_changed:
  True`, the advance ran, and no cycle opened. `should_continue` now takes the
  frontier as *measured by the caller*, and an unmeasurable frontier is
  explicitly not a change.
- **A failed assessor lost a good proposal, or rescued it by accident.** The
  assessor exhausted its structured-output retries after the proposal had been
  stored, because the controller stores and then assesses. The work item was
  recorded FAILED and the proposal survived only because the idempotency ledger
  consults the reconciler on its FAILED path — a mechanism built for something
  else. It is intended now, and `assessed=False` is stated in the result and in
  the detail.
- Two hardenings found while fixing those: a run that requires containment on a
  host without it is refused **at preflight**, before a worktree exists (a
  `SandboxError` raised from inside a check is not an `AutomationError`, so it
  escaped the handler that fails a run cleanly and left it EXECUTING forever);
  and `parked_objectives` selects one run per objective by a *total* order,
  because two runs can share `created_at` to the microsecond and both would
  otherwise be advanced.
- `runtime status` now shows parked objectives and the capsule-observation
  count. A parked run is `SUCCEEDED`, so it appeared nowhere under RUNNING,
  WAITING FOR YOU or FAILED — the state a researcher most needs to see was the
  one hardest to notice. The observation count answers the other question a
  stuck researcher asks: is the watcher watching?

#### What three independent adversarial audits changed

Three audits ran against this branch — authority, containment, concurrency —
after the first closed-loop pilot. Every finding they raised was about a claim
the code or its documentation made and did not support, not about a missing
feature. `docs/RUNTIME.md` §16 has the full table; the substantive repairs:

- **The citable finding ids now live outside every data fence** in the prompt
  that writes a proposal, not only in the one that corrects it. The list of ids
  a worker may cite must not be reachable from inside a block of text a model
  wrote, which is the whole reason `render_supplied_findings` renders the ids
  twice.
- **The stored `SuppliedFinding.statement` is the string the worker read.** The
  prompt clipped at 1500 characters and the record kept 4000, so a conclusion
  past the cut appeared in `propose show` and never reached the worker.
- **Grounding is checked in both directions.** A citable finding whose text the
  proposal does not carry is refused, not just a quoted finding outside the
  allowlist. The justification for the one-directional form described a caller
  that does not exist.
- **`runtime_proposal_links` distinguishes offered from cited** (schema 0011).
  The table claimed to record what a proposal rested on and recorded the whole
  packet it was shown.
- **Proposals are listed by `created_at`, not by id.** A reserved id's timestamp
  is a deliberate placeholder so a retry can recompute it, which sorted every
  runtime proposal ahead of everything the researcher wrote.
- **Choosing the Slurm executor is no longer a route around `required`
  containment.** Nothing in this process can contain a process on a compute
  node, so at `required` the Slurm executor refuses as
  `WAITING_FOR_EXTERNAL_DEPENDENCY` rather than running uncontained. Batch
  scripts also carry `#SBATCH --export=NONE`: sbatch's default copied the
  daemon's environment, including provider keys, into every job.
- **Containment no longer makes the acceptance command vanish.** `uv` lives
  outside the sandbox's read-only OS paths and its cache lives under a home the
  sandbox replaces with a tmpfs, so a contained check exited 127 and the failure
  looked like the project's.
- **Bidi, zero-width, separator and tag characters are escaped at the display
  boundary.** They are not control characters, so they reached a researcher's
  terminal intact and could make a finding read differently from what is
  archived.
- **Selecting and claiming an experiment to interpret is one transaction**
  (`for update of j skip locked`). Two workers each performed the whole reading
  and the unique constraint discarded the loser's.
- **One successor per run is a partial unique index** (schema 0014). The
  advisory lock that was supposed to serialise it was taken and released inside
  its own transaction, so it serialised nothing.
- **The preregistration lookup is an indexed equality match** (schema 0012). It
  scanned the newest 500 and reported "no preregistration found" — falsely —
  for everything past the horizon.
- **Deleting a run no longer deletes the interpretations of its experiments**
  (schema 0013). `external_jobs.run_id` was the only external-effect relation in
  the schema that cascaded.
- **Every value-list check constraint in the database must mirror a Python
  enum**, tested in both directions. Two constraints had escaped the
  forward-only proof.

#### What the second pilot found, which nothing else did

Three attempts, three real findings, none of them from a test or an audit:

- **Delegated model calls did not reach the runtime's budget ledger.**
  `runtime run` reported one model call for a cycle that made four, and a run
  started with `--max-cost-usd 6` reported a tenth of what it had spent. The
  router reserves before every call it makes and makes none of the calls inside
  a delegated action: `propose_capsule_change` hands the work to the v1
  `ProposalController` and the coding action to `AutomationController`, both of
  which own their own providers. Nothing was unbounded — each action passes a
  ceiling — and the *cost* cap did not apply to those calls at all.
  `BudgetLedger.charge_all` records a spend that already happened, past the
  limit when it must, and reports which budgets it broke; `charge_delegated_spend`
  calls it with the invocation records the controllers return, on the success
  path and the failure path both, because a failed proposal's calls cost what a
  successful one's do. `ProposalController` now attaches those records to its
  exceptions, not only their count.
- **A legitimate planner decision killed the pilot harness.** The proposal
  worker put a capsule id where a proposed item id belongs, the validator
  refused it correctly, and the script died at phase 2 with no verdict because
  "no proposal to promote" exits non-zero under `set -o pipefail`. It now
  reports and explains that outcome, re-verifies that the researcher's project
  is unchanged, and exits with that code.
- **The migration checksum guard refused this session's own edit** to `0014`,
  between phase 3 and phase 4 of a pilot whose disposable database had already
  applied the original. Working as designed, on the person who wrote the rule.
- **The cross-cycle proposal dedup had never matched anything.** It compared the
  runtime's `FindingPacket.digest` against the `finding_packet_digest` v1 stores
  in a proposal's basis snapshot, and those are different functions over
  different material, so the comparison returned nothing every time. Two
  proposals over one unchanged finding packet, caught by the pilot's own
  "exactly one logical proposal" assertion. With the comparison working, two
  more bugs surfaced at once: a promotion of one item was treated as an answer
  to the other eight, and the dedup had to be taught to exclude *this cycle's
  own* reserved id so a crashed attempt still reaches the recovery path. The
  two digests also stopped sharing a name in the run payload.
- **The project-untouched guard reported the wrong repository.**
  `fingerprint_source` read `git rev-parse HEAD` and `git status --porcelain`
  unconditionally, so for a project vendored inside another repository it
  reported the *enclosing* repository's state: a passing run was reported as
  having modified the researcher's project while every `.research` hash was
  byte-for-byte identical. Git state is now read only when the source is its own
  work-tree root, and the file hashes cover the whole project.

Not fixed, and recorded as remaining work: **a researcher cannot decline a
proposal.** `ProposalStore` records promotions and nothing else, so now that the
dedup works, a proposal somebody read and rejected stays pending forever and the
runtime will never propose about those findings again.

#### What the final adversarial review found

An independent review of the finished branch, told to attack this release's own
report. `docs/RUNTIME.md` §16 has the table; the two that matter:

- **The acceptance gate had no display boundary.** `src/research_os/cli.py`
  contained no use of `terminal_safe` at all, so `researchctl review` — the one
  command that records human acceptance of a Claim — printed model-written
  `claim.statement` raw immediately above the approve/reject prompt. A statement
  ending `\x1b[1A\x1b[2K` rewrites the line above it, so the sentence on screen
  when the reviewer answers is not the sentence being digested. Every report
  module wrapped its output; the highest-authority screen did not. Fixed, along
  with the only `ensure_ascii=False` on a display path in the tree.
- **Containment granted host code execution.** `uv_support_paths` put uv's
  cache and data directories in `writable`, so they were bound read-write. The
  managed interpreter there is what nine virtual environments on this machine
  execute, and its stdlib directory is writable by this user — so contained
  model-written code could drop a `sitecustomize.py` and the next `uv run`
  anywhere on the host would execute it outside the sandbox, seen by no
  fingerprint. Introduced earlier the same day by the fix for "containment makes
  the command vanish". The cache is now not bound at all (a read-only cache
  makes uv exit before running anything — measured) and the data directory is
  read-only; uv gets the sandbox's own tmpfs `HOME`.

Also: a failed coding run charged nothing to the budget ledger (the charge sat
after a call that re-raises); `UV_CACHE_DIR` from the environment selected an
arbitrary read-write bind; 0014's duplicate precheck was not atomic with its
index build; 0013's FK rebuild could not finish under a pooled `lock_timeout`;
`redact_dsn` printed the password in full for a libpq URI with no path
component; an experiment parameter could choose the program that runs;
`_program_binding` would bind an arbitrary host path; `DECEPTIVE_CHARS` missed
thirteen ranges including the one its own docstring describes; and
`research/report.py` left four model-written fields raw, one of them the line
stating what an autonomous task may write.

**Reported and not fixed:** `artifact_links.run_id` is part of that table's
primary key, so it cannot be nullable without a key migration, and the
preregistration guard scopes its lookup through that table — so pruning a run
still makes the guard refuse permanently. The refusal message now says the link
is unreachable rather than claiming no preregistration exists.

#### Known divergence, reported rather than closed

v1.1 moved validation-check resolution into the controller: `researchctl
research run` resolves `projects.<id>.check_profiles` and runs the argv the
*researcher* declared. The runtime's coding action dispatches through
`AutomationController` without a plan, so its acceptance commands still come
from the automation planner. Same project, same goal, two different gates. Not a
correctness or authority defect — every resolved argv passes the same command
policy and nothing merges or pushes either way — and `runtime doctor` now warns
when a project declares profiles a runtime cycle would ignore. Closing it means
threading the project profile through `AutomationController`, which is v1 work
this integration did not take on.

### From the v1.1 autonomy line

**Not released.** Not merged, not tagged. The package still reports version
`1.0.0`, deliberately: changing it would imply a release that has not happened.
This entry describes the candidate awaiting external cross-family review.

Five things a model was being asked to judge became things the controller
knows. Nothing a human decides changed.

#### Added

- **Deterministic project profiles.** Before a model is asked anything, the
  controller reads the repository and the researcher's configuration and records
  what it found — capsule or not, Python or not, lock file, `src` layout,
  declared experiment commands, resolvable checks, manuscript sources — each
  fact carrying its origin (`explicit_config`, `repository_metadata`,
  `deterministic_structure`, `unavailable`, which is never the same as "no").
  Profiling executes no repository code, reads tracked files rather than the
  working tree, carries no timestamp, and cannot be influenced by model output.
  Explicit configuration always beats discovery.
- **Capsule-less `TechnicalAssessment`.** A project with no `.research/` now
  reasons in `repository_assessment` mode over tracked files at the base commit,
  symbols, deterministic check ids and retrieved works, and produces an
  assessment rather than a scientific proposal. It is not science, has no
  promotion path, is never written under `.research/`, and never enters the
  repository. Scientific identifiers fail closed: the field that could carry one
  is validated against an allowlist the controller leaves empty in that mode.
  The controller chooses the mode from the profile; a worker cannot.
- **Controller-owned validation profiles.** A plan selects `required_checks:
  ["tests", "lint"]` and the controller resolves each id to argv it already
  knows. For a `pyproject.toml` beside a `uv.lock` that is `uv run pytest -q`,
  which is what actually imports a `src`-layout project. Explicit
  `projects.<id>.check_profiles` in `automation.yaml` replaces discovery
  entirely. Every resolved argv passes the same command policy planner-authored
  commands face, so a profile cannot introduce a forbidden program.
- **`--checkpoint-policy scientific-only`.** Checkpoints carry a typed kind. The
  controller decides whether that kind is possible here, from what the capsule
  holds, and whether it is permitted under this run's policy. A discretionary
  checkpoint in an unattended run is refused with one deterministic reason, gets
  the existing single bounded replan, and then fails explicitly. Hard
  checkpoints — human Review, Claim acceptance, prespecified-criterion change,
  cross-project promotion, costly authorisation — are unreachable by any flag.
  Default is `standard`, which behaves exactly as v1.0.0 did.
- **Persistent literature pacing** (store schema 2). Cache first, before the
  slot; an atomic `BEGIN IMMEDIATE` reservation so two concurrent runs cannot
  both issue a request; only provider-reported information persisted; a
  `Retry-After` beyond the inline budget recorded rather than slept through.
  `lit sources` shows the persisted health beside the probe. New
  `cache_ttl_seconds` in `literature.yaml`, a day by default, `0` to ask every
  time.

#### Fixed

- A plan whose every field was structural filler, or under three characters,
  could validate and execute. A live run searched two providers for the query
  `"q"` and reported `READY_FOR_HUMAN`.
- A `Retry-After` of an hour caused two minutes of inline sleeping across
  retries; it is now recorded and the other providers are asked instead.
- An assessment that cited nothing, or pointed at an id that is not in it, could
  not reach the single bounded grounding correction and failed terminally.
- The research planner was refused for omitting a declared experiment's required
  parameter without ever having been told the parameter existed.
- A project profile could state a capability's absence with a sentence
  asserting its presence.

#### Fixed after the independent delta review

An independent read-only review of the candidate returned PASS WITH BOUNDED
REPAIR and twelve findings. All twelve were repaired before the candidate was
pushed.

- `capsule_present` accepted an on-disk `.research/` directory and never asked
  whether the capsule parsed, so it could disagree with the science context and
  put two contradictory controller-authored blocks in one prompt.
- `cross_project_promotion` was eligible in any project with a capsule, which
  made it a label that could stop an unattended run for any reason at all;
  `costly_authorization` was eligible in runs that could spend nothing.
- Check discovery offered `uv run pytest` to projects declaring no pytest,
  reintroducing the v1.0.0 `src`-layout trap through the profile.
- A truncated tracked-file list was reported as a deterministic absence rather
  than as unavailable.
- A cache-served retrieval wrote no `SearchRecord`; `network_calls` counted a
  real 429 as though no request had been made; a long `Retry-After` on a 5xx
  was not persisted; a successful search erased a recorded quota reset.
- The pacer leaked raw `sqlite3` errors, and a failed `COMMIT` left the
  transaction open under a restored isolation level.
- The placeholder guard, over-corrected earlier in this release, wrongly refused
  ordinary fields such as "Query the data" and "Results summary".
- The budget entry gate stated a requirement that is false for a capsule-less
  proposal task; a symbol could not be written `solve()`.

#### Changed

- **The shipped default planner model is now `opus`, was `sonnet`.** Decided by
  measurement, not principle: thirty real planner calls over five archived
  fixtures from this release's own validation campaign, each rebuilt through the
  production prompt builder with the project's real profile, budgets, checkpoint
  policy, validation profiles and declared experiments, and every answer judged
  by the production validators. `sonnet` plus the existing one re-ask reached
  seven valid plans in ten and needed sixteen calls; `opus` reached nine in nine
  and needed ten. Every structured-output exhaustion and every placeholder plan
  in the benchmark came from the smaller model. `docs/V1_BUILD_RECORD.md` §32
  records the protocol and every call.

  A bounded `sonnet` → `opus` escalation was benchmarked as a third policy and
  **deliberately not implemented**: it recovered two of the three losses, still
  spent sixteen calls, and would have added routing code for less reliability
  than asking the stronger model first. There is no planner fallback in this
  release, and `tests/test_planner_model_policy.py` exists partly to notice one
  appearing.

  `planner:` in `automation.yaml` overrides this. Naming a *provider* this
  machine does not have still re-homes the role and drops the model with it,
  which is unchanged and documented.

#### Fixed after the planner delta review

- `ClaudeCodeProvider._resolved_model` could record the requested model alias
  when a different model answered — when the provider billed the reply only to
  the auxiliary model, or to two substantive models neither of which was the one
  asked for. Both shapes made a run record attribute a plan to a model that
  never made it. Silence is now the only case that falls back to the alias: if
  usage was reported at all, the record names what was reported. Pre-existing,
  and made load-bearing by the planner default naming one specific model.
- Three of the sixteen new planner tests did not constrain the property in their
  name: "both attempts are charged" asserted only the two requests, the
  unreachable-model test exercised no unreachability, and the ledger test could
  not distinguish the requested model from the resolved one because the test
  double reports them identically. All three now fail under the mutations they
  were written to catch.

#### Unchanged

R0 kernel semantics, scientific object schemas, digests, stale-review detection,
Claim acceptance, worktree isolation, argv-based execution with no shell, the
prompt/data boundary, and the one-repair bound everywhere it already applied.
Also unchanged by the planner default: `PLAN_SCHEMA`, the degenerate-plan guard,
the planner's two-attempt bound, and how a model call is charged.

### From the R5 autonomous runtime

On `r5/autonomous-runtime`. The scientific kernel is unchanged; this is the
durable operational layer around it. `docs/RUNTIME.md` is the specification and
`docs/R5_BUILD_RECORD.md` the build record.

#### Added

- **An operational database, and the boundary that keeps it operational.**
  PostgreSQL holds runs, work items, events, approvals, invocations, model-call
  provenance, external jobs, artifact references, budgets, schedules and
  provider health. It holds no science. `research_os.runtime.kernel` is the only
  module that reads a capsule and has no method that writes one;
  `tests/test_runtime_authority.py` asserts that by parsing the package.
- **A durable work queue.** `for update skip locked`, renewable leases with
  server-computed deadlines, and reclamation when a holder stops reporting.
  Claiming charges the attempt, so work that kills its worker runs out of
  attempts rather than out of workers.
- **An idempotency ledger.** Every externally visible side effect is claimed
  durably before it happens, keyed by the action's stable identity. A retry
  reuses a completed one; an outcome nothing can establish is refused rather
  than guessed at.
- **Bounded resumable cycles.** One LangGraph thread per cycle, persistent
  PostgreSQL checkpoints, `durability="sync"`, and checkpoint retention that is
  implemented rather than aspirational. A human gate is three nodes — prepare,
  interrupt, apply — because a side effect before `interrupt()` is replayed when
  the interrupt is answered. That was measured, not read.
- **`researchd`**, a control-plane daemon whose loop is one testable function.
  It ingests events, claims due work, renews and reclaims leases, polls external
  jobs, enforces budgets, tracks provider health and surfaces approvals. It
  calls no model.
- **Provider routing with honest independence.** A graph node asks for a
  capability and a criticality, never a vendor. Critical work is never silently
  downgraded. Independence is *reported*, so a review that had to run on the
  producer's own family is recorded as degraded rather than claimed as
  independent.
- **`researchctl runtime`** — status, runs, run, approvals, approve, decline,
  jobs, costs, events, cancel, doctor, migrate, daemon, dev-db — each with a
  deterministic `--json` counterpart.
- **A disposable local PostgreSQL** (`runtime dev-db`) for a machine with no
  system service, no container runtime and no root.
- **`deploy/researchd.service`**, a systemd *user* unit, shipped and never
  installed.
- **`pilots/run_pilot.sh`**, one command against a real project, which hashes
  the project before and after and fails if it moved.

#### Changed

- `ARCHITECTURE.md` §12: PostgreSQL, LangGraph and a background service left the
  postponed list, each with the requirement that forced it recorded in §12a.
  `DESIGN_INVARIANTS.md` gains a change-control record; no invariant changed.
- Three new fences in `automation/promptdata.FENCES` for frontier state,
  hypothesis proposals and experiment results.

#### Fixed

Two adversarial reviews and a real pilot found these; `docs/RUNTIME.md` §16
records them in full, including one reported finding that was wrong and one
suggested fix that was wrong.

- The attempt cap was defeated by the recovery code meant to honour it.
- The invocation ledger's `FAILED` path deleted its row without consulting the
  reconciler — but `perform` routinely raises *after* the effect lands, so this
  submitted the same experiment twice.
- Events were consumed and enqueued in separate transactions, and nothing
  re-emits them, so a crash between lost a run permanently.
- Nothing serialised two workers entering one LangGraph thread.
- Every lock was taken with an unbounded wait, so one stuck holder froze the
  control plane and every contention handler was unreachable.
- The preregistration guard was skipped by omitting the digest, and the
  experimentalist authored its own `argv`. It now selects a command the
  *researcher* declared.
- `runtime approve` had no interactive guard and recorded a fabricated actor.
- Continuation assumed progress. A real pilot chained seven cycles over an
  identical frontier; it now hashes the frontier and stops.

## [1.0.0] — 2026-09-13

First stable operational release. The architecture of `research-os-v1` plus the
hardening required to run it unattended.

### Added

- **Bounded grounding correction.** A structured proposal that fails
  deterministic reference validation gets exactly one correction attempt, with
  the same evidence packet, the same goal, and no new authority. A second
  invalid answer fails closed. The correction is charged against the model
  budget and is checked for affordability before it is spent; the refused
  proposal is kept beside the corrected one rather than overwritten.
- **Crash-consistent inner-run dispatch.** A research run reserves the
  identity of the automation run it is about to start, and writes it to its own
  ledger, before the child run directory can exist. A process killed anywhere
  inside `start` now leaves a child this run can still name.
  `ResearchController.reserved_dispatches` reports them.
- **Crash-consistent experiment worktrees.** An experiment's store record is
  written in a new `PREPARING` state *before* its Git worktree is created, so a
  worktree can no longer exist without an owner. Failure during preparation
  records a failed run rather than a phantom active one.
- **Transport retries for literature sources.** A request that never completed —
  a timeout, a reset, a DNS failure — is retried on the same bounded terms as a
  503, instead of aborting the whole source on one blip. `Retry-After` is now
  honoured in both its numeric and its HTTP-date forms.
- **Hermetic CI** on GitHub Actions: tests, lint and format, with no provider
  credentials and no network.
- `CONTRIBUTING.md`, this changelog, and a documented security trust boundary.

### Fixed

- `_worktree_run_id` read the clock for its timestamp, so two calls a second
  apart returned different ids. Harmless while it was called once; wrong as soon
  as the path had to be recorded before it was created.
- An experiment retried within the same second collided on its run id, because
  experiment ids had no attempt discriminator. Retrying promptly after a failure
  is the ordinary thing to do.
- Delegated model-call spend was reconciled only from runs that reported
  starting, so a run killed inside `start` spent budget nobody counted.
- `experiment cleanup` returned early when the worktree was absent, leaving a
  stale lock that made the path permanently unusable.
- `storage` and `storage --reclaim` could not see a worktree lock with no
  worktree beside it.

### Security

- Worktree lock removal now derives its target from the worktree path and
  re-checks the derived filename, so no cleanup or recovery path can remove a
  run lock. Removing a held `flock`'s name lets the next arrival lock a
  different inode at the same path — two writers, one run.
- Full Git history screened for credentials across every reachable blob.

### Changed

- Package version aligned to the release version, with a regression test that
  keeps `pyproject.toml` and `research_os.__version__` from drifting apart.

## [research-os-v1] — 2026-09-12

The architecture. Tagged as `research-os-v1`; that tag is historical and is not
moved.

- **R0 integrity kernel.** Git-tracked YAML and Markdown as the only scientific
  state, project-scoped semantic digests, cross-object validation, the
  review-gated Claim acceptance rule, the rebuildable project registry, and the
  human review flow.
- **Automation control plane.** Planner, isolated-write Coder, independent
  Reviewer, deterministic controller-run acceptance checks, and exactly one
  bounded repair per work item.
- **Read-only Analyst.** Snapshot-read repository analysis whose findings enter
  a downstream prompt as fenced data.
- **Literature intelligence.** OpenAlex, Crossref and arXiv adapters, content
  identity and merging, a local index, and a read-only literature analyst. A
  provider that could not be reached is never reported as a provider that found
  nothing.
- **Scientific proposals.** Structured hypotheses, experiments and open
  questions, grounded only in what the run actually had, assessed by a
  context-only reviewer, and promotable into a project only by a human and only
  as a draft.
- **Experiments and HPC.** Declared commands with typed parameters, local and
  Slurm execution, isolated worktrees, declared outputs, and content-addressed
  evidence packets.
- **Cross-project insights.** Explicit, human-authorised transfer of findings
  between projects. Never silent promotion.
- **Evidence-grounded writing.** Manuscript drafting that may only rest on the
  project's own accepted scientific objects.
- **Unified research runs.** One goal dispatched across all of the above, with
  budgets checked before every spend, ending at `READY_FOR_HUMAN`.
- **Known boundary.** Trusted local repositories. Worktree isolation is not an
  OS sandbox, and project checks execute project code with the user's
  permissions.

## [research-mvp-v0.1] — 2026-09-10

Planner–executor–reviewer loop, isolated worktrees, prompt/data boundary,
argv-based command execution, bounded repair.
