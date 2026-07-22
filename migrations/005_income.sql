-- #005 — income classification foundation (CORE-DESIGN sequence step 6,
-- INCOME-DESIGN schema section). Schema only: no verb reads or writes
-- these columns yet, so every existing row keeps its meaning exactly —
-- direction defaults to 'out' (a spend, what every row has been until
-- now), income_type stays NULL. Nothing about balance or spending
-- derivations changes shape; this migration must gate zero-diff.
--
-- set_paid_by references members(id), not the retired users(id) —
-- CORE-DESIGN's amendment to INCOME-DESIGN (people-references are member
-- ids). hit_count is observability only: which rules are actually
-- earning their keep, never read by classification logic itself.
ALTER TABLE transactions ADD COLUMN direction TEXT NOT NULL DEFAULT 'out';
       -- 'out' (spend) | 'in' (inflow)
ALTER TABLE transactions ADD COLUMN income_type TEXT;
       -- NULL for direction='out'. For 'in':
       -- 'paycheck' | 'reimbursement' | 'refund' | 'transfer'
       -- | 'gift' | 'other' | 'unclassified'

CREATE TABLE IF NOT EXISTS income_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    priority      INTEGER NOT NULL DEFAULT 0,   -- lower runs first
    match_desc    TEXT,          -- substring match on description, case-insensitive
    match_account TEXT,          -- SimpleFIN account id, or NULL = any
    min_cents     INTEGER,       -- inclusive bounds, either may be NULL
    max_cents     INTEGER,
    set_type      TEXT NOT NULL, -- income_type to assign
    set_paid_by   INTEGER REFERENCES members(id),  -- owner override, or NULL
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0    -- observability: is this rule alive?
);
