# Automation MVP

Research OS can now take a goal, plan bounded work, dispatch a coding agent into
an isolated Git worktree, run acceptance commands itself, have the result
reviewed by a separate read-only model, and stop.

It stops at `READY_FOR_HUMAN` with a diff on a branch. It never merges, never
pushes, and never touches scientific acceptance.

## What is automated

- deterministic preflight and project context building;
- planning a bounded set of work orders from a goal;
- creating an isolated Git worktree per write-enabled work order;
- invoking a coding agent inside that worktree;
- running the work order's acceptance commands and observing their exit codes;
- an independent read-only review of the resulting diff;
- a complete runtime and provenance ledger.

## What remains human

Everything scientific, and everything irreversible:

- authoring a Review and accepting a Claim. `researchctl review` still requires
  an interactive terminal, and `AGENTS.md` still binds agents not to record
  human Reviews. The automation layer has no code path that writes a scientific
  object;
- merging. A finished run leaves a branch and a worktree; the merge command is
  printed for you to run;
- pushing. Nothing is pushed;
- deciding what to do about reviewer findings.

Automation may propose, implement, and verify. It may not approve.

## Architecture

The orchestrator is ordinary deterministic Python
(`research_os.automation.controller`). Models are bounded workers it calls.

```text
goal
  ↓  deterministic preflight (git state, clean tree, base commit)
  ↓  deterministic context packet (capsule inventories, digests, validation)
  ↓  planner model            read-only, no tools, JSON-schema output
  ↓  local plan validation    scope, budget, command allowlist
  ↓  worktree manager         isolated checkout + exclusive lock
  ↓  coder model              write-enabled, only inside that worktree
  ↓  deterministic checks     run by the controller, never by the model
  ↓  reviewer model           read-only, no tools, frozen review packet
  ↓  READY_FOR_HUMAN
```

No model ever sets run state, decides whether a check passed, or authorises the
next step.

## Run states

```text
CREATED → PREFLIGHTED → PLANNING → PLAN_READY → EXECUTING → CHECKING
        → REVIEWING → READY_FOR_HUMAN
```

`CHECKING` and `REVIEWING` may also return to `EXECUTING` for a later work
order. Any non-terminal state may move to `FAILED` or `CANCELLED`.
`READY_FOR_HUMAN`, `FAILED`, and `CANCELLED` are terminal and have no successor:
a failed run can never be walked back into a ready one.

Every transition is validated against an explicit table and appended to the
event ledger.

## The READY_FOR_HUMAN gate

Re-evaluated from persisted state, not from what the controller believes
happened. A run may only become `READY_FOR_HUMAN` when, for every work order:

- its status is `reviewed`;
- it ran at least one acceptance command;
- every required acceptance command passed;
- a review was recorded and its verdict is not `FAIL`.

A reviewer verdict of `PASS_WITH_REPAIR` reaches `READY_FOR_HUMAN` with its
findings listed as unresolved. A verdict of `FAIL` fails the run.

## Provider configuration

Providers are discovered locally. Nothing is assumed about a CLI that is not
installed:

```bash
researchctl auto providers
```

This reports, per provider: executable path, availability, whether
non-interactive invocation was verified from the local `--help`, version,
authentication status, and the resulting role assignment.

Roles are configured in `~/.config/research-os/automation.yaml`. Every key is
optional; anything absent uses the shipped default.

```yaml
planner:
  provider: claude
  model: sonnet
  effort: high
  read_only: true
coder:
  provider: claude
  model: opus
  read_only: false
  tools: [Read, Write, Edit, Glob, Grep]
reviewer:
  provider: claude
  model: sonnet
  effort: high
  read_only: true
budget:
  max_model_calls: 8
  max_command_timeout_seconds: 900
  max_wall_clock_seconds: 3600
  max_write_work_orders: 2
  max_work_orders: 4
allowed_check_programs: [uv, python, python3, pytest, ruff, git]
```

### Review independence

Each run records how independent its review actually was:

| value | meaning |
|---|---|
| `INDEPENDENT_PROVIDER_FAMILY` | the reviewer ran on a different model family from the implementer |
| `DEGRADED_SAME_PROVIDER_FAMILY` | different models, same family. **Not an independent review.** |
| `DEGRADED_SAME_MODEL` | same provider and same model. The weakest case. |

When two families are installed, the reviewer is moved off the implementer's
family automatically unless the config file pins it. When only one family is
installed the run still proceeds, and every report says plainly that the review
is not independent.

## Security boundary

- **Read-only roles get no tools at all.** The planner and reviewer are invoked
  with an empty tool set, so they cannot read or write the repository even if
  their prompt is subverted. They see only the packet the controller built.
- **The coding agent gets no command-running tool.** The default tool set is
  `Read, Write, Edit, Glob, Grep`. It runs under the provider's restricted mode,
  which confines file tools to the working directory, and with permission
  prompts denied rather than escalated.
- **Scope is enforced, not requested.** After execution the controller reads the
  worktree itself and fails the work order if anything outside `allowed_paths`
  changed. Any change under `.research/` fails unconditionally, even if a plan
  named it.
- **Acceptance commands are argument vectors on an allowlist.** They run without
  a shell, so there is no quoting or metacharacter surface, and only programs in
  `allowed_check_programs` may be named.
