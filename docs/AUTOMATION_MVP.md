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
  ↓  planner model            context-only, no tools, JSON-schema output
  ↓  local plan validation    scope, budget, command allowlist
  ↓  snapshot manager         isolated read-only checkout, pinned to the base
  ↓  analyst model            snapshot-read: Read, Glob, Grep and nothing else
  ↓  structured findings      schema-validated, archived, treated as data
  ↓  worktree manager         isolated checkout + exclusive lock
  ↓  coder model              write-enabled, only inside that worktree
  ↓  deterministic checks     run by the controller, never by the model
  ↓  reviewer model           context-only, no tools, frozen review packet
  ↓  one bounded repair       at most once, same worktree, same scope
  ↓  checks + reviewer again  every required check re-established
  ↓  READY_FOR_HUMAN
```

The analysis step is optional: a plan contains an analyst task only when the
planner asks for one. A plan that needs no investigation is still the single
coder task it was before.

No model ever sets run state, decides whether a check passed, or authorises the
next step.

## Run states

```text
CREATED → PREFLIGHTED → PLANNING → PLAN_READY → EXECUTING → CHECKING
        → REVIEWING → READY_FOR_HUMAN
```

`CHECKING` and `REVIEWING` may also return to `EXECUTING`, either for a later
work order or for the single bounded repair, which re-enters execution and then
comes back through `CHECKING`. Any non-terminal state may move to `FAILED` or
`CANCELLED`.
`READY_FOR_HUMAN`, `FAILED`, and `CANCELLED` are terminal and have no successor:
a failed run can never be walked back into a ready one.

Every transition is validated against an explicit table and appended to the
event ledger.

## The READY_FOR_HUMAN gate

Re-evaluated from persisted state, not from what the controller believes
happened. A run may only become `READY_FOR_HUMAN` when, for every coding work
order:

- its status is `reviewed`;
- it ran at least one acceptance command;
- every required acceptance command passed;
- a review was recorded and its verdict is not `FAIL`.

An analyst work order is judged differently, because it changed nothing: its
status must be `analyzed` and it must have archived a schema-valid report. No
acceptance command is run for it and no review is recorded, so requiring either
would be requiring evidence that cannot exist.

A verdict of `FAIL` fails the run and is never repaired. A verdict of
`PASS_WITH_REPAIR` spends the single bounded repair if one remains, after which
every deterministic check and the reviewer both run again; if the reviewer asks
for a repair a second time, the run reaches `READY_FOR_HUMAN` with the
remaining findings listed as unresolved.

## The single bounded repair

One repair attempt per work order, `max_repair_attempts: 1` by default and
capped at one by the budget model itself, so no configuration file or flag can
turn it into a loop. It is triggered by a failed required check or by a
reviewer returning `PASS_WITH_REPAIR`, never by `FAIL`.

The repair runs in the same worktree, and is given more evidence rather than
more authority: the original work order, the current diff, the exact failed
command with its captured output, the reviewer findings, and the analyst
findings. Its `allowed_paths`, `forbidden_paths`, acceptance commands, and tool
set are the ones the plan produced. It spends an ordinary model call from
`max_model_calls`. Afterwards **every** required check runs again, not only the
one that failed, so a repair that breaks something that previously passed is
caught.

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
analyst:
  provider: claude
  model: sonnet
  effort: high
  read_only: true
  access: snapshot_read
  tools: [Read, Glob, Grep]
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
  max_repair_attempts: 1
  max_work_orders: 4
allowed_check_programs: [uv, pytest, ruff]
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

- **Three named positions, not a permission framework.** Every role declares
  one `access` value and is given exactly what that position allows.
  `context_only` is the planner and reviewer: no tools at all. `snapshot_read`
  is the analyst: the read-only file tools, in a pinned snapshot.
  `isolated_write` is the coder: write tools, in its own worktree. A role whose
  declared reach and tool set disagree is rejected when the config is loaded,
  and the invocation layer independently re-applies the same rule, so no future
  caller can assemble an invocation that hands a model more than its position
  allows.
- **Context-only roles get no tools at all.** The planner and reviewer are
  invoked with an empty tool set, so they cannot read or write the repository
  even if their prompt is subverted. They see only the packet the controller
  built. A configuration that declares `read_only: true` together with any tool
  is refused; asking for file tools requires naming `access: snapshot_read`
  explicitly, so an existing config cannot acquire them by accident.
- **Read-only workers do not run in your repository.** The planner and reviewer
  processes are started from a runtime-owned directory under the run
  (`<run>/context/`), not from the project checkout. The project path reaches
  them as data in the context packet. The analyst runs in its own snapshot,
  never the canonical checkout. Each invocation records the directory it
  actually ran in.
- **The analyst cannot act, and is proved not to have.** It receives only
  `Read`, `Glob`, and `Grep` — no `Write`, no `Edit`, no `Bash` — so there is
  nothing for it to change a file or run a command with. That is not taken on
  trust: the controller records the snapshot's `HEAD` and `git status` before
  the invocation and requires both unchanged afterwards. A changed snapshot
  fails the work order and the run and is recorded in the ledger. Nothing is
  quietly restored, because restoring it would destroy the only evidence that a
  boundary did not hold.
- **Analyst output is data, never instruction.** The parsed report is quoted
  into a downstream prompt inside a labelled block, with the closing delimiter
  stripped from every field so a finding cannot forge the end of the fence. It
  cannot change tool permissions, allowed paths, acceptance commands, budgets,
  worktree paths, or run state: those are fixed on the work order when the plan
  is validated, and the controller re-checks them from the work order after the
  worker has stopped. Only the validated artifact crosses; no provider session
  or free-form prior output is carried across, and the exact artifact used is
  recorded with its SHA-256.
- **The coding agent gets no command-running tool.** The default tool set is
  `Read, Write, Edit, Glob, Grep`. It runs under the provider's restricted mode,
  which confines file tools to the working directory, with `--strict-mcp-config`
  so the run cannot inherit MCP servers configured elsewhere on the machine, and
  with permission prompts denied rather than escalated.
- **Scope is enforced, not requested.** After execution the controller reads the
  worktree itself and fails the work order if anything outside `allowed_paths`
  changed. Any change under `.research/` fails unconditionally, even if a plan
  named it. `.research` and `.research/...` are refused identically at plan
  validation, because naming the directory grants exactly what naming a file
  inside it grants.
- **Symlinks may not leave the worktree.** Git-level isolation is not
  filesystem-level isolation: a symlink inside the worktree that points outside
  it would carry a write past the isolation boundary, and because the link
  itself does not change, `git diff` would report a clean, in-scope run. Before
  a writer is invoked, and again when evidence is collected, the controller
  scans the worktree and refuses the work order if any symlink resolves outside
  it. This does not depend on the provider's restricted mode, which is
  defence in depth only.
- **Acceptance commands are authorised as whole argument vectors.** Not by
  program name: `python -c`, `git push`, and `uv run python -c` are all
  reachable from an `argv[0]` allowlist. See *Acceptance commands* below.
- **No external content ingestion.** There is no literature or web retrieval in
  this MVP. The rule that raw retrieved external content must never flow into a
  tool-enabled write agent is preserved by not having such a path at all.

### Acceptance commands

The controller runs acceptance commands itself, so which command shapes a plan
may name is policy. A planner-originated command is authorised against a small
explicit grammar, and anything the grammar does not recognise is refused before
the argument vector reaches the operating system. These are the supported forms:

```text
pytest [options] [test paths]
ruff check [options] [paths]
ruff format --check [options] [paths]
uv run pytest [options] [test paths]
uv run ruff check [options] [paths]
uv run ruff format --check [options] [paths]
```

- Options are an approved set per tool, not a pass-through: pytest accepts
  `-q`, `-qq`, `--quiet`, `-v`, `-vv`, `--verbose`, `-x`, `--exitfirst`,
  `--no-header`, `--no-summary`, `--strict-config`, `--strict-markers`,
  `--maxfail=N`, and `--tb=STYLE`; ruff accepts `-q`, `--quiet`, `--no-cache`,
  `--exit-non-zero-on-fix`, `--no-fix` (check), and `--diff` (format). An
  unrecognised option is rejected rather than forwarded. The set is deliberately
  small and will grow only when a real plan needs a flag.
- Path arguments must be relative to the worktree. Absolute paths, `..`, and `~`
  are refused, as is any token carrying shell syntax.
- `python`, `python3`, `git`, `bash`, `sh`, `zsh`, and their `uv run` forms are
  not reachable from planner output at all.
- `allowed_check_programs` may only narrow this grammar. Naming a program the
  grammar has no rule for is a configuration error, not a way to enable it.
- Nothing runs through a shell: `subprocess.run` is always given a list.
- The controller injects one Git observation of its own, `git diff --check
  HEAD`. It does not come from a model and deliberately does not use the
  planner authorisation path.

### Trusted-project execution boundary

**Research OS Automation MVP is currently intended for trusted local
repositories. Acceptance checks such as pytest execute repository code,
including code an automation worker has just modified, with the permissions of
the `researchctl` process. Worktree isolation protects the canonical Git
checkout; it is not an OS sandbox.**

This is intentional for the current MVP, and it is the boundary to understand
before pointing `auto` at anything:

- Running a project's own tests means running the project's own code. A worker
  that edits a module the test suite imports has, by that edit, chosen what the
  controller will execute next. No acceptance-command policy can change this;
  the policy limits which *commands* may run, not what a project's code does
  when it runs.
- There is no network or process-level sandboxing. Check subprocesses inherit
  the environment of the `researchctl` process, including any credentials in it.
- Do not pass an arbitrary untrusted or freshly cloned repository to
  `researchctl auto`. Read the code you are automating first, exactly as you
  would before running its test suite by hand.
- Containers and sandboxed execution are deferred. They are the right answer for
  untrusted repositories and this MVP does not pretend to provide them.

What worktree isolation *does* give you: a write-enabled worker cannot reach the
canonical checkout, cannot commit, merge, or push, and cannot change scientific
files. Those are Git-level and controller-level guarantees, and they hold. They
are not a claim about what a process can do to the machine it runs on.

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
- the worktree is scanned for symlinks that resolve outside it, before the
  writer is invoked and again during evidence collection. Any such link fails
  the work order, because a write through it would land outside the isolation
  boundary without appearing in `git diff`. A symlink whose target stays inside
  the worktree is fine; an absolute link back at the canonical checkout is not,
  since that is the checkout the worktree exists to protect;
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
  budget cannot cover it (one coder call plus one reviewer call per coding work
  order, one call per analyst work order);
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
