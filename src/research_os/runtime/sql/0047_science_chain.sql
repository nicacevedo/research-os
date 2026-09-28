-- The frozen science chain: scientific contract -> experimental design ->
-- capability binding -> execution plan -> execution -> system-computed outcome
-- (docs/SCIENCE_EXECUTION.md).
--
-- What this adds, and what the database itself refuses:
--
--   1. `science_objects` -- the three frozen objects, content-addressed. The
--      digest is the primary key, so a change to any of them is a new row
--      with a new identity and never an edit. A DESIGN names the CONTRACT it
--      realises, a PLAN names the DESIGN it realises and the capability it is
--      bound to; each is refused unless its parent exists, belongs to the
--      same idea version and was frozen no later than it. Immutable.
--   2. `idea_experiments.plan_digest` -- the plan an execution realises. Set
--      when the experiment is recorded, never changed, and refused unless it
--      is a PLAN of the same idea version whose specification digest is the
--      experiment's.
--   3. `execution_receipts.plan_digest` -- the runner binds the receipt of a
--      plan-bound execution to that plan; a receipt naming another plan, or
--      none, is refused.
--   4. `science_outcomes` -- every system-computed outcome, one row per
--      determination, immutable. A reading's outcome (SUPPORTED, REFUTED,
--      INCONCLUSIVE) must name its whole chain -- the three frozen objects,
--      the capability, the execution's receipt and the validated result's
--      digest -- and the chain must be the experiment's own: its plan, the
--      plan's design, the design's contract, and a receipt of that execution
--      of that plan. One reading per execution.

create table if not exists science_objects (
    object_digest     text        primary key,
    kind              text        not null,
    project_id        text        not null references projects(project_id),
    idea_id           text        not null,
    idea_version      integer     not null,
    parent_digest     text        references science_objects(object_digest),
    artifact_id       text        not null references artifacts(artifact_id),
    capability_ref    text,
    capability_digest text,
    spec_digest       text,
    frozen_at         timestamptz not null default clock_timestamp(),

    constraint science_objects_kind_ck check (kind in ('CONTRACT','DESIGN','PLAN')),
    -- A contract has no parent; a design realises a contract; a plan realises
    -- a design and is bound to exactly one capability and one specification.
    constraint science_objects_shape_ck check (
        (kind = 'CONTRACT' and parent_digest is null and capability_ref is null
            and capability_digest is null and spec_digest is null)
        or (kind = 'DESIGN' and parent_digest is not null and capability_ref is null
            and capability_digest is null and spec_digest is null)
        or (kind = 'PLAN' and parent_digest is not null and capability_ref is not null
            and capability_digest is not null and spec_digest is not null)),
    -- The digest is the content's, and its prefix says which kind it is.
    constraint science_objects_digest_ck check (
        (kind = 'CONTRACT' and object_digest = 'rscontract-v1:' || artifact_id)
        or (kind = 'DESIGN' and object_digest = 'rsdesign-v1:' || artifact_id)
        or (kind = 'PLAN' and object_digest = 'rsplan-v1:' || artifact_id))
);
create index if not exists science_objects_idea_idx
    on science_objects(idea_id, idea_version, kind);

create or replace function science_objects_insert() returns trigger
language plpgsql as $$
declare
    parent science_objects%rowtype;
begin
    if new.parent_digest is null then
        return new;
    end if;
    select * into parent from science_objects where object_digest = new.parent_digest;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    if (new.kind = 'DESIGN' and parent.kind <> 'CONTRACT')
       or (new.kind = 'PLAN' and parent.kind <> 'DESIGN') then
        raise exception
            'a % realises a %, and % is a %',
            lower(new.kind), case new.kind when 'DESIGN' then 'contract' else 'design' end,
            new.parent_digest, lower(parent.kind)
            using errcode = 'check_violation';
    end if;
    if parent.project_id <> new.project_id or parent.idea_id <> new.idea_id
       or parent.idea_version <> new.idea_version then
        raise exception
            '% % belongs to % v%, and its parent % to % v%',
            lower(new.kind), new.object_digest, new.idea_id, new.idea_version,
            parent.object_digest, parent.idea_id, parent.idea_version
            using errcode = 'check_violation';
    end if;
    if parent.frozen_at > new.frozen_at then
        raise exception
            'nothing is frozen before what it realises: % would precede its parent %',
            new.object_digest, parent.object_digest
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists science_objects_insert_trg on science_objects;
create trigger science_objects_insert_trg
    before insert on science_objects
    for each row execute function science_objects_insert();

