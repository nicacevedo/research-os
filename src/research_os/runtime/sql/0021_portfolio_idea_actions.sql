-- Every stage attempt against an idea, and the two invariants that live on it.
--
-- This table is deliberately doing two jobs the brief listed separately,
-- because they turn out to be the same row:
--
--   "only one active track per idea"      -> a partial unique index
--   "same scientific-basis action is
--    idempotent"                          -> a unique index on the basis key
--
-- A separate `idea_tracks` table would have held exactly one extra fact -- that
-- a track is in flight -- which is what `status = 'ACTIVE'` already says.
create table if not exists idea_actions (
    action_id     text           primary key,
    idea_id       text           not null references ideas(idea_id) on delete cascade,
    idea_version  integer        not null,
    stage         text           not null,

    -- A digest over (content_digest, the evidence set, the live review set):
    -- the scientific basis this action would run against. Two requests to run
    -- the same stage against the same basis are one action, which is what
    -- makes a replayed event, a reclaimed lease and a duplicated portfolio
    -- tick all harmless.
    basis_digest  text           not null,

    status        text           not null default 'ACTIVE',
    work_id       text,
    thread_id     text,

    -- The allocator's scheduling utility at the moment this was chosen. An
    -- operational number: it is here so allocation decisions are auditable and
    -- reproducible, and it is never part of the scientific record. See
    -- docs/adr/0004.
    utility       numeric(12, 6),

    disposition   text,
    detail        text,
    failure_class text,
    cost_usd      numeric(12, 6) not null default 0 check (cost_usd >= 0),
    model_calls   integer        not null default 0 check (model_calls >= 0),

    created_at    timestamptz    not null default now(),
    updated_at    timestamptz    not null default now(),
    completed_at  timestamptz,

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint idea_actions_status_ck check (status in (
        'ACTIVE','SUCCEEDED','FAILED','CANCELLED','SUPERSEDED')),
    constraint idea_actions_stage_ck check (stage in (
        'dedup','novelty_screen','falsify','discover','adjudicate','evidence',
        'literature_audit','review_board','meta_review','replicate','branch')),
    constraint idea_actions_disposition_ck check (disposition is null or disposition in (
        'REJECT','PARK','REVISE','DEEPEN','BRANCH','PROMISING','VALIDATED',
        'HUMAN_READY','CONTINUE','DUPLICATE')),
    constraint idea_actions_completion_ck check (
        (status = 'ACTIVE') = (completed_at is null))
);
-- "Only one active track per idea."
create unique index if not exists idea_actions_active_idx
    on idea_actions(idea_id) where status = 'ACTIVE';
-- "The same scientific-basis action is idempotent."
--
-- Partial, and the predicate is the whole correctness of it. An unqualified
-- unique index would mean a stage that FAILED on a provider outage could never
-- be attempted again against the same science, because the dead row would own
-- the key forever -- and `work_items.dedup_key` is permanently unique too, so
-- a relaunch would return the dead work item and the allocator would believe
-- it had launched something. The idea would sit in BLOCKED_PROVIDER until a
-- person noticed.
--
-- What the invariant actually means is: one scientific basis has at most one
-- action that is running or that succeeded. A failure is not a scientific act
-- that happened; it is one that did not.
create unique index if not exists idea_actions_basis_idx
    on idea_actions(idea_id, idea_version, stage, basis_digest)
    where status in ('ACTIVE','SUCCEEDED');
create index if not exists idea_actions_idea_idx on idea_actions(idea_id, created_at desc);
create index if not exists idea_actions_work_idx on idea_actions(work_id) where work_id is not null;
-- The reconciler's query: active actions whose work item is gone or dead.
create index if not exists idea_actions_stale_idx on idea_actions(updated_at)
    where status = 'ACTIVE';
