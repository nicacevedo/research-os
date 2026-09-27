-- Trusted execution provenance for every measurement, and a replication chain
-- no program or model can supply (docs/ARCHITECTURE_INVARIANTS.md, INV-02,
-- INV-06, INV-07, INV-08).
--
-- What the independent review of 8e92e8c reproduced:
--
--   * INTEGRATED_MODEL_RECEIPT_FORGERY -- independence rested on an
--     `execution_receipt` object the *program* wrote into its own output. A
--     model-composed plan carried an escaped `execution_receipt` key that
--     the raw-byte guard missed; the declared command parsed and echoed it,
--     ignored the seed, and the replication was recorded independent and
--     SUPPORTS;
--   * WRONG_PARENT_STALE_RECEIPT -- a copied program receipt with the right
--     seed verified, although nothing bound it to this job or parent;
--   * REPOINTED_MANIFEST -- a replication's manifest pointer was changed after
--     its evidence existed, and the gate, which read only the evidence row,
--     still passed;
--   * LEGACY_REPLICATION -- a 0036-era replication with no manifest and no
--     receipt satisfied the current replication gate after upgrade.
--
-- So:
--
--   1. `execution_receipts` -- one row per execution, written by Research OS's
--      own runner from what *it* observed: the execution it launched, the
--      argument vector and environment it delivered, the inputs it
--      materialised and re-hashed, the code identity, the declared command,
--      and the digest of every declared output hashed the moment the process
--      exited. The receipt document is content-addressed in the artifact
--      store and named here. Nothing a program prints is read into it, and
--      the row can never be changed or removed. A replication's receipt also
--      binds its frozen manifest (by artifact and digest) and its parent's
--      receipt;
--   2. `replication_assessments` -- one row per replication reading, written
--      in the same transaction as its evidence row: the two receipts, the
--      manifest, the parent's evidence and analysis, and the three separate
--      findings -- configuration independence (proved from the receipts),
--      perturbation validity (only ever *attested*, by the researcher's
--      declaration frozen in the manifest) and agreement. Immutable;
--   3. a replication's manifest pointer is frozen once a result exists, like
--      every other column its reading depends on;
--   4. **legacy fails closed.** Every *executed* replication evidence row
--      (one naming a job) written before this migration gets an assessment
--      marked `legacy`, not independent and not attested, with the reason.
--      It stays history; no gate reads it as a replication, and the gate
--      requires a non-legacy assessment besides.
create table if not exists execution_receipts (
    receipt_id           text        primary key,
    job_id               text        not null unique references external_jobs(job_id),
    experiment_id        text        not null references idea_experiments(experiment_id),
    idea_id              text        not null,
    idea_version         integer     not null,
    role                 text        not null,
    action_id            text,
    run_id               text,
    work_id              text,
    command              text        not null,
    command_digest       text        not null,
    spec_digest          text        not null,
    base_commit          text        not null,
    delivered_digest     text        not null,
    inputs_digest        text        not null,
    outputs_digest       text        not null,
    exit_code            integer,
    manifest_artifact_id text        references artifacts(artifact_id),
    manifest_digest      text,
    parent_receipt_id    text        references execution_receipts(receipt_id),
    receipt_artifact_id  text        not null references artifacts(artifact_id),
    created_at           timestamptz not null default now(),

    constraint execution_receipts_role_ck check (role in ('PRIMARY','REPLICATION')),
    -- A replication's receipt names what it was frozen to be and whose
    -- result it replicates; a primary's names neither.
    constraint execution_receipts_replication_ck check (
        (role = 'REPLICATION'
            and manifest_artifact_id is not null
            and manifest_digest is not null
            and parent_receipt_id is not null)
        or (role = 'PRIMARY'
            and manifest_artifact_id is null
            and parent_receipt_id is null))
);
create index if not exists execution_receipts_experiment_idx
    on execution_receipts(experiment_id);

create table if not exists replication_assessments (
    assessment_id               text        primary key,
    evidence_id                 text        not null unique references idea_evidence(evidence_id),
    experiment_id               text        references idea_experiments(experiment_id),
    idea_id                     text        not null,
    idea_version                integer     not null,
    legacy                      boolean     not null default false,
    receipt_id                  text        references execution_receipts(receipt_id),
    parent_receipt_id           text        references execution_receipts(receipt_id),
    parent_experiment_id        text,
    parent_evidence_id          text,
    parent_analysis_artifact_id text,
    manifest_artifact_id        text,
    manifest_digest             text,
    analysis_artifact_id        text,
    configuration_independent   boolean     not null,
    perturbation_attested       boolean     not null,
    varied                      jsonb       not null default '[]'::jsonb,
    attested                    jsonb       not null default '[]'::jsonb,
    unattested                  jsonb       not null default '[]'::jsonb,
    agrees                      boolean,
    identical_values            boolean,
    basis                       text        not null,
    created_at                  timestamptz not null default now(),

    -- A current assessment names its own reading and receipt. One that finds
    -- anything names its whole chain; a legacy one names none of it and can
    -- find nothing independent or attested.
    constraint replication_assessments_chain_ck check (
        legacy
        or (experiment_id is not null
            and receipt_id is not null
            and analysis_artifact_id is not null
            and (not (configuration_independent or perturbation_attested)
                 or (parent_receipt_id is not null
                     and receipt_id <> parent_receipt_id
                     and parent_experiment_id is not null
                     and parent_evidence_id is not null
                     and parent_analysis_artifact_id is not null
                     and manifest_artifact_id is not null
                     and manifest_digest is not null)))),
    constraint replication_assessments_legacy_ck check (
        not legacy or (not configuration_independent and not perturbation_attested)),
    -- An attested perturbation of a configuration that was not delivered is
    -- no perturbation at all.
    constraint replication_assessments_attested_ck check (
        not perturbation_attested or configuration_independent)
);
create index if not exists replication_assessments_idea_idx
    on replication_assessments(idea_id, idea_version);

