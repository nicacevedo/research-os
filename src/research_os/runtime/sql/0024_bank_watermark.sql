-- What the Curator last wrote, so it can tell its own commit from somebody
-- else's.
--
-- The autonomous bank's ref namespace is excluded from the coding pipeline's
-- escape fingerprint (`runtime/refs.py`), which is what lets curating and a
-- coding run happen at the same time without the second reporting an escape.
-- That exemption is a blind spot, and this is what covers it: the Curator
-- records the commit it wrote, and refuses to commit onto a tip it does not
-- recognise, naming the unexpected sha.
--
-- The digest is kept beside it because it is what makes a curation idempotent:
-- a snapshot equal to the recorded one commits nothing at all, so a tick that
-- fires with nothing changed costs a render and no Git write.
alter table portfolio_state
    add column if not exists bank_commit text;
alter table portfolio_state
    add column if not exists bank_digest text;
alter table portfolio_state
    add column if not exists bank_written_at timestamptz;
