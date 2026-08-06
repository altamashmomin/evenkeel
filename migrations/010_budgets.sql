-- #010 — budgets (Analytics Tier C: category spending limits).
-- A monthly spending limit per category. `budget_status` compares each limit
-- against that month's net spend (spending_summary, refund-netted) — nothing
-- derived is stored here, only the limits. One row per category (UNIQUE);
-- set_budget upserts and remove_budget soft-deletes (active=0), and audit_log is
-- the change history. Household-scoped (no per-member column), same posture as
-- bills/goals. Simple monthly budgets, NOT rollover envelopes (BUDGETS-DESIGN).
--
-- Additive: a fresh empty table, no existing row changes, and NOTHING here
-- touches money — a category limit is not a transaction, so the who-owes-whom
-- balance and every monthly total are byte-identical (budgets never cross the
-- finance path; budget_status only READS spend on the analytics side).
CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL UNIQUE,           -- one budget per category
    amount_cents INTEGER NOT NULL,               -- monthly limit, > 0 (verb-enforced)
    created_at   TEXT NOT NULL,                  -- ISO-8601
    updated_at   TEXT NOT NULL,                  -- ISO-8601
    active       INTEGER NOT NULL DEFAULT 1      -- soft delete; history stays attributable
);
