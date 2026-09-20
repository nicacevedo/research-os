-- What kind of run a `research_runs` row is.
--
-- An idea-track stage needs a run row. Three things in the kernel take a
-- non-optional `run_id` and are not worth duplicating: `BudgetLedger.
-- reserve_all` (which nests RUN under PROJECT under SYSTEM), `ModelRouter`
-- (which records every call's provenance against one), and `researchctl
-- runtime run <id>`, which is the whole per-cycle observability surface and
-- which a track gets for free by being a run.
--
-- But two kernel queries walk *every* run in a project and would act on a
-- track as though it were an objective cycle:
--
--   `parked_objectives`  -> `_work_advance_objective` would open a successor
--                           *cycle* for a finished track on every capsule
--                           change. The portfolio decides what runs next; a
--                           second scheduler deciding it as well is two
--                           schedulers.
--   `stranded_runs`      -> the reconciler would enqueue `resume_cycle`, which
--                           resumes a CycleGraph thread. A track's thread is
--                           not one.
--
-- A discriminating column rather than a convention on the objective string,
-- because the objective is free text and the two queries would be matching on
-- a prefix a model could produce. Additive, defaulted, and the default is the
-- existing behaviour, so every row written before this migration is a cycle
-- and every caller that does not know about tracks keeps working.
alter table research_runs
    add column if not exists run_kind text not null default 'cycle';
alter table research_runs drop constraint if exists research_runs_kind_ck;
alter table research_runs add constraint research_runs_kind_ck
    check (run_kind in ('cycle','idea_track'));

-- The two filtered queries' hot paths.
create index if not exists research_runs_kind_idx
    on research_runs(project_id, run_kind, created_at desc);
