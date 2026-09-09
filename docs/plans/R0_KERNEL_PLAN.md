# R0 Kernel — Implementation Contract

```text
Status: HUMAN-APPROVED FOR R0 IMPLEMENTATION
Approved: 2026-09-08
Scope: R0 Kernel only
```

This file is the version-controlled implementation contract for Research OS R0.
It is the complete final human-approved plan. Scientific decisions in the plan
body are frozen and must not be reinterpreted during implementation.

> **HISTORICAL NOTE (WP-A).** Milestones M1-M3 have since been implemented, and
> the WP-A scientific-integrity corrections changed parts of the schema and
> digest specification recorded below. **`docs/CAPSULE.md` is authoritative
> wherever it and this document disagree.** The sections below are preserved as
> the approved plan of record and are deliberately **not** rewritten to look
> retroactively consistent. Known superseded points: the semantic-digest
> specification in section 7 (see the marker there) and the flat Claim
> `evidence` field, now `supporting_evidence` / `contrary_evidence` /
> `contrary_evidence_addressed`.

---

# R0 Kernel — final amended implementation-ready plan

Status: **previous revision approved except the eight targeted amendments below, which this document incorporates.** Implementation of repository source is not started.

This is the complete plan, not a delta.

**STOP after this plan.** No repository implementation, installs, commits, or R1/R2 work until an explicit implement instruction.

---

## 1. Verified current repository state

Inspected `/home/nicacevedo/research/research-os` on 2026-09-08. Unchanged by planning.

**Git:** `r0/kernel-v1` clean, even with `main` / `origin/main` at `e35414f` *Bootstrap Research OS foundation*; remote `git@github.com:nicacevedo/research-os.git`.

**Substance:** [DESIGN_INVARIANTS.md](DESIGN_INVARIANTS.md) complete (do not modify). [ARCHITECTURE.md](ARCHITECTURE.md) truncated. [pyproject.toml](pyproject.toml) `0.1.0`, Python `>=3.12,<3.13`, zero dependencies, `researchctl = research_os.cli:main`. CLI: `version` and `doctor` only. Empty: `README.md`, `ROADMAP.md`, `SECURITY.md`, `MACHINE_AUDIT.md`. No tests, schemas, capsule, registry, indexer. No `AGENTS.md`.

**Runtime (do not redo machine setup):** uv 0.12.10; Python 3.12.14; `researchctl` version/doctor PASS; sqlite3 3.45.1. Doctor does not check XDG writability.

**XDG leftovers (not R0 contract):** empty `literature/`, `embeddings/`, `scheduler/`, etc. Neither require, populate, nor delete.

**Invariant conflicts in the repo:** none.

---

## 2. Gaps against R0

Docs, `AGENTS.md`, capsule, schemas, semantic digests, CLI beyond version/doctor, both SQLite stores, and tests are missing. Runtime deps still empty (PyYAML + pydantic after implement go-ahead).

---

## 3. R0 scope and explicit non-goals

**In scope:** durable kernel — docs, `AGENTS.md`, Research Capsule v1, Pydantic+YAML schemas, IDs/refs/lifecycle graphs, semantic Review binding, human-only claim acceptance, active-evidence rules, deterministic validation, CLI, rebuildable project SQLite, noncanonical registry SQLite, experiment provenance pointers, pytest/ruff.

**Out of scope:** literature APIs/corpora, explorers, LLMs, execution, containers, WO/Handoff/RUN schemas, parsing `.research/literature/`, `config.toml` parser, firmware/MOK/Secure Boot, systemd, treating `independent_agent` as an R0 acceptance authority.

---

## 4. Exact target repository tree

Kernel repo only (no science `.research/` inside `research-os`):

