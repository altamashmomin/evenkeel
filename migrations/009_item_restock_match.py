"""#009 — items.restock_match (INVENTORY-DESIGN step 5, purchase-feed
auto-population). Schema only: one nullable column on the pantry's items
table, the OPTIONAL override phrase that ties a staple to a purchase's
description for restock detection.

No verb reads or writes it yet (the restock_suggestions derivation and the
set_item_match verb land in the next increment), so every existing item keeps
its exact meaning — restock_match defaults to NULL, and the derivation falls
back to the item's own name (the auto-guess) when it is NULL. Inventory never
touches money, so this gates with only the schema_version bump (adding a
column changes no row count; items already exists).

Guarded-idempotent like every other migration (hard rule 1): SQLite has no
ADD COLUMN IF NOT EXISTS, so the add is gated by a PRAGMA table_info check —
re-running apply() is a clean no-op. Mirrors #005's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    if "restock_match" not in columns(conn, "items"):
        # Optional substring matched (case-insensitively) against a purchase's
        # description to mean "buying this restocks this staple". NULL = fall
        # back to the item name (the auto-guess).
        conn.execute("ALTER TABLE items ADD COLUMN restock_match TEXT")
