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

### R5 lifecycle hardening — provider-failure closure

The first unattended run across a real infrastructure failure, on 2026-09-19,
found that the runtime could report an outage as science. A planner call died
on an OAuth refresh collision; three research runs concluded
`SUCCEEDED / DONE_FOR_NOW` and `runtime status` told the researcher they had
finished cleanly with a decision waiting. Three more runs were left in flight
with nothing that could advance them, which removed their objectives from the
frontier permanently.

Closed: provider failures now leave the graph as exceptions rather than as
responses a node interprets; retry schedules respect the provider's own
cooldown and refund attempts for calls that never happened; a generic
reconciliation pass recovers any run left in flight with no live work;
objective advancement is fault-isolated with an explicit disposition each; and
infrastructure failures no longer consume an objective's cycle allowance.
`docs/RUNTIME.md` §17 is the contract and
`tests/test_runtime_provider_failure_lifecycle.py` the regression.

Not closed, and a deployment matter rather than a code one: `researchd` shares
provider credentials with the researcher's interactive sessions unless they
install an isolated one. `deploy/researchd.service` documents three ways;
SECURITY.md states the boundary. The runtime recovers from the contention
either way.

## Two external prerequisites, and the policy on each

Neither is a defect. Both are things this workstation cannot establish, and
both are stated here as decisions rather than left as a gap a reader has to
infer from a skip count.

### Slurm stays production architecture, and is unvalidated

**Decision: it remains in the architecture, gated on validation elsewhere.**

There is no `sbatch`, `squeue`, `sacct`, config or `munge` on this host, so the
submission path has never met a scheduler. What exists is the executor, the raw
state mapping, the failure classification and the reconciliation
(`docs/RUNTIME.md` §10), held down against a mock by
`tests/test_experiment_slurm.py`; and a live harness,
`tests/test_experiment_slurm_live.py`, whose bodies are written, are built from
the same API, and have never executed. It is deselected by default behind
`-m slurm_live` and every test in it skips with a reason naming the missing
prerequisite.

So the claim this release makes is: **the Slurm path is implemented and
unvalidated.** It must not be described as production-ready until the live
harness has run against a real cluster, and no test or document may imply that
it has. This is the FULL-readiness gate for the cluster backend; it does not
gate the local architecture, whose executor is `LocalExecutor` and which the
thesis pilot runs entirely through.

Slurm is deliberately *not* installed here to clear the skip. This workstation
is not the intended scheduler environment, and a single-node Slurm stood up to
make a checklist green would validate a configuration nobody will run.

### Review independence is achieved where possible, and reported where not

**Decision: cross-provider-family review is not required for a Release
Candidate. It is required before any claim of independent scientific review,
and therefore before FULL.**

Only one provider family (anthropic) is installed here. The router gives the
strongest separation it can — a different model, `sonnet` reviewing `opus` —
and records what it *achieved* rather than what was asked for, so every
critical review on this host is marked
`DEGRADED_SAME_PROVIDER_FAMILY: NOT an independent review` and that note reaches
the run report. `runtime doctor` says it before a run rather than after.
`review_independence: require` in `runtime.yaml` turns the degradation into a
refusal that fails closed into `WAITING_FOR_EXTERNAL_DEPENDENCY`.

The policy is therefore not "same-family models are independent" — the system
already refuses to say that. It is that a *degraded, labelled* review is
acceptable for RC, because a different model reading a frozen diff and a frozen
evidence packet does catch real defects, and because the alternative on a
one-family machine is no review at all. What it may not do is be *called*
independent. A consequential review presented as independent requires a second
provider family, which is an install and not a code change.

### And one that is now closed

Earlier releases listed **no OS containment** here. That is no longer true and
the entry is retired: `bubblewrap 0.12.0` is present and security-eligible, the
targeted AppArmor profile in `deploy/apparmor-bwrap` grants it `userns` while
the global unprivileged-userns restriction stays on, nested user namespaces are
denied inside the sandbox, and `researchctl runtime containment-audit` records
38/38 adversarial checks held through the production `contain()` adapter, keyed
to the binary's own hash and the policy digest. `SECURITY.md` states the scope
and what the suite does not prove.

## Postponed until a later release proves them necessary

Docker, Podman, Apptainer, vector databases, MCP, PaperQA, local LLMs, Ollama,
web UI, automatic merge, automatic scientific acceptance.

PostgreSQL, LangGraph and a background service left this list in R5; Slurm and
HPC abstraction left it in v1. `ARCHITECTURE.md` §12 and §12a record what forced
each, because "we needed orchestration" is not a reason and would have
justified any of them.
