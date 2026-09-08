# Research OS roadmap

This is a release sequence, not a claim that later releases already exist.

**R2 is the first major scientific-value target.**

## R0 — Kernel

Durable local kernel: architecture and security documentation, Research Capsule
v1, Git-tracked YAML scientific objects, Pydantic validation, Review-gated Claim
acceptance, rebuildable project SQLite, noncanonical project registry, CLI.

R0 is under implementation on `r0/kernel-v1`.

Authorized R0 milestones:

- **M1** — contract freeze, documentation, XDG paths, doctor writability, pytest/ruff
- **M2** — IDs, Pydantic models, semantic digests, transition graphs, validators
- **M3** — capsule CLI, project registry
- **M4** — rebuildable index, isolation and failure tests, R0 acceptance gates

Later milestones are not available as commands until they are implemented.

## R1 — Literature

Shared literature library, provider APIs, PDF cache, local ranking, and
full-text retrieval. Not started.

## R2 — Co-Explorer

Blind / Seed / Skeptic explorers, literature verification, and hypothesis
portfolio. First major scientific-value target. Not started.

## R3 — Experimentalist

Work Orders, coding-agent execution, validation, Slurm, and related execution
adapters. Not started.

## R4 — Author / Referee

Writing packets, independent review tooling, and identity-backed review.
Kernel Claim/Review objects exist in R0; R4 adds packet and identity machinery.
Not started.

## R5 — Autonomous OS

Watchers, MCP, cross-project exploration, optional UI and containers.
Not started.

## Postponed until a later release proves them necessary

Docker, Podman, Apptainer, PostgreSQL, vector databases, MCP, LangGraph,
PaperQA, local LLMs, Ollama, web UI, systemd services, Claude Code / Codex
execution adapters, Slurm, HPC abstraction.
