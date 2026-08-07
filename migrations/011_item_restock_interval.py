"""#011 — items.restock_interval_days + items.last_stocked_at (INVENTORY-DESIGN
step 5, user-set restock cadence). Schema only: two nullable columns on the
pantry's items table.

Motivation: restock_forecast has only ever INFERRED a cadence from the bank
feed (the median gap between matched purchases, >= 3 needed) — invisible and
sparse (SimpleFIN gives the merchant, not the product). These columns let a
person SET the cadence directly instead:

  - restock_interval_days — an optional "remind me every N days"; NULL means
    "no manual cadence, fall back to the inferred median" (the prior behavior).
  - last_stocked_at — the anchor the manual cadence counts from: the last time
    the item was marked 'stocked' (add_item seeds it for a stocked add;
    set_item_status writes it on every stock event). NULL for pre-#011 rows and
    for staples never marked stocked; the derivation falls back to updated_at,
    then to the last matching purchase.

No verb reads or writes these yet (set_item_interval + the restock_forecast
manual branch land in the next increment), so every existing item keeps its
exact meaning: both columns default to NULL and restock_forecast behaves
byte-identically until a manual interval is set. Inventory never touches money,
so this gates with only the schema_version bump (adding columns changes no row
count; items already exists).

Guarded-idempotent like every other migration (hard rule 1): SQLite has no
ADD COLUMN IF NOT EXISTS, so each add is gated by a PRAGMA table_info check —
re-running apply() is a clean no-op. Mirrors #005/#009's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "items")
    if "restock_interval_days" not in have:
        # Optional user-set restock cadence in days; NULL = infer from purchase
        # history (the prior median-gap behavior).
        conn.execute("ALTER TABLE items ADD COLUMN restock_interval_days INTEGER")
    if "last_stocked_at" not in have:
        # ISO-8601 timestamp of the last time this item was marked 'stocked' —
        # the anchor a manual cadence counts from. NULL until the next stock
        # event; the derivation falls back to updated_at.
        conn.execute("ALTER TABLE items ADD COLUMN last_stocked_at TEXT")
