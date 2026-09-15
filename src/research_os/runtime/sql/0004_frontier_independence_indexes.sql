-- Three corrections an adversarial review and a real pilot found together.

-- 1. The frontier could not be compared between cycles, so a cycle that
--    changed nothing still recommended a successor.
--
--    Demonstrated rather than predicted: the first real pilot, against the
--    CCAO mass-appraisal project, ran SEVEN chained cycles that each recomputed
--    an identical frontier and each concluded START_NEXT_CYCLE, stopping only
--    at the configured ceiling. Fifteen model calls, 2.65 USD, and one
--    assessment's worth of information.
--
--    The reason is structural and worth stating: the runtime cannot write a
--    capsule, so it cannot retire a hypothesis, record an experiment, or move a
--    claim. Its own work therefore never changes the frontier the frontier is
--    derived from. Continuation has to notice that instead of assuming progress.
alter table research_runs add column if not exists frontier_digest text;

-- 2. Achieved independence was only persisted when the call also failed, so
--    "this was independently reviewed" was unfalsifiable after the checkpoint
--    holding it was pruned. It is the claim the whole review apparatus rests
--    on, so it belongs in the durable record.
alter table model_calls add column if not exists independence text;
alter table model_calls add column if not exists independence_note text;

-- 3. Indexes for queries that were full scans on the highest-churn tables.
--    `work_items` has no retention path, so these grow for the life of a
--    deployment.
create index if not exists work_items_failed_idx
    on work_items (updated_at desc)
    where status = 'FAILED';
create index if not exists events_project_idx
    on events (project_id, created_at desc);
drop index if exists budget_reservations_held_idx;
create index if not exists budget_reservations_held_idx
    on budget_reservations (created_at)
    where status = 'HELD';
