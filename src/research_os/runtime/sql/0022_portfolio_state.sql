-- Per-project portfolio status, and the periodic autonomous digest.
--
-- The closed status list below is the release-critical part of this file. An
-- individual idea may be HUMAN_READY and stay that way indefinitely; the
-- portfolio must keep researching everything else. So there is deliberately no
-- `WAIT_HUMAN` value here to reach, and capacity is computed over ideas whose
-- operational_state is ACTIVE -- of which a HUMAN_READY idea is not one,
-- because its track has ended.
create table if not exists portfolio_state (
    project_id     text        primary key references projects(project_id) on delete cascade,
    status         text        not null default 'RUNNING',

    -- The capsule digest this portfolio last reconciled against, so a charter
    -- change is noticed by comparison rather than by a model being asked.
    charter_digest text,
    detail         text,

    paused_at      timestamptz,
    paused_by      text,
    last_tick_at   timestamptz,
    last_digest_at timestamptz,

    -- Per-project overrides of the configured bounds. Empty by default: the
    -- bounds are configuration, not scientific policy, and a project that
    -- needs different ones says so here rather than in code.
    bounds         jsonb       not null default '{}'::jsonb,

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint portfolio_state_status_ck check (status in (
        'RUNNING','PAUSED_BY_RESEARCHER','PAUSED_BUDGET_EXHAUSTED',
        'PAUSED_NO_FRONTIER','PAUSED_BLOCKED_EXTERNAL')),
    constraint portfolio_state_pause_ck check (
        (status = 'RUNNING') = (paused_at is null))
);

create table if not exists portfolio_digests (
    digest_id    text        primary key,
    project_id   text        not null references projects(project_id) on delete cascade,
    period_start timestamptz not null,
    period_end   timestamptz not null,
    payload      jsonb       not null,
    artifact_id  text        references artifacts(artifact_id) on delete set null,
    created_at   timestamptz not null default now(),
    constraint portfolio_digests_period_ck check (period_end >= period_start)
);
create index if not exists portfolio_digests_project_idx
    on portfolio_digests(project_id, created_at desc);

-- Researcher seeds that have not yet become ideas.
--
-- Not an idea row, because a seed is a sentence a person typed and an idea is a
-- structured object with a falsifier. The seeded explorer consumes these; the
-- idea it produces carries origin RESEARCHER_SEED and an edge back to nothing,
-- because a seed has no lineage -- it is where lineage starts.
create table if not exists portfolio_seeds (
    seed_id      text        primary key,
    project_id   text        not null references projects(project_id) on delete cascade,
    text         text        not null,
    note         text        not null default '',
    consumed_at  timestamptz,
    consumed_by  text,
    created_at   timestamptz not null default now()
);
create index if not exists portfolio_seeds_pending_idx
    on portfolio_seeds(project_id, created_at) where consumed_at is null;
