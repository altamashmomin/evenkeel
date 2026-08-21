"""#013 — income_rules.set_transfer (transfer-neutral fix, increment T3a).

Motivation: T1/T2 let a person mark a transaction as a transfer (the
`is_transfer` flag), but each mis-signed row is still marked by hand. A
credit-card "Payment Thank You" arrives every statement cycle, so it wants a
RULE — "always treat this as a transfer" — the way income rules already
auto-classify recurring inflows.

This column is the rule's transfer ACTION, kept separate from `set_type` so a
rule flags a match explicitly rather than by overloading the income_type: a
rule with set_transfer=1 sets `is_transfer=1` on matching inflows (during sync
in record_transaction, and retroactively over the unclassified backlog in
apply_rules). `is_transfer` stays the single source of truth (T1/T2).

Schema only, and gated ZERO-DIFF by enumeration: the column defaults to 0, so
no existing rule flags anything — sync and apply_rules behave byte-identically
until a transfer rule is CREATED (increment T3b, which teaches
create_income_rule to accept the flag and adds the "make this a rule?" nudge
after a manual Mark-as-transfer). Rules never touch money directly and no row
is re-flagged here, so the balance and every monthly total are unchanged.

Guarded-idempotent like every migration (hard rule 1): the add is gated by a
PRAGMA table_info check — re-running apply() is a clean no-op. Mirrors
#005/#009/#011/#012's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "income_rules")
    if "set_transfer" not in have:
        # 1 = a rule that marks its matches as transfers (is_transfer=1); 0 =
        # an ordinary income-classification rule, the meaning every existing
        # rule keeps.
        conn.execute(
            "ALTER TABLE income_rules ADD COLUMN set_transfer INTEGER NOT NULL DEFAULT 0")
