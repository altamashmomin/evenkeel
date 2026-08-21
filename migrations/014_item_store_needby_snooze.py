"""#014 — items.store + items.need_by + items.snoozed_until (INVENTORY-DESIGN,
Pantry v2 amendment increment 2). Schema only: three nullable TEXT columns on
the pantry's items table, each following the amendment's codified parameter
grammar — explicit nullable override; NULL means today's exact behavior:

  - store — where the item is bought ("Costco"); powers the store-grouped
    shopping list ("I'm at Trader Joe's — what do we need HERE?"). NULL =
    ungrouped, exactly the pre-#014 list.
  - need_by — an ISO 'YYYY-MM-DD' deadline, mostly for one-offs ("candles
    before Saturday"); sorts the shopping list by urgency. NULL = whenever.
  - snoozed_until — pause nudges for this item until an ISO date ("we're
    traveling, stop nagging about milk"). The derivations stay clock-free:
    rows carry the date and the VIEW decides what "snoozed right now" means
    against the client's today. NULL = live.

No verb reads or writes these yet (the set_item_store / set_item_need_by /
set_item_snooze setters and the grouped-list frontend land with this same
increment's code, but the columns default to NULL), so every existing item
keeps its exact meaning and every derivation behaves byte-identically until a
person sets a value. Inventory never touches money, so this gates with only
the schema_version bump (adding columns changes no row count; items already
exists) — see notes/014-gate-expectation.seed.json.

Guarded-idempotent like every other migration (hard rule 1): SQLite has no
ADD COLUMN IF NOT EXISTS, so each add is gated by a PRAGMA table_info check —
re-running apply() is a clean no-op. Mirrors #009/#011's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "items")
    if "store" not in have:
        # Where this item is bought; NULL = ungrouped (the pre-#014 list).
        conn.execute("ALTER TABLE items ADD COLUMN store TEXT")
    if "need_by" not in have:
        # Optional ISO deadline ("2026-08-22"); NULL = whenever.
        conn.execute("ALTER TABLE items ADD COLUMN need_by TEXT")
    if "snoozed_until" not in have:
        # Nudges paused until this ISO date; NULL = live. Clock-free rows —
        # the view compares against the client's today.
        conn.execute("ALTER TABLE items ADD COLUMN snoozed_until TEXT")
