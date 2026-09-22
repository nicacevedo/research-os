-- When a person last said "whatever was blocking these, look again".
--
-- `Bounds.max_stage_failures` stops a portfolio choosing a stage that will
-- never succeed: three terminal failures of one `(idea, stage, version)` and
-- the idea goes `BLOCKED_EXTERNAL`, which means "a person has to fix
-- something". That bound is right and it is why `tick._clear_blocks`
-- deliberately guesses at nothing.
--
-- What had no answer is the other half: *after* the person fixes it. The
-- count is over failed work items and never decays, so
-- `researchctl portfolio resume` -- the one signal in the system that means a
-- missing capability has arrived -- returned the ideas to IDLE and the very
-- next tick read the same historical count and blocked them again. Observed
-- on 2026-09-22: three empirical ideas whose evidence stage had failed while
-- the experiment route did not exist could not be retried after it did.
--
-- This is the shape `failed_stage_counts` already records paying for once,
-- one level up: "after the defect that caused the failures was fixed, the
-- portfolio still could not recover, because the keys were gone."
--
-- **The count itself must not be reset**, and that is the whole reason this
-- is a watermark rather than a delete. `work_items.dedup_key` embeds the
-- failure count so that a retried stage gets a *fresh* key against a
-- permanently unique index; resetting the count would re-use a spent key and
-- `enqueue`'s `on conflict do nothing` would refuse it silently -- which is
-- precisely the wedge the count exists to prevent.
--
-- So the all-time count keeps feeding the key, and the *ceiling* counts only
-- failures since the researcher last said carry on.
alter table portfolio_state
    add column if not exists failures_forgiven_at timestamptz;

comment on column portfolio_state.failures_forgiven_at is
    'When `researchctl portfolio resume` last forgave this project''s stage '
    'failures. The dedup key still counts all of them; the stage-failure '
    'ceiling counts only those after this moment.';
