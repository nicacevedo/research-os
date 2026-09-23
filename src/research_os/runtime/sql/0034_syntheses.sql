-- Evidence syntheses: a writer's account of what the portfolio's reviewed
-- evidence establishes, and a referee's challenge to it.
--
-- Operational text about candidate ideas. Not a manuscript (R4's `paper`
-- writes those, from the capsule, and a person runs it), not a capsule
-- object, and not an approval of anything: the referee's verdict grants
-- nothing, and the highest state a synthesis reaches is REFEREED.
--
-- `basis_digest` is what the synthesis was written *from* -- the ideas, the
-- evidence rows and the literature claims it was given -- so a synthesis is
-- written once per basis, and a new one is due exactly when the reviewed
-- evidence changes. Its statements and the grounding report that checked
-- every citation and every number are in the document artifact; the
-- referee's findings are in the referee artifact and, when they ask a
-- question, in `frontier_requests` naming this synthesis.

create table if not exists syntheses (
    synthesis_id          text        primary key,
    project_id            text        not null references projects(project_id) on delete cascade,
    state                 text        not null default 'DRAFTED',
    basis_digest          text        not null,
    document_artifact_id  text        not null references artifacts(artifact_id),
    referee_artifact_id   text        references artifacts(artifact_id),
    writer_call_id        text,
    referee_call_id       text,
    statements            integer     not null default 0,
    findings              integer     not null default 0,
    referee_verdict       text,
    detail                text,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    constraint syntheses_state_ck check (state in ('DRAFTED','REFEREED','SUPERSEDED')),
    constraint syntheses_refereed_ck check (
        state <> 'REFEREED' or (referee_artifact_id is not null and referee_verdict is not null)),
    constraint syntheses_verdict_ck check (
        referee_verdict is null or referee_verdict in ('SOUND','MAJOR_REVISION','UNSOUND'))
);
create unique index if not exists syntheses_basis_idx
    on syntheses(project_id, basis_digest);
create index if not exists syntheses_project_idx
    on syntheses(project_id, created_at desc);
