"""#021 — bank_accounts.joint (a bank account the two of you share).

Motivation: #020 lets each synced account belong to one person, so its charges
are credited to them. A joint account belongs to both: its money is already
both of yours, so a charge from it is household spending that moves nobody's
who-owes-whom balance. One column says so:

  joint  INTEGER NOT NULL DEFAULT 0   1 = shared by the household (member_id
                                      stays NULL), 0 = one person's or not set

How a joint charge is recorded uses the existing split model, no new math: the
sync imports it as shared with the payer's share at 100% (paid_by is the
sync's SYNC_PAID_BY default), so compute_balance finds no non-payer share and
nobody owes anybody. "Joint" in the UI is derived from that shape plus the
account (nothing derived is stored).

Schema only for the gate, gated by enumeration: no new table, no new rows, the
column defaults to 0, and no derivation reads it until someone marks an account
joint, so the balance and every monthly total are byte-identical; the sole gate
diff is schema_version 20 -> 21 (notes/021-gate-expectation.seed.json).

Guarded-idempotent like every migration: the add is gated by a PRAGMA
table_info check, so re-running apply() is a clean no-op.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    if "joint" not in columns(conn, "bank_accounts"):
        conn.execute(
            "ALTER TABLE bank_accounts ADD COLUMN joint INTEGER NOT NULL DEFAULT 0 "
            "CHECK (joint IN (0, 1))")
