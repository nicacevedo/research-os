# Changelog

All notable changes to Research OS. Dates are release dates.

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
