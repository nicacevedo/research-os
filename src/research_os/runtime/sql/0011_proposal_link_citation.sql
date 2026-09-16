-- Distinguish the findings a proposal *cited* from the findings it was *offered*.
--
-- `0007` created `runtime_proposal_links` with the comment "Which findings one
-- runtime action's proposal actually cited ... grounded in these findings and no
-- others". The writer did not do that. It inserted every finding in the packet,
-- because that is what was handed to the proposal worker, and a worker that
-- reads eight findings and grounds its items in two is the normal case rather
-- than an anomaly. So the table said a proposal rested on six findings it never
-- used, and "what is this proposal resting on" -- the one question the relation
-- exists to answer -- got a padded answer.
--
-- Both facts are worth keeping, and they answer different questions:
--
--   cited = true   what the proposal's items are grounded in. This is the
--                  audit chain: item -> finding -> artifact -> bytes.
--   cited = false  what the worker was shown and did not use. This is the
--                  evidence that it was not fed only the findings that agreed
--                  with the change it proposed, which is a question a reviewer
--                  of the *process* asks rather than a reader of the proposal.
--
-- One row per (proposal, finding) either way, so the primary key is unchanged
-- and the migration is a column with a default. Existing rows become
-- `cited = false`: the honest value, because for those rows nothing recorded
-- which findings were cited and inferring it now would be inventing the fact
-- this column exists to stop inventing.
alter table runtime_proposal_links
    add column if not exists cited boolean not null default false;

-- The citation edges alone, which is what a reader of one proposal wants.
create index if not exists runtime_proposal_links_cited_idx
    on runtime_proposal_links(proposal_id) where cited;