```text
~/research/research-os/
├── DESIGN_INVARIANTS.md
├── ARCHITECTURE.md
├── ROADMAP.md
├── SECURITY.md
├── MACHINE_AUDIT.md
├── README.md
├── AGENTS.md                     # agent-operation contract (M1)
├── docs/CAPSULE.md
├── pyproject.toml                # + PyYAML, pydantic; dev: pytest, ruff
├── uv.lock
├── .python-version
├── .gitignore
├── src/research_os/
│   ├── __init__.py
│   ├── cli.py
│   ├── errors.py
│   ├── paths.py                  # XDG + env overrides only; no config parser
│   ├── ids.py
│   ├── models.py
│   ├── digests.py                # semantic projection + SHA-256
│   ├── transitions.py
│   ├── capsule.py
│   ├── validate.py
│   ├── registry.py
│   └── index.py
└── tests/
    ├── conftest.py
    ├── test_ids.py
    ├── test_paths.py
    ├── test_models.py
    ├── test_digests.py
    ├── test_transitions.py
    ├── test_validate.py
    ├── test_capsule.py
    ├── test_registry.py
    ├── test_index.py
    ├── test_cli.py
    ├── test_isolation.py
    ├── test_integration_rebuild.py
    ├── test_failures.py
    └── fixtures/
```

**Removed from R0:** `config.py`, `tests/test_config.py`. No empty `literature/`, `agents/`, `execution/` packages.

---

## 5. Research Capsule v1 proposal

```text
.research/
├── .gitignore                    # /runtime/  (not part of source digest)
├── project.yaml
├── CHARTER.md
├── STATE.md
├── questions/                    # optional; missing = zero objects
├── ideas/
├── hypotheses/
├── assumptions/
├── claims/
├── decisions/
├── experiments/EXP-NNNN/manifest.yaml
├── reviews/
├── evidence/                     # EVI-NNNN.yaml
└── runtime/                      # on demand; gitignored
    └── state.sqlite
```

Not created and not required: `work_orders/`, `handoffs/`, `literature/`, `runtime/` (until rebuild). No `.gitkeep`. Reserved dirs with files: **WARNING**, not indexed, not in the source digest.

Init writes `.research/.gitignore` containing `/runtime/` and does **not** append to the project root `.gitignore`.

---

## 6. Canonical object/schema proposal

Shared envelope (Pydantic v2, `extra='forbid'`):

- **Required:** `id`, `type`, `schema_version` (1), `status`, `title`
- **Optional:** `notes`, `created_from` (list of IDs), `supersedes` (list of same-type IDs, default empty)
- **Forbidden:** `superseded_by`, routine timestamps, secrets, unknown keys

### Project — `project.yaml`

`id` (slug `^[a-z][a-z0-9-]{1,62}$`), `title`, `capsule_version` (1), `status` (`active` | `paused` | `archived`); optional `description`. No secrets, no HPC. Path is not identity.

### Question / Idea / Assumption

Unchanged scientifically. Question: `statement`; statuses `open|paused|answered|withdrawn|superseded`. Idea: `statement`; `draft|active|promoted|discarded|superseded`. Assumption: `statement`, `scope`; `active|relaxed|withdrawn|superseded`.

### Hypothesis

Required: `statement`. Non-draft: `falsification`. Optional: `mechanism`, `addresses`, `assumptions`, `supporting_evidence`, `contrary_evidence`, `confidence` (`0..1`), `confidence_basis` (required iff `confidence` set).

Statuses: `draft|active|testing|supported|rejected|inconclusive|withdrawn|superseded`.

**Qualifying EVI** (see Evidence rules): `supported` needs ≥1 qualifying supporting EVI; `rejected` ≥1 qualifying contrary EVI; `inconclusive` ≥1 qualifying EVI in the union of those lists.

### Claim

Required: `statement`. Optional: `evidence`, `hypotheses`. Statuses: `draft|evidence_linked|accepted|withdrawn|superseded`.

- **`evidence_linked`:** ≥1 **qualifying** EVI
- **`accepted`:** ≥1 **qualifying** EVI **and** ≥1 **qualifying human Review** (below)

### Decision

`statement`, `rationale`; `alternatives_considered` required when `accepted`. Statuses: `proposed|accepted|withdrawn|superseded`.

### Experiment

