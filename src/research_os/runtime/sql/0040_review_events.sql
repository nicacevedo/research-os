-- One review row per reviewer call, append-only
-- (docs/ARCHITECTURE_INVARIANTS.md, INV-04 and INV-08).
--
-- `idea_reviews` was unique on (idea, version, role, content digest,
-- evidence digest), and `record_review` inserted `on conflict do nothing`
-- and handed back whatever row was already there. Two defects followed, both
-- reproduced by the final adversarial review of 37e8afe:
--
--   * H4 -- a second meta-review asked on an unchanged record had its row
--     silently dropped, and its recommendation applied anyway: the idea was
--     VALIDATED beside the only META review on file, which had declined it;
--   * M3 -- a re-run reviewer's objections were attached to the *first*
--     call's review, so which call raised them was no longer recorded.
--
-- A review is now an event of the call that produced it. The identity index
-- becomes an ordinary one; each call gets exactly one row (a replay of the
-- same call finds it again); a different call on the same binding is a
-- different row with its own attempt number, the review it supersedes, the
-- action that bought it and a digest of the response it recorded.
drop index if exists idea_reviews_identity_idx;
create index if not exists idea_reviews_binding_idx
    on idea_reviews(idea_id, idea_version, reviewer_role,
                    reviewed_content_digest, reviewed_evidence_digest);
create unique index if not exists idea_reviews_call_idx
    on idea_reviews(call_id) where call_id is not null;

alter table idea_reviews
    add column if not exists action_id text
        references idea_actions(action_id) on delete set null,
    add column if not exists attempt integer not null default 1,
    add column if not exists supersedes_review_id text
        references idea_reviews(review_id) on delete set null,
    add column if not exists response_digest text;

alter table idea_reviews drop constraint if exists idea_reviews_attempt_ck;
alter table idea_reviews add constraint idea_reviews_attempt_ck check (attempt >= 1);
