-- Pruning a run must not destroy the record of what the run caused.
--
-- 0013 narrowed one relation and said so: `external_jobs.run_id` stopped
-- cascading, and its own comment named the three it left alone --
-- `tool_invocations` (the idempotency ledger, whose header says every
-- externally visible side effect is claimed there *before* it happens),
-- `model_calls` (which carries `cost_usd`: money actually spent with a
-- provider), and `artifact_links` (the only row pointing at content-addressed
-- bytes on disk). This is that separate decision.
--
-- **Why `artifact_links` is the consequential one.** The preregistration guard
-- is what establishes that the criteria a result was read against were fixed
-- before the result existed. It found the preregistration by joining
-- `artifact_links` to `research_runs` to scope by project -- so deleting a run
-- deleted the only link naming its preregistration, and the guard then refused
-- that experiment permanently. The document was still on disk, content-
-- addressed and intact, and nothing could find it. A final adversarial review
-- of the previous release executed exactly that sequence.
--
-- **0013's stated obstruction had already been removed.** Its comment said the
-- fix needed "a primary-key migration on `artifact_links` plus a backfilled
-- `project_id`", because `run_id` was "part of that table's primary key -- so
-- it is implicitly `not null`". `0002` had already dropped that primary key and
-- run `alter column run_id drop not null`, two releases earlier, for a
-- different reason. The blocker was real when it was first written and stale
-- when it was repeated; only the backfilled `project_id` was ever needed. This
-- file cannot correct 0013 in place -- `migrations.py` checksums every applied
-- file and refuses an edit, which is the point -- so the correction is here.
--
-- **The fix is a project column, not a nullable run.** These three tables had
-- no `project_id` at all; the only way to ask "whose is this" was through the
-- run. That is why 0013's answer was adequate for `external_jobs`, which has
-- carried `project_id not null` since 0001, and is not adequate here. With
-- `project_id` on the row, the lookup is scoped without the run and survives
-- the run's deletion, and deleting a *project* still erases everything --
-- which `tests/test_runtime_schema.py` asserts and which is the intended
-- erasure.
--
-- **Why `run_id` keeps its value instead of becoming null.** Two reasons, and
-- the first is a correctness one. `artifact_links_identity_idx` (0002) is
-- unique over `(artifact_id, coalesce(run_id, ''), coalesce(work_id, ''),
-- role)`. Content addressing makes two runs storing the same bytes under the
-- same role ordinary -- one prompt template reused is enough -- so nulling
-- `run_id` on delete can collide two surviving rows into one identity, and
-- PostgreSQL would then fail the delete with a unique violation. A prune that
-- cannot run is worse than a label that outlives its row.
--
-- The second reason is what the column is for. "RUN-x produced this, and
-- RUN-x's own record has been pruned" is strictly more provenance than "some
-- run, unknown", and provenance is the entire subject of this migration. The
-- cost is that `run_id` is no longer a foreign key, so a reader must left-join
-- it. Every reader in `src/` already does or does not need to.
--
-- `external_jobs` is deliberately left as 0013 set it. Reversing a decision
-- the previous release made and tested, with no new evidence, is churn; and
-- because that table has always had `project_id not null`, the argument above
-- does not apply to it.
--
-- Forward-only and re-runnable in shape, like every file here.

-- Dropping a foreign key needs ACCESS EXCLUSIVE on the child table, every
-- pooled connection is pinned with `lock_timeout=30s` (`runtime/db.py`), and
-- `migrate()` applies every pending file in one transaction -- so a single
-- daemon tick holding a row lock would roll back this whole batch. `set local`
-- reverts at the end of the transaction. An upgrade should still be done with
-- the daemon stopped: this waits rather than failing, and while it queues for
-- ACCESS EXCLUSIVE it blocks every tick behind it. `docs/OPERATIONS.md` says so.
set local lock_timeout = '0';

-- ------------------------------------------------------------ artifact_links
alter table artifact_links
    add column if not exists project_id text references projects(project_id) on delete cascade;

update artifact_links l
   set project_id = r.project_id
  from research_runs r
 where l.run_id = r.run_id
   and l.project_id is null;

create index if not exists artifact_links_project_idx
    on artifact_links(project_id, created_at desc);

alter table artifact_links drop constraint if exists artifact_links_run_id_fkey;

-- --------------------------------------------------------- tool_invocations
alter table tool_invocations
    add column if not exists project_id text references projects(project_id) on delete cascade;

update tool_invocations t
   set project_id = r.project_id
  from research_runs r
 where t.run_id = r.run_id
   and t.project_id is null;

create index if not exists tool_invocations_project_idx
    on tool_invocations(project_id, started_at desc);

alter table tool_invocations drop constraint if exists tool_invocations_run_id_fkey;

-- -------------------------------------------------------------- model_calls
alter table model_calls
    add column if not exists project_id text references projects(project_id) on delete cascade;

update model_calls m
   set project_id = r.project_id
  from research_runs r
 where m.run_id = r.run_id
   and m.project_id is null;

create index if not exists model_calls_project_idx
    on model_calls(project_id, created_at desc);

alter table model_calls drop constraint if exists model_calls_run_id_fkey;
