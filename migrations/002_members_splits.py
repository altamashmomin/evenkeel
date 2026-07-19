"""#002 — members + splits (CORE-DESIGN sequence step 2, settled decision
"Split semantics normalized at migration time").

users -> members (auth columns carried over, ids PRESERVED so paid_by and
SYNC_PAID_BY keep meaning the same people). Every shared transaction's
payer_share_pct — the PAYER's own share, per the reviewed interpretation —
explodes into two splits rows in basis points summing to 10000. The old
column and the users table are then dropped; the percentage's ambiguity
does not survive.

Guards, each aborting the whole migration (the runner rolls back):
  - a shared transaction whose pct*100 is not an integer (no exact bp form)
  - any row where the recomputed owed-cents differs from the old formula
    round(amount * (100 - pct) / 100) — the balance may not move by a cent
  - shared rows present with member count != 2 (the old column is only
    meaningful for exactly two people)

Table rebuilds follow the SQLite procedure (create new, copy, drop, rename);
the runner runs migrations with foreign keys OFF and foreign_key_checks
before COMMIT.
"""
from datetime import datetime, timezone

MEMBERS_DDL = """
CREATE TABLE members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
)"""

SPLITS_DDL = """
CREATE TABLE splits (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    member_id      INTEGER NOT NULL REFERENCES members(id),
    share_bp       INTEGER NOT NULL CHECK (share_bp BETWEEN 0 AND 10000),
    PRIMARY KEY (transaction_id, member_id)
)"""

TRANSACTIONS_NEW_DDL = """
CREATE TABLE transactions_new (
    id              INTEGER PRIMARY KEY,
    txn_date        TEXT NOT NULL,                 -- ISO date YYYY-MM-DD
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Other',
    paid_by         INTEGER NOT NULL REFERENCES members(id),
    is_shared       INTEGER NOT NULL DEFAULT 1,    -- 0/1; 1 <=> splits rows exist
    source          TEXT NOT NULL DEFAULT 'manual', -- manual | bill | simplefin | settlement
    external_id     TEXT UNIQUE,                   -- dedupe key for automated sources
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)"""

GOAL_CONTRIBUTIONS_NEW_DDL = """
CREATE TABLE goal_contributions_new (
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES members(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    c_date       TEXT NOT NULL,
    note         TEXT
)"""


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def apply(conn):
    if table_exists(conn, "members") and not table_exists(conn, "users"):
        return  # already applied — idempotent no-op
    if table_exists(conn, "members") or table_exists(conn, "splits"):
        # The runner is transactional, so this state can only come from a
        # hand-edited database; refuse to guess.
        raise RuntimeError("members/splits already exist alongside users — "
                           "database is in a state this migration does not recognize")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- members: same people, same ids, honest name
    conn.execute(MEMBERS_DDL)
    conn.execute(
        "INSERT INTO members (id, username, display_name, password_hash, active, created_at) "
        "SELECT id, username, display_name, password_hash, 1, ? FROM users ORDER BY id",
        (now,))
    member_ids = [r[0] for r in conn.execute("SELECT id FROM members ORDER BY id")]

    # -- splits: explode the payer-share percentage, guarded
    conn.execute(SPLITS_DDL)
    shared = conn.execute(
        "SELECT id, amount_cents, paid_by, payer_share_pct FROM transactions "
        "WHERE is_shared = 1 ORDER BY id").fetchall()
    if shared and len(member_ids) != 2:
        raise RuntimeError(
            f"{len(shared)} shared transactions but {len(member_ids)} members — "
            "payer_share_pct is only meaningful for exactly two people")

    problems = []
    split_rows = []
    for txn_id, amount_cents, paid_by, pct in shared:
        if paid_by not in member_ids:
            problems.append(f"txn {txn_id}: paid_by={paid_by} is not a member")
            continue
        payer_bp = round(pct * 100)
        if abs(pct * 100 - payer_bp) > 1e-6:
            problems.append(f"txn {txn_id}: payer_share_pct={pct!r} has no exact "
                            "basis-point representation")
            continue
        owed_old = round(amount_cents * (100 - pct) / 100)
        owed_new = round(amount_cents * (10000 - payer_bp) / 10000)
        if owed_old != owed_new:
            problems.append(f"txn {txn_id}: owed cents would move "
                            f"{owed_old} -> {owed_new}")
            continue
        other = member_ids[0] if paid_by == member_ids[1] else member_ids[1]
        split_rows.append((txn_id, paid_by, payer_bp))
        split_rows.append((txn_id, other, 10000 - payer_bp))
    if problems:
        shown = "; ".join(problems[:10])
        raise RuntimeError(f"{len(problems)} row(s) cannot convert safely: {shown}")
    conn.executemany(
        "INSERT INTO splits (transaction_id, member_id, share_bp) VALUES (?, ?, ?)",
        split_rows)

    # -- transactions: drop payer_share_pct, point paid_by at members
    conn.execute(TRANSACTIONS_NEW_DDL)
    conn.execute(
        "INSERT INTO transactions_new (id, txn_date, amount_cents, description, "
        "category, paid_by, is_shared, source, external_id, created_at) "
        "SELECT id, txn_date, amount_cents, description, category, paid_by, "
        "is_shared, source, external_id, created_at FROM transactions")
    conn.execute("DROP TABLE transactions")
    conn.execute("ALTER TABLE transactions_new RENAME TO transactions")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date)")

    # -- goal_contributions: user_id now references members
    conn.execute(GOAL_CONTRIBUTIONS_NEW_DDL)
    conn.execute(
        "INSERT INTO goal_contributions_new (id, goal_id, user_id, amount_cents, "
        "c_date, note) SELECT id, goal_id, user_id, amount_cents, c_date, note "
        "FROM goal_contributions")
    conn.execute("DROP TABLE goal_contributions")
    conn.execute("ALTER TABLE goal_contributions_new RENAME TO goal_contributions")

    # -- retire users
    conn.execute("DROP TABLE users")
