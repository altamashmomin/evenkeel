-- #007 — pending_actions (AGENT-DESIGN step 4: the agent write tier's
-- two-phase choreography). A propose call parks an action here with a frozen
-- payload and a computed preview; a confirm call (after the human has seen the
-- preview and said yes) executes exactly that frozen payload. Server-side, not
-- a `confirm=true` parameter: the payload can't drift between approval and
-- execution, the token is single-use, and expiry kills stale approvals — the
-- guarantees live in the database, not the conversation (AGENT-DESIGN's
-- "confirmations live in the database").
--
-- token       — short random single-use string the confirm call presents.
-- action_type — which verb to dispatch: 'create_rule' | 'apply_rules'.
-- payload_json — the exact args to execute with (frozen at propose time).
-- preview_json — what was shown to the human (the dry-run result).
-- created_by  — the api_tokens row whose agent proposed this.
-- expires_at  — ~10 minutes out; a confirm after this is refused.
-- status      — pending | confirmed | expired | cancelled.
--
-- Additive: a fresh empty table, no existing row changes. No balance or spend
-- path reads or writes it, so the who-owes-whom balance and every monthly
-- total are byte-identical.
CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,             -- single-use random string
    action_type  TEXT NOT NULL,                    -- 'create_rule' | 'apply_rules'
    payload_json TEXT NOT NULL,                    -- exact args to execute with
    preview_json TEXT NOT NULL,                    -- what was shown to the human
    created_by   INTEGER REFERENCES api_tokens(id),
    created_at   TEXT NOT NULL,                    -- ISO-8601
    expires_at   TEXT NOT NULL,                    -- ISO-8601; stale approvals die
    status       TEXT NOT NULL DEFAULT 'pending'   -- pending|confirmed|expired|cancelled
);
