-- #019 — check_in_mutes (Ask check-ins, Nov 4, 2026). Schema only: one new,
-- empty table; no existing row changes.
--
-- Why: Ledger's assistant now starts a conversation on its own ("Coffee is
-- usually restocked every 12 days — running low?"). Which check-ins exist is
-- DERIVED on read (derivations.check_ins, from the pantry forecast, bills,
-- budgets and the feed) and never stored. What IS stored is the one thing
-- that can't be derived: what each person told it to stop raising. That is a
-- per-person preference, an input fact like a budget limit, not a derivation.
--
-- One row per (member, key). Two key shapes:
--   'kind:<kind>'            — this person turned a whole kind off
--                              ("don't ask me about budgets")
--   '<kind>:<subject...>'    — one check-in dismissed (until NULL) or
--                              snoozed (until = a 'YYYY-MM-DD'). The subject
--                              carries the instance (a bill AND its month, an
--                              item AND its forecast date), so next month's
--                              bill or the next restock asks again on its own.
-- until NULL = until turned back on. Rows are written only by the
-- mute_check_in / unmute_check_in verbs (invariant 2).
--
-- Money posture: no money derivation reads this table and nothing here
-- references a transaction, so the who-owes-whom balance and every monthly
-- total are byte-identical. Gate: schema_version 18 -> 19 is the only diff
-- (the new table starts empty; notes/019-gate-expectation.seed.json).
CREATE TABLE IF NOT EXISTS check_in_mutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    key         TEXT NOT NULL,        -- 'kind:<kind>' or '<kind>:<subject>'
    until       TEXT,                 -- 'YYYY-MM-DD' snooze end; NULL = until turned back on
    created_at  TEXT NOT NULL,        -- ISO-8601
    updated_at  TEXT NOT NULL,        -- ISO-8601
    UNIQUE (member_id, key)
);
