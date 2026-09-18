-- What a cycle concluded should happen next, on the run's own row.
--
-- What a cycle concluded should happen next had no durable home. It was
-- returned in `CycleResult`, which is memory, and written into the payload of
-- the `RESEARCH_CYCLE_FINISHED` event, which is an operational message. The
-- daemon then copied it again into the payload of the `continue_objective` work
-- item, and `should_continue` read *that* -- two copies away from the run that
-- reached the conclusion, in a row that outlives the process, the build and
-- any later correction.
--
-- The consequence, found in the live thesis runtime rather than by reading the
-- code. `RRUN-20260918T054218Z-cb4962f4` asked the frontier role whether
-- another cycle was warranted and was told `WAIT_HUMAN`, with the reason that
-- the questions were already in front of the researcher. The build of the day
-- reached `WAIT_HUMAN` only through `requires_human_promotion`, so the cycle
-- concluded `START_NEXT_CYCLE` and that word -- not the frontier's -- is what
-- went into the event, the work item, and the successor the daemon opened. The
-- `conclude` node now honours the frontier's own recommendation, and that fix
-- does nothing for the frozen payload: the work item is still on the queue,
-- still says `START_NEXT_CYCLE`, and would still be believed on the next
-- restart.
--
-- A recommendation that lives only in a message cannot be reconciled, because
-- there is nothing to reconcile it against. So the run row records what the run
-- concluded, in the same statement that records that it concluded, and
-- `continue_objective` reads the run rather than the message it arrived in. An
-- event becomes what it should always have been: notification that a run
-- finished, not the authority on what the run decided.
--
-- Nullable, and that is the honest shape. A run finished by an earlier build
-- recorded nothing here, and `''` would assert that it recommended nothing
-- when what is true is that we do not know. The handler falls back to the
-- payload for exactly those rows, says in its result that it did, and every
-- run concluded from here on has an answer that does not depend on a message
-- surviving intact.

alter table research_runs
    add column if not exists next_recommendation text;

comment on column research_runs.next_recommendation is
    'What this cycle concluded should happen next, recorded with its terminal '
    'state. Authoritative over any event or work-item payload carrying a copy. '
    'Null means a build that predates this column finished the run.';
