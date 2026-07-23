-- #003 — links (CORE-DESIGN sequence step 3, invariant 5).
-- Typed relationships between transactions. Additive metadata: creating a
-- link changes no row; deleting one reverts everything. Table only — the
-- settles/bill_payment wiring arrives with verb extraction, and historic
-- backfill is deliberately refused (forward-only).
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type   TEXT NOT NULL,   -- 'refund_of' | 'transfer_pair'
                                 -- | 'reimburses' | 'settles'
                                 -- | 'bill_payment'
    from_id     INTEGER NOT NULL REFERENCES transactions(id),
    to_id       INTEGER NOT NULL REFERENCES transactions(id),
    created_by  TEXT NOT NULL,   -- actor string: ui:<member> | sync | mcp:<token-label>
    created_at  TEXT NOT NULL,   -- ISO-8601
    UNIQUE(link_type, from_id, to_id)
);
