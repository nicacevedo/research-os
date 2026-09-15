-- `artifact_links` could not record the link it exists to record.
--
-- The original composite primary key was (artifact_id, run_id, work_id, role),
-- and a PRIMARY KEY implies NOT NULL on every column in it. But both of those
-- columns are legitimately absent: an artifact produced by a run as a whole has
-- no work item, and one recorded outside any run has neither. So the only link
-- the table accepted was the fully-specified one, and the ordinary case --
-- "this review packet belongs to this run" -- was rejected outright.
--
-- Fixed with a unique index over coalesced expressions instead. `on conflict do
-- nothing` with no conflict target still applies, because it matches any unique
-- violation rather than one named constraint.
--
-- This is a second migration rather than an edit to 0001 because an applied
-- migration must never be edited: `migrations.py` stores each file's checksum
-- and refuses a mismatch, precisely so that two machines cannot silently hold
-- different schemas under the same version number.

alter table artifact_links drop constraint if exists artifact_links_pkey;

alter table artifact_links alter column run_id drop not null;
alter table artifact_links alter column work_id drop not null;

create unique index if not exists artifact_links_identity_idx
    on artifact_links (
        artifact_id,
        coalesce(run_id, ''),
        coalesce(work_id, ''),
        role
    );
