# CLAUDE.md — Ledger

Household finance app. Flask + sqlite3 + vanilla SPA, deployed on a
Raspberry Pi, synced from SimpleFIN. **This is a live app with real
financial data and a second user (Charlee). Correctness beats speed.**

## Read before designing anything

- `docs/CORE-DESIGN.md` — the constitution. Invariants, schema grammar,
  action registry, migration sequence, the pipeline. It governs this
  branch; check new ideas against it.
- `docs/INCOME-DESIGN.md` — income/classification feature (sequence step 6).
- `docs/AGENT-DESIGN.md` — MCP agent layer (read tier early, writes step 7).

Where a design doc and deployed code spell a column differently, the
deployed spelling wins. Roles matter, not names.

## Hard rules (from CORE-DESIGN invariants — not negotiable)

1. Schema changes ONLY as numbered idempotent migration files run by the
   migration runner and recorded in `schema_version`. Never ad-hoc
   ALTER/CREATE against any database, including dev copies.
2. Every write path is a named verb in `actions.py`
   (validate → edit → side effects → audit). Routes, sync, and MCP tools
   are thin callers. Do not write INSERT/UPDATE/DELETE in a route.
3. Money is integer cents; all computation float-free (`derivations.py`).
   Dollars-as-floats survive only at the deployed JSON edge until the
   API-versioning increment (hardening disposition 2). Timestamps are
   ISO-8601 text.
4. Nothing derived is stored. Balance, totals, summaries: computed on
   read by named functions; every surface calls the same function.
5. No code may assume the household has exactly 2 members. Member count
   is data. Features gate via submission criteria inside verbs.
6. NEVER touch `finance.db` (the live database) directly. All local work
   runs against a copy: `cp finance.db dev.db`. The backup copy on the
   Pi is the rollback.
7. Never commit: `.env`, secrets, SimpleFIN tokens, `*.db`, `*.db.bak-*`.
8. `main` is always deployable. Small increments; one migration or one
   verb per merge; never a batch.

## The per-increment loop

1. Build the increment on the rework branch.
2. `cp finance.db dev.db` — stage against real data.
3. Run the balance gate (below) against `dev.db`.
4. On the Pi: `cp finance.db finance.db.bak-<date>`, apply, re-verify.
5. Merge to `main`. Repeat.

## The balance gate (run before every merge)

Old code and new code, side by side against `dev.db`, must agree on:
- the who-owes-whom balance **to the cent**
- monthly spend totals for every month present
- per-table row counts

An increment that intentionally changes a number must enumerate the
expected diff in its notes; only the enumerated diff passes. If the gate
script doesn't exist yet, building it precedes the increment it gates.

## Current position in the sequence

Pre-step-0: the Pi is not yet deployed (hardware pending). Everything so
far lives on `rework`, unmerged — merging waits for the Pi deploy and the
`v1.0` tag at the deployed state.

Done (sessions one and two, on `rework`; session one reviewed and
approved, session two awaiting review):
- Migration runner `migrate.py` + migration #001 (`schema_version`)
- The balance gate `gate.py` (snapshot / compare / run, enumerated
  expected diffs)
- Synthetic seed generator `seed_db.py` (frozen v1.0 DDL, faithful
  settlements)
- Migration #002 — `users` → `members` (ids preserved), split column
  exploded into basis-point `splits` rows, old column dropped; app +
  sync moved to members/splits with byte-identical API responses;
  gated with zero balance/monthly change (notes/002-gate-expectation)
- Migration #003 — `links` table, unwired; gated (one empty table)

Hardening (from `codex/rework-hardening`) reviewed, accepted, and merged
into `rework` (July 19, 2026):
- Schema is migration-owned end to end: `migrate.py init` creates fresh
  databases, app/sync verify migration history read-only at startup.
- The gate compares old code + untouched DB against new code + a
  separately migrated copy, via each version's own derivations.
- 15 regression tests, including byte-identical API parity (v1 vs
  current) and the finance.db name guard; `--live` flag added for the
  Pi's own deploy steps.
- The four review dispositions are settled — recorded under "Hardening
  dispositions" in CORE-DESIGN.md.

Verb extraction underway (one route per session):
- `settle_up` extracted (July 19, 2026): migration #004 (`audit_log`),
  `actions.py` registry born, settlement route is a thin caller; the
  verb writes row + splits + `settles` links + audit atomically. Gated
  (one empty table, zero number change); suite at 22 tests.

- `mark_bill_paid` + `unmark_bill_paid` extracted (July 19, 2026): bill
  pay/unpay routes are thin callers; unmark reverts links too. Bugfix
  alongside: deleting a settlement-covered transaction cleans up its
  links. Zero-diff gate; suite at 28 tests.

