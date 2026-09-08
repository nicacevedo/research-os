# Research OS Architecture

Research OS is a local-first scientific orchestration system for reproducible,
evidence-grounded, auditable, and selectively autonomous research.

Its purpose is not to replace scientific judgment. Its purpose is to make
scientific exploration, literature synthesis, experimentation, validation, and
writing easier to reproduce, inspect, and coordinate.

`DESIGN_INVARIANTS.md` is the architectural constitution. This document describes
system boundaries and responsibilities. `docs/CAPSULE.md` is the Research Capsule
v1 contract. `docs/plans/R0_KERNEL_PLAN.md` is the version-controlled R0
implementation contract.

## 1. Operating model

The default workflow is:

```text
local deterministic computation
        ↓
interesting / uncertain event
        ↓
high-quality scientific reasoning
        ↓
structured scientific state
        ↓
local validation / indexing / monitoring
```

Research OS is **local-first / hybrid**. Deterministic parsing, validation,
indexing, and bookkeeping run on the local machine. Expensive model reasoning,
when later releases add it, is event-triggered rather than continuous. R0
implements only the local deterministic kernel: no LLMs, no agents, no network
literature APIs.

## 2. Filesystem and authority boundaries

```text
CODE
~/research/research-os

SCIENCE
individual project Git repositories
each with a project-local .research/ capsule

DURABLE SHARED DATA
~/.local/share/research-os

CACHE
~/.cache/research-os

CONFIG
~/.config/research-os

RUNTIME
~/.local/state/research-os
```

These XDG locations may be overridden for tests and isolated runs:

```text
RESEARCH_OS_CONFIG_HOME
RESEARCH_OS_DATA_HOME
RESEARCH_OS_CACHE_HOME
RESEARCH_OS_STATE_HOME
```

`~/.config/research-os/config.toml` is reserved for later releases. R0 has no
configuration parser.

The kernel repository must not become a dumping ground for literature corpora,
large data, model caches, embeddings, temporary results, runtime databases, or
secrets.

Deleting cache or runtime state must not damage canonical scientific state.
Reinstalling Research OS must not delete a literature library or project science.

## 3. Canonical scientific state

**Git-tracked human-readable files are scientific truth.**

Inside each scientific project, canonical state lives under `.research/`:

- `project.yaml`
- `CHARTER.md`
- `STATE.md`
- typed YAML objects (questions, ideas, hypotheses, assumptions, claims,
  decisions, experiments, reviews, evidence)

Canonical files are inspectable, diffable, reviewable, and reconstructible from
Git history. The kernel never rewrites existing canonical YAML except
`init-project` creating a missing capsule (M3) and an explicit, human-run,
versioned schema migration, whose effect must appear as an ordinary reviewable
Git diff in the science project (see the migration policy in
`docs/CAPSULE.md`). Validate never rewrites files.

Scientific objects are **project-isolated**. Hypotheses, claims, experiments, and
interpretations belong to the project that owns them. Cross-project reuse is
explicit later; R0 does not scan other projects or `shared_knowledge/`.

## 4. Project-local scientific state vs global durable data

| Kind | Location | Canonical? |
|---|---|---|
| Project science | `<project>/.research/*.yaml`, Markdown | yes |
| Project index | `<project>/.research/runtime/state.sqlite` | no; rebuildable |
| Global project registry | `~/.local/share/research-os/project_registry.sqlite` | no; disposable metadata |
| Shared literature (R1+) | `~/.local/share/research-os/literature/` | later; not R0 kernel state |
| Secrets (later) | `~/.config/research-os/secrets.env` | never Git-tracked |

R0 must not read host `literature/` or project `.research/literature/` as kernel
state. Empty bootstrap subdirectories under the host XDG trees are leftovers,
not an R0 contract.

## 5. Research Capsule

A Research Capsule is the `.research/` directory of a scientific Git repository.
Layout, object types, IDs, lifecycle, and acceptance rules are specified in
`docs/CAPSULE.md`.

Capsule highlights for architecture:

- Evidence lives in `.research/evidence/`.
- `.research/literature/`, `work_orders/`, and `handoffs/` are reserved and not
  parsed in R0.
- `.research/runtime/` is gitignored and created on demand by index rebuild.
- Missing typed object directories mean zero objects, not an error.

This kernel repository itself must not contain a science capsule.

## 6. Evidence directory

Evidence is a **project-local interpretation** of literature, experimental
results, or other support. It is not a paper corpus.

- Path: `.research/evidence/EVI-NNNN.yaml`
- Kinds: `literature`, `experiment`, `other`
- Qualifying evidence for terminal/accepted scientific states must be `active`;
  experiment-kind evidence additionally requires a `completed` Experiment

