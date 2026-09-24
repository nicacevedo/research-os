-- The research frontier: why every idea exists, and the questions that are
-- owed an idea.
--
-- Two tables, and the architecture's §20 is their specification.
--
-- `frontier_requests` is the unit of recursive discovery. Before it, the only
-- producer of a child idea was the BRANCH stage, which `select_stage` reaches
-- after meta-review and replication -- and on real work nothing ever got that
-- far, so `max_depth` was 0 across 254 ideas (report §AB.7). A result, an
-- anomaly, a falsifier's objection, a reviewer's criticism, a replication
-- that disagreed, a literature contradiction, a referee's finding or a gap a
-- writer found each become one row here, written by ordinary code where the
-- event is recorded; the portfolio buys a follow-up explorer for it, and the
-- children it proposes carry the request, the basis and -- where there is
-- one -- a lineage edge to the idea the question came from. The parent is
-- never edited: a follow-up is a new idea with its own contract.
--
-- `idea_provenance` is why an idea exists, append-only. `ideas.origin` says
-- which generator first produced a row; it cannot say that a researcher's
-- seed, a blind explorer and a literature contradiction independently arrived
-- at the same direction -- which is scientifically the most interesting fact
-- about it, and which deduplication used to throw away.

create table if not exists frontier_requests (
    request_id      text        primary key,
    project_id      text        not null references projects(project_id) on delete cascade,
    -- What is being asked for: new ideas (FOLLOW_UP), or retrieval and
    -- reading of sources for one idea (LITERATURE).
    kind            text        not null,
    -- Why: which kind of event raised the question.
    basis           text        not null,
    source_idea_id  text        references ideas(idea_id) on delete cascade,
    source_version  integer,
    -- The object that raised it: an experiment, an objection, a review, a
    -- literature claim, a synthesis finding. Named, so the chain from a child
    -- idea back to the event that produced it is a join rather than a story.
    source_ref      text        not null,
    question        text        not null,
    detail          text,
    state           text        not null default 'OPEN',
    attempts        integer     not null default 0,
    resolution      text,
    resolved_by     text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint frontier_requests_kind_ck check (kind in ('FOLLOW_UP','LITERATURE')),
    constraint frontier_requests_basis_ck check (basis in (
        'RESULT','INSUFFICIENT','ANOMALY','FALSIFIER_OBJECTION','REVIEWER_CRITICISM',
        'REPLICATION','LITERATURE','REFEREE_FINDING','EVIDENCE_GAP')),
    constraint frontier_requests_state_ck check (state in (
        'OPEN','CONSUMED','DECLINED')),
    -- A closed request says how it closed.
    constraint frontier_requests_resolved_ck check (
        state = 'OPEN' or (resolution is not null and resolution <> '')),
    constraint frontier_requests_question_ck check (question <> '')
);
-- One request per raising event. The event is recorded once and so is its
-- question: a replayed stage or a re-run tick finds the row instead of
-- writing a second, which is what keeps recursion bounded by events rather
-- than by retries.
create unique index if not exists frontier_requests_event_idx
    on frontier_requests(project_id, kind, basis, source_ref);
create index if not exists frontier_requests_open_idx
    on frontier_requests(project_id, state, created_at);

create table if not exists idea_provenance (
    provenance_id   text        primary key,
    -- Carried so the append-only trigger can tell a project being deleted
    -- (allowed) from an idea being deleted under it (refused).
    project_id      text        not null references projects(project_id) on delete cascade,
    idea_id         text        not null references ideas(idea_id) on delete cascade,
    basis           text        not null,
    -- A seed, a request, a literature claim, a parent idea, a candidate call.
    source_ref      text,
    -- Cascade rather than set null: a request cited by provenance can only
    -- disappear with its project, and a set-null would be an update the
    -- append-only trigger refuses.
    request_id      text        references frontier_requests(request_id) on delete cascade,
    call_id         text,
    detail          text        not null default '',
    created_at      timestamptz not null default now(),
    constraint idea_provenance_basis_ck check (basis in (
        'HUMAN_SEED','BLIND_EXPLORATION','SEEDED_EXPLORATION','FAILURE_MINING',
        'LITERATURE','RESULT','INSUFFICIENT','ANOMALY','FALSIFIER_OBJECTION',
        'REVIEWER_CRITICISM','REPLICATION','REFEREE_FINDING','EVIDENCE_GAP',
        'BRANCH','REVIVAL','MERGE','CONVERGENCE'))
);
create index if not exists idea_provenance_idea_idx on idea_provenance(idea_id, created_at);
create index if not exists idea_provenance_request_idx on idea_provenance(request_id);