`purpose`; `hypotheses` required when not `draft`. Statuses: `draft|specified|running|completed|failed|withdrawn|superseded`. No `audited`. Completed requires `provenance.{code,config,data,git_commit}`; optional pointer-only `result_manifest`, `artifacts`.

### Review

Required: `subject` (Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|EVI — **not REV**), `reviewer_kind` (`human` | `independent_agent`).

- `human`: a person is the reviewer of record
- `independent_agent`: declared automated reviewer; **may be stored and indexed**; **does not authorize Claim `accepted` in R0**

Statuses: `draft|submitted|concluded|withdrawn`. When `concluded`: `findings`, `verdict` (`approve|reject|revise`), and **`subject_digest`** (64-char lowercase hex SHA-256) are required. Draft/submitted: `subject_digest` optional; if present it is stored but not an acceptance gate.

Reviews do not use `supersedes`. REV-of-REV: ERROR.

**Qualifying human Review** for Claim `accepted`:

- `subject` equals the Claim ID
- `subject_digest` equals the **current** semantic digest of that Claim
- `status == concluded`
- `verdict == approve`
- `reviewer_kind == human`

A concluded Review whose `subject_digest` no longer matches the current subject is **historically valid**. Emit **WARNING** `W_STALE_SUBJECT_DIGEST`. It does **not** qualify for acceptance. Changing Claim `status` (`evidence_linked` → `accepted`) must **not** change the digest.

### Evidence

Path: `evidence/EVI-NNNN.yaml`. `kind`: `literature|experiment|other`. Literature: `citation` required; optional `global_ref`, `locator`. Experiment: `experiment` EXP- ID. Other: `citation` or `notes`. Statuses: `active|withdrawn|superseded`.

**Qualifying evidence** (used by terminal/accepted scientific states):

- EVI exists and `status == active`
- if `kind == experiment`: referenced Experiment exists and `status == completed`

Withdrawn or superseded EVI may remain as references but **do not qualify**. Non-completed experiment EVI does not qualify.

### Deferred

WO, Handoff, RUN: no schema, no required directories.

---

## 7. ID, reference, lifecycle, immutability, supersession, schema version, semantic digest

### IDs (frozen)

`^(Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|REV|EVI)-[0-9]{4,}$`. Unique in-project. Immutable. Project ids are slugs.

### References

`created_from` any ID; `supersedes` list of same type; hyp `addresses`/`assumptions`/evidence lists; claim `evidence`/`hypotheses`; experiment `hypotheses`; review `subject`; EVI `experiment`. Dangling, cycles, duplicate IDs: ERROR. Validate never rewrites files.

### Semantic digest (new)

> **SUPERSEDED BY WP-A.** This subsection is historical. The digest is now
> project-scoped and carries an explicit algorithm version (`1:<64 hex>`), the
> projection includes `id` and `project`, and the Claim and Experiment key sets
> changed. See the "Semantic digest" section of `docs/CAPSULE.md`, which is
> authoritative.

Module: [src/research_os/digests.py](src/research_os/digests.py).

**Goal:** hash *scientific content reviewed*, not raw YAML and not lifecycle/admin fields.

**Algorithm**

1. Parse object to a typed model.
2. Build a JSON-compatible projection dict with a **fixed key set per type** (below). Absent optionals are JSON `null`. ID lists are de-duplicated and sorted lexicographically. Strings are the parsed Unicode values (no extra whitespace folding).
3. Serialize: `json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")`.
4. `subject_digest = hashlib.sha256(bytes).hexdigest()` (lowercase hex).

**Excluded from every projection:** `status`, `schema_version`, `notes`, `created_from`, `supersedes`, `id` (identity is Review.`subject`), unknown/admin fields.

**Included (material) by type**

