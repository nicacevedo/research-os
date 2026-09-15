# Changelog

All notable changes to Research OS. Dates are release dates.

## [Unreleased] — integration/autonomous-runtime-vnext

**Not released.** Not merged, not tagged. The package version is deliberately
not advanced here. This entry converges two lines that were developed in
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
