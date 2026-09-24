-- Which frozen contract a replication replicates, and which contract a
-- measurement is read under, as relationships the database checks.
--
-- `scientific_contracts.parent_contract_id` has existed since 0031, as a
-- foreign key the immutability trigger never lets change. It named an
-- EXPLORATORY contract's parent; a replication's contract left it empty and
-- recorded the primary only in its analysis document. From this release a
-- replication's contract names the primary contract it inherits, and its
-- digest commits to that contract's digest.
--
-- A foreign key says the parent exists. It does not say the parent is the
-- right one -- another idea's contract, another version's, another
-- replication -- and neither did anything else. This trigger does, on
-- insert: after insert the column cannot change anyway.
--
-- Rows written before this migration are not touched. A replication
-- contract frozen earlier keeps an empty parent: its digest was computed
-- without one, so naming a parent now would make it fail verification, and
-- the only record of its primary is document text this migration will not
-- parse into a relationship. Empty means "not recorded", never a guess:
-- either the primary predated contracts, or the replication's analysis was
-- frozen before this migration (its `analysis_prompt` then reads
-- `inherited:PEXP-...` or `inherited:PCON-...` respectively).
create or replace function scientific_contracts_lineage() returns trigger
language plpgsql as $$
declare
    parent scientific_contracts%rowtype;
begin
    if new.parent_contract_id is null then
        return new;
    end if;
    select * into parent from scientific_contracts
     where contract_id = new.parent_contract_id;
    if not found then
        raise exception 'contract % names parent %, which does not exist',
            new.contract_id, new.parent_contract_id
            using errcode = 'foreign_key_violation';
    end if;
    if parent.project_id <> new.project_id
       or parent.idea_id <> new.idea_id
       or parent.idea_version <> new.idea_version then
        raise exception
            'contract % tests % v%, and its parent % tests % v%; a contract departs only from one for the same hypothesis',
            new.contract_id, new.idea_id, new.idea_version,
            parent.contract_id, parent.idea_id, parent.idea_version
            using errcode = 'check_violation';
    end if;
    if parent.kind <> 'PREREGISTERED' or parent.state <> 'FROZEN' then
        raise exception
            'contract % names parent %, which is % and %; only a frozen preregistered contract is inherited from',
            new.contract_id, parent.contract_id, parent.kind, parent.state
            using errcode = 'check_violation';
    end if;
    if new.kind = 'PREREGISTERED'
       and not (new.role = 'REPLICATION' and parent.role = 'PRIMARY') then
        raise exception
            'preregistered contract % (%) names parent % (%); only a replication inherits, and only from its primary',
            new.contract_id, new.role, parent.contract_id, parent.role
            using errcode = 'check_violation';
    end if;
    -- And a replication inherits: it freezes exactly its primary's analysis.
    if new.kind = 'PREREGISTERED'
       and (new.analysis_digest <> parent.analysis_digest
            or new.analysable <> parent.analysable) then
        raise exception
            'replication contract % names % as its primary and freezes a different analysis',
            new.contract_id, parent.contract_id
            using errcode = 'check_violation';
    end if;
    if new.kind = 'EXPLORATORY' and new.role <> parent.role then
        raise exception
            'exploratory contract % (%) departs from % (%), a contract of another role',
            new.contract_id, new.role, parent.contract_id, parent.role
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
drop trigger if exists scientific_contracts_lineage_trg on scientific_contracts;
create trigger scientific_contracts_lineage_trg
    before insert on scientific_contracts
    for each row execute function scientific_contracts_lineage();

-- A measurement is read under the preregistered contract of its own idea
-- version and role, and no other. The application checks this before
-- anything runs or is read; the database refuses to record the pairing at
-- all. `idea_experiments.contract_id` cannot change after insert (0031), so
-- checking the insert is checking the row.
create or replace function idea_experiments_contract_match() returns trigger
language plpgsql as $$
declare
    bound scientific_contracts%rowtype;
begin
    if new.contract_id is null then
        return new;
    end if;
    select * into bound from scientific_contracts where contract_id = new.contract_id;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    if bound.idea_id <> new.idea_id
       or bound.idea_version <> new.idea_version
       or bound.role <> new.role
       or bound.kind <> 'PREREGISTERED' then
        raise exception
            'experiment % (% v% %) cannot be read under contract % (% v% % %)',
            new.experiment_id, new.idea_id, new.idea_version, new.role,
            bound.contract_id, bound.idea_id, bound.idea_version, bound.role, bound.kind
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
drop trigger if exists idea_experiments_contract_match_trg on idea_experiments;
create trigger idea_experiments_contract_match_trg
    before insert on idea_experiments
    for each row execute function idea_experiments_contract_match();
