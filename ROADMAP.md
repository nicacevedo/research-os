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

The durable autonomous runtime: an operational database, a work queue with
leases, an idempotency ledger, a content-addressed artifact store, bounded
resumable reasoning cycles, a control-plane daemon, and the capability handlers
that let a cycle actually do research. In progress; `docs/RUNTIME.md` is its
live specification and `docs/R5_BUILD_RECORD.md` records what was built and
what remains.

What R5 deliberately did **not** acquire is epistemic authority. All eight
actions that require human scientific authority turn out to be actions the
person performs; the runtime prepares the decision and hands over the command.

Still not started, and not part of R5: MCP, a UI, containers.

### R5 integration — in progress

`integration/autonomous-runtime-vnext` converges the v1.1 autonomy line with
R5, which were siblings on v1.0.0 rather than a chain, and closes the loop R5
shipped without: runtime findings, a grounded `propose_capsule_change`, a
durable experiment-interpretation identity, protection against promoting onto a
stale scientific basis, automatic continuation after a human scientific change,
and `nominate_insight`. Every action in the policy table now has a handler or is
one a person performs. `docs/INTEGRATION_BUILD_RECORD.md` is the record and
`docs/RUNTIME.md` §14a–§14c the specification.

Two things it could not verify here, both genuine external prerequisites rather
than defects:

- **no Slurm.** No `sbatch`, `squeue`, `sacct`, config or `munge` on this host,
  so the submission path has still never met a scheduler. The live harness is
  `tests/test_experiment_slurm_live.py`, behind `-m slurm_live`.
- **no OS containment.** `research_os/sandbox.py` implements it and this
  kernel refuses the unprivileged user namespaces every available mechanism
  needs, so high-autonomy execution of model-written code is *refused* here
  rather than run uncontained. `runtime doctor` reports the probe and the
  remedy.

## Postponed until a later release proves them necessary

Docker, Podman, Apptainer, vector databases, MCP, PaperQA, local LLMs, Ollama,
web UI, automatic merge, automatic scientific acceptance.

PostgreSQL, LangGraph and a background service left this list in R5; Slurm and
HPC abstraction left it in v1. `ARCHITECTURE.md` §12 and §12a record what forced
each, because "we needed orchestration" is not a reason and would have
justified any of them.