-- `trusted_provenance_immutable` is 0046's: never updated, never deleted.
drop trigger if exists science_objects_immutable_trg on science_objects;
create trigger science_objects_immutable_trg
    before update or delete on science_objects
    for each row execute function trusted_provenance_immutable();

alter table idea_experiments
    add column if not exists plan_digest text references science_objects(object_digest);
alter table execution_receipts
    add column if not exists plan_digest text references science_objects(object_digest);

-- An execution realises a plan of its own idea version whose specification is
-- the one it preregistered; and it keeps that plan.
create or replace function idea_experiments_plan() returns trigger
language plpgsql as $$
declare
    plan science_objects%rowtype;
begin
    if tg_op = 'UPDATE' and new.plan_digest is distinct from old.plan_digest then
        raise exception
            'the plan experiment % realises is frozen; a different plan is a new experiment',
            old.experiment_id
            using errcode = 'check_violation';
    end if;
    if tg_op = 'INSERT' and new.plan_digest is not null then
        select * into plan from science_objects where object_digest = new.plan_digest;
        if found and (plan.kind <> 'PLAN'
                      or plan.idea_id <> new.idea_id
                      or plan.idea_version <> new.idea_version
                      or plan.spec_digest <> new.spec_digest) then
            raise exception
                'experiment % names % , which is not a plan of % v% for its specification',
                new.experiment_id, new.plan_digest, new.idea_id, new.idea_version
                using errcode = 'check_violation';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists idea_experiments_plan_trg on idea_experiments;
create trigger idea_experiments_plan_trg
    before insert or update on idea_experiments
    for each row execute function idea_experiments_plan();

-- A receipt of a plan-bound execution names that plan; any other receipt names none.
create or replace function execution_receipts_plan() returns trigger
language plpgsql as $$
declare
    wanted text;
begin
    select plan_digest into wanted from idea_experiments
     where experiment_id = new.experiment_id;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    if new.plan_digest is distinct from wanted then
        raise exception
            'receipt % names plan % and experiment % realises %',
            new.receipt_id, coalesce(new.plan_digest, '(none)'), new.experiment_id,
            coalesce(wanted, '(none)')
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists execution_receipts_plan_trg on execution_receipts;
create trigger execution_receipts_plan_trg
    before insert on execution_receipts
    for each row execute function execution_receipts_plan();

create table if not exists science_outcomes (
    outcome_id         text        primary key,
    project_id         text        not null references projects(project_id),
    idea_id            text        not null,
    idea_version       integer     not null,
    role               text        not null,
    state              text        not null,
    reason             text        not null,
    contract_id        text        references scientific_contracts(contract_id),
    experiment_id      text        references idea_experiments(experiment_id),
    contract_digest    text        references science_objects(object_digest),
    design_digest      text        references science_objects(object_digest),
    plan_digest        text        references science_objects(object_digest),
    capability_ref     text,
    capability_digest  text,
    receipt_id         text        references execution_receipts(receipt_id),
    result_sha256      text,
    estimate           double precision,
    record_artifact_id text        not null references artifacts(artifact_id),
    created_at         timestamptz not null default clock_timestamp(),

    constraint science_outcomes_role_ck check (role in ('PRIMARY','REPLICATION')),
    constraint science_outcomes_state_ck check (state in (
        'SUPPORTED','REFUTED','INCONCLUSIVE','EXECUTION_FAILED',
        'CAPABILITY_LIMITED','INVALID_EVIDENCE','BUDGET_LIMITED')),
    -- A reading names its whole chain, down to the validated result's bytes.
    constraint science_outcomes_reading_ck check (
        state not in ('SUPPORTED','REFUTED','INCONCLUSIVE')
        or (experiment_id is not null and contract_digest is not null
            and design_digest is not null and plan_digest is not null
            and capability_ref is not null and capability_digest is not null
            and receipt_id is not null and result_sha256 is not null)),
    -- An execution that failed, or a reading refused as evidence, is of one
    -- execution; a capability or budget limit is of one contract.
    constraint science_outcomes_subject_ck check (
        (state not in ('EXECUTION_FAILED','INVALID_EVIDENCE') or experiment_id is not null)
        and (state not in ('CAPABILITY_LIMITED','BUDGET_LIMITED')
             or contract_id is not null or experiment_id is not null))
);
create index if not exists science_outcomes_idea_idx
    on science_outcomes(idea_id, idea_version, role, created_at);