- **No external content ingestion.** There is no literature or web retrieval in
  this MVP. The rule that raw retrieved external content must never flow into a
  tool-enabled write agent is preserved by not having such a path at all.

## Worktree isolation

One writing worker, one dedicated worktree. This is enforced:

- the branch is `automation/<run-id>/<task-id>`, based on the work order's
  recorded base commit;
- the worktree is created under `~/.local/state/research-os/worktrees/`, outside
  every project repository;
- an exclusive lock file under `~/.local/state/research-os/locks/` refuses a
  second concurrent worker in the same checkout;
- `assert_isolated` runs immediately before every write invocation and refuses
  the canonical checkout, a path inside the project, a missing worktree, a
  missing lock, or a lock held for a different path;
- a run refuses to start at all if the project tree is dirty, so the base commit
  is unambiguous;
- failed worktrees are deliberately left in place. They are removed only by
  `researchctl auto cleanup`, which keeps the branch.

`git worktree add` does add a branch and a worktree registration to the project
repository. It never changes the project's working tree, HEAD, or any file.

## Runtime data

Runtime state lives under the Research OS state home, which
`DESIGN_INVARIANTS.md` assigns to `~/.local/state/research-os`:

```text
~/.local/state/research-os/
  runs/RUN-<utc>-<hash>/
    run.json          atomic whole-file replacement
    events.jsonl      append-only ledger
    worktrees.json    projection of the run's worktrees
    context/          the context packet, as JSON and as supplied text
    prompts/          every prompt sent, verbatim
    model_outputs/    every response, raw and structured
    logs/             provider stdout and stderr
    plan/             the parsed, validated plan
    checks/           per-command stdout, stderr, and results
    execution/        the diff each work order produced
    reviews/          each structured review outcome
  worktrees/RUN-.../T-001/
  locks/
```

None of this is scientific state. Deleting a run directory loses a provenance
ledger and corrupts nothing: no capsule file is written to track a run, and no
scientific object references one.

## Budgets

Enforced before every model call, not audited afterwards:

- `max_model_calls` per run. A plan is refused up front when the remaining
  budget cannot cover it (one coder call plus one reviewer call per work order);
- `max_command_timeout_seconds` per acceptance command;
- `max_wall_clock_seconds` per run;
- `max_write_work_orders` and `max_work_orders` per plan.

Provider-reported cost and token counts are recorded when the provider reports
them and stored as `null` when it does not. Nothing is estimated: an unknown
cost is reported as unknown.

## Example workflow

```bash
# 1. See what this machine can actually do.
researchctl auto providers

# 2. Plan without spending anything on a write agent.
researchctl auto start ~/research/my-project \
  --goal "Add a regression test for the loader and fix the off-by-one" \
  --dry-run

# 3. Start for real. This plans; it does not execute.
researchctl auto start ~/research/my-project \
  --goal "Add a regression test for the loader and fix the off-by-one"

# 4. Execute the plan: worktree, coder, checks, review.
researchctl auto run RUN-20260909T101500Z-0a1b2c3d

# 5. Read the diff yourself, then merge if you accept it.
git -C ~/.local/state/research-os/worktrees/RUN-.../T-001 diff HEAD
git -C ~/research/my-project merge --no-ff automation/run-.../t-001

# 6. Release the worktree. The branch is kept.
researchctl auto cleanup RUN-20260909T101500Z-0a1b2c3d
```

## Commands

| command | effect |
|---|---|
| `researchctl auto providers` | local provider discovery and role assignment |
| `researchctl auto start PROJECT --goal "..."` | preflight, context, plan. No write agent. |
| `researchctl auto start ... --dry-run` | the same, marked so it can never be executed |
| `researchctl auto start ... --skip-planner` | preflight and context only; no model call at all |
| `researchctl auto start ... --max-model-calls N` | override the run's model-call budget |
| `researchctl auto run RUN_ID` | execute a `PLAN_READY` run through checks and review |
| `researchctl auto status RUN_ID [--json]` | current state, checks, verdicts |
| `researchctl auto report RUN_ID` | the full human report, including the next action |
| `researchctl auto events RUN_ID [--limit N]` | the append-only ledger |
| `researchctl auto runs` | every run on this machine |
| `researchctl auto cancel RUN_ID [--reason ...]` | cancel a run that is not terminal |
| `researchctl auto cleanup RUN_ID` | remove the run's worktrees, keep the branches |

`PROJECT` is a registered project id or a path. A registered id wins.

## Inspecting, resuming, cleaning up

- **Inspect**: `researchctl auto report RUN_ID` for the narrative,
  `researchctl auto events RUN_ID` for the ledger, and the run directory for
  every prompt, response, diff, and command output.
- **Resume**: there is no resume in this MVP. A run executes once; a failed run
  is inspected and a new run started. Its worktree and branch are kept so no
  work is lost.
- **Clean up**: `researchctl auto cleanup RUN_ID` removes the worktrees and
  releases their locks. Branches, run records, and ledgers are kept. Delete a
  run directory by hand when you no longer want the provenance.

## Not implemented here

Deliberately out of scope: literature retrieval, embeddings, MCP, containers,
schedulers, daemons, background autonomous loops, dashboards, Slurm, automatic
merge, and automatic scientific acceptance. See `ROADMAP.md`.
