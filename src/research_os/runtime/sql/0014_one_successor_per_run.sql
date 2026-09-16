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
