-- Every spend-bearing call carries a provider-enforced cap no greater than
-- its reservation (docs/ARCHITECTURE_INVARIANTS.md, INV-01).
--
-- 0037 made unknown spend fail closed: a reservation whose work was handed to
-- a provider and whose cost never came back is charged in full. That is an
-- upper bound on what was billed only if the provider could not bill more
-- than the reservation. The independent review of 8e92e8c showed it could:
-- the objective cycle's calls and every delegated call reached the provider
-- with no cap at all, reserving a 0.05 or 0.50 USD *estimate*. A 1.20 USD call
-- settled against a 1.00 USD run, project and system ceiling, and two such
-- calls whose settlement was lost were charged 1.00 in every scope while 2.40
-- was billed.
--
-- So the cap the provider is handed is now part of the reservation, recorded
-- in the same statement that marks the work submitted, and the database
-- refuses to let a `model_cost_usd` reservation become submitted without one
-- no greater than its amount -- whatever the application does. Sum of caps
-- handed out <= sum of reservations <= every scope's limit.
--
-- Existing rows keep a null cap. They are all submitted already (0037 marked
-- them so) and the rule binds the *transition* to submitted, so no historical
-- row is rewritten or reinterpreted.
alter table budget_reservations
    add column if not exists provider_cap_usd numeric(18, 6);

alter table budget_reservations drop constraint if exists budget_reservations_cap_ck;
alter table budget_reservations add constraint budget_reservations_cap_ck
    check (provider_cap_usd is null
           or (provider_cap_usd > 0 and provider_cap_usd <= amount));

-- A provider that billed more than the cap it was handed is recorded at what
-- it billed, and marked.
alter table budget_reservations drop constraint if exists budget_reservations_basis_ck;
alter table budget_reservations add constraint budget_reservations_basis_ck
    check (settlement_basis is null or settlement_basis in (
        'reported',
        'reported_over_reservation',
        'estimate',
        'unknown_outcome',
        'not_invoked',
        'refused',
        'stale_not_submitted',
        'stale_unknown_outcome'));

create or replace function budget_reservations_capped_submission() returns trigger
language plpgsql as $$
declare
    dim text;
begin
    -- Only the transition into "handed to the provider" is checked, so a row
    -- that was submitted before this migration is never re-judged.
    if new.submitted_at is null then
        return new;
    end if;
    if tg_op = 'UPDATE' and old.submitted_at is not null then
        if new.provider_cap_usd is distinct from old.provider_cap_usd then
            raise exception
                'reservation % was submitted with provider cap %, and what a '
                'provider was authorised to spend is not rewritten afterwards',
                old.reservation_id, old.provider_cap_usd
                using errcode = 'check_violation';
        end if;
        return new;
    end if;
    select dimension into dim from budgets where budget_id = new.budget_id;
    if dim = 'model_cost_usd' and new.provider_cap_usd is null then
        raise exception
            'reservation % of % USD cannot be handed to a provider without a '
            'provider-enforced cap no greater than it (INV-01)',
            new.reservation_id, new.amount
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;

drop trigger if exists budget_reservations_capped_submission_trg on budget_reservations;
create trigger budget_reservations_capped_submission_trg
    before insert or update on budget_reservations
    for each row execute function budget_reservations_capped_submission();

comment on column budget_reservations.provider_cap_usd is
    'The hard spend cap handed to the provider when this model_cost_usd '
    'reservation was submitted; never more than amount (INV-01).';
