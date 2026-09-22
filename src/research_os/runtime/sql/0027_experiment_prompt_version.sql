-- An experiment designed by a prompt this build has superseded.
--
-- The portfolio already has this rule for reviews and states it plainly:
-- `idea_reviews.prompt_version` is part of liveness, because "a review
-- produced by a prompt that has since been superseded is a review of a
-- question no longer being asked". An experiment is the same object under a
-- different name -- a commitment produced by one version of one prompt -- and
-- it had no such column, so a design made by a retired prompt would be
-- resubmitted unchanged until the stage-failure ceiling stopped the idea.
--
-- That is the fourth defect of this exact shape on this branch: the
-- version-blind dedup key, the permanently-unique work item key, the
-- released worktree's surviving branch, and now this. Each one wedged
-- something forever and each one was invisible until the thing in front of it
-- was fixed.
--
-- Measured, not anticipated. The first real design over `benchmark` invented
-- a plan path because the catalogue listed no files; the catalogue now lists
-- them and the same idea designs against the real
-- `experiments/EXP-0001-plan.json`. Without this column the improved prompt
-- could not reach that idea: its experiment row still named the invented
-- path, and every retry ran it again.
alter table idea_experiments
    add column if not exists prompt_version text not null default '';

-- One *live* experiment per idea version per role, which is what the rule
-- always meant. A SUPERSEDED row is history: it records that a measurement
-- was designed and why it stopped being the one being asked for, and it must
-- not occupy the name.
drop index if exists idea_experiments_identity_idx;
create unique index if not exists idea_experiments_identity_idx
    on idea_experiments(idea_id, idea_version, role)
    where state <> 'SUPERSEDED';
