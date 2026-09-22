-- A ceiling a person typed, told apart from one the runtime derived.
--
-- `cycles.ensure_budgets` raises the per-project cost ceiling to
-- `max_model_cost_usd * max_cycles_per_objective` whenever an objective cycle
-- starts, and its docstring is careful about why: an earlier version derived
-- that ceiling from *one objective's* `--max-cost-usd`, so the safest command
-- a researcher could type -- a 0.50 USD smoke run -- wrote a lifetime ceiling
-- that bricked the project. Raising, never lowering, fixed that.
--
-- What it left open is the other direction, and the overnight run of
-- 2026-09-22 walked straight into it. An operator set
--
--     researchctl runtime budget cg-sparse-regression --max-cost-usd 50.00
--
-- and, minutes later, a *recovered* objective cycle -- one nobody started,
-- rescheduled by the reconciler -- raised the same ceiling to 300.00, which
-- is `25 * 12` from the shipped configuration. Nothing said so. The command
-- whose entire purpose is to set a project's standing cost ceiling had an
-- effect that lasted until the next cycle began.
--
-- That is an authority defect rather than an accounting one.
-- `docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` §13 puts "unbounded budget
-- changes" among the acts a human performs, and a machine that can multiply
-- a human's number by six has made one.
--
-- So the two kinds of ceiling are now distinguishable. A derived ceiling is
-- still raised, never lowered, exactly as before -- the bricking defect stays
-- fixed. An *explicit* one is left alone, and an objective that needs more
-- than it stops on `BUDGET_EXHAUSTED`, which is terminal, honest, and
-- already what the system does when a person's number runs out.
--
-- Default false, so every row that exists today keeps today's behaviour: the
-- ceilings in flight were all derived, and marking them explicit
-- retroactively would freeze numbers nobody chose.
alter table budgets
    add column if not exists explicit boolean not null default false;

comment on column budgets.explicit is
    'true when a person set this limit through `researchctl runtime budget`. '
    'The runtime may raise a derived ceiling and may not raise an explicit '
    'one.';
