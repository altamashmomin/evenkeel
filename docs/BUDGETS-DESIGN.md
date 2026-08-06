# BUDGETS-DESIGN — Analytics Tier C

Category spending limits. The natural completion of the analytics tab: you
already *see* spend by category (composition, trends); budgets let you set a
target per category and track that month's net spend against it. Deferred out of
INCOME-DESIGN as "a different feature with its own design" — this is that design.

Checked against CORE-DESIGN; it governs. Where a design word and deployed code
disagree, deployed wins.

## Settled decisions (Alta, Aug 5, 2026)

1. **Simple monthly budgets, NOT rollover envelopes.** A budget is a fresh
   monthly limit; `budget_status` compares that month's spend to it. Stateless —
   reuses `spending_summary` directly, no per-month balance carried forward.
   Envelopes (unspent rolls to next month) are a bigger, stateful feature and can
   layer on later if wanted.
2. **UI: an Analytics-tab "Budgets" card** (not a dedicated view). Progress bars
   per budgeted category, over-budget in red, with lightweight inline set/edit.
   Reuses the analytics month-nav that already exists.
3. **Per-category limits** in v1 (no separate household-total line — a trivial
   add later if wanted).
4. **Any category is budgetable** (free-form, matching the transaction category
   vocab); the set/edit picker defaults to `app.DEFAULT_CATEGORIES`.
5. **Display-only** — over-budget shows a red bar; no alerts/nudges in v1 (a
   later ops-layer-style add).
6. **Show an "unbudgeted spend" line** so categories without a limit aren't
   invisible in the card.
7. **Refunds net against actuals** (via `spending_summary`), consistent with the
   whole app — return the air fryer and Household's actual drops.

## How it fits the CORE-DESIGN invariants

- **Rule 1 (schema is migration-owned):** a numbered `budgets` migration,
  `CREATE TABLE IF NOT EXISTS`, idempotent — same shape as #008 `items`.
- **Rule 2 (every write a verb):** `set_budget` (upsert one category's limit) and
  `remove_budget` (soft-delete); routes are thin callers, no raw SQL in a route.
- **Rule 4 (nothing derived stored):** the table stores only the limits;
  `budget_status` is computed on read.
- **Rule 5 (member-count agnostic):** household-scoped, no per-member column, same
  posture as bills/goals.
- **The money invariant is untouched:** a category limit is not a transaction, so
  budgets never cross the finance path — balance and every monthly total stay
  byte-identical. The migration's gate is an enumerated structural diff only
  (`budgets` table + `schema_version` 9→10). `budget_status` only *reads* spend on
  the analytics side.
- **Derivation tripwire:** `budget_status` reads `spending_summary` (refund-netted)
  → **EXEMPT**, exactly like `category_trend`. The exemption is bounded to the
  spend it reads; it computes no other inflow-sensitive number.

## Schema — migration #010

```
budgets(
  id           INTEGER PRIMARY KEY,
  category     TEXT NOT NULL UNIQUE,   -- one budget per category
  amount_cents INTEGER NOT NULL,       -- monthly limit, > 0 (verb-enforced)
  created_at   TEXT NOT NULL,          -- ISO-8601
  updated_at   TEXT NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1
)
```

`UNIQUE(category)`: one row per category, ever. `set_budget` upserts
(`INSERT … ON CONFLICT(category) DO UPDATE`, reactivating a soft-deleted row);
`remove_budget` sets `active=0`. The table holds only the current limit — the
change history lives in `audit_log`.

## Verbs (registered in CORE-DESIGN's table before they exist)

- `set_budget(db, actor, {category, amount})` — validate category non-empty +
  amount positive (integer cents via `to_cents`); upsert; audit before/after.
- `remove_budget(db, actor, {id|category})` — soft delete (`active=0`); audit.

## Derivation

`budget_status(db, period)` — for each active budget: `budgeted_cents`,
`actual_cents` (that category's `spending_summary` for `period`, refund-netted),
`remaining_cents` (budgeted − actual), `over` (bool), `pct` (actual/budgeted,
`round_ratio`). Money `{cents, display}` at the JSON edge. Driven by the
analytics month-nav (any month vs the standing limit). Plus an
`unbudgeted_spend` total (categories with spend but no budget) so nothing hides.

## API

```
GET    /api/analytics/budget-status?period=YYYY-MM   the card's data (default current_period)
GET    /api/budgets                                  list active budgets
POST   /api/budgets   {category, amount}             set_budget (upsert)
DELETE /api/budgets/<id>                              remove_budget (soft delete)
```

## Build order (one migration/verb per merge, per the per-increment loop)

1. **Migration #010 `budgets`** + registry rows + `GOVERNED_TABLES` +
   `REQUIRED_SCHEMA_VERSION` 9→10 → enumerated-diff gate (`budgets`=0 +
   `schema_version` bump), deployed `--live`.
2. **Verbs** `set_budget`/`remove_budget` + thin routes → zero-diff gate.
3. **Derivation** `budget_status` + `GET /api/analytics/budget-status` →
   zero-diff gate.
4. **Analytics UI** — the Budgets card (progress bars, red over-budget, inline
   set/edit) → frontend, no gate.

## Deliberately out of scope (v1)

- **Rollover envelopes** — the stateful carry-forward model; its own later design.
- **Alerts / proactive over-budget notifications** — a cron+notification concern
  (the ops layer's shape), not a read derivation.
- **Household-total budget** — per-category only for now.
- **Per-member budgets** — household-scoped, like everything else here.
