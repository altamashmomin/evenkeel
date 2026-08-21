"""#015 — items.status gains 'ordered' (INVENTORY-DESIGN, Pantry v2 amendment
increment 7 — built Aug 21, 2026 after the Alta/Charlee call the amendment
asked for). 'ordered' = bought online, not yet arrived: off the shopping list
(it's handled) but not stocked (don't count on it yet) — the state that
prevents real double-buys.

Why a table rebuild: #008 created status with
    CHECK (status IN ('stocked', 'low', 'out'))
and SQLite cannot ALTER a CHECK, so the only honest way to widen the vocab is
the documented rebuild dance (the same one #002 used): CREATE items_new with
the widened CHECK and every column the table has accumulated (#008 base,
#009 restock_match, #011 restock_interval_days + last_stocked_at, #014 store
+ need_by + snoozed_until), copy EVERY row with its id (so audit targets
'item:<id>' and history stay attributable; the AUTOINCREMENT sequence follows
the rename), DROP the old table, RENAME. No FK references items and it has
no indexes, so nothing else moves. Row counts are identical before and after
(the copy is total), every existing status value is already in the new vocab,
and the pantry never touches money — so this gates with only the
schema_version bump (notes/015-gate-expectation.seed.json).

Idempotent (hard rule 1): applying is gated on the LIVE DDL in sqlite_master —
if items' CREATE already allows 'ordered', this is a clean no-op. The runner's
transaction wraps the whole rebuild (a failure mid-way rolls back to the old
table intact).
"""

ITEMS_DDL = """
CREATE TABLE items_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    category              TEXT,
    kind                  TEXT NOT NULL DEFAULT 'staple'
                          CHECK (kind IN ('staple', 'oneoff')),
    status                TEXT NOT NULL DEFAULT 'stocked'
                          CHECK (status IN ('stocked', 'low', 'out', 'ordered')),
    note                  TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    active                INTEGER NOT NULL DEFAULT 1,
    restock_match         TEXT,
    restock_interval_days INTEGER,
    last_stocked_at       TEXT,
    store                 TEXT,
    need_by               TEXT,
    snoozed_until         TEXT
)
"""

COLUMNS = ("id", "name", "category", "kind", "status", "note", "created_at",
           "updated_at", "active", "restock_match", "restock_interval_days",
           "last_stocked_at", "store", "need_by", "snoozed_until")


def _items_sql(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()
    return row[0] if row else None


def apply(conn):
    sql = _items_sql(conn)
    if sql is None:
        raise RuntimeError("items table missing — #008 must run before #015")
    if "'ordered'" in sql:
        return  # already applied — idempotent no-op
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    missing = [c for c in COLUMNS if c not in have]
    if missing:
        raise RuntimeError(f"items is missing {missing} — #009/#011/#014 must run before #015")
    conn.execute(ITEMS_DDL)
    cols = ", ".join(COLUMNS)
    conn.execute(f"INSERT INTO items_new ({cols}) SELECT {cols} FROM items ORDER BY id")
    conn.execute("DROP TABLE items")
    conn.execute("ALTER TABLE items_new RENAME TO items")
