-- Every literature search the system executes, as a durable record
-- (docs/ARCHITECTURE_INVARIANTS.md, INV-05).
--
-- Until this table the portfolio had no record of a *search* at all -- only
-- of the evidence rows a scout wrote about what it was shown. Two gates were
-- computed from what a model said instead:
--
--   * HUMAN_READY's "second terminology path" compared the keys two scouts
--     *cited*. Two byte-identical retrievals passed as independent paths when
--     the second scout cited a work the first left out of its matrix, and a
--     retried first audit counted as the second path -- the final adversarial
--     review of 37e8afe reproduced both (H1);
--   * `literature_confidence` was `len(audit.queries) / 6`, the scout's own
--     list of queries, none of which had to have been run (M1).
--
-- A retrieval row is written STARTED before the search and COMPLETED after
-- it, with the exact query, its normalised digest, the backend, the limit and
-- every key it returned with their digest -- or FAILED with the error. It is
-- owned by the action (and run, and work item) that executed it. Evidence an
-- audit writes names the retrieval that supplied its source, so a gate can ask
-- what was *retrieved*, by which execution, with which words.
create table if not exists literature_retrievals (
    retrieval_id   text        primary key,
    project_id     text        not null references projects(project_id) on delete cascade,
    idea_id        text        references ideas(idea_id) on delete cascade,
    idea_version   integer,
    action_id      text        references idea_actions(action_id) on delete set null,
    request_id     text,
    run_id         text,
    work_id        text,
    purpose        text        not null,
    query          text        not null,
    query_digest   text        not null,
    backend        text        not null,
    result_limit   integer     not null check (result_limit > 0),
    status         text        not null default 'STARTED',
    result_keys    jsonb       not null default '[]'::jsonb,
    result_digest  text,
    error          text,
    started_at     timestamptz not null default now(),
    completed_at   timestamptz,

    constraint literature_retrievals_purpose_ck check (purpose in (
        'novelty_screen','audit','second_path','reading')),
    constraint literature_retrievals_status_ck check (status in (
        'STARTED','COMPLETED','FAILED')),
    -- A completed search says what it found; a started one has found nothing.
    constraint literature_retrievals_completion_shape_ck check (
        (status = 'COMPLETED') = (result_digest is not null and completed_at is not null)
        or status = 'FAILED')
);
create index if not exists literature_retrievals_idea_idx
    on literature_retrievals(idea_id, idea_version, status);

-- The retrieval whose results a literature row cites. Null on every row
-- written before this migration, which is exactly why no such row can count
-- as a retrieval path: nothing records what search produced it.
alter table idea_evidence
    add column if not exists retrieval_id text
        references literature_retrievals(retrieval_id) on delete set null;
