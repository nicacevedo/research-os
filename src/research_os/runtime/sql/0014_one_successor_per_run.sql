-- One parent run, at most one successor. Enforced by the database.
--
-- `RuntimeStore.lock_run` was written to serialise this, and its docstring
-- described the race it was closing: two passes over the same parked objective
-- each see no successor and each open one. An adversarial review read the
-- implementation and found the lock does not close it. `pg_advisory_xact_lock`
-- is transaction-scoped, `lock_run` takes it inside its own `tx()`, and that
-- transaction commits before the function returns -- so the lock is already
-- released when `has_successor` runs, and long gone by the time `start_cycle`
-- inserts the row. The mutual exclusion was between the lock and nothing.
--
-- The lock could have been widened to span all three steps, but `start_cycle`
-- is a whole bounded cycle: it runs a LangGraph graph, makes model calls, and
-- takes minutes. Holding a database transaction open across it to protect one
-- `insert` is the wrong shape, and a lock held for minutes is a lock that
-- times out or gets abandoned.
--
-- So the invariant goes where invariants belong. A partial unique index says
-- "at most one row may name this parent", which is exactly the property
-- `has_successor` was checking for. The check stays as a cheap way to avoid
-- doing the work, and it is no longer what makes the outcome correct: the
-- second inserter now loses at the `insert`, whenever it arrives, with no
-- window at all.
--
-- Partial, because `parent_run_id is null` for every run a researcher starts
-- and those must not be unique with respect to each other.
--
-- ## Upgrading a database the race already happened in
--
-- The race was real, so a deployment that ran the previous build may already
-- hold two runs naming one parent -- and then this index cannot be built. Left
-- alone, the operator sees `could not create unique index
-- "research_runs_one_successor_idx"` with a DETAIL naming one duplicated key,
-- on a forward-only checksum-verified migration they cannot edit, and no
-- instruction about what to do.
--
-- So the duplicates are found first and reported as an error that says which
-- runs they are. Deleting one is a decision about a research run and its
-- checkpoints, which is the operator's to make and not a migration's; what this
-- can do is make the decision an informed one.
-- The lock `create unique index` needs anyway, taken *before* the precheck.
--
-- Without it the two statements are a check-then-act with a window between
-- them, in READ COMMITTED, and a final adversarial review executed the race: a
-- successor committed by the daemon between the precheck and the build is
-- invisible to the first and fatal to the second, so the operator gets
-- verbatim the raw `could not create unique index ... Key (parent_run_id)=(...)
-- is duplicated` that the message below exists to replace. The window is real,
-- because `migrate()` runs at every daemon startup and the daemon opens
-- successors on its own schedule.
--
-- SHARE mode is exactly what the index build takes: it blocks writers, permits
-- readers, and holding it from here makes the precheck's answer still true when
-- the index is built.
lock table research_runs in share mode;

do $$
declare
    offenders text;
begin
    select string_agg(
               format('%s -> %s', parent_run_id, children), E'\n  '
               order by parent_run_id
           )
      into offenders
      from (
          select parent_run_id,
                 string_agg(run_id, ', ' order by created_at, run_id) as children
            from research_runs
           where parent_run_id is not null
           group by parent_run_id
          having count(*) > 1
      ) as duplicated;
    if offenders is not null then
        raise exception
            E'this database holds runs with more than one successor, which the '
            'index this migration creates forbids:\n  %\n'
            'That state was reachable in earlier builds -- `lock_run` released '
            'its advisory lock before the check it was meant to serialise -- so '
            'finding it here is expected on an upgrade. Decide which successor '
            'to keep (`researchctl runtime run <id>` shows each one), delete the '
            'other and its checkpoints, and migrate again. A migration will not '
            'choose between two research runs for you.',
            offenders;
    end if;
end $$;

create unique index if not exists research_runs_one_successor_idx
    on research_runs(parent_run_id) where parent_run_id is not null;

-- And prove it is the index this migration means.
--
-- `if not exists` matches on *name*, so a pre-existing index called
-- `research_runs_one_successor_idx` with any other definition -- non-unique,
-- on a different column, unpartialled -- makes the statement above report
-- success while the first line of this file ("Enforced by the database") is
-- untrue, with nothing detecting it. A final adversarial review pointed that
-- out. Asserting it here costs one catalogue read at migration time.
do $$
declare
    definition text;
begin
    select indexdef into definition
      from pg_indexes
     where schemaname = current_schema() and indexname = 'research_runs_one_successor_idx';
    if definition is null then
        raise exception 'research_runs_one_successor_idx was not created';
    end if;
    if definition not like '%UNIQUE%'
       or definition not like '%parent_run_id%'
       or definition not like '%WHERE%' then
        raise exception
            E'an index named research_runs_one_successor_idx already existed '
            'with a different definition, so "one successor per run" is NOT '
            'enforced:\n  %\n'
            'Drop it and migrate again.',
            definition;
    end if;
end $$;
