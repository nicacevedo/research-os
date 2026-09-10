# Research OS roadmap

This is a release sequence, not a claim that later releases already exist.

**R2 is the first major scientific-value target.**

## R0 — Kernel

Durable local kernel: architecture and security documentation, Research Capsule
v1, Git-tracked YAML scientific objects, Pydantic validation, Review-gated Claim
acceptance, a noncanonical project registry, and the CLI.

R0 is under implementation.

R0 milestones:

- **M1 — implemented.** Contract freeze, documentation, XDG paths, doctor
  writability, pytest/ruff.
- **M2 — implemented.** IDs, Pydantic models, semantic digests, cross-object
  validators.
- **M3 — implemented.** Capsule CLI, project registry.
- **WP-A — implemented.** Scientific integrity corrections: Experiment
  preregistration, supporting/contrary Evidence polarity, versioned
  project-scoped digests, and explicit `evidence_digests` on Reviews with
  complete-coverage enforcement.
- **WP-B — implemented.** Usability and kernel simplification: `researchctl
  digest`, interactive human `researchctl review`, strict UTF-8 handling, a JSON
  project registry, and removal of unused machinery.
- **M4 — not started.** Isolation and failure tests, R0 acceptance gates.

Later milestones are not available as commands until they are implemented.

Withdrawn from R0: a project-local materialized index
(`.research/runtime/state.sqlite`, `rebuild-index`, and a whole-capsule
`canonical_source_digest`). Canonical Git-tracked files are the only project
scientific state, and the kernel depends on no database. `.research/runtime/`
remains reserved, gitignored scratch space.

Lifecycle status transitions for every object type are specified in
`docs/CAPSULE.md`. R0 validates current state, not transition history, and
enforces no transition graph at runtime.

## Automation MVP — implemented, outside the R0 kernel

A deterministic planner/executor/reviewer control plane (`researchctl auto`),
authorised by explicit human instruction rather than by the R0 milestone
sequence. It automates software work under isolation, budgets, and deterministic
acceptance checks. It automates no scientific judgement: no capsule file is
written, no Review authored, no Claim accepted, nothing merged or pushed. See
`docs/AUTOMATION_MVP.md`.

This supersedes the earlier "Claude Code / Codex execution adapters" postponement
for orchestration only. Every other postponed technology below still stands.

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
PaperQA, local LLMs, Ollama, web UI, systemd services, Slurm, HPC abstraction,
automatic merge, automatic scientific acceptance.
