"""#017 — bills.pay_url + bills.autopay (where a bill is paid, and whether it
pays itself).

Motivation: after the move, the household's bills live on several different
sites (the landlord's resident portal, the utility's billing service, the
internet provider). The Calendar tab already shows WHEN each bill is due and
whether it's been marked paid; it can't say WHERE to pay it or whether the bill
is already on autopay — the two questions asked every month. Two columns on the
existing bill definition answer both:

  pay_url  TEXT NULL                    the https page the bill is paid on;
                                        NULL = not recorded (every existing row)
  autopay  INTEGER NOT NULL DEFAULT 0   1 = the biller draws it automatically,
                                        0 = someone has to go pay it

Definition metadata only — like the bill's name or category, neither column is
money and no derivation reads them. The verb that sets them
(`set_bill_pay_details`) lands in this same increment; `bill_to_json` is a
curated dict (not SELECT *), so the byte-frozen /api/bills shape can't change —
the new fields are served by their own `GET /api/bills/pay-details`.

Schema only for the gate, and gated ZERO-DIFF by enumeration: no new table and
no new rows, both columns default to "unknown / not autopay", so the
who-owes-whom balance and every monthly total are byte-identical; the sole gate
diff is schema_version 16→17.

Guarded-idempotent like every migration (hard rule 1): each add is gated by a
PRAGMA table_info check — re-running apply() is a clean no-op. Mirrors
#012/#013/#016's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "bills")
    if "pay_url" not in have:
        # The https page this bill is paid on. NULL = not recorded, the meaning
        # every pre-#017 bill keeps.
        conn.execute("ALTER TABLE bills ADD COLUMN pay_url TEXT")
    if "autopay" not in have:
        # 1 = the biller draws it automatically; 0 = someone pays it by hand.
        # NOT NULL DEFAULT 0 so pre-#017 bills read as "not autopay" — the safe
        # default (it keeps the bill looking like it needs attention).
        conn.execute(
            "ALTER TABLE bills ADD COLUMN autopay INTEGER NOT NULL DEFAULT 0 "
            "CHECK (autopay IN (0, 1))")
