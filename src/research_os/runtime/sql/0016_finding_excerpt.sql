-- A finding must be able to carry what it found, not only that it found it.
--
-- The defect this closes, in the words of the system that hit it. A
-- `critique_hypotheses` cycle wrote an eleven-kilobyte artifact holding six
-- alternative explanations, recorded a finding for it, and linked the
-- provenance. All of that was correct. The finding's `summary` was the
-- handler's own sentence -- "6 alternative explanation(s) for 5 target(s)" --
-- and `summary` was the only text the proposal layer could read. So the
-- proposal grounded in that finding says, in its own PR-002:
--
--     Only that summary is available to this proposal; the text of the six
--     alternatives is not.
--
-- and lists `full text of FIND-20260917T183904Z-9b053d6e` under
-- `required_inputs`. The worker refused to reason from content it could not
-- see, which is the correct behaviour and not a working architecture: the
-- system had produced the evidence and then withheld it from itself.
--
-- `excerpt` is the producer's own bounded selection from what it wrote. Not a
-- second artifact, not a pointer, not a summary of the summary: a quotation,
-- chosen by the handler that built the structure, bounded by
-- `research_os.runtime.findings.MAX_EXCERPT_CHARS`.
--
-- **Why `default ''` and not null.** A finding with no excerpt and a finding
-- with an empty one are the same finding, and code that has to distinguish
-- them would acquire a null check at every read for a distinction with no
-- meaning. Existing rows become excerpt-less rather than unknown, which is
-- what they are.
--
-- **Why this does not restate existing findings.** The excerpt enters
-- `RuntimeFinding.digest` only when non-empty, so every row this migration
-- backfills with '' keeps the digest under which it was already cited. A
-- proposal resting on FIND-...-9b053d6e still resolves to the same finding
-- after this migration as before it, and `supplied_findings_digest` still
-- recomputes to what was recorded at promotion time.

alter table runtime_findings
    add column if not exists excerpt text not null default '';

comment on column runtime_findings.excerpt is
    'Producer-authored bounded quotation of what this finding found. '
    'Noncanonical untrusted model output, like summary. Never a Claim.';