Correction pass (July 20, 2026) — Codex's nine-point review of the verb
extractions, implemented jointly (Codex commits ddf5f24..fe3d733, Claude
finished the baseline pin, auth parity, and --live proof):
- Synthetic clock explicit: `seed_db.py --as-of`; fixtures are pure
  functions of (seed, months, as_of); tests freeze 2026-07-19.
- Verbs own their transaction boundary (`action_transaction`: BEGIN
  IMMEDIATE, rollback on any failure, refuse pre-open transactions).
- Legacy split adapter honestly two-member-only with cardinality
  submission criteria; percentages parsed as exact basis points.
- `settle_up` is a command: server derives amount/ower/description from
  `compute_balance(as_of=...)`; caller values are stale-state assertions
  (curl-level arbitrary settlements now rejected — deliberate).
- Deletion centralized (`delete_transaction_graph`) with before-image
  audit details; settlement-history edit policy recorded in CORE-DESIGN
  for the coming `edit_transaction` extraction.
- Parity pinned to immutable baseline 41c2040 (→ `v1.0` tag later) and
  covers status/categories/unauthenticated surface. Suite at 35 tests;
  zero-diff gate dbd5cd4→HEAD.

- Goal verbs extracted (July 20, 2026): `create_goal`, `delete_goal`,
  `contribute_to_goal`, `withdraw_from_goal` — registry rows first, four
  verbs under the corrected contract, `/contribute` dispatches on sign
  (row stores the signed amount, the verb name records intent),
  `delete_goal` audits goal summary + contribution count + saved total
  before the cascade. `archive_goal`/`restore_goal` still deferred to
  their own migration increment. Zero-diff gate f39f3ba→HEAD; suite at
  43 tests.

- `edit_transaction` + `delete_transaction` extracted (July 22, 2026):
  transaction PUT/DELETE routes are thin callers. `edit_transaction`
  enforces the settlement-history edit policy — settlement rows refuse
  edits (delete and recreate is the correction path), and editing a
  covered ordinary row severs its incoming `settles` link in-verb,
  recorded in the audit detail, reopening the row for the next
  settlement. `delete_transaction` wraps the existing
  `delete_transaction_graph` helper with an audit row carrying the full
  before-image; deleting a settlement reopens everything it covered via
  the same graph cleanup. `payer_share_pct` moved from `app.py` into
  `actions.py` since the edit verb needs it to carry an untouched share
  forward. `set_splits` still deferred — no standalone caller exists
  yet. Zero-diff gate f31ab1f→133ca94 (fresh seeded `dev.db`, no
  `finance.db` yet — pre-step-0); suite at 59 tests.

- `record_transaction` extracted (July 22, 2026): the last verb in the
  extraction sequence. Unifies the UI's manual-entry insert and
  `simplefin_sync.py`'s insert — dedupe on `external_id` lives inside
  the verb (`ON CONFLICT DO NOTHING`, same as the deployed sync script
  used directly, now a no-op-not-an-error return value rather than a
  silent skip), `source` is a verb decision never taken from caller data
  (same discipline as `settle_up`/`mark_bill_paid`). Closes a real gap:
  manual entries previously wrote no audit row at all. `sync.py` drops
  its hand-rolled insert/split/`other_id` logic for one call per
  transaction, each committing independently now rather than the whole
  run sharing one transaction — matches every other verb's per-call
  atomicity. `set_splits` still deferred — no standalone caller exists.
  Zero-diff gate 9168edf→3ace89d (fresh seeded `dev.db`, no `finance.db`
  yet — pre-step-0); suite at 71 tests.

Verb extraction (CORE-DESIGN sequence step 5) is now complete: every
write path is a named verb in `actions.py`, and audit_log covers UI and
sync both. Next: the income build (sequence step 6, per INCOME-DESIGN),
built inside the grammar — classification verbs enter the registry,
aggregates are derivations, rules run as side effects of
`record_transaction`. Merging to `main` still waits for the Pi deploy
and the `v1.0` tag.

Tag `v1.0` at the deployed state before the first rework commit lands.

After each merged increment, update this "Current position in the
sequence" section to reflect what's done and what's next.

## Conventions

- Branch: `rework`. Commits small and single-purpose; message states
  which sequence step / migration number it advances.
- Migrations live in `migrations/NNN_description.sql` (or `.py` when
  logic is needed), idempotent, applied in order inside a transaction.
- Tests use a synthetic seed database that mirrors the deployed schema;
  never real data in tests.
- Actor strings everywhere: `ui:<member>` | `sync` | `mcp:<token-label>`.