- **question:** `type`, `title`, `statement`
- **idea:** `type`, `title`, `statement`
- **hypothesis:** `type`, `title`, `statement`, `falsification`, `mechanism`, `addresses`, `assumptions`, `supporting_evidence`, `contrary_evidence`, `confidence`, `confidence_basis`
- **assumption:** `type`, `title`, `statement`, `scope`
- **claim:** `type`, `title`, `statement`, `evidence`, `hypotheses`
- **decision:** `type`, `title`, `statement`, `rationale`, `alternatives_considered`, `related`
- **experiment:** `type`, `title`, `purpose`, `hypotheses`, `provenance`, `result_manifest`, `artifacts`
- **evidence:** `type`, `title`, `kind`, `statement`, `citation`, `global_ref`, `locator`, `experiment`

`provenance` is included as a sorted-key object of its four strings when present, else `null`.

Lifecycle-only Claim change `evidence_linked` → `accepted` does not change the digest. Changing `title`, `statement`, `evidence`, or `hypotheses` does.

### Lifecycle two layers

**Layer A** (`validate-project`): current-state enums + conditioned invariants. No Git mining.

**Layer B:** `is_valid_transition(type, old_status, new_status)`; same-status always valid.

**Claim graph (amended):** `draft` → `{evidence_linked, withdrawn, superseded}` only. `evidence_linked` → `{accepted, withdrawn, superseded}`. `accepted` → `{withdrawn, superseded}`. **`draft` → `accepted` is illegal.**

**Experiment retry (unchanged):** `failed` → `{specified, withdrawn, superseded}`. No extra execution semantics.

Other graphs unchanged from the previous revision (Question, Idea, Hypothesis, Assumption, Decision, Review, Evidence, Project).

### Supersession (list; split/merge)

```yaml
supersedes:
  - OLD-ID-1
  - OLD-ID-2
```

- Default empty; no duplicate IDs in the list; same type; all exist; cycles ERROR
- One object may supersede many predecessors; many successors may supersede one predecessor
- `superseded_by` is **derived only** (index `refs` reverse); never canonical YAML
- `status: superseded` requires **at least one** derived successor
- `withdrawn` = abandoned without replacement (must not be listed in any `supersedes`)
- SQLite: one `refs(from_id, to_id, rel='supersedes')` row per predecessor

### Schema versions

Package `0.1.0` until R0 accepted; `capsule_version: 1`; object `schema_version: 1`. No migrator.

> **SUPERSEDED BY WP-A.** "No migrator" is no longer absolute. WP-A corrected
> schema v1 in place because no real capsule existed; from the first persisted
> real capsule onward, material schema changes require an explicit version bump
> and a reviewable, human-run versioned migration. No migration framework is
> implemented. See the migration policy in `docs/CAPSULE.md`.

---

## 8. SQLite materialized-index design

`.research/runtime/state.sqlite`. Tables `meta`, `objects`, `refs` as before. `payload_json` may include computed `semantic_digest` as cache only.

**canonical_source_digest** includes only:

- `project.yaml`
- `CHARTER.md`
- `STATE.md`
- all indexed canonical object YAML files

**Excludes:** `.research/.gitignore`, `runtime/`, reserved unparsed dirs/files (`literature/`, `work_orders/`, `handoffs/`).

```text
canonical_source_digest = sha256(
  newline-joined UTF-8 records sorted by relative path:
    "{relative_path}:{sha256_hex_of_file_bytes}"
  plus trailing newline
)
```

`working_tree_dirty` is computed over **the same path set** as the digest (not ignore-file noise).

If dirty, `git_commit` is not a complete identity of indexed bytes.

Rebuild: validate ERROR-free → `state.sqlite.new` → checks → `os.replace`. Files win. No permissive mode.

---

## 9. Global project-registry design

Unchanged: `~/.local/share/research-os/project_registry.sqlite`; columns `project_id, path, title, capsule_version, status, last_seen`; `last_seen` only on init/register; not scientific truth; re-register after deletion.

---

## 10. CLI contract

Unchanged command set and exit codes (`0/1/2`; non-capsule path = `1`). Doctor does not require `config.toml` or registry. Init does not touch root `.gitignore`. `register-project` does not modify canonical files.

---

## 11. Configuration and security design

R0 has **no configurable settings** except XDG location overrides in [src/research_os/paths.py](src/research_os/paths.py):

