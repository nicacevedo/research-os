# Research OS

Local-first kernel for reproducible, evidence-grounded scientific research.

This repository is the **central Research OS kernel**. It is not an individual
science project. Scientific work lives in separate Git repositories, each with
its own `.research/` capsule.

## Architecture principles

- Git-tracked YAML/Markdown is canonical scientific state
- The global project registry is rebuildable discovery metadata, not truth
- Scientific state is project-isolated
- No agent approves its own scientific work
- Local deterministic computation before any later LLM reasoning

See `DESIGN_INVARIANTS.md` and `ARCHITECTURE.md`.

| document | what it covers |
| --- | --- |
| `docs/CAPSULE.md` | the scientific record: objects, digests, the acceptance rule |
| `docs/RESEARCH.md` | research runs: one goal to a human handoff |
| `docs/AUTOMATION_MVP.md` | the automation control plane a code task is dispatched to |
| `docs/LITERATURE.md` | retrieval, identity, indexing, and search |
| `docs/EXPERIMENTS.md` | declared commands, local and Slurm execution, evidence packets |
| `docs/OPERATIONS.md` | `doctor`, `storage`, recovery, and budgets |

## Status

**R0 — Kernel** is complete: the Research Capsule layout, scientific object
schemas, project-scoped semantic digests, cross-object validation with the
review-gated Claim acceptance rule, the global project registry, and the human
review flow.

**Research OS v1** adds the layers above it: literature retrieval, a
read-only analyst, structured proposals, declared-command experiment execution,
evidence-grounded manuscript drafting, cross-project insights, and the unified
research runs that orchestrate all of it. Every one of them is bounded,
delegated to a deterministic controller, and stops at a human.

`docs/CAPSULE.md` is the live specification and is authoritative on anything
scientific. Canonical Git-tracked YAML and Markdown under `.research/` are the
only project scientific state: there is no materialized project index, and the
kernel needs no database.

`~/.config/research-os/config.toml` is reserved for later releases and is not
read in R0. Path locations can be overridden with:

```text
RESEARCH_OS_CONFIG_HOME
RESEARCH_OS_DATA_HOME
RESEARCH_OS_CACHE_HOME
RESEARCH_OS_STATE_HOME
```

