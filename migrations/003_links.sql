-- #003 — links (CORE-DESIGN sequence step 3, invariant 5).
-- Typed relationships between transactions. Additive metadata: creating a
-- link changes no row; deleting one reverts everything. Table only — the
-- settles wiring arrives with verb extraction, and historic backfill is
-- deliberately refused (forward-only). Bill-to-payment linkage is NOT a
-- link type: the bill_payments table already is that relationship
-- (hardening disposition 3; comment amended pre-deploy, before any real
-- database had applied this migration).
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type   TEXT NOT NULL,   -- 'refund_of' | 'transfer_pair'
                                 -- | 'reimburses' | 'settles'
    from_id     INTEGER NOT NULL REFERENCES transactions(id),
    to_id       INTEGER NOT NULL REFERENCES transactions(id),
    created_by  TEXT NOT NULL,   -- actor string: ui:<member> | sync | mcp:<token-label>
    created_at  TEXT NOT NULL,   -- ISO-8601
    UNIQUE(link_type, from_id, to_id)
);
