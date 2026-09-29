-- Multi-execution scientific campaigns (docs/SCIENCE_EXECUTION.md §3a).
--
-- One frozen design may compile to a campaign: a PLAN whose execution units
-- are frozen with it, each run once by the trusted runner with its own
-- receipt, each result validated, then combined by the capability's declared
-- rule and read once. What this adds, and what the database itself refuses:
--
--   1. `science_campaign_units` -- the units a campaign plan froze, by index,
--      each with its own specification digest. Written with the plan,
--      immutable, and only of a PLAN.
--   2. `execution_receipts.unit_index`, `unit_attempt` -- a receipt of an
--      execution under a campaign plan names its unit and the attempt, and
--      its specification must be that unit's; a receipt under any other plan
--      names neither and keeps the old rule (the experiment's specification).
--      One receipt per unit per attempt. A replication unit's parent is the
--      primary execution of the same unit.
--   3. `campaign_unit_results` -- a unit's validated result, by content: the
--      artifact holding its bytes is addressed by their sha256.
--   4. `science_outcome_units` -- every unit receipt and result a campaign's
--      outcome was computed from. A reading of a campaign names every unit of
--      its plan, all of one attempt, at commit; a receipt is read once.

create table if not exists science_campaign_units (
    plan_digest      text    not null references science_objects(object_digest),
    unit_index       integer not null check (unit_index >= 0),
    spec_digest      text    not null,
    variation_digest text    not null,
    primary key (plan_digest, unit_index),
    constraint science_campaign_units_spec_uq unique (plan_digest, spec_digest)
);

create or replace function science_campaign_units_insert() returns trigger
language plpgsql as $$
declare
    plan science_objects%rowtype;
begin
    select * into plan from science_objects where object_digest = new.plan_digest;
    if found and plan.kind <> 'PLAN' then
        raise exception 'campaign units belong to a plan, and % is a %',
            new.plan_digest, lower(plan.kind)
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists science_campaign_units_insert_trg on science_campaign_units;
create trigger science_campaign_units_insert_trg
    before insert on science_campaign_units
    for each row execute function science_campaign_units_insert();
drop trigger if exists science_campaign_units_immutable_trg on science_campaign_units;
create trigger science_campaign_units_immutable_trg
    before update or delete on science_campaign_units
    for each row execute function trusted_provenance_immutable();

alter table execution_receipts
    add column if not exists unit_index integer check (unit_index >= 0);
alter table execution_receipts
    add column if not exists unit_attempt integer check (unit_attempt >= 0);
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'execution_receipts_unit_ck'
    ) then
        alter table execution_receipts add constraint execution_receipts_unit_ck
            check ((unit_index is null) = (unit_attempt is null));
    end if;
end;
$$;
create unique index if not exists execution_receipts_unit_idx
    on execution_receipts(experiment_id, unit_attempt, unit_index)
    where unit_index is not null;

-- 0046's receipt rule, with the campaign case: a campaign unit's receipt
-- describes that unit's frozen specification, not the campaign's.
create or replace function execution_receipts_insert() returns trigger
language plpgsql as $$
declare
    exp idea_experiments%rowtype;
    campaign boolean;
    parent execution_receipts%rowtype;
