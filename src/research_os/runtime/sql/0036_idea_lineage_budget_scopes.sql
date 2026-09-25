-- An idea's and a lineage's spend ceilings, as budgets the ledger reserves.
--
-- Until this release the discovery portfolio's idea and lineage ceilings
-- were enforced by the allocator alone, against `idea_actions.cost_usd`
-- summed *after* each stage finished. Three things followed, and a hostile
-- review reproduced the first two:
--
--   * a lineage one cent under its ceiling could be sold several stages in
--     one tick, each judged against the same pre-tick sum;
--   * a stage in flight was invisible to that sum, so concurrent stages of
--     one lineage were each told the whole remainder was theirs;
--   * a failure the provider billed was charged to the project ledger and
--     never reached `idea_actions`, so the idea and lineage sums missed it.
--
-- The ledger already solves all three for projects and runs: capacity is
-- taken before the spend in one statement whose `where` clause is the check,
-- and settled at the reported cost afterwards. So an idea and a lineage
-- become two more scopes of the same ledger, reserved per call alongside
-- run, project and system. Nothing else about the ledger changes.
--
-- Existing rows are untouched; this only widens what a new row may name.
alter table budgets drop constraint if exists budgets_scope_ck;
alter table budgets add constraint budgets_scope_ck
    check (scope in ('system','project','run','work_item','idea','lineage'));
