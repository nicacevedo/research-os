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

**R0 — Kernel** is under implementation. The current milestone is **M1**:
documentation contract, XDG path helpers, and `researchctl doctor` writability
checks.

Capsule schemas, project init, registry, and SQLite indexing are specified in
`docs/CAPSULE.md` and `docs/plans/R0_KERNEL_PLAN.md` but are **not implemented
yet**.

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
```

`version` prints `0.1.0`.

`doctor` checks that the process is Python 3.12, that `git` and `sqlite3` are
on `PATH`, and that the four Research OS XDG directories exist, are directories,
and are writable. It does not create those directories, does not require
`config.toml`, `secrets.env`, or a project registry, and does not inspect
firmware.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
```
