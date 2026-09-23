-- Literature intelligence: what the published record says, each statement
-- tied to the retrieved works it rests on.
--
-- The shared SQLite index (R1) holds *works*: metadata, text, citations,
-- rebuildable from providers. What it cannot hold is what a reader concluded
-- about them for a question this project asked -- a finding, a method, a
-- dataset, a limitation, a disagreement between two papers, a gap nobody
-- measured. Those are interpretations, produced by a model in a project's
-- context, so they live beside the project's other operational state and
-- carry `project_id` (invariant 4); the works they cite stay shared
-- (invariant 5).
--
-- The property the table exists to enforce is the one invariant 10 states
-- for claims and the portfolio already enforces for evidence: **a statement
-- about the literature rests on retrieved sources, or it is not stored.**
-- `work_keys` may not be empty, every key was in the packet the reader was
-- given and resolved in the index when the claim was written, and a quoted
-- excerpt was found in that source's stored text or the whole reading was
-- refused. Model memory is not storable here.

create table if not exists literature_claims (
    claim_id        text        primary key,
    project_id      text        not null references projects(project_id) on delete cascade,
    kind            text        not null,
    statement       text        not null,
    work_keys       text[]      not null,
    excerpt         text        not null default '',
    -- CITED: every key was supplied and resolves. QUOTED: and the excerpt
    -- was found, verbatim after whitespace normalisation, in a cited
    -- source's stored title or abstract.
    verification    text        not null,
    query           text        not null,
    request_id      text        references frontier_requests(request_id) on delete set null,
    idea_id         text        references ideas(idea_id) on delete set null,
    source_call_id  text,
    artifact_id     text        references artifacts(artifact_id),
    digest          text        not null,
    created_at      timestamptz not null default now(),
    constraint literature_claims_kind_ck check (kind in (
        'FINDING','METHOD','DATASET','LIMITATION','DISAGREEMENT','GAP','OPEN_QUESTION')),
    constraint literature_claims_verification_ck check (verification in ('CITED','QUOTED')),
    constraint literature_claims_sourced_ck check (cardinality(work_keys) >= 1),
    constraint literature_claims_statement_ck check (statement <> ''),
    constraint literature_claims_quoted_ck check (verification <> 'QUOTED' or excerpt <> '')
);
create unique index if not exists literature_claims_digest_idx
    on literature_claims(project_id, digest);
create index if not exists literature_claims_project_idx
    on literature_claims(project_id, kind, created_at);
create index if not exists literature_claims_idea_idx on literature_claims(idea_id);

-- Immutable: a claim that could be edited after something cited it would
-- make the citation worthless, the rule `runtime_findings` already follows.
create or replace function literature_claims_immutable() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' and pg_trigger_depth() >= 2 then
        return old;
    end if;
    if tg_op = 'UPDATE'
       and new.claim_id = old.claim_id
       and new.project_id = old.project_id
       and new.kind = old.kind
       and new.statement = old.statement
       and new.work_keys = old.work_keys
       and new.excerpt = old.excerpt
       and new.verification = old.verification
       and new.query = old.query
       and new.digest = old.digest then
        -- Only the nullable references may change, and only because a
        -- referenced row was deleted by a cascade elsewhere.
        return new;
    end if;
    raise exception 'literature claim % is immutable', old.claim_id
        using errcode = 'check_violation';
end;
$$;
drop trigger if exists literature_claims_immutable_trg on literature_claims;
create trigger literature_claims_immutable_trg
    before update or delete on literature_claims
    for each row execute function literature_claims_immutable();
