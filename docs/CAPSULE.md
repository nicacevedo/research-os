# Research Capsule v1

This is the approved Research Capsule contract for Research OS R0.
Implementation of schemas, validation, CLI capsule commands, and indexing is
**not** part of M1. This document freezes the semantics those later R0
milestones must implement.

Package version remains `0.1.0` until R0 is accepted.
`capsule_version` is `1`. Object `schema_version` is `1`. There is no migrator.

## Capsule layout

A capsule is the `.research/` directory of a scientific Git repository. The
Research OS kernel repository itself does not contain a science capsule.

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

`init-project` (M3) writes `.research/.gitignore` containing `/runtime/` and
does **not** append to the project root `.gitignore`.

Not created and not required: `work_orders/`, `handoffs/`, `literature/`,
`runtime/` (until rebuild). No `.gitkeep`.

Reserved directories that contain files (`literature/`, `work_orders/`,
`handoffs/`, and other unparsed reserved paths): **WARNING**, not indexed, not
in the source digest.

Canonical science: YAML objects + `CHARTER.md` + `STATE.md` + `project.yaml`.
The CLI never rewrites existing canonical YAML except `init-project` creating a
missing capsule.

## Canonical versus runtime

| Layer | What | Canonical? |
|---|---|---|
| Git-tracked YAML and Markdown under `.research/` | scientific truth | yes |
| `.research/.gitignore` | operational ignore of runtime | not scientific; excluded from source digest |
| `.research/runtime/state.sqlite` | materialized index | no; rebuildable |
| Global `project_registry.sqlite` | discovery metadata | no; disposable |
| Reserved unparsed directories | future releases | not R0 kernel state |

Files always win over SQLite. Deleting `runtime/` or the registry must not
delete science.

## Object types

Shared envelope on every typed YAML object (Pydantic v2, `extra='forbid'`):

- **Required:** `id`, `type`, `schema_version` (1), `status`, `title` (non-empty)
- **Optional:** `notes` (string), `created_from` (list of IDs), `supersedes`
  (list of same-type IDs, default empty)
- **Forbidden:** `superseded_by` (derived only), routine timestamps, secrets,
  unknown keys

Non-empty required scientific strings must contain at least one
non-whitespace character. Validators reject whitespace-only values and do
not rewrite canonical YAML.

No timestamps as scientific provenance. Git is history.

### Project — `project.yaml`

- **Required:** `id` (slug `^[a-z][a-z0-9-]{1,62}$`), `title`,
  `capsule_version` (1), `status` (`active` | `paused` | `archived`)
- **Optional:** `description`
- **No secrets, no HPC blocks.** Path is not part of identity.

### Question — `questions/Q-NNNN.yaml`

- **Required extra:** `statement`
- **Status:** `open` | `paused` | `answered` | `withdrawn` | `superseded`

### Idea — `ideas/IDEA-NNNN.yaml`

- **Required extra:** `statement`
- **Status:** `draft` | `active` | `promoted` | `discarded` | `superseded`
- Promotion = new Hypothesis with `created_from` containing this ID (not type
  mutation)
- `promoted` without a hypothesis pointing at it: **WARNING**

### Hypothesis — `hypotheses/HYP-NNNN.yaml`

- **Required extra:** `statement`
- **Required when `status != draft`:** `falsification` (non-empty)
- **Optional:** `mechanism`; `addresses` (Q- IDs); `assumptions` (ASM- IDs);
  `supporting_evidence` (EVI- IDs); `contrary_evidence` (EVI- IDs);
  `confidence` (YAML numeric scalar, `0 <= x <= 1`; strings and booleans
  are rejected); `confidence_basis` (string)
- **Status:** `draft` | `active` | `testing` | `supported` | `rejected` |
  `inconclusive` | `withdrawn` | `superseded`

### Assumption — `assumptions/ASM-NNNN.yaml`

- **Required extra:** `statement`, `scope`
- **Status:** `active` | `relaxed` | `withdrawn` | `superseded`

### Claim — `claims/CLAIM-NNNN.yaml`

- **Required extra:** `statement`
- **Optional:** `evidence` (EVI- IDs), `hypotheses` (HYP- IDs)
- **Status:** `draft` | `evidence_linked` | `accepted` | `withdrawn` |
  `superseded`