-- A receipt is written once, by the runner, for an execution of the
-- experiment it names, and is never changed or removed.
create or replace function execution_receipts_insert() returns trigger
language plpgsql as $$
declare
    exp idea_experiments%rowtype;
begin
    select * into exp from idea_experiments where experiment_id = new.experiment_id;
    if not found then
        return new;  -- the foreign key reports it
    end if;
    if exp.idea_id <> new.idea_id or exp.idea_version <> new.idea_version
       or exp.role <> new.role or exp.command <> new.command
       or exp.spec_digest <> new.spec_digest then
        raise exception
            'receipt % does not describe experiment % (% v% % %)',
            new.receipt_id, exp.experiment_id, exp.idea_id, exp.idea_version,
            exp.role, exp.command
            using errcode = 'check_violation';
    end if;
    if new.parent_receipt_id is not null and not exists (
        select 1 from execution_receipts p
          join idea_experiments pe on pe.experiment_id = p.experiment_id
         where p.receipt_id = new.parent_receipt_id
           and p.role = 'PRIMARY'
           and pe.idea_id = new.idea_id
           and pe.idea_version = new.idea_version) then
        raise exception
            'receipt % names parent % , which is not a primary execution of % v%',
            new.receipt_id, new.parent_receipt_id, new.idea_id, new.idea_version
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists execution_receipts_insert_trg on execution_receipts;
create trigger execution_receipts_insert_trg
    before insert on execution_receipts
    for each row execute function execution_receipts_insert();

create or replace function trusted_provenance_immutable() returns trigger
language plpgsql as $$
begin
    raise exception '% is trusted provenance and is never % after it is written',
        tg_table_name, lower(tg_op)
        using errcode = 'check_violation';
end;
$$;

drop trigger if exists execution_receipts_immutable_trg on execution_receipts;
create trigger execution_receipts_immutable_trg
    before update or delete on execution_receipts
    for each row execute function trusted_provenance_immutable();
drop trigger if exists replication_assessments_immutable_trg on replication_assessments;
create trigger replication_assessments_immutable_trg
    before update or delete on replication_assessments
    for each row execute function trusted_provenance_immutable();

-- 3. The manifest pointer joins what an experiment preregistered and what its
-- reading depends on: frozen once a result exists. Otherwise the function is
-- the one 0031 wrote.
create or replace function idea_experiments_frozen() returns trigger
language plpgsql as $$
begin
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
    if new.execution_manifest_artifact_id is distinct from old.execution_manifest_artifact_id
       and (old.state in ('COMPLETED','INTERPRETED')
            or old.analysis_artifact_id is not null
            or old.evidence_id is not null) then
        raise exception
            'the execution manifest of experiment % is frozen once its execution '
            'has a result; a changed configuration is a new execution',
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

-- 4. Legacy replication evidence: kept, marked, and never a replication.
insert into replication_assessments
    (assessment_id, evidence_id, experiment_id, idea_id, idea_version, legacy,
     configuration_independent, perturbation_attested, basis)
select 'RASM-legacy-' || e.evidence_id,
       e.evidence_id,
       (select x.experiment_id from idea_experiments x
         where x.role = 'REPLICATION'
           and (x.evidence_id = e.evidence_id
                or (e.job_id is not null and x.job_id = e.job_id))
         order by x.created_at limit 1),
       e.idea_id,
       e.idea_version,
       true,
       false,
       false,
       'recorded before trusted execution receipts (sql/0046): no receipt the '
       'runner wrote, no frozen manifest bound to both executions, so neither '
       'configuration independence nor perturbation validity is established. '
       'Kept as history; not a replication for any current gate.'
  from idea_evidence e
 where e.kind = 'replication'
   and e.job_id is not null
on conflict (evidence_id) do nothing;

comment on table execution_receipts is
    'One row per execution, written by the Research OS runner from what it '
    'observed; never from program output. Immutable (INV-02, INV-07).';
comment on table replication_assessments is
    'One row per replication reading: configuration independence (proved), '
    'perturbation validity (attested only) and agreement, kept apart. '
    'Legacy rows fail closed.';