begin
    select * into exp from idea_experiments where experiment_id = new.experiment_id;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    if exp.idea_id <> new.idea_id or exp.idea_version <> new.idea_version
       or exp.role <> new.role or exp.command <> new.command then
        raise exception
            'receipt % does not describe experiment % (% v% % %)',
            new.receipt_id, exp.experiment_id, exp.idea_id, exp.idea_version,
            exp.role, exp.command
            using errcode = 'check_violation';
    end if;
    campaign := exp.plan_digest is not null and exists (
        select 1 from science_campaign_units u where u.plan_digest = exp.plan_digest);
    if campaign then
        if new.unit_index is null then
            raise exception
                'receipt % is of campaign experiment % and names no unit',
                new.receipt_id, exp.experiment_id
                using errcode = 'check_violation';
        end if;
        if not exists (
            select 1 from science_campaign_units u
             where u.plan_digest = exp.plan_digest
               and u.unit_index = new.unit_index
               and u.spec_digest = new.spec_digest) then
            raise exception
                'receipt % names unit % with specification %, which campaign % did not freeze',
                new.receipt_id, new.unit_index, new.spec_digest, exp.plan_digest
                using errcode = 'check_violation';
        end if;
    else
        if new.unit_index is not null then
            raise exception
                'receipt % names a campaign unit and experiment % runs no campaign',
                new.receipt_id, exp.experiment_id
                using errcode = 'check_violation';
        end if;
        if exp.spec_digest <> new.spec_digest then
            raise exception
                'receipt % does not describe experiment % (% v% % %)',
                new.receipt_id, exp.experiment_id, exp.idea_id, exp.idea_version,
                exp.role, exp.command
                using errcode = 'check_violation';
        end if;
    end if;
    if new.parent_receipt_id is not null then
        select p.* into parent from execution_receipts p
          join idea_experiments pe on pe.experiment_id = p.experiment_id
         where p.receipt_id = new.parent_receipt_id
           and p.role = 'PRIMARY'
           and pe.idea_id = new.idea_id
           and pe.idea_version = new.idea_version;
        if not found then
            raise exception
                'receipt % names parent % , which is not a primary execution of % v%',
                new.receipt_id, new.parent_receipt_id, new.idea_id, new.idea_version
                using errcode = 'check_violation';
        end if;
        if parent.unit_index is distinct from new.unit_index then
            raise exception
                'replication unit % of receipt % names the primary execution of unit %',
                coalesce(new.unit_index::text, '(none)'), new.receipt_id,
                coalesce(parent.unit_index::text, '(none)')
                using errcode = 'check_violation';
        end if;
    end if;
    return new;
end;
$$;

create table if not exists campaign_unit_results (
    receipt_id         text        primary key references execution_receipts(receipt_id),
    experiment_id      text        not null references idea_experiments(experiment_id),
    unit_index         integer     not null check (unit_index >= 0),
    unit_attempt       integer     not null check (unit_attempt >= 0),
    result_sha256      text        not null,
    result_artifact_id text        not null references artifacts(artifact_id),
    created_at         timestamptz not null default clock_timestamp(),
    -- The stored bytes are the result's: the artifact is addressed by them.
    constraint campaign_unit_results_bytes_ck check (result_artifact_id = result_sha256)
);

create or replace function campaign_unit_results_insert() returns trigger
language plpgsql as $$
declare
    rct execution_receipts%rowtype;
begin
    select * into rct from execution_receipts where receipt_id = new.receipt_id;
    if found and (rct.experiment_id <> new.experiment_id
                  or rct.unit_index is distinct from new.unit_index
                  or rct.unit_attempt is distinct from new.unit_attempt) then
        raise exception
            'unit result % is not of the unit its receipt names', new.receipt_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists campaign_unit_results_insert_trg on campaign_unit_results;
create trigger campaign_unit_results_insert_trg
    before insert on campaign_unit_results
    for each row execute function campaign_unit_results_insert();
drop trigger if exists campaign_unit_results_immutable_trg on campaign_unit_results;
create trigger campaign_unit_results_immutable_trg
    before update or delete on campaign_unit_results
    for each row execute function trusted_provenance_immutable();

alter table science_outcomes
    add column if not exists unit_count integer check (unit_count >= 1);

create table if not exists science_outcome_units (
    outcome_id    text    not null references science_outcomes(outcome_id),
    unit_index    integer not null check (unit_index >= 0),
    receipt_id    text    not null references execution_receipts(receipt_id),
    result_sha256 text,
    primary key (outcome_id, unit_index),
    -- A unit's execution is read once.
    constraint science_outcome_units_receipt_uq unique (receipt_id)
);

create or replace function science_outcome_units_insert() returns trigger
language plpgsql as $$
declare
    oc science_outcomes%rowtype;
    rct execution_receipts%rowtype;
    anchor execution_receipts%rowtype;
