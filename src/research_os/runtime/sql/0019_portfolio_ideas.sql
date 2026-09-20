-- The autonomous discovery portfolio: ideas, their immutable versions, and
-- their lineage.
--
-- Nothing in this file is scientific truth, and the distinction is sharper here
-- than anywhere else in this schema because the object is called an Idea and
-- the capsule already has a type with that name.
--
--     capsule Idea    IDEA-0001, in .research/ideas/, on the canonical branch,
--                     written or promoted by a person. Scientific state.
--
--     portfolio Idea  PIDEA-<stamp>-<hex>, here, materialised to the
--                     research-os/autonomous branch by the Curator. A
--                     *candidate direction*. No scientific status at all.
--
-- The only edge between them is the one this whole layer exists to produce: a
-- HUMAN_READY portfolio idea becomes a Proposal, and a person promotes it.
--
-- See docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md and docs/adr/0001, 0002.

-- ------------------------------------------------------------------ ideas --
-- Stable identity and *mutable pointers only*. Every scientifically material
-- field lives on an immutable version row, because a review binds to a version
-- and a review that can be silently re-pointed is not a review.
create table if not exists ideas (
    idea_id           text        primary key,
    project_id        text        not null references projects(project_id) on delete cascade,

    -- Lineage. `depth` is write-once: the composite foreign keys on idea_edges
    -- reference (idea_id, depth), so PostgreSQL refuses any update to it that
    -- an edge depends on. That is what makes the acyclicity check below sound
    -- without a trigger.
    depth             integer     not null default 0 check (depth >= 0),
    lineage_root      text        not null,

    -- Where it came from. Not a free-text note: the failure-mining explorer
    -- selects on it, the diversity constraint groups on it, and the digest
    -- reports it.
    origin            text        not null,

    -- Current pointers.
    current_version   integer     not null default 1 check (current_version >= 1),
    status            text        not null default 'CANDIDATE',
    operational_state text        not null default 'IDLE',

    -- The high-water mark of gates passed, which `status` cannot carry: an
    -- idea rejected after reaching PROMISING is a different fact from one
    -- rejected as a candidate, and §32 of the brief asks the digest to report
    -- "ideas demoted".
    quality_tier      text        not null default 'NONE',

    -- What the Curator has already written. The gap between this and the
    -- current state is the exposure to losing the operational database, and
    -- `researchctl portfolio status` reports it as a number rather than
    -- leaving it as an assumption. See docs/adr/0002.
    curated_digest    text,
    curated_at        timestamptz,

    -- Retirement memory, and the reason it is required rather than optional.
    -- `docs/CAPSULE.md` makes `retire_reason` mandatory on a discarded capsule
    -- Idea "so the project can tell 'we ruled this out' apart from 'we forgot
    -- about it'". The same argument applies here twice over: the
    -- failure-mining explorer's entire input is *why* things died, and
    -- `researchctl ideas rejected` is unreadable without it. Killing an idea
    -- still needs no gate. It needs a sentence.
    retire_reason     text,
    revisit_if        text,

    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),

    constraint ideas_status_ck check (status in (
        'CANDIDATE','PROMISING','INVESTIGATING','REVIEW','VALIDATED',
        'HUMAN_READY','PARKED','REJECTED','SUPERSEDED')),
    -- Operational blockage is orthogonal to scientific quality. The brief's §7
    -- requires `quality = PROMISING, operational = BLOCKED_PROVIDER` to be
    -- representable, and the provider-failure closure (docs/RUNTIME.md §17)
    -- established why: an outage must never be able to read as a verdict.
    constraint ideas_operational_ck check (operational_state in (
        'ACTIVE','IDLE','BLOCKED_PROVIDER','BLOCKED_BUDGET','BLOCKED_EXTERNAL',
        'BLOCKED_DEPENDENCY')),
    constraint ideas_quality_tier_ck check (quality_tier in (
        'NONE','PROMISING','VALIDATED','HUMAN_READY')),
    constraint ideas_origin_ck check (origin in (
        'BLIND_EXPLORER','SEEDED_EXPLORER','FAILURE_MINING_EXPLORER',
        'RESEARCHER_SEED','BRANCH','REVIVAL','MERGE')),
    -- A root is its own lineage root. A descendant's root is someone else's id,
    -- which the store sets from the parent and which the depth rule below keeps
    -- consistent.
    constraint ideas_lineage_root_ck check (depth > 0 or lineage_root = idea_id),
    constraint ideas_curation_ck check ((curated_digest is null) = (curated_at is null)),
    constraint ideas_retire_reason_ck check (
        status not in ('REJECTED','PARKED','SUPERSEDED')
        or (retire_reason is not null and retire_reason <> '')),
    -- A parked idea says what would bring it back, because "parked" without
    -- one is indistinguishable from forgotten.
    constraint ideas_revisit_ck check (
        status <> 'PARKED' or (revisit_if is not null and revisit_if <> '')),
    -- Referenced by idea_edges' composite foreign keys. Redundant given the
    -- primary key, and required by SQL for a composite FK target.
    constraint ideas_id_depth_uq unique (idea_id, depth)
);
create index if not exists ideas_project_idx on ideas(project_id, created_at desc);
-- The allocator's hot path: what is allocatable in this project right now.
create index if not exists ideas_allocatable_idx on ideas(project_id, status)
    where status not in ('REJECTED','SUPERSEDED');
create index if not exists ideas_active_idx on ideas(project_id)
    where operational_state = 'ACTIVE';
