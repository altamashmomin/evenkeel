-- #001 — migration bookkeeping (CORE-DESIGN sequence step 1).
-- Creates the table the runner records every migration in, itself included.
-- Idempotent: IF NOT EXISTS makes a re-run a no-op.
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,     -- migration number
    applied_at  TEXT NOT NULL,           -- ISO-8601
    description TEXT NOT NULL
);
