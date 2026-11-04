"""#022 — bank_accounts.shared_default (do new charges from this card start
shared, or as the owner's own spending?).

Motivation: before #022 the sync imported every charge as shared 50/50. On a
card one person uses for their own things, every personal purchase, card bill
payment and Venmo then counted as half-owed by the other person, so the
who-owes-whom balance drifted further from the truth every week.
One column says how an account's NEW charges start:

  shared_default  INTEGER NOT NULL DEFAULT 0   1 = new charges start shared
                                               50/50 (the old behaviour);
                                               0 = they start as the owner's
                                               own spending (the new default)

A person flips it per card in Activity > Whose accounts?. The sync's other
signals still win over it (derivations.import_share): a joint account, a
charge that matches the one linked to a bill last time, and a merchant a
person already marked shared or personal.

Schema only for the gate, gated by enumeration: no new table, no new rows, and
no stored transaction changes, so the balance and every monthly total are
byte-identical; the sole gate diff is schema_version 21 -> 22
(notes/022-gate-expectation.seed.json). The column only shapes charges the
sync imports AFTER the deploy.

Guarded-idempotent like every migration: the add is gated by a PRAGMA
table_info check, so re-running apply() is a clean no-op.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    if "shared_default" not in columns(conn, "bank_accounts"):
        conn.execute(
            "ALTER TABLE bank_accounts ADD COLUMN shared_default INTEGER NOT NULL "
            "DEFAULT 0 CHECK (shared_default IN (0, 1))")
