-- What an idea rests on, and what independent reviewers said about it.
--
-- Three of this file's check constraints are scientific policy expressed as
-- schema, and each one is here rather than in Python because a rule enforced
-- only in the layer that wants to break it is not enforced:
--
--   1. a numerical witness is not a proof;
--   2. literature evidence must name a retrieved source, so model memory
--      cannot be stored as literature at all;
--   3. a review binds to the exact content digest it read.

-- ---------------------------------------------------------- idea_evidence --
create table if not exists idea_evidence (
    evidence_id    text        primary key,
    idea_id        text        not null,
    idea_version   integer     not null,
    kind           text        not null,
    strength       text        not null,
    summary        text        not null,

    -- What it rests on. At least one must be present; see the constraint.
    artifact_id    text        references artifacts(artifact_id) on delete set null,
    finding_id     text        references runtime_findings(finding_id) on delete set null,
    job_id         text        references external_jobs(job_id) on delete set null,
    literature_key text,
    source_call_id text        references model_calls(call_id) on delete set null,

    created_at     timestamptz not null default now(),

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint idea_evidence_kind_ck check (kind in (
        'literature','experiment','derivation','numerical','code','replication',
        'inspection')),
    constraint idea_evidence_strength_ck check (strength in (
        'SUPPORTS','CONTRADICTS','CONSISTENT_WITH','INCONCLUSIVE')),

    -- 1. A numerical witness is consistent with a proposition or refutes it. It
    --    never establishes one. `SUPPORTS` is available to derivations and to
    --    experiments; a numerical check cannot be recorded as it, so a
    --    MATHEMATICAL idea cannot reach VALIDATED on arithmetic.
    constraint idea_evidence_numerical_ck check (
        kind <> 'numerical' or strength in ('CONSISTENT_WITH','CONTRADICTS','INCONCLUSIVE')),

    -- 2. Literature evidence names a retrieved source or the artifact that
    --    holds it. A model's recollection of a paper has neither, so it is not
    --    storable here, which is how "model memory is never sufficient to
    --    establish novelty" stops being an instruction in a prompt.
    constraint idea_evidence_literature_ck check (
        kind <> 'literature' or literature_key is not null or artifact_id is not null),

    -- An evidence row with nothing under it is prose.
    constraint idea_evidence_grounded_ck check (
        artifact_id is not null or finding_id is not null or job_id is not null
        or literature_key is not null),

    -- An experiment's evidence is of an execution, not of an intention.
    constraint idea_evidence_experiment_ck check (
        kind <> 'experiment' or job_id is not null or finding_id is not null)
);
create index if not exists idea_evidence_idea_idx on idea_evidence(idea_id, idea_version);
create index if not exists idea_evidence_kind_idx on idea_evidence(idea_id, kind);