## Installation

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:nicacevedo/research-os.git
cd research-os
uv sync
```

## Current commands

```bash
uv run researchctl version
uv run researchctl doctor
uv run researchctl init-project [path]
uv run researchctl register-project [path]
uv run researchctl validate-project [path]
uv run researchctl projects
uv run researchctl status [path]
uv run researchctl digest <OBJECT-ID> [path]
uv run researchctl review <CLAIM-ID> [path]
uv run researchctl auto providers
uv run researchctl auto start PROJECT --goal "..." [--dry-run]
uv run researchctl auto run RUN_ID
uv run researchctl auto status RUN_ID
uv run researchctl auto report RUN_ID
uv run researchctl auto events RUN_ID
uv run researchctl auto runs
uv run researchctl auto cancel RUN_ID
uv run researchctl auto cleanup RUN_ID
uv run researchctl research start PROJECT --goal "..." [--run]
uv run researchctl research run RUN_ID
uv run researchctl research answer RUN_ID --answer "..." [--stop]
uv run researchctl research resume RUN_ID [--retry] [--force]
uv run researchctl research status|report|cancel|cleanup|events RUN_ID
uv run researchctl research list
uv run researchctl lit sources|retrieve|fetch|search|show|index|status
uv run researchctl propose start|list|show|promote|events
uv run researchctl experiment commands|scheduler|run|show|poll|cancel|runs|cleanup
uv run researchctl insight list|show|nominate|promote|search
uv run researchctl paper sources|write|show|list|cleanup
uv run researchctl storage [--reclaim]
```

`version` prints `0.1.0`.

`doctor` reports what this machine can actually do: Python and `git`, the four
Research OS XDG directories, the agent providers and how the roles would be
assigned, whether the review would be independent, the literature configuration
and index, the declared experiment commands and the scheduler, any run that is
stuck or waiting, and disk. It makes no network request, submits no job and
invokes no model. `PASS` works, `FAIL` means something you asked for is broken
and exits `1`, and `WARN` means a capability is simply absent — one provider, no
cluster — which exits `0`, because a machine in that state is a perfectly good
machine and an exit code that said otherwise would train you to ignore it. It
creates nothing. `--json` emits the same report for a script.

`storage` measures every runtime store and releases, with `--reclaim`, only
what a finished run is still holding and can be rebuilt: worktrees — from
automation, paper and experiment runs alike — and check environments. Records,
ledgers, prompts, model outputs, reviews and branches are kept. A single run can
be released on its own with `auto cleanup`, `paper cleanup` or
`experiment cleanup`.

`init-project` creates a new Research Capsule in an existing Git repository
(default path: `.`). It never runs `git init`, never overwrites an existing
`.research/`, and never modifies the repository root `.gitignore`. Use `--id`
when the repository directory name is not a valid project slug, and `--title`
to set the project title (default: the repository directory name). After the
capsule is created it is registered in the noncanonical project registry.

`register-project` records discovery metadata for an existing capsule. It does
not initialize a capsule or rewrite canonical YAML/Markdown.

`validate-project` is read-only. Warnings do not fail the command; any ERROR
exits `1`. Pass `--json` for a deterministic machine-readable report.

`projects` lists registered projects in project-id order and marks each path
`AVAILABLE` or `MISSING`. A missing registry is treated as empty and is not
created.

`status` reports file-derived capsule identity, object counts, and validation
error/warning counts. It does not create runtime state or consult the registry
for counts.

`digest` prints the current project-scoped semantic digest of one scientific
object, as a bare `1:<64 hex>` line, so a researcher never hand-computes one.
Digests are project-scoped, so the same object copied into another project has a
different digest and does not inherit the first project's reviewed identity.
Reviews are not reviewable subjects and have no digest.

`review` records a human Review of a Claim. It renders a review packet — the
Claim, its digest, every linked supporting and contrary Evidence object with its
source pointers and current digest, every Experiment that evidence rests on —
each digest-material field in full, plus its current digest — the
`contrary_evidence_addressed` note, and the referenced hypotheses — then asks for a verdict (`approve`,
`revise`, `reject`, or `cancel`), findings, and a final confirmation, and writes
one canonical `.research/reviews/REV-NNNN.yaml` atomically. It binds the Claim
digest, the digest of *every* linked Evidence object, and the digest of *every*
Experiment they reach, which is what the acceptance rule requires.

The Claim must be at `status: evidence_linked`. `review` requires an interactive
terminal, has no noninteractive approval flag, and **never** changes the Claim's
status: promoting a Claim to `accepted` is a deliberate edit to canonical YAML
and stays a human act. There is no `researchctl set-status`. To re-review an
accepted Claim, set it back to `evidence_linked` first — R0 acceptance is
existential, so a new verdict cannot override an approval that already satisfies
the gate.

`auto` is the deterministic automation control plane: it turns a goal into a
bounded plan, dispatches a coding agent into an isolated Git worktree, runs the
acceptance commands itself, has the diff reviewed by a separate read-only model,
and stops at `READY_FOR_HUMAN`. It never merges, never pushes, and never records
a scientific Review or accepts a Claim. Runtime state lives under
`~/.local/state/research-os/runs/` and can be deleted without affecting any
project. See `docs/AUTOMATION_MVP.md`.

`auto` is for repositories you trust. Its acceptance checks run the project's
own code, including code a worker has just written, as your user, and a
declared experiment runs whatever command you declared. Every write-capable
task -- code, paper and experiment alike -- works in an isolated Git worktree
rather than your canonical checkout, and a write worker is given file tools
only, never a command tool. That is Git isolation, not an OS sandbox: the
process can still reach any path your user can, and containers are not
provided. Do not point `auto` or `experiment` at an untrusted or freshly cloned
repository.

`research` is the whole architecture behind a few verbs. It plans a research
goal into a bounded DAG of typed tasks — literature, analysis, proposal, code,
experiment, paper, human checkpoint — dispatches each to the controller that
already owns that kind of work, and stops at a human. It owns no worker: a code
task becomes an automation run and gets that layer's worktree isolation, scope
enforcement, command policy, acceptance checks, independent review and single
bounded repair without any of it being reimplemented. Model calls, write tasks,
experiments, cluster submissions and wall clock are separately budgeted and
every spend is checked before it happens. Experiments do not execute without
`--execute-experiments`; without it, an experiment task resolves its exact
command and stops. See `docs/RESEARCH.md`.

Everything automation produces is a candidate. Proposals are suggestions,
evidence packets are candidates, drafts are prose. Promoting any of it into a
project's scientific record is a human act through `propose promote`, and a
Claim becomes `accepted` only through `review` plus a deliberate edit — both of
which require an interactive terminal.

Exit codes: `0` success, `1` project/validation/runtime failure (including a
path that is not a Research OS project), `2` usage/argument error.

The project registry lives at `~/.local/share/research-os/project_registry.json`
(or `$RESEARCH_OS_DATA_HOME/project_registry.json`). It holds discovery metadata
only, is written by atomic replacement, and may be deleted freely — nothing in
your projects depends on it, and `researchctl register-project` rebuilds any
entry. If a registry from an older build (`project_registry.sqlite`) is still
present and the JSON registry does not yet exist, `researchctl projects`
refuses to enumerate rather than report an empty list, but `init-project` and
`register-project` still work: the missing JSON registry is treated as empty,
the project you named is registered, and the JSON file is created. Either
command then notes on stderr that the legacy file is still there and can be
deleted. That file is never read, imported, modified, or removed
automatically, by any command.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

All three must pass before stopping.
