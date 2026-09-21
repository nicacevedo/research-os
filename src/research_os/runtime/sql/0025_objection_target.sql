-- What an objection is *about*, so that killing an idea and fixing its test
-- stop being the same decision.
--
-- The first dogfood's scientific-quality audit found this. The falsify stage
-- has exactly two outcomes -- REJECT if any objection is FATAL, CONTINUE
-- otherwise -- and at least three of the seven rejections read in full were
-- objections to the *stated falsifier*, not to the *research question*:
--
--   "the test can only confirm what correctness already requires"
--   "no control variable is included, so any outcome is uninterpretable"
--   "treats a hoped-for result from elsewhere as a load-bearing premise"
--
-- A researcher reading those fixes the test. This system rejected the
-- question, wrote the test's flaw into `retire_reason`, and recorded "we
-- ruled this out" for something that had not been ruled out. That is the
-- distinction `docs/CAPSULE.md`'s retirement-memory rule exists to preserve.
--
-- The fix is not to let the model choose its disposition -- the architecture
-- is explicit that a generator may not pick its own evidentiary bar. It is to
-- have the model report one more *fact* about its own objection, and let
-- ordinary Python route on it, exactly as the adjudication type is computed
-- from the falsifier rather than chosen by it.
--
-- CLAIM is the default, and deliberately: an objection that does not say what
-- it is about keeps today's behaviour, so the softer path is only reachable
-- when a model states plainly that the question survives its own objection.
alter table idea_objections
    add column if not exists target text not null default 'CLAIM';

alter table idea_objections
    drop constraint if exists idea_objections_target_ck;
alter table idea_objections
    add constraint idea_objections_target_ck check (target in ('CLAIM','TEST'));
