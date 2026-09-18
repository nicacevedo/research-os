-- A finding's identity, when its content will not serve as one.
--
-- A finding is deduplicated on `(project_id, digest)`, and the digest is
-- computed over what the finding says and what it rests on. That is right
-- whenever the content *is* the observation. It is wrong for a handler whose
-- result contains a model's prose.
--
-- `assess_frontier` is that handler. Ask it the same question over the same
-- scientific state twice and it reaches the same recommendation over the same
-- ranked candidates, in prose phrased differently -- so the excerpt differs,
-- and the artifact holding the ranking hashes differently, and the finding
-- digest differs. A second citable identifier is minted for one observation.
-- The planner is shown at most `MAX_PLANNER_FINDINGS` findings, so twenty
-- repetitions of one assessment fill the window and push the project's real
-- findings out of it, with nothing about the project having changed. That is
-- operational repetition presented as scientific progress, which is the
-- failure this release exists to stop -- here one layer below where the pilot
-- first found it.
--
-- So a handler that knows which part of its result is the observation may say
-- so, and identity is computed from that string instead of from the content.
-- Recorded in a column rather than left implicit in the digest, because "why
-- are these two the same finding" is a question a researcher will ask about a
-- deduplicated row, and a 64-hex digest is not an answer to it.
--
-- **Why this restates nothing already cited.** The keyed digest is only taken
-- when `semantic_key` is non-empty, and every row this migration touches gets
-- `''`. Each keeps the digest under which it was recorded and cited, and
-- `supplied_findings_digest` still recomputes at promotion time to what it
-- recomputed before.
--
-- **Why `default ''` and not null.** A finding with no producer-stated identity
-- and a finding with an empty one are the same finding. A null would buy a
-- distinction with no meaning and cost a null check at every read.

alter table runtime_findings
    add column if not exists semantic_key text not null default '';

comment on column runtime_findings.semantic_key is
    'Producer-stated identity of this observation, when content will not do. '
    'When non-empty it, and not the summary/excerpt/references, decides '
    'whether a repeat is a new finding. Never scientific authority.';