- `RESEARCH_OS_CONFIG_HOME`
- `RESEARCH_OS_DATA_HOME`
- `RESEARCH_OS_CACHE_HOME`
- `RESEARCH_OS_STATE_HOME`

Defaults: DESIGN_INVARIANTS XDG paths. `~/.config/research-os/config.toml` is **reserved for later releases**; **do not implement a parser** in R0. Do not add `config.py`. Do not load `secrets.env`. Never print secrets; never touch firmware/MOK/Secure Boot/sudo/systemd. Doctor FAIL if an XDG dir exists but is unwritable.

---

## 12. Provenance boundary

Unchanged split: Git/YAML identity; index hashes + digest; completed-experiment pointers; no RUN objects. Semantic `subject_digest` is review-binding, not experiment execution provenance.

---

## 13. Minimal dependency proposal

Runtime: **PyYAML** (`safe_load` only), **pydantic** v2 `extra='forbid'`. Stdlib: argparse, sqlite3, hashlib, pathlib, json, os. Dev: pytest, ruff. **tomllib unused in R0** (no config parser). Do not add typer, platformdirs, jsonschema, ruamel, pydantic-settings.

---

## 14. Unit-test plan

Temp fixtures + XDG env overrides.

Add/keep: ID grammar; Pydantic unknown fields; confidence pairing; **every transition edge including `draft ↛ accepted`**; duplicate/dangling/circular refs; REV-of-REV; **semantic digest stable under status-only change**; digest changes when claim statement/evidence/hypotheses/title change; **stale `subject_digest` does not qualify**; **`independent_agent` approve does not qualify**; **human approve with matching digest does**; qualifying vs withdrawn/superseded EVI; experiment EVI with non-completed EXP; supersedes list split/merge (two predecessors, two successors); orphan superseded; `superseded_by` unknown-field; missing dirs; capsule gitignore vs root; registry; isolation; digest excludes `.gitignore`; dirty-tree meta; doctor writability. **No config parser tests.**

---

## 15. Integration-test plan

Same lifecycle as before, with amendments in the fixture:

- Claim path is `draft` → `evidence_linked` → `accepted` (not a one-step skip)
- Qualifying Review is `reviewer_kind: human` with `subject_digest` matching current claim digest
- `supersedes` is a list; include a merge (one object superseding two) or split (two superseding one)
- After rebuild, changing only Claim `status` keeps digest; changing `statement` then leaving `accepted` fails validate until a new Review
- Deleting `.research/.gitignore` content change does not change `canonical_source_digest` if that file is excluded (gitignore still exists from init; test that editing it does not change digest)

---

## 16. Failure-injection plan

Previous cases, **replacing** “two successors is ERROR” with “two successors is OK”; **adding**:

- `accepted` with only `independent_agent` Review → ERROR
- `accepted` with human Review whose digest is stale → ERROR
- `evidence_linked` / `accepted` / `supported` / `rejected` / `inconclusive` citing only withdrawn or superseded EVI → ERROR
- experiment-kind EVI citing `running`/`specified`/`failed` EXP used as qualifying evidence → ERROR
- `draft` → `accepted` via `is_valid_transition` → False
- `.gitignore`-only change does not alter source digest

---

## 17. Documentation changes

- **DESIGN_INVARIANTS.md** — unchanged
- **ARCHITECTURE.md** — R0 contract including human-only acceptance, semantic review binding, evidence/ vs literature/, no config parser
- **docs/CAPSULE.md** — fields, projections, digest algorithm, transition graphs, supersession lists
- **AGENTS.md** — agent-operation contract (not science). Minimum instructions: read DESIGN_INVARIANTS, ARCHITECTURE, SECURITY, and the active release plan before significant work; work only on the currently approved milestone; never `sudo` or modify host packages without explicit human approval; never modify firmware/UEFI/MOK/Secure Boot; never expose or commit secrets; Git/YAML are canonical scientific state; SQLite is rebuildable; do not implement later-release functionality opportunistically; run deterministic tests before stopping; do not push, merge, or enable persistent services unless explicitly instructed; stop after the approved milestone. No large Cursor-specific rules framework.
- **ROADMAP.md**, **SECURITY.md**, **MACHINE_AUDIT.md**, **README.md** — as previously specified; README notes reserved `config.toml` and env path overrides

