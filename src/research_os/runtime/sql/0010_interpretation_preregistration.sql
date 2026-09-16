-- Bind an interpretation to the exact preregistration it was compared against.
--
-- `spec_digest` identifies the *execution* -- argv, cwd, environment, seeds --
-- and deliberately nothing else, because it has to be stable across attempts.
-- The prespecified criteria live beside it in the preregistration record and
-- are **not** in that hash. So two design passes over the same declared command
-- can produce two preregistration artifacts sharing one `spec_digest` with
-- *different* primary endpoints, and a lookup by digest alone has to choose
-- between them.
--
-- It chose the newest. Which means: read an experiment, crash before recording
-- it, design again with a different endpoint, and the retry compares the same
-- result against the new criteria and reports
-- `criteria_were_fixed_before_results: true`. That is a post-hoc
-- primary-endpoint change with no `change_primary_endpoint` decision and no
-- trace -- the gate `docs/RUNTIME.md` calls the most important one in the
-- system, defeated by a lookup.
--
-- An adversarial review found it by executing exactly that sequence.
--
-- The criteria are now resolved once, at the moment the interpretation is
-- claimed, and the artifact that carried them is named on the row. A replay
-- reads the criteria from *that* artifact and cannot be handed a different
-- set, whatever has been designed since.
alter table experiment_interpretations
    add column if not exists preregistration_artifact_id text
        references artifacts(artifact_id) on delete set null;

create index if not exists experiment_interpretations_prereg_idx
    on experiment_interpretations(preregistration_artifact_id)
    where preregistration_artifact_id is not null;
