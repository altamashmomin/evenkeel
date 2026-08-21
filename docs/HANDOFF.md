# HANDOFF — Ledger

A quick orientation for a new session. **Read [`../CLAUDE.md`](../CLAUDE.md)
first** (the load-bearing rules + the "Current position in the sequence"
pointer), then [`PROGRESS-LOG.md`](PROGRESS-LOG.md) for the full increment-by-
increment narrative. This file is a snapshot; CLAUDE.md's "Current position" is
the authoritative running state and is updated after every increment/deploy.

## Current state (as of Aug 21, 2026)

- `origin/main` == the deployed Raspberry Pi tree. **Live schema: v13.**
- Everything merged is deployed; no open PRs, nothing merged-but-undeployed.
- Balance gate green; the money invariants hold throughout.

## What the last session shipped (all live)

- **Transfer-neutral fix — complete (T1–T3 + consistency).** Fixes SimpleFIN
  mis-signs, e.g. a credit-card "Payment Thank You" that posts *positive* and
  reads as income when it's really a transfer between the household's own
  accounts.
  - **T1** — `is_transfer` flag (migration `012_transfer_flag`); every money
    derivation excludes `is_transfer = 0`.
  - **T2** — the `set_transfer` verb + "Mark as transfer" toggle in the classify
    & edit dialogs; neutral 🔁 row rendering; hidden from the spending/income
    activity filters.
  - **T3** — a transfer can be auto-tagged by an income rule
    (`income_rules.set_transfer`, migration `013_rule_set_transfer`), with a
    "make this a rule?" nudge after the 2nd manual mark; creating the rule sweeps
    the unclassified backlog (`apply_rules`) and self-flags future syncs.
  - **Consistency** — a marked transfer also drops out of `top_merchants`,
    `recurring_charges`, and the pantry inference views, not just the totals.
- **Settle-up breakdown** — a "why is it this amount" itemized ledger in the
  settle dialog; reconciles to `compute_balance` to the cent via a `carryover`
  residual (handles legacy settlements that link no rows).
- **Recategorize from Spent** — tap a category on Home → move its transactions
  into a new/existing category (categories are emergent transaction tags; no
  `categories` table).
- **Scenario port** — kept the Goals-tab goal-pace what-if; the Forecast lab was
  ported, then removed once it wasn't earning its place.

## Ground rules that bite (from CORE-DESIGN)

- Money is **integer cents**, float-free in `derivations.py`; dollars only at the
  JSON edge.
- **One write path**: every write is a named verb in `actions.py`
  (validate → edit → side effects → audit) and registered in CORE-DESIGN's action
  table. Routes/sync/MCP are thin callers — no raw INSERT/UPDATE/DELETE in a
  route.
- **Migration-owned schema**: schema changes only via numbered idempotent
  migration files recorded in `schema_version`. Never ad-hoc ALTER/CREATE, even
  on dev copies.
- **Nothing derived is stored** — balances/totals/summaries computed on read;
  every surface calls the same function.
- **The balance gate before every merge**: old code vs new code on a `dev.db`
  copy must agree to the cent (balance, monthly totals, per-table row counts). A
  migration's only allowed diff is the enumerated `schema_version` bump, recorded
  in `notes/NNN-gate-expectation.seed.json`.
- **NEVER touch `finance.db`** (the live DB) directly; work against a `dev.db`
  copy.

## Environment note

A cloud/web session is a fresh clone that **cannot reach the Pi** (it's on the
household tailnet). Deploys and the real-data balance gate are Alta's to run on
the Pi (`deploy/deploy.sh origin/main`); a cloud session gates against a
*synthetic* `dev.db` and hands off the deploy. After Alta deploys, record it in
`PROGRESS-LOG.md` + the CLAUDE.md pointer.

## Optional shelf items (nothing pending)

- A read-only **health sweep** (dependency freshness / CVEs / test health /
  balance gate) — prudent after two live migrations this week
  (`ledger-health-sweep` agent).
- Extract the state-coupled render fns **`txnRow` / `beamHTML`** from `app.js`
  into `render.js` with node seam tests (CLAUDE.md's named next testability
  target; `txnRow` was just touched for the transfer rendering).
- Alta's off-repo **tailnet-ACL check** for the MCP write tier (long-standing
  open item, not code).
