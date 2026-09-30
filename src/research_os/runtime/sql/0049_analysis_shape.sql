-- The analysis is checked against the capability envelope before it is
-- frozen (docs/SCIENCE_EXECUTION.md §2a).
--
-- The second final v1 qualification (66d5704) had two lineages reach the
-- evidence stage, and both analyses froze `fixed_single_execution` for a
-- sample one execution of their capability could not hold; the experiment
-- designer saw the per-execution bound afterwards and refused, correctly,
-- and the contracts were blocked for good. The analysis is now checked, by
-- ordinary code, against an envelope derived from the committed capability
-- declaration, the host's commands and the human-set bounds -- before it is
-- frozen. What this adds, and what the database itself refuses:
--
--   1. `scientific_contracts.envelope_digest` -- the envelope the analysis
--      was checked against. Written with the analysis half and never
--      changed: a changed envelope is a new check, and an unread contract
--      whose envelope no longer holds is superseded by the route, never
--      rebound.
--   2. `analysis_drafts` -- every proposed analysis the check refused
--      (EXECUTION_SHAPE_MISMATCH or CAPABILITY_LIMITED), with the envelope
--      it was refused under and the structured reason. Immutable, never
--      deleted while its project exists, and written only while no
--      preregistered contract is live for its version and role.
--   3. A contract whose analysis is a draft refused under the same envelope
--      -- or under any envelope, when the contract names none -- is refused
--      at insert. A refused draft is never frozen, whatever calls the store.

alter table scientific_contracts
    add column if not exists envelope_digest text;

create or replace function scientific_contracts_envelope_fixed() returns trigger
language plpgsql as $$
begin
    if new.envelope_digest is distinct from old.envelope_digest then
        raise exception
            'scientific contract % was checked against envelope %; that binding is part of its frozen analysis and never changes',
            old.contract_id, coalesce(old.envelope_digest, '(none)')
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists scientific_contracts_envelope_fixed_trg on scientific_contracts;
create trigger scientific_contracts_envelope_fixed_trg
    before update on scientific_contracts
    for each row execute function scientific_contracts_envelope_fixed();

create table if not exists analysis_drafts (
    draft_id          text        primary key,
    project_id        text        not null references projects(project_id) on delete cascade,
    idea_id           text        not null,
    idea_version      integer     not null,
    role              text        not null,
    verdict           text        not null,
    -- The proposal, by its analysis digest, and the artifact holding it with
    -- the check that refused it.
    analysis_digest   text        not null,
    artifact_id       text        not null references artifacts(artifact_id),
    envelope_digest   text        not null,
    code_commit       text        not null,
    check_record      jsonb       not null,
    analysis_prompt   text        not null default '',
    analysis_call_id  text,
    created_at        timestamptz not null default clock_timestamp(),

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint analysis_drafts_role_ck check (role in ('PRIMARY','REPLICATION')),
    constraint analysis_drafts_verdict_ck check (
        verdict in ('EXECUTION_SHAPE_MISMATCH','CAPABILITY_LIMITED')),
    constraint analysis_drafts_envelope_ck check (envelope_digest like 'renv-v1:%')
);
create index if not exists analysis_drafts_version_idx
    on analysis_drafts(idea_id, idea_version, role, envelope_digest, created_at);

-- A draft is a record of what was proposed and refused. Nothing about it
-- changes, and it is not deleted while its project exists -- the rule 0031
-- applies to a contract, for the same reason: the refusal is what the
-- revision was shown and what the bounded revision is counted from.
create or replace function analysis_drafts_immutable() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from projects where project_id = old.project_id) then
            raise exception
                'analysis draft % is the record of a refused proposal and is never deleted',
                old.draft_id
                using errcode = 'check_violation';
        end if;
        return old;
    end if;
    raise exception 'analysis draft % is the record of a refused proposal and never changes',
        old.draft_id
        using errcode = 'check_violation';
end;
$$;

drop trigger if exists analysis_drafts_immutable_trg on analysis_drafts;
create trigger analysis_drafts_immutable_trg
    before update or delete on analysis_drafts
    for each row execute function analysis_drafts_immutable();

-- A proposal is refused *before* anything is frozen. Once a contract is live
-- for the version and role there is nothing left to propose, and a draft
-- written then would be a second preregistration of one question, refused.
create or replace function analysis_drafts_insert() returns trigger
language plpgsql as $$
begin
    if exists (
        select 1 from scientific_contracts c
         where c.idea_id = new.idea_id
           and c.idea_version = new.idea_version
           and c.role = new.role
           and c.kind = 'PREREGISTERED'
           and c.state <> 'SUPERSEDED') then
        raise exception
            'analysis draft of % v% % is refused: a contract for it is already frozen',
            new.idea_id, new.idea_version, new.role
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists analysis_drafts_insert_trg on analysis_drafts;
create trigger analysis_drafts_insert_trg
    before insert on analysis_drafts
    for each row execute function analysis_drafts_insert();

-- The guarantee the check exists for, held where every caller has to pass
-- through it: an analysis refused under an envelope is never frozen under
-- that envelope, and never frozen under none.
create or replace function scientific_contracts_not_a_refused_draft() returns trigger
language plpgsql as $$
begin
    if exists (
        select 1 from analysis_drafts d
         where d.idea_id = new.idea_id
           and d.idea_version = new.idea_version
           and d.role = new.role
           and d.analysis_digest = new.analysis_digest
           and (new.envelope_digest is null
                or d.envelope_digest = new.envelope_digest)) then
        raise exception
            'scientific contract % freezes analysis %, which the execution-shape check refused before freezing under %',
            new.contract_id, new.analysis_digest,
            coalesce(new.envelope_digest, 'an envelope this contract does not name')
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists scientific_contracts_not_a_refused_draft_trg on scientific_contracts;
create trigger scientific_contracts_not_a_refused_draft_trg
    before insert on scientific_contracts
    for each row execute function scientific_contracts_not_a_refused_draft();
