-- Whether an execution was contained, and how long it took.
--
-- `DESIGN_INVARIANTS.md` leans on containment: `.git` and `.research/` are
-- bound read-only inside the sandbox, so the repository is byte-identical.
-- That is true *when contained*, and `SandboxMode.PREFERRED` runs a command
-- uncontained when the host has no backend -- so the two cases exist and the
-- record could not tell them apart. The analysis artifact wrote
-- `job.detail` under the key `containment`, which for a local run is the
-- literal string "completed".
--
-- An invariant asserted where it should be measured is an invariant nobody
-- can check afterwards. These three columns are what makes it checkable.
-- Nullable, because every row written before this migration genuinely does
-- not know, and a default would invent an answer for them.

alter table external_jobs
    add column if not exists contained boolean;

alter table external_jobs
    add column if not exists containment text;

alter table external_jobs
    add column if not exists wall_clock_seconds numeric(12, 3);