### Decision — `decisions/DEC-NNNN.yaml`

- **Required extra:** `statement`, `rationale`
- **Required when `accepted`:** `alternatives_considered` (list of strings,
  min 1)
- **Optional:** `related` (IDs)
- **Status:** `proposed` | `accepted` | `withdrawn` | `superseded`

### Experiment — `experiments/EXP-NNNN/manifest.yaml`

- **Required extra:** `purpose`
- **Required when `status != draft`:** `hypotheses` (non-empty HYP- list)
- **Status:** `draft` | `specified` | `running` | `completed` | `failed` |
  `withdrawn` | `superseded`
- **No `audited` status.** Audit lives on Review.
- **When `completed`, required:**

```yaml
provenance:
  code: "<pointer>"
  config: "<pointer>"
  data: "<pointer>"
  git_commit: "<commit>"
```

- **When `completed`, optional pointers only:** `result_manifest` (string),
  `artifacts` (list of strings)
- Kernel does not hash, fetch, or execute these pointers in R0

### Review — `reviews/REV-NNNN.yaml`

- **Required extra:** `subject` (ID), `reviewer_kind` (`human` |
  `independent_agent`)
- **`human`:** a person is the reviewer of record
- **`independent_agent`:** declared automated reviewer; may be stored and
  indexed; does **not** authorize Claim `accepted` in R0
- **Subject types allowed:** Q, IDEA, HYP, ASM, CLAIM, DEC, EXP, EVI
- **REV-of-REV: ERROR**
- **Optional:** `findings`, `verdict` (`approve` | `reject` | `revise`),
  `subject_digest` (64-char lowercase hex SHA-256)
- **When `concluded`:** `findings`, `verdict`, and `subject_digest` are required
- Draft/submitted: `subject_digest` optional; if present it is stored but is
  not an acceptance gate
- **Status:** `draft` | `submitted` | `concluded` | `withdrawn`
- Reviews do **not** use `supersedes`. If `supersedes` is present on a Review:
  **ERROR**

### Evidence — `evidence/EVI-NNNN.yaml`

- Project-local interpretation of evidence. Not a paper corpus.
- **Required extra:** `kind` (`literature` | `experiment` | `other`),
  `statement`
- **If `literature`:** required `citation`; optional `global_ref` (opaque,
  unresolved); optional `locator`
- **If `experiment`:** required `experiment` (EXP- ID). The `experiment`
  pointer may be present only when `kind == experiment`; it is invalid on
  `literature` and `other`.
- **If `other`:** at least one of `citation` or `notes` non-empty
- **Status:** `active` | `withdrawn` | `superseded`
- Boundary: global literature object (R1) → project-local Evidence (R0) →
  Hypothesis / Claim / Decision
- No `PAPER-` objects. `.research/literature/` is reserved for R1 and is not
  parsed in R0

## IDs

```text
^(Q|IDEA|HYP|ASM|CLAIM|DEC|EXP|REV|EVI)-[0-9]{4,}$
```

- Uppercase prefixes; at least four zero-padded digits; `HYP-10000` allowed
- Unique within a project; filename/dir stem equals ID
- Immutable; never UUIDs or SQLite rowids
- Project `id`: slug, independent of path, unique in the machine registry

### References

Allowed edges (ERROR if missing or wrong type):

- `created_from` → any existing object ID
- `supersedes` → list of same type
- Hypothesis `addresses` → Q; `assumptions` → ASM; evidence lists → EVI
- Claim `evidence` → EVI; `hypotheses` → HYP
- Experiment `hypotheses` → HYP
- Review `subject` → Q | IDEA | HYP | ASM | CLAIM | DEC | EXP | EVI (not REV)
- Evidence `experiment` → EXP

Dangling, forward-as-missing, duplicate IDs, `created_from` cycles,
`supersedes` cycles: **ERROR**. Validate never rewrites files.

Severities: `ERROR` (exit 1), `WARNING` (exit 0 if no errors), `INFO`.

## Evidence semantics

**Qualifying evidence** (used by terminal/accepted scientific states):

- EVI exists and `status == active`
- if `kind == experiment`: referenced Experiment exists and `status == completed`