begin
    select * into oc from science_outcomes where outcome_id = new.outcome_id;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    select * into rct from execution_receipts where receipt_id = new.receipt_id;
    if not found then
        return new;
    end if;
    if rct.experiment_id is distinct from oc.experiment_id
       or rct.plan_digest is distinct from oc.plan_digest
       or rct.unit_index is distinct from new.unit_index then
        raise exception
            'outcome % names receipt % as unit %, and it is not that unit of its experiment and plan',
            new.outcome_id, new.receipt_id, new.unit_index
            using errcode = 'check_violation';
    end if;
    if oc.receipt_id is not null then
        select * into anchor from execution_receipts where receipt_id = oc.receipt_id;
        if found and anchor.unit_attempt is distinct from rct.unit_attempt then
            raise exception
                'outcome % combines units of different attempts', new.outcome_id
                using errcode = 'check_violation';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists science_outcome_units_insert_trg on science_outcome_units;
create trigger science_outcome_units_insert_trg
    before insert on science_outcome_units
    for each row execute function science_outcome_units_insert();
drop trigger if exists science_outcome_units_immutable_trg on science_outcome_units;
create trigger science_outcome_units_immutable_trg
    before update or delete on science_outcome_units
    for each row execute function trusted_provenance_immutable();

-- At commit: a reading of a campaign names every unit of its plan, including
-- the receipt it is anchored on; an outcome of anything else names no unit.
create or replace function science_outcomes_units_complete() returns trigger
language plpgsql as $$
declare
    frozen integer;
    named integer;
begin
    select count(*) into frozen from science_campaign_units
     where plan_digest = new.plan_digest;
    select count(*) into named from science_outcome_units
     where outcome_id = new.outcome_id;
    if frozen = 0 then
        if named <> 0 or new.unit_count is not null then
            raise exception
                'outcome % names campaign units and its plan is no campaign',
                new.outcome_id
                using errcode = 'check_violation';
        end if;
        return null;
    end if;
    if new.state in ('SUPPORTED','REFUTED','INCONCLUSIVE') then
        if new.unit_count is distinct from frozen or named <> frozen
           or not exists (select 1 from science_outcome_units
                           where outcome_id = new.outcome_id
                             and receipt_id = new.receipt_id) then
            raise exception
                'reading % of a % unit campaign names % unit(s); a campaign is read '
                'over every unit or not at all',
                new.outcome_id, frozen, named
                using errcode = 'check_violation';
        end if;
    elsif named > frozen then
        raise exception 'outcome % names more units than its campaign froze',
            new.outcome_id
            using errcode = 'check_violation';
    end if;
    return null;
end;
$$;

drop trigger if exists science_outcomes_units_complete_trg on science_outcomes;
create constraint trigger science_outcomes_units_complete_trg
    after insert on science_outcomes
    deferrable initially deferred
    for each row execute function science_outcomes_units_complete();

-- 5. What an idea says a settling measurement would need (planning metadata,
--    `research_os.portfolio.feasibility`). A model's typed claim, matched by
--    code against the committed capability declarations to order scarce
--    advancement work; never evidence, never part of an idea's content. One
--    per idea version, written by the stage that sharpened it.
create table if not exists idea_evidence_needs (
    idea_id        text        not null references ideas(idea_id) on delete cascade,
    idea_version   integer     not null check (idea_version >= 1),
    needs          jsonb       not null,
    origin_call_id text,
    created_at     timestamptz not null default now(),
    primary key (idea_id, idea_version)
);

comment on table science_campaign_units is
    'The execution units a campaign plan froze, each with its own specification '
    '(docs/SCIENCE_EXECUTION.md §3a). Immutable.';
comment on table campaign_unit_results is
    'One validated campaign unit result, stored by content. Immutable.';
comment on table science_outcome_units is
    'Every unit receipt and result a campaign outcome was computed from. '
    'A reading names every unit of its plan. Immutable.';