---

## 18. Implementation sequence (4 milestones)

Implement only after an explicit implement instruction.

**M1:** DESIGN_INVARIANTS untouched; fill ARCHITECTURE/ROADMAP/SECURITY/MACHINE_AUDIT/README; add `AGENTS.md` + `docs/CAPSULE.md`; `uv add pydantic pyyaml`; `uv add --dev pytest ruff`; `paths.py`, `errors.py`; doctor writability. **No config module.**

**M2:** `ids.py`, `models.py`, `digests.py`, `transitions.py`, `validate.py`; unit tests (semantic digest, human-only acceptance, active evidence, supersession lists).

**M3:** capsule + registry + CLI (`init-project`, `register-project`, `validate-project`, `projects`, `status`).

**M4:** `index.py`; source digest scope; isolation; rebuild equivalence; failure injection; acceptance gates.

---

## 19. R0 acceptance/release gates

Commands on a temp git repo: `version`, `doctor`, `init-project`, `register-project`, `validate-project`, `rebuild-index`.

Also prove: digest-stable rebuild; `.gitignore` excluded from digest; isolation; stale Review digest rejects `accepted`; `independent_agent` Review does not accept; withdrawn EVI does not qualify; missing dirs OK; root gitignore untouched; registry/index disposable; non-capsule path exit `1`.

---

## 20. Risks

- Honesty hole remains: anyone can set `reviewer_kind: human` (R0 has no identity). **Mitigation:** human-only gate + `subject_digest` closes content-drift and agent-self-approve-by-schema-loophole.
- Semantic projection might omit a later-material field → capsule_version 2.
- Many-to-many supersession can create messy graphs → cycle detection + at-least-one successor.
- Transition graph unused by ordinary validate — intentional.
- No config parser — agents must not invent settings files that the kernel silently ignores except as unknown extra files outside schema.

---

## 21. Human-approval list

### Frozen (do not reopen)

Q1–Q17 and A–H from the 2026-09-08 review, **as amended by this round**:

- Q2 **amended:** R0 acceptance gate is qualifying **human** Review + qualifying EVI + matching `subject_digest`. `independent_agent` is stored, not authorizing.
- D **amended:** `supersedes` is a list; many-to-many; at least one successor.
- F **amended:** digest path set excludes `.gitignore` and unparsed reserved files.
- Residual former #2 (exactly one successor) and #7 (`draft` → `accepted` legal) are **struck**.

### Still in force from prior residual closures

1. `confidence_basis` required iff `confidence` is set
2. `last_seen` only on init/register
3. Init may create R0 typed dirs, not reserved-future dirs
4. Reviews cannot use `supersedes`
5. `failed` → `specified` allowed on the same EXP

### Closed by this amendment round (not blocking)

- Semantic projection key sets (section 7)
- Stale review = WARNING on Review, ERROR on `accepted` Claim
- Extra withdrawn EVI in a list is OK if ≥1 qualifying EVI remains
- `working_tree_dirty` uses the digest path set
- No R0 `config.toml` parser

**No remaining human decision is genuinely blocking implementation**, unless you reject one of the eight amendments just given.

---

## 22. Invariant-by-invariant compliance audit

