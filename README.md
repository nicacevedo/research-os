# Research OS

Local-first kernel for reproducible, evidence-grounded scientific research.

This repository is the **central Research OS kernel**. It is not an individual
science project. Scientific work lives in separate Git repositories, each with
its own `.research/` capsule.

## Architecture principles

- Git-tracked YAML/Markdown is canonical scientific state
- SQLite indexes and the global project registry are rebuildable, not truth
- Scientific state is project-isolated
- No agent approves its own scientific work
- Local deterministic computation before any later LLM reasoning

See `DESIGN_INVARIANTS.md` and `ARCHITECTURE.md`.

## Status

**R0 — Kernel** is under implementation. The current milestone is **M3**:
Research Capsule filesystem layout, global project registry, and CLI commands
for init, register, validate, list, and status.

M2 scientific models and `validate_objects()` remain the source of scientific
semantics. M4 (`rebuild-index` and `.research/runtime/state.sqlite`) is not
implemented yet.

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
```

`version` prints `0.1.0`.

`doctor` checks that the process is Python 3.12, that `git` and `sqlite3` are
on `PATH`, and that the four Research OS XDG directories exist, are directories,
and are writable. It does not create those directories, does not require
`config.toml`, `secrets.env`, or a project registry, and does not inspect
firmware.

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

`status` reports file-derived capsule identity, object counts, validation
error/warning counts, and whether `runtime/state.sqlite` is present. It does
not create runtime state or consult the registry for counts.

Exit codes: `0` success, `1` project/validation/runtime failure (including a
path that is not a Research OS project), `2` usage/argument error.

The project registry lives at `~/.local/share/research-os/project_registry.sqlite`
(or `$RESEARCH_OS_DATA_HOME/project_registry.sqlite`). Deleting it does not
alter project files.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
```
