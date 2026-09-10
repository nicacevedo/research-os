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

See `DESIGN_INVARIANTS.md` and `ARCHITECTURE.md`. The automation control plane
is described in `docs/AUTOMATION_MVP.md`.

## Status

**R0 — Kernel** is under implementation. Implemented: the Research Capsule
layout, scientific object schemas, project-scoped semantic digests, cross-object
validation with the review-gated Claim acceptance rule, the global project
registry, and the CLI below.

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
```

`version` prints `0.1.0`.

`doctor` checks that the process is Python 3.12, that `git` is on `PATH`, and
that the four Research OS XDG directories exist, are directories, and are
writable. It does not create those directories, does not require `config.toml`,
`secrets.env`, or a project registry, and does not inspect firmware. It tests
only what the kernel actually needs.

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
