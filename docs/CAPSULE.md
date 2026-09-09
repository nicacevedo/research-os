# Research Capsule v1

This is the approved Research Capsule contract for Research OS R0.
Implementation of schemas, validation, CLI capsule commands, and indexing is
**not** part of M1. This document freezes the semantics those later R0
milestones must implement.

Package version remains `0.1.0` until R0 is accepted.
`capsule_version` is `1`. Object `schema_version` is `1`.

WP-A corrected schema v1 **in place**, before any real research capsule
existed, so nothing needed migrating. Beginning with the first persisted real
capsule, scientifically material schema changes require an explicit version
bump and a reviewable migration. See **Schema-version boundaries**.

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
- **Required when `discarded`:** `retire_reason` (non-empty)
- **Optional:** `revisit_if` (string, any status)
- **Status:** `draft` | `active` | `promoted` | `discarded` | `superseded`
- Promotion = new Hypothesis with `created_from` containing this ID (not type
  mutation)
- `promoted` without a hypothesis pointing at it: **WARNING**

### Hypothesis — `hypotheses/HYP-NNNN.yaml`

- **Required extra:** `statement`
- **Required when `status != draft`:** `falsification` (non-empty)
- **Required when `rejected` or `withdrawn`:** `retire_reason` (non-empty)
- **Optional:** `mechanism`; `addresses` (Q- IDs); `assumptions` (ASM- IDs);
  `supporting_evidence` (EVI- IDs); `contrary_evidence` (EVI- IDs);
  `confidence` (YAML numeric scalar, `0 <= x <= 1`; strings and booleans
  are rejected); `confidence_basis` (string); `revisit_if` (string, any
  status)
- **Status:** `draft` | `active` | `testing` | `supported` | `rejected` |
  `inconclusive` | `withdrawn` | `superseded`

### Assumption — `assumptions/ASM-NNNN.yaml`

- **Required extra:** `statement`, `scope`
- **Status:** `active` | `relaxed` | `withdrawn` | `superseded`

### Claim — `claims/CLAIM-NNNN.yaml`

- **Required extra:** `statement`
- **Optional:** `supporting_evidence` (EVI- IDs), `contrary_evidence`
  (EVI- IDs), `contrary_evidence_addressed` (string), `hypotheses` (HYP- IDs)
- **Required when `accepted` and `contrary_evidence` is non-empty:**
  `contrary_evidence_addressed` (non-empty)
- `contrary_evidence_addressed` without `contrary_evidence`: **ERROR**
- **Status:** `draft` | `evidence_linked` | `accepted` | `withdrawn` |
  `superseded`

```yaml
supporting_evidence:
  - EVI-0001

contrary_evidence:
  - EVI-0002

contrary_evidence_addressed: >
  The contrary result applies only below 200 K.
```

There is no flat `evidence` field. Contrary evidence never satisfies the
positive evidence requirement of `evidence_linked` or `accepted`. A capsule
that still uses the pre-WP-A `evidence:` key fails loudly as `E_SCHEMA`
(`extra='forbid'`); it is never silently accepted or reinterpreted.

No evidence weights, no numerical confidence, no scoring.

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

#### Preregistration

**Required when `status` is one of `specified`, `running`, `completed`,
`failed`, `superseded`** (the *preregistered* states). `draft` and `withdrawn`
are exempt, so an under-specified draft can still be abandoned:

```yaml
predictions:
  - hypothesis: HYP-0001
    predicted_outcome: >
      Heldout RMSE falls below 0.20.
    discriminates: true

primary_metrics:
  - heldout_rmse

decision_rule: >
  Reject HYP-0001 if heldout_rmse is at least 0.20.
```

