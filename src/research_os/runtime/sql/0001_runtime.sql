-- Research OS autonomous runtime: operational state.
--
-- Nothing in this schema is scientific truth. A question, a hypothesis, a
-- claim, an accepted verdict -- none of them are here, and adding them later
-- would be the single change that breaks the system's authority model. What is
-- here is the operational record: what the runtime is doing, what it owes, what
-- it spent, what it already did once and must not do twice.
--
-- Status columns are `text` with a check constraint rather than PostgreSQL
-- enums. Enums are painful to extend inside a transaction and the Python
-- StrEnum is the real source of truth; `tests/test_runtime_schema.py` asserts
-- the two agree, so drift is a test failure rather than a discovery in
-- production.

create table if not exists schema_migrations (
    version     text        primary key,
    checksum    text        not null,
    applied_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- projects --
-- A mirror of the kernel's project identity, not a replacement for it. The
-- capsule on disk remains canonical; this row exists so runtime rows have
-- something to reference and so the daemon can find the repository.
create table if not exists projects (
    project_id  text        primary key,
    repo_path   text        not null,
    title       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- ----------------------------------------------------------- research_runs --
-- One bounded research cycle. Bounded is the operative word: a cycle ends, and
-- if more work is warranted it starts a *new* cycle with `parent_run_id` set.
-- There is deliberately no way to express "run forever".
create table if not exists research_runs (
    run_id          text        primary key,
    project_id      text        not null references projects(project_id) on delete cascade,
    objective       text        not null,
    status          text        not null default 'CREATED',
    terminal_state  text,
    autonomy        text        not null default 'high',
    parent_run_id   text        references research_runs(run_id) on delete set null,
    cycle_index     integer     not null default 0 check (cycle_index >= 0),
    thread_id       text        unique,
    detail          text,
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    updated_at      timestamptz not null default now(),
    constraint research_runs_status_ck check (status in (
        'CREATED','RUNNING','WAITING_EXTERNAL','WAITING_HUMAN','BLOCKED',
        'SUCCEEDED','FAILED','CANCELLED')),
    constraint research_runs_terminal_ck check (terminal_state is null or terminal_state in (
        'DONE_FOR_NOW','WAITING_FOR_SCIENTIFIC_DECISION','WAITING_FOR_EXTERNAL_DEPENDENCY',
        'BUDGET_EXHAUSTED','FATAL_INFRASTRUCTURE_ERROR','CANCELLED')),
    constraint research_runs_autonomy_ck check (autonomy in ('low','medium','high')),
    -- A run is not its own parent. Deeper cycles are prevented by the lineage
    -- depth check in Python; this catches the one case SQL can catch cheaply.
    constraint research_runs_lineage_ck check (parent_run_id is null or parent_run_id <> run_id)
);
create index if not exists research_runs_project_idx on research_runs(project_id, created_at desc);
create index if not exists research_runs_status_idx on research_runs(status) where status not in ('SUCCEEDED','FAILED','CANCELLED');

-- -------------------------------------------------------------- work_items --
-- The durable queue. Claimed with `for update skip locked`, held by a lease,
-- and recoverable when the holder dies without saying so.
create table if not exists work_items (
    work_id          text        primary key,
    run_id           text        references research_runs(run_id) on delete cascade,
    project_id       text        not null references projects(project_id) on delete cascade,
    kind             text        not null,
    payload          jsonb       not null default '{}'::jsonb,
    status           text        not null default 'PENDING',
    priority         integer     not null default 100,
    scheduled_at     timestamptz not null default now(),
    attempts         integer     not null default 0 check (attempts >= 0),
    max_attempts     integer     not null default 3 check (max_attempts >= 1),
    lease_owner      text,
    lease_expires_at timestamptz,
    -- Two producers asking for the same work must produce one row. The unique
    -- index is the whole mechanism; `on conflict do nothing` is the whole API.
    dedup_key        text        unique,
    failure_class    text,
    last_error       text,
    result           jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    constraint work_items_status_ck check (status in (
        'PENDING','LEASED','SUCCEEDED','FAILED','CANCELLED','BLOCKED','WAITING')),
    -- A leased row has an owner and a deadline; an unleased row has neither.
    -- Half a lease is how double execution starts.
    constraint work_items_lease_ck check (
        (status = 'LEASED' and lease_owner is not null and lease_expires_at is not null)
        or (status <> 'LEASED' and lease_owner is null and lease_expires_at is null))
);
-- The claim query's index: due, runnable, best first.
create index if not exists work_items_claimable_idx
    on work_items(priority, scheduled_at, work_id)
    where status = 'PENDING';
create index if not exists work_items_lease_idx
    on work_items(lease_expires_at)
    where status = 'LEASED';
create index if not exists work_items_run_idx on work_items(run_id, created_at);

-- ------------------------------------------------------------------ events --
-- Append-only. Nothing updates an event except the consumer stamping
-- `consumed_at`, and nothing ever deletes one inside a run's lifetime.
create table if not exists events (
    event_id    text        primary key,
    project_id  text        references projects(project_id) on delete cascade,
    run_id      text        references research_runs(run_id) on delete cascade,
    work_id     text,
    kind        text        not null,
    payload     jsonb       not null default '{}'::jsonb,
    dedup_key   text        unique,
    created_at  timestamptz not null default now(),
    consumed_at timestamptz
);
create index if not exists events_unconsumed_idx on events(created_at, event_id) where consumed_at is null;
create index if not exists events_run_idx on events(run_id, created_at desc);
create index if not exists events_kind_idx on events(kind, created_at desc);

-- --------------------------------------------------------------- approvals --
-- A human scientific gate, with the decision packet that was prepared for it.
-- `interrupt_key` ties the row to the LangGraph interrupt it is answering, so
-- resuming twice with one decision cannot apply it twice.
create table if not exists approvals (
    approval_id   text        primary key,
    run_id        text        not null references research_runs(run_id) on delete cascade,
    project_id    text        not null references projects(project_id) on delete cascade,
    kind          text        not null,
    question      text        not null,
    packet        jsonb       not null default '{}'::jsonb,
    status        text        not null default 'PENDING',
    decision      jsonb,
    decided_by    text,
    thread_id     text,
    interrupt_key text        unique,
    requested_at  timestamptz not null default now(),
    decided_at    timestamptz,
    applied_at    timestamptz,
    constraint approvals_status_ck check (status in ('PENDING','GRANTED','DECLINED','EXPIRED','APPLIED'))
);
create index if not exists approvals_pending_idx on approvals(requested_at) where status = 'PENDING';
create index if not exists approvals_run_idx on approvals(run_id, requested_at desc);

-- -------------------------------------------------------- tool_invocations --
-- The idempotency ledger, and the reason this runtime can be crashed safely.
--
-- Every externally visible side effect is claimed here *before* it happens and
-- completed here after. A retry that finds a COMPLETED row reuses its result
-- and emits nothing. A retry that finds an IN_FLIGHT row from a dead worker
-- finds a side effect whose outcome is unknown, which is a different and more
-- dangerous thing, and is handled by an explicit reconciler rather than by
-- hoping.
create table if not exists tool_invocations (
    invocation_id   text        primary key,
    idempotency_key text        not null unique,
    run_id          text        references research_runs(run_id) on delete cascade,
    work_id         text,
    kind            text        not null,
    request         jsonb       not null default '{}'::jsonb,
    status          text        not null default 'IN_FLIGHT',
    result          jsonb,
    error           text,
    attempts        integer     not null default 1 check (attempts >= 1),
    owner           text,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    constraint tool_invocations_status_ck check (status in ('IN_FLIGHT','COMPLETED','FAILED','ABANDONED'))
);
create index if not exists tool_invocations_inflight_idx on tool_invocations(started_at) where status = 'IN_FLIGHT';
create index if not exists tool_invocations_run_idx on tool_invocations(run_id, started_at desc);

-- ------------------------------------------------------------- model_calls --
-- Provenance for every model invocation, including the independence group, so
-- "this was independently reviewed" is a claim a person can audit rather than
-- one the system asserts about itself.
create table if not exists model_calls (
    call_id            text        primary key,
    run_id             text        references research_runs(run_id) on delete cascade,
    work_id            text,
    invocation_id      text        references tool_invocations(invocation_id) on delete set null,
    provider           text        not null,
    model              text,
    role               text        not null,
    criticality        text        not null default 'normal',
    independence_group text,
    prompt_version     text,
    input_digest       text,
    output_artifact_id text,
    tokens_in          integer,
    tokens_out         integer,
    cost_usd           numeric(12, 6),
    latency_ms         integer,
    status             text        not null default 'OK',
    error              text,
    created_at         timestamptz not null default now(),
    constraint model_calls_status_ck check (status in ('OK','FAILED','REFUSED','MALFORMED','TIMEOUT'))
);
create index if not exists model_calls_run_idx on model_calls(run_id, created_at desc);
create index if not exists model_calls_group_idx on model_calls(independence_group) where independence_group is not null;

-- ----------------------------------------------------------- external_jobs --
-- Slurm and anything else that outlives the process that launched it. The
-- submission is recorded before the scheduler is told, so a crash between the
-- two leaves a row to reconcile rather than a job nobody owns.
create table if not exists external_jobs (
    job_id           text        primary key,
    run_id           text        references research_runs(run_id) on delete cascade,
    work_id          text,
    project_id       text        not null references projects(project_id) on delete cascade,
    executor         text        not null,
    scheduler_job_id text,
    spec_digest      text        not null,
    run_dir          text        not null,
    status           text        not null default 'SUBMITTING',
    failure_class    text,
    exit_code        integer,
    detail           text,
    submitted_at     timestamptz not null default now(),
    last_polled_at   timestamptz,
    finished_at      timestamptz,
    constraint external_jobs_status_ck check (status in (
        'SUBMITTING','SUBMITTED','PENDING','RUNNING','COMPLETED','FAILED',
        'CANCELLED','TIMED_OUT','UNKNOWN'))
);
create index if not exists external_jobs_active_idx on external_jobs(last_polled_at nulls first)
    where status in ('SUBMITTING','SUBMITTED','PENDING','RUNNING','UNKNOWN');
create index if not exists external_jobs_run_idx on external_jobs(run_id, submitted_at desc);

-- ----------------------------------------------------------------- artifacts --
-- References only. The bytes live in the content-addressed store on disk;
-- PostgreSQL holds the identity, the size and what produced it. A large object
-- in a row here would be a large object in every backup and every checkpoint.
create table if not exists artifacts (
    artifact_id text        primary key,   -- sha256 of the content, lowercase hex
    size_bytes  bigint      not null check (size_bytes >= 0),
    media_type  text        not null default 'application/octet-stream',
    role        text,
    producer    text,
    source      text,
    created_at  timestamptz not null default now()
);
create table if not exists artifact_links (
    artifact_id text        not null references artifacts(artifact_id) on delete cascade,
    run_id      text        references research_runs(run_id) on delete cascade,
    work_id     text,
    role        text        not null,
    created_at  timestamptz not null default now(),
    primary key (artifact_id, run_id, work_id, role)
);
create index if not exists artifact_links_run_idx on artifact_links(run_id, created_at desc);

-- ------------------------------------------------------------------ budgets --
-- reserve -> execute -> reconcile. `reserved` is money promised but not yet
-- known to be spent; `spent` is money that certainly is. Available is
-- limit - reserved - spent, so two concurrent workers cannot both be told there
-- is room for the last call.
create table if not exists budgets (
    budget_id   text        primary key,
    scope       text        not null,
    scope_id    text        not null,
    dimension   text        not null,
    limit_value numeric(18, 6) not null check (limit_value >= 0),
    reserved    numeric(18, 6) not null default 0 check (reserved >= 0),
    spent       numeric(18, 6) not null default 0 check (spent >= 0),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint budgets_scope_ck check (scope in ('system','project','run','work_item')),
    constraint budgets_unique_scope unique (scope, scope_id, dimension)
);
create table if not exists budget_reservations (
    reservation_id text        primary key,
    budget_id      text        not null references budgets(budget_id) on delete cascade,
    work_id        text,
    amount         numeric(18, 6) not null check (amount >= 0),
    status         text        not null default 'HELD',
    created_at     timestamptz not null default now(),
    settled_at     timestamptz,
    constraint budget_reservations_status_ck check (status in ('HELD','SETTLED','RELEASED'))
);
create index if not exists budget_reservations_held_idx on budget_reservations(budget_id) where status = 'HELD';

-- ---------------------------------------------------------------- schedules --
-- A schedule produces events. It never performs work directly, so a scheduled
-- action passes through exactly the same provenance, budget, locking and
-- failure handling as one a person asked for.
create table if not exists schedules (
    schedule_id      text        primary key,
    project_id       text        references projects(project_id) on delete cascade,
    kind             text        not null,
    payload          jsonb       not null default '{}'::jsonb,
    interval_seconds integer     not null check (interval_seconds >= 60),
    next_run_at      timestamptz not null default now(),
    last_run_at      timestamptz,
    enabled          boolean     not null default true,
    created_at       timestamptz not null default now()
);
create index if not exists schedules_due_idx on schedules(next_run_at) where enabled;

-- ---------------------------------------------------------- provider_status --
create table if not exists provider_status (
    provider             text        primary key,
    healthy              boolean     not null default true,
    consecutive_failures integer     not null default 0 check (consecutive_failures >= 0),
    cooldown_until       timestamptz,
    last_error           text,
    last_ok_at           timestamptz,
    updated_at           timestamptz not null default now()
);