-- Append-only, by the database: provenance that could be rewritten would be
-- a story about why an idea exists rather than a record of it.
create or replace function idea_provenance_append_only() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE'
       and not exists (select 1 from projects where project_id = old.project_id) then
        return old;  -- the project itself is being deleted
    end if;
    raise exception 'idea provenance % is append-only', old.provenance_id
        using errcode = 'check_violation';
end;
$$;
drop trigger if exists idea_provenance_append_only_trg on idea_provenance;
create trigger idea_provenance_append_only_trg
    before update or delete on idea_provenance
    for each row execute function idea_provenance_append_only();

-- A request is the record of an event. What raised it, what it asked and
-- where it came from never change after it is written; only its state
-- moves, once, from OPEN to closed, with its resolution. It is removed only
-- with its project.
create or replace function frontier_requests_immutable() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from projects where project_id = old.project_id) then
            raise exception 'frontier request % is a record and is never deleted',
                old.request_id
                using errcode = 'check_violation';
        end if;
        return old;
    end if;
    if new.request_id <> old.request_id
       or new.project_id <> old.project_id
       or new.kind <> old.kind
       or new.basis <> old.basis
       or new.source_ref <> old.source_ref
       or new.question <> old.question
       or new.detail is distinct from old.detail
       or new.source_idea_id is distinct from old.source_idea_id
       or new.source_version is distinct from old.source_version
       or new.created_at <> old.created_at
       or (old.state <> 'OPEN' and (
               new.state <> old.state
               or new.resolution is distinct from old.resolution
               or new.resolved_by is distinct from old.resolved_by)) then
        raise exception 'frontier request % is a record of an event and is not rewritten',
            old.request_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
drop trigger if exists frontier_requests_immutable_trg on frontier_requests;
create trigger frontier_requests_immutable_trg
    before update or delete on frontier_requests
    for each row execute function frontier_requests_immutable();

-- Two new origins, for the two generators this release adds.
alter table ideas drop constraint if exists ideas_origin_ck;
alter table ideas add constraint ideas_origin_ck check (origin in (
    'BLIND_EXPLORER','SEEDED_EXPLORER','FAILURE_MINING_EXPLORER','RESEARCHER_SEED',
    'BRANCH','REVIVAL','MERGE','FOLLOW_UP','LITERATURE_EXPLORER'));

-- Every existing idea gets the provenance its origin already implies, so the
-- invariant "every idea has at least one provenance row" holds on an
-- upgraded database as it does on a new one. Derived, and said to be.
insert into idea_provenance
    (provenance_id, project_id, idea_id, basis, source_ref, detail, created_at)
select
    'IPRV-' || to_char(i.created_at at time zone 'UTC', 'YYYYMMDD"T"HH24MISS"Z"')
        || '-' || substr(md5(i.idea_id), 1, 8),
    i.project_id,
    i.idea_id,
    case i.origin
        when 'BLIND_EXPLORER' then 'BLIND_EXPLORATION'
        when 'SEEDED_EXPLORER' then 'SEEDED_EXPLORATION'
        when 'FAILURE_MINING_EXPLORER' then 'FAILURE_MINING'
        when 'RESEARCHER_SEED' then 'HUMAN_SEED'
        when 'BRANCH' then 'BRANCH'
        when 'REVIVAL' then 'REVIVAL'
        when 'MERGE' then 'MERGE'
        else 'BRANCH'
    end,
    (select e.parent_idea_id from idea_edges e
      where e.child_idea_id = i.idea_id and e.is_lineage
      order by e.created_at limit 1),
    'backfilled by migration 0032 from ideas.origin',
    i.created_at
from ideas i
where not exists (select 1 from idea_provenance p where p.idea_id = i.idea_id);

-- And the convergences deduplication used to discard: every DUPLICATE_OF
-- edge says its survivor was proposed a second time.
insert into idea_provenance
    (provenance_id, project_id, idea_id, basis, source_ref, detail, created_at)
select
    'IPRV-' || to_char(e.created_at at time zone 'UTC', 'YYYYMMDD"T"HH24MISS"Z"')
        || '-' || substr(md5(e.parent_idea_id || e.child_idea_id), 1, 8),
    i.project_id,
    e.parent_idea_id,
    'CONVERGENCE',
    e.child_idea_id,
    'backfilled by migration 0032 from a DUPLICATE_OF edge: ' || coalesce(e.detail, ''),
    e.created_at
from idea_edges e
join ideas i on i.idea_id = e.parent_idea_id
where e.kind = 'DUPLICATE_OF'
on conflict (provenance_id) do nothing;
