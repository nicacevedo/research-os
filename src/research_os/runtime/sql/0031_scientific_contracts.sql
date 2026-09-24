-- The scientific contract: hypothesis + analysis + design, frozen before any
-- result exists, and immutable afterwards by the database rather than by
-- the absence of a setter.
--
-- Why a table and not three more columns on `idea_experiments`. An
-- experiment row is an *execution* state machine -- PROPOSED, RUNNING,
-- OPERATIONALLY_FAILED, INTERPRETED -- and one contract may be executed more
-- than once: an implementation repair re-runs the same frozen science with a
-- different time limit, and a capability that arrives later resumes a
-- contract whose analysis was frozen weeks earlier. The science and the
-- attempts to measure it have different lifetimes, so they are different
-- rows, and the experiment points at the contract it executes.
--
-- Two halves, frozen at two moments, by two roles:
--
--   analysis   authored first, blind to any design: estimand, observables,
--              inclusion rules, reductions, primary statistic, uncertainty,
--              the two predicates, the support the data must exhibit.
--              State ANALYSIS_FROZEN.
--   design     authored second, against the frozen analysis, whose
--              thresholds its author is not shown: command, grid,
--              parameters, seeds. State FROZEN -- or BLOCKED_CAPABILITY when
--              no declared command can produce the analysis's observables.
--
-- `contract_digest` covers both halves and the hypothesis (the idea
-- version's content digest). It deliberately covers no provider, model,
-- call id or prompt identity: those are provenance, recorded beside the
-- digest, and a contract re-derived by a different model family with the
-- same content is the same science.

create table if not exists scientific_contracts (
    contract_id          text        primary key,
    project_id           text        not null references projects(project_id) on delete cascade,
    idea_id              text        not null,
    idea_version         integer     not null,
    role                 text        not null,
    kind                 text        not null default 'PREREGISTERED',
    state                text        not null,

    -- The hypothesis this contract tests, by the version's content digest.
    hypothesis_digest    text        not null,

    -- The analysis half. `analysable` false is a frozen, recorded answer:
    -- the observables available do not identify the quantity, so any
    -- measurement of this idea version is capped at INSUFFICIENT.
    analysable           boolean     not null,
    analysis_digest      text        not null,
    analysis_artifact_id text        not null references artifacts(artifact_id),
    analysis_prompt      text        not null default '',
    analysis_call_id     text,

    -- The design half, and the whole.
    design_digest        text,
    design_artifact_id   text        references artifacts(artifact_id),
    design_prompt        text,
    design_call_id       text,
    contract_digest      text,
    contract_artifact_id text        references artifacts(artifact_id),

    -- A post-result change never edits a contract: it creates an EXPLORATORY
    -- one naming the contract it departs from.
    parent_contract_id   text        references scientific_contracts(contract_id),

    -- When no declared command can produce the analysis's observables: what
    -- one would have to do, and the declared command set it was judged
    -- against, so a change to `experiments.yaml` is observable.
    capability_request   jsonb,
    command_set_digest   text,

    detail               text,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    frozen_at            timestamptz,

    foreign key (idea_id, idea_version)
        references idea_versions(idea_id, version) on delete cascade,

    constraint scientific_contracts_role_ck check (role in ('PRIMARY','REPLICATION')),
    constraint scientific_contracts_kind_ck check (kind in ('PREREGISTERED','EXPLORATORY')),
    constraint scientific_contracts_state_ck check (state in (
        'ANALYSIS_FROZEN','FROZEN','BLOCKED_CAPABILITY','SUPERSEDED')),
    -- A FROZEN contract has both halves and the digest over them.
    constraint scientific_contracts_frozen_ck check (
        state <> 'FROZEN' or (
            design_digest is not null and design_artifact_id is not null
            and contract_digest is not null and contract_artifact_id is not null)),
    -- A capability block says what capability.
    constraint scientific_contracts_blocked_ck check (
        state <> 'BLOCKED_CAPABILITY' or capability_request is not null),
    -- An exploratory contract departs from something.
    constraint scientific_contracts_exploratory_ck check (
        kind <> 'EXPLORATORY' or parent_contract_id is not null)
);

-- One live preregistered contract per (idea version, role). Partial, so a
-- superseded contract stays as the record of what was frozen and why it
-- stopped being the one asked for, and exploratory contracts -- which may be
-- several -- do not occupy the name.
create unique index if not exists scientific_contracts_live_idx
    on scientific_contracts(idea_id, idea_version, role)
    where state <> 'SUPERSEDED' and kind = 'PREREGISTERED';
create index if not exists scientific_contracts_project_idx
    on scientific_contracts(project_id, state);
create index if not exists scientific_contracts_parent_idx
    on scientific_contracts(parent_contract_id);

-- Immutability, enforced where a migration, a restore, a psql session and a
-- future keyword argument all have to pass through it.
--
-- The analysis half never changes after insert. The design half never
-- changes once written. SUPERSEDED is terminal and nothing about a superseded
-- contract changes again; FROZEN may only become SUPERSEDED, and not at all
-- once a measurement has been *read* under it -- otherwise a second
-- preregistration of the same question could be frozen after the first
-- result existed. Nothing re-enters ANALYSIS_FROZEN. A delete is refused
-- while the project exists: only deleting the project itself (an operator
-- act on operational state, by which time the project row is gone) removes
-- a contract, and deleting an idea or a version does not.
create or replace function scientific_contracts_immutable() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from projects where project_id = old.project_id) then
            raise exception
                'scientific contract % is a frozen scientific record and is never deleted',
                old.contract_id
                using errcode = 'check_violation';
        end if;
        return old;
    end if;
    if old.state = 'SUPERSEDED' and (
           new.state <> old.state
           or new.design_digest is distinct from old.design_digest
           or new.contract_digest is distinct from old.contract_digest
           or new.frozen_at is distinct from old.frozen_at
           or new.capability_request is distinct from old.capability_request) then
        raise exception 'scientific contract % is superseded and nothing about it changes',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    if old.state = 'FROZEN' and new.state = 'SUPERSEDED' and exists (
           select 1 from idea_experiments e
            where e.contract_id = old.contract_id
              and (e.state = 'INTERPRETED' or e.evidence_id is not null
                   or e.analysis_artifact_id is not null)) then
        raise exception
            'scientific contract % has a measurement read under it and cannot be superseded',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    if new.contract_id <> old.contract_id
       or new.project_id <> old.project_id
       or new.idea_id <> old.idea_id
       or new.idea_version <> old.idea_version
       or new.role <> old.role
       or new.kind <> old.kind
       or new.hypothesis_digest <> old.hypothesis_digest
       or new.analysable <> old.analysable
       or new.analysis_digest <> old.analysis_digest
       or new.analysis_artifact_id <> old.analysis_artifact_id
       or new.analysis_prompt <> old.analysis_prompt
       or new.analysis_call_id is distinct from old.analysis_call_id
       or new.parent_contract_id is distinct from old.parent_contract_id
       or new.created_at <> old.created_at then
        raise exception
            'the analysis half of scientific contract % is frozen; a changed rule is a new EXPLORATORY contract, never an edit',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    if old.design_digest is not null and (
           new.design_digest is distinct from old.design_digest
           or new.design_artifact_id is distinct from old.design_artifact_id
           or new.design_prompt is distinct from old.design_prompt
           or new.design_call_id is distinct from old.design_call_id
           or new.contract_digest is distinct from old.contract_digest
           or new.contract_artifact_id is distinct from old.contract_artifact_id
           or new.frozen_at is distinct from old.frozen_at) then
        raise exception
            'the design half of scientific contract % is frozen',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    if old.state = 'SUPERSEDED' and new.state <> 'SUPERSEDED' then
        raise exception 'scientific contract % is superseded and stays so', old.contract_id
            using errcode = 'check_violation';
    end if;
    if old.state = 'FROZEN' and new.state not in ('FROZEN','SUPERSEDED') then
        raise exception 'scientific contract % is frozen; it can only be superseded',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    if new.state = 'ANALYSIS_FROZEN' and old.state <> 'ANALYSIS_FROZEN' then
        raise exception 'scientific contract % cannot return to ANALYSIS_FROZEN',
            old.contract_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists scientific_contracts_immutable_trg on scientific_contracts;
