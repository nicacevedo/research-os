-- Noncanonical runtime findings, and the immutable links that give them
-- provenance.
--
-- These are NOT scientific facts and this table is not scientific state. A
-- finding is "here is something this runtime observed, produced by this action,
-- in this cycle, resting on these artifacts". It has no status a person could
-- accept, no acceptance rule, no digest that participates in the capsule's, and
-- no path into `.research/` except through the v1 proposal layer, which a human
-- promotes.
--
-- What the table buys is the thing §5 of the integration brief asks for:
-- auditable grounding. Before it, a runtime result reached the proposal layer
-- as prose concatenated into a natural-language goal, so "what is this proposal
-- resting on" was answerable only by reading a model's sentence. Now a proposal
-- cites `finding_id`s, the proposal grounding allowlist contains exactly the
-- findings that were supplied, and each finding names the artifacts, capsule
-- objects, literature keys and experiment job it came from. The chain
--
--     proposed change -> proposal item -> finding -> artifact -> bytes
--
-- is traversable without trusting anything a model wrote.
--
-- Immutability: nothing updates a finding once written. A finding that turns out
-- to be wrong is superseded by another one, because a mutable finding would
-- break the one property that makes a citation worth anything -- that the thing
-- cited still says what it said when it was cited. `summary` is the only free
-- text and it is rendered as fenced data everywhere it reaches a prompt.
create table if not exists runtime_findings (
    finding_id        text        primary key,
    project_id        text        not null references projects(project_id) on delete cascade,
    kind              text        not null,
    summary           text        not null,
    source_run_id     text        references research_runs(run_id) on delete set null,
    source_cycle      integer,
    source_work_id    text,
    source_action     text,
    experiment_job_id text        references external_jobs(job_id) on delete set null,
    spec_digest       text,
    digest            text        not null,
    created_at        timestamptz not null default now(),
    constraint runtime_findings_kind_ck check (kind in (
        'literature','experiment','code','review','inspection','frontier',
        'interpretation','other')),
    constraint runtime_findings_cycle_ck check (source_cycle is null or source_cycle >= 0)
);
create index if not exists runtime_findings_project_idx
    on runtime_findings(project_id, created_at desc);
create index if not exists runtime_findings_run_idx
    on runtime_findings(source_run_id, created_at desc);
-- The dedupe index. A deterministic repeat of the same observation in the same
-- project is one finding, not a growing pile: the runtime recomputes an
-- unchanged frontier on every cycle, and without this each recomputation would
-- mint a new citable id for the same fact.
create unique index if not exists runtime_findings_digest_idx
    on runtime_findings(project_id, digest);

-- What each finding rests on. A separate table rather than JSON arrays on the
-- row, because these are the edges a person traverses and an edge that lives
-- inside a JSON blob cannot be joined, counted or indexed.
create table if not exists runtime_finding_refs (
    finding_id text not null references runtime_findings(finding_id) on delete cascade,
    kind       text not null,
    ref        text not null,
    primary key (finding_id, kind, ref),
    constraint runtime_finding_refs_kind_ck check (kind in (
        'artifact','capsule_object','literature_key'))
);
create index if not exists runtime_finding_refs_ref_idx on runtime_finding_refs(kind, ref);

-- Which findings one runtime action's proposal actually cited. Written when the
-- proposal is created, never updated: this is the record that the proposal a
-- person is reading was grounded in these findings and no others.
create table if not exists runtime_proposal_links (
    proposal_id text        not null,
    finding_id  text        not null references runtime_findings(finding_id) on delete cascade,
    run_id      text        references research_runs(run_id) on delete set null,
    work_id     text,
    created_at  timestamptz not null default now(),
    primary key (proposal_id, finding_id)
);
create index if not exists runtime_proposal_links_run_idx
    on runtime_proposal_links(run_id, created_at desc);
create index if not exists runtime_proposal_links_finding_idx
    on runtime_proposal_links(finding_id);

-- One runtime action, one logical proposal. The reservation row is written
-- before the v1 ProposalController is called, so a crash between "the proposal
-- store has a directory" and "the runtime knows about it" leaves the id to
-- reconcile against rather than an orphan a retry would duplicate.
--
-- Keyed by the action's stable identity -- run, cycle, action, grounding digest
-- -- and never by the attempt. The proposal id is derived from that key, so the
-- retry computes the same id, finds the same directory, and adopts it.
create table if not exists runtime_proposal_reservations (
    reservation_key text        primary key,
    proposal_id     text        not null unique,
    project_id      text        not null references projects(project_id) on delete cascade,
    run_id          text        references research_runs(run_id) on delete set null,
    work_id         text,
    status          text        not null default 'RESERVED',
    detail          text,
    created_at      timestamptz not null default now(),
    settled_at      timestamptz,
    constraint runtime_proposal_reservations_status_ck check (status in (
        'RESERVED','CREATED','FAILED'))
);
create index if not exists runtime_proposal_reservations_project_idx
    on runtime_proposal_reservations(project_id, created_at desc);