-- One reading per execution: a receipt is read once.
create unique index if not exists science_outcomes_one_reading_idx
    on science_outcomes(receipt_id)
    where receipt_id is not null
      and state in ('SUPPORTED','REFUTED','INCONCLUSIVE','INVALID_EVIDENCE');

create or replace function science_outcomes_insert() returns trigger
language plpgsql as $$
declare
    exp idea_experiments%rowtype;
    rct execution_receipts%rowtype;
    plan science_objects%rowtype;
    design science_objects%rowtype;
begin
    if new.experiment_id is not null then
        select * into exp from idea_experiments where experiment_id = new.experiment_id;
        if found and (exp.idea_id <> new.idea_id or exp.idea_version <> new.idea_version
                      or exp.role <> new.role) then
            raise exception 'outcome % is not of experiment %',
                new.outcome_id, new.experiment_id
                using errcode = 'check_violation';
        end if;
        if found and new.plan_digest is distinct from exp.plan_digest
           and new.plan_digest is not null then
            raise exception
                'outcome % names plan % and experiment % realises %',
                new.outcome_id, new.plan_digest, new.experiment_id,
                coalesce(exp.plan_digest, '(none)')
                using errcode = 'check_violation';
        end if;
    end if;
    if new.receipt_id is not null then
        select * into rct from execution_receipts where receipt_id = new.receipt_id;
        if found and (rct.experiment_id is distinct from new.experiment_id
                      or rct.plan_digest is distinct from new.plan_digest) then
            raise exception
                'outcome % reads receipt % , which is not of experiment % under plan %',
                new.outcome_id, new.receipt_id, new.experiment_id,
                coalesce(new.plan_digest, '(none)')
                using errcode = 'check_violation';
        end if;
    end if;
    if new.plan_digest is not null then
        select * into plan from science_objects where object_digest = new.plan_digest;
        if found and (plan.kind <> 'PLAN'
                      or plan.parent_digest is distinct from new.design_digest
                      or plan.capability_digest is distinct from new.capability_digest
                      or plan.capability_ref is distinct from new.capability_ref) then
            raise exception
                'outcome % names a design or capability its plan % does not',
                new.outcome_id, new.plan_digest
                using errcode = 'check_violation';
        end if;
    end if;
    if new.design_digest is not null then
        select * into design from science_objects where object_digest = new.design_digest;
        if found and (design.kind <> 'DESIGN'
                      or design.parent_digest is distinct from new.contract_digest) then
            raise exception
                'outcome % names a contract its design % does not realise',
                new.outcome_id, new.design_digest
                using errcode = 'check_violation';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists science_outcomes_insert_trg on science_outcomes;
create trigger science_outcomes_insert_trg
    before insert on science_outcomes
    for each row execute function science_outcomes_insert();

drop trigger if exists science_outcomes_immutable_trg on science_outcomes;
create trigger science_outcomes_immutable_trg
    before update or delete on science_outcomes
    for each row execute function trusted_provenance_immutable();

comment on table science_objects is
    'The frozen scientific contract, experimental design and execution plan, '
    'content-addressed; ordering and ancestry enforced; immutable '
    '(docs/SCIENCE_EXECUTION.md).';
comment on table science_outcomes is
    'Every system-computed outcome, bound to its frozen chain, receipt and '
    'validated result. No model decides one. Immutable.';
