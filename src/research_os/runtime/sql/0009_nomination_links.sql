-- Which findings one cross-project nomination rests on.
--
-- The same shape as `runtime_proposal_links` and deliberately a separate table
-- rather than a shared one keyed by a `target_kind` column. A proposal and a
-- nomination cross different boundaries -- one into *this* project's capsule,
-- one into *other* projects' prompts -- and a query that had to filter on a
-- discriminator to tell them apart is a query that can be written wrong once
-- and leak one into the other's audit trail.
--
-- Invariant 15 is what this is for: cross-project reuse happens through
-- deliberately promoted shared knowledge. A promoted insight arrives in another
-- project's prompt behind a fence saying whose findings it was, and this table
-- is how that provenance is reconstructible all the way back to the artifact a
-- runtime cycle produced.
create table if not exists runtime_nomination_links (
    nomination_id text        not null,
    finding_id    text        not null references runtime_findings(finding_id) on delete cascade,
    project_id    text        not null references projects(project_id) on delete cascade,
    run_id        text        references research_runs(run_id) on delete set null,
    work_id       text,
    created_at    timestamptz not null default now(),
    primary key (nomination_id, finding_id)
);
create index if not exists runtime_nomination_links_project_idx
    on runtime_nomination_links(project_id, created_at desc);
create index if not exists runtime_nomination_links_finding_idx
    on runtime_nomination_links(finding_id);
