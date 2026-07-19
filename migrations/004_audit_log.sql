-- #004 — audit_log (CORE-DESIGN sequence step 4, AGENT-DESIGN schema,
-- amended: every write path logs here, not only the agent's).
-- Table only; rows arrive as each verb is extracted, because the verb
-- contract writes the audit row inside the same transaction as its edit
-- (hardening disposition 4). settle_up is the first writer.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,          -- ISO-8601
    actor        TEXT NOT NULL,          -- 'ui:<member>' | 'sync' | 'mcp:<token-label>'
    action       TEXT NOT NULL,          -- verb name: 'settle_up' | ...
    target       TEXT,                   -- 'transaction:412' | 'rule:7'
    detail_json  TEXT NOT NULL           -- before/after or payload
);
