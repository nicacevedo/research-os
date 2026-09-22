-- The bridge an empirical idea crosses to become measured rather than argued.
--
-- The finding that forced it: every adjudicated idea both real projects have
-- produced is `empirical`. Not a classifier error -- the falsifiers ask for
-- grids to be run, bootstraps to be resampled and wall clock to be recorded.
-- The evidence stage refused all of them with "the experiment pipeline is not
-- wired into the idea track", which made `review_board`, `meta_review` and
-- `replicate` unreachable for the only kind of idea this portfolio actually
-- generates.
--
-- What this table is, and is not. It is **not** a second experiment
-- framework. `external_jobs` still records the execution, `artifacts` still
-- holds the bytes, `tool_invocations` still makes submission idempotent and
-- `budgets` still counts. This is the one thing none of them can express:
-- *which idea version asked for this measurement, and how far the asking has
-- got.* Association by "the job that finished most recently" is what
-- `sql/0006_experiment_interpretations.sql` exists because of, and doing it
-- again one layer up would repeat the mistake.
--
-- Three properties are schema rather than convention, because each one is a
-- rule the layer above would otherwise be trusted to keep:
--
--   1. one experiment per (idea version, role). An empirical idea version
--      gets exactly one primary experiment, ever. A replay that designs a
--      second one collides here rather than submitting twice.
--   2. an experiment names an idea *version*, not an idea. A revision does
--      not inherit its predecessor's measurement, which is invariant 2 of the
--      empirical brief expressed as a foreign key.
--   3. a terminal state carries what made it terminal. An experiment that is
--      `OPERATIONALLY_FAILED` has a failure class; one that is `INTERPRETED`
--      has an analysis artifact and a conclusion. Neither can be recorded
--      without the other half.
create table if not exists idea_experiments (
    experiment_id  text        primary key,
    idea_id        text        not null,
    idea_version   integer     not null,
    project_id     text        not null references projects(project_id) on delete cascade,

    -- PRIMARY settles the question; REPLICATION verifies it a second way. Two
    -- rows rather than a flag, because they are two measurements with two
    -- specifications, two jobs and two analyses.
    role           text        not null default 'PRIMARY',
    state          text        not null default 'PROPOSED',

    -- What the researcher declared, by name, and what the resolved argument
    -- vector hashed to. `experiments.yaml` lives outside every worktree, so a
    -- command nobody declared cannot appear here.
    command        text        not null,
    spec_digest    text        not null,
    -- The scientific identity of the execution with the *workspace path*
    -- removed: argv, seeds, resources, expected outputs. A replication must
    -- differ in this and not merely in where it ran, and `spec_digest` cannot
    -- express that because the disposable worktree is named for the
    -- experiment.
    variation_digest text      not null,
    workspace_path text        not null,

    -- Preregistration. `decision_rule` is the frozen, machine-checkable rule;
    -- when there is none, `no_rule_reason` says why rather than leaving the
    -- absence to be discovered when the result arrives.
    preregistration_artifact_id text references artifacts(artifact_id) on delete set null,
    decision_rule  jsonb,
    no_rule_reason text,

    -- Execution and analysis, each addressable.
    job_id         text        references external_jobs(job_id) on delete set null,
    analysis_artifact_id text  references artifacts(artifact_id) on delete set null,
    conclusion     text,
    evidence_id    text        references idea_evidence(evidence_id) on delete set null,

    failure_class  text,
    detail         text,
    attempts       integer     not null default 0,
    -- The design call, so a replication can be shown to be independent of the
    -- work it replicates the way `gates._replication_met` asks.
    origin_call_id text        references model_calls(call_id) on delete set null,

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint idea_experiments_role_ck check (role in ('PRIMARY','REPLICATION')),
    constraint idea_experiments_state_ck check (state in (
        'PROPOSED','EXECUTABLE','RUNNING','COMPLETED','OPERATIONALLY_FAILED',
        'INTERPRETED','SUPERSEDED')),
    constraint idea_experiments_conclusion_ck check (conclusion is null or conclusion in (
        'SUPPORTS','CONTRADICTS','INCONCLUSIVE','INSUFFICIENT','OPERATIONALLY_BLOCKED')),

    -- 3a. A preregistration is a rule or a stated reason there is none.
    -- Silence is the state this exists to make unreachable: an experiment
    -- whose endpoint was never fixed and whose absence nobody wrote down is
    -- one whose result can be read whichever way suits afterwards.
    constraint idea_experiments_prereg_ck check (
        decision_rule is not null or (no_rule_reason is not null and no_rule_reason <> '')),

    -- 3b. An operational failure names its class. "It did not work" without
    -- one is the shape in which an outage becomes a scientific verdict.
    constraint idea_experiments_failed_ck check (
        state <> 'OPERATIONALLY_FAILED' or (failure_class is not null and failure_class <> '')),

    -- 3c. An interpreted experiment has a conclusion and the analysis that
    -- reached it. A conclusion with no analysis artifact behind it is prose.
    constraint idea_experiments_interpreted_ck check (
        state <> 'INTERPRETED'
        or (conclusion is not null and analysis_artifact_id is not null)),

    -- 3d. Anything past PROPOSED has an execution to point at.
    constraint idea_experiments_running_ck check (
        state not in ('RUNNING','COMPLETED','INTERPRETED') or job_id is not null)
);

-- 1. One experiment per idea version per role. This is "an empirical idea
-- creates exactly one experiment spec" and "replay creates no duplicate",
-- enforced where a replay cannot argue with it.
create unique index if not exists idea_experiments_identity_idx
    on idea_experiments(idea_id, idea_version, role);
create index if not exists idea_experiments_idea_idx
    on idea_experiments(idea_id, idea_version);
create index if not exists idea_experiments_open_idx
    on idea_experiments(project_id, state)
    where state in ('PROPOSED','EXECUTABLE','RUNNING','COMPLETED');
-- The job an experiment is of, for the reverse lookup the digest and the
-- Curator need. Not unique: a job could in principle be named by a primary
-- and, after a revision superseded it, by nothing else -- but two live
-- experiments naming one job would be the temporal-coincidence defect again,
-- so it is unique over the states that are still live.
create unique index if not exists idea_experiments_job_idx
    on idea_experiments(job_id)
    where job_id is not null and state <> 'SUPERSEDED';