- `predictions` — non-empty list. Each entry requires `hypothesis` (a HYP- ID
  that **must** appear in this Experiment's `hypotheses`), `predicted_outcome`
  (non-empty), and `discriminates` (boolean, explicit — whether the prediction
  is intended to discriminate among competing hypotheses). `Prediction` is a
  nested value model like `Provenance`, **not** a prefixed object type: there
  is no `PRED-` ID and no `predictions/` directory.
- `primary_metrics` — at least one preregistered metric name; entries
  non-empty and unique; authored order is preserved and is material. Kept
  separate from `decision_rule` so a later release can determine
  deterministically whether a headline conclusion used a preregistered
  primary metric, without parsing prose.
- `decision_rule` — non-empty statement of how the observed primary
  metric changes support for the tested hypotheses.

No metric registry, no secondary-outcome framework, no power analysis, no
statistical-analysis DSL, no analysis-plan object.

#### Provenance

**When `completed`, required:**

```yaml
provenance:
  code: "src/experiment.py"        # repository-relative POSIX path
  config: "configs/run.toml"       # repository-relative POSIX path
  data: "s3://bucket/dataset"      # nonblank opaque locator
  git_commit: "deadbeef"           # ^[0-9a-f]{7,40}$
```

- `git_commit` — 7 to 40 lowercase hexadecimal characters.
- `code`, `config` — repository-relative POSIX paths. Absolute paths, a
  leading `~`, backslashes, Windows drive prefixes, and `.`, `..` or empty
  path segments are all **ERROR**. A single trailing slash is allowed, since a
  directory pointer is legitimate. Values are never normalized or rewritten,
  because the semantic digest hashes the literal string.
- `data` — a **nonblank opaque locator**, deliberately unrestricted. Real
  datasets routinely live outside Git and outside the project filesystem:
  scratch space, mounted institutional or HPC storage, object stores, DOIs,
  dataset identifiers, database or table references, and URLs are all valid.
  Only blankness is an error.
- **When `completed`, optional pointers only:** `result_manifest` (string),
  `artifacts` (list of strings)
- This is format validation, not verification. The kernel does not hash,
  fetch, resolve, or execute these pointers, does not check that the commit or
  any path exists, and keeps no provenance database.

### Review — `reviews/REV-NNNN.yaml`

- **Required extra:** `subject` (ID), `reviewer_kind` (`human` |
  `independent_agent`)
- **`human`:** a person is the reviewer of record
- **`independent_agent`:** declared automated reviewer; may be stored and
  indexed; does **not** authorize Claim `accepted` in R0
- **Subject types allowed:** Q, IDEA, HYP, ASM, CLAIM, DEC, EXP, EVI
- **REV-of-REV: ERROR**
- **Optional:** `findings`, `verdict` (`approve` | `reject` | `revise`),
  `subject_digest` (versioned digest, `1:<64-char lowercase hex>`),
  `evidence_digests` (map of EVI- ID to versioned digest)
- **When `concluded`:** `findings`, `verdict`, and `subject_digest` are required
- Draft/submitted: `subject_digest` optional; if present it is stored but is
  not an acceptance gate
- `evidence_digests` is valid **only** when `subject` is a Claim; every key
  must be an Evidence ID that the reviewed Claim links, in either polarity,
  otherwise **ERROR** `E_EVIDENCE_DIGEST_UNLINKED`
- **Status:** `draft` | `submitted` | `concluded` | `withdrawn`
- Reviews do **not** use `supersedes`. If `supersedes` is present on a Review:
  **ERROR**

`evidence_digests` records the digest of each Evidence object the review
actually examined:

```yaml
evidence_digests:
  EVI-0001: "1:<64 hex>"
  EVI-0002: "1:<64 hex>"
```

Reviews have **no** semantic digest of their own, because a Review is not a
reviewable subject. `evidence_digests` is therefore part of the review's
validation contract, not of any hash. See **Claim acceptance rule** for the
coverage requirement that makes a review qualify.

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
- Claim `supporting_evidence` → EVI; `contrary_evidence` → EVI;
  `hypotheses` → HYP
- Experiment `hypotheses` → HYP; each `predictions[].hypothesis` must appear
  in the same Experiment's `hypotheses`
- Review `subject` → Q | IDEA | HYP | ASM | CLAIM | DEC | EXP | EVI (not REV)
- Review `evidence_digests` keys → EVI, restricted to Evidence linked by the
  reviewed Claim
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

The same applies per evidence entry: a concluded Review holding an
`evidence_digests` value that no longer matches that Evidence object's current
digest is historically valid and emits **WARNING**
`W_STALE_EVIDENCE_DIGEST`, and it does not qualify for Claim acceptance.

`independent_agent` is an actor-class enum value, not an R0 acceptance
authority.

## Claim acceptance rule

- **`evidence_linked`:** at least one **qualifying** EVI in
  `supporting_evidence`
- **`accepted`:** at least one **qualifying** EVI in `supporting_evidence`
  **and** at least one **qualifying human Review**

`contrary_evidence` never satisfies either requirement.

**Qualifying human Review** for Claim `accepted`:

- `subject` equals the Claim ID
- `subject_digest` equals the **current** project-scoped semantic digest of
  that Claim
- `evidence_digests` covers **every** Evidence object the Claim currently
  links — the full `supporting_evidence` ∪ `contrary_evidence` set — and each
  stored digest equals that Evidence object's **current** digest
- `status == concluded`
- `verdict == approve`
- `reviewer_kind == human`

This is schema-enforced, not policy-only.

Incomplete coverage is **ERROR** `E_EVIDENCE_DIGESTS_INCOMPLETE`: an approval
that did not examine every linked Evidence object must not gate acceptance,
because the unexamined evidence could then change undetected. A digest
mismatch, on the subject or on any evidence entry, is **ERROR**
`E_STALE_REVIEW_DIGEST`.

Changing Claim `status` (`evidence_linked` → `accepted`) must **not** change
the digest.

### Known limitation: experiment-derived evidence

Evidence of `kind: experiment` references an Experiment by ID. That reference
is validated and the Experiment must be `completed` to qualify, but the
Evidence object's own digest does **not** cover the referenced Experiment's
scientific content. Changing a completed Experiment's `provenance`,
`predictions`, or `primary_metrics` therefore changes the Experiment digest
without invalidating an approval bound to the derived Evidence.

WP-A deliberately closes the demonstrated Claim → Evidence hole and stops
there. This remaining Experiment → Evidence hop must be revisited **before**
any automated promotion of experiment-derived Evidence, and before the system
relies on fully transitive Experiment → Evidence → Claim review binding.
Closing it needs either recursive digest resolution or an explicit
experiment-digest map; neither is implemented, and current Experiment ID
traceability is preserved in the meantime.

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

## Retirement memory

Ideas and hypotheses that are not currently pursued must keep their reasoning,
so the project can tell "we ruled this out" apart from "we forgot about it".

```yaml
retire_reason: >
  Existing data cannot identify the mechanism.

revisit_if: >
  Facility-level cooling telemetry becomes available.
```

- `retire_reason` is **required** for `Idea` `discarded` and for `Hypothesis`
  `rejected` or `withdrawn`
- `revisit_if` is optional and may be set at any status
- `superseded` objects are exempt: they have a successor, so they were
  replaced rather than retired
- both fields are ordinary canonical YAML and are therefore searchable and
  indexable later
- both are **excluded** from the semantic digest (see below)
- there is no automated reactivation. Revisiting a parked idea is a human act.

## Semantic digest

Module (M2): `src/research_os/digests.py`.

Hash *scientific content reviewed*, not raw YAML and not lifecycle/admin fields.

Algorithm:

1. Parse object to a typed model.
2. Build a JSON-compatible projection dict with a **fixed key set per type**
   (below), plus the `project` key. Absent scalar optionals are JSON `null`.
   Optional reference collections whose semantics are "no references"
   (`supporting_evidence`, `contrary_evidence`, `hypotheses`, `addresses`,
   `assumptions`, `related`) normalize `None` and `[]` to the same empty list.
   Remaining ID lists are de-duplicated and sorted lexicographically. Strings
   are the parsed Unicode values (no extra whitespace folding).
3. Serialize: `json.dumps(projection, ensure_ascii=False, sort_keys=True,
   separators=(",", ":"), allow_nan=False).encode("utf-8")`.
4. `subject_digest = f"{DIGEST_VERSION}:{hashlib.sha256(bytes).hexdigest()}"`.

### Digest version

A digest is `1:<64-char lowercase hex>`. The explicit version distinguishes
*changed science* from a *changed algorithm*. `DIGEST_VERSION` lives in
`models.py`, which owns the field semantics of `Review.subject_digest` and
`Review.evidence_digests`. A digest whose version is absent, unknown, or
malformed is **ERROR**; there is no dual-version reader and no digest
migration framework.

### Project scope

`semantic_projection(obj, *, project_id)` and `subject_digest(obj, *,
project_id)` both **require** a valid project slug, and the projection carries
it as `project`. Scientific object IDs such as `CLAIM-0001` are project-local,
so an otherwise identical object copied into another project must not inherit
the first project's reviewed identity; project-scoping the digest makes the
copied approval read as stale, forcing a fresh review.

Project identity is always passed explicitly. There is no global lookup, no
ambient state, and the visible ID grammar is unchanged. `validate_objects` also
requires it, so there is deliberately **no** supported library or CLI path that
validates accepted Claims while leaving project-scoped review and digest
binding disabled. `validate-project` therefore runs cross-object scientific
validation only when `project.yaml` yields a valid identity; a capsule without
one already reports a hard error, and object validation is skipped for that
pass rather than run in a weaker mode.

**Excluded from every projection:** `status`, `schema_version`, `notes`,
`created_from`, `supersedes`, `retire_reason`, `revisit_if`, unknown/admin
fields.

`retire_reason` and `revisit_if` are excluded deliberately: they record a
project decision about what to stop pursuing and when to look again, not the
scientific content a prior review approved. Retiring an Idea or Hypothesis
must not invalidate a historical approval of its science.

**Included in every projection:** `project`, plus immutable object `id`, plus
the type-specific scientific fields below. Identity is therefore bound in the
digest itself; Review.`subject` still names the reviewed object.

**Included (material) by type**

- **question:** `id`, `type`, `title`, `statement`
- **idea:** `id`, `type`, `title`, `statement`
- **hypothesis:** `id`, `type`, `title`, `statement`, `falsification`,
  `mechanism`, `addresses`, `assumptions`, `supporting_evidence`,
  `contrary_evidence`, `confidence`, `confidence_basis`
- **assumption:** `id`, `type`, `title`, `statement`, `scope`
- **claim:** `id`, `type`, `title`, `statement`, `supporting_evidence`,
  `contrary_evidence`, `contrary_evidence_addressed`, `hypotheses`
- **decision:** `id`, `type`, `title`, `statement`, `rationale`,
  `alternatives_considered`, `related`
- **experiment:** `id`, `type`, `title`, `purpose`, `hypotheses`,
  `predictions`, `primary_metrics`, `decision_rule`, `provenance`,
  `result_manifest`, `artifacts`
- **evidence:** `id`, `type`, `title`, `kind`, `statement`, `citation`,
  `global_ref`, `locator`, `experiment`

**review:** none. A Review is not a reviewable subject and has no semantic
digest.

`provenance` is included as a sorted-key object of its four strings when
present, else `null`.

`predictions` is included as an ordered list of `hypothesis`,
`predicted_outcome`, `discriminates` objects when present, else `null`.

`predictions`, `primary_metrics`, `artifacts`, and `alternatives_considered`
are **ordered** lists: they keep authored order, and `None` (absent) is
distinct from `[]` (present and empty). Only reference collections are
empty-normalized.

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
2. `capsule_version: 1` in `project.yaml` — unknown → ERROR
3. Object `schema_version: 1`

Omitting a later-material field from a semantic projection requires
`capsule_version` 2.

### Migration policy

WP-A corrected schema v1 in place because no real research capsule existed
yet, so there was nothing to migrate. From the first persisted real capsule
onward:

- scientifically material schema changes are permitted, through an **explicit
  versioned migration**;
- an initial migration may simply be a small Python script committed with
  Research OS;
- running a migration must produce an ordinary reviewable Git diff in the
  science project — canonical YAML stays the scientific record, and the change
  stays inspectable in Git history;
- no generalized migration framework, migration runner, or automatic upgrade
  on read is implemented, and none is required now;
- an unknown `capsule_version` remains a hard ERROR. Migration is a deliberate,
  human-run, reviewable act, never a silent side effect of validation.

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