create trigger scientific_contracts_immutable_trg
    before update or delete on scientific_contracts
    for each row execute function scientific_contracts_immutable();

-- An experiment names the contract it executes. Nullable, because every
-- experiment written before this migration executed a design that carried
-- its own rule, and it stays interpretable exactly as it was.
alter table idea_experiments
    add column if not exists contract_id text references scientific_contracts(contract_id);
create index if not exists idea_experiments_contract_idx
    on idea_experiments(contract_id);

-- What an experiment preregistered is immutable too. Nothing in the
-- application updates these columns -- `_preregistered` re-verifies the
-- specification and the rule against the stored artifact precisely because
-- that absence of a setter was the only thing holding them -- and now the
-- database says so as well.
create or replace function idea_experiments_frozen() returns trigger
language plpgsql as $$
begin
    -- A reading, once recorded, is the record: set once, and an INTERPRETED
    -- experiment stays INTERPRETED. The job may change only while nothing
    -- has been read from it -- a retry after an operational failure is a new
    -- submission of the same preregistration, and a finished run is not.
    if (old.job_id is not null and new.job_id is distinct from old.job_id
           and (old.state in ('COMPLETED','INTERPRETED')
                or old.analysis_artifact_id is not null))
       or (old.analysis_artifact_id is not null
           and new.analysis_artifact_id is distinct from old.analysis_artifact_id)
       or (old.conclusion is not null and new.conclusion is distinct from old.conclusion)
       or (old.evidence_id is not null and new.evidence_id is distinct from old.evidence_id)
       or (old.state = 'INTERPRETED' and new.state <> 'INTERPRETED') then
        raise exception
            'experiment % has been read, and what was read is not rewritten',
            old.experiment_id
            using errcode = 'check_violation';
    end if;
    if new.experiment_id <> old.experiment_id
       or new.idea_id <> old.idea_id
       or new.idea_version <> old.idea_version
       or new.role <> old.role
       or new.command <> old.command
       or new.spec_digest <> old.spec_digest
       or new.variation_digest <> old.variation_digest
       or new.workspace_path <> old.workspace_path
       or new.preregistration_artifact_id is distinct from old.preregistration_artifact_id
       or new.decision_rule is distinct from old.decision_rule
       or new.no_rule_reason is distinct from old.no_rule_reason
       or new.contract_id is distinct from old.contract_id then
        raise exception
            'what experiment % preregistered is frozen; a changed specification or rule is a new experiment',
            old.experiment_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists idea_experiments_frozen_trg on idea_experiments;
create trigger idea_experiments_frozen_trg
    before update on idea_experiments
    for each row execute function idea_experiments_frozen();

-- The declared experiment capability a portfolio last saw. A change to it is
-- a person's act -- `experiments.yaml` lives outside every worktree -- and
-- the tick reads it the way a researcher typing `portfolio resume` is read:
-- as the fact that a blocked capability may have arrived.
alter table portfolio_state
    add column if not exists command_set_digest text;
