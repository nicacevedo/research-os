-- Why the system parked an idea, as data rather than prose
-- (docs/ARCHITECTURE_INVARIANTS.md; M4).
--
-- `tick._never_allocatable` parks an idea whose next stage cannot fit under
-- its idea or lineage spend ceiling, and wrote what would bring it back as a
-- sentence: "a person raises bounds.idea_spend_ceiling_usd". Nothing read it.
-- The final adversarial review of 37e8afe raised the ceiling exactly as the
-- record said and found the idea still retired, listed with the negative
-- results and shown to the failure-mining explorer as a rejected idea.
--
-- So a system park carries its block structurally: what parked it, the stage
-- it was waiting to run, and the status it resumes as. The tick re-checks a
-- budget park against the ceilings a person set and resumes it only when
-- nothing blocks it any more. A park for any other reason -- a synthesis
-- that parked it, a dead end -- carries no reason and no ceiling revives it.
alter table ideas
    add column if not exists park_reason text,
    add column if not exists park_stage text,
    add column if not exists resume_status text;

alter table ideas drop constraint if exists ideas_park_reason_ck;
alter table ideas add constraint ideas_park_reason_ck check (
    park_reason is null or park_reason in (
        'idea_spend_ceiling','lineage_spend_ceiling','novelty_floor','lineage_room'));

-- A block explains a park and nothing else.
alter table ideas drop constraint if exists ideas_park_shape_ck;
alter table ideas add constraint ideas_park_shape_ck check (
    status = 'PARKED' or (park_reason is null and park_stage is null
                          and resume_status is null));
