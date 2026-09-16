-- A submitted job outlives the run that submitted it.
--
-- `external_jobs.run_id` cascaded on delete, and it was the only external-effect
-- relation in the schema that did. Every other one -- `experiment_interpretations`,
-- `runtime_findings`, `runtime_proposal_links`, `runtime_proposal_reservations` --
-- uses `on delete set null`, for the reason the pattern exists: a run is a unit
-- of *work*, and deleting the work record must not delete the record of an
-- effect that happened outside this database.
--
-- An adversarial review of the interpretation relation traced where the outlier
-- led. `experiment_interpretations.job_id` cascades from `external_jobs`, which
-- cascaded from `research_runs`, so pruning one old run deleted its jobs and,
-- with them, the durable record that those experiments had been interpreted --
-- while the project, the capsule and the interpretation artifacts all survived.
-- The artifact then had nothing pointing at it and the reading it recorded had
-- no row saying it had happened. An interpretation is not derived state that
-- can be recomputed; it is the identity of one scientific reading of one
-- experiment, which is the whole reason `0006` created the table.
--
-- Deleting a *project* still removes everything, because every one of these
-- tables cascades on `project_id`. That is the intended erasure and
-- `tests/test_runtime_schema.py` proves it. What this changes is only the
-- narrower path: deleting a run now orphans its jobs from that run rather than
-- destroying them.
alter table external_jobs drop constraint if exists external_jobs_run_id_fkey;
alter table external_jobs
    add constraint external_jobs_run_id_fkey
    foreign key (run_id) references research_runs(run_id) on delete set null;
