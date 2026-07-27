-- #006 — api_tokens (AGENT-DESIGN step 1: the assistant layer's auth).
-- Revocable bearer tokens for non-browser clients (the tailnet MCP server,
-- any future API client). Session cookies are for browsers; agents need a
-- hashed, labeled, revocable token. We store ONLY the SHA-256 hash, never
-- the token itself. user_id -> members(id) makes tokens per-person, so
-- actions attribute to a person and income visibility can differ per agent.
-- scopes defaults to 'read', so a read-only token exists on day one and the
-- read tier can ship before any write tool. Additive: a fresh empty table,
-- no existing row changes -- balance and every monthly total are identical.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,           -- SHA-256 hex; never the token
    label        TEXT NOT NULL,                  -- "claude-code on laptop"
    user_id      INTEGER REFERENCES members(id), -- whose agent this is
    scopes       TEXT NOT NULL DEFAULT 'read',   -- 'read' | 'read,write'
    created_at   TEXT NOT NULL,                  -- ISO-8601
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
