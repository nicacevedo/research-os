# Research OS Architecture

Research OS is a local-first scientific orchestration system for reproducible,
evidence-grounded, auditable, and selectively autonomous research.

Its purpose is not to replace scientific judgment. Its purpose is to make
scientific exploration, literature synthesis, experimentation, validation, and
writing easier to reproduce, inspect, and coordinate.

`DESIGN_INVARIANTS.md` is the architectural constitution. This document describes
system boundaries and responsibilities. `docs/CAPSULE.md` is the Research Capsule
v1 contract and is authoritative on anything scientific. `docs/plans/` holds
historical, superseded plans of record; `docs/CAPSULE.md` wins wherever they
disagree.

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
| Global project registry | `~/.local/share/research-os/project_registry.json` | no; disposable metadata |
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
- `.research/runtime/` is reserved, gitignored scratch space. Nothing in the
  kernel writes there.
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

## 7. Global project registry

`project_registry.json` under the data home is **noncanonical metadata** for
discovery (`project_id`, `path`, `title`, `capsule_version`, `status`,
`last_seen`). Canonical Git-tracked files under `.research/` are the only
project scientific state: R0 materializes no project index, and the kernel
depends on no database.

- Deleting it must not alter project files
- Science remains in the project Git repository
- `last_seen` is updated only by `init-project` and `register-project`
- No filesystem scan of `~/research/`
- Doctor must not require the registry to exist
- Writes replace the whole file atomically (temp file in the same directory,
  then `os.replace`), so an interrupted write leaves the previous store intact
- The store is plain JSON and enforces no shape of its own, so everything read
  back is validated structurally; a corrupt store is a clean error, never a
  partially-trusted registry

## 8. CLI-first interface

The user-facing interface is `researchctl` (stdlib `argparse`).

R0 commands:

- `researchctl version`
- `researchctl doctor`
- `researchctl init-project [path]`
- `researchctl register-project [path]`
- `researchctl validate-project [path]`
- `researchctl projects`
- `researchctl status [path]`
- `researchctl digest <OBJECT-ID> [path]`
- `researchctl review <CLAIM-ID> [path]`

`digest` prints the current project-scoped semantic digest of one object, so a
researcher never hand-computes one. `review` records a human Review of a Claim
(see section 9). There is no `researchctl set-status` and no
`researchctl delete`: changing a Claim's status is a deliberate, reviewable edit
to canonical YAML.

Exit codes (R0 contract): `0` success, `1` validation/project/runtime failure
(including “not a Research OS project”), `2` usage error.

Doctor checks the local environment: the running Python version, `git` on
`PATH`, and that the four Research OS XDG directories exist, are directories,
and are writable. It must not create directories, `chmod`, `chown`, repair the
OS, require R1-shaped subdirectories, require `config.toml` or `secrets.env`,
require the project registry, or inspect firmware. It tests only what the
kernel actually needs.

## 9. Review-gated Claim acceptance

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
the approval too, through the review's explicit `evidence_digests` map, and so
must changing an Experiment that evidence rests on, through the review's
explicit `experiment_digests` map. Both maps are flat and inspectable; neither
digest is resolved recursively.

`researchctl review` is the supported way to record that approval. It renders
the Claim, every linked Evidence object with its source pointers, every
Experiment that evidence rests on with all of its digest-material content, and
the exact digests the Review will bind, then requires an explicit verdict, findings, and
confirmation from an interactive terminal. It never changes the Claim's status.

No agent approves its own scientific work. R0 enforces this structurally through
the schema and the acceptance gate; it does not prove reviewer identity. The
interactive-terminal requirement is a usability and safety guard, not
authentication: the binding rule that agents must not record human Reviews lives
in `AGENTS.md`.

## 10. Release boundaries

| Release | Role | Status |
|---|---|---|
| **R0 — Kernel** | repo, CLI, capsule, schemas, Git-tracked canonical state | complete and frozen |
| **R1 — Literature** | scholarly APIs, shared index, PDF cache, BM25 retrieval | in v1 (`researchctl lit`) |
| **R2 — Co-Explorer** | read-only analyst, structured proposals, cross-project insights | in v1 (`propose`, `insight`) |
| **R3 — Experimentalist** | declared commands, local and Slurm execution, evidence packets | in v1 (`experiment`) |
| **R4 — Author / Referee** | source packets, grounded drafting, independent writing review | in v1 (`paper`) |
| **R5 — Autonomous OS** | watchers, MCP, background services, optional UI | not started |

Research OS v1 delivers the working core of R1 through R4, orchestrated by
`researchctl research`. What it deliberately does not deliver is R5: there are
no watchers, no background services, no MCP, and no UI. Nothing runs unless a
person runs it.

