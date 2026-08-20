"""#012 — transactions.is_transfer (transfer-neutral fix, increment T1).

Motivation: sync sets a transaction's `direction` from the SIGN of the
SimpleFIN amount, which breaks on liability accounts. A credit-card
"Payment Thank You" posts as a POSITIVE amount, so it lands as direction='in'
and the app reads it as income — when it's really a transfer between the
household's own accounts, and its funding leg (the checking-side debit) is
likewise not spend. A transfer is NEITHER income NOR spend and is not part of
who-owes-whom.

`is_transfer` is a direction-agnostic flag so BOTH legs can be marked (an
inflow "Payment Thank You" and its matching outflow debit). One predicate,
`is_transfer = 0`, is added to the money derivations (spending_summary,
income_summary, compute_balance, member_breakdown) in this same increment so a
flagged row drops out of spend, income, and the balance everywhere.

Schema only, and gated ZERO-DIFF by enumeration: the column defaults to 0, so
NO existing row is a transfer yet — every derivation returns byte-identical
numbers, and adding a column changes no row count. The flag only exists here;
the verb that sets it (set_transfer) and the "Mark as transfer" UI are
increment T2. `txn_to_json` is a curated dict (not SELECT *), so the new
column can't leak into the byte-frozen /api/transactions shape.

Guarded-idempotent like every migration (hard rule 1): SQLite has no
ADD COLUMN IF NOT EXISTS, so the add is gated by a PRAGMA table_info check —
re-running apply() is a clean no-op. Mirrors #005/#009/#011's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "transactions")
    if "is_transfer" not in have:
        # 1 = a transfer between the household's own accounts (neither income
        # nor spend, not part of the balance); 0 = a real inflow/outflow, the
        # meaning every existing row keeps. NOT NULL DEFAULT 0 so pre-#012 rows
        # are unambiguously non-transfers.
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN is_transfer INTEGER NOT NULL DEFAULT 0")
