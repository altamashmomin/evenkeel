-- #018 — receipts + receipt_lines (INVENTORY-DESIGN, receipt-scan amendment,
-- Nov 3, 2026). Schema only: two new empty tables, no existing row changes.
--
-- Why: the bank feed sees the MERCHANT, never the products (the
-- merchant-not-product limit that hobbles restock matching). A store receipt
-- names every product. A person photographs it, the app reads it, the person
-- confirms each line on a review sheet, and only then does record_receipt
-- write: the confirmed lines land here, the matched pantry items restock, and
-- new staples are added with the receipt's store.
--
-- What is stored is what the receipt SAYS plus what a person CONFIRMED — input
-- facts, never derivations (invariant 4). The photo itself is never stored.
-- Each confirmed line doubles as the mapping memory: next time the same
-- printed text shows up at the same store, the review sheet suggests the same
-- item. That suggestion is computed on read from these rows (no alias table,
-- no stored normalisation).
--
-- Money posture: a receipt is NOT a transaction. total_cents / price_cents are
-- what the paper says; no money derivation reads these tables, record_receipt
-- writes no transaction, split, or link, so the who-owes-whom balance and every
-- monthly total are byte-identical. txn_id only POINTS at the bank charge the
-- person confirmed it matches (ON DELETE SET NULL, mirroring
-- bill_payments.txn_id), it never creates or edits one.
--
-- Gate: schema_version 17 -> 18 is the only diff (two new tables start empty;
-- see notes/018-gate-expectation.seed.json).
CREATE TABLE IF NOT EXISTS receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store         TEXT NOT NULL,          -- as confirmed on the review sheet ("ShopRite")
    purchased_on  TEXT,                   -- 'YYYY-MM-DD' printed on the receipt; NULL = unreadable
    total_cents   INTEGER,                -- printed total; NULL = unreadable. Never summed into money.
    txn_id        INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                                          -- the bank charge it was confirmed to match; NULL = none
    created_by    TEXT NOT NULL,          -- actor ('ui:<member>')
    created_at    TEXT NOT NULL,          -- ISO-8601
    active        INTEGER NOT NULL DEFAULT 1  -- soft delete, like items/bills
);

CREATE TABLE IF NOT EXISTS receipt_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id    INTEGER NOT NULL REFERENCES receipts(id),
    position      INTEGER NOT NULL,       -- 0-based order on the paper
    raw_text      TEXT NOT NULL,          -- exactly as printed ("GV 2% MLK GAL")
    quantity      INTEGER,                -- printed count; NULL = not shown. The pantry stays quantity-free.
    price_cents   INTEGER,                -- printed line amount (negative for a discount); NULL = unreadable
    item_id       INTEGER                 -- the pantry item it was confirmed as; NULL = skipped.
                                          -- Deliberately NOT a REFERENCES: items has been rebuilt
                                          -- once (#015) and an FK here would pin that table.
);
