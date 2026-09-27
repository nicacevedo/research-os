-- A review and every objection it raised are one durable event
-- (docs/ARCHITECTURE_INVARIANTS.md, INV-04 and INV-08).
--
-- The independent review of 8e92e8c reproduced three failures of one design:
--
--   * FATAL_DEDUPED_AS_MINOR -- objections were unique on (idea, normalised
--     text, version), so a later reviewer's FATAL objection with the same
--     wording as an earlier reviewer's MINOR one was "the same objection":
--     the insert did nothing, the MINOR row and the earlier review's
--     attribution stood, and the fatal-objection gate found nothing;
--   * LOST_REVIEW_OBJECTION -- the review was committed in one transaction
--     and its objections in others, so a worker that died between them left a
--     completed PASS_WITH_OBJECTIONS review of FATAL severity with no
--     objection at all, and the role counted as done;
--   * M3, still -- a re-run's objection with the same text was attached to
--     the first call's review.
--
-- Now:
--
--   1. an objection belongs to exactly one review event, at its position in
--      that review's list (`ordinal`); nothing is deduplicated across
--      reviews. Two reviewers who use the same words raise two objections,
--      each with its own review, role, severity and provenance. The
--      normalised-text key stays, as the stickiness key a revision names to
--      answer an objection -- never as identity;
--   2. a review records how many objections it raised (`objection_count`),
--      and a deferred constraint checks, at commit, that exactly that many
--      rows belong to it and that the worst of them is the review's own
--      severity. A review committed without its objections -- in a separate
--      transaction, or after a crash between the two -- cannot commit, and
--      an objection cannot be added to a review afterwards;
--   3. what a review and its objections said is immutable. Resolution is the
--      only change an objection row may take.
--
-- **Legacy rows fail closed.** A review written before this migration has a
-- null `objection_count` unless its completeness is certain: severity NONE
-- (a review's severity is the worst of its objections, so NONE means it
-- raised none) and no objection attributed to it. Every other legacy review
-- -- one that may have lost objections to a crash or to deduplication -- is
-- kept, and is not live (`PortfolioStore.live_reviews`), so a gate never
-- counts it. Legacy objection rows keep a null ordinal and stay standing.
alter table idea_reviews add column if not exists objection_count integer;
alter table idea_reviews drop constraint if exists idea_reviews_objection_count_ck;
alter table idea_reviews add constraint idea_reviews_objection_count_ck
    check (objection_count is null or objection_count >= 0);

alter table idea_objections add column if not exists ordinal integer;
alter table idea_objections drop constraint if exists idea_objections_ordinal_ck;
alter table idea_objections add constraint idea_objections_ordinal_ck
    check (ordinal is null or ordinal >= 0);

drop index if exists idea_objections_key_idx;
create index if not exists idea_objections_key_lookup_idx
    on idea_objections(idea_id, objection_key, raised_at_version);
create unique index if not exists idea_objections_review_ordinal_idx
    on idea_objections(raised_in_review, ordinal) where ordinal is not null;
create index if not exists idea_objections_review_idx
    on idea_objections(raised_in_review);

update idea_reviews r
   set objection_count = 0
 where r.objection_count is null
   and r.severity = 'NONE'
   and not exists (
       select 1 from idea_objections o where o.raised_in_review = r.review_id);

create or replace function portfolio_severity_rank(severity text) returns integer
language sql immutable as $$
    select case severity
        when 'NONE' then 0 when 'MINOR' then 1 when 'MAJOR' then 2
        when 'CRITICAL' then 3 when 'FATAL' then 4 end
$$;

-- New events only: a review states its objection count, and an objection
-- names its place in a review that did.
create or replace function idea_review_events_insert() returns trigger
language plpgsql as $$
begin
    if tg_table_name = 'idea_reviews' then
        if new.objection_count is null then
            raise exception
                'review % must record how many objections it raised; a review '
                'and its objections are one event', new.review_id
                using errcode = 'check_violation';
        end if;
        return new;
    end if;
    if new.ordinal is null then
        raise exception
            'an objection is recorded only as part of the review event that '
            'raised it (review %)', new.raised_in_review
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists idea_reviews_event_insert_trg on idea_reviews;
create trigger idea_reviews_event_insert_trg
    before insert on idea_reviews
    for each row execute function idea_review_events_insert();
