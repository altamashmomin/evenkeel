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

**The whole `rework` is built, deployed, and live on the Raspberry Pi (schema
v11).** `origin/main` == the deployed tree; `origin/rework` sits a doc commit or
two ahead by convention. The app is feature-complete across its domains — the
who-owes-whom finance core, income classification, analytics (Tiers A–C incl.
budgets), the household pantry with purchase-feed inference, the assistant (the
tailnet MCP read+write tier and Charlee's in-app Ask tab), the Garden UI, and a
lean 6-agent ops/security layer — with the money invariants (integer cents, one
write path, migration-owned schema, the balance gate) intact throughout.

**The full narrative — every increment, its gate result, deploy record, and the
design decisions behind it — lives in [`docs/PROGRESS-LOG.md`](docs/PROGRESS-LOG.md)**
(moved there Aug 9, 2026 to keep this file to the load-bearing rules; the history
is preserved in git and in that log). Read it for context on what is done and why.

**Most recent work (Aug 8–9, 2026):** a comprehensive multi-agent code review
(delivered as a PDF) and the remediation of its findings — agent roster pruned
14→6, the `member_breakdown` odd-cent conservation bug fixed, `/trace` put behind a
session, `deploy.sh` hardened (no-op/race guard + local-branch heal + fatal smoke
check), and the MCP write tier made opt-in (`LEDGER_MCP_ENABLE_WRITES`) — all
deployed. Then a repo-hygiene pass: this CLAUDE.md split and a shared, cached test
fixture (suite runtime ~64s → ~32s). **All code-review findings are now closed**
(the one open item is Alta's off-repo tailnet-ACL check for the MCP write tier).
Then (Aug 10): **`GET /api/ontology` + the Trace Web data-driven from it** — the
map now fetches its facts at load, so it cannot drift; two real ontology-
derivation bugs (docstring-as-call, paren-less references) found and fixed in the
process. Suite 536+98, no gate. **Deployed** (`main` `a6adc03`, GATE PASS zero-diff;
`/api/ontology` verified 401 unauthenticated). Then (Aug 18): the Pi's
`ANTHROPIC_API_KEY` rotated on schedule — new expiry 2026-09-17, ops guardian
green (see PROGRESS-LOG).

After each increment, append the record to `docs/PROGRESS-LOG.md` (not this file),
and keep this section a short pointer to the current state.

## Conventions

- Branch: `rework`. Commits small and single-purpose; message states
  which sequence step / migration number it advances.
- Migrations live in `migrations/NNN_description.sql` (or `.py` when
  logic is needed), idempotent, applied in order inside a transaction.
- Tests use a synthetic seed database that mirrors the deployed schema;
  never real data in tests.
- Actor strings everywhere: `ui:<member>` | `sync` | `mcp:<token-label>`.
- `seed_income.py` (added July 23, 2026) is the income build's fixture,
  run as a third step after `seed_db.py` + `migrate.py apply` — it can't
  be folded into `seed_db.py` itself, which freezes the v1.0 DDL and
  runs before `direction`/`income_type` exist. Use it whenever a change
  should be checked against realistic mixed spend+income data, including
  when building `dev.db` for the balance gate.
- `tests/test_derivation_tripwire.py` automatically checks every
  db-taking function in `derivations.py` against inflow-contamination
  (introspects the module, so a new aggregate is covered without anyone
  registering it) — the organic version of the manual cross-error audit
  that caught `spending_summary`'s original bug. It only catches a
  *mandatory* filter going missing; a *defense-in-depth* one
  (`compute_balance`, `settle_up`) still needs
  `test_income_isolation.py`'s deliberately-invariant-violating
  fixtures. Both were verified to actually fail by temporarily
  reintroducing the bugs they guard
  against — a claimed regression test is unproven until it's watched
  to fail once.
  **Honest coverage note (corrected Aug 7, 2026, CODE-REVIEW #8): "the
  tripwire covers it" is real but NOT automatic-for-free.** A derivation
  is only genuinely covered if the fixture contains data the probe can
  contaminate. The fixture was enriched (setUp seeds `items`; the probe
  adds three matching in-window shopping-category inflows) so removing the
  repo's `direction='out'` filters now trips 8 of the 15 discovered
  functions (was 1 — the fixture previously had no `items` rows, so seven
  pantry derivations compared `[] == []`). The other 7 are provably
  uncontaminatable by an inflow (splits INNER JOIN → `test_income_isolation`;
  read no transactions; or excluded-by-design), documented in the test's
  docstring. When a NEW derivation says "tripwire-covered," confirm the
  fixture actually reaches it — the hand-written `*_ignores_inflows` tests
  remain the primary coverage for anything the generic probe can't move.
- Frontend testing (added July 24, 2026): the pure presentation helpers
  live in `static/render.js` (a dual-environment module — browser reads
  `window.Render`, node `require`s it), split out of `app.js` precisely
  so they can be unit-tested headless. `tests/test_render.js` covers them
  in plain node (no framework, no build step); `tests/test_frontend_render.py` shells out to it so `python -m unittest` runs it too (skips
  if node is absent). This closed the frontend's zero-coverage gap; the
  stack stays vanilla (see the stack decision — the debt was tests, not
  the framework). State-coupled render fns (`txnRow`, `beamHTML`) are the
  next extraction targets when they're touched — they need light
  dependency-injection (pass `users` in) to become pure/testable.
