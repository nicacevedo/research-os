-- Who owns a stage while it runs (docs/ARCHITECTURE_INVARIANTS.md, INV-03).
--
-- `idea_actions.work_id` existed from the start and was never written:
-- `track.advance_idea` accepted the work item's id and did not pass it on, so
-- `stale_actions` -- "active actions whose work item can no longer advance
-- them" -- joined against NULL and treated every stage older than
-- `stale_action_grace_seconds` (600 s) as dead. A literature scout that took
-- eleven minutes was reclaimed as `worker_crash` while it ran, and the idea
-- was bought a second time. The final adversarial review of 37e8afe
-- reproduced it with no fault injection at all.
--
-- Elapsed time is not evidence of death. What is:
--
--   * the stage's execution holds a session advisory lock keyed by its own
--     run from before the action is opened until after it is closed, and
--     PostgreSQL releases that lock when the session ends -- so "the owner's
--     process is gone" is a fact the server establishes, not a timer; and
--   * the work item that bought the stage is still LEASED to the same owner
--     at the same attempt with an unexpired lease the daemon keeps renewing.
--
-- An action is reclaimable only when neither holds. These columns record the
-- owner so both can be checked: the run (and so the lock), the work item's
-- attempt and lease owner at the moment the action was opened, and the
-- process that opened it, for a person reading the row.
alter table idea_actions
    add column if not exists run_id      text,
    add column if not exists attempt     integer,
    add column if not exists lease_owner text,
    add column if not exists executor    text;

-- The run is recoverable for every action opened before this migration: the
-- LangGraph thread is derived from it (`portfolio.ids.track_thread_id`).
update idea_actions
   set run_id = substring(thread_id from '^idea-track:(.*)$')
 where run_id is null
   and thread_id like 'idea-track:%';

comment on column idea_actions.run_id is
    'The run whose session advisory lock the executing process holds while '
    'this action is ACTIVE. The lock, not the age of the row, says whether the '
    'owner is alive.';
comment on column idea_actions.attempt is
    'The work item attempt that opened this action. A later attempt of the '
    'same item is a different owner.';
