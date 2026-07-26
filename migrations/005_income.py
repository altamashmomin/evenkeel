"""#005 — income classification foundation (CORE-DESIGN sequence step 6,
INCOME-DESIGN schema section). Schema only: no verb reads or writes these
columns yet, so every existing row keeps its meaning exactly — direction
defaults to 'out' (a spend, what every row has been until now), income_type
stays NULL. Nothing about balance or spending derivations changes shape;
this migration gates zero-diff.

set_paid_by references members(id), not the retired users(id) —
CORE-DESIGN's amendment to INCOME-DESIGN (people-references are member
ids). hit_count is observability only: which rules are actually earning
their keep, never read by classification logic itself.

Idempotent like every other migration (CORE-DESIGN invariant 7 / CLAUDE.md
hard rule 1): SQLite has no ADD COLUMN IF NOT EXISTS, so each add is
guarded by a PRAGMA table_info check and income_rules uses CREATE TABLE IF
NOT EXISTS — re-running apply() is a clean no-op. This was a .sql file
until July 23, 2026; converting it to a guarded .py closed the one
migration that would have errored on re-run. Safe to change because no
deployed database has applied it (pre-step-0, Pi not yet deployed).
"""

INCOME_RULES_DDL = """
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
)"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    existing = columns(conn, "transactions")
    if "direction" not in existing:
        # 'out' (spend) | 'in' (inflow)
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN direction TEXT NOT NULL DEFAULT 'out'")
    if "income_type" not in existing:
        # NULL for direction='out'. For 'in': 'paycheck' | 'reimbursement'
        # | 'refund' | 'transfer' | 'gift' | 'other' | 'unclassified'
        conn.execute("ALTER TABLE transactions ADD COLUMN income_type TEXT")
    conn.execute(INCOME_RULES_DDL)
