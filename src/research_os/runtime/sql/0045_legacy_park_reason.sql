-- A park written before structural park reasons fails closed, visibly
-- (docs/ARCHITECTURE_INVARIANTS.md, INV-10; the independent review of
-- 8e92e8c, LEGACY_BUDGET_PARK).
--
-- 0042 made a system park structural -- `park_reason`, `park_stage`,
-- `resume_status` -- and left every older PARKED row with all three null. The
-- review upgraded a 0036 idea parked on its spend ceiling ("a person raises
-- bounds.idea_spend_ceiling_usd"), raised the ceiling, and found it neither
-- revived nor marked as something a ceiling could not revive: scientifically
-- closed, and misleading about how it recovers.
--
-- Can the old rows be made structural budget parks? Not without guessing:
--
--   * `revisit_if` is prose, and model-writable -- the discover stage writes a
--     model's `minimum_decisive_action` into it -- so no text match, however
--     exact, distinguishes the tick's sentence from a model that wrote the
--     same one;
--   * a structural budget park also needs the stage it was waiting for and the
--     status it resumes as, and no legacy column holds either. Reading them
--     back out of `retire_reason` would be parsing prose again.
--
-- So no legacy row can unambiguously be established as a budget park, and
-- every PARKED row with no structural reason is marked `legacy_unknown`: kept
-- exactly as it was (its reason and revisit text are history), never revived
-- by a ceiling or by the lineage-room rule, and shown to a person as needing
-- their decision.
alter table ideas drop constraint if exists ideas_park_reason_ck;
alter table ideas add constraint ideas_park_reason_ck check (
    park_reason is null or park_reason in (
        'idea_spend_ceiling','lineage_spend_ceiling','novelty_floor','lineage_room',
        'legacy_unknown'));

update ideas
   set park_reason = 'legacy_unknown'
 where status = 'PARKED'
   and park_reason is null;
