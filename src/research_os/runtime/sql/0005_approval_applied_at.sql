-- `APPLIED` as a *status* destroyed the information the status exists to carry.
--
-- Applying a decision overwrote GRANTED or DECLINED with APPLIED, so a replayed
-- `await_decision` read a declined gate back as granted. No authority was
-- bypassed -- the second `mark_approval_applied` returned false and stopped the
-- action -- but the provenance said the opposite of what the researcher
-- decided, and provenance is the whole point of the row.
--
-- `applied_at` already carried "this was acted on", losslessly and orthogonally.
-- So the status now keeps saying what was decided, and APPLIED is removed
-- rather than left in the constraint as a value nothing writes.

update approvals set applied_at = coalesce(applied_at, decided_at, now())
where status = 'APPLIED';

update approvals set status = 'GRANTED'
where status = 'APPLIED' and (decision ->> 'granted')::boolean is not false;

update approvals set status = 'DECLINED' where status = 'APPLIED';

alter table approvals drop constraint if exists approvals_status_ck;
alter table approvals add constraint approvals_status_ck
    check (status in ('PENDING','GRANTED','DECLINED','EXPIRED'));