- **1 Local before LLM — PASS.** Deterministic parse/validate/index/CLI; semantic digest is local hashing.
- **2 No continuous agents — PASS.**
- **3 Event-triggered reasoning — PASS.** Vacuous.
- **4 Project-isolated science — PASS.**
- **5 Shared literature — RISK not conflict.** Opaque `global_ref` only; do not read host or project `literature/` as kernel state.
- **6 Git-tracked files are truth — PASS.** YAML/MD canonical; `.gitignore` operational not science.
- **7 SQLite rebuildable — PASS.**
- **8 No self-approval — PASS with residual honesty risk.** Structural: Claim `accepted` requires a concluded **human** Review bound to current **semantic** `subject_digest`, plus qualifying evidence. `independent_agent` cannot accept in R0 because identity/session provenance does not exist yet. Residual: a producer can still type `reviewer_kind: human`. R4 may add identity and frozen packets. Experiment has no self-`audited` status.
- **9 Clean reviewer context — RISK.** Review YAML + digest is a frozen seed; packet tooling is R4.
- **10 Claims traceable to evidence — PASS.** `evidence_linked`/`accepted` require **active** qualifying EVI; experiment-kind requires completed EXP.
- **11 Experiment provenance — PASS at pointer level.**
- **12 API budgets — PASS (vacuous).**
- **13 No unofficial browser automation — PASS.**
- **14 Finite agent loops — PASS.** `AGENTS.md` requires stop after milestone.
- **15 Explicit cross-project transfer — PASS / RISK.** Do not scan `shared_knowledge/`.

No invariant-document change.

---

## 23. Final recommended R0 plan

On `r0/kernel-v1`, after an **explicit implement instruction**:

1. YAML capsules; Evidence in `evidence/`; optional object dirs; capsule-local `/runtime/` ignore.
2. Pydantic objects; `digests.py` semantic hashes; Layer A validate + Layer B transition function.
3. Claim acceptance = qualifying active evidence + concluded **human** Review with matching `subject_digest`.
4. `supersedes` lists for split/merge; derived reverse refs.
5. Rebuildable `state.sqlite` with digest over science files only; disposable `project_registry.sqlite`.
6. Env XDG overrides only; no config parser; `AGENTS.md` in M1; argparse CLI; four milestones.

### Contradiction check (entire plan)

- Q2 said human *or* independent_agent could accept → **amended** to human-only for R0; actor enum retained. Not a leftover contradiction.
- Former unique successor vs split/merge → **list + at-least-one successor**. Tests no longer treat two successors as failure.
- Former digest included `.gitignore` → **excluded**. `working_tree_dirty` aligned to digest paths.
- Former `draft` → `accepted` legal → **illegal** in Layer B; Layer A still does not mine Git (a YAML file that jumps to `accepted` can pass Layer A if evidence+human review+digest match, while Layer B would reject that jump if a caller supplies old status). **Not a kernel contradiction:** current-state vs historical-transition remain distinct by design (Q8). Ordinary validate will not catch the skip; the transition function and tests will. Acceptable.
- `independent_agent` exists but cannot accept → intentional, documented.
- `config.toml` reserved but unparsed → extra files in `~/.config/research-os/` are ignored except we must not treat unknown YAML in the capsule that way. Config dir is not a capsule. No contradiction if README says reserved/unused.
- Qualifying evidence `active` vs listing withdrawn IDs → allowed as non-qualifying extras; at least one qualifier required. Consistent.
- Experiment EVI requires completed EXP, while Review of an experiment can exist earlier → Review is not an evidence qualifier. Consistent.
- No `config.py` vs env vars in `paths.py` — consistent.

**New contradiction introduced:** none that block R0.

---

### Report required by this amendment round

1. **Sections changed:** 3–8, 11, 13–23, target tree (removed `config.py` / `test_config.py`; added `AGENTS.md`, `digests.py`, `test_digests.py`), Review/Claim/Evidence/Hypothesis rules, supersession, source digest, tests/failure injection, M1, invariant 8 and 10, human-approval list.
2. **New contradiction:** none blocking. Noted Layer A vs Layer B on skipped `draft`→`accepted` (pre-existing Q8 design, now with a stricter graph).
3. **Remaining blocking human decision:** **none.**
4. **Final recommendation: `READY FOR R0 IMPLEMENTATION`.** The architecture is fully specified, prior settlements are preserved except where these eight amendments explicitly override them, and no further schema question is required before M1.

---

**STOP.** Do not modify repository source. Do not install packages. Do not commit. Do not push. Do not implement R0. Do not begin R1 or R2.

Wait for an explicit instruction to implement.
