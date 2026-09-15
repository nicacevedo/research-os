-- Durable identity for "which experiment was interpreted, and by which reader".
--
-- Before this table, `interpret_results` picked the job with the most recent
-- `finished_at`. That is association by temporal coincidence, and it is wrong
-- in every way that matters scientifically: two jobs that finish while a cycle
-- is planning give the interpretation to whichever the scheduler happened to
-- reap second; a job interpreted in cycle 3 is interpreted again in cycle 4
-- because nothing recorded that it had been; and a result can be attached to a
-- preregistration that belongs to a different experiment entirely.
--
-- The relation is immutable in the part that constitutes identity. A row is
-- claimed `IN_PROGRESS` before any reading happens, and the unique constraint
-- on `(job_id, interpreter_version)` is what makes two concurrent workers
-- resolve to one interpretation rather than two. `spec_digest` is copied onto
-- the row at claim time, so the binding between "this interpretation" and
-- "exactly this frozen specification" survives even if the job row is later
-- corrected.
--
-- `interpreter_version` is in the key rather than outside it because changing
-- how a result is read is a legitimate reason to read it again, and the second
-- reading is a *different* interpretation rather than a correction of the
-- first. Both are kept.
create table if not exists experiment_interpretations (
    interpretation_id   text        primary key,
    job_id              text        not null references external_jobs(job_id) on delete cascade,
    project_id          text        not null references projects(project_id) on delete cascade,
    run_id              text        references research_runs(run_id) on delete set null,
    work_id             text,
    spec_digest         text        not null,
    interpreter_version text        not null,
    artifact_id         text        references artifacts(artifact_id) on delete set null,
    status              text        not null default 'IN_PROGRESS',
    detail              text,
    created_at          timestamptz not null default now(),
    completed_at        timestamptz,
    constraint experiment_interpretations_status_ck check (status in (
        'IN_PROGRESS','COMPLETED','ABANDONED')),
    -- One interpretation per (experiment, reader). The whole mechanism.
    constraint experiment_interpretations_identity unique (job_id, interpreter_version),
    -- A completed interpretation has a completion time; an incomplete one does
    -- not. Half a completion is how a crashed reading looks like a finished one.
    constraint experiment_interpretations_completion_ck check (
        (status = 'COMPLETED' and completed_at is not null)
        or (status <> 'COMPLETED' and completed_at is null))
);

-- The eligibility query: terminal jobs for one project that no interpretation
-- of the current reader version has *completed*, oldest first.
create index if not exists experiment_interpretations_job_idx
    on experiment_interpretations(job_id, interpreter_version);
create index if not exists experiment_interpretations_project_idx
    on experiment_interpretations(project_id, created_at desc);
create index if not exists experiment_interpretations_run_idx
    on experiment_interpretations(run_id, created_at desc);

-- Terminal jobs, oldest first. `interpret_results` selects from this order and
-- never from `finished_at desc`: "oldest eligible" is a stable total order, and
-- "most recently finished" is a race.
create index if not exists external_jobs_terminal_idx
    on external_jobs(project_id, finished_at nulls last, job_id)
    where status in ('COMPLETED','FAILED','TIMED_OUT','CANCELLED');
