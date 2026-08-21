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
v14 as of Aug 21, 2026 — migration `014_item_store_needby_snooze`; v13/v12 came
Aug 21/20 with the transfer flags; v11 held from the initial bring-up until then).**
`origin/main` == the deployed tree; `origin/rework` sits a doc commit or two ahead
by convention. The app is feature-complete across its domains — the
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
process. On `rework` (`f757174`), suite 536+98, no gate — not yet deployed.
Then (Aug 12): **the Forecast lab** — port increment 1 from Alta & Charlee's
standalone scenario dashboards (the "Sorting Finances" Cowork handoff):
`derivations.forecast_baselines` + `GET /api/forecast/baselines` (measured
facts only; every what-if is client-side, nothing scenario-shaped stored) + an
Analytics-tab card with 0–200% category sliders, an income override, a
6/12/24-mo horizon, and a hand-rolled SVG cumulative line. On
`claude/scenario-planning-ledger-4lt781`; suite 550+111, gate PASS zero-diff
(synthetic dev.db — re-gate on the Pi before deploy). Port increment 2 (same
day, same branch): **Goals-tab pace line + per-goal what-if** — frontend-only
over the already-deployed `goal_pace` endpoint (pace sentence per card; type a
$/mo, months-to-finish recomputes client-side, never stored). Suite 550+115,
no gate (no money path). Port increment 3: **savings target + "Suggest cuts"
optimizer** riding the lab's sliders (greedy 75%→50% walk, only lowers, honest
give-up; suggestions land as real slider state, undoable per slider) —
frontend-only, suite 550+123. That completes the planned port scope (the
brief's not-ported list is recorded in the log). None of it deployed yet.
Scenario port merged to `main` (Aug 12, PR #10, `8074ffa`) — still awaiting the
Pi deploy. Then (Aug 19): **recategorize from the Home "Spent" section** — tap a
spent-category row → a bottom sheet lists that category's txns this month as a
checklist (with select-all) → move them into a new/existing category. Since
categories are emergent transaction tags (no `categories` table), "create a
category" = retag: frontend + the existing `edit_transaction` verb, NO schema
change. `/api/activity` gained an optional exact `category` filter. A
category-only edit relabels only (splits/balance/month-total unchanged, proven).
On `claude/recategorize-from-spent-4lt781`; suite 554+127, gate PASS zero-diff
(synthetic dev.db — re-gate on the Pi before deploy). **DEPLOYED (Aug 19):**
Alta ran `deploy.sh origin/main` on the Pi — one deploy took the tree from
`a6adc03` to `0824348`, shipping BOTH the scenario port (PR #10) and
recategorize (PR #16) live; live real-data gate PASS zero-diff, schema still
v11. `origin/main` == the deployed tree again; nothing merged-but-undeployed
remains. Then (Aug 20): **removed the Forecast lab** — product call, the
Analytics-tab scenario what-if card (sliders/income override/horizon/target/
"Suggest cuts") wasn't earning its place; its grounded cousins already exist
(Budgets, the Cash-flow forecast card, the Savings-rate trend). Deleted
`forecast_baselines` + `/api/forecast/baselines` + the whole lab frontend;
KEPT the Goals what-if, recategorize, the cash-flow card, and pantry
`restock_forecast`. On `claude/remove-forecast-lab-4lt781`; suite 540+106, gate
PASS zero-diff. **DEPLOYED (Aug 20, PR #18):** Alta ran `deploy.sh origin/main`
on the Pi, tree `0824348`→`cd878ad`, live real-data gate PASS zero-diff, schema
still v11. `origin/main` == the deployed tree; nothing merged-but-undeployed
remains. Then (Aug 20): **settle-up breakdown** — a "why is it this amount"
disclosure in the settle dialog. `derivations.settle_breakdown` takes the
authoritative figure from `compute_balance` and itemizes the unsettled shared
expenses behind it; a `carryover_cents` residual (signed_balance − Σ open
lines) guarantees **lines + carryover == the balance to the cent** even on
legacy/seed data whose settlements link no rows (0 on clean `settle_up`
history). `GET /api/settle/breakdown` + a viewer-aware ledger in `dlg-settle`.
A live browser smoke caught a reconciliation gap the unit tests missed (seed
settlements without links) → the carryover fix. On
`claude/settle-breakdown-4lt781`; suite 554+113, gate PASS zero-diff (synthetic
dev.db). DEPLOYED (Aug 20, PR #20, tree `cd878ad`→`9016b30`, live gate PASS
zero-diff, schema still v11). Then (Aug 20): **transfer-neutral fix, increment
T1** — for SimpleFIN mis-signs (a credit-card "Payment Thank You" posts positive
→ lands as income when it's really a transfer). Migration `012_transfer_flag`
adds a direction-agnostic `is_transfer` flag (NOT NULL DEFAULT 0); the money
derivations (`compute_balance`, `spending_summary`, `income_summary`,
`member_breakdown`, `settle_breakdown`) gain `AND is_transfer = 0`. Schema v11 →
**v12**. No verb sets it yet (that's T2), so **gate PASS by enumeration** — the
sole diff is schema_version 11→12 (`notes/012-gate-expectation.seed.json`); every
money number byte-identical. On `claude/transfer-flag-t1-4lt781`; suite 561+113
(merged to main, PR #22, `0b0b93c`; not yet deployed). Then **increment T2** —
the mechanism: verb `set_transfer(db, actor, txn_id, is_transfer)` (flag-only,
reversible, rejects settlements, audited; registered in CORE-DESIGN.md) + `PUT
/api/transactions/<id>/transfer` + `/api/activity` gaining `is_transfer` and
hiding transfers from the spending/income filters + a "Mark as transfer" toggle
in the classify & edit dialogs (the weak "Transfer" income-type button dropped)
+ neutral 🔁 rendering. On `claude/transfer-flag-t2-4lt781`; suite 571+113, gate
PASS zero-diff (no schema change), browser-smoke verified (marking the real
"Payment Thank You" case dropped gross income $270.36→$0, row went neutral). That
completes the transfer-neutral fix; T3 (auto-tag / account-aware sync) is
optional/deferred. **DEPLOYED (Aug 20, PRs #22+#23, tree `9016b30`→`d04fa62`):**
Alta ran `deploy.sh origin/main` on the Pi — the live gate passed with the sole
enumerated diff `schema_version 11→12`, migration `012` applied to the live DB
(**live schema now v12**), balance/monthly totals byte-identical. `origin/main`
== the deployed tree; nothing merged-but-undeployed remains. Then (Aug 21):
**transfer auto-tag, increment T3a** — so a recurring "Payment Thank You" gets a
RULE instead of a manual mark each cycle. Migration `013_rule_set_transfer` adds
`set_transfer` to `income_rules` (NOT NULL DEFAULT 0); `record_transaction` (sync)
and `apply_rules` (retroactive backlog) set `is_transfer=1` when a matched rule
carries it (`is_transfer` stays the single source of truth — Approach A over
coupling income_type). Schema v12 → **v13**. No verb sets the flag yet (T3b), so
**gate PASS by enumeration** — sole diff schema_version 12→13
(`notes/013-gate-expectation.seed.json`). On `claude/rule-transfer-t3a-4lt781`;
suite 576+113 (merged to main, PR #25, `28aae38`; not deployed). Then
**increment T3b** — the mechanism: `create_income_rule` accepts `set_transfer`
(a transfer rule is just `{match_desc, set_transfer:1}`, set_type defaults to
'transfer') + `suggest_transfer_rule_after_mark` (offers a pre-filled rule at
the 2nd transfer, wait-for-a-repeat) surfaced as the `set_transfer` route's
`rule_suggestion`, chained into the rule dialog after Mark-as-transfer (transfer
copy via `transferRuleText`). Creating it sweeps the unclassified backlog
(`apply_rules`, T3a) + self-flags future syncs. On
`claude/rule-transfer-t3b-4lt781`; suite 582+114, gate PASS zero-diff (no schema
change), browser-smoke verified (2nd "Payment Thank You" mark fired the nudge,
created a `set_transfer` rule). **That completes the transfer-neutral fix
(T1–T3): mark once, then a rule auto-tags the rest.** **DEPLOYED (Aug 21, PRs
#25+#26, tree `d04fa62`→`4dc262a`):** Alta ran `deploy.sh origin/main` on the Pi
— the live gate passed with the sole enumerated diff `schema_version 12→13`,
migration `013` applied to the live DB (**live schema now v13**),
balance/monthly totals byte-identical. `origin/main` == the deployed tree;
nothing merged-but-undeployed remains. Then (Aug 21): **transfer consistency for
the merchant/pantry views** — closed T1's deliberate scope boundary by adding
`AND is_transfer = 0` to every remaining outflow-reading derivation
(`top_merchants`, `recurring_charges`, `new_staple_suggestions`,
`last_shopping_trip`, `_purchase_index` + `_matching_purchases`), so a marked
transfer outflow no longer shows as a top merchant / recurring subscription /
pantry purchase. Pure read change, no schema/verb/money path. On
`claude/transfer-merchant-consistency-4lt781`; suite 585+114, gate PASS
zero-diff. **DEPLOYED (Aug 21, PR #28, tree `4dc262a`→`e4a728f`):** Alta ran
`deploy.sh origin/main` on the Pi, live gate PASS zero-diff, no migration
(schema still v13). `origin/main` == the deployed tree; nothing
merged-but-undeployed remains. That closes the transfer effort end to end and
live. Meanwhile on the rework lineage: (Aug 18) the Pi's `ANTHROPIC_API_KEY`
rotated on schedule — new expiry 2026-09-17, ops guardian green (see
PROGRESS-LOG); (Aug 20) the **Pantry v2 amendment** landed in
INVENTORY-DESIGN.md — parameter grammar codified, status-transition cadence,
migration #014 nouns, `restock_items`, money tie-in, trip composition,
hygiene layer, a 7-step build order; and (Aug 20) `rework` was synced with
`origin/main` (merge `360291c`), so `rework` again contains the deployed tree.
Then (Aug 20): **Pantry v2 increment 1 — `restock_items`**, the after-shopping
batch verb ("we got everything"): all-or-nothing validation, per-item audit
rows via a helper shared with `set_item_status`, callers = thin route + a
"Got everything (N)" shopping-card button + `ledger_restock_items` in the Ask
write tier; PARAM_SPECS gained array support. Suite 589+125, GATE PASS
zero-diff, no schema change. **DEPLOYED** (`main` `6b3fba9`, live gate PASS zero-diff, tailnet-verified; the deploy also revealed PR #28 had never actually been live — shipped now). Then (Aug 20): **`merge_category`** — delete/merge/rename a
category orphan-proof: one atomic relabel of every reference (transactions all
months, bills, pantry, budget follows-or-retires) into a required destination;
Settlement protected; UI = a delete zone in the recategorize sheet ("add" is
already emergent — type a new name at retag time). Suite 595+126, GATE PASS
zero-diff, no schema change. **DEPLOYED** (`main` `a40e9ff`, live no-op
guard confirmed the apply; tailnet-verified: route 401s, JS carries the
delete zone). Then (Aug 20): **Pantry v2 increment 2 — migration #014**
(`items.store` / `need_by` / `snoozed_until`, schema v13→v14) + the three
metadata setters (no `updated_at` bump — inference-bound rule), the
store-grouped/deadline-sorted shopping list, view-layer snooze with a Wake
drawer, and three new Ask tools (19+10). Suite 600+129, GATE PASS by
enumeration (sole diff schema_version 13→14). **DEPLOYED** (`main`
`87c1fe7`, migration 014 applied live — **live schema v14**; tailnet-verified). Then (Aug 21): **Pantry v2 increment 3 — the status-derived
cadence**: `item_history` (the audit log's status timeline, named) +
`restock_forecast`'s third rung (manual → status → purchase-median; median
stocked→low/out cycle, ≥ 2 cycles, anchored at `last_stocked_at`), with the
forecast card attributing it ("from your last K cycles"). No migration.
Suite 604+129, GATE PASS zero-diff. **DEPLOYED** (`main` `7c32262`, third
attempt — two pushes lost races to same-day `claude/*` PRs #32/#33;
tailnet-verified). `origin/main` == the deployed tree. Then (Aug 21): **Pantry v2
increment 4 — `list_estimate` + the price trend**: the shopping list priced
from median restock cost (unpriced lines stay honest; coverage reported) and
`staple_spend` drift in basis points, surfaced as "This trip ≈ $X" and ↑/↓
badges; Ask's inventory tool taught the questions. Suite 608+135, GATE PASS
zero-diff. **DEPLOYED** (`main` `7f5334a`, alongside the cloud lineage's PR
#34 billRow extraction folded in pre-push; tailnet-verified). `origin/main`
== the deployed tree. Then (Aug 21): **Pantry v2 increment 5 — the trip
composition**: `trip_plan` (the priced list + `due_soon` stocked staples with
store/price — "also grab while you're out") and `trip_closure` (restock hints
grouped by the purchase behind them → one "Yes, restocked all N" feeding
`restock_items`); estimate priced two ways; generic nudge yields to a concrete
closure card. Reads only. Suite 611+138, GATE PASS zero-diff. **DEPLOYED**
(`main` `ba008a1`, first try; tailnet-verified). `origin/main` == the deployed
tree. Then (Aug 21): **Pantry v2 increment 6 — the hygiene layer**:
`stale_staples` (curation guard, 180-day view grace, "Still tracking
these?"), `pantry_pulse` (the weekly digest named; route + Ask/MCP read tool,
20+10), the Pi-side pulse job (`deploy/pantry_pulse.py` + weekly units +
install doc — the cloud can't reach the Pi; **awaits Alta's install**), and
the Garden line via `GET /api/inventory/badge` (the dashboard payload is
frozen to v1 — parity test enforced it). Suite 618+147, GATE PASS zero-diff.
**DEPLOYED** (`main` `6b6bbf5`, with PRs #35/#36 folded in pre-push;
tailnet-verified). `origin/main` == the deployed tree. **Pantry v2 increments
1–6 are live, the Sunday pantry-pulse timer is installed on the Pi (first
issue #37 filed Aug 21); 7 (the `ordered` status) awaits Alta/Charlee's
call.**

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
