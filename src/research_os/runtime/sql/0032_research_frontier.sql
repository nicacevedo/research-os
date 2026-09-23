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
    idea_id         text        not null references ideas(idea_id) on delete cascade,
    basis           text        not null,
    -- A seed, a request, a literature claim, a parent idea, a candidate call.
    source_ref      text,
    request_id      text        references frontier_requests(request_id) on delete set null,
    call_id         text,
    detail          text        not null default '',
    created_at      timestamptz not null default now(),
    constraint idea_provenance_basis_ck check (basis in (
        'HUMAN_SEED','BLIND_EXPLORATION','SEEDED_EXPLORATION','FAILURE_MINING',
        'LITERATURE','RESULT','INSUFFICIENT','ANOMALY','FALSIFIER_OBJECTION',
        'REVIEWER_CRITICISM','REPLICATION','REFEREE_FINDING','EVIDENCE_GAP',
        'BRANCH','REVIVAL','CONVERGENCE'))
);
create index if not exists idea_provenance_idea_idx on idea_provenance(idea_id, created_at);
create index if not exists idea_provenance_request_idx on idea_provenance(request_id);

-- Append-only, by the database: provenance that could be rewritten would be
-- a story about why an idea exists rather than a record of it.
create or replace function idea_provenance_append_only() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' and pg_trigger_depth() >= 2 then
        return old;  -- a cascade from deleting the idea's project
    end if;
    raise exception 'idea provenance % is append-only', old.provenance_id
        using errcode = 'check_violation';
end;
$$;
drop trigger if exists idea_provenance_append_only_trg on idea_provenance;
create trigger idea_provenance_append_only_trg
    before update or delete on idea_provenance
    for each row execute function idea_provenance_append_only();

-- Two new origins, for the two generators this release adds.
alter table ideas drop constraint if exists ideas_origin_ck;
alter table ideas add constraint ideas_origin_ck check (origin in (
    'BLIND_EXPLORER','SEEDED_EXPLORER','FAILURE_MINING_EXPLORER','RESEARCHER_SEED',
    'BRANCH','REVIVAL','MERGE','FOLLOW_UP','LITERATURE_EXPLORER'));

-- Every existing idea gets the provenance its origin already implies, so the
-- invariant "every idea has at least one provenance row" holds on an
-- upgraded database as it does on a new one. Derived, and said to be.
insert into idea_provenance (provenance_id, idea_id, basis, source_ref, detail, created_at)
select
    'IPRV-' || to_char(i.created_at at time zone 'UTC', 'YYYYMMDD"T"HH24MISS"Z"')
        || '-' || substr(md5(i.idea_id), 1, 8),
    i.idea_id,
    case i.origin
        when 'BLIND_EXPLORER' then 'BLIND_EXPLORATION'
        when 'SEEDED_EXPLORER' then 'SEEDED_EXPLORATION'
        when 'FAILURE_MINING_EXPLORER' then 'FAILURE_MINING'
        when 'RESEARCHER_SEED' then 'HUMAN_SEED'
        when 'BRANCH' then 'BRANCH'
        when 'REVIVAL' then 'REVIVAL'
        else 'BRANCH'
    end,
    (select e.parent_idea_id from idea_edges e
      where e.child_idea_id = i.idea_id and e.is_lineage
      order by e.created_at limit 1),
    'backfilled by migration 0032 from ideas.origin',
    i.created_at
from ideas i
where not exists (select 1 from idea_provenance p where p.idea_id = i.idea_id);
