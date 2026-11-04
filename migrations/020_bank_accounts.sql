-- #020 — bank_accounts (who owns each synced bank account, Nov 4, 2026).
-- Schema only: one new, empty table; no existing row changes.
--
-- Why: the SimpleFIN sync stamped EVERY imported charge with one person
-- (SYNC_PAID_BY), whatever account it came from. With both people's banks
-- linked, Charlee's card charges landed as "Altamash paid, shared 50/50", so
-- the balance said Charlee owed Alta half of her own spending. The account a
-- row came from already lives in its external_id ('simplefin:<account>:<txn>'),
-- but nothing said whose account that is. This table is that one input fact.
--
-- One row per SimpleFIN account id. `name` is the last name SimpleFIN reported
-- (written by the sync through note_bank_account). `member_id` is whose
-- account it is; NULL = not set yet, and the sync keeps its old default
-- (SYNC_PAID_BY) for it. Rows are written only by the note_bank_account and
-- set_account_owner verbs (invariant 2).
--
-- Money posture: no money derivation reads this table. It changes who the sync
-- credits for charges it imports AFTER an owner is set; it never rewrites an
-- existing transaction (fixing past rows is fix_payers.py, dry run by
-- default, through edit_transaction). So the who-owes-whom balance and every
-- monthly total are byte-identical. Gate: schema_version 19 -> 20 and the new
-- empty table are the only diff (notes/020-gate-expectation.seed.json).
CREATE TABLE IF NOT EXISTS bank_accounts (
    account_id  TEXT PRIMARY KEY,     -- SimpleFIN account id (middle of external_id)
    name        TEXT,                 -- last name SimpleFIN reported; NULL until seen
    member_id   INTEGER REFERENCES members(id),  -- whose account; NULL = not set
    created_at  TEXT NOT NULL,        -- ISO-8601
    updated_at  TEXT NOT NULL         -- ISO-8601
);