R0 includes Claim/Review/Evidence schemas as kernel state, and v1 did not
change them. The writing layer builds review packets around that kernel rather
than reimplementing its acceptance rule: `paper` calls the validator's own
`claim_approval()`, so there is exactly one definition of what "accepted" means.

## 11. Automation control plane

The R0 scientific kernel is frozen. Alongside it, and outside it, sits an
explicitly human-authorised automation control plane
(`research_os.automation`, `researchctl auto`). It is orchestration, not
science: a deterministic Python controller that plans bounded work with a
read-only model, dispatches a coding agent into an isolated Git worktree, runs
acceptance commands itself, obtains an independent read-only review, and stops
at `READY_FOR_HUMAN`.

It holds to every invariant in `DESIGN_INVARIANTS.md`. In particular it never
writes a capsule file, never authors a Review, never accepts a Claim, never
merges, and never pushes. Its runtime state lives under the state home and is
disposable. `docs/AUTOMATION_MVP.md` is its live specification.

It is scoped to trusted local repositories. Running a project's acceptance
checks runs that project's code — including code a worker has just modified —
with the permissions of the `researchctl` process. Worktree isolation protects
the canonical checkout; it is not an OS sandbox, and no network or process
sandboxing is provided. `docs/AUTOMATION_MVP.md` states that boundary in full.

Work Orders, Handoffs, and RUN records still have no *scientific* schema. The
runtime work-order and run records the control plane keeps are orchestration
state under `~/.local/state/research-os/`, not capsule objects.

## 11a. Research runs

Above the automation control plane, and equally outside the kernel, sits the
unified research layer (`research_os.research`, `researchctl research`). It
turns one research goal into a bounded DAG of typed tasks and dispatches each to
the controller that already owns that kind of work.

It owns no worker. A `code` or `analysis` task becomes an automation run with a
supplied plan, and therefore inherits that layer's worktree isolation, scope
enforcement from the observed diff, command policy, acceptance checks,
independent review and single bounded repair without reimplementation. A
`proposal`, `experiment` or `paper` task is delegated the same way.

What this layer owns is dispatch, ordering, budgeting and stopping:

- `TaskKind` is a closed enum and dispatch is a table with no default branch.
  A kind with no handler is refused when the plan is validated.
- A plan is a forward-only DAG, so declaration order is a valid execution order
  and a cycle cannot be expressed.
- Model calls, write tasks, experiments, cluster submissions and wall clock are
  separately budgeted, checked before the spend, against the value on disk.
- A `human_checkpoint` halts the run until a person answers; declining ends it.
- A crashed run becomes `INTERRUPTED` rather than a stale `EXECUTING`, and only
  `researchctl research resume` moves it.

`docs/RESEARCH.md` is its live specification; `docs/OPERATIONS.md` covers
`doctor`, `storage` and recovery.

## 11b. Untrusted text

Two kinds of text in this system were written by something that is not the
controller, and both are handled as data rather than instruction.

**Model-originated strings** pass through one serializer
(`research_os.automation.promptdata`) on their way into any later prompt. Every
delimiter it knows about is inert inside every block it renders, and a rendered
block is re-read before it is returned, so an ambiguous fence is a failure
rather than an escape.

**Retrieved external content** — a paper's abstract, a fetched full text — is
quoted behind a fence that says so in the delimiter itself, because its author
has never heard of this system and a paper about prompt injection contains
prompt injections as its subject matter. Raw external content never reaches a
write- or execute-enabled worker directly.

A third boundary, the display, escapes control characters on the way to a
terminal (`research_os.textsafe`). Archives keep the raw bytes; machine-readable
JSON is ASCII-escaped. The three boundaries agree about which characters are
dangerous and differ only in what they do about them.

## 12. Postponed technologies

Research OS does not include and must not opportunistically add:

Docker, Podman, Apptainer, PostgreSQL, vector database servers, MCP, LangGraph,
PaperQA, local LLMs, Ollama, web UI, background systemd services, embeddings,
unofficial browser automation, firmware/Secure Boot/MOK automation, automatic
merge, and automatic scientific acceptance.

Two items left this list in v1, and how they arrived matters. **Literature
APIs** are three plain HTTP clients over OpenAlex, Crossref and arXiv with a
SQLite FTS5 index — no embeddings, no vector store, no framework. **Slurm** is
`sbatch`/`squeue`/`sacct` behind an abstraction thin enough to read in one
sitting, invoked over `ssh <alias>` with no credential handling of its own.
Neither brought a dependency of any size, which was the condition for adding
them at all.

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