drop trigger if exists idea_objections_event_insert_trg on idea_objections;
create trigger idea_objections_event_insert_trg
    before insert on idea_objections
    for each row execute function idea_review_events_insert();

-- At commit: the event is whole.
create or replace function idea_review_event_complete() returns trigger
language plpgsql as $$
declare
    event_review text;
    wanted integer;
    stated text;
    found_n integer;
    worst integer;
begin
    if tg_table_name = 'idea_reviews' then
        event_review := new.review_id;
    elsif tg_op = 'DELETE' then
        event_review := old.raised_in_review;
    else
        event_review := new.raised_in_review;
    end if;
    select objection_count, severity into wanted, stated
      from idea_reviews where review_id = event_review;
    if not found then
        return null;  -- the review went too (a cascade); nothing is left to be whole
    end if;
    if wanted is null then
        -- A legacy review: its objections are whatever it had, and nothing
        -- may be added to it now.
        if exists (select 1 from idea_objections
                    where raised_in_review = event_review and ordinal is not null) then
            raise exception
                'review % predates atomic review events; objections cannot be '
                'added to it', event_review
                using errcode = 'check_violation';
        end if;
        return null;
    end if;
    select count(*), coalesce(max(portfolio_severity_rank(severity)), 0)
      into found_n, worst
      from idea_objections where raised_in_review = event_review;
    if found_n <> wanted or worst <> portfolio_severity_rank(stated) then
        raise exception
            'review % is one event with % objection(s) at worst %; % are '
            'recorded at worst rank %. A review is committed with all of its '
            'objections or not at all.',
            event_review, wanted, stated, found_n, worst
            using errcode = 'check_violation';
    end if;
    return null;
end;
$$;

drop trigger if exists idea_reviews_event_complete_trg on idea_reviews;
create constraint trigger idea_reviews_event_complete_trg
    after insert on idea_reviews
    deferrable initially deferred
    for each row execute function idea_review_event_complete();
drop trigger if exists idea_objections_event_complete_trg on idea_objections;
create constraint trigger idea_objections_event_complete_trg
    after insert or update or delete on idea_objections
    deferrable initially deferred
    for each row execute function idea_review_event_complete();

-- What was said is not rewritten.
create or replace function idea_review_events_immutable() returns trigger
language plpgsql as $$
begin
    if tg_table_name = 'idea_reviews' then
        if new.review_id <> old.review_id
           or new.idea_id <> old.idea_id
           or new.idea_version <> old.idea_version
           or new.reviewer_role <> old.reviewer_role
           or new.verdict <> old.verdict
           or new.severity <> old.severity
           or new.summary <> old.summary
           or new.recommendation is distinct from old.recommendation
           or new.reviewed_content_digest <> old.reviewed_content_digest
           or new.reviewed_evidence_digest <> old.reviewed_evidence_digest
           or new.prompt_version <> old.prompt_version
           or new.call_id is distinct from old.call_id
           or new.objection_count is distinct from old.objection_count then
            raise exception 'review % is a recorded event and is not rewritten',
                old.review_id using errcode = 'check_violation';
        end if;
        return new;
    end if;
    if new.objection_id <> old.objection_id
       or new.idea_id <> old.idea_id
       or new.raised_in_review <> old.raised_in_review
       or new.raised_at_version <> old.raised_at_version
       or new.objection_key <> old.objection_key
       or new.severity <> old.severity
       or new.summary <> old.summary
       or new.target is distinct from old.target
       or new.ordinal is distinct from old.ordinal then
        raise exception
            'objection % is part of the review event that raised it; only its '
            'resolution may change', old.objection_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists idea_reviews_immutable_trg on idea_reviews;
create trigger idea_reviews_immutable_trg
    before update on idea_reviews
    for each row execute function idea_review_events_immutable();
drop trigger if exists idea_objections_immutable_trg on idea_objections;
create trigger idea_objections_immutable_trg
    before update on idea_objections
    for each row execute function idea_review_events_immutable();

comment on column idea_reviews.objection_count is
    'How many objections this review event raised; null only on a legacy row '
    'whose completeness cannot be established, which is never live.';
comment on column idea_objections.ordinal is
    'This objection''s position in the review event that raised it.';