create index if not exists ideas_lineage_idx on ideas(lineage_root);
create index if not exists ideas_uncurated_idx on ideas(project_id)
    where curated_at is null;

-- ---------------------------------------------------------- idea_versions --
-- Append-only. Nothing updates a row here, ever.
create table if not exists idea_versions (
    idea_id                  text        not null references ideas(idea_id) on delete cascade,
    version                  integer     not null check (version >= 1),

    -- --- scientifically material: these fields and only these are hashed
    --     into content_digest ---------------------------------------------
    title                    text        not null,
    research_question        text        not null,
    core_idea                text        not null,
    mechanism                text        not null default '',
    why_it_matters           text        not null default '',
    falsifier                text        not null default '',
    adjudication_types       text[]      not null default '{}',
    closest_prior_work       text        not null default '',
    claimed_difference       text        not null default '',
    assumptions              jsonb       not null default '[]'::jsonb,
    alternative_explanations jsonb       not null default '[]'::jsonb,
    open_uncertainties       jsonb       not null default '[]'::jsonb,

    -- --- not hashed: changing any of these must not stale a review --------
    next_best_action         text        not null default '',
    dimensions               jsonb       not null default '{}'::jsonb,
    -- Which prior objections this version was written to answer. Set only by a
    -- REVISE that responds to them; it is what makes an objection resolvable
    -- without making it dismissable.
    addressed_objections     jsonb       not null default '[]'::jsonb,

    -- --- identity ---------------------------------------------------------
    -- Over the material fields above, canonical JSON, sha256, version-tagged.
    content_digest           text        not null,
    -- Over an aggressively normalised projection of research_question and
    -- core_idea. Two phrasings of one idea collide here on purpose; this is the
    -- deterministic half of deduplication and it is never a model's output.
    canonical_digest         text        not null,

    -- --- provenance -------------------------------------------------------
    -- The call that produced this version. Read by the review-independence
    -- check: a review whose call_id equals this is self-review, which is a
    -- code error rather than a weak review, and is refused.
    origin_call_id           text        references model_calls(call_id) on delete set null,
    origin_role              text        not null,
    origin_stage             text,
    created_at               timestamptz not null default now(),

    primary key (idea_id, version),

    -- The adjudication vocabulary is `research_os.runtime.adjudication`'s, and
    -- a constraint rather than a convention because every quality gate is
    -- parameterised by it: an idea that could declare its own type could
    -- choose its own evidentiary bar. The values are the runtime's
    -- `AdjudicationKind`, which is computed from the falsification clause by
    -- ordinary Python and never asked of a model.
    constraint idea_versions_adjudication_ck check (
        adjudication_types <@ array[
            'empirical','mathematical','novelty_or_literature','mixed',
            'diagnostic','undetermined']::text[])
);
create index if not exists idea_versions_canonical_idx on idea_versions(canonical_digest);
create index if not exists idea_versions_content_idx on idea_versions(content_digest);

-- ------------------------------------------------------------- idea_edges --
-- Lineage and relations in one table, distinguished by `kind`, with acyclicity
-- enforced by the database rather than by a Python convention.
--
-- How it works: every lineage edge must strictly increase depth. A cycle would
-- require d1 < d2 < ... < d1, which is unsatisfiable. The depths are copied
-- onto the edge, and the composite foreign keys below make the copies provably
-- equal to the real ones -- an UPDATE to ideas.depth that an edge depends on is
-- refused by the foreign key, so the copy cannot drift.
--
-- CONTRADICTS and DUPLICATE_OF are relations, not lineage. They are exempt,
-- and every lineage traversal filters on `is_lineage` rather than re-listing
-- the kinds.
create table if not exists idea_edges (
    parent_idea_id text        not null,
    child_idea_id  text        not null,
    kind           text        not null,
    parent_depth   integer     not null,
    child_depth    integer     not null,
    detail         text        not null default '',
    created_at     timestamptz not null default now(),

    is_lineage     boolean     generated always as
                       (kind not in ('CONTRADICTS','DUPLICATE_OF')) stored,

    primary key (parent_idea_id, child_idea_id, kind),
    foreign key (parent_idea_id, parent_depth)
        references ideas(idea_id, depth) on delete cascade,
    foreign key (child_idea_id, child_depth)
        references ideas(idea_id, depth) on delete cascade,
    constraint idea_edges_kind_ck check (kind in (
        'DERIVED_FROM','GENERALIZES','SPECIALIZES','MERGED_FROM','REVIVES',
        'CONTRADICTS','DUPLICATE_OF')),
    constraint idea_edges_self_ck check (parent_idea_id <> child_idea_id),
    constraint idea_edges_acyclic_ck check (not is_lineage or child_depth > parent_depth)
);
-- An idea is a duplicate of at most one other idea, so the survivor chain a
-- traversal walks has a fixed point. Without this, `A DUPLICATE_OF B` and
-- `A DUPLICATE_OF C` both insert and "which one survived" has two answers.
-- Mutual duplication (`A->B` and `B->A`) is still expressible and is resolved
-- by the traversal's visited set, which is why `duplicate_survivor` carries
-- one.
create unique index if not exists idea_edges_one_duplicate_idx
    on idea_edges(child_idea_id) where kind = 'DUPLICATE_OF';
create index if not exists idea_edges_child_idx on idea_edges(child_idea_id, kind);
create index if not exists idea_edges_parent_idx on idea_edges(parent_idea_id, kind);
