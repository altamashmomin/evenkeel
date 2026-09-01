"""#016 — pending_actions.proposed_by_user (MIRAGE F2: bind the two-phase
confirm to the proposing identity).

Motivation: the two-phase machinery (step 7) parks a proposal under a
single-use token, then executes it on confirm. `confirm_action` looked the
row up by token ALONE and never checked who was confirming, so in a
multi-member household one person could confirm a proposal another person
parked. `created_by` (migration #007) couldn't serve as the binding key: it
REFERENCES api_tokens(id) — so it's NULL for the Ask surface (sessions carry
no token_id) and can't hold a member id under the runtime's enforced FK.

This column records the PROPOSING member (from the authenticated identity at
propose time — g.auth["user_id"], populated for BOTH a browser/Ask session and
a per-person bearer token). `confirm_action` rejects a token whose
`proposed_by_user` differs from the confirming identity. `created_by` is kept
unchanged as the token-level audit trail.

Nullable by design: legacy/pre-migration pending rows keep NULL, and a NULL
proposer is treated as unbound — it never blocks a confirm (there are no
long-lived pending rows across a deploy; the TTL is ~10 minutes). The FK to
members(id) matches the runtime's `PRAGMA foreign_keys = ON`.

Schema only, and gated ZERO-DIFF by enumeration: no money path reads or writes
pending_actions, no existing row changes, the column defaults to NULL. The
who-owes-whom balance and every monthly total are byte-identical; the sole
gate diff is schema_version 15→16.

Guarded-idempotent like every migration (hard rule 1): the add is gated by a
PRAGMA table_info check — re-running apply() is a clean no-op. Mirrors
#005/#009/#011/#012/#013's pattern.
"""


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn):
    have = columns(conn, "pending_actions")
    if "proposed_by_user" not in have:
        # The member who proposed this action, captured from the authenticated
        # identity at propose time. NULL = legacy/unbound (never blocks a
        # confirm). REFERENCES members(id) under the runtime's enforced FK.
        conn.execute(
            "ALTER TABLE pending_actions "
            "ADD COLUMN proposed_by_user INTEGER REFERENCES members(id)")
