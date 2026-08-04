-- #008 — items (INVENTORY-DESIGN: the household pantry, MVP step 1).
-- The household's tracked staples + transient one-off shopping needs. Curated,
-- not exhaustive; three states (stocked|low|out), no quantities (counting units
-- is upkeep death). A 'staple' lives forever and its status cycles as it's used
-- and restocked; a 'oneoff' is a shopping-list need that archives itself once
-- bought. Household-scoped (no per-person column), same posture as bills/goals.
-- The shopping list is a DERIVATION over this table, never stored (invariant 6).
--
-- Additive: a fresh empty table, no existing row changes, and NOTHING here
-- touches money — the who-owes-whom balance and every monthly total are
-- byte-identical (inventory never crosses the finance path).
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT,                            -- reuse the txn category vocab; nullable
    kind        TEXT NOT NULL DEFAULT 'staple'   -- 'staple' | 'oneoff'
                CHECK (kind IN ('staple', 'oneoff')),
    status      TEXT NOT NULL DEFAULT 'stocked'  -- 'stocked' | 'low' | 'out'
                CHECK (status IN ('stocked', 'low', 'out')),
    note        TEXT,                            -- brand, store, "the big blue jug"
    created_at  TEXT NOT NULL,                   -- ISO-8601
    updated_at  TEXT NOT NULL,                   -- ISO-8601
    active      INTEGER NOT NULL DEFAULT 1       -- soft delete; history stays attributable
);