Withdrawn or superseded EVI may remain as references but **do not qualify**.
Non-completed experiment EVI does not qualify.

Extra withdrawn EVI in a list is allowed if at least one qualifying EVI remains.

## Review semantics

A concluded Review whose `subject_digest` no longer matches the current subject
is **historically valid**. Emit **WARNING** `W_STALE_SUBJECT_DIGEST`. It does
**not** qualify for Claim acceptance.

`independent_agent` is an actor-class enum value, not an R0 acceptance
authority.

## Claim acceptance rule

- **`evidence_linked`:** at least one **qualifying** EVI
- **`accepted`:** at least one **qualifying** EVI **and** at least one
  **qualifying human Review**

**Qualifying human Review** for Claim `accepted`:

- `subject` equals the Claim ID
- `subject_digest` equals the **current** semantic digest of that Claim
- `status == concluded`
- `verdict == approve`
- `reviewer_kind == human`

This is schema-enforced, not policy-only.

Changing Claim `status` (`evidence_linked` → `accepted`) must **not** change
the digest.

## Hypothesis confidence semantics

`confidence` is subjective research confidence, not a calibrated probability
unless a human documents otherwise. Not required. Not copied onto other types.

Coupling: if `confidence` is set, `confidence_basis` is required and non-empty;
if `confidence_basis` is set without `confidence`: **ERROR**.

Evidence rules (current-state), using **qualifying** EVI:

- `supported` → at least one qualifying `supporting_evidence` EVI
- `rejected` → at least one qualifying `contrary_evidence` EVI
- `inconclusive` → at least one qualifying EVI in the union of those lists

Non-draft hypotheses require `falsification`.

## Semantic digest

Module (M2): `src/research_os/digests.py`.

Hash *scientific content reviewed*, not raw YAML and not lifecycle/admin fields.

Algorithm:

1. Parse object to a typed model.
2. Build a JSON-compatible projection dict with a **fixed key set per type**
   (below). Absent scalar optionals are JSON `null`. Optional reference
   collections whose semantics are "no references" (`evidence`, `hypotheses`,
   `addresses`, `assumptions`, `supporting_evidence`, `contrary_evidence`,
   `related`) normalize `None` and `[]` to the same empty list. Remaining ID
   lists are de-duplicated and sorted lexicographically. Strings are the
   parsed Unicode values (no extra whitespace folding).
3. Serialize: `json.dumps(projection, ensure_ascii=False, sort_keys=True,
   separators=(",", ":"), allow_nan=False).encode("utf-8")`.
4. `subject_digest = hashlib.sha256(bytes).hexdigest()` (lowercase hex).

**Excluded from every projection:** `status`, `schema_version`, `notes`,
`created_from`, `supersedes`, unknown/admin fields.

**Included in every projection:** immutable object `id`, plus the
type-specific scientific fields below. Identity is therefore bound in the
digest itself; Review.`subject` still names the reviewed object.

**Included (material) by type**

- **question:** `id`, `type`, `title`, `statement`
- **idea:** `id`, `type`, `title`, `statement`
- **hypothesis:** `id`, `type`, `title`, `statement`, `falsification`,
  `mechanism`, `addresses`, `assumptions`, `supporting_evidence`,
  `contrary_evidence`, `confidence`, `confidence_basis`
- **assumption:** `id`, `type`, `title`, `statement`, `scope`
- **claim:** `id`, `type`, `title`, `statement`, `evidence`, `hypotheses`
- **decision:** `id`, `type`, `title`, `statement`, `rationale`,
  `alternatives_considered`, `related`
- **experiment:** `id`, `type`, `title`, `purpose`, `hypotheses`, `provenance`,
  `result_manifest`, `artifacts`
- **evidence:** `id`, `type`, `title`, `kind`, `statement`, `citation`,
  `global_ref`, `locator`, `experiment`

`provenance` is included as a sorted-key object of its four strings when
present, else `null`.

## Transition graphs

Two layers:

**Layer A** (`validate-project`): current-state enums + conditioned invariants.
No Git mining.

**Layer B:** `is_valid_transition(type, old_status, new_status)`; same-status
always valid. Not invoked by ordinary validate.

