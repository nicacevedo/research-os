# Changelog

All notable changes to Research OS. Dates are release dates.

## [Unreleased] — R5 autonomous runtime

On `r5/autonomous-runtime`. The scientific kernel is unchanged; this is the
durable operational layer around it. `docs/RUNTIME.md` is the specification and
`docs/R5_BUILD_RECORD.md` the build record.

### Added

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

### Changed

- `ARCHITECTURE.md` §12: PostgreSQL, LangGraph and a background service left the
  postponed list, each with the requirement that forced it recorded in §12a.
  `DESIGN_INVARIANTS.md` gains a change-control record; no invariant changed.
- Three new fences in `automation/promptdata.FENCES` for frontier state,
  hypothesis proposals and experiment results.

### Fixed

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
