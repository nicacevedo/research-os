-- Consuming an event and queueing its work were two transactions, so a crash
-- between them lost the work permanently.
--
-- The original code's comment claimed "the next producer of the same event
-- enqueues the same dedup key rather than a duplicate". There is no next
-- producer: RESEARCH_RUN_REQUESTED is emitted once when a run is requested,
-- SCIENTIFIC_DECISION_RECORDED once when a person answers, and
-- EXTERNAL_JOB_FINISHED once per job -- after which the job is terminal and
-- never polled again. A crash in that window meant a run that never started or
-- a human decision that never resumed its cycle, recoverable only by hand.
--
-- Events are now *leased* the way work items are: claiming sets `claimed_at`
-- and `claimed_by`, and `consumed_at` is stamped only once the work row exists.
-- An expired claim makes the event claimable again.

alter table events add column if not exists claimed_at timestamptz;
alter table events add column if not exists claimed_by text;

drop index if exists events_unconsumed_idx;
create index if not exists events_claimable_idx
    on events (created_at, event_id)
    where consumed_at is null;
