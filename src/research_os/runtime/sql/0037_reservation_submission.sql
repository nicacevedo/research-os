-- Unknown spend fails closed (docs/ARCHITECTURE_INVARIANTS.md, INV-01).
--
-- A reservation used to be HELD until something settled or released it, and
-- two paths released one whose outcome nobody knew:
--
--   * the router released a call that ran to its timeout, because the killed
--     provider reported no cost -- a CLI capped at 0.60 USD that ran for 600
--     seconds had billed up to 0.60, and the next call was authorised the
--     whole ceiling again. The final adversarial review of 37e8afe
--     reproduced five such calls under a 1.00 USD ceiling;
--   * `reconcile_stale` released every HELD row older than an hour, on the
--     grounds that the spend was unknown. Unknown is exactly the case a
--     ceiling a person typed must not be handed back on.
--
-- What distinguishes "never spent" from "spent an unknown amount" is whether
-- the external call was ever *started*, so that is now recorded, durably,
-- before it starts:
--
--   submitted_at      null  -> the caller had not yet handed the work to the
--                              provider; releasing is safe;
--                     set   -> it may have started; the outcome is unknown
--                              until the provider reports, and without a
--                              report the reservation is settled at its whole
--                              amount.
--
-- `settlement_basis` says which rule closed a reservation, so a ledger that
-- charged a ceiling because it could not know is distinguishable from one
-- that charged a reported cost.
--
-- **Existing rows are marked submitted.** Every reservation written before
-- this migration was written by code that never recorded submission, so its
-- absence proves nothing; treating a pre-existing HELD row as "never started"
-- would release exactly the rows whose outcome is unknown.
alter table budget_reservations
    add column if not exists submitted_at timestamptz,
    add column if not exists settlement_basis text;

update budget_reservations
   set submitted_at = created_at
 where submitted_at is null;

alter table budget_reservations drop constraint if exists budget_reservations_basis_ck;
alter table budget_reservations add constraint budget_reservations_basis_ck
    check (settlement_basis is null or settlement_basis in (
        'reported',
        'estimate',
        'unknown_outcome',
        'not_invoked',
        'refused',
        'stale_not_submitted',
        'stale_unknown_outcome'));

comment on column budget_reservations.submitted_at is
    'When the reserved work was handed to the external system. Null means it '
    'provably was not, and only then may a reservation be released without a '
    'reported cost.';
comment on column budget_reservations.settlement_basis is
    'Which rule closed this reservation: a reported cost, the estimate, the '
    'whole amount because the outcome was unknown, or a release because the '
    'work was never started.';
