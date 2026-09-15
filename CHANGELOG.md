# Changelog

All notable changes to Research OS. Dates are release dates.

## [Unreleased] — release candidate `release/v1.1.0-autonomy`

**Not released.** Not merged, not tagged. The package still reports version
`1.0.0`, deliberately: changing it would imply a release that has not happened.
This entry describes the candidate awaiting external cross-family review.

Five things a model was being asked to judge became things the controller
knows. Nothing a human decides changed.

### Added

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

### Fixed

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

### Fixed after the independent delta review

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

### Unchanged

R0 kernel semantics, scientific object schemas, digests, stale-review detection,
Claim acceptance, worktree isolation, argv-based execution with no shell, the
prompt/data boundary, and the one-repair bound everywhere it already applied.

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
