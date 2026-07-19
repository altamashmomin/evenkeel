-- #001 — v1 baseline + migration bookkeeping (CORE-DESIGN sequence step 1).
-- Existing deployed databases already have the v1 tables, so these statements
-- are no-ops there. A fresh `migrate.py init` database gets the exact same v1
-- shape before later migrations transform it. Schema is therefore created only
-- by numbered migrations, never by importing or starting the application.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    txn_date        TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Other',
    paid_by         INTEGER NOT NULL REFERENCES users(id),
    is_shared       INTEGER NOT NULL DEFAULT 1,
    payer_share_pct REAL NOT NULL DEFAULT 50
                    CHECK (payer_share_pct >= 0 AND payer_share_pct <= 100),
    source          TEXT NOT NULL DEFAULT 'manual',
    external_id     TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);

CREATE TABLE IF NOT EXISTS bills (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_day      INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    category     TEXT NOT NULL DEFAULT 'Bills',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bill_payments (
    id      INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    period  TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    txn_id  INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    UNIQUE (bill_id, period)
);

CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    target_date  TEXT,
    created_at   TEXT NOT NULL DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    c_date       TEXT NOT NULL,
    note         TEXT
);

-- Creates the table the runner records every migration in, itself included.
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,     -- migration number
    applied_at  TEXT NOT NULL,           -- ISO-8601
    description TEXT NOT NULL
);
