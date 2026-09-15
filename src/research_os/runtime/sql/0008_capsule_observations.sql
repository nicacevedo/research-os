-- What the runtime last saw of each project's canonical scientific state.
--
-- The whole of §8's mechanism: human scientific authority is intentional, human
-- workflow choreography is not. After the runtime hands a researcher a proposal
-- and they promote it with `researchctl propose promote`, nothing told the
-- runtime. The objective sat parked until somebody typed a continue command --
-- which is precisely the routine human orchestration this release is meant to
-- remove.
--
-- So the runtime *observes* instead. It hashes the canonical capsule, compares
-- the hash with what it last saw, and emits one `CAPSULE_CHANGED` event when
-- they differ. Nothing in the scientific kernel is involved beyond being read:
-- the kernel does not learn about PostgreSQL, does not learn about LangGraph,
-- and does not notify anyone. Observation is the runtime's job and it is done
-- entirely from the runtime's side.
--
-- A separate table rather than columns on `projects`, for one reason: this is a
-- *compare-and-set* target written on a cadence, and `projects` is a slowly
-- changing mirror of the kernel's project identity that several code paths
-- upsert. Mixing a hot observation counter into a row that `upsert_project`
-- rewrites on every cycle start is how an observation gets clobbered and a
-- change is missed exactly once -- which is the failure mode that would be
-- hardest to see, because the *next* change would be noticed and the
-- researcher would never learn that one had been skipped.
create table if not exists capsule_observations (
    project_id      text        primary key references projects(project_id) on delete cascade,
    capsule_digest  text        not null,
    frontier_digest text        not null,
    observed_at     timestamptz not null default now(),
    changes_seen    integer     not null default 0 check (changes_seen >= 0)
);
create index if not exists capsule_observations_observed_idx
    on capsule_observations(observed_at);