Withdrawn or superseded evidence may remain as references but does not qualify.

## 7. SQLite materialization

`.research/runtime/state.sqlite` is a rebuildable index of canonical files.
Files always win. There is no permissive index mode and no second state machine
in SQL.

The index records object rows, reference edges, and a
`canonical_source_digest` over scientific files only (see `docs/CAPSULE.md`).
Deleting or corrupting the database must be recoverable by `rebuild-index`
(M4) without changing YAML.

## 8. Global project registry

`project_registry.sqlite` under the data home is **noncanonical metadata** for
discovery (`project_id`, `path`, `title`, `capsule_version`, `status`,
`last_seen`).

- Deleting it must not alter project files
- Science remains in the project Git repository
- `last_seen` is updated only by `init-project` and `register-project` (M3)
- No filesystem scan of `~/research/`
- R0 doctor must not require the registry to exist

## 9. CLI-first interface

The user-facing interface is `researchctl` (stdlib `argparse`).

R0/M1 commands that exist:

- `researchctl version`
- `researchctl doctor`

R0 commands specified for later kernel milestones (not implemented in M1):

- `init-project`, `register-project`, `validate-project`, `rebuild-index`,
  `projects`, `status`

Exit codes (R0 contract): `0` success, `1` validation/project/runtime failure
(including “not a Research OS project”), `2` usage error.

Doctor checks the local environment. It must not create directories, `chmod`,
`chown`, repair the OS, require R1-shaped subdirectories, require
`config.toml` or `secrets.env`, require the project registry, or inspect
firmware.

## 10. Review-gated Claim acceptance

A Claim may be `accepted` only when all of the following hold:

1. at least one **qualifying** Evidence object is referenced;
2. at least one **qualifying human Review** exists:
   - `subject` equals the Claim ID
   - `subject_digest` equals the current project-scoped semantic digest of
     that Claim
   - `evidence_digests` covers every Evidence object the Claim currently
     links, and each stored digest equals that Evidence object's current
     digest
   - `status == concluded`
   - `verdict == approve`
   - `reviewer_kind == human`

`independent_agent` Reviews may be stored and indexed. They do **not** authorize
Claim `accepted` in R0.

Lifecycle-only Claim changes (`evidence_linked` → `accepted`) must not change
the semantic digest. Changing title, statement, supporting or contrary
evidence, the response to contrary evidence, or hypotheses must. Changing the
scientific content of an Evidence object the review examined must invalidate
the approval too, through the review's explicit `evidence_digests` map.

No agent approves its own scientific work. R0 enforces this structurally; it
does not prove reviewer identity.

## 11. Release boundaries

| Release | Role | Status |
|---|---|---|
| **R0 — Kernel** | repo, CLI, capsule, schemas, Git/SQLite state | in implementation |
| **R1 — Literature** | APIs, shared library, PDF cache, ranking, retrieval | not started |
| **R2 — Co-Explorer** | explorers, literature verification, hypothesis portfolio | not started |
| **R3 — Experimentalist** | Work Orders, execution, validation, Slurm | not started |
| **R4 — Author / Referee** | writing packets, independent review tooling | not started |
| **R5 — Autonomous OS** | watchers, MCP, cross-project exploration, optional UI | not started |

**R2 is the first major scientific-value target.** Future releases must not be
treated as if they already exist.

R0 includes Claim/Review/Evidence schemas as kernel state. R4 adds identity and
frozen review-packet tooling around that kernel.

## 12. Postponed technologies

R0 does not include and must not opportunistically add:

Docker, Podman, Apptainer, PostgreSQL, vector database servers, MCP, LangGraph,
PaperQA, local LLMs, Ollama, web UI, background systemd services, Claude Code
execution adapter, Codex adapter, Slurm, HPC abstraction, literature APIs,
embeddings, unofficial browser automation, firmware/Secure Boot/MOK automation.

Work Orders, Handoffs, and RUN records have no R0 schema.

## 13. Human versus agent authority

Humans retain authority over architectural invariants, schema freeze, scientific
lifecycle semantics, Linux package/firmware changes, credentials, provider
subscriptions, scientific ambiguity, costly experiments, acceptance of
scientific claims, manuscript framing, persistent background services, onboarding
real projects, and release declarations.

Agents may implement schemas, validators, CLI, capsule/registry/index code,
deterministic tests, and documentation **within an authorized milestone**.

Agents must stop at the authorized milestone. They must not push, merge, enable
services, or modify the host outside the repository except ordinary uv-managed
project environment operations.