-- ----------------------------------------------------------- idea_reviews --
-- Every version-bound evaluative judgement: the falsifier, the three
-- independent reviewers, the replicator and the meta-reviewer.
--
-- One table rather than several because they share the only three things that
-- matter structurally -- they bind to a version, they carry provenance for an
-- independence claim, and they can raise objections -- and differ only in the
-- question asked, which is `reviewer_role`.
create table if not exists idea_reviews (
    review_id               text        primary key,
    idea_id                 text        not null,
    idea_version            integer     not null,
    reviewer_role           text        not null,
    verdict                 text        not null,
    severity                text        not null default 'NONE',
    summary                 text        not null,
    -- The meta-reviewer's workflow recommendation. Null for every other role,
    -- because only the meta-reviewer is asked what should happen next -- and it
    -- is a recommendation, which `gates.py` may lower and may never raise.
    recommendation          text,
    detail_artifact_id      text        references artifacts(artifact_id) on delete set null,

    -- 3. The binding, and it is two digests rather than one.
    --
    -- `reviewed_content_digest` is the idea version's material content.
    -- `reviewed_evidence_digest` is the *set of evidence rows that version
    -- linked at the moment this review was written*. Both are copied here at
    -- write time rather than joined.
    --
    -- The second one closes the hole the kernel already closed for Claims. A
    -- Review of a Claim carries `evidence_digests` covering every Evidence
    -- object the Claim links, because otherwise the evidence can be swapped
    -- under a standing approval and the approval still reads as current
    -- (`docs/CAPSULE.md`, the Claim acceptance rule). Without this column the
    -- portfolio layer reopened exactly that: get three reviews, replace every
    -- evidence row, and the gate sees three "live" reviews beside evidence no
    -- reviewer ever saw.
    reviewed_content_digest  text       not null,
    reviewed_evidence_digest text       not null,
    -- The exact packet the reviewer read, by digest. Two reviewers of one
    -- version read identical packets, and neither packet contains the other's
    -- verdict -- ReviewPacket has no field for one.
    packet_digest           text        not null,
    -- The prompt identity, `role@version`. Part of liveness: a review produced
    -- by a prompt that has since been superseded is a review of a question no
    -- longer being asked, and the gate treats it as stale.
    prompt_version          text        not null,

    -- Independence provenance. Recorded, never assumed. See docs/adr/0003.
    --
    -- `independence_vs_origin` is NOT a copy of `model_calls.independence`.
    -- They answer different questions and storing one under the other's name
    -- is how a row comes to say two things. The router's value is per
    -- independence *group*: "which provider family has already answered in
    -- this group". This column is the comparison this layer's gate actually
    -- needs: the review's model call against the *version's origin call*. It
    -- is derived from `model_calls` at write time by
    -- `research_os.portfolio.gates.classify_independence`, and it reuses
    -- `research_os.runtime.interfaces.Independence`'s vocabulary verbatim
    -- rather than inventing a second one.
    --
    -- There is deliberately no `SAME_CALL` value. A review whose call *is* the
    -- origin call is not a weak review, it is a defect, and the classifier
    -- raises instead of returning something storable.
    call_id                 text        references model_calls(call_id) on delete set null,
    provider                text        not null,
    model                   text,
    provider_family         text        not null,
    independence_vs_origin  text        not null,
    context_class           text        not null,
    independence_note       text        not null default '',

    created_at              timestamptz not null default now(),

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint idea_reviews_role_ck check (reviewer_role in (
        'falsifier','methodology_reviewer','novelty_reviewer','skeptic_reviewer',
        'replicator','meta_reviewer')),
    constraint idea_reviews_verdict_ck check (verdict in (
        'PASS','PASS_WITH_OBJECTIONS','REVISE','REJECT','INCONCLUSIVE')),
    constraint idea_reviews_severity_ck check (severity in (
        'NONE','MINOR','MAJOR','CRITICAL','FATAL')),
    constraint idea_reviews_independence_ck check (independence_vs_origin in (
        'none','different_context','different_model','different_family')),
    constraint idea_reviews_context_ck check (context_class in (
        'FROZEN_PACKET','SHARED_CONTEXT')),
    -- Only the meta-reviewer recommends.
    constraint idea_reviews_recommendation_ck check (
        recommendation is null or reviewer_role = 'meta_reviewer'),
    -- A verdict of PASS with a standing objection is a contradiction, and it is
    -- the shape a persuasive reviewer would produce.
    constraint idea_reviews_pass_ck check (
        verdict <> 'PASS' or severity = 'NONE')
);
-- "The same review request is idempotent." One reviewer answers one version's
-- one content digest once.
create unique index if not exists idea_reviews_identity_idx
    on idea_reviews(idea_id, idea_version, reviewer_role, reviewed_content_digest,
                    reviewed_evidence_digest);
create index if not exists idea_reviews_idea_idx on idea_reviews(idea_id, created_at desc);

-- -------------------------------------------------------- idea_objections --
-- Objections are their own rows, and this is the one place the table count
-- grew past the brief's list. It is here because the gate has to *count*
-- unresolved objections, and because an objection has to be able to survive
-- the revision that claims to answer it.
--
-- The anti-gaming property: revising an idea makes every review of the prior
-- version stale, including the approvals. So an objection cannot be dropped by
-- revising while keeping a passing review -- reaching the gate again costs a
-- full re-review. And an objection raised at version N stays unresolved at
-- version N+1 unless N+1 explicitly addressed it, so "revise until a
-- stochastic reviewer forgets" leaves the original row standing.
create table if not exists idea_objections (
    objection_id         text        primary key,
    idea_id              text        not null references ideas(idea_id) on delete cascade,
    raised_in_review     text        not null references idea_reviews(review_id) on delete cascade,
    raised_at_version    integer     not null,
    -- A hash of the normalised objection text. The stickiness key: the "same"
    -- objection raised again by a different reviewer, or at a later version, is
    -- recognisably the same one.
    objection_key        text        not null,
    severity             text        not null,
    summary              text        not null,

    addressed_at_version integer,
    response             text,
    -- Who established that it was answered. Not the producer, and not the
    -- revision that claims to answer it: a later review, by a role other than
    -- the one that raised the objection, that read the new version and did not
    -- re-raise it. Without this column "addressed" is a field the generating
    -- side can write, which makes the whole review board advisory.
    resolved_by_review   text        references idea_reviews(review_id) on delete restrict,
    resolved_at          timestamptz,

    created_at           timestamptz not null default now(),

    constraint idea_objections_severity_ck check (severity in (
        'MINOR','MAJOR','CRITICAL','FATAL')),
    constraint idea_objections_resolution_ck check (
        (addressed_at_version is null) = (resolved_at is null)),
    constraint idea_objections_resolver_ck check (
        (resolved_by_review is null) = (resolved_at is null)),
    constraint idea_objections_response_ck check (
        resolved_at is null or (response is not null and response <> '')),
    -- Resolving an objection requires a *later* version than the one that
    -- raised it. You cannot answer an objection in the text it objects to.
    constraint idea_objections_forward_ck check (
        addressed_at_version is null or addressed_at_version > raised_at_version)
);
create unique index if not exists idea_objections_key_idx
    on idea_objections(idea_id, objection_key, raised_at_version);
create index if not exists idea_objections_open_idx
    on idea_objections(idea_id, severity) where resolved_at is null;