**Question:** `open` ↔ `paused`; `{open,paused}` → `{answered,withdrawn,superseded}`;
`answered` → `{withdrawn,superseded}`; terminals otherwise.

**Idea:** `draft` → `{active,discarded,superseded}`; `active` →
`{promoted,discarded,superseded}`; `promoted` → `{superseded}`; terminals
otherwise.

**Hypothesis:** `draft` → `{active,withdrawn,superseded}`; `active` →
`{testing,withdrawn,superseded}`; `testing` →
`{supported,rejected,inconclusive,active,withdrawn,superseded}`;
`{supported,rejected,inconclusive}` → `{withdrawn,superseded}` only.

**Assumption:** `active` → `{relaxed,withdrawn,superseded}`; `relaxed` →
`{withdrawn,superseded}`.

**Claim:** `draft` → `{evidence_linked, withdrawn, superseded}` only.
`evidence_linked` → `{accepted, withdrawn, superseded}`.
`accepted` → `{withdrawn, superseded}`.
**`draft` → `accepted` is illegal.**

A YAML file that is already `accepted` can still pass Layer A if evidence +
human review + matching digest hold. Layer B rejects the skip if a caller
supplies the old status. Current-state vs historical-transition remain distinct
by design.

**Decision:** `proposed` → `{accepted,withdrawn,superseded}`; `accepted` →
`{withdrawn,superseded}`.

**Experiment:** `draft` → `{specified,withdrawn,superseded}`; `specified` →
`{running,failed,withdrawn,superseded}`; `running` →
`{completed,failed,specified,withdrawn,superseded}`; `failed` →
`{specified,withdrawn,superseded}` (re-specify the same ID after failure);
`completed` → `{withdrawn,superseded}` only.

**Review:** `draft` → `{submitted,withdrawn}`; `submitted` →
`{concluded,draft,withdrawn}`; `concluded` → `{withdrawn}` only.

**Evidence:** `active` → `{withdrawn,superseded}`.

**Project:** `active` ↔ `paused`; `{active,paused}` → `archived`; `archived` →
`{active,paused}`.

No mega-lifecycle across types.

## Supersession

On the replacement object:

```yaml
supersedes:
  - OLD-ID-1
  - OLD-ID-2
```

- Default empty; no duplicate IDs in the list; same type; all exist; cycles ERROR
- Every referenced predecessor must have `status == superseded`
- One object may supersede many predecessors; many successors may supersede one
  predecessor
- `superseded_by` is **derived only** (index `refs` reverse); never canonical YAML
- `status: superseded` requires **at least one** derived successor
- `withdrawn` = abandoned without replacement (must not be listed in any
  `supersedes`)
- SQLite: one `refs(from_id, to_id, rel='supersedes')` row per predecessor

Physical delete remains a Git/user action; dangling refs ERROR. No
`researchctl delete`.

## Schema-version boundaries

1. Package version (`researchctl version`) — stay `0.1.0` until R0 accepted
2. `capsule_version: 1` in `project.yaml` — unknown → ERROR, no migrator
3. Object `schema_version: 1`

Omitting a later-material field from a semantic projection requires
`capsule_version` 2.

## Source digest

`.research/runtime/state.sqlite` (M4) stores `canonical_source_digest`.

**Includes only:**

- `project.yaml`
- `CHARTER.md`
- `STATE.md`
- all indexed canonical object YAML files

**Excludes:** `.research/.gitignore`, `runtime/`, reserved unparsed dirs/files
(`literature/`, `work_orders/`, `handoffs/`).

```text
canonical_source_digest = sha256(
  newline-joined UTF-8 records sorted by relative path:
    "{relative_path}:{sha256_hex_of_file_bytes}"
  plus trailing newline
)
```

`working_tree_dirty` is computed over **the same path set** as the digest.
If dirty, `git_commit` is not a complete identity of indexed bytes.

Rebuild: validate ERROR-free → `state.sqlite.new` → checks → `os.replace`.
Files win. No permissive mode.

## Reserved and deferred objects

No R0 schema and no required directories for:

- Work Order
- Handoff
- RUN / provenance execution records

`.research/literature/` is reserved for R1 curation and is not parsed in R0.
`global_ref` on literature Evidence is opaque and unresolved in R0.
